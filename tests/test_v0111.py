"""v0.11.1 回归测试。

覆盖本次迭代修复的关键缺陷：
1. 不限时建卡码（expires_at=''）不被误判过期（内联判定 + 后台清扫）。
2. 克隆副本文件同步不再触发 FOREIGN KEY constraint failed（多世界场景）。
3. world_contract 对 v5 世界缺 minimum_plugin_version 给出准确的错误信息。
4. world_simulate 的体积/深度防护与冲突前缀标记。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tavern.config import TavernConfig
from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now
from tavern.world_contract import validate_world_contract

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _time_rules_unlimited() -> dict:
    rules = dict(load_json("templates/world-package-v5-full-example.json").get(
        "rules", {}
    ).get("time_rules") or {})
    rules["card_code_ttl_seconds"] = None
    return rules


class V0111DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v0111", "qq:group-v0111", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        # 会话进入准备态，便于加入席位（建卡流程）。
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _seed_unlimited_participant_and_code(self) -> dict:
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE instance_configs SET time_rules_json = ?
                WHERE session_id = ?
                """,
                (
                    json.dumps(_time_rules_unlimited(), ensure_ascii=False),
                    self.session["id"],
                ),
            )
            connection.execute("COMMIT")
        result = await self.database.reserve_participant(
            self.session["id"], "user-1", "旅客"
        )
        # 不限时规则下 reserve_participant 自动生成的码 expires_at=''。
        self.assertEqual(result["binding_expires_at"], "")
        return {"code": result["binding_code"], "participant_id": result["id"]}

    async def test_unlimited_card_code_is_not_marked_expired_on_bind(self) -> None:
        """不限时建卡码（expires_at=''）提交时不得被判过期并补发。"""
        seeded = await self._seed_unlimited_participant_and_code()
        result = await self.database.bind_card_code(
            seeded["code"], "user-1", "private:user-1"
        )
        # 不限时码应直接绑定成功，而不是触发补发。
        self.assertNotIn("binding_code_reissued", result)
        self.assertEqual(result.get("id"), seeded["participant_id"])
        self.assertEqual(result.get("private_user_id"), "user-1")

    async def test_cleanup_keeps_unlimited_card_codes_active(self) -> None:
        """后台清扫不得把 expires_at='' 的不限时码置为 expired。"""
        seeded = await self._seed_unlimited_participant_and_code()
        report = await self.database.cleanup(audit_retention_days=90)
        self.assertEqual(report.get("card_codes", 0), 0)
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT status FROM card_binding_codes WHERE code = ?",
                (seeded["code"],),
            ).fetchone()
        self.assertEqual(row["status"], "active")

    async def test_expired_card_code_is_reissued_but_unlimited_is_kept(self) -> None:
        """对照：有时限的过期码仍会被补发，验证修复未破坏原有逻辑。"""
        seeded = await self._seed_unlimited_participant_and_code()
        past = "2020-01-01T00:00:00+00:00"
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE card_binding_codes SET expires_at = ? WHERE code = ?",
                (past, seeded["code"]),
            )
        result = await self.database.bind_card_code(
            seeded["code"], "user-1", "private:user-1"
        )
        self.assertTrue(result.get("binding_code_reissued"))
        self.assertNotEqual(result.get("binding_code"), seeded["code"])

    async def test_clone_session_with_multiple_worlds_syncs_without_fk_error(self) -> None:
        """多世界目录下克隆副本，文件同步不再触发 FOREIGN KEY constraint failed。

        复现路径：目录内存在多个世界（内置世界 + 新导入世界），
        world_snapshots.world_id 为 ON DELETE RESTRICT，旧实现会在此失败。
        """
        world = load_json("worlds/aelvion-ashen-crown.json")
        world = dict(world)
        world["slug"] = "fk-repro-0111"
        world["name"] = "FK 复现世界 0111"
        await self.database.save_world(world, "admin")

        source = await self.database.ensure_session(
            "qq", "group-fk-0111", "qq:group-fk-0111",
            "fk-repro-0111", "admin-1", "fk-source-0111", "FK 源副本 0111",
        )
        clone = await self.database.clone_session(
            source["id"], "admin-1",
            instance_slug="fk-clone-0111",
            instance_name="FK 克隆副本 0111",
            candidate_world_ref="",
        )
        # 克隆后直接触发文件同步，旧实现会在 _prune_instance 抛 FK 错误。
        result = self.database.storage.sync_session(clone["id"])
        self.assertTrue(result["database"].exists())
        self.assertTrue(result["manifest"].exists())


