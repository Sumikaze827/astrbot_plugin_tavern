"""世界包确定性校验（tavern.worldgen.lint）的回归测试。

覆盖三类内容：

1. **真实缺陷必须报 error**——悬空 next_chapter_id、exits_when 引用未定义里程碑、
   total_milestones 计数不符、id 重复、职业预算不符、多自由文本项、全队选项超限、
   key_npcs 引用不存在的 NPC、章节成环。这些是让世界包真正跑不起来的问题。

2. **约定与风险只报 warning**——缺 exits_when、缺 min_turns、里程碑 id 未用
   m_XX_YY 约定、state 预写生死地点、开场过长。前两条特意锁定「引擎有容错」这一
   事实，防止有人凭文档把定级改回 error：
   - 缺 exits_when：引擎回退为「本章全部里程碑完成才切章」（engine.py:1262-1270）。
   - 里程碑 id：引擎按精确字符串匹配（engine.py:1103），不解析 m_XX_YY 数字段。

3. **真实世界包回归**——worlds/rezero-first-day-no-return.json（基准包）必须零错误；
   worlds/rezero-zhaidi-changye.json 必须报出已知的 total_milestones 计数缺陷。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tavern.worldgen.lint import lint_world_package

WORLDS_DIR = Path(__file__).resolve().parents[1] / "worlds"

#: 与 worlds/rezero-first-day-no-return.json 一致的 7 属性，合计 50。
BASE_ATTRIBUTES = {
    "strength": 11,
    "agility": 9,
    "vitality": 10,
    "intellect": 4,
    "willpower": 6,
    "perception": 6,
    "charisma": 4,
}


def _milestone(mid: str, label: str = "队伍取得可行动的确切情报并已互相传达") -> dict:
    return {
        "id": mid,
        "label": label,
        "evidence_required": [
            {"type": "clue_keyword_any", "match": ["拿到名单", "确认位置", "目击证词"]}
        ],
    }


def _chapter(
    cid: str,
    *,
    next_id: str | None = None,
    milestone_ids: tuple[str, ...] = ("m_01_01_trust",),
    exits: list[str] | None = None,
    min_turns: int | None = 1,
    max_turns: int = 8,
    key_npcs: list[dict] | None = None,
) -> dict:
    chapter: dict = {
        "id": cid,
        "title": "测试章节",
        "subtitle": "测试副标题",
        "max_turns": max_turns,
        "narrative_length_band": "standard",
        "current_objective": "测试目标",
        "pacing_directive": "测试节奏指令",
        "hook_pool": ["钩子一"],
        "key_npcs": key_npcs if key_npcs is not None else [{"ref": "npc_a", "role": "测试角色"}],
        "milestones": [_milestone(m) for m in milestone_ids],
        "exits_when": {"all_milestones": list(exits) if exits is not None else list(milestone_ids)},
    }
    if min_turns is not None:
        chapter["min_turns"] = min_turns
    if next_id is not None:
        chapter["next_chapter_id"] = next_id
    return chapter


def _card() -> dict:
    professions = [
        {
            "id": f"prof_{i}",
            "name": f"职业{i}",
            "description": "测试职业",
            "base_attributes": dict(BASE_ATTRIBUTES),
        }
        for i in range(6)
    ]
    return {
        "version": 1,
        "fields": [
            {"key": "name", "label": "角色姓名", "type": "text", "required": True, "max_chars": 12},
            {"key": "code", "label": "代号", "type": "text", "required": True, "max_chars": 12},
            {"key": "profession", "label": "职业", "type": "preset_select", "required": True},
            {
                "key": "primary_attribute",
                "label": "主属性",
                "type": "preset_select",
                "required": True,
            },
            {
                "key": "secondary_attribute",
                "label": "副属性",
                "type": "preset_select",
                "required": True,
            },
            {"key": "supplement", "label": "补充说明", "type": "text", "required": False, "max_chars": 300},
        ],
        "stats": {
            "mode": "preset",
            "base_budget": 50,
            "bonus_budget": 10,
            "effective_total": 60,
            "attributes": [
                {"key": key, "label": key, "minimum": 0, "maximum": 20, "default": 5}
                for key in BASE_ATTRIBUTES
            ],
        },
        "profession_presets": professions,
    }


def _world(**overrides) -> dict:
    """构造一个可通过全部检查的最小 v5 世界包。"""
    world: dict = {
        "world_schema_version": 5,
        "minimum_plugin_version": "v0.12.0",
        "world_content_version": "1.0.0",
        "protocol": {"core_version": 5, "features": {}},
        "slug": "test-world",
        "name": "测试世界",
        "description": "用于单元测试的最小世界包。",
        "system_prompt": "测试系统提示词。",
        "opening_scene": "你在一间陌生的房间里醒来。",
        "rules": {
            "character_card": _card(),
            "opening_choices": [
                {"key": "A", "text": "起身查看房间", "risk": "safe", "collective": False},
                {"key": "B", "text": "先喊一声有没有人", "risk": "safe", "collective": False},
                {"key": "C", "text": "检查随身物品", "risk": "safe", "collective": False},
                {"key": "D", "text": "全队一起下楼", "risk": "safe", "collective": True},
            ],
            "progress": {
                "chapter": "第一章",
                "current_chapter_id": "ch_01_start",
                "current_objective": "测试目标",
                "completed_milestones": 0,
                "total_milestones": 1,
                "chapters": [
                    _chapter("ch_01_start", milestone_ids=("m_01_01_trust",)),
                ],
            },
        },
        "initial_state": {"location": "测试地点", "summary": "测试摘要"},
    }
    world.update(overrides)
    return world


def _codes(report: dict, level: str | None = None) -> list[str]:
    return [
        item["code"]
        for item in report["issues"]
        if level is None or item["level"] == level
    ]


class CleanPackageTests(unittest.TestCase):
    def test_minimal_world_is_clean(self) -> None:
        report = lint_world_package(_world())
        self.assertTrue(report["ok"], msg=report["issues"])
        self.assertEqual(0, report["errors"])

    def test_non_mapping_input_is_rejected(self) -> None:
        report = lint_world_package(["not", "a", "world"])  # type: ignore[arg-type]
        self.assertFalse(report["ok"])
        self.assertIn("package.not_object", _codes(report))


class ChapterGraphTests(unittest.TestCase):
    def test_dangling_next_chapter_is_error(self) -> None:
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start", next_id="ch_02_missing"),
        ]
        report = lint_world_package(world)
        self.assertIn("chapter.next_dangling", _codes(report, "error"))

    def test_exits_when_referencing_undefined_milestone_is_error(self) -> None:
        """这类门槛永远无法满足——章节会真的卡死，必须 error。"""
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter(
                "ch_01_start",
                milestone_ids=("m_01_01_trust",),
                exits=["m_01_09_never_defined"],
            ),
        ]
        report = lint_world_package(world)
        self.assertIn("chapter.exits_when_undefined_milestone", _codes(report, "error"))

    def test_missing_exits_when_is_warning_not_error(self) -> None:
        """锁定引擎容错事实：缺 exits_when 会回退到本章全部里程碑，不会卡死。

        见 engine.py:1262-1270。若有人把这条改回 error，本测试会失败。
        """
        world = _world()
        chapter = _chapter("ch_01_start")
        chapter.pop("exits_when")
        world["rules"]["progress"]["chapters"] = [chapter]
        report = lint_world_package(world)
        self.assertIn("chapter.exits_when_missing", _codes(report, "warning"))
        self.assertNotIn("chapter.exits_when_missing", _codes(report, "error"))

    def test_duplicate_chapter_id_is_error(self) -> None:
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start", next_id="ch_01_start", milestone_ids=("m_01_01_trust",)),
            _chapter("ch_01_start", milestone_ids=("m_01_02_next",)),
        ]
        report = lint_world_package(world)
        self.assertIn("chapter.id_duplicate", _codes(report, "error"))

    def test_chapter_cycle_is_error(self) -> None:
        """从 current_chapter_id 出发走不出去——必须报环。

        注意 current_chapter_id 必须落在环内，否则命中的是
        current_chapter_dangling / no_terminal 这两条更外层的问题。
        """
        world = _world()
        world["rules"]["progress"]["current_chapter_id"] = "ch_01_a"
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_a", next_id="ch_02_b"),
            _chapter("ch_02_b", next_id="ch_01_a"),
        ]
        report = lint_world_package(world)
        errors = _codes(report, "error")
        self.assertIn("chapter.cycle", errors)
        self.assertIn("chapter.no_terminal", errors)

    def test_unreachable_chapter_is_warning(self) -> None:
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start"),
            _chapter("ch_02_orphan"),
        ]
        report = lint_world_package(world)
        self.assertIn("chapter.unreachable", _codes(report, "warning"))

    def test_inverted_turn_range_is_error(self) -> None:
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start", min_turns=10, max_turns=3),
        ]
        report = lint_world_package(world)
        self.assertIn("chapter.turn_range_inverted", _codes(report, "error"))

    def test_missing_min_turns_is_warning_not_error(self) -> None:
        """省略 min_turns 时引擎按 1 回合处理（更宽松），不是缺陷。"""
        world = _world()
        world["rules"]["progress"]["chapters"] = [_chapter("ch_01_start", min_turns=None)]
        report = lint_world_package(world)
        self.assertIn("chapter.min_turns_missing", _codes(report, "warning"))
        self.assertNotIn("chapter.min_turns_missing", _codes(report, "error"))


class MilestoneTests(unittest.TestCase):
    def test_total_milestones_mismatch_is_error(self) -> None:
        world = _world()
        world["rules"]["progress"]["total_milestones"] = 9
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start", milestone_ids=("m_01_01_trust", "m_01_02_anomaly")),
        ]
        report = lint_world_package(world)
        self.assertIn("progress.total_milestones_mismatch", _codes(report, "error"))

    def test_duplicate_milestone_id_across_chapters_is_error(self) -> None:
        world = _world()
        world["rules"]["progress"]["total_milestones"] = 2
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start", next_id="ch_02_next", milestone_ids=("m_01_01_trust",)),
            _chapter("ch_02_next", milestone_ids=("m_01_01_trust",)),
        ]
        report = lint_world_package(world)
        self.assertIn("milestone.id_duplicate", _codes(report, "error"))

    def test_non_conventional_milestone_id_is_warning_only(self) -> None:
        """锁定引擎事实：里程碑按精确字符串匹配，不解析 m_XX_YY 数字段。

        见 engine.py:1103 _milestone_ledger_completed。若有人把这条改成 error，
        会让 m_00_bridge_glyph 这类合法旧包被误判为坏包。
        """
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter("ch_01_start", milestone_ids=("m_00_bridge_glyph",)),
        ]
        report = lint_world_package(world)
        self.assertIn("milestone.id_format", _codes(report, "warning"))
        self.assertNotIn("milestone.id_format", _codes(report, "error"))

    def test_vague_evidence_words_are_warned(self) -> None:
        world = _world()
        chapter = _chapter("ch_01_start")
        chapter["milestones"] = [
            {
                "id": "m_01_01_trust",
                "label": "队伍取得了确切情报并已传达",
                "evidence_required": [{"type": "clue_keyword_any", "match": ["调查"]}],
            }
        ]
        world["rules"]["progress"]["chapters"] = [chapter]
        report = lint_world_package(world)
        self.assertIn("milestone.evidence_vague", _codes(report, "warning"))


class NpcRefTests(unittest.TestCase):
    def test_missing_npc_slug_is_error(self) -> None:
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter(
                "ch_01_start",
                key_npcs=[{"ref": "npc_ghost", "role": "不存在的人"}],
            )
        ]
        report = lint_world_package(world, npc_slugs=["npc_a", "npc_b"])
        self.assertIn("npc.ref_not_found", _codes(report, "error"))

    def test_without_npc_package_refs_are_unverified(self) -> None:
        report = lint_world_package(_world())
        self.assertIn("npc.refs_unverified", _codes(report, "info"))

    def test_state_clobber_risk_is_warning(self) -> None:
        world = _world()
        world["rules"]["progress"]["chapters"] = [
            _chapter(
                "ch_01_start",
                key_npcs=[{"ref": "npc_a", "role": "测试", "state": {"location": "宅邸", "alive": True}}],
            )
        ]
        report = lint_world_package(world, npc_slugs=["npc_a"])
        self.assertIn("npc.state_clobber_risk", _codes(report, "warning"))


class CardTests(unittest.TestCase):
    def test_profession_budget_must_equal_fifty(self) -> None:
        world = _world()
        world["rules"]["character_card"]["profession_presets"][0]["base_attributes"] = {
            **BASE_ATTRIBUTES,
            "strength": 12,
        }
        report = lint_world_package(world)
        self.assertIn("card.profession_budget", _codes(report, "error"))

    def test_profession_unknown_attribute_is_error(self) -> None:
        world = _world()
        attrs = dict(BASE_ATTRIBUTES)
        attrs.pop("charisma")
        attrs["luck"] = 4
        world["rules"]["character_card"]["profession_presets"][0]["base_attributes"] = attrs
        report = lint_world_package(world)
        self.assertIn("card.profession_unknown_attribute", _codes(report, "error"))

    def test_too_few_professions_is_warning(self) -> None:
        world = _world()
        world["rules"]["character_card"]["profession_presets"] = world["rules"]["character_card"][
            "profession_presets"
        ][:3]
        report = lint_world_package(world)
        self.assertIn("card.too_few_professions", _codes(report, "warning"))

    def test_multiple_free_text_fields_is_error(self) -> None:
        world = _world()
        world["rules"]["character_card"]["fields"].append(
            {"key": "secret", "label": "秘密", "type": "text", "required": False, "max_chars": 200}
        )
        report = lint_world_package(world)
        self.assertIn("card.multiple_free_text", _codes(report, "error"))


class OpeningTests(unittest.TestCase):
    def test_too_many_collective_choices_is_error(self) -> None:
        world = _world()
        choices = world["rules"]["opening_choices"]
        choices[0]["collective"] = True
        choices[1]["collective"] = True
        report = lint_world_package(world)
        self.assertIn("opening.too_many_collective", _codes(report, "error"))

    def test_long_opening_scene_is_warning(self) -> None:
        world = _world(opening_scene="很长" * 300)
        report = lint_world_package(world)
        self.assertIn("opening.too_long", _codes(report, "warning"))


class RealPackageRegressionTests(unittest.TestCase):
    """对仓库里真实世界包的回归——防止校验规则漂移到误报。"""

    def _load(self, filename: str) -> dict:
        path = WORLDS_DIR / filename
        if not path.exists():
            self.skipTest(f"缺少 fixture：{filename}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _npc_slugs(self, filename: str) -> list[str] | None:
        path = WORLDS_DIR / filename
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return None
        return [i["slug"] for i in items if isinstance(i, dict) and i.get("slug")]

    def test_reference_package_is_clean(self) -> None:
        """王都第一日是新包写作的基准，必须零错误零警告。"""
        world = self._load("rezero-first-day-no-return.json")
        slugs = self._npc_slugs("rezero-first-day-no-return-npcs.json")
        report = lint_world_package(world, npc_slugs=slugs)
        self.assertEqual(0, report["errors"], msg=_codes(report, "error"))
        self.assertEqual(0, report["warnings"], msg=_codes(report, "warning"))

    def test_zhaidi_changye_reports_milestone_count_defect(self) -> None:
        """宅邸长夜声明 total_milestones=9，实际定义了 11 个——已知缺陷，必须报出。"""
        world = self._load("rezero-zhaidi-changye.json")
        slugs = self._npc_slugs("rezero-zhaidi-changye-npcs.json")
        report = lint_world_package(world, npc_slugs=slugs)
        self.assertIn("progress.total_milestones_mismatch", _codes(report, "error"))
        # NPC 引用本身是齐的，不应误报
        self.assertNotIn("npc.ref_not_found", _codes(report))

    def test_aelvion_reports_broken_npc_refs(self) -> None:
        """aelvion 的 key_npcs 引用了 NPC 包里不存在的 slug（前缀不一致），必须报出。"""
        world = self._load("aelvion-ashen-crown.json")
        slugs = self._npc_slugs("aelvion-ashen-crown-npcs.json")
        if slugs is None:
            self.skipTest("缺少 aelvion NPC 包")
        report = lint_world_package(world, npc_slugs=slugs)
        self.assertIn("npc.ref_not_found", _codes(report, "error"))


if __name__ == "__main__":
    unittest.main()
