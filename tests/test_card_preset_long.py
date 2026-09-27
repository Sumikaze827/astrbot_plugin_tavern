"""建卡向导：预设字段选项值超过 max_chars 不应被拒绝。

背景：灰烬圣所世界 conviction(信念誓言) max_chars=10，但选项里有
「救赎（还清三百年前的血债）」13 字、「复仇（让背叛者付出代价）」12 字。
单选预设把选项值当 raw_value 走 clean_card_field 长度校验，导致选到这些
选项就报「内容超过 10 字符上限」，无论输数字还是输名字都卡死。
修复：单选预设的值来自插件自己列出的合法选项，跳过 max_chars 长度校验。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tavern.constants import SESSION_PREPARING
from tavern.database import TavernDatabase
from tavern.lifecycle import preset_options

ROOT = Path(__file__).resolve().parents[1]
WORLD_FILE = ROOT / "worlds" / "ashen-sanctum-raid.json"


class PresetLongValueCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        world = json.loads(WORLD_FILE.read_text("utf-8"))
        # 复现 bug：把 conviction.max_chars 压回 10（选项里有 13 字的值）
        for f in (
            (world.get("rules", {}) or {}).get("character_card", {}).get("fields", [])
        ):
            if isinstance(f, dict) and f.get("key") == "conviction":
                f["max_chars"] = 10
        await self.database.save_world(world, "admin")
        self.session = await self.database.ensure_session(
            "qq", "group-preset", "qq:group-preset", world["slug"], "admin"
        )
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
        )
        self._counter = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _fresh_origin(self) -> str:
        """每个用例独立建参与者+草稿，避免草稿状态相互污染。"""
        self._counter += 1
        user = f"user-preset-{self._counter}"
        reserved = await self.database.reserve_participant(
            self.session["id"], user, "测试玩家"
        )
        origin = f"private:{user}"
        await self.database.bind_card_code(
            reserved["binding_code"], user, origin
        )
        return origin

    async def _advance_to_conviction(self, origin: str) -> dict:
        draft = await self.database.card_draft_for_private(origin)
        guard = 0
        while (
            draft["current_step"] < len(draft["template"]["fields"])
            and guard < 30
        ):
            field = draft["template"]["fields"][draft["current_step"]]
            key = str(field.get("key") or "")
            if key == "conviction":
                break
            options = preset_options(draft["template"], field, draft["fields"])
            if field.get("type") == "text":
                value = "测试名" if key == "name" else "测试"
            elif options:
                value = options[0]["value"]
            else:
                value = "x"
            draft = await self.database.fill_card_draft(origin, str(value))
            guard += 1
        self.assertLess(guard, 30, "未能走到 conviction 步骤")
        self.assertEqual(
            str(draft["template"]["fields"][draft["current_step"]]["key"]),
            "conviction",
        )
        return draft

    async def test_select_long_option_by_number_is_accepted(self) -> None:
        """用序号选 13 字的选项，不再报「内容超过」卡死。"""
        origin = await self._fresh_origin()
        await self._advance_to_conviction(origin)
        result = await self.database.fill_card_draft(origin, "3")
        self.assertIn("救赎", str(result["fields"].get("conviction")))

    async def test_select_long_option_by_full_name_is_accepted(self) -> None:
        """用完整选项名选长选项同样通过。"""
        origin = await self._fresh_origin()
        await self._advance_to_conviction(origin)
        result = await self.database.fill_card_draft(
            origin, "救赎（还清三百年前的血债）"
        )
        self.assertIn("救赎", str(result["fields"].get("conviction")))

    async def test_short_text_field_still_enforces_max_chars(self) -> None:
        """自由文本字段仍受 max_chars 约束，不因修复而放宽。"""
        origin = await self._fresh_origin()
        # name 字段 max_chars<=10，15 字应被拒
        with self.assertRaisesRegex(ValueError, "超过"):
            await self.database.fill_card_draft(
                origin, "一二三四五六七八九十甲乙丙丁戊"
            )


if __name__ == "__main__":
    unittest.main()
