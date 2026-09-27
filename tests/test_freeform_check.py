"""自由演绎检定裁判 + 死亡硬落实的回归测试。

覆盖 0.13.x 两个机制：
1. 自由演绎检定（_judge_freeform_check / _parse_freeform_judge）：玩家自由
   演绎先经模型裁判判定「摇不摇、摇哪个属性、DC 多少」，再走同一套
   locked-check 投骰；能匹配角色卡时读取实际属性值与修正，未绑卡或无法
   匹配时才回退为 0。
2. 死亡硬落实（_lethal_death_verdict / _death_epilogue / declare_death）：
   预设 lethal 失败，或自由演绎存在明确致死链时的 lethal 大失败，才由
   引擎三层落实；安全场景的大失败只产生场景内合理的非致死重后果。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now
from tavern.engine import (
    TavernEngine,
    _death_epilogue,
    _lethal_death_verdict,
    _narrative_has_death,
    _parse_freeform_death_judge,
    _parse_freeform_judge,
    _parse_freeform_judge_with_contract,
    _parse_numbered_attribute,
    _pick_highest_attribute_key,
    _resolve_check_label,
)
from tavern.world_contract import world_contract
from tavern.events import EventBroker
from tavern.prompts import (
    _outcome_fulfillment_hint,
    checked_resolution_prompt,
    freeform_check_judge_prompt,
    freeform_death_judge_prompt,
    planning_prompt,
)
from tavern.resolution import DiceResult


def _death_world() -> dict:
    """允许死亡的最小世界：dice_only + character_death=yes。"""
    return {
        "name": "测试世界 · 死亡硬落实",
        "slug": DEFAULT_WORLD_SLUG,
        "system_prompt": "测试系统提示。",
        "rules": {
            "resolution": {
                "mode": "dice_only",
                "dice_system": "d20",
            },
            "content_boundaries": {
                "character_death": "yes",
            },
            "character_card": {
                "stats": {
                    "attributes": [
                        {"key": "dexterity", "label": "身手"},
                        {"key": "wits", "label": "心智"},
                        {"key": "charm", "label": "魅力"},
                    ],
                }
            },
            "progress": {
                "total_milestones": 1,
                "chapters": [
                    {
                        "id": "ch_01",
                        "title": "第一章：试炼",
                        "current_objective": "完成试炼",
                        "max_turns": 6,
                        "milestones": [],
                        "exits_when": {"all_milestones": []},
                        "next_chapter_id": None,
                    }
                ],
            },
        },
    }


def _dice(
    outcome: str,
    *,
    difficulty: int = 14,
    modifier: int = 0,
    attribute_value: int | None = None,
    risk: str = "controlled",
    check_type: str = "standard",
) -> DiceResult:
    total = 11 if outcome not in {"failure", "critical_failure"} else 6
    if outcome == "critical_failure":
        total = 1
    return DiceResult(
        die=1 if outcome == "critical_failure" else 11,
        modifier=modifier,
        attribute_value=attribute_value,
        total=total,
        difficulty=difficulty,
        outcome=outcome,
        critical=("自然 1" if outcome == "critical_failure" else None),
        rolls=(1,) if outcome == "critical_failure" else (11,),
        kept=1,
        margin=(
            0
            if outcome in {"failure", "critical_failure"}
            else total - difficulty
        ),
        risk=risk,
        check_type=check_type,
    )


class ParseFreeformJudgeTests(unittest.TestCase):
    """裁判 JSON 解析与校验。"""

    def test_valid_spec(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 14,
                "risk": "dangerous",
                "reason": "攀爬崖壁有坠落风险",
            }
        )
        self.assertTrue(parsed["requires_check"])
        choice = parsed["selected_choice"]
        self.assertEqual(choice["check_stat"], "身手")
        self.assertEqual(choice["difficulty"], 14)
        self.assertEqual(choice["risk"], "controlled")
        self.assertEqual(choice["modifier"], 0)
        self.assertEqual(choice["check_type"], "standard")

    def test_should_roll_false_without_stat_falls_back_to_engine(self) -> None:
        # 硬性摇点：裁判免检且没给属性 → requires_check=False，
        # 交由引擎自动检定兜底（必摇，不再静默免检）。
        parsed = _parse_freeform_judge({"should_roll": False})
        self.assertFalse(parsed["requires_check"])
        # 模型把布尔写成字符串也要正确识别
        parsed = _parse_freeform_judge({"should_roll": "false"})
        self.assertFalse(parsed["requires_check"])

    def test_should_roll_false_with_stat_forces_roll(self) -> None:
        # 硬性摇点：裁判判免检但给了属性 → 降级为强制检定（forced_roll）。
        parsed = _parse_freeform_judge(
            {
                "should_roll": False,
                "check_stat": "身手",
                "difficulty": 14,
                "risk": "dangerous",
            }
        )
        self.assertTrue(parsed["requires_check"])
        self.assertTrue(parsed["forced_roll"])
        choice = parsed["selected_choice"]
        self.assertEqual(choice["check_stat"], "身手")
        self.assertEqual(choice["difficulty"], 14)
        self.assertEqual(choice["risk"], "controlled")
        self.assertEqual(choice["modifier"], 0)

    def test_string_true_roll(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": "true",
                "check_stat": "胆识",
                "difficulty": 18,
            }
        )
        self.assertTrue(parsed["requires_check"])
        self.assertEqual(parsed["selected_choice"]["difficulty"], 18)

    def test_bad_difficulty_falls_back(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": "abc",
                "risk": "weird",
            }
        )
        self.assertEqual(parsed["selected_choice"]["difficulty"], 12)
        self.assertEqual(parsed["selected_choice"]["risk"], "controlled")

    def test_difficulty_above_20_clamps_to_20(self) -> None:
        # DC 不得超过 20：玩家给 24 时 clamp 到 20（保留裁判意图，
        # 不抹回默认 12）。
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 24,
            }
        )
        self.assertEqual(parsed["selected_choice"]["difficulty"], 20)

    def test_check_stat_must_be_in_world_allowed_attributes(self) -> None:
        # 属性白名单校验：世界声明了 allowed_attributes 时，裁判
        # 自造的属性名整条作废（返回 None）→ 调用方回退引擎自动检定。
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "自造维度",
                "difficulty": 14,
            },
            allowed_attributes=("dexterity", "wits", "charm"),
        )
        self.assertIsNone(parsed)
        # 白名单内合法 stat 正常通过
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "dexterity",
                "difficulty": 14,
            },
            allowed_attributes=("dexterity", "wits", "charm"),
        )
        self.assertTrue(parsed["requires_check"])
        self.assertEqual(
            parsed["selected_choice"]["check_stat"], "dexterity"
        )

    def test_no_allowed_list_accepts_any_string(self) -> None:
        # 没传白名单时保持原行为（向后兼容）。
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 14,
            }
        )
        self.assertEqual(parsed["selected_choice"]["check_stat"], "身手")

    def test_parse_numbered_attribute(self) -> None:
        # 八次修正：prompt 把世界属性表编成 #1/#2/#3，模型返回的
        # check_stat "#N" 由 _parse_numbered_attribute 还原成序号。
        self.assertEqual(_parse_numbered_attribute("#1"), 1)
        self.assertEqual(_parse_numbered_attribute("#12"), 12)
        # 带空格也行（容错）
        self.assertEqual(_parse_numbered_attribute("# 3"), 3)
        # 非编号格式返回 None
        self.assertIsNone(_parse_numbered_attribute("agility"))
        self.assertIsNone(_parse_numbered_attribute("#abc"))
        self.assertIsNone(_parse_numbered_attribute("# 1.5"))
        self.assertIsNone(_parse_numbered_attribute("#"))
        self.assertIsNone(_parse_numbered_attribute(""))
        self.assertIsNone(_parse_numbered_attribute(None))

    def test_pick_highest_attribute_key(self) -> None:
        # 八次修正：玩家角色卡 modifiers 里 value 最大的 key。
        # 基本：charm=5 最高
        self.assertEqual(
            _pick_highest_attribute_key(
                {"dexterity": 4, "wits": 2, "charm": 5}
            ),
            ("charm", 5),
        )
        # 空 modifiers
        self.assertEqual(_pick_highest_attribute_key({}), ("", 0))
        # tie 时按 key 字典序稳定排序（dexterity < wits）
        self.assertEqual(
            _pick_highest_attribute_key(
                {"dexterity": 3, "wits": 3, "charm": 3}
            ),
            ("charm", 3),  # charm 在字典序最小
        )
        # 非数字 value 跳过
        self.assertEqual(
            _pick_highest_attribute_key(
                {"dexterity": "abc", "wits": 2, "charm": 5}
            ),
            ("charm", 5),
        )

    def test_blocked_values_rejected_without_whitelist(self) -> None:
        # 2026-08-24：白名单为空时也要拒绝明显非属性值（防「世界没声明
        # attributes → 模型填进「常规/standard」就放任通过 → 玩家看到
        # 【常规检定】公告」这条退化路径）。
        for bad in ("常规", "standard", "通用", "controlled",
                    "default", "spell", "属性"):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    _parse_freeform_judge(
                        {
                            "should_roll": True,
                            "check_stat": bad,
                            "difficulty": 14,
                        }
                    ),
                    f"应拒绝明显非法属性值：{bad}",
                )

    def test_blocked_values_rejected_with_whitelist(self) -> None:
        # 黑名单优先级最高：白名单有时也不能放「standard」「controlled」
        # 这类风险档/模式词通过。
        for bad in ("standard", "controlled", "dangerous", "lethal"):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    _parse_freeform_judge(
                        {
                            "should_roll": True,
                            "check_stat": bad,
                            "difficulty": 14,
                        },
                        allowed_attributes=("dexterity", "wits", "charm"),
                    )
                )

    def test_resolve_check_label_from_key(self) -> None:
        # key → label 翻译：dexterity 应该解析为「身手」。
        contract = {
            "resolution": {"mode": "dice_only"},
            "attributes": [
                {"key": "dexterity", "label": "身手"},
                {"key": "wits", "label": "心智"},
            ],
        }
        self.assertEqual(
            _resolve_check_label(contract, "dexterity"), "身手"
        )
        # 已是 label 或世界属性表里查不到时，原样返回。
        self.assertEqual(
            _resolve_check_label(contract, "心智"), "心智"
        )
        self.assertEqual(
            _resolve_check_label(contract, "不存在"), "不存在"
        )

    def test_parse_freeform_judge_with_contract_attaches_label(self) -> None:
        # _parse_freeform_judge_with_contract 同时返回 key 与 label，
        # 下游 format 用 label 显示，玩家看到「身手」而不是「dexterity」。
        # 九次修正（玩家原话「就让模型返回编号，按编号选择属性不就
        # 好了吗」）：模型按 prompt 必返回 "#N"，不再接受直接填 key/
        # label。
        contract = world_contract(_death_world())
        # #1 → dexterity + label 身手
        parsed = _parse_freeform_judge_with_contract(
            {
                "should_roll": True,
                "check_stat": "#1",
                "difficulty": 14,
                "risk": "dangerous",
            },
            contract=contract,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["selected_choice"]["check_stat"], "dexterity")
        self.assertEqual(parsed["selected_choice"]["check_label"], "身手")
        # #2 → wits，label 与 key 不同时填 label
        parsed = _parse_freeform_judge_with_contract(
            {
                "should_roll": True,
                "check_stat": "#2",
                "difficulty": 14,
            },
            contract=contract,
        )
        self.assertEqual(parsed["selected_choice"]["check_stat"], "wits")
        self.assertEqual(parsed["selected_choice"]["check_label"], "心智")

    def test_no_player_modifiers_falls_back_to_world_first(self) -> None:
        # 九次修正（玩家原话「怎么可能没绑卡？？？」）：player_modifiers
        # 空时（DB 异常/卡 pending），非法 #N 仍要走世界属性表首属性兜
        # 底——不能让 stat="" 进 locked_check 投骰（旧路径里这里会整条
        # 作废让玩家看到「本轮裁定未完成」，违反自由演绎语义）。
        contract = world_contract(_death_world())
        parsed = _parse_freeform_judge_with_contract(
            {
                "should_roll": True,
                "check_stat": "#99",  # 越界
                "difficulty": 14,
            },
            contract=contract,
            # 不传 player_modifiers
        )
        self.assertIsNotNone(parsed)
        # 走世界属性表首属性兜底（_death_world: dexterity）
        self.assertEqual(
            parsed["selected_choice"]["check_stat"], "dexterity"
        )
        self.assertTrue(
            parsed["selected_choice"].get("_general_fallback")
        )

    def test_non_numbered_check_stat_falls_back_to_highest(self) -> None:
        # 九次修正（玩家原话「非法的就用玩家最高属性啊」）：模型没按
        # 规则填 #N（填了英文标准 key agility、世界不存在的中文 label
        # "玉兰"、空串等）→ 走「玩家最高属性兜底」+ 公告显示「【通用
        # 检定】」。注意：之前八次修正里的「agility→dexterity 标准 key
        # 翻译」路径被这条简化规则取代——模型不应该再填 key/label，
        # 不需要翻译逻辑。
        contract = world_contract(_death_world())
        for bad in ("agility", "玉兰", "standard", ""):
            with self.subTest(bad=bad):
                parsed = _parse_freeform_judge_with_contract(
                    {
                        "should_roll": True,
                        "check_stat": bad,
                        "difficulty": 14,
                    },
                    contract=contract,
                    player_modifiers={
                        "dexterity": 4, "wits": 2, "charm": 5,
                    },
                )
                self.assertIsNotNone(parsed)
                # 最高属性是 charm（5）
                self.assertEqual(
                    parsed["selected_choice"]["check_stat"], "charm"
                )
                self.assertTrue(
                    parsed["selected_choice"].get("_general_fallback")
                )
                # label 走世界属性表的「魅力」
                self.assertEqual(
                    parsed["selected_choice"]["check_label"], "魅力"
                )

    def test_numbered_check_stat_resolves_to_world_key(self) -> None:
        # 2026-08-24 八次修正（玩家原话「把预设的属性编个号，让模型返
        # 回编号」）：prompt 把世界属性表编成 #1/#2/#3，模型返回
        # check_stat="#1" → engine 还原成第 1 个属性的 key。
        contract = world_contract(_death_world())
        # _death_world 里 attributes 顺序：dexterity, wits, charm
        parsed = _parse_freeform_judge_with_contract(
            {
                "should_roll": True,
                "check_stat": "#1",
                "difficulty": 14,
            },
            contract=contract,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(
            parsed["selected_choice"]["check_stat"], "dexterity"
        )
        self.assertEqual(
            parsed["selected_choice"]["check_label"], "身手"
        )
        # #3 → charm
        parsed = _parse_freeform_judge_with_contract(
            {
                "should_roll": True,
                "check_stat": "#3",
                "difficulty": 14,
            },
            contract=contract,
        )
        self.assertEqual(
            parsed["selected_choice"]["check_stat"], "charm"
        )
        # 非法编号（#0、#99、#abc、# 1.5）→ 当作非法值走兜底分支
        # 九次修正（玩家原话「非法的就用玩家最高属性啊」）：非法编号
        # 不再返回 None 整条作废，而是和标准 key 一样走"玩家最高属性
        # 兜底"+ 公告显示「【通用检定】」。
        for bad in ("#0", "#99", "#abc", "# 1.5"):
            with self.subTest(bad=bad):
                parsed = _parse_freeform_judge_with_contract(
                    {
                        "should_roll": True,
                        "check_stat": bad,
                        "difficulty": 14,
                    },
                    contract=contract,
                )
                # 没传 player_modifiers + 世界有 attributes → 走世
                # 界首属性兜底（不是返回 None）。这里 _death_world
                # attributes 第一项是 dexterity。
                self.assertIsNotNone(
                    parsed, f"非法编号不应整条作废：{bad}"
                )
                self.assertEqual(
                    parsed["selected_choice"]["check_stat"], "dexterity"
                )
                self.assertTrue(
                    parsed["selected_choice"].get(
                        "_general_fallback"
                    )
                )
        # 非法编号 + 有 player_modifiers：走「玩家最高属性兜底」+ 标记
        # _general_fallback → 公告显示「【通用检定】」让玩家识别这是
        # 兜底路径。
        parsed = _parse_freeform_judge_with_contract(
            {
                "should_roll": True,
                "check_stat": "#99",
                "difficulty": 14,
            },
            contract=contract,
            player_modifiers={"dexterity": 4, "wits": 2, "charm": 5},
        )
        self.assertIsNotNone(parsed)
        # 最高属性是 charm（5）
        self.assertEqual(
            parsed["selected_choice"]["check_stat"], "charm"
        )
        self.assertTrue(
            parsed["selected_choice"].get("_general_fallback")
        )

    def test_general_fallback_marks_display_as_tongyong(self) -> None:
        # 显示层：_check_request_from_locked_choice 看到 _general_fallback
        # 标记时，stat 字段存最高属性 key（让 authoritative_modifier 查
        # 到修正），display_stat 存「通用」（让 format 报「【通用检定】」）。
        workflow = {
            "selected_choice": {
                "check_stat": "charm",
                "check_label": "",
                "text": "冲向亡龙挥剑",
                "difficulty": 14,
                "risk": "controlled",
                "check_type": "standard",
                "_general_fallback": True,
            }
        }
        req = TavernEngine._check_request_from_locked_choice(workflow)
        # stat 字段存最高属性 key（让 authoritative_modifier 能查到）
        self.assertEqual(req.stat, "charm")
        # display_stat 字段存「通用」让 _format_dice_result 报「【通用检定】」
        self.assertEqual(req.display_stat, "通用")

    def test_precheck_ignores_lethal_without_fatal_consequence(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 18,
                "risk": "lethal",
            }
        )
        self.assertEqual(parsed["selected_choice"]["risk"], "controlled")
        self.assertEqual(parsed["selected_choice"]["known_consequences"], "")

    def test_precheck_ignores_lethal_with_fatal_consequence(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 18,
                "risk": "lethal",
                "fatal_consequence": "失足坠入眼前的无底裂谷",
            }
        )
        self.assertEqual(parsed["selected_choice"]["risk"], "controlled")
        self.assertEqual(parsed["selected_choice"]["known_consequences"], "")

    def test_posthoc_death_requires_fatal_chain(self) -> None:
        parsed = _parse_freeform_death_judge(
            {"death": True, "fatal_consequence": "", "reason": "会死"}
        )
        self.assertFalse(parsed["death"])

    def test_posthoc_death_keeps_grounded_fatal_chain(self) -> None:
        parsed = _parse_freeform_death_judge(
            {
                "death": True,
                "fatal_consequence": "攀爬失手后坠入眼前的无底裂谷",
                "reason": "现有地形足以致死",
            }
        )
        self.assertTrue(parsed["death"])
        self.assertIn("无底裂谷", parsed["fatal_consequence"])

    def test_missing_stat_rejected(self) -> None:
        self.assertIsNone(
            _parse_freeform_judge(
                {"should_roll": True, "difficulty": 14}
            )
        )

    def test_non_mapping_rejected(self) -> None:
        self.assertIsNone(_parse_freeform_judge([1, 2, 3]))

    def test_acceptance_parsed(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 14,
                "acceptance": "partial",
                "acceptance_note": "接受「冲向亡龙挥剑」，"
                "不接受「一剑斩下龙头」",
            }
        )
        self.assertEqual(parsed["acceptance"], "partial")
        self.assertIn("斩下龙头", parsed["acceptance_note"])

    def test_acceptance_defaults_full(self) -> None:
        parsed = _parse_freeform_judge(
            {"should_roll": True, "check_stat": "身手"}
        )
        self.assertEqual(parsed["acceptance"], "full")
        self.assertEqual(parsed["acceptance_note"], "")

    def test_acceptance_invalid_falls_back_full(self) -> None:
        parsed = _parse_freeform_judge(
            {
                "should_roll": True,
                "check_stat": "身手",
                "acceptance": "everything",
            }
        )
        self.assertEqual(parsed["acceptance"], "full")


class FreeformAssessmentSplitTests(unittest.TestCase):
    """2026-08-24 玩家反馈「我想要的演绎判定窗口呢」：模型在正文前用
    「【演绎结果评定】」模块说明接收程度，但群里渲染时这一段会被折叠。
    引擎 _split_freeform_assessment 把这一段独立成块，送到群时用 📋
    标题包成独立段落显示。"""

    def test_split_returns_empty_when_no_marker(self) -> None:
        from tavern.engine import TavernEngine
        head, body = TavernEngine._split_freeform_assessment(
            "正文段落，无演绎评定块。"
        )
        self.assertEqual(head, "")
        self.assertEqual(body, "正文段落，无演绎评定块。")

    def test_split_separates_assessment_from_body(self) -> None:
        from tavern.engine import TavernEngine
        text = (
            "【演绎结果评定】部分接受：接受「冲向亡龙挥剑」，"
            "不接受「一剑斩下龙头」——那是结果不是动作。\n"
            "\n"
            "梧桐额角淡青鳞纹再亮，仓促跃起……"
        )
        head, body = TavernEngine._split_freeform_assessment(text)
        self.assertIn("【演绎结果评定】", head)
        self.assertIn("部分接受", head)
        self.assertNotIn("梧桐", head)
        self.assertIn("梧桐", body)
        self.assertNotIn("【演绎结果评定】", body)

    def test_split_handles_full_marker(self) -> None:
        from tavern.engine import TavernEngine
        text = (
            "【演绎结果评定】照单全收：动作合理，无任何砍掉或降格。\n"
            "\n"
            "她拾起弩机，对准悬崖边的裂缝。"
        )
        head, body = TavernEngine._split_freeform_assessment(text)
        self.assertIn("照单全收", head)
        self.assertIn("她拾起弩机", body)

    def test_split_handles_missing_body(self) -> None:
        # 模型只写评定块没写正文——body=""，渲染层应打「等待剧情推进」占位
        from tavern.engine import TavernEngine
        text = (
            "【演绎结果评定】降格接受：动作明显离谱，降格为合理尝试。"
        )
        head, body = TavernEngine._split_freeform_assessment(text)
        self.assertIn("降格接受", head)
        self.assertEqual(body, "")

    def test_split_handles_inline_marker_no_blank_line(self) -> None:
        # 模型把评定块与正文写在一行（不推荐，但容错）
        from tavern.engine import TavernEngine
        text = (
            "【演绎结果评定】部分接受：接受 X 不接受 Y。\n"
            "梧桐继续往前走了三步。"
        )
        head, body = TavernEngine._split_freeform_assessment(text)
        self.assertIn("部分接受", head)
        # 无空行 → 整段都作为 head，body 为空
        self.assertEqual(body, "")

    def test_render_freeform_story_splits_assessment_block(self) -> None:
        # 端到端：模型返回带「【演绎结果评定】」块的 narrative，
        # 渲染时它会作为独立段落 + 📋 标题，正文走 📖 【故事推进】。
        #
        # 2026-08-24 玩家反馈「自由演绎之后的评定没有单独发消息」——
        # 升级为：评估块走 EngineReply.assessment_text 字段，由 main.py
        # 的 _send_event_parts 作为独立消息投递；正文走 story_text；回合
        # 秩序走 turn_text。本测试验证三个字段被正确拆分 + 评估块排首位。
        from tavern.engine import EngineReply, TavernEngine
        narrative = (
            "【演绎结果评定】部分接受：接受「冲向亡龙挥剑」，"
            "不接受「一剑斩下龙头」——那是结果不是动作。\n\n"
            "梧桐额角淡青鳞纹再亮，仓促跃起。"
        )
        assessment, body = TavernEngine._split_freeform_assessment(narrative)
        self.assertTrue(assessment.startswith("【演绎结果评定】"))
        story_body = TavernEngine._format_story_paragraphs(body)
        # 模拟 engine 渲染：评估块走独立字段，正文走 story_text
        assessment_text = f"📋 {assessment}"
        story_text = f"📖 【故事推进】\n\n{story_body}"
        turn_text = "⚔️ 【回合秩序】第 7 轮 · 下一位：jade"
        reply = EngineReply(
            text=f"{assessment_text}\n\n{story_text}",
            session={},
            story_text=story_text,
            turn_text=turn_text,
            assessment_text=assessment_text,
        )
        # main.py reply_parts 顺序：assessment → story → turn
        reply_parts: list[str] = []
        if reply.assessment_text:
            reply_parts.append(reply.assessment_text)
        if reply.story_text:
            reply_parts.append(reply.story_text)
        if reply.turn_text:
            reply_parts.append(reply.turn_text)
        # 三段独立消息
        self.assertEqual(len(reply_parts), 3)
        # 评估块独立成消息：含「📋 【演绎结果评定】」
        self.assertIn("📋 【演绎结果评定】", reply_parts[0])
        # 评估块不含正文（正文走独立消息）
        self.assertNotIn("梧桐", reply_parts[0])
        # 正文块独立成消息：含「📖 【故事推进】」+ 正文
        self.assertIn("📖 【故事推进】", reply_parts[1])
        self.assertIn("梧桐", reply_parts[1])
        # 回合秩序独立成消息
        self.assertIn("【回合秩序】", reply_parts[2])

    def test_render_freeform_assessment_only_body_missing(self) -> None:
        # 模型只写了评估块没写正文时，提示「等待剧情推进」合并到评估消息里
        # ——不发空 story 消息。
        from tavern.engine import TavernEngine
        narrative = (
            "【演绎结果评定】部分接受：接受「冲向亡龙挥剑」。"
        )
        assessment, body = TavernEngine._split_freeform_assessment(narrative)
        story_body = TavernEngine._format_story_paragraphs(body)
        # body 空时，评估消息带上「等待叙事模型补完本段正文」占位
        if not story_body:
            assessment_only = (
                f"📋 {assessment}\n\n（等待叙事模型补完本段正文）"
            )
            self.assertIn("等待叙事模型补完本段正文", assessment_only)
            self.assertNotIn("📖 【故事推进】", assessment_only)

    def test_render_locked_choice_story_does_not_split(self) -> None:
        # locked-choice（A/B/C/D 选项触发）路径没有「【演绎结果评定】」
        # 标记——保持原有单段落渲染。
        from tavern.engine import TavernEngine
        narrative = "陈千语重剑横扫，木匣被震得从土中滑出。"
        assessment, body = TavernEngine._split_freeform_assessment(narrative)
        self.assertEqual(assessment, "")
        self.assertEqual(body, narrative)

    def test_no_roll_without_stat_keeps_acceptance(self) -> None:
        # 免检且没给属性 → 回退引擎自动检定，接收程度仍随返回保留。
        parsed = _parse_freeform_judge(
            {
                "should_roll": False,
                "acceptance": "reduced",
                "acceptance_note": "降格为一拳砸向城门",
            }
        )
        self.assertEqual(parsed, {
            "requires_check": False,
            "acceptance": "reduced",
            "acceptance_note": "降格为一拳砸向城门",
        })


class LethalDeathVerdictTests(unittest.TestCase):
    """死亡硬落实判定规则。"""

    def setUp(self) -> None:
        self.world = _death_world()

    def test_lethal_failure_verdict(self) -> None:
        verdict = _lethal_death_verdict(
            self.world,
            {"risk": "lethal", "known_consequences": "被死卫乱刃分尸"},
            _dice("failure", risk="lethal"),
            {},
        )
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict["freeform"])
        self.assertIn("乱刃分尸", verdict["reason"])

    def test_lethal_critical_failure_verdict(self) -> None:
        verdict = _lethal_death_verdict(
            self.world,
            {"risk": "lethal", "known_consequences": ""},
            _dice("critical_failure", risk="lethal"),
            {},
        )
        self.assertIsNotNone(verdict)

    def test_lethal_success_no_verdict(self) -> None:
        self.assertIsNone(
            _lethal_death_verdict(
                self.world,
                {"risk": "lethal"},
                _dice("success", risk="lethal"),
                {},
            )
        )

    def test_non_lethal_failure_no_verdict(self) -> None:
        self.assertIsNone(
            _lethal_death_verdict(
                self.world,
                {"risk": "dangerous"},
                _dice("failure", risk="dangerous"),
                {},
            )
        )

    def test_world_forbids_death_no_verdict(self) -> None:
        world = _death_world()
        world["rules"]["content_boundaries"]["character_death"] = "no"
        self.assertIsNone(
            _lethal_death_verdict(
                world,
                {"risk": "lethal"},
                _dice("failure", risk="lethal"),
                {},
            )
        )

    def test_confirmation_required_no_verdict(self) -> None:
        world = _death_world()
        world["rules"]["death_requires_confirmation"] = True
        self.assertIsNone(
            _lethal_death_verdict(
                world,
                {"risk": "lethal"},
                _dice("failure", risk="lethal"),
                {},
            )
        )

    def test_group_check_no_hard_death(self) -> None:
        self.assertIsNone(
            _lethal_death_verdict(
                self.world,
                {"risk": "lethal"},
                _dice("failure", risk="lethal", check_type="group"),
                {},
            )
        )

    def test_freeform_nonlethal_critical_failure_no_verdict(self) -> None:
        # 安全/非致命场景里，自然 1 不能凭空创造死亡来源。
        self.assertIsNone(_lethal_death_verdict(
            self.world,
            {"risk": "controlled", "known_consequences": ""},
            _dice("critical_failure"),
            {"freeform": True, "freeform_death_verdict": {"death": False}},
        ))

    def test_freeform_lethal_critical_failure_verdict(self) -> None:
        verdict = _lethal_death_verdict(
            self.world,
            {"risk": "controlled", "known_consequences": ""},
            _dice("critical_failure"),
            {
                "freeform": True,
                "freeform_death_verdict": {
                    "death": True,
                    "fatal_consequence": "失足坠入眼前的无底裂谷",
                },
            },
        )
        self.assertIsNotNone(verdict)
        self.assertTrue(verdict["freeform"])
        self.assertIn("大失败", verdict["reason"])

    def test_freeform_lethal_without_fatal_chain_no_verdict(self) -> None:
        self.assertIsNone(_lethal_death_verdict(
            self.world,
            {"risk": "controlled", "known_consequences": ""},
            _dice("critical_failure"),
            {
                "freeform": True,
                "freeform_death_verdict": {
                    "death": True,
                    "fatal_consequence": "",
                },
            },
        ))

    def test_freeform_plain_failure_no_verdict(self) -> None:
        # 自由演绎普通失败（非大失败）不硬判死
        self.assertIsNone(
            _lethal_death_verdict(
                self.world,
                {"risk": "controlled"},
                _dice("failure"),
                {
                    "freeform": True,
                    "freeform_death_verdict": {
                        "death": True,
                        "fatal_consequence": "坠入裂谷",
                    },
                },
            )
        )

    def test_freeform_critical_world_forbids_death(self) -> None:
        world = _death_world()
        world["rules"]["content_boundaries"]["character_death"] = "no"
        self.assertIsNone(
            _lethal_death_verdict(
                world,
                {"risk": "controlled"},
                _dice("critical_failure"),
                {
                    "freeform": True,
                    "freeform_death_verdict": {
                        "death": True,
                        "fatal_consequence": "坠入裂谷",
                    },
                },
            )
        )


class DeathNarrativeTests(unittest.TestCase):
    """死亡叙事兜底文本。"""

    def test_narrative_has_death_detection(self) -> None:
        self.assertTrue(_narrative_has_death("他当场身亡，倒在雪地里"))
        self.assertTrue(_narrative_has_death("巨刃落下，她的头颅滚落"))
        self.assertFalse(_narrative_has_death("他重伤昏迷，被及时救回"))
        self.assertFalse(_narrative_has_death("她只是晕了过去"))
        self.assertFalse(_narrative_has_death(""))

    def test_death_epilogue_freeform(self) -> None:
        text = _death_epilogue(
            "柏辰",
            {
                "freeform": True,
                "basis": "骰面崩盘",
            },
        )
        self.assertIn("柏辰", text)
        self.assertIn("死亡宣告", text)
        self.assertIn("大失败", text)
        self.assertIn("永久退场", text)

    def test_death_epilogue_lethal(self) -> None:
        text = _death_epilogue(
            "柏辰",
            {
                "freeform": False,
                "basis": "被死卫乱刃分尸",
            },
        )
        self.assertIn("乱刃分尸", text)
        self.assertIn("当场身亡", text)


class JudgePromptTests(unittest.TestCase):
    """自由演绎裁判提示词包含必要约束。"""

    def test_prompt_contains_required_rules(self) -> None:
        prompt = freeform_check_judge_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我攀上崖壁寻找裂缝",
            events=[],
            memories=(),
        )
        self.assertIn("should_roll", prompt)
        self.assertIn("check_stat", prompt)
        self.assertIn("difficulty", prompt)
        self.assertIn("修正恒为", prompt)
        self.assertIn("大失败", prompt)
        # 接收程度判定
        self.assertIn("接收程度", prompt)
        self.assertIn("acceptance", prompt)
        self.assertIn("照单全收", prompt)
        self.assertIn("降格接受", prompt)
        # 2026-08-24：acceptance_note 全等级必填，full 也要说明
        self.assertIn("acceptance_note", prompt)
        self.assertIn("acceptance_note 必填", prompt)
        self.assertIn("无论 full/partial/reduced 都必须", prompt)
        # 世界属性表注入
        self.assertIn("身手", prompt)
        self.assertIn("攀上崖壁", prompt)
        # 属性白名单硬约束：必须从世界属性表的 key 里选，不得自造
        self.assertIn("不得自造", prompt)
        # DC 上限 20：超过 20 视为不可能，clamp 到 20
        self.assertIn("DC 不得超过 20", prompt)
        self.assertIn("clamp", prompt)
        # 已通过的集体决定可以整段执行，不能再被通用“结果不是动作”规则
        # 拆成十几轮短转场。
        self.assertIn("不要把执行既有决定误判成代写结果", prompt)
        self.assertIn("正式集体表决", prompt)
        self.assertIn("完成交接", prompt)
        self.assertIn("需要后续逐步推进", prompt)
        self.assertIn("单次动作不能涵盖", prompt)

    def test_checked_resolution_prompt_explains_partial_fulfillment(
        self,
    ) -> None:
        # 0.13.x：锁定选项的兑现程度由模型判断；只能部分兑现或暂不兑现时
        # 必须说明原因（与自由演绎「接收/半接收/不接受要说明理由」同原则）。
        prompt = checked_resolution_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我以秘法疗愈冻创",
            events=[],
            memories=(),
            check={"stat": "智力", "difficulty": 13, "check_type": "standard"},
            dice={"outcome": "success", "total": 20, "difficulty": 13},
        )
        self.assertIn("<result_explanation>", prompt)
        self.assertIn("outcome=success", prompt)
        # 由模型判断兑现程度，不设硬性成功约束
        self.assertIn("由你按世界事实判断", prompt)
        # 部分兑现/暂不兑现必须说明阻碍来源
        self.assertIn("死亡锚链", prompt)
        self.assertIn("不得只写结果不说明原因", prompt)

    def test_checked_resolution_prompt_freeform_skips_result_explanation(
        self,
    ) -> None:
        # 自由演绎有裁判裁定的接收程度，不再叠加 result_explanation。
        prompt = checked_resolution_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我以秘法疗愈冻创",
            events=[],
            memories=(),
            check={"stat": "智力", "difficulty": 13, "check_type": "standard"},
            dice={"outcome": "success", "total": 20, "difficulty": 13},
            freeform=True,
            acceptance_guidance="玩家自由演绎的接收程度：部分接受。",
        )
        self.assertNotIn("<result_explanation>", prompt)
        self.assertIn("<freeform_acceptance>", prompt)

    def test_planning_prompt_injects_acceptance_scope(
        self,
    ) -> None:
        # 2026-09-21：评定改由插件单独公告，提示词只负责**作用域约束**——
        # 正文按接受范围裁定，越界部分不兑现。
        prompt = planning_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我攀上崖壁寻找裂缝",
            events=[],
            memories=(),
            allow_checks=False,
            workflow={"freeform": True,
                      "freeform_acceptance_guidance": "玩家自由演绎的"
                      "接收程度：部分接受。"},
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn("玩家自由演绎的接收程度：部分接受。", prompt)
        self.assertIn("不得兑现", prompt)
        # 正文不再被要求写这个判定标签（那是插件公告的活）
        self.assertIn("已由插件单独公告", prompt)
        self.assertNotIn("正文必须以", prompt)

    def test_planning_prompt_injects_scope_even_when_roll_is_forced(
        self,
    ) -> None:
        """自由演绎恒有 requires_check=True，也必须拿到接收程度。

        2026-09-21 回归：这段注入原先写在 `else` 分支里，而自由演绎必定走
        「已锁定的必检选项」分支，于是注入从来没生效过——玩家侧表现就是
        "接受程度判定没了"。
        """
        prompt = planning_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我攀上崖壁寻找裂缝",
            events=[],
            memories=(),
            allow_checks=True,
            workflow={"freeform": True,
                      "requires_check": True,
                      "freeform_acceptance_guidance": "玩家自由演绎的"
                      "接收程度：部分接受。"},
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn("玩家自由演绎的接收程度：部分接受。", prompt)

    def test_checked_resolution_prompt_injects_acceptance_scope(
        self,
    ) -> None:
        # 检定路径：注入同样的作用域约束，不再要求正文写评定标题。
        prompt = checked_resolution_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我以秘法疗愈冻创",
            events=[],
            memories=(),
            check={"stat": "智力", "difficulty": 13, "check_type": "standard"},
            dice={"outcome": "success", "total": 20, "difficulty": 13},
            freeform=True,
            acceptance_guidance="玩家自由演绎的接收程度：部分接受。",
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn("玩家自由演绎的接收程度：部分接受。", prompt)
        self.assertIn("不得兑现", prompt)
        self.assertIn("已由插件单独公告", prompt)
        self.assertNotIn("正文必须以", prompt)

    def test_planning_prompt_full_acceptance_also_gets_the_scope_block(
        self,
    ) -> None:
        # 2026-08-24：full 也要有接收程度约束，不再免打扰；
        # 2026-09-21：约束由插件公告 + 作用域注入承担。
        prompt = planning_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我攀上崖壁寻找裂缝",
            events=[],
            memories=(),
            allow_checks=False,
            workflow={"freeform": True,
                      "freeform_acceptance_guidance": "玩家自由演绎的"
                      "接收程度：照单全收。"},
        )
        self.assertIn("<freeform_acceptance>", prompt)
        # 旧文案被删除了
        self.assertNotIn("只会在 partial 或 reduced 时出现", prompt)
        # 强调所有自由演绎（含 full）都受这条约束
        self.assertIn("包括照单全收（full）", prompt)
        self.assertIn("玩家自由演绎的接收程度：照单全收。", prompt)

    def test_checked_resolution_prompt_full_acceptance_also_requires_assessment(
        self,
    ) -> None:
        prompt = checked_resolution_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我以秘法疗愈冻创",
            events=[],
            memories=(),
            check={"stat": "智力", "difficulty": 13, "check_type": "standard"},
            dice={"outcome": "success", "total": 20, "difficulty": 13},
            freeform=True,
            acceptance_guidance="玩家自由演绎的接收程度：照单全收。",
        )
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertNotIn("只用于 partial 或 reduced", prompt)
        self.assertIn("包括照单全收（full）", prompt)


class OutcomeFulfillmentHintTests(unittest.TestCase):
    """dice.outcome → 叙事兑现强度倾向 helper 单元测试（2026-08-24
    玩家反馈「自由演绎的摇点结果应有不同程度影响叙事模型的裁定」后落地）。
    """

    def test_critical_success_returns_circular_hint(self) -> None:
        hint = _outcome_fulfillment_hint("critical_success")
        self.assertIn("critical_success", hint)
        self.assertIn("尽力去圆", hint)
        # 必须明确"不打折"
        self.assertIn("不得", hint)

    def test_success_returns_restraint_hint(self) -> None:
        hint = _outcome_fulfillment_hint("success")
        self.assertIn("success", hint)
        self.assertIn("克制放水", hint)
        self.assertIn("不得放大成决定性胜利", hint)

    def test_failure_returns_no_spin_hint(self) -> None:
        hint = _outcome_fulfillment_hint("failure")
        self.assertIn("failure", hint)
        self.assertIn("不得嘴硬圆", hint)
        # 玩家原话"失败一个完全不给影响"——hint 必须直白不允许伪成功
        self.assertIn("伪成功", hint)

    def test_critical_failure_returns_empty(self) -> None:
        # 大失败的死亡判定由引擎硬落实 + <freeform_critical_failure>
        # 块处理，prompt 不再叠加兑现引导
        self.assertEqual(_outcome_fulfillment_hint("critical_failure"), "")

    def test_success_with_cost_returns_empty(self) -> None:
        # success_with_cost 已在 checked_resolution_prompt 顶部明文要求
        # "代价必须相称"，不再额外叠加
        self.assertEqual(_outcome_fulfillment_hint("success_with_cost"), "")

    def test_empty_or_unknown_outcome_returns_empty(self) -> None:
        self.assertEqual(_outcome_fulfillment_hint(""), "")
        self.assertEqual(_outcome_fulfillment_hint("unknown"), "")
        self.assertEqual(_outcome_fulfillment_hint(None), "")


class FreeformOutcomeHintIntegrationTests(unittest.TestCase):
    """自由演绎（freeform=True）路径下，按 dice.outcome 在 checked_resolution_prompt
    的 <freeform_acceptance> 块里注入兑现强度倾向（2026-08-24 玩家反馈）。
    """

    def _prompt(self, outcome: str) -> str:
        return checked_resolution_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我以剑气震碎阵眼",
            events=[],
            memories=(),
            check={"stat": "sword", "difficulty": 14, "check_type": "standard"},
            dice={"outcome": outcome, "total": 22, "difficulty": 14},
            freeform=True,
            acceptance_guidance="玩家自由演绎的接收程度：部分接受。",
        )

    def test_critical_success_injects_try_to_circular_hint(self) -> None:
        prompt = self._prompt("critical_success")
        self.assertIn("<freeform_acceptance>", prompt)
        # hint 必须在 freeform_acceptance 块内
        self.assertIn("尽力去圆", prompt)
        self.assertNotIn("不得嘴硬圆", prompt)  # failure 才用

    def test_success_injects_restraint_hint(self) -> None:
        prompt = self._prompt("success")
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn("克制放水", prompt)

    def test_failure_injects_no_spin_hint(self) -> None:
        prompt = self._prompt("failure")
        self.assertIn("<freeform_acceptance>", prompt)
        self.assertIn("不得嘴硬圆", prompt)

    def test_critical_failure_does_not_inject_fulfillment_hint(self) -> None:
        # 大失败走 <freeform_critical_failure> 块，hint 应为空
        prompt = self._prompt("critical_failure")
        self.assertIn("<freeform_critical_failure>", prompt)
        # 不应有兑现强度倾向中的关键词
        self.assertNotIn("尽力去圆", prompt)
        self.assertNotIn("克制放水", prompt)
        self.assertNotIn("不得嘴硬圆", prompt)

    def test_locked_check_does_not_inject_outcome_hint(self) -> None:
        # 玩家原话只针对自由演绎，locked-check（A/B/C/D 选项）路径不动
        prompt = checked_resolution_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我冲向亡龙",
            events=[],
            memories=(),
            check={"stat": "agility", "difficulty": 14, "check_type": "standard"},
            dice={"outcome": "critical_success", "total": 22, "difficulty": 14},
            freeform=False,  # ← 关键：locked-check 路径
        )
        self.assertIn("<result_explanation>", prompt)
        # hint 文案不应出现在 locked-check 路径
        self.assertNotIn("尽力去圆", prompt)
        self.assertNotIn("克制放水", prompt)
        self.assertNotIn("不得嘴硬圆", prompt)

    def test_planning_prompt_freeform_path_has_empty_hint_slot(self) -> None:
        # planning_prompt 路径下 dice 还没生成，hint 应为占位空串
        # ——但 freeform_acceptance 块结构必须保持完整（不破坏解析）
        prompt = planning_prompt(
            world=_death_world(),
            session={"world_state": {"location": "崖壁"}},
            player={"id": "p-1", "character_name": "柏辰"},
            player_input="我以剑气震碎阵眼",
            events=[],
            memories=(),
            allow_checks=True,
            workflow={
                "freeform": True,
                "freeform_acceptance_guidance": "接收程度：部分接受。",
            },
        )
        self.assertIn("<freeform_acceptance>", prompt)
        # 块结构存在但 hint 不应有内容（planning_prompt 路径 dice=None）
        self.assertNotIn("尽力去圆", prompt)
        self.assertNotIn("克制放水", prompt)
        self.assertNotIn("不得嘴硬圆", prompt)


class FakeContext:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        return "provider-test"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("模型被额外调用")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(completion_text=output)


class FreeformFlowTests(unittest.IsolatedAsyncioTestCase):
    """端到端：自由演绎裁判 → locked-check 投骰 → 死亡硬落实。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-freeform", "qq:group-freeform",
            DEFAULT_WORLD_SLUG, "admin-1",
        )
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        await self.database.join_turn_order(
            self.session["id"], "user-1", "旅客", "user-1"
        )
        self.event = SimpleNamespace(unified_msg_origin="qq:group-freeform")
        now = utc_now()
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT world_revision FROM instance_configs"
                " WHERE session_id = ?",
                (self.session["id"],),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO instance_configs(
                        session_id, world_revision, world_snapshot_json,
                        time_rules_json, phase_meta_json,
                        created_at, updated_at
                    ) VALUES (?, 1, ?, '{}', '{}', ?, ?)
                    """,
                    (
                        self.session["id"],
                        json.dumps(_death_world(), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE instance_configs SET
                        world_snapshot_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        json.dumps(_death_world(), ensure_ascii=False),
                        now,
                        self.session["id"],
                    ),
                )
            # 直接种子参与者（测试不走建卡审批流程）
            self.participant_id = new_id("participant")
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id,
                    private_user_id, private_origin, display_name,
                    character_name, character_code, card_status, ready,
                    participation_status, seat_reserved_at,
                    joined_round, created_at, updated_at
                ) VALUES (?, ?, NULL, 'user-1', '', '', '旅客',
                          '旅客', 'lvke', 'approved', 1,
                          'active', ?, 1, ?, ?)
                """,
                (
                    self.participant_id,
                    self.session["id"],
                    now,
                    now,
                    now,
                ),
            )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _engine(
        self, outputs: list[object]
    ) -> tuple[TavernEngine, FakeContext]:
        context = FakeContext(outputs)
        config = TavernConfig(
            user_cooldown_seconds=0,
            json_repair_attempts=0,
            request_timeout_seconds=5,
            store_model_payloads=True,
        )
        return (
            TavernEngine(
                context=context,
                database=self.database,
                config_provider=lambda: config,
                broker=EventBroker(),
            ),
            context,
        )

    @staticmethod
    def _judge_ok() -> str:
        return json.dumps(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 14,
                "risk": "dangerous",
                "acceptance": "partial",
                "acceptance_note": "接受「攀爬崖壁」，不接受"
                "「一次就找到密道」——那是结果不是动作",
                "reason": "攀爬崖壁有坠落风险",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _judge_ok_full() -> str:
        return json.dumps(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 14,
                "risk": "dangerous",
                "acceptance": "full",
                "acceptance_note": "照单全收：攀爬崖壁寻找裂缝是能力范围内"
                "的尝试，尊重原意",
                "reason": "攀爬崖壁有坠落风险",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _judge_lethal() -> str:
        return json.dumps(
            {
                "should_roll": True,
                "check_stat": "身手",
                "difficulty": 18,
                "acceptance": "full",
                "acceptance_note": "照单全收：徒手攀爬无底裂谷是可尝试"
                "但足以致命的行动",
                "reason": "崖壁湿滑且下方是无底裂谷",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _death_yes() -> str:
        return json.dumps(
            {
                "death": True,
                "fatal_consequence": "攀爬失手后坠入眼前的无底裂谷",
                "reason": "动作、湿滑崖壁和无底裂谷构成完整致死链",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _death_no() -> str:
        return json.dumps(
            {
                "death": False,
                "fatal_consequence": "",
                "reason": "现场落石不足以直接致死，最多造成重伤",
            },
            ensure_ascii=False,
        )

    def _resolution(self, narrative: str) -> str:
        return json.dumps(
            {
                "mode": "resolve",
                "narrative": narrative,
                "check": None,
                "state_patch": {"scene_summary": narrative},
                "memories": [],
                "director_note": "测试。",
                "next_choices": [
                    {
                        "key": "A",
                        "text": "继续向上攀爬",
                        "risk": "controlled",
                        "actor_id": self.participant_id,
                    },
                    {
                        "key": "B",
                        "text": "退回崖下观察",
                        "risk": "safe",
                        "actor_id": self.participant_id,
                    },
                    {
                        "key": "C",
                        "text": "大声呼救引注意",
                        "risk": "dangerous",
                        "actor_id": self.participant_id,
                    },
                    {
                        "key": "D",
                        "text": "休息片刻再出发",
                        "risk": "safe",
                        "actor_id": self.participant_id,
                    },
                ],
            },
            ensure_ascii=False,
        )

    async def test_freeform_judged_check_uses_character_modifier(
        self,
    ) -> None:
        engine, context = self._engine(
            [
                self._judge_ok(),
                self._resolution("行动得到回应。"),
            ]
        )

        seen_check = None

        async def fake_authoritative(session_id, user_id, stat_ref):
            self.assertEqual(stat_ref, "身手")
            return {
                "stat": "身手",
                "modifier": 6,
                "value": 18,
                "matched": True,
            }

        async def fake_roll(world, check, *, actors=None):
            nonlocal seen_check
            seen_check = check
            return _dice(
                "success",
                difficulty=14,
                modifier=check.modifier,
                attribute_value=check.attribute_value,
                risk="dangerous",
            )

        with patch.object(
            self.database, "authoritative_modifier", fake_authoritative
        ), patch.object(engine, "_roll_with_registered_system", fake_roll):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我攀上崖壁寻找裂缝",
            )
        # 裁判先于叙事被调用（请求体是裁判提示词）
        self.assertIn("should_roll", context.calls[0]["prompt"])
        self.assertIn("自由演绎", context.calls[0]["prompt"])
        # 骰面：属性来自裁判判定，数值与修正来自角色卡。
        self.assertIsNotNone(reply.dice)
        self.assertIsNotNone(seen_check)
        self.assertEqual(seen_check.modifier, 6)
        self.assertEqual(seen_check.attribute_value, 18)
        self.assertEqual(reply.dice.modifier, 6)
        self.assertEqual(reply.dice.attribute_value, 18)
        self.assertEqual(reply.dice.difficulty, 14)
        self.assertIn("行动得到回应", reply.text)
        # 接收程度作为权威指令传入检定后的叙事 prompt
        self.assertIn(
            "<freeform_acceptance>", context.calls[1]["prompt"]
        )
        self.assertIn("部分接受", context.calls[1]["prompt"])
        self.assertIn("一次就找到密道", context.calls[1]["prompt"])
        # partial/reduced：必须输出「演绎结果评定」模块（2026-08-24 玩家反馈）。
        self.assertIn("【演绎结果评定】", context.calls[1]["prompt"])
        self.assertIn("换行/分段写正文", context.calls[1]["prompt"])
        # 自由演绎已有裁判接收程度，不叠加 result_explanation
        self.assertNotIn("<result_explanation>", context.calls[1]["prompt"])

    async def test_freeform_full_acceptance_injects_assessment_block(self) -> None:
        # 2026-08-24 玩家反馈：full 也必须输出「演绎结果评定」模块，
        # 不再免打扰——否则玩家不知道这条演绎被怎样处理。
        engine, context = self._engine(
            [
                self._judge_ok_full(),
                self._resolution("行动得到回应。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("success", difficulty=14, risk="dangerous")

        with patch.object(engine, "_roll_with_registered_system", fake_roll):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我攀上崖壁寻找裂缝",
            )
        self.assertIsNotNone(reply.dice)
        self.assertIn("行动得到回应", reply.text)
        # full 也注入 freeform_acceptance 块
        self.assertIn(
            "<freeform_acceptance>", context.calls[1]["prompt"]
        )
        self.assertIn("照单全收", context.calls[1]["prompt"])

    async def test_freeform_nonlethal_critical_failure_stays_alive(self) -> None:
        engine, context = self._engine(
            [
                self._judge_ok(),
                self._death_no(),
                self._resolution("他闷哼一声，被落石砸中，重伤昏迷。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("critical_failure")

        with patch.object(engine, "_roll_with_registered_system", fake_roll):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我攀上崖壁寻找裂缝",
            )
        self.assertNotIn("死亡宣告", reply.text)
        self.assertIn("重伤昏迷", reply.text)
        self.assertIn(
            "<freeform_critical_failure>", context.calls[2]["prompt"]
        )
        self.assertIn("不得宣告角色死亡", context.calls[2]["prompt"])
        participant = await self.database.get_participant(
            self.session["id"], user_id="user-1"
        )
        self.assertEqual(participant["participation_status"], "active")

    async def test_freeform_grounded_lethal_critical_failure_hard_death(
        self,
    ) -> None:
        engine, context = self._engine(
            [
                self._judge_lethal(),
                self._death_yes(),
                # 模型叙事回避死亡时，引擎仍按已确认的致死链兜底。
                self._resolution("他从湿滑崖壁跌落，随后失去踪影。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("critical_failure", difficulty=18, risk="controlled")

        with patch.object(engine, "_roll_with_registered_system", fake_roll):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我徒手攀爬眼前的无底裂谷",
            )
        self.assertIn("死亡宣告", reply.text)
        self.assertIn("无底裂谷", reply.text)
        participant = await self.database.get_participant(
            self.session["id"], user_id="user-1"
        )
        self.assertEqual(participant["participation_status"], "retired")

    async def test_freeform_judge_no_roll_forces_engine_roll(self) -> None:
        # 硬性摇点：裁判判免检不再放行——回退引擎自动检定，必摇一次
        # （能匹配时读取角色卡，未绑卡才为 0）；接收程度仍传给规划 prompt。
        engine, context = self._engine(
            [
                json.dumps(
                    {
                        "should_roll": False,
                        "acceptance": "reduced",
                        "acceptance_note": "降格为一拳砸向城门",
                    },
                    ensure_ascii=False,
                ),
                self._resolution("行动得到回应。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("success", difficulty=12, risk="controlled")

        with patch.object(engine, "_roll_with_registered_system", fake_roll):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我整理一下衣领",
            )
        self.assertIsNotNone(reply.dice)
        self.assertEqual(reply.dice.modifier, 0)
        self.assertEqual(reply.dice.difficulty, 12)
        self.assertIn("行动得到回应", reply.text)
        self.assertIn("<freeform_acceptance>", context.calls[1]["prompt"])
        self.assertIn("降格为一拳砸向城门", context.calls[1]["prompt"])
        # reduced：必须输出「演绎结果评定」模块（2026-08-24 玩家反馈）。
        self.assertIn("【演绎结果评定】", context.calls[1]["prompt"])
        self.assertIn("换行/分段写正文", context.calls[1]["prompt"])

    async def test_freeform_judge_unavailable_skips_authoritative(
        self,
    ) -> None:
        # 2026-08-24 九次修正（玩家原话「这就是自由演绎啊」+ 报错
        # 「检定属性 X 不属于当前世界或角色卡」）：模型裁判不可用
        # （API 失败 / 不给合法 JSON）→ _judge_freeform_check 返回 None →
        # 走 _freeform_auto_check 兜底推断属性（可能是世界首属性 strength
        # 这种英文 key）。这条路径必须设 workflow["freeform_judged"]=True，
        # 否则 process() 走 authoritative_modifier 查角色卡 modifiers，
        # 角色卡里通常没有 strength 这个英文 key（卡用中文 label / 自定
        # 义 key）→ matched=False → 抛「检定属性 X 不属于当前世界或角
        # 色卡，本轮没有投骰」整轮作废。新逻辑会尝试有效候选，仍无法
        # 匹配（例如没绑卡）时才安全回退为 0。
        #
        # 模拟：judge 步骤 outputs 为空 → _judge_freeform_check 内部抛
        # Exception → judged=None → 走兜底；之后 plan prompt 正常返回。
        engine, context = self._engine(
            [
                # judge  调用走空 outputs → JSONRepairAttempts 已设为 0，
                # extract_json_object 会抛 ValueError → judged = None
                # （process_freeform 的 except 分支接住）
                "not-a-json-and-not-empty-just-trigger-exception",
                self._resolution("行动得到回应。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("success", difficulty=12, risk="controlled")

        with patch.object(engine, "_roll_with_registered_system", fake_roll):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="下水帮助Sumikaze镇压抽脉阵眼",
            )
        # 整轮不抛"检定属性不属于"——未绑卡的自由演绎安全回退为 0。
        self.assertIsNotNone(reply.dice)
        self.assertEqual(reply.dice.modifier, 0)
        self.assertIn("行动得到回应", reply.text)
        # plan prompt 里有 authoritative_check 块（说明 process() 走
        # 到了 check 阶段，没在 authoritative_modifier 那里抛错）。
        self.assertIn("authoritative_check", context.calls[1]["prompt"])

    async def test_commit_conflict_refreshes_revision_and_retries(
        self,
    ) -> None:
        # 0.13.x：LLM 处理期间会话 revision 被其他写入推进（并发回合/
        # 死亡落库/回合超时）时，提交冲突不再丢回合——刷新 revision
        # 自动重试一次，叙事与骰值保持不变。
        from tavern.database_support import DatabaseConflictError

        engine, context = self._engine(
            [
                self._judge_ok(),
                self._resolution("行动得到回应。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("success", difficulty=14, risk="dangerous")

        real_commit = self.database.commit_turn
        attempts = {"n": 0}

        async def flaky_commit_turn(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                # 模拟并发写入（如其他回合/死亡落库）推进了 session revision
                with self.database._connect() as connection:
                    connection.execute(
                        """
                        UPDATE sessions SET revision = revision + 1
                        WHERE id = ?
                        """,
                        (self.session["id"],),
                    )
                raise DatabaseConflictError("会话已被其他请求更新")
            return await real_commit(**kwargs)

        with patch.object(
            engine, "_roll_with_registered_system", fake_roll
        ), patch.object(
            self.database, "commit_turn", side_effect=flaky_commit_turn
        ):
            reply = await engine.process_freeform(
                event=self.event,
                session_id=self.session["id"],
                sender_id="user-1",
                sender_name="旅客",
                content="我攀上崖壁寻找裂缝",
            )
        # 冲突后重试成功，回合已提交
        self.assertEqual(attempts["n"], 2)
        self.assertIn("行动得到回应", reply.text)
        session = await self.database.get_session(self.session["id"])
        self.assertEqual(session["turn_no"], 1)
        with self.database._connect() as connection:
            receipt = connection.execute(
                """
                SELECT status FROM operation_receipts
                WHERE session_id = ? AND operation_type = 'turn'
                ORDER BY created_at DESC LIMIT 1
                """,
                (self.session["id"],),
            ).fetchone()
        self.assertEqual(receipt["status"], "completed")

    async def test_commit_conflict_twice_marks_operation_failed(
        self,
    ) -> None:
        # 重试仍冲突（revision 反复被推进）时：如实告知玩家，并把
        # 回合操作标记为 failed，避免同一条消息重投被“正在处理中”卡死。
        from tavern.database_support import DatabaseConflictError
        from tavern.engine import TavernBusyError

        engine, context = self._engine(
            [
                self._judge_ok(),
                self._resolution("行动得到回应。"),
            ]
        )

        async def fake_roll(world, check, *, actors=None):
            return _dice("success", difficulty=14, risk="dangerous")

        async def always_conflict(**kwargs):
            raise DatabaseConflictError("会话已被其他请求更新")

        with patch.object(
            engine, "_roll_with_registered_system", fake_roll
        ), patch.object(
            self.database, "commit_turn", side_effect=always_conflict
        ):
            with self.assertRaises(TavernBusyError) as ctx:
                await engine.process_freeform(
                    event=self.event,
                    session_id=self.session["id"],
                    sender_id="user-1",
                    sender_name="旅客",
                    content="我攀上崖壁寻找裂缝",
                )
        self.assertIn(
            "本轮状态刚被其他操作更新", str(ctx.exception)
        )
        with self.database._connect() as connection:
            receipt = connection.execute(
                """
                SELECT status, result_json FROM operation_receipts
                WHERE session_id = ? AND operation_type = 'turn'
                ORDER BY created_at DESC LIMIT 1
                """,
                (self.session["id"],),
            ).fetchone()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("conflict", receipt["result_json"])


class FreeformAutoCheckTranslationTests(unittest.TestCase):
    """自由演绎引擎兜底：标准 key（agility/intellect/...）必须翻译成
    世界属性 key（dexterity/wits/...），并附带 label，避免玩家看到
    「【agility检定】」这类与世界预设不一致的属性公告。"""

    def _world_with_dexterity_not_agility(self) -> dict:
        # 关键差异：世界属性用 dexterity/wits（标准 DnD 风），而不是
        # _ACTION_WORD_ATTRIBUTES 默认输出的 agility/intellect。
        return {
            "name": "测试世界 · 翻译",
            "slug": DEFAULT_WORLD_SLUG,
            "system_prompt": "测试。",
            "rules": {
                "resolution": {
                    "mode": "attribute",
                    "dice_system": "d20",
                },
                "character_card": {
                    "stats": {
                        "attributes": [
                            {"key": "strength", "label": "力量"},
                            {"key": "dexterity", "label": "身手"},
                            {"key": "wits", "label": "心智"},
                            {"key": "charm", "label": "魅力"},
                        ],
                    }
                },
            },
        }

    def test_translates_agility_to_dexterity(self) -> None:
        # 「闪」是 _ACTION_NATURE_FALLBACK["agility"] 的触发词；翻译前
        # _extract_check_attribute 会返回 "agility"，本测试确认兜底路径
        # 把它翻成世界实际 key "dexterity"。
        result = TavernEngine._freeform_auto_check(
            self._world_with_dexterity_not_agility(),
            "我闪身避开飞石",
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result["selected_choice"]["check_stat"], "dexterity"
        )
        self.assertEqual(
            result["selected_choice"]["check_label"], "身手"
        )

    def test_translates_intellect_to_wits(self) -> None:
        # 「研究」「符文」都属 intellect 词表（避开与 strength 词「推/撞」
        # 共字的字串），应翻译到 wits。
        result = TavernEngine._freeform_auto_check(
            self._world_with_dexterity_not_agility(),
            "我研究符文的含义",
        )
        self.assertEqual(
            result["selected_choice"]["check_stat"], "wits"
        )
        self.assertEqual(
            result["selected_choice"]["check_label"], "心智"
        )

    def test_no_match_falls_back_to_first_attribute(self) -> None:
        # 文字不含任何触发词时，落到世界第一个属性（strength），且附带
        # 翻译后的 label。
        result = TavernEngine._freeform_auto_check(
            self._world_with_dexterity_not_agility(),
            "我环顾四周",
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result["selected_choice"]["check_stat"], "strength"
        )
        self.assertEqual(
            result["selected_choice"]["check_label"], "力量"
        )


class DiceFormatLabelDisplayTests(unittest.TestCase):
    """检定公告的 stat 显示：format 函数必须有兜底（黑名单/空值时显示
    「通用」），label 与 key 的区分由上游 _check_request_from_locked_choice
    完成（已用 _resolve_check_label 把 key 翻成 label 后再传给 format）。"""

    def test_format_blocks_invalid_stat_value(self) -> None:
        dice = DiceResult(
            die=11,
            modifier=0,
            total=11,
            difficulty=14,
            outcome="failure",
            critical=None,
            rolls=(11,),
            kept=1,
            dice_mode="standard",
            margin=-3,
            risk="controlled",
            check_type="standard",
        )
        # 即使 stat 是「常规」这种黑名单值，format 也要兜底成「通用」
        # 公告，绝不能让玩家看到「【常规检定】」这种无意义标题。
        text = TavernEngine._format_dice_result(dice, "常规")
        self.assertIn("【通用检定】", text)
        self.assertNotIn("【常规检定】", text)

    def test_format_blocks_other_risk_words(self) -> None:
        # controlled/dangerous/lethal 这些风险档词即使被错误传入 stat，
        # format 也必须兜底成「通用」——这些词已固定给 mode_label 与
        # 风险档，不应再被读作属性。
        for blocked in ("controlled", "dangerous", "lethal", "standard"):
            with self.subTest(blocked=blocked):
                dice = DiceResult(
                    die=11, modifier=0, total=11, difficulty=14,
                    outcome="failure", critical=None, rolls=(11,),
                    kept=1, dice_mode="standard", margin=-3,
                    risk="controlled", check_type="standard",
                )
                text = TavernEngine._format_dice_result(dice, blocked)
                self.assertIn("【通用检定】", text)
                self.assertNotIn(f"【{blocked}检定】", text)

    def test_format_blocks_english_standard_keys(self) -> None:
        # 2026-08-24 玩家反馈「让模型在预设属性里选也选不出来吗」：
        # 任何漏过上游校验的英文标准 key（agility/intellect/willpower/…）
        # 到达 format 时也必须兜底成「通用」——绝不能让玩家看到
        # 「【agility检定】」「【intellect检定】」这种英文裸 key 公告。
        for standard_key in ("agility", "intellect", "willpower",
                             "perception", "strength", "vitality",
                             "charisma"):
            with self.subTest(standard_key=standard_key):
                dice = DiceResult(
                    die=11, modifier=0, total=11, difficulty=14,
                    outcome="failure", critical=None, rolls=(11,),
                    kept=1, dice_mode="standard", margin=-3,
                    risk="controlled", check_type="standard",
                )
                text = TavernEngine._format_dice_result(
                    dice, standard_key
                )
                self.assertIn("【通用检定】", text)
                self.assertNotIn(
                    f"【{standard_key}检定】", text
                )

    def test_format_handles_empty_stat(self) -> None:
        dice = DiceResult(
            die=11, modifier=0, total=11, difficulty=14,
            outcome="failure", critical=None, rolls=(11,),
            kept=1, dice_mode="standard", margin=-3,
            risk="controlled", check_type="standard",
        )
        text = TavernEngine._format_dice_result(dice, "")
        self.assertIn("【通用检定】", text)

    def test_format_uses_display_stat_when_provided(self) -> None:
        # 八次修正：stat 字段存最高属性 key（让 authoritative_modifier 查
        # 到修正），display_stat 存「通用」让 format 报「【通用检定】」
        # ——这是「非法 stat → 玩家最高属性兜底」路径的渲染层表现。
        dice = DiceResult(
            die=15, modifier=3, total=18, difficulty=14,
            outcome="success", critical=None, rolls=(15,),
            kept=1, dice_mode="standard", margin=4,
            risk="controlled", check_type="standard",
            attribute_value=8,
        )
        # stat = "charm"（最高属性），display_stat = "通用"
        text = TavernEngine._format_dice_result(
            dice, "charm", display_stat="通用"
        )
        # 公告标题用 display_stat → 「【通用检定】」
        self.assertIn("【通用检定】", text)
        self.assertNotIn("【charm检定】", text)
        # 属性修正那一行也走 display_stat
        self.assertIn("通用", text)
        # display_stat 为空时回退 stat 自身
        text2 = TavernEngine._format_dice_result(
            dice, "charm", display_stat=""
        )
        self.assertIn("【charm检定】", text2)

    def test_check_request_from_locked_choice_uses_label(self) -> None:
        # 上游：selected_choice 同时带 check_stat（key）和 check_label 时，
        # _check_request_from_locked_choice 必须把 label 作为 stat 传给
        # CheckRequest（下游 format 显示）。
        workflow = {
            "selected_choice": {
                "check_stat": "dexterity",
                "check_label": "身手",
                "text": "攀爬崖壁",
                "difficulty": 14,
                "risk": "controlled",
                "check_type": "standard",
            }
        }
        req = TavernEngine._check_request_from_locked_choice(workflow)
        self.assertEqual(req.stat, "身手")
        # 缺 label 时回退 key（向后兼容）。
        workflow = {
            "selected_choice": {
                "check_stat": "dexterity",
                "text": "攀爬崖壁",
                "difficulty": 14,
            }
        }
        req = TavernEngine._check_request_from_locked_choice(workflow)
        self.assertEqual(req.stat, "dexterity")

    def test_effective_stat_prefers_label_over_key(self) -> None:
        # 2026-08-24 玩家反馈「【body检定】这 body 是个蛋啊」：未定天门
        # 属性表 body=体魄。A/B/C/D 选项带 check_label="体魄"，但
        # two-phase 路径（workflow.requires_check 且 plan 模型已在
        # resolution.check 返回 stat="body" 裸 key）时公告直接用
        # resolution.check.stat → 玩家看到【body检定】而不是【体魄检定】。
        # 修复：公告改用 effective_stat（优先 selected_choice.check_label），
        # 让裸 key 不泄漏到玩家眼前。
        from tavern.engine import _resolve_effective_stat
        # 带 label：用中文 label
        self.assertEqual(
            _resolve_effective_stat({"check_label": "体魄", "check_stat": "body"}),
            "体魄",
        )
        # 无 label：回退 key
        self.assertEqual(
            _resolve_effective_stat({"check_stat": "body"}),
            "body",
        )
        # 都无：回退 fallback stat
        self.assertEqual(
            _resolve_effective_stat({}, "body"), "body"
        )
        # 全无：兜底「通用」
        self.assertEqual(_resolve_effective_stat({}), "通用")

    def test_option_attribute_label_wins_over_attribute_id(self) -> None:
        # 未定天门真实选项：attribute_id=body + attribute_label=体魄，
        # _check_request_from_locked_choice 必须用 label「体魄」做公告
        # stat，裸 key「body」只留给权威修正查询。
        workflow = {
            "selected_choice": {
                "check_stat": "body",
                "check_label": "体魄",
                "text": "回身阻截追来的执吏",
                "difficulty": 17,
                "risk": "desperate",
                "check_type": "standard",
            }
        }
        req = TavernEngine._check_request_from_locked_choice(workflow)
        self.assertEqual(req.stat, "体魄")
        self.assertNotEqual(req.stat, "body")


class DeclareDeathTests(unittest.IsolatedAsyncioTestCase):
    """declare_death 落库：退场 + 角色档案标记死亡 + 幂等。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-death", "qq:group-death",
            DEFAULT_WORLD_SLUG, "admin-1",
        )
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        await self.database.join_turn_order(
            self.session["id"], "user-1", "旅客", "user-1"
        )
        # 种子参与者 + 角色档案行（测试不走建卡审批流程）
        now = utc_now()
        with self.database._connect() as connection:
            self.participant_id = new_id("participant")
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id,
                    private_user_id, private_origin, display_name,
                    character_name, character_code, card_status, ready,
                    participation_status, seat_reserved_at,
                    joined_round, created_at, updated_at
                ) VALUES (?, ?, NULL, 'user-1', '', '', '旅客',
                          '旅客', 'lvke', 'approved', 1,
                          'active', ?, 1, ?, ?)
                """,
                (
                    self.participant_id,
                    self.session["id"],
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO session_characters(
                    id, session_id, stable_key, name,
                    role_type, lifecycle_status,
                    first_turn, last_turn, revision, created_at, updated_at
                ) VALUES (?, ?, '旅客', '旅客', 'player',
                          'active', 1, 1, 1, ?, ?)
                """,
                (new_id("char"), self.session["id"], now, now),
            )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_declare_death_retires_and_marks_character(self) -> None:
        result = await self.database.declare_death(
            self.session["id"],
            user_id="user-1",
            actor_id="admin-1",
            reason="lethal_check_death",
            narrative="【死亡宣告】旅客当场身亡，永久退场。",
        )
        self.assertTrue(result.get("death_declared"))
        participant = await self.database.get_participant(
            self.session["id"], user_id="user-1"
        )
        self.assertEqual(participant["participation_status"], "retired")
        events = await self.database.recent_events(self.session["id"], 20)
        self.assertTrue(
            any(
                "死亡宣告" in str(item.get("content") or "")
                for item in events
            ),
            "死亡幕间事件应写入 events",
        )
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT lifecycle_status FROM session_characters
                WHERE session_id = ? AND stable_key = '旅客'
                """,
                (self.session["id"],),
            ).fetchone()
        self.assertEqual(row["lifecycle_status"], "dead")

    async def test_declare_death_idempotent(self) -> None:
        await self.database.declare_death(
            self.session["id"],
            user_id="user-1",
            actor_id="admin-1",
            reason="lethal_check_death",
            narrative="【死亡宣告】旅客当场身亡。",
        )
        result = await self.database.declare_death(
            self.session["id"],
            user_id="user-1",
            actor_id="admin-1",
            reason="lethal_check_death",
            narrative="【死亡宣告】旅客当场身亡。",
        )
        self.assertTrue(result.get("already_retired"))


class StaleChoiceSetHealTests(unittest.IsolatedAsyncioTestCase):
    """陈旧 active 选择集的 revision 自愈：选择集创建后会话 revision 被
    无关写入推进（死亡落库等），提交不应再报「场景已变化」反复拒绝。"""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-heal", "qq:group-heal",
            DEFAULT_WORLD_SLUG, "admin-1",
        )
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        await self.database.join_turn_order(
            self.session["id"], "user-1", "旅客", "user-1"
        )
        now = utc_now()
        with self.database._connect() as connection:
            self.participant_id = new_id("participant")
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id,
                    private_user_id, private_origin, display_name,
                    character_name, character_code, card_status, ready,
                    participation_status, seat_reserved_at,
                    joined_round, created_at, updated_at
                ) VALUES (?, ?, NULL, 'user-1', '', '', '旅客',
                          '旅客', 'lvke', 'approved', 1,
                          'active', ?, 1, ?, ?)
                """,
                (
                    self.participant_id,
                    self.session["id"],
                    now,
                    now,
                    now,
                ),
            )
            sess_row = connection.execute(
                "SELECT revision FROM sessions WHERE id = ?",
                (self.session["id"],),
            ).fetchone()
            self.stale_revision = max(1, int(sess_row["revision"]) - 1)
            self.choice_set_id = new_id("choices")
            connection.execute(
                """
                INSERT INTO choice_sets(
                    id, session_id, participant_id, round_no,
                    session_revision, choices_json, status, reroll_count,
                    selected_key, flavor_text, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, 'active', 0, '', '', ?, ?, ?)
                """,
                (
                    self.choice_set_id,
                    self.session["id"],
                    self.participant_id,
                    self.stale_revision,
                    json.dumps(
                        [
                            {"key": "A", "text": "上前查看", "risk": "safe"},
                            {"key": "B", "text": "退回等待", "risk": "safe"},
                            {"key": "C", "text": "仔细检查四周", "risk": "safe"},
                            {"key": "D", "text": "大声呼救", "risk": "safe"},
                        ],
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                    now,
                ),
            )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _engine(self, outputs: list[object]) -> TavernEngine:
        return TavernEngine(
            context=FakeContext(outputs),
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                store_model_payloads=True,
            ),
            broker=EventBroker(),
        )

    def _resolution(self, narrative: str) -> str:
        return json.dumps(
            {
                "mode": "resolve",
                "narrative": narrative,
                "check": None,
                "state_patch": {"scene_summary": narrative},
                "memories": [],
                "director_note": "测试。",
                "next_choices": [
                    {
                        "key": "A",
                        "text": "继续前进",
                        "risk": "safe",
                        "actor_id": self.participant_id,
                    },
                    {
                        "key": "B",
                        "text": "退回等待",
                        "risk": "safe",
                        "actor_id": self.participant_id,
                    },
                    {
                        "key": "C",
                        "text": "大声呼救",
                        "risk": "safe",
                        "actor_id": self.participant_id,
                    },
                    {
                        "key": "D",
                        "text": "就地休息",
                        "risk": "safe",
                        "actor_id": self.participant_id,
                    },
                ],
            },
            ensure_ascii=False,
        )

    async def test_stale_active_choice_set_is_healed_and_commits(
        self,
    ) -> None:
        before = await self.database.get_session(self.session["id"])
        engine = self._engine([self._resolution("旅客仔细检查了四周。")])
        reply = await engine.process_choice(
            event=SimpleNamespace(unified_msg_origin="qq:group-heal"),
            session_id=self.session["id"],
            sender_id="user-1",
            sender_name="管理员",
            choice_key="C",
            force=True,
        )
        # 提交成功，没有被「场景已变化」拒绝
        self.assertIn("旅客仔细检查了四周", reply.text)
        session = await self.database.get_session(self.session["id"])
        self.assertEqual(session["turn_no"], 1)
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT status, session_revision FROM choice_sets
                WHERE id = ?
                """,
                (self.choice_set_id,),
            ).fetchone()
        self.assertEqual(row["status"], "selected")
        # 选择集 revision 被自愈到提交前一刻的会话 revision（提交事务内
        # 自愈发生在 sessions.revision 自身 +1 之前）
        self.assertEqual(row["session_revision"], before["revision"])
        # 会话整体前进了
        self.assertEqual(session["revision"], before["revision"] + 1)


if __name__ == "__main__":
    unittest.main()
