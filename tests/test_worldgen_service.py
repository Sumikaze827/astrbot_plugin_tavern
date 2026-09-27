"""世界包生成服务的端到端集成测试（用假模型，不联网、不花额度）。

覆盖整条链路：

    intake → sourcing → checkpoint_extract → ⏸审批门 → inherit
          → generate → reflect → gate → emit

重点验证三件事：

1. **审批门真的会停住**——作业走到 ``checkpoint_approve`` 后任务退出、
   ``job.json`` 落盘为 ``awaiting_approval``；提交后才继续。
2. **交付产物真的能导入**——最终两个文件必须过 lint 与 preflight 两道闸门。
3. **驳回会触发局部重写**，且不影响其它已批准的条目。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.worldgen.models import JobState, PhaseId
from tavern.worldgen.service import WorldgenService
from tavern.worldgen.store import JobStore

ATTR_KEYS = ["strength", "agility", "vitality", "intellect", "willpower", "perception", "charisma"]
BASE_SETS = [
    [11, 9, 10, 4, 6, 6, 4], [6, 12, 9, 4, 6, 8, 5], [5, 6, 7, 11, 8, 8, 5],
    [6, 7, 7, 12, 6, 8, 4], [4, 6, 6, 10, 7, 10, 7], [4, 5, 6, 9, 8, 7, 11],
    [5, 8, 6, 6, 7, 7, 11], [4, 11, 5, 8, 6, 10, 6],
]

#: ``_chapter()`` 备了几章的返回数据（逐章调用要各给各的）
CHAPTER_PAYLOAD_COUNT = 2

README = "## 第二章　『测试之卷』\n\n- [01　『开端』](01.md)\n- [02　『宅邸』](02.md)\n"
DOC1 = "# 『开端』\n\n拉姆与雷姆站在走廊尽头，她们是罗兹瓦尔宅邸的双胞胎女仆。\n"
DOC2 = "# 『宅邸』\n\n尤里乌斯是近卫骑士团的骑士，他在王选前就与昴敌对。\n"


class FakeLLM:
    """按 ``request_type`` 返回预设 payload 的假模型。

    ``extract_chapter`` 是**逐章**调的，必须按调用次序逐章返回——
    否则每章都拿到第一章的数据，覆盖检查会（正确地）报"第 2 篇没人取材"。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        #: 每次调用的 (request_type, prompt)。用来断言"要求有没有真的喂到这一步"——
        #: 只看返回值看不出提示词里少了一个块。
        self.prompts: list[tuple[str, str]] = []
        self.overrides: dict[str, dict] = {}
        self._counts: dict[str, int] = {}

    def prompt_for(self, request_type: str) -> str:
        for name, prompt in self.prompts:
            if name == request_type:
                return prompt
        raise AssertionError(f"没有调用过 {request_type}")

    def _payload_for(self, request_type: str) -> dict:
        if request_type in self.overrides:
            return self.overrides[request_type]
        # 逐章返回不同的章节数据（延后取，_chapter 在本类之后才定义）
        if request_type == "worldgen_extract_chapter":
            index = self._counts.get(request_type, 0)
            return _chapter(min(index, CHAPTER_PAYLOAD_COUNT - 1))
        return _RESPONSES[request_type]()

    async def generate_json(self, *, system_prompt, prompt, validate=None, request_type="", **kwargs):
        self.calls.append(request_type)
        self.prompts.append((request_type, prompt))
        payload = self._payload_for(request_type)
        self._counts[request_type] = self._counts.get(request_type, 0) + 1
        problems = list(validate(payload)) if validate else []
        result = SimpleNamespace(
            provider_id="fake", attempts=1, input_chars=len(prompt), output_chars=100, usage={}
        )
        return payload, result, problems


def _timeline() -> dict:
    """两条线缝成一条：第二段里「争执」那条不依赖回溯，必须并进来。

    这正是实测踩过的坑的缩小版——被重置的那条线里装着最激烈的冲突，
    不能因为"它被重置了"就整段丢掉。
    """
    return {
        "structure": "loop",
        "note": "两条时间线，第二段以死亡收场",
        "player_role": {"replaces": [], "note": "操作者未要求替换任何角色"},
        "segments": [
            {"segment_id": "tl_01", "label": "第一轮", "kind": "loop_iteration",
             "source_files": ["01.md"], "summary": "初次接触", "ends_with_reset": True,
             "key_beats": ["与双子初见"]},
            {"segment_id": "tl_02", "label": "第二轮", "kind": "main",
             "source_files": ["02.md"], "summary": "骑士登场", "ends_with_reset": False,
             "key_beats": ["与骑士对立"]},
        ],
        "merged": [
            {"beat_id": "b01", "label": "与双子初见", "from_segments": ["tl_01"],
             "depends_on_reset": False, "merge_action": "keep"},
            {"beat_id": "b02", "label": "与骑士对立", "from_segments": ["tl_02"],
             "depends_on_reset": True, "merge_action": "adapt",
             "adaptation_note": "原作里靠回溯预判；改为从现场痕迹推断"},
        ],
        "dropped": [],
        "coherence_notes": [],
    }


