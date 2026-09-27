"""章节推进指令（_pacing_chapter_directive）的回归测试。

覆盖：
1. HARD-COMPLETE：所有 milestone 精确 ID 完成 → 必须本轮切章。
2. SOFT-PENDING：仍有未达成 milestone 但未到硬阈值 → 仅提示。
3. 所有 milestone 已 completed → 不发指令（切章由 _maybe_advance_chapter
   接管，避免重复指令）。
4. _realign_chapter_progress 把陈旧 objective 刷掉后，指令里的
   current_objective 必须用章节声明文本。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now
from tavern.engine import TavernEngine
from tavern.events import EventBroker


def _custom_world() -> dict:
    """Return a two-chapter world with three exact-ID milestones."""
    return {
        "name": "测试世界 · 章节推进指令",
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
                        "max_turns": 6,  # 缩到 6 让 soft/hard 易触发
                        "min_turns": 6,
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


def _bump_turn_no(connection, session_id: str, turn_no: int) -> None:
    connection.execute(
        "UPDATE sessions SET turn_no = ? WHERE id = ?",
        (turn_no, session_id),
    )


class ChapterPacingDirectiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-pacing", "qq:group-pacing", DEFAULT_WORLD_SLUG,
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
        self.world = _custom_world()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _seed_completed_evidence(self, turn_no: int) -> None:
        """Seed three completed milestones by exact authoritative IDs."""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
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
            _bump_turn_no(connection, self.session["id"], turn_no)
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

    async def test_completed_chapter_does_not_wait_for_budget_fraction(self) -> None:
        """Completed outcomes request closure without filling 75% of the budget."""
        chapter = self.world["rules"]["progress"]["chapters"][0]
        chapter["min_turns"] = 1
        chapter["max_turns"] = 48
        await self._seed_completed_evidence(turn_no=3)
        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Pacing Chapter-HARD-COMPLETE]", directive)
        self.assertNotIn("SOFT-COMPLETE", directive)
        self.assertIn("必须结算当前冲突", directive)
        self.assertIn("ch_02_center", directive)  # 仍告知下一章方向

    async def test_hard_complete_after_min_experience(self) -> None:
        """达成且已滞留 ≥ soft（体验充分）→ HARD-COMPLETE 强制本轮切章。"""
        await self._seed_completed_evidence(turn_no=10)
        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Pacing Chapter-HARD-COMPLETE]", directive)
        self.assertIn("ch_02_center", directive)
        self.assertIn("必须结算当前冲突", directive)
        self.assertIn("不得修改 state_patch.progress.current_chapter_id", directive)

    async def test_soft_pending_when_one_milestone_unfinished(self) -> None:
        """还有未达成 milestone 但未到硬阈值 → SOFT-PENDING 不含 HARD。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
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
            _bump_turn_no(connection, self.session["id"], 1)
            _seed_ledger(
                connection,
                self.session["id"],
                [
                    {
                        "stable_key": "usd_a",
                        "kind": "clue",
                        "title": "老周已转交加密U盘一枚",
                        "status": "active",
                    },
                ],
            )
            connection.execute("COMMIT")

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Pacing Chapter-SOFT-PENDING]", directive)
        self.assertNotIn("HARD-COMPLETE", directive)
        self.assertIn("m_01_02_silent_list_revealed", directive)
        self.assertIn("m_01_03_tail_shaken", directive)

    async def test_pending_reaches_hard_at_declared_max_turns(self) -> None:
        """短章在声明的第6次行动进入 HARD，不再被全局16次下限拖长。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter_entered_at_turn": 0,
                    "current_objective": "取得U盘并甩掉盯梢",
                },
            )
            _bump_turn_no(connection, self.session["id"], 6)
            connection.execute("COMMIT")

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Pacing Chapter-HARD-PENDING]", directive)
        self.assertIn("6/6", directive)
        self.assertIn("不得阻止玩家前往任何合理地点", directive)

    async def test_severely_overdue_chapter_must_finish_declared_travel(self) -> None:
        """超过两倍预算后不得把一次撤离继续拆成多轮短距离移动。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter_entered_at_turn": 0,
                    "current_objective": "取得U盘并甩掉盯梢",
                },
            )
            _bump_turn_no(connection, self.session["id"], 12)
            connection.execute("COMMIT")

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Pacing Chapter-CRITICAL-PENDING]", directive)
        self.assertIn("完成整段转场", directive)
        self.assertIn("禁止把一次旅行拆成数轮十几米", directive)
        self.assertIn("不得伪造里程碑", directive)

    async def test_terminal_complete_does_not_request_second_ending(self) -> None:
        """Terminal milestones complete -> do not write the ending twice."""
        # 改用终章场景：ch_02 是最后一章，所有 milestone 已完成
        terminal_world = {
            "name": "测试世界 · 终章",
            "slug": DEFAULT_WORLD_SLUG,
            "system_prompt": "测试世界系统提示。",
            "rules": {
                "progress": {
                    "total_milestones": 1,
                    "chapters": [
                        {
                            "id": "ch_terminal",
                            "title": "终章：证据的去向",
                            "current_objective": "全体就证据最终去向作出集体表决",
                            "milestones": [
                                {
                                    "id": "m_term_01",
                                    "label": "尘埃落定",
                                    "evidence_required": [],
                                },
                            ],
                            "exits_when": {"all_milestones": ["m_term_01"]},
                            "next_chapter_id": None,
                        },
                    ],
                }
            },
        }
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], terminal_world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_terminal",
                    "chapter": "终章：证据的去向",
                    "current_objective": "全体就证据最终去向作出集体表决",
                    "completed_milestones": 1,
                    "total_milestones": 1,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            _seed_ledger(
                connection,
                self.session["id"],
                [
                    {
                        "stable_key": "m_term_01",
                        "kind": "milestone",
                        "title": "尘埃落定",
                        "status": "completed",
                    },
                ],
            )
            connection.execute("COMMIT")

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], terminal_world
        )
        self.assertEqual(directive, "")

    async def test_declared_ending_milestone_triggers_one_compact_epilogue(self) -> None:
        terminal_world = {
            "name": "测试世界 · 终章",
            "slug": DEFAULT_WORLD_SLUG,
            "system_prompt": "测试世界系统提示。",
            "rules": {"progress": {"total_milestones": 1, "chapters": [{
                "id": "ch_terminal",
                "title": "终章",
                "current_objective": "结束主线",
                "milestones": [{
                    "id": "m_epilogue",
                    "label": "紧凑尾声",
                    "ending_milestone": True,
                    "evidence_required": [],
                }],
                "exits_when": {"all_milestones": ["m_epilogue"]},
                "next_chapter_id": None,
            }]}}
        }
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], terminal_world)
            _seed_progress(connection, self.session["id"], {
                "current_chapter_id": "ch_terminal",
                "chapter_entered_at_turn": 0,
                "current_objective": "结束主线",
            })
            _bump_turn_no(connection, self.session["id"], 40)
            connection.execute("COMMIT")

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], terminal_world
        )
        self.assertIn("[Pacing Chapter-ENDING-NOW]", directive)
        self.assertIn("队伍整体离场/去向", directive)
        self.assertIn("不得逐一为每名玩家安排私人结局", directive)
        context = await self.engine._ending_phase_context(
            self.session["id"], terminal_world
        )
        self.assertIsNotNone(context)
        self.assertEqual(
            [item["id"] for item in context["milestones"]],
            ["m_epilogue"],
        )

    async def test_legacy_terminal_world_gets_generic_ending_fallback(self) -> None:
        """Old worlds need no ending_milestone migration."""
        terminal_world = {
            "name": "旧世界",
            "slug": DEFAULT_WORLD_SLUG,
            "rules": {"progress": {"chapters": [{
                "id": "ch_old_end",
                "title": "终章",
                "current_objective": "交代任务结果并离场",
                "milestones": [{"id": "m_old_done", "label": "任务完成"}],
                "exits_when": {"all_milestones": ["m_old_done"]},
                "next_chapter_id": None,
            }]}},
        }
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], terminal_world)
            _seed_progress(connection, self.session["id"], {
                "current_chapter_id": "ch_old_end",
                "chapter_entered_at_turn": 0,
                "current_objective": "交代任务结果并离场",
            })
            _seed_ledger(connection, self.session["id"], [{
                "stable_key": "m_old_done",
                "kind": "milestone",
                "title": "任务完成",
                "status": "completed",
            }])
            _bump_turn_no(connection, self.session["id"], 40)
            connection.execute("COMMIT")

        context = await self.engine._ending_phase_context(
            self.session["id"], terminal_world
        )
        self.assertIsNotNone(context)
        self.assertTrue(context["legacy_fallback"])
        self.assertEqual(context["milestones"], [])

    async def test_guard_hard_fires_before_story_complete(self) -> None:
        """结局点未达成且严重超时 → Guard-HARD 仍会压模型提交里程碑/钩子。"""
        terminal_world = {
            "name": "测试世界 · 终章",
            "slug": DEFAULT_WORLD_SLUG,
            "system_prompt": "测试世界系统提示。",
            "rules": {
                "progress": {
                    "total_milestones": 1,
                    "chapters": [
                        {
                            "id": "ch_terminal",
                            "title": "终章：证据的去向",
                            "current_objective": "全体就证据最终去向作出集体表决",
                            "max_turns": 1,
                            "milestones": [
                                {
                                    "id": "m_term_01",
                                    "label": "尘埃落定",
                                    "evidence_required": [],
                                },
                            ],
                            "exits_when": {"all_milestones": ["m_term_01"]},
                            "next_chapter_id": None,
                        },
                    ],
                }
            },
        }
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], terminal_world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_terminal",
                    "chapter": "终章：证据的去向",
                    "current_objective": "全体就证据最终去向作出集体表决",
                    "completed_milestones": 0,
                    "total_milestones": 1,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            _bump_turn_no(connection, self.session["id"], 30)
            connection.execute("COMMIT")

        directive = await self.engine._pacing_directive(
            self.session["id"], terminal_world
        )
        self.assertIn("[Pacing Guard-HARD]", directive)

    async def test_guard_backs_off_when_story_complete(self) -> None:
        """结局点已达成（story_complete=True）→ 不再给 Guard-HARD，
        避免模型继续补里程碑/钩子而写不出收尾。"""
        terminal_world = {
            "name": "测试世界 · 终章",
            "slug": DEFAULT_WORLD_SLUG,
            "system_prompt": "测试世界系统提示。",
            "rules": {
                "progress": {
                    "total_milestones": 1,
                    "chapters": [
                        {
                            "id": "ch_terminal",
                            "title": "终章：证据的去向",
                            "current_objective": "全体就证据最终去向作出集体表决",
                            "max_turns": 1,
                            "milestones": [
                                {
                                    "id": "m_term_01",
                                    "label": "尘埃落定",
                                    "evidence_required": [],
                                },
                            ],
                            "exits_when": {"all_milestones": ["m_term_01"]},
                            "next_chapter_id": None,
                        },
                    ],
                }
            },
        }
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], terminal_world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_terminal",
                    "chapter": "终章：证据的去向",
                    "current_objective": "全体就证据最终去向作出集体表决",
                    "completed_milestones": 1,
                    "total_milestones": 1,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                    "story_complete": True,
                },
            )
            _bump_turn_no(connection, self.session["id"], 30)
            connection.execute("COMMIT")

        directive = await self.engine._pacing_directive(
            self.session["id"], terminal_world
        )
        self.assertEqual(directive, "")

    async def test_directive_uses_chapter_declaration_after_realign(self) -> None:
        """_realign_chapter_progress 之后，指令里的 objective 用声明文本。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
            _seed_progress(
                connection,
                self.session["id"],
                {
                    "current_chapter_id": "ch_01_trace",
                    "chapter": "自定义旧标题",
                    "current_objective": "陈旧目标字符串",
                    "completed_milestones": 0,
                    "total_milestones": 4,
                    "chapter_entered_at_turn": 0,
                    "narrative_length_band": "standard",
                },
            )
            connection.execute("COMMIT")

        await self.engine._maybe_advance_chapter(self.session["id"])

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertNotIn("陈旧目标字符串", directive)
        self.assertIn("抵达周记复印店", directive)

    async def test_soft_pending_does_not_rush_milestones(self) -> None:
        """未到软阈值时不催赶——里程碑由玩家行动自然达成（2026-08-23
        节奏反馈：催促是剧情推进过快的主因）。"""
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
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
            _bump_turn_no(connection, self.session["id"], 1)
            connection.execute("COMMIT")

        directive = await self.engine._pacing_chapter_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Pacing Chapter-SOFT-PENDING]", directive)
        self.assertIn("由玩家行动自然达成", directive)
        self.assertIn("不要为完成里程碑而催赶剧情", directive)
        self.assertIn("章节不是地点锁", directive)
        self.assertIn("玩家可前往任何合理地点", directive)
        self.assertNotIn("必须停留在本章场景", directive)
        self.assertNotIn("不得进入下一章的地点", directive)
        self.assertNotIn("需推进", directive)


if __name__ == "__main__":
    unittest.main()
