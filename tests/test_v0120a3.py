"""0.12.0-A3 回归测试。

覆盖（接口与 WebUI 总览 / 副本运行卡片对齐）：
1. overview() 新增 integrity / commands 块（近 24h 审计统计）。
2. global_token_usage()：滚动窗口内的已完成 Token 用量合计。
3. session_dashboard() 新增 waiting_for 与 world_state.progress。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.dashboard import session_dashboard
from tavern.database import TavernDatabase
from tavern.database_support import json_dump, new_id, utc_now


class V0120A3OverviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-a3", "qq:group-a3", DEFAULT_WORLD_SLUG, "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_overview_contains_integrity_and_commands(self) -> None:
        # 写入一条成功审计、一条失败审计与一条兜底解析命中审计。
        await self.database.write_audit(
            self.session["id"], "admin-1", "session.action", "session",
            {"ok": True},
        )
        await self.database.write_audit(
            self.session["id"], "admin-1", "turn.failed", "",
            {"error": "模型返回非法 JSON"},
        )
        await self.database.write_audit(
            "", "admin-1", "command.relaxed_parse", "qq:g",
            {"action": "状态", "platform_id": "qq"},
        )
        result = await self.database.overview()
        self.assertIn("integrity", result)
        self.assertIn("commands", result)
        self.assertEqual(
            result["integrity"]["schema_version"],
            result["schema_version"],
        )
        self.assertGreaterEqual(
            result["integrity"]["failed_operations_24h"], 1
        )
        self.assertGreaterEqual(
            result["commands"]["command_count_24h"], 3
        )
        self.assertGreaterEqual(
            result["commands"]["relaxed_parse_hits_24h"], 1
        )
        self.assertTrue(result["commands"]["jg_enabled"])
        rate = result["commands"]["success_rate"]
        self.assertGreaterEqual(rate, 0.0)
        self.assertLessEqual(rate, 100.0)

    async def test_global_token_usage_respects_window(self) -> None:
        now = utc_now()
        # 使用固定远古时间戳作为窗口外记录，彻底消除时钟/时区抖动影响。
        old = "2020-01-01T00:00:00+00:00"
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for stamp, tokens in ((now, 400), (now, 100), (old, 9999)):
                connection.execute(
                    """
                    INSERT INTO token_usage(
                        id, session_id, group_id, request_type,
                        provider_id, total_tokens, status,
                        created_at
                    ) VALUES (?, ?, 'g', 'check', 'p', ?, 'completed', ?)
                    """,
                    (new_id("tu"), self.session["id"], tokens, stamp),
                )
            connection.execute("COMMIT")
        used = await self.database.global_token_usage(3600)
        self.assertEqual(used, 500)
        # 窗口为 3 小时时，两条近期记录计入（远古记录仍被排除，因为它早于任何窗口）。
        used_wide = await self.database.global_token_usage(3 * 3600)
        self.assertEqual(used_wide, 500)


class V0120A3DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-a3b", "qq:group-a3b", DEFAULT_WORLD_SLUG, "admin-1"
        )
        # 在 world_state 写入进度字段。
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE sessions SET world_state_json = ? WHERE id = ?",
                (
                    json_dump(
                        {
                            "location": "鸦渡镇码头",
                            "scene_summary": "灰月低垂",
                            "progress": {
                                "chapter": "序章：灰月下的鸦渡镇",
                                "current_objective": "查明旧桥符文",
                                "completed_milestones": 2,
                                "total_milestones": 12,
                            },
                        }
                    ),
                    self.session["id"],
                ),
            )
            connection.execute("COMMIT")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_session_dashboard_has_progress_and_waiting(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        detail = await session_dashboard(self.database, self.session["id"])
        progress = detail["session"]["world_state"]["progress"]
        self.assertEqual(progress["chapter"], "序章：灰月下的鸦渡镇")
        self.assertEqual(progress["completed_milestones"], 2)
        self.assertEqual(progress["total_milestones"], 12)
        self.assertEqual(detail["session"]["waiting_for"], "preparation")
        # 运行态且无投票/选项时，不误报等待。
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        detail = await session_dashboard(self.database, self.session["id"])
        self.assertEqual(detail["session"]["waiting_for"], "")


if __name__ == "__main__":
    unittest.main()
