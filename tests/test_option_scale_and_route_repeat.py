"""「做出来」不等于「走一小段路」、地点不重复建立（2026-09-20）。

玩家反馈：「白鲸那段再前面一点，还是在说一些很琐碎的事情」，并指出
「很多复现的短语都是 50 轮以内出现的」——否掉了按时间清账本的思路。

实测那一局：
- 最近 40 个玩家行动：打听/核实 15、移动/赶路 8、文书/程序 8，**推进/决定只有 2**；
- 最近 30 条正文：深绿木门 13 条、正屋门口 12 条、青石窄街 11 条（同一路线反复重述）；
- 第 55 轮给出的四个选项全是微步骤：
    「沿村道走向村口井台，进老驿卒石屋送预警信」
    「在村口井台旁坐下，等老驿卒晌午来打水时当面交信」
    「先到村口最近一户人家敲门，问老驿卒石屋在哪」
    「沿村道先走到村口，观察井台与石屋方位再决定」

原先的选项规则只要求「至少两个选项是现在就能做出来的动作」——「走进石屋
送信」字面上满足它，纯流程（备案/登记/递交）也满足它，所以这是个自己留的
口子。这里把它收紧到「完成一个结果环节」，并把路径复述写进反注水清单。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tavern.prompts import _ending_length_rule

ROOT = Path(__file__).resolve().parent.parent


class RouteRepetitionTests(unittest.TestCase):
    def test_length_rule_bans_renarrating_known_places(self) -> None:
        rule = _ending_length_rule(False, "standard")
        for phrase in (
            "已经写过的地点再次出现时",
            "不要重走一遍路线或重新建立场景",
            "路径复述",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rule)

    def test_plodding_list_still_covers_old_cases(self) -> None:
        """加规则不能把原有的反注水清单挤掉。"""
        rule = _ending_length_rule(False, "standard")
        for phrase in ("出发/准备的过程流水", "只为凑气氛的路人闲笔", "可行动的结论"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rule)


class ChoiceScaleTests(unittest.TestCase):
    """选项层的规则在 _choice_pacing_directive 里（依赖数据库），做静态断言。"""

    def setUp(self) -> None:
        self.source = (ROOT / "tavern" / "engine.py").read_text(encoding="utf-8")

    def test_result_step_is_required_instead_of_micro_movement(self) -> None:
        for phrase in (
            "「做出来」指**完成一个结果环节**，不是移动一小段路",
            "先观察方位再决定",
            "除非有真实阻力",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.source)

    def test_paperwork_is_not_counted_as_progress_by_default(self) -> None:
        self.assertIn("纯流程动作（备案、登记、递交、报名单、留个记录、整理成材料）", self.source)

    def test_original_requirements_are_preserved(self) -> None:
        for phrase in (
            "现在就能把它做出来",
            "禁止生成正文已经给出答案的打听类选项",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.source)


if __name__ == "__main__":
    unittest.main()
