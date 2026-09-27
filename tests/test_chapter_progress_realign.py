"""Chapter progress alignment and exact milestone-ID regression tests.

覆盖：
1. _realign_chapter_progress：rule_state 里的 chapter / current_objective
   会被强制同步为世界配置中当前章节声明的权威值；陈旧字面值会被刷新。
2. Only completed milestone rows with authoritative IDs advance chapters.
3. Clue text and active milestone rows never count as completion evidence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from types import SimpleNamespace

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now
from tavern.engine import TavernEngine
from tavern.events import EventBroker


def _custom_world() -> dict:
    """Return a two-chapter world for progress alignment tests."""
    return {
        "name": "测试世界 · 关键词命中",
        "slug": DEFAULT_WORLD_SLUG,
        "system_prompt": "测试世界系统提示。",
        "rules": {
            "progress": {
                "total_milestones": 4,
                "chapters": [
                    {
                        "id": "ch_01_trace",
                        "title": "第一章：城中村的盲区",
                        "current_objective": (
                            "抵达周记复印店取得加密U盘与线索，"
                            "识破被盯梢，得知「沉默者名单」"
                        ),
                        "max_turns": 6,
                        "milestones": [
                            {
                                "id": "m_01_01_get_usb",
                                "label": "从老周处取得加密U盘",
                                "evidence_required": [
                                    {"type": "clue_keyword_any", "match": ["U盘", "加密"]}
                                ],
                            },
                            {
                                "id": "m_01_02_silent_list_revealed",
                                "label": "得知「沉默者名单」的存在与用途",
                                "evidence_required": [
                                    {"type": "clue_keyword_any", "match": ["沉默者名单"]}
                                ],
                            },
                            {
                                "id": "m_01_03_tail_shaken",
                                "label": "识破并甩掉盯梢",
                                "evidence_required": [
                                    {"type": "clue_keyword_any", "match": ["盯梢", "跟踪"]}
                                ],
                            },
                        ],
                        "exits_when": {
                            "all_milestones": [
                                "m_01_01_get_usb",
                                "m_01_02_silent_list_revealed",
                                "m_01_03_tail_shaken",
                            ]
                        },
                        "next_chapter_id": "ch_02_center",
                    },
                    {
                        "id": "ch_02_center",
                        "title": "第二章：数据中心的暗门",
                        "current_objective": (
                            "进入云溯科技数据中心机房，读到「沉默者名单」"
                            "后门日志并对林川处置作出集体抉择"
                        ),
                        "milestones": [],
                        "exits_when": {"all_milestones": []},
                        "next_chapter_id": None,
                    },
                ],
            }
        },
    }


def _seed_instance_world(connection, session_id: str, world: dict) -> None:
    """把 instance_configs.world_snapshot_json 写成自定义世界，
    让 _maybe_advance_chapter 在 get_instance_config 路径上拿到。"""
    now = utc_now()
    row = connection.execute(
        "SELECT world_revision FROM instance_configs WHERE session_id = ?",
        (session_id,),
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
            (session_id, json.dumps(world, ensure_ascii=False), now, now),
        )
    else:
        connection.execute(
            """
            UPDATE instance_configs SET
                world_snapshot_json = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (json.dumps(world, ensure_ascii=False), now, session_id),
        )


