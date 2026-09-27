"""时间线缝合 + 覆盖检查的测试。

这里锁的是**一个真实发生过的改编事故**：某卷是多周目结构，生成时把
「她怀疑主角并与主角动手」那一段以"属于另一条时间线"为由整段删掉了——
而它恰恰是全卷张力最强的部分。原作目录里明写着「二次轮回」「三度轮回」。

事故有两个成因，所以这里有两组测试：

1. **判据错了**——把"这条线被重置了"当成了不可改编的理由。正确的是
   "这个事件本身是否依赖主角能回溯"。→ ``TimelineMergeRuleTests``
2. **反思看不见遗漏**——断言由遍历草稿而来，被丢掉的剧情产出零条断言，
   永远进不了视野。→ ``CoverageReportTests`` / ``CoverageClaimsTests`` /
   ``OmissionGateTests``
"""

from __future__ import annotations

import asyncio
import unittest

from tavern.worldgen.models import (
    MergeAction,
    MergedBeat,
    ProposalChapter,
    Timeline,
    TimelineSegment,
)
from tavern.worldgen.reflect import coverage_claims
from tavern.worldgen.service import coverage_report
from tavern.worldgen.steps import validate_timeline


def _segment(seg_id: str, files: list[str], *, reset: bool = False) -> dict:
    return {
        "segment_id": seg_id,
        "label": f"第{seg_id}段",
        "kind": "loop_iteration" if reset else "main",
        "source_files": files,
        "summary": "概要",
        "ends_with_reset": reset,
        "key_beats": ["某事"],
    }


def _beat(beat_id: str, *, depends: bool = False, action: str = "keep",
          note: str = "", segments: list[str] | None = None) -> dict:
    return {
        "beat_id": beat_id,
        "label": f"情节{beat_id}",
        # 注意用 is None 判断：传空列表是要测"缺失"，不能被 or 兜回默认值
        "from_segments": ["tl_01"] if segments is None else list(segments),
        "depends_on_reset": depends,
        "merge_action": action,
        "adaptation_note": note,
    }


class TimelineMergeRuleTests(unittest.TestCase):
    """判据本身的规则：依赖回溯 → 不能原样保留。"""

    def _payload(self, **overrides) -> dict:
        base = {
            "structure": "loop",
            "player_role": {"replaces": [], "note": "操作者没有要求替换，玩家作为新加入者参与"},
            "segments": [_segment("tl_01", ["01.md"], reset=True)],
            "merged": [_beat("b01")],
        }
        base.update(overrides)
        return base

    def test_valid_timeline_passes(self) -> None:
        self.assertEqual([], validate_timeline(self._payload()))

    def test_merged_is_required(self) -> None:
        """只列 segments 不算完成——那还是"哪些线可用"的旧思路。"""
        problems = validate_timeline(self._payload(merged=[]))
        self.assertTrue(any("merged" in p for p in problems), msg=problems)

    def test_depends_on_reset_cannot_be_kept(self) -> None:
        """★ 核心规则：声明依赖回溯却选了 keep，必须拦下。

        这条一旦失守，就等于又回到了"按哪条线是正史取材"的老路上。
        """
        problems = validate_timeline(
            self._payload(merged=[_beat("b01", depends=True, action="keep")])
        )
        self.assertTrue(
            any("depends_on_reset" in p and "keep" in p for p in problems),
            msg=problems,
        )

    def test_adapt_requires_a_note(self) -> None:
        """改写却不说明怎么改 = "自圆其说"落空。"""
        problems = validate_timeline(
            self._payload(merged=[_beat("b01", depends=True, action="adapt")])
        )
        self.assertTrue(any("adaptation_note" in p for p in problems), msg=problems)

    def test_adapt_with_note_is_accepted(self) -> None:
        payload = self._payload(
            merged=[_beat("b01", depends=True, action="adapt",
                          note="原作里靠回溯预判；改为从现场痕迹推断")]
        )
        self.assertEqual([], validate_timeline(payload))

    def test_drop_is_always_allowed(self) -> None:
        payload = self._payload(
            merged=[_beat("b01", depends=True, action="drop")]
        )
        self.assertEqual([], validate_timeline(payload))

    def test_from_segments_must_exist(self) -> None:
        problems = validate_timeline(
            self._payload(merged=[_beat("b01", segments=["tl_99"])])
        )
        self.assertTrue(any("tl_99" in p for p in problems), msg=problems)

    def test_from_segments_is_required(self) -> None:
        problems = validate_timeline(
            self._payload(merged=[_beat("b01", segments=[])])
        )
        self.assertTrue(any("from_segments" in p for p in problems), msg=problems)

    def test_duplicate_beat_id_is_rejected(self) -> None:
        problems = validate_timeline(
            self._payload(merged=[_beat("b01"), _beat("b01")])
        )
        self.assertTrue(any("重复" in p for p in problems), msg=problems)

    def test_illegal_kind_and_action_are_rejected(self) -> None:
        payload = self._payload(
            segments=[_segment("tl_01", ["01.md"]) | {"kind": "nonsense"}],
            merged=[_beat("b01") | {"merge_action": "maybe"}],
        )
        problems = validate_timeline(payload)
        self.assertTrue(any("kind" in p for p in problems), msg=problems)
        self.assertTrue(any("merge_action" in p for p in problems), msg=problems)


