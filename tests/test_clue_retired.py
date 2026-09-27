"""线索（ledger kind='clue'）停止生成（2026-09-20）。

它是**已删除功能的残留**：

1. 原本引擎用**关键词子串匹配线索标题**里的 `evidence_required` 词来判断里程碑
   是否达成（见 `milestone_judge_prompt` 的说明与 prompts.py:1229 原本那句
   「clue 条目标题必须包含其 evidence_required 中的关键词」）。
2. 2026-08-23 用户反馈「不要靠正则，没有用的」——那条匹配路径被删除，改用模型
   裁判 + 已提交正文举证。
3. 但生成侧一直没跟着删：schema 仍列出 `clue`，写入路径仍接受。于是模型持续
   按一份作废的格式写记录。某局 270+ 轮攒了 126 条，只有 2 条被结清；叙事循环
   里没有任何逻辑读它们，反而把 `context_budget.ledger_items` 的 8 格占满，
   把 19 条 completed 里程碑全部挤出上下文。

**唯一的读取方**是前作继承（`worldgen/continuity.py` 的 `carried_facts`）。历史
行保留，续作照样继承；只是新的一局不再积攒。

本测试锁两件事：白名单不再包含 clue，且 clue 必须被**整条丢弃**——只从白名单里
拿掉会让它落进 `kind = "objective"` 兜底，换个名字继续写进去。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ClueRejectedEverywhereTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolution = (ROOT / "tavern" / "resolution.py").read_text(encoding="utf-8")
        self.worlds = (
            ROOT / "tavern" / "repositories" / "worlds.py"
        ).read_text(encoding="utf-8")

    def test_resolution_drops_clue_before_kind_fallback(self) -> None:
        marker = '== "clue"'
        self.assertIn(marker, self.resolution)
        window = self.resolution[self.resolution.index(marker) :][:400]
        self.assertIn("continue", window)
        # 白名单里不能再留 clue，否则等于没删
        self.assertNotIn('"clue",', self.resolution)

    def test_worlds_drops_clue_before_kind_fallback(self) -> None:
        marker = '== "clue"'
        self.assertIn(marker, self.worlds)
        window = self.worlds[self.worlds.index(marker) :][:400]
        self.assertIn("continue", window)
        self.assertNotIn('"clue",', self.worlds)

    def test_fallback_still_coerces_unknown_kinds(self) -> None:
        """丢弃 clue 不能顺手改掉「未知 kind 兜底成 objective」的既有行为。"""
        for source in (self.resolution, self.worlds):
            self.assertIn('= "objective"', source)


class PromptNoLongerAsksForCluesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompts = (ROOT / "tavern" / "prompts.py").read_text(encoding="utf-8")

    def test_schema_enum_drops_clue(self) -> None:
        self.assertIn('"kind": "main | side | objective | milestone"', self.prompts)
        self.assertNotIn("objective | clue | milestone", self.prompts)

    def test_evidence_keyword_instruction_is_gone(self) -> None:
        """那句是旧关键词匹配器的输入格式，随匹配器一起删。"""
        self.assertNotIn("clue 条目标题必须包含其 evidence_required", self.prompts)

    def test_milestone_submission_instruction_survives(self) -> None:
        for phrase in (
            "milestone 条目的 stable_key 必须逐字使用该里程碑的 id",
            "标题必须逐字使用该里程碑的 label",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.prompts)


class ContinuityStillReadsHistoricalRowsTests(unittest.TestCase):
    """续作继承的代码不能一起删——历史行仍要被带进续作。"""

    def test_continuity_still_builds_carried_facts(self) -> None:
        source = (
            ROOT / "tavern" / "worldgen" / "continuity.py"
        ).read_text(encoding="utf-8")
        self.assertIn("carried_facts", source)
        self.assertIn('!= "milestone"', source)


if __name__ == "__main__":
    unittest.main()