def _arc_map() -> dict:
    return {
        "arc_title": "第二章　『测试之卷』",
        "premise": "一行人住进宅邸，四天之内必须活下来。",
        "candidate_chapters": [
            {"order": 1, "title": "陌生的天花板", "source_files": ["01.md"],
             "beats": ["b01"], "why": "开场"},
            {"order": 2, "title": "宅邸与骑士", "source_files": ["02.md"],
             "beats": ["b02"], "why": "展开"},
        ],
        "unadapted": [{"source": "原作的三周目循环", "reason": "本世界玩家不具备回归能力"}],
    }


def _chapter(index: int) -> dict:
    titles = ["陌生的天花板", "宅邸与骑士"]
    return {
        "chapter": {
            "title": titles[index],
            "subtitle": "测试副标题",
            "source_files": [f"0{index + 1}.md"],
            "beats": [f"b0{index + 1}"],
            "rationale": "测试",
            "min_turns": 1,
            "max_turns": 10,
            "current_objective": "确认自己身在何处并找到愿意回应的人",
            "pacing_directive": "若无人回应则局势逐步收紧，但不得封锁退路",
            "hook_pool": ["走廊尽头的脚步声"],
            "milestones": [
                {
                    "label": f"第{index}章：队伍取得可行动的确切情报并已互相传达",
                    "evidence_required": [{"type": "clue_keyword_any", "match": ["确认位置", "目击证词"]}],
                    "source": {"rel_path": f"0{index + 1}.md", "line_start": 1, "line_end": 3,
                               "quote": "拉姆与雷姆站在走廊尽头"},
                }
            ],
            "key_npcs": [{"ref": "npc_ram", "role": "冷面女仆，负责指路"}],
        }
    }


def _cast() -> dict:
    return {
        "npcs": [
            {
                "slug": "npc_ram",
                "name": "拉姆",
                "identity": "罗兹瓦尔宅邸的双胞胎女仆之一",
                "appearance": "粉色短发，前刘海盖住右眼",
                "personality": "直率、嘴硬",
                "public_background": "宅邸的女仆",
                "location": "主楼走廊",
                "capabilities": ["清扫", "对外交涉"],
                "limitations": ["厨艺弱于妹妹", "正面战斗能力有限"],
                "prompt": "她想要维持宅邸的秩序，不会主动透露主人的私事。",
                "source": {"rel_path": "01.md", "line_start": 1, "line_end": 3,
                           "quote": "拉姆与雷姆站在走廊尽头"},
            }
        ]
    }


def _card() -> dict:
    return {
        "professions": [
            {
                "id": f"prof_{i}",
                "name": f"职业{i}",
                "description": "擅长若干事务，短板明确。",
                "base_attributes": dict(zip(ATTR_KEYS, values)),
            }
            for i, values in enumerate(BASE_SETS)
        ],
        "attribute_labels": {k: k for k in ATTR_KEYS},
    }


def _prose() -> dict:
    return {
        "opening_scene": "你在陌生的房间里醒来，走廊望不到头，两扇门在你面前。",
        "opening_choices": [
            {"key": "A", "text": "起身查看房间", "risk": "safe"},
            {"key": "B", "text": "敲门问有没有人", "risk": "safe"},
            {"key": "C", "text": "检查随身物品", "risk": "safe"},
            {"key": "D", "text": "全队一起下楼", "risk": "safe", "collective": True},
        ],
        "system_prompt": "本世界的稳定规律：不存在死亡回归，玩家的每一次选择都会留下后果。",
        "chapters": [{"chapter_id": "ch_01_unknown_ceiling", "pacing_directive": "局势逐步收紧"}],
    }


def _reflect() -> dict:
    return {
        "findings": [
            {"claim_id": "c001", "verdict": "supported", "severity": "info",
             "reason": "与原文一致"},
            {"claim_id": "c002", "verdict": "contradicted", "severity": "blocking",
             "reason": "原文并未如此描述",
             "source": {"rel_path": "01.md", "line_start": 999, "line_end": 999,
                        "quote": "这段原文其实并不存在"}},
        ]
    }


