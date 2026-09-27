"""世界包装配与交付闸门的测试。

核心断言只有一条：**装配出来的包必须同时过 lint 与 preflight 两道闸门**。
这条一旦成立，说明"结构由 Python 拼"的分工是有效的——模型只提供内容，
所有标识符、计数、引用闭合都由确定性代码生成。

另外覆盖几类必须被拦住的装配错误（闸门存在的意义）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tavern.lifecycle import card_template, player_limits
from tavern.world_contract import world_contract
from tavern.worldgen.lint import lint_world_package
from tavern.worldgen.emit import (
    EmitError,
    build_character_card,
    build_chapters,
    build_npcs,
    build_world,
    drop_replaced_npcs,
    package_summary,
    reconcile_npc_refs,
    reconcile_resolution_mode,
    run_gates,
    write_packages,
)
from tavern.worldgen.models import ProposalChapter, ProposalMilestone

ATTR_KEYS = [
    "strength", "agility", "vitality", "intellect",
    "willpower", "perception", "charisma",
]
#: 全部取自王都第一日，各自合计 50
BASE_SETS = [
    [11, 9, 10, 4, 6, 6, 4],
    [6, 12, 9, 4, 6, 8, 5],
    [5, 6, 7, 11, 8, 8, 5],
    [6, 7, 7, 12, 6, 8, 4],
    [4, 6, 6, 10, 7, 10, 7],
    [4, 5, 6, 9, 8, 7, 11],
    [5, 8, 6, 6, 7, 7, 11],
    [4, 11, 5, 8, 6, 10, 6],
]


def _professions() -> list[dict]:
    return [
        {
            "id": f"prof_{index}",
            "name": f"职业{index}",
            "description": "擅长某些事，短板明确。",
            "base_attributes": dict(zip(ATTR_KEYS, values)),
        }
        for index, values in enumerate(BASE_SETS)
    ]


def _chapters() -> list[ProposalChapter]:
    return [
        ProposalChapter(
            item_id="cp_1",
            chapter_id="ch_01_arrival",
            title="第一章：陌生的天花板",
            subtitle="醒来",
            min_turns=1,
            max_turns=10,
            current_objective="确认自己身在何处",
            pacing_directive="若无人回应则局势收紧",
            hook_pool=["走廊尽头的脚步声"],
            key_npcs=[{"ref": "npc_ram", "role": "冷面女仆"}],
            milestones=[
                ProposalMilestone(
                    "cp_m1", "m_01_01_trust", "队伍取得可行动的确切情报并已互相传达",
                    evidence_required=[{"type": "clue_keyword_any", "match": ["确认位置", "目击证词"]}],
                ),
                ProposalMilestone(
                    "cp_m2", "m_01_02_anomaly", "确认宅邸存在一处无法解释的异常并记录",
                    evidence_required=[{"type": "clue_keyword_any", "match": ["异常记录"]}],
                ),
            ],
        ),
        ProposalChapter(
            item_id="cp_2",
            chapter_id="ch_02_village",
            title="第二章：村庄",
            subtitle="出门",
            min_turns=1,
            max_turns=12,
            current_objective="在村中取得第一手证词",
            pacing_directive="幼犬躁动逐日加重",
            hook_pool=["结界薄弱点"],
            key_npcs=[{"ref": "npc_ram", "role": "陪同"}],
            milestones=[
                ProposalMilestone(
                    "cp_m3", "m_02_01_signal", "拿到至少一名村民关于异常的目击证词",
                    evidence_required=[{"type": "clue_keyword_any", "match": ["村民证词"]}],
                )
            ],
        ),
    ]


def _prose() -> dict:
    return {
        "opening_scene": "你在陌生的房间里醒来，走廊望不到头。",
        "system_prompt": "本世界规律：不存在死亡回归。",
        "opening_choices": [
            {"key": "A", "text": "起身查看房间"},
            {"key": "B", "text": "敲门问人"},
            {"key": "C", "text": "检查随身物品"},
            {"key": "D", "text": "全队一起下楼", "collective": True},
        ],
        "chapters": [{"chapter_id": "ch_01_arrival", "pacing_directive": "若无人回应则局势收紧"}],
    }


def _npcs() -> list[dict]:
    return [
        {
            "slug": "npc_ram",
            "name": "拉姆",
            "identity": "双胞胎女仆之一",
            "appearance": "粉发短发",
            "personality": "嘴硬",
            "location": "主楼走廊",
            "capabilities": ["清扫", "交涉"],
            "limitations": ["厨艺弱于妹妹"],
            "prompt": "她只回答与日常工作有关的问题。",
        }
    ]


def _world(**overrides) -> dict:
    params = dict(
        slug="demo-mansion",
        name="宅邸长夜",
        description="四天之内活下来。",
        chapters=_chapters(),
        prose=_prose(),
        professions=_professions(),
    )
    params.update(overrides)
    return build_world(**params)


class GateTests(unittest.TestCase):
    def test_resolution_mode_is_one_the_runtime_accepts(self) -> None:
        """模式名写错 → 运行时**静默降级为 none**，整包世界一次检定都不摇。

        生成器曾经写死 ``"d20"``（那是 ``dice_system`` 的值，不是模式），
        生产出的世界全部无检定，而 lint 与 preflight 都不管这一项——
        因为 ``world_contract`` 认不出模式时不报错，只悄悄当 none 用。
        """
        from tavern.world_contract import RESOLUTION_MODES

        world = _world()
        mode = world["rules"]["resolution"]["mode"]
        self.assertIn(mode, RESOLUTION_MODES, msg=f"非法模式 {mode!r}")
        # 而且运行时的确按它生效（不是被降级掉）
        self.assertIn(
            world_contract(world)["resolution"]["mode"], {"dice_only", "attribute"}
        )

    def test_unknown_resolution_mode_is_rejected_by_lint(self) -> None:
        world = _world()
        world["rules"]["resolution"]["mode"] = "d20"
        report = lint_world_package(world)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(i["code"] == "resolution.mode_unknown" for i in report["issues"]),
            msg=[i["code"] for i in report["issues"]],
        )

    def test_assembled_package_passes_both_gates(self) -> None:
        """整体验收：装配结果必须同时过 lint 与 preflight。"""
        world = _world()
        npcs = build_npcs(slug="demo-mansion", npcs=_npcs())
        gates = run_gates(world, npcs)
        self.assertEqual(0, gates["lint"]["errors"], msg=gates["lint"]["issues"])
        self.assertTrue(gates["preflight"].get("compatible"), msg=gates["preflight"])

    def test_chapter_pointers_are_closed(self) -> None:
        """next_chapter_id 由 Python 生成，不允许出现悬空指针。"""
        chapters = _world()["rules"]["progress"]["chapters"]
        ids = [c["id"] for c in chapters]
        for chapter in chapters:
            nxt = chapter.get("next_chapter_id")
            if nxt is None:
                continue  # 终章不续指，由下一条断言单独覆盖
            self.assertIn(nxt, ids, msg=f"{chapter['id']} 的 next_chapter_id 悬空")
        # 终章不指向下一章
        self.assertNotIn("next_chapter_id", chapters[-1])
        self.assertEqual(ids[0], _world()["rules"]["progress"]["current_chapter_id"])

    def test_total_milestones_matches_definition(self) -> None:
        world = _world()
        progress = world["rules"]["progress"]
        actual = sum(len(c["milestones"]) for c in progress["chapters"])
        self.assertEqual(actual, progress["total_milestones"])
        self.assertEqual(0, progress["completed_milestones"])

    def test_evidence_without_match_is_omitted(self) -> None:
        """空 match 的信号比没有更糟——应当整键省略，由 lint 给出准确警告。"""
        chapter = ProposalChapter(
            item_id="c", chapter_id="ch_01_a", title="t", min_turns=1, max_turns=5,
            current_objective="o", pacing_directive="p",
            milestones=[ProposalMilestone("m", "m_01_01_x", "取得确切情报并已传达",
                                          evidence_required=[{"type": "clue_keyword_any", "match": []}])],
        )
        built = build_chapters([chapter])
        self.assertNotIn("evidence_required", built[0]["milestones"][0])


class CardTests(unittest.TestCase):
    def test_preset_select_fields_declare_option_source(self) -> None:
        """preset_select 必须声明选项来源，否则体检报「没有任何有效选项」。"""
        card = build_character_card(professions=_professions())
        by_key = {f["key"]: f for f in card["fields"]}

        self.assertEqual("profession_presets", by_key["profession"].get("preset_source"))
        for key in ("primary_attribute", "secondary_attribute"):
            options = by_key[key].get("options")
            self.assertTrue(options, msg=f"{key} 缺少 options")
            # 选项填的是**显示标签**，不是属性 key
            self.assertIn(by_key[key]["label"].split("（")[0], ["选择主属性", "选择副属性"])
            self.assertNotIn("strength", options)

    def test_single_optional_free_text_field(self) -> None:
        card = build_character_card(professions=_professions())
        free = [f for f in card["fields"] if f["type"] == "text" and not f.get("required")]
        self.assertEqual(1, len(free))
        self.assertEqual("supplement", free[0]["key"])
        self.assertEqual(300, free[0]["max_chars"])

    def test_profession_budget_is_preserved_verbatim(self) -> None:
        card = build_character_card(professions=_professions())
        for preset in card["profession_presets"]:
            self.assertEqual(50, sum(preset["base_attributes"].values()))


class PlayerLimitsTests(unittest.TestCase):
    """生成的世界必须**显式声明**人数上限。

    真实事故：生成器不写 ``rules.player_limits``，``lifecycle.player_limits``
    就退回内置默认 ``maximum=4``——副本永远只能坐 4 个人，第 5 个人被拦在
    「当前副本已满（4/4）」外面。操作者在面板上把「强制人数上限」改成 5 也没用：
    他改的是世界，副本用的是建本那一刻冻结的快照；只要重新导入一次世界包，
    改动又被文件里那个不存在的值覆盖回去。
    """

    def test_generated_world_declares_player_limits(self) -> None:
        world = _world()
        declared = (world.get("rules") or {}).get("player_limits")
        self.assertTrue(declared, msg="生成的世界没有声明 player_limits，会退回默认上限 4")
        self.assertEqual(
            {"minimum_start", "maximum", "recommended_min", "recommended_max"},
            set(declared),
        )

    def test_default_ceiling_allows_five_players(self) -> None:
        world = _world()
        self.assertGreaterEqual(player_limits(world)["maximum"], 5)


class CanonicalCardTests(unittest.TestCase):
    """生成器产出的建卡模板必须与既有世界**逐字一致**（只差职业预设）。

    真实事故：跨副本导入角色卡被拒——「来源与当前本的建卡模板不兼容」。
    原因是生成器自己拼了一套 v2 形状的卡，字段标签、属性描述、修正表、
    ``preset_selector`` / ``bonus_choices`` / ``total_validation`` 全都与
    同作品的其他世界不同。导入比较的是 ``card_template`` 里的
    ``fields`` / ``stats`` / ``preset_dimensions``，任何一项不同就拒绝。

    后果不是报错而是**隔离**：每个生成出来的世界都是一座孤岛，玩家在同一部
    作品的两卷之间搬不过去角色卡，而差异藏在三个 JSON 子树里肉眼看不出来。
    """

    #: 参照世界：同一部作品里手写并跑通过的那一本。
    REFERENCE = Path(__file__).resolve().parents[1] / "worlds" / "rezero-zhaidi-changye.json"

    def _reference_card(self) -> dict:
        if not self.REFERENCE.is_file():
            self.skipTest(f"参照世界不存在：{self.REFERENCE}")
        return json.loads(self.REFERENCE.read_text(encoding="utf-8"))["rules"]["character_card"]

    def test_card_template_matches_the_reference_world(self) -> None:
        built = build_character_card(professions=_professions())
        reference = self._reference_card()
        left = card_template({"rules": {"character_card": built}})
        right = card_template({"rules": {"character_card": reference}})
        for key in ("fields", "stats", "preset_dimensions"):
            self.assertEqual(
                right.get(key),
                left.get(key),
                msg=f"生成器产出的 {key} 与参照世界不同——跨副本导入角色卡会被拒绝",
            )

    def test_only_professions_vary_between_worlds(self) -> None:
        """骨架必须**逐字**相同，不只是 card_template 认得的那几项。

        card_template 会归一化掉一些东西（例如 max_chars 的下限），所以只比它
        不够——属性描述、字段顺序这类差异它看不见，却会让玩家在两个世界里
        看到不一样的建卡界面。
        """
        built = build_character_card(professions=_professions())
        reference = self._reference_card()
        strip = lambda card: {  # noqa: E731
            key: value for key, value in card.items() if key != "profession_presets"
        }
        self.assertEqual(
            json.dumps(strip(reference), ensure_ascii=False, sort_keys=True),
            json.dumps(strip(built), ensure_ascii=False, sort_keys=True),
        )

    def test_relabelled_attributes_carry_into_the_option_lists(self) -> None:
        """主/副属性的 options 填的是**显示标签**：改了标签，选项必须跟着改。"""
        card = build_character_card(
            professions=_professions(),
            attribute_labels={"strength": "体魄"},
        )
        by_key = {f["key"]: f for f in card["fields"]}
        self.assertIn("体魄", by_key["primary_attribute"]["options"])
        self.assertNotIn("力量", by_key["primary_attribute"]["options"])


class RejectionTests(unittest.TestCase):
    """闸门存在的意义是拦住坏包——这里验证它确实拦得住。"""

    def test_illegal_slug_is_rejected(self) -> None:
        with self.assertRaises(EmitError):
            _world(slug="非法 Slug!")

    def test_no_chapters_is_rejected(self) -> None:
        with self.assertRaises(EmitError):
            build_world(
                slug="x", name="n", description="d",
                chapters=[], prose=_prose(), professions=_professions(),
            )

    def test_broken_profession_budget_fails_gate(self) -> None:
        professions = _professions()
        professions[0]["base_attributes"]["strength"] += 1  # 合计变成 51
        world = _world(professions=professions)
        npcs = build_npcs(slug="demo-mansion", npcs=_npcs())
        with self.assertRaises(EmitError) as ctx:
            run_gates(world, npcs)
        self.assertTrue(
            any("51" in p or "50" in p for p in ctx.exception.problems),
            msg=ctx.exception.problems,
        )

    def test_orphan_npc_ref_fails_gate(self) -> None:
        chapters = _chapters()
        chapters[0].key_npcs = [{"ref": "npc_ghost", "role": "不存在的人"}]
        world = _world(chapters=chapters)
        npcs = build_npcs(slug="demo-mansion", npcs=_npcs())
        with self.assertRaises(EmitError) as ctx:
            run_gates(world, npcs)
        self.assertTrue(any("npc_ghost" in p for p in ctx.exception.problems))

    def test_empty_npc_list_is_rejected(self) -> None:
        with self.assertRaises(EmitError):
            build_npcs(slug="x", npcs=[])


class ReconcileNpcRefsTests(unittest.TestCase):
    """章节引用必须收敛到 NPC 包真的有的角色上。

    真实事故（chapter030 首次 E2E）：章节抽取照着原文给每个有名有姓的人
    写了 ref（24 个），而 CAST 明确要求「背景板人物不要收」只建了 7 张卡。
    两边判据相反又没人对账，于是整条流水线跑完二十来分钟，在最后一刻被
    交付闸门一句 ``key_npcs 引用了 NPC 包中不存在的 slug`` 打回，
    artifacts 全须全尾——纯粹白跑。
    """

    def _reconciled(self) -> tuple[dict, list]:
        chapters = _chapters()
        chapters[0].key_npcs = [
            {"ref": "npc_ram", "role": "女仆"},
            {"ref": "npc_ghost", "role": "只被提到过的人"},
        ]
        world = _world(chapters=chapters)
        npcs = build_npcs(slug="demo-mansion", npcs=_npcs())
        return reconcile_npc_refs(world, npcs)

    def test_orphan_ref_is_dropped_and_reported(self) -> None:
        fixed, dropped = self._reconciled()
        self.assertEqual(["npc_ghost"], [d["ref"] for d in dropped])
        self.assertEqual(
            fixed["rules"]["progress"]["chapters"][0]["id"],
            dropped[0]["chapter_id"],
        )
        self.assertEqual("只被提到过的人", dropped[0]["role"])

    def test_known_ref_survives(self) -> None:
        fixed, _ = self._reconciled()
        self.assertIn("npc_ram", json.dumps(fixed, ensure_ascii=False))

    def test_input_is_not_mutated(self) -> None:
        """对账要能安全地反复跑（闸门阶段会再跑一次）。"""
        chapters = _chapters()
        chapters[0].key_npcs = [{"ref": "npc_ghost", "role": "x"}]
        world = _world(chapters=chapters)
        snapshot = json.dumps(world, ensure_ascii=False, sort_keys=True)
        reconcile_npc_refs(world, build_npcs(slug="demo-mansion", npcs=_npcs()))
        self.assertEqual(
            snapshot, json.dumps(world, ensure_ascii=False, sort_keys=True)
        )

    def test_reconciled_package_passes_the_gate(self) -> None:
        """对账之后闸门必须放行——这才叫修好了，而不是把错误换个地方。"""
        fixed, _ = self._reconciled()
        gates = run_gates(fixed, build_npcs(slug="demo-mansion", npcs=_npcs()))
        self.assertEqual(
            [], [i for i in gates["lint"]["issues"] if i["level"] == "error"]
        )

    def test_resolution_mode_heals_and_reports(self) -> None:
        """认不出的模式要修掉**并报出来**，不能只改不说。"""
        world = _world()
        world["rules"]["resolution"]["mode"] = "d20"
        fixed, note = reconcile_resolution_mode(world)
        self.assertEqual("attribute", fixed["rules"]["resolution"]["mode"])
        self.assertIn("d20", note)
        # 原对象不动
        self.assertEqual("d20", world["rules"]["resolution"]["mode"])

    def test_valid_resolution_mode_is_left_alone(self) -> None:
        world = _world()
        _, note = reconcile_resolution_mode(world)
        self.assertEqual("", note)

    def test_no_card_attributes_falls_back_to_dice_only(self) -> None:
        """没有属性表就退到 dice_only——attribute 模式会要求属性 ID。"""
        world = _world()
        world["rules"]["resolution"]["mode"] = "d20"
        world["rules"]["character_card"]["stats"]["attributes"] = []
        fixed, _ = reconcile_resolution_mode(world)
        self.assertEqual("dice_only", fixed["rules"]["resolution"]["mode"])

    def test_missing_npc_package_drops_everything(self) -> None:
        """没有 NPC 包（或包是空的）时不留孤儿引用，全部摘掉。"""
        fixed, dropped = self._reconciled()
        empty, all_dropped = reconcile_npc_refs(fixed, {"items": []})
        for chapter in empty["rules"]["progress"]["chapters"]:
            self.assertEqual([], list(chapter.get("key_npcs") or []))
        self.assertTrue(all_dropped)


class DropReplacedNpcsTests(unittest.TestCase):
    """由玩家取代的原作角色，绝不能出现在 NPC 名录里。

    真实事故：操作者在生成要求里写了「由玩家小队替换掉男主 486」，但这条意图
    只喂给了时间线与卷级地图两步。CAST 看不到它，于是照着原文给戏份最多的
    男主建了卡——交付出来的 NPC 包里躺着一张 ``npc_subaru``（菜月昴），
    而玩家本该站在他的位置上。

    提示词是劝告，这里是保证。
    """

    def _npcs(self) -> list[dict]:
        return [
            {"slug": "npc_ram", "name": "拉姆", "prompt": "女仆。"},
            {"slug": "npc_subaru", "name": "菜月昴", "prompt": "被卷入异世界的少年。"},
            {"slug": "npc_rem", "name": "雷姆", "prompt": "女仆。"},
        ]

    def test_replaced_character_is_dropped_and_reported(self) -> None:
        kept, dropped = drop_replaced_npcs(self._npcs(), ["菜月昴"])
        self.assertEqual(["npc_ram", "npc_rem"], [n["slug"] for n in kept])
        self.assertEqual(["npc_subaru"], [d["slug"] for d in dropped])
        # 报出来的是**为什么**被剔除，面板要显示它
        self.assertEqual("菜月昴", dropped[0]["name"])
        self.assertEqual("菜月昴", dropped[0]["matched"])

    def test_short_name_still_matches_full_name(self) -> None:
        """原文里可能只写名不写姓：声明「菜月昴」，名录里叫「昴」也要命中。"""
        kept, dropped = drop_replaced_npcs(
            [{"slug": "npc_subaru", "name": "昴", "prompt": "x"}], ["菜月昴"]
        )
        self.assertEqual([], kept)
        self.assertEqual(["npc_subaru"], [d["slug"] for d in dropped])

    def test_nothing_is_dropped_without_a_replacement(self) -> None:
        """没有要求替换时一个都不能少——这条规则不能变成"删掉戏份多的人"。"""
        kept, dropped = drop_replaced_npcs(self._npcs(), [])
        self.assertEqual(3, len(kept))
        self.assertEqual([], dropped)

    def test_unrelated_names_are_untouched(self) -> None:
        kept, _ = drop_replaced_npcs(self._npcs(), ["菜月昴"])
        self.assertEqual(["拉姆", "雷姆"], [n["name"] for n in kept])

    def test_input_is_not_mutated(self) -> None:
        npcs = self._npcs()
        snapshot = json.dumps(npcs, ensure_ascii=False, sort_keys=True)
        drop_replaced_npcs(npcs, ["菜月昴"])
        self.assertEqual(snapshot, json.dumps(npcs, ensure_ascii=False, sort_keys=True))


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_two_files(self) -> None:
        """默认产出**两个**包：世界包 + NPC 包。"""
        world = _world()
        npcs = build_npcs(slug="demo-mansion", npcs=_npcs())
        paths = write_packages(output_dir=self.directory, slug="demo-mansion", world=world, npcs=npcs)

        self.assertTrue(Path(paths["world"]).is_file())
        self.assertTrue(Path(paths["npcs"]).is_file())
        self.assertEqual("demo-mansion.json", Path(paths["world"]).name)
        self.assertEqual("demo-mansion-npcs.json", Path(paths["npcs"]).name)

        npc_payload = json.loads(Path(paths["npcs"]).read_text(encoding="utf-8"))
        self.assertEqual(2, npc_payload["template_version"])
        self.assertEqual("demo-mansion", npc_payload["world_slug"])
        self.assertEqual(["npc_ram"], [i["slug"] for i in npc_payload["items"]])

    def test_refuses_to_overwrite_existing(self) -> None:
        world = _world()
        npcs = build_npcs(slug="demo-mansion", npcs=_npcs())
        write_packages(output_dir=self.directory, slug="demo-mansion", world=world, npcs=npcs)
        with self.assertRaises(EmitError):
            write_packages(output_dir=self.directory, slug="demo-mansion", world=world, npcs=npcs)

    def test_summary_shape(self) -> None:
        summary = package_summary(_world(), build_npcs(slug="demo-mansion", npcs=_npcs()))
        self.assertEqual(2, summary["chapters"])
        self.assertEqual(3, summary["milestones"])
        self.assertEqual(1, summary["npcs"])
        self.assertEqual(8, summary["professions"])


if __name__ == "__main__":
    unittest.main()
