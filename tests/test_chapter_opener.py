"""章节题材补写（Chapter-Opener）的调用路径回归。

2026-09-20：该调用块里把 `config.enforce_mobile_output` 写成
`config.enforce_mobile_limits`，AttributeError 被最外层兜底吞成
「叙事引擎出现内部错误」，两轮作废。这里用假 database 真跑一遍那条调用，
既不依赖 SQLite，又能覆盖到每个关键字参数。

同时锁住该功能的三条边界：不超期不触发、正文已带上题材就不补写、
补写仍不合格则保留原稿。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tavern.config import TavernConfig
from tavern.engine import TavernEngine
from tavern.events import EventBroker
from tavern.resolution import Resolution


def _resolution(narrative: str) -> Resolution:
    return Resolution(
        mode="resolve",
        narrative=narrative,
        check=None,
        state_patch={},
        memories=(),
        next_choices=(),
        group_decision=None,
        return_progress=None,
        npc_ops=(),
        clock_ops=(),
        ledger_ops=(),
        location_ops=(),
        status_ops=(),
        assist_ops=(),
        director_note="",
        raw={},
    )


def _world() -> dict:
    return {
        "rules": {
            "progress": {
                "chapters": [
                    {
                        "id": "ch_06",
                        "max_turns": 14,
                        "current_objective": "获知领地面临魔女教威胁与归途白鲸风险",
                        "milestones": [
                            {
                                "id": "m_06_01",
                                "label": "获知威胁",
                                "evidence_required": [
                                    {"type": "clue_keyword_any",
                                     "match": ["魔女教", "白鲸"]}
                                ],
                            }
                        ],
                        "exits_when": {"all_milestones": ["m_06_01"]},
                    }
                ]
            }
        }
    }


class _FakeDatabase:
    def __init__(self, turn_no: int, seen: set[str] | None = None) -> None:
        self.turn_no = turn_no
        self.seen = seen or set()

    async def get_session(self, session_id: str):
        return {"id": session_id, "turn_no": self.turn_no}

    async def get_session_rule_state(self, session_id: str):
        return {"progress": {"current_chapter_id": "ch_06",
                             "chapter_entered_at_turn": 1}}

    async def list_story_ledger(self, session_id: str):
        return []

    async def seen_terms(self, terms):
        return {t for t in terms if t in self.seen}


class ChapterOpenerTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, turn_no: int, seen=None) -> TavernEngine:
        engine = TavernEngine(
            context=SimpleNamespace(),
            database=_FakeDatabase(turn_no, seen),
            config_provider=lambda: TavernConfig(),
            broker=EventBroker(),
        )
        engine._generate_resolution = AsyncMock(
            return_value=(_resolution("驿使带来领地急信：魔女教正在逼近。"),
                          "model")
        )
        return engine

    async def _call(self, engine, narrative: str):
        return await engine._ensure_chapter_opener(
            resolution=_resolution(narrative),
            session_id="s",
            world=_world(),
            session={"progress": {"current_chapter_id": "ch_06",
                                  "chapter_entered_at_turn": 1}},
            provider_ids=["model"],
            config=TavernConfig(),
            system="sys",
            prompt="prompt",
            expected_actor={},
            movement_users=None,
            roster=(),
            party_follow=True,
            enforce_mobile_limits=False,
            npc_direction=None,
        )

    async def test_call_block_uses_real_config_attributes(self):
        """超期 + 题材未登场 → 真的去补写，且整条调用不抛异常。"""
        engine = self._engine(turn_no=20)
        result = await self._call(engine, "众人仍在院里商量盘缠。")
        engine._generate_resolution.assert_awaited_once()
        self.assertIn("魔女教", result.narrative)

    async def test_below_threshold_never_calls_model(self):
        engine = self._engine(turn_no=3)
        result = await self._call(engine, "众人仍在院里商量盘缠。")
        engine._generate_resolution.assert_not_awaited()
        self.assertIn("商量盘缠", result.narrative)

    async def test_narrative_already_carries_subject_skips_retry(self):
        engine = self._engine(turn_no=20)
        result = await self._call(engine, "有人提起魔女教与白鲸。")
        engine._generate_resolution.assert_not_awaited()
        self.assertIn("魔女教", result.narrative)

    async def test_failed_retry_keeps_original(self):
        engine = self._engine(turn_no=20)
        engine._generate_resolution = AsyncMock(
            return_value=(_resolution("还是没提。"), "model")
        )
        result = await self._call(engine, "原稿。")
        self.assertIn("原稿", result.narrative)

    async def test_internal_failure_never_breaks_the_turn(self):
        """补写内部炸了也必须返回原稿（曾被 AttributeError 废掉两轮）。"""
        engine = self._engine(turn_no=20)
        engine._generate_resolution = AsyncMock(
            side_effect=AttributeError("boom")
        )
        result = await self._call(engine, "原稿仍在。")
        self.assertIn("原稿仍在", result.narrative)

    async def test_retry_is_throttled_to_once_every_few_turns(self):
        """同一副本 3 回合内最多补写一次，避免每回合白烧一次调用。"""
        engine = self._engine(turn_no=20)
        first = await self._call(engine, "众人仍在院里商量盘缠。")
        self.assertIn("魔女教", first.narrative)
        engine._generate_resolution.reset_mock()
        # 同一回合（滞留数不变）再走一次：节流命中，不再调用模型。
        again = await self._call(engine, "众人仍在院里商量盘缠。")
        engine._generate_resolution.assert_not_awaited()
        self.assertIn("商量盘缠", again.narrative)


if __name__ == "__main__":
    unittest.main()
