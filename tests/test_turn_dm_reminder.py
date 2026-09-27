"""玩家迟迟不选时的私信催办（2026-09-20 新增功能）。

覆盖：
1. 时间规则默认值与归一化（5 分钟 / 30 分钟两档、开关、-1 关闭单档）。
2. ``due_dm_reminder_stage``：档位选择、去重、跨档只补最新一档。
3. ``private_notice_origin``：优先已建立的私聊来源，只在允许时兜底构造。
4. ``process_due_timers``：真实计时器行到点后只发一次，30 分钟再发一次，
   关闭开关后完全不发。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.database_support import utc_now
from tavern.lifecycle import (
    due_dm_reminder_stage,
    normalize_time_rules,
    turn_dm_reminder_thresholds,
)
from tavern.platform_delivery import private_notice_origin


class ThresholdTests(unittest.TestCase):
    def test_defaults_are_five_and_thirty_minutes(self):
        rules = normalize_time_rules({})
        self.assertTrue(rules["turn_dm_reminder_enabled"])
        self.assertEqual(
            turn_dm_reminder_thresholds(rules), [300, 1800]
        )

    def test_switch_off_and_custom_thresholds(self):
        rules = normalize_time_rules(
            {
                "turn_dm_reminder_enabled": False,
                "turn_dm_reminder_seconds": 120,
                "turn_dm_reminder_repeat_seconds": 600,
            }
        )
        self.assertFalse(rules["turn_dm_reminder_enabled"])
        self.assertEqual(turn_dm_reminder_thresholds(rules), [120, 600])

    def test_minus_one_disables_a_single_stage(self):
        rules = normalize_time_rules(
            {
                "turn_dm_reminder_seconds": 300,
                "turn_dm_reminder_repeat_seconds": -1,
            }
        )
        self.assertEqual(turn_dm_reminder_thresholds(rules), [300])

    def test_duplicate_thresholds_collapse(self):
        rules = normalize_time_rules(
            {
                "turn_dm_reminder_seconds": 600,
                "turn_dm_reminder_repeat_seconds": 600,
            }
        )
        self.assertEqual(turn_dm_reminder_thresholds(rules), [600])

    def test_stage_selection(self):
        thresholds = [300, 1800]
        self.assertIsNone(due_dm_reminder_stage(299, thresholds))
        self.assertEqual(due_dm_reminder_stage(300, thresholds), 1)
        self.assertEqual(due_dm_reminder_stage(1799, thresholds), 1)
        self.assertEqual(due_dm_reminder_stage(1800, thresholds), 2)
        # 第一档已发过，到点只补第二档。
        self.assertEqual(
            due_dm_reminder_stage(1800, thresholds, [1]), 2
        )
        self.assertIsNone(due_dm_reminder_stage(1800, thresholds, [1, 2]))
        # 一次跨过多档（暂停后恢复）只发最新一档，避免刷屏。
        self.assertEqual(due_dm_reminder_stage(7200, thresholds), 2)
        self.assertIsNone(due_dm_reminder_stage(None, thresholds))
        self.assertIsNone(due_dm_reminder_stage(9999, []))


class PrivateOriginTests(unittest.TestCase):
    def test_prefers_recorded_private_origin(self):
        origin = private_notice_origin(
            {"platform_id": "onebot"},
            [
                {"user_id": "123", "private_origin": ""},
                {"user_id": "456", "private_origin": "onebot:FriendMessage:456"},
            ],
            allow_constructed=True,
        )
        self.assertEqual(origin, "onebot:FriendMessage:456")

    def test_constructs_only_when_allowed(self):
        session = {"platform_id": "onebot"}
        targets = [{"user_id": "123", "display_name": "五条悟"}]
        self.assertEqual(
            private_notice_origin(session, targets, allow_constructed=True),
            "onebot:FriendMessage:123",
        )
        # 建卡进度等既有私信没有私聊通道时保持原行为：不构造、不退回群聊。
        self.assertEqual(private_notice_origin(session, targets), "")

    def test_missing_platform_or_user_yields_empty(self):
        self.assertEqual(
            private_notice_origin(
                {}, [{"user_id": "123"}], allow_constructed=True
            ),
            "",
        )
        self.assertEqual(
            private_notice_origin(
                {"platform_id": "onebot"},
                [{"user_id": ""}],
                allow_constructed=True,
            ),
            "",
        )


class ChoiceSetDmReminderTests(unittest.IsolatedAsyncioTestCase):
    """走真实选项集行，确认到点才发、每档一次、关掉就不发。

    2026-09-20 线上复现：副本根本没开回合倒计时（timer_instances 全是
    paused、deadline 为空），以计时器为锚点永远不会提醒；改为以活动选项集
    的创建时刻为锚点。
    """

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-dm", "qq:group-dm", DEFAULT_WORLD_SLUG, "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id, private_user_id,
                    private_origin, display_name, character_card_id,
                    character_version_id, character_name, character_code,
                    aliases_json, card_status, ready, participation_status,
                    seat_reserved_at, joined_round, consecutive_timeouts,
                    exit_reason, created_at, updated_at, action_locked
                ) VALUES ('participant_dm', ?, NULL, '1579093646', '', '',
                          '五条悟', NULL, NULL, '五条悟', '五条悟', '[]',
                          'approved', 1, 'active', ?, 1, 0, '', ?, ?, 0)
                """,
                (self.session["id"], now, now, now),
            )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _seed_choice_set(self, *, started_seconds_ago: int) -> None:
        now = datetime.now(timezone.utc)
        created = now - timedelta(seconds=started_seconds_ago)
        with self.database._connect() as connection:
            connection.execute(
                """
                INSERT INTO choice_sets(
                    id, session_id, participant_id, round_no,
                    session_revision, choices_json, status, reroll_count,
                    selected_key, flavor_text, idempotency_key,
                    created_at, updated_at
                ) VALUES ('choices_dm', ?, 'participant_dm', 1, 1, '[]',
                          'active', 0, '', '', 'k', ?, ?)
                """,
                (
                    self.session["id"],
                    created.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )

    async def _set_rules(self, rules: dict) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        merged = {**dict(config["time_rules"]), **rules}
        await self.database.save_instance_time_rules(
            self.session["id"], merged, "test"
        )

    async def _dm_stages(self) -> list[int]:
        return [
            item["stage"]
            for item in await self.database.process_due_timers()
            if item.get("kind") == "dm_reminder"
        ]

    async def _deliver_and_mark(self) -> list[dict]:
        """模拟真实链路：轮询 → 投递 → 投递成功后写「已处理」标记。"""

        delivered: list[dict] = []
        for item in await self.database.process_due_timers():
            if item.get("kind") != "dm_reminder":
                continue
            delivered.append(item)
            await self.database.mark_dm_reminder_sent(
                session_id=item["session_id"],
                choice_set_id=item["choice_set_id"],
                stages=item["mark_stages"],
                elapsed_seconds=item["elapsed_seconds"],
            )
        return delivered

    async def test_first_and_second_stage_fire_once_each(self):
        self._seed_choice_set(started_seconds_ago=360)
        self.assertEqual(
            [item["stage"] for item in await self._deliver_and_mark()], [1]
        )
        # 投递并标记之后不能重复发第一档。
        self.assertEqual(await self._deliver_and_mark(), [])

        # 同一选项集推进到 31 分钟：补发第二档。
        now = datetime.now(timezone.utc)
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE choice_sets SET created_at = ? WHERE id = 'choices_dm'",
                (
                    (now - timedelta(seconds=1900)).isoformat(
                        timespec="seconds"
                    ),
                ),
            )
        delivered = await self._deliver_and_mark()
        self.assertEqual([item["stage"] for item in delivered], [2])
        # 跨档时不连发：一次只投递一条，但两档都标记为已处理。
        self.assertEqual(delivered[0]["mark_stages"], [1, 2])
        self.assertEqual(await self._deliver_and_mark(), [])

    async def test_unhandled_reminder_is_reissued_after_restart(self):
        """轮询出结果但还没投递就重启 → 下一轮必须补发，不能静默丢掉。"""

        self._seed_choice_set(started_seconds_ago=700)
        first = await self.database.process_due_timers()
        self.assertEqual(
            [i["stage"] for i in first if i.get("kind") == "dm_reminder"], [1]
        )
        # 直接丢弃这批通知（不写标记），相当于投递前进程被杀。
        second = await self.database.process_due_timers()
        self.assertEqual(
            [i["stage"] for i in second if i.get("kind") == "dm_reminder"], [1]
        )

    async def test_restart_on_a_long_pending_turn_sends_latest_stage_once(self):
        """重启后第一次轮询：已等 45 分钟 → 只补第二档，且只发一条。"""

        self._seed_choice_set(started_seconds_ago=2700)
        delivered = await self._deliver_and_mark()
        self.assertEqual([item["stage"] for item in delivered], [2])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(await self._deliver_and_mark(), [])

    async def test_notifications_carry_the_acting_player_and_no_deadline(self):
        self._seed_choice_set(started_seconds_ago=360)
        notifications = [
            item
            for item in await self.database.process_due_timers()
            if item.get("kind") == "dm_reminder"
        ]
        self.assertEqual(len(notifications), 1)
        item = notifications[0]
        self.assertEqual(item["timer_type"], "turn")
        self.assertEqual(
            [target["user_id"] for target in item["targets"]],
            ["1579093646"],
        )
        self.assertEqual(
            [target["display_name"] for target in item["targets"]],
            ["五条悟"],
        )
        # 没有回合倒计时时不带剩余时间，文案层不会写「剩余 0秒」。
        self.assertNotIn("remaining_seconds", item)

    async def test_selected_choice_set_stops_reminding(self):
        self._seed_choice_set(started_seconds_ago=360)
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE choice_sets SET status = 'selected' WHERE id = 'choices_dm'"
            )
        self.assertEqual(await self._dm_stages(), [])

    async def test_switch_off_suppresses_every_stage(self):
        await self._set_rules({"turn_dm_reminder_enabled": False})
        self._seed_choice_set(started_seconds_ago=3600)
        self.assertEqual(await self._dm_stages(), [])

    async def test_custom_thresholds_respected(self):
        await self._set_rules(
            {
                "turn_dm_reminder_seconds": 60,
                "turn_dm_reminder_repeat_seconds": 120,
            }
        )
        self._seed_choice_set(started_seconds_ago=90)
        # 已经跨过两档：只补最新一档（2），不连发两条。
        self.assertEqual(await self._dm_stages(), [2])


if __name__ == "__main__":
    unittest.main()
