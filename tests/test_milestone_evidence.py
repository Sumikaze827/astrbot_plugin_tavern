"""里程碑证据核验：省略号压缩的引用必须能通过，伪造的仍然要被拒。

2026-09-21 线上故障：`ch_07` 的 `m_07_02_battle_result` / `m_07_03_relief_departed`
被里程碑裁判连续判 `achieved=true`（8 次 / 4 次，最后四轮两项全 true），但账本
0 次落账、章节永远不切。根因是裁判抄证据时习惯把长句压成「前半……后半」，而
`_evidence_quote_matches` 把整段当成一个连续子串比对——用了省略号就命中不了；
核验又要求**每条** criteria 都通过，于是三条里只要有一条带省略号，整个里程碑
被静默作废（驳回只写 INFO 日志，审计里记的是核验前的原始判定，看起来一切正常）。

这里锁定修复后的契约：按省略号切段，每段都要逐字出现在原文里。
"""

from __future__ import annotations

import unittest

from tavern.engine import TavernEngine, _parse_milestone_judge

EVENT = {
    "id": "event_x",
    "role": "narrator",
    "turn_no": 326,
    # 两段真实证据之间有别的文字：省略号引用的正是这种压缩写法
    "content": (
        "五条悟按住右眼。白鲸本体庞大的身躯从半空砸下来，"
        "把整片泥地都震得跳了一下，然后就彻底不动了。雾也跟着散了。"
    ),
}


def engine() -> TavernEngine:
    return TavernEngine.__new__(TavernEngine)


class EllipsisQuoteTests(unittest.TestCase):
    def test_compressed_quote_passes_when_every_segment_is_real(self) -> None:
        self.assertTrue(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯从半空砸下来……然后就彻底不动了"
            )
        )

    def test_three_segment_quote_passes(self) -> None:
        self.assertTrue(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯……震得跳了一下……彻底不动了"
            )
        )

    def test_fabricated_tail_is_still_rejected(self) -> None:
        self.assertFalse(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯从半空砸下来……它当场化为灰烬了"
            )
        )

    def test_fabricated_middle_is_still_rejected(self) -> None:
        self.assertFalse(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯……被黑雾重新托了起来……彻底不动了"
            )
        )

    def test_ascii_ellipsis_is_treated_the_same(self) -> None:
        self.assertTrue(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯从半空砸下来......然后就彻底不动了"
            )
        )


class PlainQuoteTests(unittest.TestCase):
    def test_contiguous_quote_passes(self) -> None:
        self.assertTrue(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯从半空砸下来"
            )
        )

    def test_punctuation_differences_are_tolerated(self) -> None:
        self.assertTrue(
            engine()._evidence_quote_matches(
                EVENT, "白鲸本体庞大的身躯从半空砸下来,把整片泥地都震得跳了一下"
            )
        )

    def test_short_quote_is_rejected(self) -> None:
        self.assertFalse(engine()._evidence_quote_matches(EVENT, "彻底不动了"))

    def test_absent_text_is_rejected(self) -> None:
        self.assertFalse(
            engine()._evidence_quote_matches(EVENT, "白鲸当场被斩成两半掉了下来")
        )

    def test_non_narrator_event_is_rejected(self) -> None:
        self.assertFalse(
            engine()._evidence_quote_matches(
                {**EVENT, "role": "player"},
                "白鲸本体庞大的身躯从半空砸下来",
            )
        )

    def test_speculative_quote_is_rejected(self) -> None:
        """引用本身是推测/计划时不算事实证据（保持原有防线）。"""
        event = {
            **EVENT,
            "content": "他可能觉得白鲸本体已经彻底不动了，准备再去补一刀。",
        }
        self.assertFalse(
            engine()._evidence_quote_matches(event, "他可能觉得白鲸本体已经彻底不动了")
        )


class VerifiedEntryTests(unittest.TestCase):
    def verdict(self, quote: str) -> dict:
        return {
            "achieved": True,
            "criteria": [
                {"name": "本体被击杀", "passed": True, "event_id": "event_x", "quote": quote}
            ],
            "reason": "正文写了",
        }

    def test_rejection_reason_is_collected_for_auditing(self) -> None:
        """驳回理由必须能被调用方取到并写进审计。

        之前驳回只写 INFO 日志，审计记的是核验**前**的判定，于是「裁判说达成、
        引擎判废」在库里看起来完全正常，故障藏了一整天。
        """
        rejections: dict[str, str] = {}
        result = engine()._verified_judge_entry(
            self.verdict("白鲸本体庞大的身躯从半空砸下来……它当场化为灰烬了"),
            events_by_id={"event_x": EVENT},
            entered_turn=242,
            milestone_id="m_x",
            rejections=rejections,
        )
        self.assertIsNone(result)
        self.assertIn("m_x", rejections)
        self.assertIn("原句核验失败", rejections["m_x"])

    def test_verified_entry_is_returned_with_evidence(self) -> None:
        result = engine()._verified_judge_entry(
            self.verdict("白鲸本体庞大的身躯从半空砸下来……然后就彻底不动了"),
            events_by_id={"event_x": EVENT},
            entered_turn=242,
            milestone_id="m_x",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["source_event_id"], "event_x")
        self.assertEqual(result["evidence_event_ids"], ["event_x"])

    def test_events_at_or_before_the_floor_are_refused(self) -> None:
        rejections: dict[str, str] = {}
        result = engine()._verified_judge_entry(
            self.verdict("白鲸本体庞大的身躯从半空砸下来"),
            events_by_id={"event_x": EVENT},
            entered_turn=326,  # 与事件同回合 → 早于证据起点
            milestone_id="m_x",
            rejections=rejections,
        )
        self.assertIsNone(result)
        self.assertIn("早于证据起点", rejections["m_x"])

    def test_event_outside_the_window_is_refused(self) -> None:
        rejections: dict[str, str] = {}
        result = engine()._verified_judge_entry(
            self.verdict("白鲸本体庞大的身躯从半空砸下来"),
            events_by_id={},
            entered_turn=242,
            milestone_id="m_x",
            rejections=rejections,
        )
        self.assertIsNone(result)
        self.assertIn("不在证据窗口", rejections["m_x"])


class ParseRequiresEvidenceTests(unittest.TestCase):
    def test_achieved_without_criteria_is_downgraded(self) -> None:
        parsed = _parse_milestone_judge(
            {"milestones": [{"id": "m_x", "achieved": True, "reason": "我觉得成了"}]}
        )
        assert parsed is not None
        self.assertFalse(parsed["m_x"]["achieved"])

    def test_achieved_with_incomplete_criterion_is_downgraded(self) -> None:
        parsed = _parse_milestone_judge(
            {
                "milestones": [
                    {
                        "id": "m_x",
                        "achieved": True,
                        "criteria": [{"passed": True, "event_id": "", "quote": ""}],
                    }
                ]
            }
        )
        assert parsed is not None
        self.assertFalse(parsed["m_x"]["achieved"])

    def test_complete_criterion_keeps_achieved(self) -> None:
        parsed = _parse_milestone_judge(
            {
                "milestones": [
                    {
                        "id": "m_x",
                        "achieved": True,
                        "criteria": [
                            {
                                "passed": True,
                                "event_id": "event_x",
                                "quote": "白鲸本体庞大的身躯从半空砸下来",
                            }
                        ],
                    }
                ]
            }
        )
        assert parsed is not None
        self.assertTrue(parsed["m_x"]["achieved"])


if __name__ == "__main__":
    unittest.main()