class V0111ContractTests(unittest.TestCase):
    def test_v5_world_without_minimum_version_reports_v5_error(self) -> None:
        """v5 世界缺 minimum_plugin_version 时，错误信息必须指向 v5 而非 v4。"""
        world = load_json("worlds/aelvion-ashen-crown.json")
        world = dict(world)
        world.pop("minimum_plugin_version", None)
        with self.assertRaisesRegex(
            ValueError,
            "v5 必须声明 minimum_plugin_version >= 0.11.0",
        ):
            validate_world_contract(world)

    def test_v4_world_without_minimum_version_reports_v4_error(self) -> None:
        world = load_json("worlds/aelvion-ashen-crown.json")
        world = dict(world)
        world["world_schema_version"] = 4
        world["protocol"] = {}
        world.pop("minimum_plugin_version", None)
        world["rules"] = dict(world["rules"])
        world["rules"]["world_schema_version"] = 4
        with self.assertRaisesRegex(
            ValueError,
            "v4 必须声明 minimum_plugin_version >= 0.10.0",
        ):
            validate_world_contract(world)

    def test_planning_prompt_builds_real_prompt(self) -> None:
        """回归：v0.11.0 中 planning_prompt 缺失函数体返回 None，
        会导致自由输入回合以空提示调用模型。"""
        from tavern.prompts import planning_prompt

        prompt = planning_prompt(
            world={},
            session={"next_actor": {}},
            player={},
            player_input="我打开门。",
            events=[],
            memories=[],
            allow_checks=True,
        )
        self.assertTrue(prompt)
        self.assertIn("裁定下面这一条玩家行动", prompt)
        self.assertIn("<player_input", prompt)

    def test_dm_beat_prompt_still_works(self) -> None:
        from tavern.prompts import dm_beat_prompt

        prompt = dm_beat_prompt(
            world={},
            session={},
            instruction="推进",
            directive="指引",
            events=[],
            memories=[],
        )
        self.assertIn("mode=resolve", prompt)

    def test_engine_retry_cap_constant_exists(self) -> None:
        import tavern.engine as engine_module

        self.assertGreaterEqual(engine_module._MAX_TOTAL_MODEL_ATTEMPTS, 4)

    def test_web_console_conflict_prefix_and_guards(self) -> None:
        """导入冲突前缀与 simulate 防护常量存在且符合约定。"""
        # 导入 tavern.web_console 需要 astrbot 桩（与 test_plugin_shell 一致）。
        import sys
        import tempfile
        import types
        from types import SimpleNamespace

        stub_dir = Path(tempfile.mkdtemp(prefix="astrbot-stub-"))
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        event = types.ModuleType("astrbot.api.event")
        star = types.ModuleType("astrbot.api.star")
        web = types.ModuleType("astrbot.api.web")
        api.AstrBotConfig = dict
        api.logger = SimpleNamespace(
            exception=print, info=print, warning=print, debug=print
        )
        event.AstrMessageEvent = object
        event.MessageChain = object
        event.filter = None
        star.Context = object
        star.Star = object
        star.StarTools = object
        web.PluginUploadFile = object
        web.error_response = lambda *a, **k: None
        web.json_response = lambda *a, **k: None
        web.file_response = lambda *a, **k: None
        web.stream_response = lambda *a, **k: None
        web.request = SimpleNamespace(username=None, query={})
        astrbot.api = api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api
        sys.modules["astrbot.api.event"] = event
        sys.modules["astrbot.api.star"] = star
        sys.modules["astrbot.api.web"] = web

        from tavern import web_console as wc

        self.assertEqual(wc._WORLD_CONFLICT_PREFIX, "导入冲突")
        self.assertTrue(wc._json_depth({"a": {"b": {"c": 1}}}, limit=2))
        self.assertFalse(wc._json_depth({"a": 1}, limit=4))
        self.assertGreater(wc._WORLD_SIMULATE_MAX_BYTES, 0)


if __name__ == "__main__":
    unittest.main()
