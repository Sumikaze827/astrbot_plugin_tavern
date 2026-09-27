"""状态(status_ops)更新策略回归测试。

背景：状态写入依赖模型每次发 status_ops，且旧代码在「目标缺 runtime 行」
时静默跳过（状态更新不上），remove 用精确名匹配（措辞略异就取消不了）。
本次修复：①缺 runtime 行自动补一条；②remove 用包含匹配（前缀/子串）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now


def _insert_participant(connection, session_id: str, pid: str, name: str) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO participants(
            id, session_id, player_id, group_user_id, private_user_id,
            private_origin, display_name, character_card_id,
            character_version_id, character_name, character_code, aliases_json,
            card_status, ready, participation_status, seat_reserved_at,
            joined_round, consecutive_timeouts, exit_reason, created_at,
            updated_at, action_locked
        ) VALUES (?, ?, NULL, ?, '', '', ?, NULL, NULL, ?, ?, '[]',
                  'approved', 1, 'active', ?, 1, 0, '', ?, ?, 0)
        """,
        (pid, session_id, pid, name, name, name, now, now, now),
    )


def _insert_runtime(connection, session_id: str, pid: str, state: dict) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO character_runtime_states(
            id, session_id, participant_id, character_card_id,
            state_json, revision, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, ?, 1, ?, ?)
        """,
        (
            new_id("runtime"),
            session_id,
            pid,
            json.dumps(state, ensure_ascii=False),
            now,
            now,
        ),
    )


class StatusOpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-status", "qq:group-status", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _call_v05(self, participant_id: str, workflow: dict) -> dict:
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_row = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (self.session["id"],)
            ).fetchone()
            participant_row = connection.execute(
                "SELECT * FROM participants WHERE id=?", (participant_id,)
            ).fetchone()
            result = self.database._apply_v05_turn_ops(
                connection,
                session=session_row,
                participant=participant_row,
                new_turn=7,
                acting_round=1,
                source_event_id="event_status_test",
                workflow=workflow,
                check_payload={},
                now=now,
            )
            connection.execute("COMMIT")
        return result

    def _statuses(self, participant_id: str):
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM character_runtime_states"
                " WHERE participant_id=?",
                (participant_id,),
            ).fetchone()
            if not row:
                return None
            return json.loads(row[0]).get("statuses", [])

    def _workflow(self, status_ops: list[dict]) -> dict:
        return {
            "status_ops": status_ops,
            "npc_ops": [],
            "clock_ops": [],
            "ledger_ops": [],
            "assist_ops": [],
        }

    async def test_add_status_without_runtime_row_creates_row(self) -> None:
        """目标没有 runtime 行时，自动补行并写入状态（不再静默丢弃）。"""
        pid = new_id("participant")
        with self.database._connect() as connection:
            _insert_participant(connection, self.session["id"], pid, "测试角色")

        self._call_v05(
            pid,
            self._workflow([
                {
                    "op": "add",
                    "target_id": pid,
                    "name": "左脚扭伤",
                    "severity": "minor",
                    "removal": "休息一天",
                }
            ]),
        )
        statuses = self._statuses(pid)
        self.assertIsNotNone(statuses)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["name"], "左脚扭伤")

    async def test_remove_status_with_prefix_name_match(self) -> None:
        """remove 措辞为存量名的前缀（位置暴露 → 位置暴露风险）也能删掉。"""
        pid = new_id("participant")
        with self.database._connect() as connection:
            _insert_participant(connection, self.session["id"], pid, "柏辰")
            _insert_runtime(
                connection,
                self.session["id"],
                pid,
                {"statuses": [{"name": "位置暴露风险", "severity": "minor"}]},
            )

        self._call_v05(
            pid,
            self._workflow([
                {"op": "remove", "target_id": pid, "name": "位置暴露"}
            ]),
        )
        self.assertEqual(self._statuses(pid), [])

    async def test_remove_status_with_exact_but_missing_does_no_harm(self) -> None:
        """remove 一个不存在的状态不报错，也不影响其他状态。"""
        pid = new_id("participant")
        with self.database._connect() as connection:
            _insert_participant(connection, self.session["id"], pid, "提丰")
            _insert_runtime(
                connection,
                self.session["id"],
                pid,
                {"statuses": [{"name": "受伤", "severity": "serious"}]},
            )

        self._call_v05(
            pid,
            self._workflow([
                {"op": "remove", "target_id": pid, "name": "完全不存在的状态"}
            ]),
        )
        statuses = self._statuses(pid)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["name"], "受伤")

    async def test_update_status_exact_match_replaces(self) -> None:
        """add/update 用精确匹配替换同名状态，不用包含匹配合并。"""
        pid = new_id("participant")
        with self.database._connect() as connection:
            _insert_participant(connection, self.session["id"], pid, "jade")
            _insert_runtime(
                connection,
                self.session["id"],
                pid,
                {"statuses": [{"name": "受伤", "severity": "minor"}]},
            )

        self._call_v05(
            pid,
            self._workflow([
                {
                    "op": "update",
                    "target_id": pid,
                    "name": "受伤",
                    "severity": "critical",
                }
            ]),
        )
        statuses = self._statuses(pid)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["severity"], "critical")

    async def test_cure_removes_only_canonical_condition_after_commit(self) -> None:
        from tavern.engine import TavernEngine
        pid = new_id("participant")
        stored = [{"name": "中毒", "severity": "serious"},
                  {"name": "中毒诅咒", "policy_source": "world", "healing_policy": "story_locked"}]
        with self.database._connect() as connection:
            _insert_participant(connection, self.session["id"], pid, "柏辰")
            _insert_runtime(connection, self.session["id"], pid, {"statuses": stored})
        ops = TavernEngine._healing_status_ops(
            player_input="治疗柏辰的中毒", outcome="success",
            status_ops=[{"op": "update", "target_id": pid, "name": "中毒", "severity": "minor"}],
            roster=[{"id": pid, "character_name": "柏辰", "runtime_state": {"statuses": stored}}],
            acting_participant=None,
        )
        self._call_v05(pid, self._workflow(ops))
        self.assertEqual([s["name"] for s in self._statuses(pid)], ["中毒诅咒"])


if __name__ == "__main__":
    unittest.main()