def _seed_progress(connection, session_id: str, progress: dict) -> None:
    """把 session_rule_states.progress_json 写成给定值（绕过 chapter_migration）。"""
    now = utc_now()
    row = connection.execute(
        "SELECT revision FROM session_rule_states WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO session_rule_states(
                session_id, progress_json, content_boundaries_json,
                npc_policy_json, context_budget_json, dice_rules_json,
                recovery_json, revision, created_at, updated_at
            ) VALUES (?, ?, '{}', '{}', '{}', '{}',
                      '{"state":"idle","message":"","operation_id":""}',
                      1, ?, ?)
            """,
            (
                session_id,
                json.dumps(progress, ensure_ascii=False),
                now,
                now,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE session_rule_states SET
                progress_json = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (json.dumps(progress, ensure_ascii=False), now, session_id),
        )


def _seed_ledger(connection, session_id: str, rows: list[dict]) -> None:
    """Insert story-ledger rows for exact milestone-state tests."""
    now = utc_now()
    for row in rows:
        connection.execute(
            """
            INSERT INTO story_ledger(
                id, session_id, stable_key, kind, title,
                description, status, visibility,
                source_event_id, completed_event_id,
                revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '', ?, 'public', '', '',
                      1, ?, ?)
            """,
            (
                row.get("id") or new_id("ledger"),
                session_id,
                row["stable_key"],
                row["kind"],
                row["title"],
                row["status"],
                now,
                now,
            ),
        )


class ChapterProgressRealignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-realign", "qq:group-realign", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        self.engine = TavernEngine(
            context=SimpleNamespace(),
            database=self.database,
            config_provider=lambda: SimpleNamespace(),
            broker=EventBroker(),
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_overdue_recovery_backfills_cited_milestones_and_preserves_history(self):
        world = _custom_world()
        chapter = world["rules"]["progress"]["chapters"][0]
        mids = chapter["exits_when"]["all_milestones"]
        with self.database._connect() as connection:
            _seed_instance_world(connection, self.session["id"], world)
            _seed_progress(connection, self.session["id"], {
                "current_chapter_id": chapter["id"], "chapter_entered_at_turn": 5,
                "milestone_evidence_since_turn": 0, "announced_milestones": [],
            })
            connection.execute("UPDATE sessions SET turn_no=20 WHERE id=?", (self.session["id"],))
        self.engine._judge_chapter_milestones = AsyncMock(return_value={
            mid: {"source_event_id": "e" + mid, "evidence_event_ids": ["e" + mid],
                  "evidence_quotes": ["独立的已完成事件"], "reason": "历史补记"}
            for mid in mids
        })
        self.engine._judge_chapter_closed = AsyncMock(return_value={
            "event_id": "closure", "quote": "众人已经离开", "reason": "历史收束", "terminal": False,
        })
        self.assertTrue(await self.engine._maybe_advance_chapter(self.session["id"]))
        self.engine._judge_chapter_milestones.assert_awaited_once()
        self.assertEqual(self.engine._judge_chapter_milestones.await_args.kwargs["entered_turn"], 0)
        state = (await self.database.get_session_rule_state(self.session["id"]))["progress"]
        self.assertEqual(state["current_chapter_id"], "ch_02_center")
        self.assertEqual(state["milestone_evidence_since_turn"], 0)
        rows = await self.database.list_story_ledger(self.session["id"])
        self.assertTrue(set(mids).issubset({r["stable_key"] for r in rows if r["status"] == "completed"}))

    async def test_on_time_multiple_verified_milestones_are_saved_together(self):
        world = _custom_world()
        chapter = world["rules"]["progress"]["chapters"][0]
        chapter["max_turns"] = 100
        chapter["min_turns"] = 1
        mids = chapter["exits_when"]["all_milestones"]
        with self.database._connect() as connection:
            _seed_instance_world(connection, self.session["id"], world)
            _seed_progress(connection, self.session["id"], {
                "current_chapter_id": chapter["id"], "chapter_entered_at_turn": 0,
            })
            connection.execute("UPDATE sessions SET turn_no=4 WHERE id=?", (self.session["id"],))
        self.engine._judge_chapter_milestones = AsyncMock(return_value={
            mid: {"source_event_id": "event_" + mid, "evidence_event_ids": ["event_" + mid],
                  "evidence_quotes": ["独立完成的结果"], "reason": "本轮完成"}
            for mid in mids
        })
        self.engine._judge_chapter_closed = AsyncMock(return_value=None)
        await self.engine._maybe_advance_chapter(self.session["id"])
        rows = await self.database.list_story_ledger(self.session["id"])
        self.assertTrue(set(mids).issubset({r["stable_key"] for r in rows if r["status"] == "completed"}))

    async def test_overdue_without_evidence_never_skips(self):
        with self.database._connect() as connection:
            _seed_instance_world(connection, self.session["id"], _custom_world())
            _seed_progress(connection, self.session["id"], {"current_chapter_id": "ch_01_trace"})
            connection.execute("UPDATE sessions SET turn_no=100 WHERE id=?", (self.session["id"],))
        self.engine._judge_chapter_milestones = AsyncMock(return_value=None)
        self.engine._judge_chapter_closed = AsyncMock()
        self.assertFalse(await self.engine._maybe_advance_chapter(self.session["id"]))
        self.engine._judge_chapter_closed.assert_not_awaited()
        state = (await self.database.get_session_rule_state(self.session["id"]))["progress"]
        self.assertEqual(state["current_chapter_id"], "ch_01_trace")

    def test_milestone_accepts_separate_events_but_rejects_missing_evidence(self):
        events = {"a": {"id": "a", "role": "narrator", "turn_no": 1, "content": "队员已经拿到遗书。"},
                  "b": {"id": "b", "role": "narrator", "turn_no": 2, "content": "另一位队员完成石碑检查。"}}
        verdict = {"achieved": True, "criteria": [
            {"passed": True, "event_id": key, "quote": event["content"]}
            for key, event in events.items()
        ]}
        self.assertIsNotNone(self.engine._verified_judge_entry(verdict, events_by_id=events, entered_turn=0))
        verdict["criteria"][1]["quote"] = "未经记录的虚构结果"
        self.assertIsNone(self.engine._verified_judge_entry(verdict, events_by_id=events, entered_turn=0))

    async def test_objective_realigns_to_chapter_declaration(self) -> None:
        """_realign_chapter_progress 把 chapter / current_objective 拉回声明。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], _custom_world())
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    # 陈旧字面值：上一轮模型 state_patch 残留的目标
                    "chapter": "自定义旧标题",
                    "current_objective": "林川会员消费记录（6月27日22:07）",
                    "completed_milestones": 0,
                    "total_milestones": 4,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            connection.execute("COMMIT")

        # 触发 _maybe_advance_chapter 顶部对齐（章节未推进前也会跑）。
        self.engine._judge_chapter_closed = AsyncMock(
            return_value={
                "event_id": "event_close",
                "quote": "本章现场已经收束",
                "reason": "test",
                "terminal": False,
            }
        )
        advanced = await self.engine._maybe_advance_chapter(self.session["id"])
        self.assertFalse(advanced)

        rs = await self.database.get_session_rule_state(self.session["id"])
        progress = rs["progress"]
        self.assertEqual(progress["chapter"], "第一章：城中村的盲区")
        self.assertEqual(
            progress["current_objective"],
            "抵达周记复印店取得加密U盘与线索，识破被盯梢，得知「沉默者名单」",
        )

    async def test_exact_completed_milestones_advance_chapter(self) -> None:
        """Exact completed milestone IDs advance the active chapter."""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], _custom_world())
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter": "第一章：城中村的盲区",
                    "current_objective": (
                        "抵达周记复印店取得加密U盘与线索，"
                        "识破被盯梢，得知「沉默者名单」"
                    ),
                    "completed_milestones": 0,
                    "total_milestones": 4,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            _seed_ledger(
                connection,
                self.session["id"],
                [
                    {
                        "stable_key": "m_01_01_get_usb",
                        "kind": "milestone",
                        "title": "从老周处取得加密U盘",
                        "status": "completed",
                    },
                    {
                        "stable_key": "m_01_02_silent_list_revealed",
                        "kind": "milestone",
                        "title": "得知「沉默者名单」的存在与用途",
                        "status": "completed",
                    },
                    {
                        "stable_key": "m_01_03_tail_shaken",
                        "kind": "milestone",
                        "title": "识破并甩掉盯梢",
                        "status": "completed",
                    },
                ],
            )
            # 章节最短体验门槛（soft = max(8, 0.75*max_turns) = 8）：
            # milestone 全达成后须体验充分才切章，这里把回合推到 9。
            connection.execute(
                "UPDATE sessions SET turn_no = 9 WHERE id = ?",
                (self.session["id"],),
            )
            connection.execute("COMMIT")

        self.engine._judge_chapter_closed = AsyncMock(
            return_value={
                "event_id": "event_close",
                "quote": "本章现场已经收束",
                "reason": "test",
                "terminal": False,
            }
        )
        advanced = await self.engine._maybe_advance_chapter(self.session["id"])
        self.assertTrue(advanced)

        rs = await self.database.get_session_rule_state(self.session["id"])
        progress = rs["progress"]
        # 切到下一章，并且新章节的 title / current_objective 也被刷成声明值。
        self.assertEqual(progress["current_chapter_id"], "ch_02_center")
        self.assertEqual(progress["chapter"], "第二章：数据中心的暗门")
        self.assertEqual(
            progress["current_objective"],
            "进入云溯科技数据中心机房，读到「沉默者名单」"
            "后门日志并对林川处置作出集体抉择",
        )

    async def test_active_milestone_entry_does_not_count(self) -> None:
        """An active milestone row never counts as completed."""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], _custom_world())
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter": "第一章：城中村的盲区",
                    "current_objective": (
                        "抵达周记复印店取得加密U盘与线索，"
                        "识破被盯梢，得知「沉默者名单」"
                    ),
                    "completed_milestones": 0,
                    "total_milestones": 4,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            _seed_ledger(
                connection,
                self.session["id"],
                [
                    {
                        # active milestone：必须精确走 completed 路径，
                        # Active status must not satisfy the chapter exit.
                        "stable_key": "m_01_02_silent_list_revealed",
                        "kind": "milestone",
                        "title": "得知「沉默者名单」的存在与用途",
                        "status": "active",
                    },
                    # 已完成的两个 milestone
                    {
                        "stable_key": "m_01_01_get_usb",
                        "kind": "milestone",
                        "title": "从老周处取得加密U盘",
                        "status": "completed",
                    },
                    {
                        "stable_key": "m_01_03_tail_shaken",
                        "kind": "milestone",
                        "title": "识破并甩掉盯梢",
                        "status": "completed",
                    },
                ],
            )
            connection.execute("COMMIT")

        advanced = await self.engine._maybe_advance_chapter(self.session["id"])
        # m_01_02_silent_list_revealed 仍为 active，不应被推进。
        self.assertFalse(advanced)
        rs = await self.database.get_session_rule_state(self.session["id"])
        self.assertEqual(rs["progress"]["current_chapter_id"], "ch_01_trace")

    async def test_no_advance_before_min_experience(self) -> None:
        """里程碑全达成但体验不足（turn 1 < soft 8）→ 不切章（2026-08-23
        反馈：检查点一达成章节就被切走，玩家没有体验。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], _custom_world())
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter": "第一章：城中村的盲区",
                    "current_objective": "抵达周记复印店取得加密U盘与线索",
                    "completed_milestones": 0,
                    "total_milestones": 4,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            _seed_ledger(
                connection,
                self.session["id"],
                [
                    {
                        "stable_key": "m_01_01_get_usb",
                        "kind": "milestone",
                        "title": "从老周处取得加密U盘",
                        "status": "completed",
                    },
                    {
                        "stable_key": "m_01_02_silent_list_revealed",
                        "kind": "milestone",
                        "title": "得知「沉默者名单」的存在与用途",
                        "status": "completed",
                    },
                    {
                        "stable_key": "m_01_03_tail_shaken",
                        "kind": "milestone",
                        "title": "识破并甩掉盯梢",
                        "status": "completed",
                    },
                ],
            )
            connection.execute("COMMIT")

        advanced = await self.engine._maybe_advance_chapter(self.session["id"])
        # All exact milestones are complete, but this is below the experience gate.
        # 引擎不切章，把章节留给玩家继续体验。
        self.assertFalse(advanced)
        rs = await self.database.get_session_rule_state(self.session["id"])
        self.assertEqual(rs["progress"]["current_chapter_id"], "ch_01_trace")

    async def test_terminal_exact_milestone_opens_ending_phase_first(self) -> None:
        """Exact final milestone must not skip the universal ending narration."""
        world = {
            "name": "测试终章",
            "slug": DEFAULT_WORLD_SLUG,
            "rules": {
                "progress": {
                    "total_milestones": 1,
                    "chapters": [
                        {
                            "id": "ch_end",
                            "title": "终章",
                            "current_objective": "完成裁决",
                            "milestones": [
                                {"id": "m_end_01_verdict", "label": "裁决落定"}
                            ],
                            "exits_when": {
                                "all_milestones": ["m_end_01_verdict"]
                            },
                            "next_chapter_id": None,
                            "min_turns": 1,
                            "max_turns": 1,
                        }
                    ],
                }
            },
        }
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_end",
                    "chapter": "终章",
                    "current_objective": "完成裁决",
                    "completed_milestones": 0,
                    "total_milestones": 1,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                    "announced_milestones": [],
                },
            )
            _seed_ledger(
                connection,
                self.session["id"],
                [
                    {
                        "stable_key": "m_end_01_verdict",
                        "kind": "milestone",
                        "title": "裁决落定",
                        "status": "completed",
                    }
                ],
            )
            connection.execute(
                "UPDATE sessions SET turn_no = 1 WHERE id = ?",
                (self.session["id"],),
            )
            connection.execute("COMMIT")

        self.engine._judge_chapter_closed = AsyncMock(
            return_value={
                "event_id": "event_ending",
                "quote": "最终方案已经执行并交代所有人的结局",
                "reason": "test",
                "terminal": True,
            }
        )
        self.assertFalse(
            await self.engine._maybe_advance_chapter(self.session["id"])
        )
        progress = (
            await self.database.get_session_rule_state(self.session["id"])
        )["progress"]
        self.assertTrue(progress["ending_pending"])
        self.engine._judge_chapter_closed.assert_not_awaited()
        self.assertFalse(progress.get("story_complete", False))
        self.assertEqual(progress["completed_milestones"], 1)
        self.assertIn("m_end_01_verdict", progress["announced_milestones"])

        # A hard-validated ending turn writes this marker atomically. Only then
        # may the same terminal checkpoint become story_complete.
        state = await self.database.get_session_rule_state(self.session["id"])
        await self.database.save_session_rule_state(
            self.session["id"],
            {
                "progress": {
                    **state["progress"],
                    "ending_pending": False,
                    "ending_narrated": True,
                    "ending_event_id": "event_ending_template",
                },
                "revision": state["revision"],
            },
            actor_id="test_ending_commit",
        )
        self.assertFalse(
            await self.engine._maybe_advance_chapter(self.session["id"])
        )
        progress = (
            await self.database.get_session_rule_state(self.session["id"])
        )["progress"]
        self.assertTrue(progress["story_complete"])

    async def test_verified_milestone_persists_event_provenance(self) -> None:
        written = await self.database.complete_milestones(
            self.session["id"],
            [
                {
                    "id": "m_audit_01",
                    "title": "取得原始契约",
                    "source_event_id": "event_narrator_42",
                    "description": '{"verification":"milestone_judge"}',
                }
            ],
        )
        self.assertEqual(written, 1)
        row = next(
            item
            for item in await self.database.list_story_ledger(self.session["id"])
            if item["stable_key"] == "m_audit_01"
        )
        self.assertEqual(row["source_event_id"], "event_narrator_42")
        self.assertEqual(row["completed_event_id"], "event_narrator_42")
        self.assertIn("milestone_judge", row["description"])

    async def test_milestone_judge_cursor_is_saved_before_judging(self) -> None:
        """A due judge run records its real turn and materializes its verdict.

        Regression for the scheduling prelude that referenced ``_pending`` and
        the turn variables before assignment.  Its swallowed NameError kept the
        cursor stale and caused every later turn to retry without reliably
        recording a completion.
        """
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], _custom_world())
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter": "第一章：城中村的盲区",
                    "current_objective": "抵达周记复印店取得加密U盘与线索",
                    "completed_milestones": 0,
                    "total_milestones": 4,
                    "chapter_entered_at_turn": 0,
                    "last_milestone_judge_at_turn": 0,
                    "narrative_length_band": "standard",
                    "announced_milestones": [],
                },
            )
            connection.execute(
                "UPDATE sessions SET turn_no = 2 WHERE id = ?",
                (self.session["id"],),
            )
            connection.execute("COMMIT")

        self.engine._judge_chapter_milestones = AsyncMock(
            return_value={
                "m_01_01_get_usb": {
                    "source_event_id": "event_usb",
                    "evidence_event_ids": ["event_usb"],
                    "evidence_quotes": ["老周把加密U盘交到众人手中"],
                    "reason": "正文明确记录了取得结果",
                }
            }
        )

        self.assertFalse(
            await self.engine._maybe_advance_chapter(self.session["id"])
        )
        progress = (
            await self.database.get_session_rule_state(self.session["id"])
        )["progress"]
        self.assertEqual(progress["last_milestone_judge_at_turn"], 2)
        self.engine._judge_chapter_milestones.assert_awaited_once()
        completed = {
            row["stable_key"]
            for row in await self.database.list_story_ledger(self.session["id"])
            if row["kind"] == "milestone" and row["status"] == "completed"
        }
        self.assertIn("m_01_01_get_usb", completed)


class MilestoneJudgeTruncationTests(unittest.IsolatedAsyncioTestCase):
    """2026-09-20 线上事故：判定输出被 max_tokens 截断 → 章节永不切。

    ch_05「王选散场」走进 40 个回合、里程碑 0 次公告，token_usage 显示
    最近 24 次 milestone_judge 里 22 次 output_tokens 正好等于上限。
    截断的 JSON 解析失败后原实现静默 ``continue``，判定结果被整个丢掉。
    """

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-judge", "qq:group-judge", DEFAULT_WORLD_SLUG, "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        self.engine = TavernEngine(
            context=SimpleNamespace(),
            database=self.database,
            config_provider=lambda: SimpleNamespace(
                max_tokens=1200, request_timeout_seconds=5,
            ),
            broker=EventBroker(),
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _events(self):
        return [
            {
                "id": "event_u",
                "role": "narrator",
                "turn_no": 12,
                "content": "老周从抽屉里取出加密U盘，交到众人手中。",
            },
            {
                "id": "event_l",
                "role": "narrator",
                "turn_no": 13,
                "content": "纸上写着沉默者名单，七个名字排成一列。",
            },
            {
                "id": "event_t",
                "role": "narrator",
                "turn_no": 14,
                "content": "众人绕过后巷，把盯梢的人甩掉了。",
            },
        ]

    async def _judge(self, completion: str):
        self.engine._progress_providers = AsyncMock(return_value=["model"])
        self.database.chapter_narrator_events = AsyncMock(return_value=self._events())
        self.engine._llm_generate_metered = AsyncMock(
            return_value=SimpleNamespace(completion_text=completion)
        )
        world = _custom_world()
        chapter = world["rules"]["progress"]["chapters"][0]
        meta = {
            m["id"]: {"label": m["label"], "words": ["U盘", "沉默者名单", "盯梢"]}
            for m in chapter["milestones"]
        }
        return await self.engine._judge_chapter_milestones(
            session_id=self.session["id"],
            world=world,
            chapter=chapter,
            pending_ids=list(meta),
            milestone_meta=meta,
            entered_turn=0,
        )

    async def test_truncated_output_still_credits_complete_entries(self):
        truncated = (
            '{"milestones": ['
            '{"id": "m_01_01_get_usb", "achieved": true, "criteria": ['
            '{"name": "取得U盘", "passed": true, "event_id": "event_u", '
            '"quote": "老周从抽屉里取出加密U盘"}], "reason": "正文记录取得结果"}, '
            '{"id": "m_01_02_silent_list_revealed", "achieved": true, "criteria": ['
            '{"name": "得知名单", "passed": true, "event_id": "event_l", '
        )
        verdict = await self._judge(truncated)
        self.assertIsNotNone(verdict)
        self.assertIn("m_01_01_get_usb", verdict)
        self.assertEqual(
            verdict["m_01_01_get_usb"]["evidence_event_ids"], ["event_u"]
        )
        self.assertNotIn("m_01_02_silent_list_revealed", verdict)

    async def test_unparseable_output_yields_no_false_credit(self):
        self.assertIsNone(await self._judge('{"milestones": [{"id": "m_01_0'))

    async def test_full_verdict_still_parses(self):
        full = json.dumps({
            "milestones": [
                {
                    "id": "m_01_01_get_usb",
                    "achieved": True,
                    "criteria": [{
                        "name": "取得U盘",
                        "passed": True,
                        "event_id": "event_u",
                        "quote": "老周从抽屉里取出加密U盘",
                    }],
                    "reason": "正文记录取得结果",
                },
                {"id": "m_01_02_silent_list_revealed", "achieved": False,
                 "criteria": [{"name": "得知名单", "passed": False,
                               "event_id": "", "quote": ""}],
                 "reason": "尚未读到"},
            ]
        }, ensure_ascii=False)
        verdict = await self._judge(full)
        self.assertEqual(list(verdict), ["m_01_01_get_usb"])

    async def test_judge_token_budget_is_not_capped_at_800(self):
        from tavern.engine import _milestone_judge_max_tokens
        self.assertGreaterEqual(
            _milestone_judge_max_tokens(SimpleNamespace(max_tokens=1200)), 2400
        )
        self.assertEqual(
            _milestone_judge_max_tokens(SimpleNamespace(max_tokens=6000)), 6000
        )
        self.assertEqual(
            _milestone_judge_max_tokens(SimpleNamespace(max_tokens=None)), 2400
        )


if __name__ == "__main__":
    unittest.main()