class PlayerRoleTests(unittest.TestCase):
    """``player_role`` 必须声明——它是后面每一步的口径来源。

    真实事故：操作者写了「由玩家小队替换掉男主 486」，但这一步不产出任何
    可被后续步骤读取的结论，这条要求就只留在提示词里。划章节和建角色卡
    都看不见它，于是男主照样拿了一张 NPC 卡（``npc_subaru``）。
    """

    def _payload(self, **overrides) -> dict:
        base = {
            "structure": "linear",
            "player_role": {"replaces": ["菜月昴"], "note": "玩家小队顶替他的位置"},
            "segments": [_segment("tl_01", ["01.md"])],
            "merged": [_beat("b01")],
        }
        base.update(overrides)
        return base

    def test_player_role_is_required(self) -> None:
        payload = self._payload()
        payload.pop("player_role")
        problems = validate_timeline(payload)
        self.assertTrue(any("player_role" in p for p in problems), msg=problems)

    def test_problem_text_repeats_the_operator_requirement(self) -> None:
        """问题清单要把操作者的要求复述一遍。

        重试时 ``llm._repair_prompt`` 不重发原始 prompt——模型看不到要求，
        而"谁该被玩家取代"正是从要求里读出来的。不复述就只能靠被拒输出猜。
        """
        payload = self._payload()
        payload.pop("player_role")
        problems = validate_timeline(
            payload, requirements="由玩家小队替换掉男主486"
        )
        self.assertTrue(
            any("由玩家小队替换掉男主486" in p for p in problems), msg=problems
        )

    def test_replaces_must_be_a_list(self) -> None:
        """空数组是合法的（本卷不替换），但"忘了写"不行——那会让下游无从判断。"""
        problems = validate_timeline(
            self._payload(player_role={"replaces": "菜月昴", "note": "顶替"})
        )
        self.assertTrue(any("replaces" in p for p in problems), msg=problems)

    def test_note_is_required(self) -> None:
        problems = validate_timeline(
            self._payload(player_role={"replaces": [], "note": ""})
        )
        self.assertTrue(any("note" in p for p in problems), msg=problems)

    def test_empty_replaces_with_a_reason_is_accepted(self) -> None:
        problems = validate_timeline(
            self._payload(
                player_role={"replaces": [], "note": "操作者要求保留原作主角作为 NPC"}
            )
        )
        self.assertEqual([], problems, msg=problems)

    def test_named_replacement_is_accepted(self) -> None:
        self.assertEqual([], validate_timeline(self._payload()))

    def test_timeline_model_round_trips_player_role(self) -> None:
        """``player_role`` 要能落进 05_timeline.json 再读回来——面板与下游都靠它。"""
        timeline = Timeline.from_dict(self._payload())
        self.assertEqual(["菜月昴"], timeline.replaced_characters)
        self.assertEqual(
            {"replaces": ["菜月昴"], "note": "玩家小队顶替他的位置"},
            timeline.to_dict()["player_role"],
        )

    def test_old_timeline_without_player_role_still_loads(self) -> None:
        """旧作业的产物里没有这一项，回读不能炸（resume 要能接着跑）。"""
        timeline = Timeline.from_dict({"structure": "linear", "segments": [], "merged": []})
        self.assertEqual([], timeline.replaced_characters)