def _coverage_all_covered() -> dict:
    """默认：所有必现情节都有落点。要测遗漏时用 overrides 覆盖它。"""
    return {"findings": [
        {"claim_id": "cov001", "verdict": "covered",
         "reason": "由「第一章：陌生的天花板」的里程碑承接"},
    ]}


_RESPONSES = {
    "worldgen_timeline": _timeline,
    "worldgen_arc_map": _arc_map,
    "worldgen_extract_chapter": lambda: _chapter(0),
    "worldgen_cast": _cast,
    "worldgen_card": _card,
    "worldgen_prose": _prose,
    "worldgen_reflect": _reflect,
    "worldgen_coverage": _coverage_all_covered,
}


class ServiceHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.corpus = self.root / "corpus"
        volume = self.corpus / "chapter020"
        volume.mkdir(parents=True)
        (volume / "README.md").write_text(README, encoding="utf-8")
        (volume / "01.md").write_text(DOC1, encoding="utf-8")
        (volume / "02.md").write_text(DOC2, encoding="utf-8")
        self.outputs = self.root / "worlds"

        self.store = JobStore(root=self.root / "jobs")
        self.service = WorldgenService(
            context=SimpleNamespace(),
            database=SimpleNamespace(),
            broker=SimpleNamespace(publish=self._publish),
            store=self.store,
            plugin_config=SimpleNamespace(),
            corpus_root=self.corpus,
            index_dir=self.root / "index",
            output_dir=self.outputs,
        )
        self.events: list[dict] = []
        self.llm = FakeLLM()
        self.service._llm = self.llm

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _publish(self, event) -> None:
        self.events.append(dict(event))

    async def _drain(self, job_id: str) -> None:
        """等待当前挂起的后台任务结束（审批门处会自然返回）。"""
        for _ in range(10):
            task = self.service._tasks.get(job_id)
            if task is None or task.done():
                return
            await task

    async def _create(self, **overrides) -> str:
        request = {
            "kind": "adaptation",
            "corpus_root": str(self.corpus),
            "volume_ref": "chapter020",
            "slug": "demo-arc",
            "name": "测试之卷",
            "description": "用于集成测试的世界。",
        }
        request.update(overrides)
        record = await self.service.create(request, actor="test")
        await self._drain(record.job_id)
        return record.job_id


class GateAndParkTests(ServiceHarness):
    def test_job_parks_at_approval_gate(self) -> None:
        async def run() -> None:
            job_id = await self._create()
            record = self.store.load(job_id)
            self.assertIs(JobState.AWAITING_APPROVAL, record.state)
            self.assertIs(PhaseId.CHECKPOINT_APPROVE, record.phase)
            # 复位到**当前**阶段而不是下一阶段：提交后要重新进入审批阶段处理
            # 驳回队列，记成 inherit 会让被驳回的条目被静默跳过。
            self.assertEqual(PhaseId.CHECKPOINT_APPROVE.value, record.resume_from_phase)
            # 提案已落盘，可以直接给面板看
            proposal = self.store.read_artifact_json(job_id, "10_checkpoints.draft.json")
            self.assertEqual(2, len(proposal["chapters"]))
            self.assertTrue(self.store.has_artifact(job_id, "11_checkpoints.approved.json"))

        asyncio.run(run())

    def test_approved_job_reaches_delivery(self) -> None:
        async def run() -> None:
            job_id = await self._create()
            await self.service.approve(job_id, submit=True, actor="test")
            await self._drain(job_id)

            record = self.store.load(job_id)
            self.assertIs(JobState.SUCCEEDED, record.state, msg=record.error)
            self.assertTrue(record.outputs)

            world_path = Path(record.outputs["world"])
            npc_path = Path(record.outputs["npcs"])
            self.assertTrue(world_path.is_file())
            self.assertTrue(npc_path.is_file())

            world = json.loads(world_path.read_text(encoding="utf-8"))
            # 结构由 Python 生成，指针必须闭合
            chapters = world["rules"]["progress"]["chapters"]
            ids = [c["id"] for c in chapters]
            for chapter in chapters[:-1]:
                self.assertIn(chapter["next_chapter_id"], ids)
            self.assertNotIn("next_chapter_id", chapters[-1])
            self.assertEqual(
                sum(len(c["milestones"]) for c in chapters),
                world["rules"]["progress"]["total_milestones"],
            )
            self.assertEqual(8, len(world["rules"]["character_card"]["profession_presets"]))

            npcs = json.loads(npc_path.read_text(encoding="utf-8"))
            self.assertEqual(2, npcs["template_version"])
            self.assertEqual(world["slug"], npcs["world_slug"])

        asyncio.run(run())

    def test_gate_passed_on_delivery(self) -> None:
        async def run() -> None:
            job_id = await self._create()
            await self.service.approve(job_id, submit=True, actor="test")
            await self._drain(job_id)
            report = self.store.read_artifact_json(job_id, "70_lint.json")
            self.assertTrue(report.get("ok"), msg=report)
            self.assertEqual(0, report["lint"]["errors"], msg=report["lint"]["issues"])

        asyncio.run(run())


