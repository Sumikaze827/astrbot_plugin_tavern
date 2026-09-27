"""Regression tests for exact-ID milestone handling without text matching."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from tavern.engine import (
    TavernEngine,
    _parse_milestone_judge,
    _select_milestone_judge_events,
)
from tavern.events import EventBroker
from tavern.lifecycle import normalize_progress
from tavern.resolution import validate_resolution


def _world() -> dict:
    """Return a minimal world with one authoritative milestone ID."""
    return {
        "rules": {
            "progress": {
                "chapters": [
                    {
                        "id": "ch_01",
                        "milestones": [
                            {"id": "m_01_01_gate", "label": "开启城门"}
                        ],
                    }
                ]
            }
        }
    }


class ExactMilestoneTests(unittest.TestCase):
    """Milestone completion uses declared IDs, never narrative similarity."""

    def setUp(self) -> None:
        self.engine = TavernEngine(
            context=SimpleNamespace(),
            database=SimpleNamespace(),
            config_provider=lambda: SimpleNamespace(),
            broker=EventBroker(),
        )

    def test_resolution_preserves_stable_key(self) -> None:
        """The structured response parser retains the milestone identity."""
        resolution = validate_resolution(
            {
                "mode": "resolve",
                "narrative": "城门已经开启。",
                "ledger_ops": [
                    {
                        "op": "create",
                        "kind": "milestone",
                        "stable_key": "m_01_01_gate",
                        "title": "开启城门",
                    }
                ],
            }
        )
        self.assertEqual(resolution.ledger_ops[0]["stable_key"], "m_01_01_gate")

    def test_narrator_cannot_complete_milestone_directly(self) -> None:
        """Even an exact current ID is only a proposal and is not committed."""
        operations = self.engine._validated_ledger_ops(
            _world(),
            {"current_chapter_id": "ch_01"},
            [
                {
                    "op": "create",
                    "kind": "milestone",
                    "stable_key": "m_01_01_gate",
                    "title": "任意标题",
                },
                {
                    "op": "create",
                    "kind": "milestone",
                    "stable_key": "m_01_99_fake",
                    "title": "开启城门",
                },
                {
                    "op": "create",
                    "kind": "milestone",
                    "title": "开启城门",
                },
            ],
        )
        self.assertEqual(operations, [])

    def test_judge_true_requires_cited_criteria(self) -> None:
        old_style = _parse_milestone_judge(
            {"milestones": [{"id": "m_01_01_gate", "achieved": True}]}
        )
        self.assertFalse(old_style["m_01_01_gate"]["achieved"])
        cited = _parse_milestone_judge(
            {
                "milestones": [
                    {
                        "id": "m_01_01_gate",
                        "achieved": True,
                        "criteria": [
                            {
                                "name": "开启城门",
                                "passed": True,
                                "event_id": "event_1",
                                "quote": "众人合力推开了沉重的城门",
                            }
                        ],
                    }
                ]
            }
        )
        self.assertTrue(cited["m_01_01_gate"]["achieved"])

    def test_engine_rejects_nonverbatim_or_speculative_evidence(self) -> None:
        event = {
            "id": "event_1",
            "turn_no": 4,
            "role": "narrator",
            "content": "众人合力推开了沉重的城门，晨光照进城道。",
        }
        valid = self.engine._verified_judge_entry(
            {
                "achieved": True,
                "criteria": [
                    {
                        "passed": True,
                        "event_id": "event_1",
                        "quote": "众人合力推开了沉重的城门",
                    }
                ],
            },
            events_by_id={"event_1": event},
            entered_turn=1,
        )
        self.assertIsNotNone(valid)
        self.assertIsNone(
            self.engine._verified_judge_entry(
                {
                    "achieved": True,
                    "criteria": [
                        {
                            "passed": True,
                            "event_id": "event_1",
                            "quote": "众人似乎快要推开城门",
                        }
                    ],
                },
                events_by_id={"event_1": event},
                entered_turn=1,
            )
        )

    def test_quote_check_tolerates_punctuation_but_not_rewriting(self) -> None:
        """2026-09-20：标点差异不再判废，改写/编造仍然拒绝。

        线上 m_05_01 在裁判判定后仍未落账，而正文里明明有可引用的原句；
        原实现要求连标点都一样，模型把「，」写成「,」或漏一个引号就会静默
        核验失败，章节继续卡住。
        """
        event = {
            "id": "event_1",
            "turn_no": 12,
            "role": "narrator",
            "content": "领头骑士把通行牌还回去，退开半步：“名册上有你，侍从栏。自称骑士这一句，记档。”",
        }
        matches = self.engine._evidence_quote_matches
        # 原样、半角标点、去标点、引号与空格差异都算同一句原文
        self.assertTrue(matches(event, "名册上有你，侍从栏。自称骑士这一句，记档。"))
        self.assertTrue(matches(event, "名册上有你, 侍从栏. 自称骑士这一句, 记档."))
        self.assertTrue(matches(event, "名册上有你侍从栏自称骑士这一句记档"))
        self.assertTrue(matches(event, "名册上有你，侍从栏"))
        # 编造、太短、含推测词、非旁白事件依旧拒绝
        self.assertFalse(matches(event, "领头骑士当场承认他是正式骑士"))
        self.assertFalse(matches(event, "记档"))
        self.assertFalse(matches(event, "名册上可能有你"))
        self.assertFalse(
            matches(
                {"role": "player", "turn_no": 12, "content": event["content"]},
                "名册上有你，侍从栏",
            )
        )

    def test_closure_audit_survives_progress_normalization(self) -> None:
        closure = {
            "chapter_id": "ch_01",
            "event_id": "event_9",
            "quote": "众人离开已经平息的城门",
            "reason": "冲突结束且队伍离场",
            "terminal": False,
        }
        progress = normalize_progress({"last_chapter_closure": closure})
        self.assertEqual(progress["last_chapter_closure"], closure)

    def test_prefix_or_similar_text_does_not_complete(self) -> None:
        """Prefix-like legacy keys and narrative text do not count."""
        metadata = {"m_01_01_gate": {"label": "开启城门"}}
        self.assertFalse(
            self.engine._milestone_ledger_completed(
                "m_01_01_gate", {"m_01_01 other"}, metadata
            )
        )
        self.assertFalse(
            self.engine._milestone_ledger_completed(
                "m_01_01_gate", {"城门缓缓开启"}, metadata
            )
        )
        self.assertTrue(
            self.engine._milestone_ledger_completed(
                "m_01_01_gate", {"m_01_01_gate"}, metadata
            )
        )

    def test_long_chapter_retrieves_old_milestone_evidence(self) -> None:
        """早期关键事件不能因最近 40 条窗口而永久失去判定资格。"""
        events = [
            {
                "id": f"event_{turn}",
                "seq": turn,
                "turn_no": turn,
                "role": "narrator",
                "content": (
                    "罗槿主动切断通讯并下令清除全部目标，"
                    "回声乔也被写进删除名单。"
                    if turn == 10
                    else f"队伍继续穿过第 {turn} 段管廊。"
                ),
            }
            for turn in range(1, 101)
        ]
        selected = _select_milestone_judge_events(
            events,
            milestone_meta={
                "m_betrayal": {
                    "words": ["罗槿", "灭口", "删除名单"],
                }
            },
            pending_ids=["m_betrayal"],
        )
        selected_ids = {event["id"] for event in selected}
        self.assertIn("event_10", selected_ids)
        self.assertIn("event_100", selected_ids)
        self.assertNotIn("event_20", selected_ids)

    def test_current_chapter_events_always_reach_the_judge(self) -> None:
        """2026-09-20：本章旁白必须全量进入裁判窗口。

        ch_05 的实例：本章早期那一拍（当众亮明身份与立场）被「最近 40 条」
        挤出窗口，裁判看不到就永远判否，章节卡死。这里锁住「chapter_start_turn
        之后的每一条都必须在窗口里」，无论它多早。
        """
        events = [
            {
                "id": f"event_{turn}",
                "seq": turn,
                "turn_no": turn,
                "role": "narrator",
                "content": f"第 {turn} 回合的旁白。",
            }
            for turn in range(1, 121)
        ]
        # 里程碑关键词一个都没出现，逼着实现只能靠「本章全量」兜住。
        selected = _select_milestone_judge_events(
            events,
            milestone_meta={"m_open": {"words": ["不存在词"]}},
            pending_ids=["m_open"],
            recent_limit=40,
            chapter_start_turn=60,
        )
        selected_turns = {int(event["turn_no"]) for event in selected}
        self.assertEqual(
            sorted(t for t in selected_turns if t >= 60),
            list(range(60, 121)),
        )
        # 本章之前的上下文只保留最近 40 条里属于它的部分，不会无限放大。
        self.assertTrue(all(t >= 21 for t in selected_turns))

    def test_event_budget_drops_oldest_but_keeps_recent(self) -> None:
        events = [
            {
                "id": f"event_{turn}",
                "seq": turn,
                "turn_no": turn,
                "role": "narrator",
                "content": "长" * 400,
            }
            for turn in range(1, 201)
        ]
        selected = _select_milestone_judge_events(
            events,
            milestone_meta={"m_x": {"words": []}},
            pending_ids=["m_x"],
            recent_limit=10,
            chapter_start_turn=1,
            char_budget=20000,
        )
        self.assertLess(len(selected), 201)
        self.assertEqual(
            [int(event["turn_no"]) for event in selected][-10:],
            list(range(191, 201)),
        )

    def test_verified_entry_accepts_evidence_across_multiple_turns(self) -> None:
        events = {
            "event_old": {
                "id": "event_old",
                "turn_no": 10,
                "role": "narrator",
                "content": "罗槿下令清除全部目标。",
            },
            "event_new": {
                "id": "event_new",
                "turn_no": 90,
                "role": "narrator",
                "content": "四人穿过维修井，成功突破数据站。",
            },
        }
        verified = self.engine._verified_judge_entry(
            {
                "achieved": True,
                "criteria": [
                    {
                        "passed": True,
                        "event_id": "event_old",
                        "quote": "罗槿下令清除全部目标",
                    },
                    {
                        "passed": True,
                        "event_id": "event_new",
                        "quote": "四人穿过维修井，成功突破数据站",
                    },
                ],
            },
            events_by_id=events,
            entered_turn=1,
        )
        self.assertIsNotNone(verified)
        self.assertEqual(
            verified["evidence_event_ids"],
            ["event_old", "event_new"],
        )

    def test_authoritative_group_vote_is_milestone_evidence(self) -> None:
        """正式集体表决是终章抉择证据，不能因 role=system 被过滤。"""
        events = [
            {
                "id": f"event_{turn}",
                "seq": turn,
                "turn_no": turn,
                "role": "narrator",
                "actor_name": "酒馆叙事者",
                "content": f"队伍继续穿过第 {turn} 段管廊。",
            }
            for turn in range(1, 101)
        ]
        events.insert(49, {
            "id": "event_vote",
            "seq": 50,
            "turn_no": 50,
            "role": "system",
            "actor_name": "集体表决",
            "content": "【集体决定】接受罗槿交易，换身份洗白和报酬",
        })
        selected = _select_milestone_judge_events(
            events,
            milestone_meta={
                "m_final_vote": {"words": ["接受罗槿交易", "表决"]}
            },
            pending_ids=["m_final_vote"],
        )
        self.assertIn(
            "event_vote",
            {event["id"] for event in selected},
        )


if __name__ == "__main__":
    unittest.main()
