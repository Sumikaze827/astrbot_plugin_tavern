"""v0.12.0-A16 回归测试。

覆盖：
1. 统一实体解析器（participant / 裸 UUID 后缀 / 队伍 / 降级名称）。
2. 关系写入规范化（relationship_ops source/target 不再裸 UUID）。
3. 可选经济系统（开关 / 货币播种 / 收支 / 转账 / 余额不足 / 幂等 / 兑换）。
4. 角色托管（授权 / 权限 / 过期 / 恢复 / 强制）。
5. actor_id 修复（校验失败进入兜底而非硬失败）。
6. 人工 DM 受控操作（插入剧情 / 公告 / 密语 / 锁定 / 关系调整 / 强制结束投票）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.backup_service import build_backup_archive
from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD_SLUG
from tavern.database import TavernDatabase
from tavern.database_support import json_dump, new_id, utc_now
from tavern.entity_resolver import (
    build_entity_labels,
    build_participant_labels,
    fallback_name,
    normalize_relationship_ops,
    resolve_label,
    strip_prefix,
)
from tavern.engine import TavernEngine
from tavern.errors import EconomyDisabledError
from tavern.events import EventBroker


def _econ_world() -> dict:
    world = {
        "slug": "econ-test",
        "name": "经济测试世界",
        "world_schema_version": 5,
        "minimum_plugin_version": "0.11.0",
        "rules": {
            "world_schema_version": 5,
            "economy": {
                "currencies": [
                    {
                        "currency_id": "gold",
                        "name": "金币",
                        "short_name": "G",
                        "precision": 2,
                        "transferable": True,
                        "exchangeable": True,
                    },
                    {
                        "currency_id": "silver",
                        "name": "银币",
                        "short_name": "S",
                        "precision": 0,
                    },
                ],
                "initial_wallets": [
                    {
                        "owner_type": "party",
                        "owner_ref": "party",
                        "currency_id": "gold",
                        "amount": "100.00",
                    }
                ],
                "exchange_rules": [
                    {
                        "from": "gold",
                        "to": "silver",
                        "rate_numerator": 10,
                        "rate_denominator": 1,
                        "fee": 0,
                    }
                ],
            },
        },
    }
    return world


class A16EntityResolverTests(unittest.TestCase):
    def test_resolve_participant_and_suffix(self) -> None:
        labels = {
            "participant_abcdef123456": {
                "id": "participant_abcdef123456",
                "type": "participant",
                "name": "白鸦",
                "source": "participant",
                "deleted": False,
                "departed": False,
            },
            "队伍": {"name": "队伍", "type": "team", "id": "team"},
        }
        self.assertEqual(
            resolve_label(labels, "participant_abcdef123456")["name"], "白鸦"
        )
        # 裸 uuid 后缀匹配
        self.assertEqual(resolve_label(labels, "abcdef123456")["name"], "白鸦")
        # 队伍
        self.assertEqual(resolve_label(labels, "队伍")["name"], "队伍")
        # 未知 → 降级名称（不返回完整内部 ID）
        result = resolve_label(labels, "ea54fbebeaf843f4ac2bcf0feb254261")
        self.assertTrue(result["fallback"])
        self.assertNotIn("ea54fbebeaf843f4ac2bcf0feb254261", result["name"])

    def test_fallback_names(self) -> None:
        self.assertEqual(fallback_name("player_xxx"), "已离开玩家")
        self.assertEqual(fallback_name("npc_xxx"), "已删除实体")
        self.assertEqual(fallback_name("队伍"), "队伍")
        self.assertIn("未知实体", fallback_name("ea54fbbe"))

    def test_npc_colon_prefix_resolves(self) -> None:
        labels = build_entity_labels(
            [],
            [
                {"id": "snpc_abc123", "stable_key": "world:character_abc123",
                 "name": "林语者莎芮"},
            ],
            [],
        )
        self.assertEqual(resolve_label(labels, "npc:林语者莎芮")["name"], "林语者莎芮")
        self.assertEqual(resolve_label(labels, "林语者莎芮")["name"], "林语者莎芮")
        self.assertEqual(resolve_label(labels, "snpc_abc123")["name"], "林语者莎芮")
        # 世界角色
        labels2 = build_entity_labels([], [], [{"name": "米拉"}])
        self.assertEqual(resolve_label(labels2, "npc:米拉")["name"], "米拉")
        # 队伍
        self.assertEqual(resolve_label(labels2, "队伍")["name"], "队伍")

    def test_normalize_relationship_ops(self) -> None:
        roster = [
            {
                "id": "participant_abc",
                "group_user_id": "user-1",
                "character_name": "白鸦",
            }
        ]
        labels = build_participant_labels(roster)
        ops = normalize_relationship_ops(
            [
                {"source": "abc", "target": "队伍", "dimension": "信任", "delta": 1},
                {"source": "ea54fbbe", "target": "白鸦", "dimension": "信任", "delta": 2},
            ],
            labels,
        )
        self.assertEqual(ops[0]["source"], "participant_abc")
        self.assertEqual(ops[0]["target"], "队伍")
        # 白鸦 → 规范化为 participant id
        self.assertEqual(ops[1]["target"], "participant_abc")
        # 无法解析的裸 uuid 前缀化
        self.assertEqual(ops[1]["source"], "participant_ea54fbbe")


class A16DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data_dir = Path(self.temp.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq", "group-a16", "qq:group-a16", DEFAULT_WORLD_SLUG, "admin-1"
        )
        await self.database.transition_session(
            self.session["id"], "preparing", "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _seed_economy(self) -> None:
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE instance_configs SET world_snapshot_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (json_dump(_econ_world()), now, self.session["id"]),
            )
            connection.execute("COMMIT")

    async def test_economy_enable_seeds_currencies_and_wallets(self) -> None:
        await self._seed_economy()
        state = await self.database.economy_state(self.session["id"])
        self.assertFalse(state["enabled"])
        await self.database.set_economy_enabled(
            self.session["id"], True, "admin-1"
        )
        summary = await self.database.economy_summary(self.session["id"])
        self.assertTrue(summary["enabled"])
        self.assertEqual(
            {c["currency_id"] for c in summary["currencies"]},
            {"gold", "silver"},
        )
        party_gold = next(
            w
            for w in summary["wallets"]
            if w["owner_type"] == "party" and w["currency_id"] == "gold"
        )
        self.assertEqual(party_gold["balance"], 10000)  # 100.00 * 100

    async def test_economy_credit_debit_transfer_idempotent(self) -> None:
        await self._seed_economy()
        await self.database.set_economy_enabled(self.session["id"], True, "admin-1")
        sid = self.session["id"]
        # 队伍 → 玩家转账
        r1 = await self.database.economy_transfer(
            session_id=sid,
            operation_id="tx-1",
            currency_id="gold",
            from_owner=("party", "party"),
            to_owner=("player", "user-1"),
            amount="10.00",
            reason="工资",
            actor_id="admin-1",
        )
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["amount"], 1000)
        # 幂等重放
        r2 = await self.database.economy_transfer(
            session_id=sid,
            operation_id="tx-1",
            currency_id="gold",
            from_owner=("party", "party"),
            to_owner=("player", "user-1"),
            amount="10.00",
            reason="工资",
            actor_id="admin-1",
        )
        self.assertTrue(r2.get("idempotent_replay"))
        bal = await self.database.economy_balance(
            sid, "player", "user-1", "gold"
        )
        self.assertEqual(bal["balance"], 1000)
        # 余额不足
        r3 = await self.database.economy_apply(
            session_id=sid,
            operation_id="tx-2",
            kind="debit",
            currency_id="gold",
            amount="999999.00",
            from_owner=("player", "user-1"),
        )
        self.assertFalse(r3["ok"])
        self.assertEqual(r3["reason"], "insufficient_funds")
        # 未启用时拒绝
        await self.database.set_economy_enabled(sid, False, "admin-1")
        with self.assertRaises(EconomyDisabledError):
            await self.database.economy_balance(sid, "player", "user-1", "gold")

    async def test_economy_exchange(self) -> None:
        await self._seed_economy()
        await self.database.set_economy_enabled(self.session["id"], True, "admin-1")
        sid = self.session["id"]
        result = await self.database.economy_exchange(
            session_id=sid,
            operation_id="ex-1",
            currency_id="gold",
            amount="1.00",
            from_owner=("party", "party"),
            to_owner=("party", "party", "silver"),
            actor_id="admin-1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["to_currency"], "silver")
        self.assertEqual(result["to_amount"], "10")

    async def test_delegation_lifecycle(self) -> None:
        reserved = await self.database.reserve_participant(
            self.session["id"], "owner-1", "主人"
        )
        grant = await self.database.grant_delegation(
            self.session["id"],
            "owner-1",
            "delegate-1",
            "owner-1",
            permissions=["choose", "vote"],
            expiry_kind="none",
            source="player",
        )
        self.assertEqual(grant["status"], "active")
        control = await self.database.authorize_participant_control(
            self.session["id"], reserved["id"], "delegate-1", "choose"
        )
        self.assertTrue(control["authorized"])
        self.assertEqual(control["mode"], "delegate")
        # 无权限
        control2 = await self.database.authorize_participant_control(
            self.session["id"], reserved["id"], "delegate-1", "modify_permanent"
        )
        self.assertFalse(control2["authorized"])
        # 恢复原玩家
        await self.database.restore_owner_control(
            self.session["id"], reserved["id"], "admin-1"
        )
        control3 = await self.database.authorize_participant_control(
            self.session["id"], reserved["id"], "delegate-1", "choose"
        )
        self.assertFalse(control3["authorized"])

    async def test_save_player_syncs_participants(self) -> None:
        """A16.3：players/save 更新角色名时同步 participants，避免两边名字不一致。"""
        reserved = await self.database.reserve_participant(
            self.session["id"], "owner-sync", "旧名"
        )
        await self.database.join_turn_order(
            self.session["id"], "owner-sync", "旧名", "owner-sync"
        )
        await self.database.save_player(
            {
                "session_id": self.session["id"],
                "user_id": "owner-sync",
                "display_name": "群昵称",
                "character_name": "新名",
                "profile": {"name": "新名"},
                "enabled": True,
            },
            "admin-1",
        )
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT character_name FROM participants WHERE id = ?",
                (reserved["id"],),
            ).fetchone()
        self.assertEqual(row["character_name"], "新名")
        turn = await self.database.get_turn_status(self.session["id"])
        self.assertEqual(
            turn["current_name"] or "",
            "新名",
        )

    async def test_dm_operations(self) -> None:
        sid = self.session["id"]
        reserved = await self.database.reserve_participant(
            sid, "dm-target", "目标角色"
        )
        await self.database.transition_session(sid, "running", "admin-1")
        n = await self.database.insert_dm_narrative(sid, "DM 插入的一段剧情。", "dm-1")
        self.assertTrue(n["event_id"])
        a = await self.database.publish_announcement(sid, "系统公告。", "dm-1")
        self.assertTrue(a["event_id"])
        w = await self.database.whisper_to(
            sid, "只有你能看到", reserved["id"], "dm-1"
        )
        self.assertTrue(w["event_id"])
        await self.database.set_action_lock(sid, reserved["id"], True, "dm-1")
        await self.database.set_input_lock(sid, True, "dm-1")
        with self.database._connect() as connection:
            locked = connection.execute(
                "SELECT input_locked FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        self.assertEqual(int(locked["input_locked"]), 1)
        rel = await self.database.apply_relationship_delta(
            sid, "队伍", "npc:某人", "信任", 2, "dm-1"
        )
        self.assertEqual(rel["delta"], 2)


class A16ActorRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-a16r", "qq:group-a16r", DEFAULT_WORLD_SLUG, "admin-1"
        )
        await self.database.transition_session(
            self.session["id"], "preparing", "admin-1"
        )
        self.reserved = await self.database.reserve_participant(
            self.session["id"], "actor-1", "行动者"
        )
        await self.database.join_turn_order(
            self.session["id"], "actor-1", "行动者", "actor-1"
        )
        await self.database.transition_session(
            self.session["id"], "running", "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _engine(self) -> TavernEngine:
        context = SimpleNamespace()
        context.llm_generate = None
        context.get_current_chat_provider_id = None
        return TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(),
            broker=EventBroker(),
        )

    async def test_ensure_next_choices_falls_back_on_bad_actor_id(self) -> None:
        """actor_id 不一致时不再硬失败：进入再生/兜底路径并强制 actor_id。"""
        reserved = self.reserved
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO choice_sets(
                    id, session_id, participant_id, round_no,
                    session_revision, choices_json, status, reroll_count,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 1, ?, 'active', 0, 'k1', ?, ?)
                """,
                (
                    new_id("choices"),
                    self.session["id"],
                    reserved["id"],
                    json_dump(
                        [{"key": "A", "text": "行动", "risk": "safe"}]
                    ),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")
        engine = self._engine()
        roster = await self.database.list_roster(self.session["id"])
        turn = await self.database.get_turn_status(self.session["id"])
        session = await self.database.get_session(self.session["id"])
        session = dict(session)
        session["roster"] = roster
        session["turn_status"] = turn
        session["next_actor"] = engine._next_actor(turn, roster)
        from tavern.resolution import Resolution

        resolution = Resolution(
            mode="resolve",
            narrative="叙事正文。",
            check=None,
            state_patch={},
            memories=(),
            next_choices=(
                {"key": "A", "text": "错误角色选项", "actor_id": "participant_wrong"},
            ),
            group_decision=None,
            return_progress=None,
            npc_ops=(),
            clock_ops=(),
            ledger_ops=(),
            location_ops=(),
            status_ops=(),
            assist_ops=(),
            director_note="",
            raw={"next_choices": [{"key": "A", "text": "错误角色选项", "actor_id": "participant_wrong"}]},
        )
        participant = session["next_actor"]
        result = await engine._ensure_next_choices(
            resolution=resolution,
            provider_ids=(),
            world=DEFAULT_WORLD_SLUG and await self.database.get_world(DEFAULT_WORLD_SLUG),
            session=session,
            participant=participant,
            roster=roster,
            events=[],
            candidate_state=session.get("world_state") or {},
            config=TavernConfig(),
        )
        self.assertTrue(result.next_choices)
        expected_id = str(participant.get("id") or "")
        self.assertTrue(
            all(str(c.get("actor_id") or "") == expected_id for c in result.next_choices)
        )


_A162_WC_FN = {}


class A16WebConsoleRegressTests(unittest.TestCase):
    """A16.2 回归：web_console._with_token_context 运行时不再 NameError。

    根因：该函数体使用 collections.abc.Mapping，但 web_console.py 未导入，
    py_compile 无法发现（运行时名字错误）；此测试直接调用以守卫。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import types as _types
        from pathlib import Path as _Path

        astrbot = _types.ModuleType("astrbot")
        api = _types.ModuleType("astrbot.api")
        event = _types.ModuleType("astrbot.api.event")
        star = _types.ModuleType("astrbot.api.star")
        web = _types.ModuleType("astrbot.api.web")

        class Logger:
            def debug(self, *a, **k): return None
            info = debug
            warning = debug
            exception = debug

        class AstrBotConfig(dict):
            def save_config(self): return None

        def response(value=None, *a, **k): return value

        api.AstrBotConfig = AstrBotConfig
        api.logger = Logger()
        event.AstrMessageEvent = type("AstrMessageEvent", (), {})
        event.MessageChain = type("MessageChain", (), {"message": lambda self, t: self})
        star.Context = type("Context", (), {})
        star.Star = type("Star", (), {})
        star.StarTools = type("StarTools", (), {"get_data_dir": staticmethod(lambda n: _Path("."))})
        web.PluginUploadFile = type("PluginUploadFile", (), {})
        web.error_response = response
        web.file_response = response
        web.json_response = response
        web.stream_response = response
        web.request = _types.SimpleNamespace(username=None)
        astrbot.api = api
        import sys as _sys
        _sys.modules["astrbot"] = astrbot
        _sys.modules["astrbot.api"] = api
        _sys.modules["astrbot.api.event"] = event
        _sys.modules["astrbot.api.star"] = star
        _sys.modules["astrbot.api.web"] = web

        from tavern.web_console import _with_token_context
        _A162_WC_FN["fn"] = _with_token_context

    def test_with_token_context_extracts_budget(self) -> None:
        usage = {"session": {"hour": 10}, "quotas": [], "by_type": []}
        instance = {
            "world_snapshot": {
                "rules": {"context_budget": {"recent_turns": 6, "memory_limit": 6}},
            }
        }
        result = _A162_WC_FN["fn"](usage, instance)
        self.assertEqual(result["context_budget"]["recent_turns"], 6)
        self.assertEqual(result["last_trim_at"], "")

    def test_with_token_context_tolerates_empty_instance(self) -> None:
        result = _A162_WC_FN["fn"]({"session": {}}, None)
        self.assertEqual(result["context_budget"], {})


class A17RegressionTests(unittest.IsolatedAsyncioTestCase):
    """A17 回归：权限异步修复 / 投票字段 / 回合作废 / 操作幂等 / 可读名称。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-a17", "qq:group-a17", DEFAULT_WORLD_SLUG, "admin-1"
        )
        await self.database.transition_session(
            self.session["id"], "preparing", "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_permissions_host_path_no_coroutine_error(self) -> None:
        """A17：can_manage_dm 不再迭代协程；host 角色可管理副本。"""
        from tavern.permissions import can_adjust_economy, can_manage_dm
        from tavern.config import TavernConfig

        config = TavernConfig.from_mapping({})  # admin_ids 为空
        control = {"mode": "auto", "active_dm_user_id": ""}
        # 非管理员、非活动 DM、非 host → False（此前会抛 coroutine TypeError）
        ok = await can_manage_dm(
            self.database, config, self.session["id"], control, "stranger"
        )
        self.assertFalse(ok)
        # 授予 host 后 → True
        await self.database.grant_permission(
            self.session["id"], "host-user", "host", "admin-1"
        )
        ok = await can_manage_dm(
            self.database, config, self.session["id"], control, "host-user"
        )
        self.assertTrue(ok)
        # 经济调整：moderator 也允许
        await self.database.grant_permission(
            self.session["id"], "mod-user", "moderator", "admin-1"
        )
        self.assertTrue(
            await can_adjust_economy(
                self.database, config, self.session["id"], control, "mod-user"
            )
        )

    async def test_normalize_vote_fields(self) -> None:
        from tavern.dashboard import _normalize_vote

        vote = {
            "id": "vote-1",
            "title": "是否前进",
            "status": "open",
            "majority": 2,
            "eligible_user_ids": ["u1", "u2", "u3"],
            "options": [
                {"key": "A", "text": "前进"},
                {"key": "B", "text": "留守"},
            ],
            "ballots": [
                {"user_id": "u1", "choice_key": "A"},
                {"user_id": "u2", "choice_key": "A"},
            ],
        }
        out = _normalize_vote(vote)
        self.assertEqual(out["unvoted_user_ids"], ["u3"])
        self.assertEqual(out["options"][0]["votes"], 2)
        self.assertEqual(len(out["ballots"]), 2)
        self.assertIn("remaining_seconds", out)

    async def test_supersede_active_choices(self) -> None:
        reserved = await self.database.reserve_participant(
            self.session["id"], "turn-user", "回合角色"
        )
        await self.database.join_turn_order(
            self.session["id"], "turn-user", "回合角色", "turn-user"
        )
        from tavern.database_support import json_dump as _jd, new_id as _nid, utc_now as _now

        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO choice_sets("
                " id, session_id, participant_id, round_no, session_revision,"
                " choices_json, status, reroll_count, idempotency_key,"
                " created_at, updated_at"
                ") VALUES (?, ?, ?, 1, 1, ?, 'active', 0, 'k-a17', ?, ?)",
                (
                    _nid("choices"),
                    self.session["id"],
                    reserved["id"],
                    _jd(
                        [
                            {"key": "A", "text": "行动A", "risk": "safe"},
                            {"key": "B", "text": "行动B", "risk": "safe"},
                            {"key": "C", "text": "行动C", "risk": "safe"},
                            {"key": "D", "text": "行动D", "risk": "safe"},
                        ]
                    ),
                    _now(),
                    _now(),
                ),
            )
            connection.execute("COMMIT")
        count = await self.database.supersede_active_choices(
            self.session["id"], "admin-1"
        )
        self.assertGreaterEqual(count, 1)
        active = await self.database.active_choice_set(self.session["id"])
        self.assertIsNone(active)

    async def test_action_operation_idempotent(self) -> None:
        claim1 = await self.database.claim_action_operation(
            self.session["id"], "op-1", "forced_choose", "actor", "operator"
        )
        claim2 = await self.database.claim_action_operation(
            self.session["id"], "op-1", "forced_choose", "actor", "operator"
        )
        self.assertTrue(claim1["claimed"])
        self.assertTrue(claim2["replay"])

    def test_readable_name_generic_entity(self) -> None:
        from tavern.entity_resolver import resolve_label

        out = resolve_label({}, "鸦渡镇守备队")
        self.assertFalse(out.get("fallback"))
        self.assertEqual(out["name"], "鸦渡镇守备队")
        self.assertEqual(out["type"], "entity")
        opaque = resolve_label({}, "ea54fbebeaf843f4ac2bcf0feb254261")
        self.assertTrue(opaque.get("fallback"))
        self.assertIn("未知实体", opaque["name"])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