class RejectionTests(ServiceHarness):
    def test_rejected_milestone_triggers_scoped_regen(self) -> None:
        """驳回一条只重写一条，其它已批准的条目不受影响。"""

        async def run() -> None:
            job_id = await self._create()
            proposal = self.store.read_artifact_json(job_id, "10_checkpoints.draft.json")
            first = proposal["chapters"][0]
            milestone_item = first["milestones"][0]["item_id"]
            chapter_item = first["item_id"]

            self.llm.overrides["worldgen_regen_milestone"] = {
                "milestone": {
                    "label": "重写后的第1章完成结果，写清了可替代途径",
                    "evidence_required": [{"type": "clue_keyword_any", "match": ["新的信号"]}],
                    "source": {"rel_path": "01.md", "line_start": 1, "line_end": 3,
                               "quote": "拉姆与雷姆站在走廊尽头"},
                }
            }
            # 驳回第一章的那条里程碑，其余保持批准，然后提交——提交才会触发重写
            await self.service.approve(
                job_id,
                decisions={milestone_item: {"status": "rejected", "note": "这条该在第 2 章"}},
                actor="test",
                submit=True,
            )
            await self._drain(job_id)

            self.assertIn("worldgen_regen_milestone", self.llm.calls)

            refreshed = self.store.read_artifact_json(job_id, "10_checkpoints.draft.json")
            chapter = next(c for c in refreshed["chapters"] if c["item_id"] == chapter_item)
            rewritten = next(
                m for m in chapter["milestones"] if m["item_id"] == milestone_item
            )
            self.assertIn("重写后", rewritten["label"])
            # id 保持不变——重写的是内容，不是身份
            self.assertEqual(
                first["milestones"][0]["milestone_id"], rewritten["milestone_id"]
            )
            # 重写后该条回到待审批，且需要重新提交
            decisions = self.store.read_artifact_json(job_id, "11_checkpoints.approved.json")
            self.assertNotIn(milestone_item, decisions["decisions"])
            self.assertFalse(decisions["submitted"])
            # 作业回到等待审批，没有被静默推进到生成阶段
            loaded = self.store.load(job_id)
            self.assertIs(JobState.AWAITING_APPROVAL, loaded.state)
            self.assertEqual("checkpoint_approve", loaded.resume_from_phase)

        asyncio.run(run())


