"""选项层节奏指令（_choice_pacing_directive）与停滞回退的回归测试。

背景：叙事层拿到 [Pacing Chapter-HARD-PENDING] 节奏指令，但 A/B/C/D
选项生成层（choice_generation_prompt）从未看到任何节奏/里程碑信息，导致
场景里玩家只能从「检查/询问/观察/等待」里选，剧情无限原地转圈（见
「签约」场景卡死 6+ 回合的案例）。

覆盖：
1. HARD：章节滞留超过 hard 阈值且仍有未达成里程碑 → 指令含
   [Choice-Pacing-HARD]、未达成里程碑、本章目标、决定性选项要求。
2. SOFT：未到 hard 阈值但仍有 pending → [Choice-Pacing-SOFT]。
3. 无 pending（当前章节里程碑全部达成）→ 返回空串。
4. 无 current_chapter_id / 世界不含该章节 → 返回空串。
5. _all_options_stall：四选项全停滞 → True；含决定性动作 → False。
6. choice_generation_prompt 在传入 pacing_directive 时渲染
   <pacing_directive> 块与决定性选项规则。
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
from tavern.prompts import choice_generation_prompt, choice_repair_prompt


def _custom_world() -> dict:
    """单章 + 2 个 milestone + max_turns=4 的世界（贴近真实终章配置）。"""
    return {
        "name": "测试世界 · 选项节奏指令",
        "slug": DEFAULT_WORLD_SLUG,
        "system_prompt": "测试世界系统提示。",
        "rules": {
            "progress": {
                "total_milestones": 2,
                "chapters": [
                    {
                        "id": "ch_01_finale",
                        "title": "终章：证据的去向",
                        "current_objective": (
                            "全体就证据最终去向作出集体表决并执行"
                        ),
                        "max_turns": 4,  # hard = max(16, 4) = 16
                        "milestones": [
                            {
                                "id": "m_01_01_verdict_voted",
                                "label": "全体就证据最终去向集体表决并执行",
                                "evidence_required": [
                                    {
                                        "type": "clue_keyword_any",
                                        "match": ["表决", "发布"],
                                    }
                                ],
                            },
                            {
                                "id": "m_01_02_aftermath",
                                "label": "尘埃落定，各人结局给出",
                                "evidence_required": [
                                    {
                                        "type": "clue_keyword_any",
                                        "match": ["结局", "后续"],
                                    }
                                ],
                            },
                        ],
                        "exits_when": {
                            "all_milestones": [
                                "m_01_01_verdict_voted",
                                "m_01_02_aftermath",
                            ]
                        },
                        "next_chapter_id": None,
                    }
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


def _bump_turn_no(connection, session_id: str, turn_no: int) -> None:
    connection.execute(
        "UPDATE sessions SET turn_no = ? WHERE id = ?",
        (turn_no, session_id),
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


def _default_progress(chapter: str = "ch_01_finale", entered: int = 0) -> dict:
    return {
        "current_chapter_id": chapter,
        "chapter": "终章：证据的去向",
        "current_objective": "全体就证据最终去向作出集体表决并执行",
        "completed_milestones": 0,
        "total_milestones": 2,
        "chapter_entered_at_turn": entered,
        "narrative_length_band": "compact",
    }


class ChoicePacingDirectiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-choice-pacing", "qq:group-choice-pacing",
            DEFAULT_WORLD_SLUG, "admin-1",
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

    def _seed(self, *, progress: dict, turn_no: int, ledger: list[dict]) -> None:
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _seed_instance_world(connection, self.session["id"], self.world)
            _seed_progress(connection, self.session["id"], progress)
            _bump_turn_no(connection, self.session["id"], turn_no)
            _seed_ledger(connection, self.session["id"], ledger)
            connection.execute("COMMIT")

    async def test_hard_when_overstayed_with_pending(self) -> None:
        """滞留超过 hard（16）且里程碑未达成 → HARD 指令带推进要求。"""
        self._seed(
            progress=_default_progress(entered=0),
            turn_no=20,
            ledger=[],  # 没有任何线索证据 → 两个 milestone 都 pending
        )
        directive = await self.engine._choice_pacing_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Choice-Pacing-HARD]", directive)
        self.assertIn("m_01_01_verdict_voted", directive)
        self.assertIn("全体就证据最终去向作出集体表决并执行", directive)
        self.assertIn("推进性", directive)

    async def test_soft_when_below_hard(self) -> None:
        """未到 hard 但有 pending → SOFT。"""
        self._seed(
            progress=_default_progress(entered=0),
            turn_no=2,
            ledger=[],
        )
        directive = await self.engine._choice_pacing_directive(
            self.session["id"], self.world
        )
        self.assertIn("[Choice-Pacing-SOFT]", directive)
        self.assertNotIn("[Choice-Pacing-HARD]", directive)

    async def test_empty_when_all_milestones_completed(self) -> None:
        """当前章节里程碑全部达成 → 空串（无推进压力）。"""
        self._seed(
            progress=_default_progress(entered=0),
            turn_no=20,
            ledger=[
                {
                    "stable_key": "m_01_01_verdict_voted",
                    "kind": "milestone",
                    "title": "全体就证据最终去向集体表决并执行",
                    "status": "completed",
                },
                {
                    "stable_key": "m_01_02_aftermath",
                    "kind": "milestone",
                    "title": "尘埃落定，各人结局给出",
                    "status": "completed",
                },
            ],
        )
        directive = await self.engine._choice_pacing_directive(
            self.session["id"], self.world
        )
        self.assertEqual(directive, "")

    async def test_empty_when_no_chapter(self) -> None:
        """无 current_chapter_id → 空串，不影响正常选项生成。"""
        self._seed(
            progress={"completed_milestones": 0, "total_milestones": 2},
            turn_no=5,
            ledger=[],
        )
        directive = await self.engine._choice_pacing_directive(
            self.session["id"], self.world
        )
        self.assertEqual(directive, "")

    async def test_stall_check_matches_all_stall_options(self) -> None:
        """四选项全停滞 → _all_options_stall 为 True。"""
        choices = [
            {"key": "A", "text": "查看备用机上的备份节点记录"},
            {"key": "B", "text": "向苏姐询问封存范围与流程"},
            {"key": "C", "text": "观察随行人员的神态与站位"},
            {"key": "D", "text": "整理物证台账并确认清单"},
        ]
        self.assertTrue(self.engine._all_options_stall(choices))

    async def test_stall_check_false_when_any_decisive(self) -> None:
        """任一选项含决定性动作词 → 不判定为停滞。"""
        choices = [
            {"key": "A", "text": "查看备用机上的备份节点记录"},
            {"key": "B", "text": "直接落笔在协议上签字完成交易"},
            {"key": "C", "text": "观察随行人员的神态与站位"},
            {"key": "D", "text": "整理物证台账并确认清单"},
        ]
        self.assertFalse(self.engine._all_options_stall(choices))

    async def test_pacing_requires_decisive_only_on_hard(self) -> None:
        self.assertTrue(
            self.engine._pacing_requires_decisive("[Choice-Pacing-HARD] x")
        )
        self.assertTrue(
            self.engine._pacing_requires_decisive("[Choice-Pacing-ENDING] x")
        )
        self.assertFalse(
            self.engine._pacing_requires_decisive("[Choice-Pacing-SOFT] x")
        )
        self.assertFalse(self.engine._pacing_requires_decisive(""))

    async def test_option_is_noop_real_world_phrases(self) -> None:
        """真实会话里出现过的零行动选项，全部应被判定为 no-op。"""
        noop = [
            "就地清点随身物证，核对U盘、台账与备份记录的完整度",
            "快速清点身上的物证台账复印件和随身物品，确认没有遗落",
            "在巷口就地清点人数，确认队友与物证都在",
            "翻出物证台账再核对一遍，确保交接时链完整",
            "拿起协议逐页翻看，核对条款是否与之前约定一致",
            "低头翻看金属台面上的协议条款，核对封存与释放条件",
            "快速审读协议关键条款，确认封存范围与免责边界",
            "站到陈默和林川身旁，帮他们挡一下雨，等全队撤离",
            "检查现在手上的物品",
            "站在原地等待下一步指示",
        ]
        for text in noop:
            self.assertTrue(
                self.engine._option_is_noop(text), f"应为零行动: {text}"
            )

    async def test_option_is_noop_planning_and_status_patterns(self) -> None:
        """结局点收尾场景反复出现的「敲定安排/核实流程/被动观察/状态检查」。
        这些不产生任何推进，必须被判为零行动。"""
        noop = [
            "在警车上低声和队友确认城郊下车后的分散路线与汇合暗号",
            "向谭队核实证据封存后的上报流程与关键时间节点",
            "留意警车行驶路线与沿途监控，判断是否有被跟踪迹象",
            "用备用机快速检查备份节点的可访问状态并记录",
            "记下谭队给出的座机号与联络时间，默存于心",
            "向谭队确认证据封存后的上报层级与大致周期",
            "和队友低声核对各自撤离后的临时落脚点与联络方式",
            "和队友快速敲定各自暂避地点与失联后的联络暗号",
        ]
        for text in noop:
            self.assertTrue(
                self.engine._option_is_noop(text), f"应为零行动: {text}"
            )

    async def test_option_is_not_noop_real_world_phrases(self) -> None:
        """真实情报搜集/决定性行动不应被误判为零行动。"""
        active = [
            "观察巷口追兵人数、装备和前进路线，判断薄弱方向",
            "核对台面上的协议与签字笔，确认文件是否被调包或改动",
            "借雨幕掩护观察巷口盯梢车辆的人员与动向",
            "直接落笔在协议上签字完成交易",
            "用备用机悄悄录下当前签约现场的对话与环境音",
            "带队走最熟的盲巷岔路，尽量绕开街面监控",
            "提议全队立刻动身前往老码头派出所后门赴约",
        ]
        for text in active:
            self.assertFalse(
                self.engine._option_is_noop(text), f"不应误判: {text}"
            )

    async def test_maybe_force_decisive_replaces_noop(self) -> None:
        """任选项为零行动 → 无条件触发一次重生成，替换为决定性选项。"""
        pid = "participant_p1"
        good = [
            {"key": "A", "actor_id": pid, "text": "落笔在协议上签字，完成移交", "danger_id": "safe"},
            {"key": "B", "actor_id": pid, "text": "当场向苏姐摊牌，公布备份节点地址", "danger_id": "desperate"},
            {"key": "C", "actor_id": pid, "text": "拒绝签字，带陈默和林川离开会场", "danger_id": "dangerous"},
            {"key": "D", "actor_id": pid, "text": "招呼全队动身前往老码头派出所后门", "danger_id": "controlled"},
        ]
        calls: list[tuple[str, str]] = []

        async def fake_llm(*, session_id, request_type, provider_id, prompt,
                           system_prompt_value="", max_tokens=0, **kwargs):
            calls.append((request_type, prompt))
            return SimpleNamespace(
                completion_text=json.dumps({"choices": good}, ensure_ascii=False)
            )

        self.engine._llm_generate_metered = fake_llm
        noop_choices = [
            {"key": "A", "text": "就地清点随身物证，核对U盘、台账与备份记录"},
            {"key": "B", "text": "检查现在手上的物品，确认没有遗漏"},
            {"key": "C", "text": "站在原地等待下一步指示"},
            {"key": "D", "text": "低头翻看协议条款，核对封存条件"},
        ]
        result = await self.engine._maybe_force_decisive(
            noop_choices,
            pacing_directive="",
            provider_ids=["provider-test"],
            world=self.world,
            session={
                "id": self.session["id"],
                "world_state": {"location": "签约现场"},
            },
            participant={"participant_id": pid, "character_name": "jade"},
            events=[],
            config=SimpleNamespace(
                temperature=0.7, max_tokens=800, request_timeout_seconds=30
            ),
        )
        self.assertEqual(len(calls), 1, "应触发一次重生成")
        self.assertTrue(calls[0][0].endswith("_decisive"))
        self.assertIn("[Choice-Force-Decisive]", calls[0][1])
        self.assertIn("零行动", calls[0][1])
        got_texts = [str(c.get("text") or "") for c in result]
        self.assertEqual(got_texts, [str(g["text"]) for g in good])

    async def test_maybe_force_decisive_skips_when_all_active(self) -> None:
        """全部选项都是实际行动 → 不触发重生成。"""
        pid = "participant_p1"
        calls: list[tuple[str, str]] = []

        async def fake_llm(*, session_id, request_type, provider_id, prompt,
                           system_prompt_value="", max_tokens=0, **kwargs):
            calls.append((request_type, prompt))
            return SimpleNamespace(completion_text="{}")

        self.engine._llm_generate_metered = fake_llm
        active = [
            {"key": "A", "text": "落笔在协议上签字，完成移交"},
            {"key": "B", "text": "当场向苏姐摊牌，公布备份节点地址"},
            {"key": "C", "text": "拒绝签字，带陈默和林川离开会场"},
            {"key": "D", "text": "招呼全队动身前往老码头派出所后门"},
        ]
        result = await self.engine._maybe_force_decisive(
            active,
            pacing_directive="",
            provider_ids=["provider-test"],
            world=self.world,
            session={"id": self.session["id"], "world_state": {}},
            participant={"participant_id": pid},
            events=[],
            config=SimpleNamespace(
                temperature=0.7, max_tokens=800, request_timeout_seconds=30
            ),
        )
        self.assertEqual(calls, [], "不应触发重生成")
        self.assertEqual(
            [str(c.get("text") or "") for c in result],
            [str(a["text"]) for a in active],
        )

    async def test_choice_pacing_ending_when_story_complete(self) -> None:
        """story_complete=True → 不再生成额外的收尾选项。"""
        self._seed(
            progress={
                "current_chapter_id": "ch_01_finale",
                "chapter": "终章：证据的去向",
                "current_objective": "全体就证据最终去向作出集体表决并执行",
                "completed_milestones": 2,
                "total_milestones": 2,
                "chapter_entered_at_turn": 0,
                "narrative_length_band": "compact",
                "story_complete": True,
            },
            turn_no=5,
            ledger=[],
        )
        directive = await self.engine._choice_pacing_directive(
            self.session["id"], self.world
        )
        self.assertEqual(directive, "")

    async def test_maybe_force_decisive_replaces_noop_on_ending(self) -> None:
        """ENDING 指令 + 四选项全停滞 → 触发重生成，force note 含收尾要求。"""
        pid = "participant_p1"
        good = [
            {"key": "A", "actor_id": pid, "text": "在警车旁向谭队作最后道别", "danger_id": "safe"},
            {"key": "B", "actor_id": pid, "text": "当场决定不再碰证据，各人按约定散开", "danger_id": "dangerous"},
            {"key": "C", "actor_id": pid, "text": "当众销毁备份，表明就此收手", "danger_id": "desperate"},
            {"key": "D", "actor_id": pid, "text": "和队友约定到老周处汇合后各安其事", "danger_id": "controlled"},
        ]
        calls: list[tuple[str, str]] = []

        async def fake_llm(*, session_id, request_type, provider_id, prompt,
                           system_prompt_value="", max_tokens=0, **kwargs):
            calls.append((request_type, prompt))
            return SimpleNamespace(
                completion_text=json.dumps({"choices": good}, ensure_ascii=False)
            )

        self.engine._llm_generate_metered = fake_llm
        noop_choices = [
            {"key": "A", "text": "和队友确认分散路线与汇合暗号"},
            {"key": "B", "text": "向谭队核实上报流程与关键时间节点"},
            {"key": "C", "text": "留意警车行驶路线与沿途监控"},
            {"key": "D", "text": "检查备份节点的可访问状态并记录"},
        ]
        result = await self.engine._maybe_force_decisive(
            noop_choices,
            pacing_directive=(
                "[Choice-Pacing-ENDING] 剧情已到结局点，必须推进收尾。"
            ),
            provider_ids=["provider-test"],
            world=self.world,
            session={"id": self.session["id"], "world_state": {}},
            participant={"participant_id": pid},
            events=[],
            config=SimpleNamespace(
                temperature=0.7, max_tokens=800, request_timeout_seconds=30
            ),
        )
        self.assertEqual(len(calls), 1, "应触发一次重生成")
        self.assertIn("[Choice-Force-Decisive]", calls[0][1])
        self.assertIn("收尾", calls[0][1])
        got_texts = [str(c.get("text") or "") for c in result]
        self.assertEqual(got_texts, [str(g["text"]) for g in good])


class ChoicePromptPacingRenderTests(unittest.TestCase):
    def test_prompt_renders_pacing_block_and_rule(self) -> None:
        prompt = choice_generation_prompt(
            world=_custom_world(),
            session={"world_state": {"location": "双子楼B座大堂外"}},
            participant={"participant_id": "p_1", "character_name": "jade"},
            events=[],
            pacing_directive=(
                "[Choice-Pacing-HARD] 当前章节 ch_01_finale 已滞留 20/4"
                " 回合，未达成的里程碑：m_01_01_verdict_voted。"
            ),
        )
        self.assertIn("<pacing_directive", prompt)
        self.assertIn("Choice-Pacing-HARD", prompt)
        self.assertIn("决定性行动", prompt)
        self.assertIn("四个选项不得全部停留在", prompt)

    def test_prompt_omits_pacing_block_when_empty(self) -> None:
        prompt = choice_generation_prompt(
            world=_custom_world(),
            session={"world_state": {"location": "广场"}},
            participant={"participant_id": "p_1"},
            events=[],
            pacing_directive="",
        )
        self.assertNotIn('<pacing_directive trust="plugin-authoritative">', prompt)

    def test_prompt_forbids_zero_action_options_unconditionally(self) -> None:
        """零行动选项禁令不依赖 pacing，任何情况下都必须出现。"""
        prompt = choice_generation_prompt(
            world=_custom_world(),
            session={"world_state": {"location": "广场"}},
            participant={"participant_id": "p_1"},
            events=[],
        )
        self.assertIn("禁止零行动选项", prompt)
        self.assertIn("只盘点物品", prompt)
        self.assertIn("不要写零行动选项", prompt)

    def test_prompt_allows_departure_to_known_destination(self) -> None:
        """当前场景约束不得退化为地点锁，旅行选项只承诺出发。"""
        prompt = choice_generation_prompt(
            world=_custom_world(),
            session={"world_state": {"location": "广场"}},
            participant={"participant_id": "p_1"},
            events=[],
        )
        self.assertIn("章节和当前场景都不是地点锁", prompt)
        self.assertIn("启程前往/返回某地", prompt)
        self.assertIn("不得预设平安抵达", prompt)

    def test_repair_prompt_forbids_zero_action_options(self) -> None:
        repair = choice_repair_prompt(
            '{"choices": []}',
            "结构校验失败",
            world=_custom_world(),
            participant={"participant_id": "p_1"},
        )
        self.assertIn("禁止零行动选项", repair)
        self.assertIn("不得保留", repair)

    def test_prompt_renders_ending_rule(self) -> None:
        """pacing 含 [Choice-Pacing-ENDING] 时，prompt 必须要求选项推进收尾。"""
        prompt = choice_generation_prompt(
            world=_custom_world(),
            session={"world_state": {"location": "警车上"}},
            participant={"participant_id": "p_1"},
            events=[],
            pacing_directive=(
                "[Choice-Pacing-ENDING] 剧情已到结局点，必须推进收尾。"
            ),
        )
        self.assertIn("[Choice-Pacing-ENDING]", prompt)
        self.assertIn("推进收尾", prompt)
        self.assertIn("不得停留在检查、确认、观望、整理或等待", prompt)


if __name__ == "__main__":
    unittest.main()