class CoverageReportTests(unittest.TestCase):
    """确定性覆盖闭合：原作有什么、草稿接住了什么，差集就是遗漏。"""

    def _timeline(self) -> Timeline:
        return Timeline(
            structure="loop",
            segments=[
                TimelineSegment("tl_01", "第一轮", source_files=["01.md"]),
                TimelineSegment("tl_02", "第二轮", source_files=["02.md"]),
            ],
            merged=[
                MergedBeat("b01", "与双子初见", ["tl_01"]),
                MergedBeat("b02", "与骑士对立", ["tl_02"],
                           depends_on_reset=True, merge_action=MergeAction.ADAPT,
                           adaptation_note="改为从现场痕迹推断"),
                MergedBeat("b03", "被丢弃的情节", ["tl_02"],
                           merge_action=MergeAction.DROP),
            ],
        )

    def _chapter(self, *files: str, beats: list[str] | None = None) -> ProposalChapter:
        return ProposalChapter(
            item_id="cp_1", chapter_id="ch_01_a", title="t",
            source_files=list(files), beats=list(beats or []),
        )

    def test_full_coverage_is_ok(self) -> None:
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md", "02.md", beats=["b01", "b02"])]
        proposal.unadapted = []
        report = coverage_report(
            proposal, timeline=self._timeline(), volume_files=["01.md", "02.md"]
        )
        self.assertTrue(report["ok"], msg=report)
        self.assertEqual([], report["missing_files"])
        self.assertEqual([], report["missing_beats"])

    def test_unclaimed_file_is_reported(self) -> None:
        """整篇没人取材 = 静默蒸发，必须点名。"""
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md", beats=["b01"])]
        proposal.unadapted = []
        report = coverage_report(
            proposal, timeline=self._timeline(), volume_files=["01.md", "02.md"]
        )
        self.assertFalse(report["ok"])
        self.assertEqual(["02.md"], report["missing_files"])

    def test_declaring_it_unadapted_counts_as_accounted_for(self) -> None:
        """在 unadapted 里点名放弃，也算"有交代"，不算遗漏。"""
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md", beats=["b01"])]
        proposal.unadapted = [{"source": "02.md", "reason": "这段依赖主角回溯"}]
        report = coverage_report(
            proposal, timeline=self._timeline(), volume_files=["01.md", "02.md"]
        )
        self.assertNotIn("02.md", report["missing_files"])

    def test_missing_beat_is_reported(self) -> None:
        """★ 这就是那次事故的形态：篇都取材了，但某条关键情节没有落点。"""
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md", "02.md", beats=["b01"])]
        proposal.unadapted = []
        report = coverage_report(
            proposal, timeline=self._timeline(), volume_files=["01.md", "02.md"]
        )
        self.assertFalse(report["ok"])
        missing = [b["beat_id"] for b in report["missing_beats"]]
        self.assertEqual(["b02"], missing)

    def test_dropped_beat_is_not_required(self) -> None:
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md", "02.md", beats=["b01", "b02"])]
        proposal.unadapted = []
        report = coverage_report(
            proposal, timeline=self._timeline(), volume_files=["01.md", "02.md"]
        )
        self.assertNotIn("b03", [b["beat_id"] for b in report["missing_beats"]])
        self.assertEqual(2, report["checked_beats"])

    def test_beat_declared_unadapted_is_not_missing(self) -> None:
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md", "02.md", beats=["b01"])]
        proposal.unadapted = [{"beat_id": "b02", "reason": "本世界没有回溯前提"}]
        report = coverage_report(
            proposal, timeline=self._timeline(), volume_files=["01.md", "02.md"]
        )
        self.assertEqual([], report["missing_beats"])

    def test_no_timeline_still_checks_files(self) -> None:
        proposal = type("P", (), {})()
        proposal.chapters = [self._chapter("01.md")]
        proposal.unadapted = []
        report = coverage_report(
            proposal, timeline=None, volume_files=["01.md", "02.md"]
        )
        self.assertEqual(["02.md"], report["missing_files"])
        self.assertEqual(0, report["checked_beats"])


