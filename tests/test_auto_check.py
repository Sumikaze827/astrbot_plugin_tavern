"""引擎自动补检定回归测试。

背景：模型（doubao）常把检定写进选项文字（「以智识检定解读」）而不是
check 字段，导致有风险的行动静默免检、玩家看不到骰子。引擎兜底：
non-safe 选项未配 check 时，从文字提取属性自动补检定；提取不到落到
generic_check（世界开启时）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tavern.lifecycle import normalize_choices

ROOT = Path(__file__).resolve().parents[1]
WORLD_FILE = ROOT / "worlds" / "ashen-sanctum-raid.json"


def _four(overrides: dict) -> list[dict]:
    """补齐 A/B/C/D 四个选项，测试项用 overrides['A']，其余给 safe 占位。"""
    result = []
    for key in ("A", "B", "C", "D"):
        if key == "A" and overrides:
            result.append({"key": key, **overrides})
        else:
            result.append({"key": key, "text": f"占位动作{key}", "danger_id": "safe"})
    return result


class AutoCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = json.loads(WORLD_FILE.read_text("utf-8"))

    def test_auto_check_from_text_attribute(self) -> None:
        """「以智识检定解读」→ 自动补 intellect 检定。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "尝试从祭坛底座辨认巨像真名，以智识检定解读",
                    "danger_id": "controlled",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check_stat"], "intellect")
        self.assertEqual(a["check"]["attribute_id"], "intellect")
        self.assertEqual(a["check"]["difficulty"], 9)  # controlled 策略
        b = next(c for c in choices if c["key"] == "B")
        self.assertFalse(b["requires_check"])

    def test_risky_command_option_forced_to_roll(self) -> None:
        """危险选项推不出属性也要强制摇点（命令巨像 → 魅力，绝不免检）。

        2026-08-23 用户拍板：危险/绝境/致命必然摇点，与措辞无关。
        """
        choices = normalize_choices(
            _four(
                {
                    "text": "举灰玫瑰信物上前，尝试命令巨像让开",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "charisma")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_auto_check_from_action_words(self) -> None:
        """动作词映射：翻滚闪避→敏捷、抽箭射向→敏捷、安抚→魅力。"""
        cases = {
            "翻滚闪避巨像下一击，设法捡回重剑": "agility",
            "抽箭射向巨像胸口烬火，吸引它的注意": "agility",
            "尝试用灰玫瑰徽记安抚巨像": "charisma",
            "破译祭坛底座的真名铭文": "intellect",
            "率先进深殿探查内部情况": "perception",
            "以奥术加固深殿门裂缝": "intellect",
            "以圣光稳住局面，向玛拉道出三百年血盟真相": "charisma",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                choices = normalize_choices(
                    _four({"text": text, "danger_id": "controlled"}),
                    self.world,
                )
                a = next(c for c in choices if c["key"] == "A")
                self.assertTrue(a["requires_check"])
                self.assertEqual(a["check"]["attribute_id"], expected)

    def test_safe_stays_checkless(self) -> None:
        choices = normalize_choices(
            _four({"text": "向索恩询问方法", "danger_id": "safe"}),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertFalse(a["requires_check"])

    def test_explicit_check_untouched(self) -> None:
        """模型已正确配置的 check 不被覆盖。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "挥剑强攻巨像",
                    "danger_id": "lethal",
                    "check": {
                        "required": True,
                        "attribute_id": "strength",
                        "type": "standard",
                    },
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check_stat"], "strength")

    def test_risky_melee_option_auto_checks_strength(self) -> None:
        """危险/致命近战选项推不出精确属性时，按行动性质回退到力量并带正确 DC。

        2026-08-23 反馈：jade 的「致命 · 直接突入前锋阵中直取断剑骑士」等
        近战选项因动词不在精确词表 → 静默免检。新增行动性质回退层后强制补检定。
        """
        cases = {
            "趁骑士停顿，正面冲上去击溃前锋前排": ("dangerous", 13),
            "借力将冲在最前的尸兵掀下桥去": ("dangerous", 13),
            "直接突入前锋阵中，直取断剑骑士（lethal）": ("lethal", 19),
        }
        for text, (risk, dc) in cases.items():
            with self.subTest(text=text):
                choices = normalize_choices(
                    _four({"text": text, "danger_id": risk}),
                    self.world,
                )
                a = next(c for c in choices if c["key"] == "A")
                self.assertTrue(a["requires_check"], f"{text} 应补检定")
                self.assertEqual(a["check"]["attribute_id"], "strength")
                self.assertEqual(a["check"]["difficulty"], dc)

    def test_risky_speech_option_falls_back_to_charisma(self) -> None:
        """呼喝/交涉类危险选项 → 魅力检定（精确词表新补「呼喝」）。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "以圣光呼喝断剑骑士之名试探其执念回应",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "charisma")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_risky_divine_option_falls_back_to_willpower(self) -> None:
        """护印/圣印类危险选项 → 意志检定（精确词表新补「护印」）。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "引动圣银护印加护挽昼的重盾稳固中线",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "willpower")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_dangerous_passive_option_still_forced_to_roll(self) -> None:
        """危险选项纯被动措辞也要摇点——落到字符重叠兜底属性（绝不免检）。

        2026-08-23 用户拍板：危险/绝境/致命必然摇点。即使文本毫无行动词
        （如「站在原地」被模型误标危险），也走世界属性描述字符重叠兜底。
        """
        choices = normalize_choices(
            _four(
                {
                    "text": "将信物挂在胸前，不发一言站在原地",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertIsNotNone(a["check"])

    def test_lethal_without_consequence_gets_default_disclosure(self) -> None:
        """致命选项漏配 known_consequences → 引擎兜底填通用警示，不卡死回合。

        2026-08-23 反馈：模型生成「致命」选项却不写后果，引擎在投骰时
        硬校验拒绝，整轮裁定失败、世界状态不变。兜底后玩家选 lethal 前
        仍能看到风险提示。
        """
        choices = normalize_choices(
            _four(
                {
                    "text": "以奥术精准轰击巴尔德残躯，尝试彻底击溃指挥",
                    "danger_id": "lethal",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertTrue(a["known_consequences"])
        self.assertEqual(a["check"]["known_consequences"], a["known_consequences"])
        self.assertIn("致命", a["known_consequences"])

    def test_lethal_with_explicit_consequence_untouched(self) -> None:
        """模型已写明的致命后果不被覆盖。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "以奥术精准轰击巴尔德残躯",
                    "danger_id": "lethal",
                    "known_consequences": "失败会被尸群反噬，队伍生命-2",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertEqual(
            a["known_consequences"], "失败会被尸群反噬，队伍生命-2"
        )

    def test_lethal_default_consequence_constant_is_nonempty(self) -> None:
        """引擎侧兜底常量非空——roll 期 fallback 依赖它。"""
        from tavern.lifecycle import _LETHAL_DEFAULT_CONSEQUENCE

        self.assertTrue(_LETHAL_DEFAULT_CONSEQUENCE.strip())
        self.assertIn("致命", _LETHAL_DEFAULT_CONSEQUENCE)

    def test_forced_fallback_holy_chain_block(self) -> None:
        """圣银链环抵门压制死卫 → 力量（精确词表补「抵住/压制」）。

        2026-08-23 二次反馈：b7d003 第 13 轮「以圣银链环残片抵住门缝，
        尝试短暂压制死卫」危险项仍免检——「圣银/抵住/压制」都在词表外。
        """
        choices = normalize_choices(
            _four(
                {
                    "text": "以圣银链环残片抵住门缝，尝试短暂压制死卫",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "strength")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_forced_fallback_call_name_of_the_dead(self) -> None:
        """圣银残片指向骑士呼名唤醒执念 → 魅力（精确词表补「呼名」）。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "以圣银链环残片指向断剑骑士，尝试呼名唤醒执念",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "charisma")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_forced_fallback_clear_death_guard(self) -> None:
        """冲下石阶清剿死卫 → 力量（精确词表补「清剿」）。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "沿石阶冲下，加入前排战团协助清剿死卫",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "strength")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_forced_fallback_holy_prayer_buff(self) -> None:
        """圣光祷言加持庇护屏障 → 意志（精确词表补「祷言/加持」）。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "吟唱圣光祷言，为前排队友加持庇护屏障",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "willpower")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_forced_fallback_bracing_shield_line(self) -> None:
        """整备盾线等死卫冲出 → 力量（精确词表补「盾线」）。"""
        choices = normalize_choices(
            _four(
                {
                    "text": "退后半步，整备盾线与站位，等待门后死卫冲出",
                    "danger_id": "dangerous",
                }
            ),
            self.world,
        )
        a = next(c for c in choices if c["key"] == "A")
        self.assertTrue(a["requires_check"])
        self.assertEqual(a["check"]["attribute_id"], "strength")
        self.assertEqual(a["check"]["difficulty"], 13)

    def test_forced_roll_generalizes_to_new_world(self) -> None:
        """新本（属性描述完全不同、措辞从未见过）危险选项仍强制摇点。

        2026-08-23 用户质疑「一到新本不又失效」：硬规则不依赖本本专有词，
        精确/宽泛推断失败后落到世界属性描述字符重叠 → 世界首个允许属性。
        无论新本把属性叫什么（moxie/tech/sympathy）、措辞多新鲜，只要模型
        标了危险就必摇。
        """
        world = {
            "rules": {
                "character_card": {
                    "stats": {
                        "attributes": [
                            {"key": "moxie", "label": "胆识", "description": "正面硬刚、威胁、街头火并。"},
                            {"key": "tech", "label": "灵能", "description": "骇入芯片、超频义体、心电感应。"},
                            {"key": "sympathy", "label": "共感", "description": "读心、交涉、安抚机械兽。"},
                        ]
                    }
                },
                "resolution": {
                    "mode": "attribute",
                    "difficulty_policy": {
                        "controlled": 9,
                        "dangerous": 13,
                        "desperate": 17,
                        "lethal": 19,
                        "safe": None,
                    },
                    "allowed_attributes": ["moxie", "tech", "sympathy"],
                    "generic_check": {"enabled": False},
                },
            }
        }
        for text in (
            "掀起垃圾桶盖，砸向巡警的后脑",  # 街头火并 → moxie
            "把精神接入芯片，暴力超频义体",  # 灵能 → tech
            "对机械兽讲一段编造的家乡故事",  # 共感 → sympathy
        ):
            with self.subTest(text=text):
                choices = normalize_choices(
                    _four({"text": text, "danger_id": "dangerous"}),
                    world,
                )
                a = next(c for c in choices if c["key"] == "A")
                self.assertTrue(a["requires_check"], f"{text} 应强制摇点")
                self.assertIsNotNone(a["check"])
                self.assertEqual(a["check"]["difficulty"], 13)
                self.assertIn(
                    a["check"]["attribute_id"], {"moxie", "tech", "sympathy"}
                )


if __name__ == "__main__":
    unittest.main()
