"""角色卡多行字段：自拟设定要能换行，身份字段仍然不许有空白字符。

背景：`clean_card_field` 原来对**所有**自由文本字段一律拒绝空白字符（含换行），
于是玩家写一段分行列能力的自拟设定时会被直接顶回来——整个「按自拟设定生成属性」
的玩法在填卡这一步就进不去。现在只有多行类型（textarea 等）放宽，姓名/代号这种
单行身份字段保持原样，避免靠空格伪造身份。
"""

from __future__ import annotations

import unittest

from tavern.card_wizard import is_multiline_field
from tavern.database_support import clean_card_field


class SingleLineFieldTests(unittest.TestCase):
    def test_rejects_every_kind_of_whitespace(self) -> None:
        for value in ("a b", "a\u3000b", "a\nb", "a\tb"):
            with self.assertRaises(ValueError):
                clean_card_field(value, label="角色姓名", max_chars=12)

    def test_plain_value_passes(self) -> None:
        self.assertEqual(
            clean_card_field("短线擒龙大王", label="角色姓名", max_chars=12),
            "短线擒龙大王",
        )


class MultilineFieldTests(unittest.TestCase):
    def test_keeps_newlines_and_spaces(self) -> None:
        text = "拥有【万象洞悉的加护】：直读人心。\n①【溯愈】修复重伤\n②【流障】流体护盾\n\n绝招【短线擒龙】。"
        self.assertEqual(
            clean_card_field(
                text, label="自拟设定", max_chars=600, allow_multiline=True
            ),
            text,
        )

    def test_still_enforces_the_character_cap(self) -> None:
        with self.assertRaises(ValueError):
            clean_card_field(
                "x" * 11, label="自拟设定", max_chars=10, allow_multiline=True
            )

    def test_strips_control_characters_but_keeps_layout(self) -> None:
        cleaned = clean_card_field(
            "第一行\x00\x07\n第二行",
            label="自拟设定",
            max_chars=600,
            allow_multiline=True,
        )
        self.assertEqual(cleaned, "第一行\n第二行")

    def test_multiline_type_detection(self) -> None:
        self.assertTrue(is_multiline_field({"type": "textarea"}))
        self.assertFalse(is_multiline_field({"type": "text"}))
        # 缺省类型是单行 text，不能被当成多行
        self.assertFalse(is_multiline_field({}))


if __name__ == "__main__":
    unittest.main()