class CoverageClaimsTests(unittest.TestCase):
    """覆盖断言是从**原作侧**造的——这是与一致性断言的根本区别。"""

    def _timeline(self) -> Timeline:
        return Timeline(
            structure="loop",
            merged=[
                MergedBeat("b01", "与双子初见", ["tl_01"]),
                MergedBeat("b02", "与骑士对立", ["tl_02"],
                           merge_action=MergeAction.ADAPT, adaptation_note="改写"),
                MergedBeat("b03", "已放弃", ["tl_02"],
                           merge_action=MergeAction.DROP),
            ],
        )

    def _world(self) -> dict:
        return {
            "rules": {
                "progress": {
                    "chapters": [
                        {"title": "第一章", "current_objective": "活下来",
                         "milestones": [{"label": "取得情报"}]}
                    ]
                }
            }
        }

    def test_one_claim_per_required_beat(self) -> None:
        claims = coverage_claims(self._world(), self._timeline())
        self.assertEqual(["cov001", "cov002"], [c.claim_id for c in claims])
        self.assertEqual(["b01", "b02"], [c.beat_id for c in claims])
        self.assertTrue(all(c.kind == "coverage" for c in claims))

    def test_dropped_beat_is_not_claimed(self) -> None:
        claims = coverage_claims(self._world(), self._timeline())
        self.assertNotIn("b03", [c.beat_id for c in claims])

    def test_claim_text_carries_the_draft_digest(self) -> None:
        """模型要拿草稿去对照，所以断言正文里必须带上草稿摘要。"""
        claims = coverage_claims(self._world(), self._timeline())
        self.assertIn("第一章", claims[0].text)
        self.assertIn("取得情报", claims[0].text)

    def test_no_timeline_yields_no_claims(self) -> None:
        self.assertEqual([], coverage_claims(self._world(), None))


class OmissionGateTests(unittest.TestCase):
    """反省发现遗漏 → 退回审批门；接受 → 直接交付，且不转死循环。"""

    def setUp(self) -> None:
        from tests.test_worldgen_service import ServiceHarness

        self._harness = ServiceHarness("run")
        self._harness.setUp()

    def tearDown(self) -> None:
        self._harness.tearDown()

    def _omitted_response(self, **_kwargs) -> dict:
        return {"findings": [
            {"claim_id": "cov001", "verdict": "omitted",
             "reason": "通篇找不到承接「与双子初见」的章节或里程碑",
             "suggestion": "补在第一章"},
        ]}

    async def _run_to_reflect(self, harness) -> str:
        """建作业 → 批准 → 一路跑到反省阶段结束。"""
        job_id = await harness._create()
        await harness.service.approve(job_id, submit=True, actor="test")
        task = harness.service._tasks.get(job_id)
        if task is not None:
            await task
        return job_id

    def test_omission_pushes_job_back_to_approval(self) -> None:
        from tavern.worldgen.models import JobState, PhaseId

        async def run() -> None:
            harness = self._harness
            # 注意：必须在跑到反省**之前**就把覆盖检查改成报遗漏
            harness.llm.overrides["worldgen_coverage"] = self._omitted_response()
            job_id = await self._run_to_reflect(harness)

            record = harness.store.load(job_id)
            self.assertIs(JobState.AWAITING_APPROVAL, record.state, msg=record.error)
            # 退回的是审批门（重新进入，好处理"补章节"），不是继承/生成
            self.assertEqual(PhaseId.COVERAGE_APPROVE.value, record.resume_from_phase)
            self.assertIn("关键剧情", record.message)
            self.assertIn("65_omissions.json", harness.store.list_artifacts(job_id))

            payload = harness.store.read_artifact_json(job_id, "65_omissions.json")
            self.assertEqual("b01", payload["omissions"][0]["beat_id"])

        asyncio.run(run())

    def test_accepting_omissions_delivers_without_regenerating(self) -> None:
        """接受遗漏 = 直接去体检交付。

        不能再跑一遍生成：既费 token，也会再次撞上同一批遗漏，转成死循环。
        """
        from tavern.worldgen.models import JobState

        async def run() -> None:
            harness = self._harness
            harness.llm.overrides["worldgen_coverage"] = self._omitted_response()
            job_id = await self._run_to_reflect(harness)

            calls_before = list(harness.llm.calls)
            await harness.service.approve(
                job_id, submit=True, actor="test", accept_omissions=True
            )
            task = harness.service._tasks.get(job_id)
            if task is not None:
                await task

            record = harness.store.load(job_id)
            self.assertIs(JobState.SUCCEEDED, record.state, msg=record.error)
            self.assertTrue(record.outputs)
            # 接受之后**一次模型都不再调**——草稿已是成品，直接去体检
            self.assertEqual(calls_before, harness.llm.calls)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
