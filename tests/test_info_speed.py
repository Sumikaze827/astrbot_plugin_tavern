"""信息获取提速（2026-09-20）的 DB-free 回归测试。

背景：玩家反馈「信息获取太慢」。诊断结论有两层：

1. 叙事层挤牙膏——一次打听只交付一层线索，来源明明知道全部答案却留到
   下一轮；一个来源不知道时也不给出下一跳，玩家只能逐个登门试。
2. 里程碑层错位——很多里程碑判定的是「某件事已经发生」（预警已发出、
   同盟已达成），叙事却一直把它当成「再多给一条线索」，于是玩家一轮轮
   打听、里程碑长期不动（真实会话里 24 回合只达成 1/3 条）。

本测试只覆盖纯函数逻辑（不依赖数据库，避免临时目录沙箱问题）：
- milestone_requirement_kind：把达成条件粗分为 info / action。
- _information_delivery_rules：情报交付硬规则存在且含关键约束。
- _pacing_requires_action / _all_options_investigate：只吃情报的选项组
  在「需行动」章节里会被识别出来。
"""

from __future__ import annotations

import unittest

from tavern.engine import TavernEngine
from tavern.prompts import (
    _information_delivery_rules,
    milestone_requirement_kind,
)

# 真实会话（re0 王选篇 ch_06「同盟的筹码」）的三条里程碑 label。
RE0_CH06_01 = (
    "有来源的观察或联络已使玩家获知梅瑟斯领的魔女教风险及归途白鲸威胁，"
    "能够判断救援对象与大致行动方向；仅听到空泛不祥预感不算，不能要求"
    "先出现村民死亡。"
)
RE0_CH06_02 = (
    "保护领地居民与艾米莉娅的预警或救援措施已实际发出/执行，并明确接应"
    "对象与路线；可经可靠联络者、派出队员或直接到场落实，不要求所有人"
    "往返或先惨败。"
)
RE0_CH06_03 = (
    "对白鲸威胁的处置及后续驰援已有可执行安排：可用战力、运输/撤离路线和"
    "各方承诺明确，已开始行动。允许等价的真实合作结果，不要求指定谈判"
    "台词、全员起誓或所有阵营到齐。"
)


def _option(text: str) -> dict:
    return {"text": text}


class MilestoneRequirementKindTests(unittest.TestCase):
    """达成条件分类：只影响指令措辞，不参与任何达成判定。"""

    def test_known_info_milestone_is_info(self) -> None:
        self.assertEqual(milestone_requirement_kind(RE0_CH06_01), "info")

    def test_action_milestones_are_action(self) -> None:
        self.assertEqual(milestone_requirement_kind(RE0_CH06_02), "action")
        self.assertEqual(milestone_requirement_kind(RE0_CH06_03), "action")

    def test_unclassifiable_label_returns_empty(self) -> None:
        self.assertEqual(milestone_requirement_kind("尘埃落定"), "")
        self.assertEqual(milestone_requirement_kind(""), "")


class InformationDeliveryRulesTests(unittest.TestCase):
    """情报交付硬规则必须写好：一次给全、给结论、给下一跳、禁多跳链。"""

    def test_rules_are_present_and_concrete(self) -> None:
        rules = _information_delivery_rules()
        for phrase in (
            "一次打听给全",
            "给结论，不给猜谜",
            "来源不知道就当场给下一跳",
            "禁止多跳情报链",
            "已知的不重复",
            "要价不跨轮",
        ):
            self.assertIn(phrase, rules)

    def test_rules_do_not_weaken_milestone_authority(self) -> None:
        """提速规则不得让叙事模型获得自行标记里程碑完成的权限。"""
        rules = _information_delivery_rules()
        self.assertNotIn("ledger_ops", rules)
        self.assertNotIn("stable_key", rules)


class InvestigationOnlyOptionsTests(unittest.TestCase):
    """四选项全是打听时，「需行动」章节应当识别出来强制换选项。"""

    def test_requires_action_reads_directive_tag(self) -> None:
        self.assertTrue(
            TavernEngine._pacing_requires_action(
                "[Choice-Pacing-HARD] 未达成的里程碑："
                "m_06_02_warning_sent（…）〔需行动〕。"
            )
        )
        self.assertFalse(
            TavernEngine._pacing_requires_action(
                "[Choice-Pacing-HARD] 未达成的里程碑："
                "m_06_01_threat_known（…）〔需信息〕。"
            )
        )

    def test_all_investigate_detects_full_probe_set(self) -> None:
        choices = [
            _option("去界桩驿找老驿卒打听北边车队的事"),
            _option("去商业街水果店询问卡德蒙最近的异常"),
            _option("核实登记簿上那三拨车队的记录"),
            _option("去士兵值班处查证往来车队登记"),
        ]
        self.assertTrue(TavernEngine._all_options_investigate(choices))

    def test_all_investigate_false_when_one_option_acts(self) -> None:
        choices = [
            _option("去界桩驿找老驿卒打听北边车队的事"),
            _option("去商业街水果店询问卡德蒙最近的异常"),
            _option("核实登记簿上那三拨车队的记录"),
            _option("直接派人回领地发出预警"),
        ]
        self.assertFalse(TavernEngine._all_options_investigate(choices))

    def test_all_investigate_needs_four_options(self) -> None:
        self.assertFalse(
            TavernEngine._all_options_investigate(
                [_option("去打听最近有什么不对劲")]
            )
        )


if __name__ == "__main__":
    unittest.main()