class PlayerReplacementTests(ServiceHarness):
    """「由玩家小队替换掉男主」这条要求，要一路走到交付产物里。

    真实事故：操作者写了这条要求，改编出来的 NPC 包里却躺着一张 ``npc_subaru``
    （菜月昴）——要求只喂给了时间线与卷级地图两步，建角色卡那一步根本看不到它。
    这里端到端验四件事：时间线声明 → 提示词带上点名的人 → 名录里没有他 →
    要求真的进了后续每一步的 prompt。
    """

    def _replace_protagonist(self) -> None:
        """让假的模型扮演一个"照原文给男主建卡"的 CAST。"""
        timeline = _timeline()
        timeline["player_role"] = {
            "replaces": ["菜月昴"],
            "note": "操作者要求由玩家小队顶替男主的位置",
        }
        self.llm.overrides["worldgen_timeline"] = timeline

        cast = _cast()
        cast["npcs"].append(
            {
                "slug": "npc_subaru",
                "name": "菜月昴",
                "identity": "被卷入异世界的少年，本卷的视角人物",
                "appearance": "黑色短发，运动服",
                "personality": "逞强、爱面子",
                "public_background": "在王都引起骚动的外来者",
                "location": "王都",
                "capabilities": ["交涉", "异世界的常识外知识"],
                "limitations": ["没有战斗力", "不被贵族信任"],
                "prompt": "他想证明自己有用。",
                "source": {"rel_path": "02.md", "line_start": 1, "line_end": 3,
                           "quote": "尤里乌斯是近卫骑士团的骑士"},
            }
        )
        self.llm.overrides["worldgen_cast"] = cast

    def test_replaced_protagonist_is_kept_out_of_the_npc_package(self) -> None:
        async def run() -> None:
            self._replace_protagonist()
            job_id = await self._create()
            await self.service.approve(job_id, submit=True, actor="test")
            await self._drain(job_id)

            record = self.store.load(job_id)
            self.assertIs(JobState.SUCCEEDED, record.state, msg=record.error)
            # 被剔除这件事必须记在作业上——面板的交付产物区要显示它
            self.assertEqual(
                ["菜月昴"], [item["name"] for item in record.request["_replaced_npcs"]]
            )

            npcs = json.loads(Path(record.outputs["npcs"]).read_text(encoding="utf-8"))
            slugs = [item["slug"] for item in npcs["items"]]
            self.assertNotIn("npc_subaru", slugs)
            self.assertIn("npc_ram", slugs)

        asyncio.run(run())

    def test_replacement_reaches_the_later_steps(self) -> None:
        """提示词里必须点名——只看产物形状看不出"要求有没有喂进去"。"""

        async def run() -> None:
            self._replace_protagonist()
            await self._create()
            for request_type in (
                "worldgen_arc_map",
                "worldgen_extract_chapter",
                "worldgen_cast",
            ):
                prompt = self.llm.prompt_for(request_type)
                self.assertIn(
                    "<player_position>", prompt, msg=f"{request_type} 没收到玩家位置"
                )
                self.assertIn("菜月昴", prompt, msg=f"{request_type} 没点名被取代的人")

        asyncio.run(run())

    def test_prose_also_knows_who_was_replaced(self) -> None:
        async def run() -> None:
            self._replace_protagonist()
            job_id = await self._create()
            await self.service.approve(job_id, submit=True, actor="test")
            await self._drain(job_id)
            self.assertIn("菜月昴", self.llm.prompt_for("worldgen_prose"))

        asyncio.run(run())

    def test_without_a_replacement_the_protagonist_stays(self) -> None:
        """没有要求替换时不能顺手删人——戏份多不等于"该由玩家取代"。"""

        async def run() -> None:
            timeline = _timeline()
            timeline["player_role"] = {"replaces": [], "note": "操作者未要求替换"}
            self.llm.overrides["worldgen_timeline"] = timeline
            cast = _cast()
            cast["npcs"].append(
                {"slug": "npc_subaru", "name": "菜月昴", "prompt": "视角人物。"}
            )
            self.llm.overrides["worldgen_cast"] = cast

            job_id = await self._create()
            record = self.store.load(job_id)
            self.assertNotIn("_replaced_npcs", record.request)
            self.assertEqual(
                ["npc_ram", "npc_subaru"],
                [item["slug"] for item in record.request["_npc_details"]],
            )

        asyncio.run(run())


class OriginModeTests(ServiceHarness):
    def test_origin_mode_skips_corpus_and_parks(self) -> None:
        async def run() -> None:
            self.llm.overrides["worldgen_origin_plan"] = {
                "premise": "一座会在夜里改变结构的宅邸。",
                "chapters": [
                    {
                        "title": "第一夜",
                        "subtitle": "陌生",
                        "min_turns": 1,
                        "max_turns": 8,
                        "current_objective": "在结构改变前找到同伴",
                        "pacing_directive": "走廊每夜改变一次",
                        "hook_pool": ["墙上的刻痕"],
                        "milestones": [
                            {"label": "找到至少一名同伴并确认其身份",
                             "evidence_required": [{"type": "clue_keyword_any", "match": ["确认身份"]}]}
                        ],
                        "key_npcs": [{"ref": "npc_guide", "name": "引路人", "role": "指路"}],
                    }
                ],
            }
            record = await self.service.create(
                {"kind": "origin", "concept": "一座夜里会改变结构的宅邸", "slug": "demo-origin",
                 "name": "夜宅"},
                actor="test",
            )
            await self._drain(record.job_id)
            loaded = self.store.load(record.job_id)
            self.assertIs(JobState.AWAITING_APPROVAL, loaded.state, msg=loaded.error)
            # 原创路径不应触碰语料
            self.assertNotIn("worldgen_arc_map", self.llm.calls)

        asyncio.run(run())


class ProgressTests(ServiceHarness):
    def test_progress_events_are_published_per_phase(self) -> None:
        async def run() -> None:
            job_id = await self._create()
            kinds = {e.get("phase") for e in self.events if e.get("type") == "worldgen"}
            self.assertIn("intake", kinds)
            self.assertIn("checkpoint_extract", kinds)
            self.assertIn("checkpoint_approve", kinds)
            # 审批门也要推一条，否则界面看不出它在等人
            self.assertTrue(any(e.get("state") == "awaiting_approval" for e in self.events))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
