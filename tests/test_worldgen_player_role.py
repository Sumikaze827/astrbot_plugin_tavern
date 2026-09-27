"""「玩家取代原作主角」这条改编规则的测试。

**真实事故**：操作者在生成要求里写了「由玩家小队替换掉男主 486」，改编出来的
世界包与 NPC 包里却躺着男主——``npc_subaru``（菜月昴）成了一张可交互的 NPC 卡，
46 条里程碑里还有一批把他写成了行动的施动者（「昴发现小巷中的对峙并抵达现场」）。

成因是**这条要求没有出口**：``requirements`` 只喂给了时间线与卷级地图两步，
而真正决定「谁有角色卡」的 ``CAST``、决定「每个里程碑由谁完成」的 ``EXTRACT``、
写开场白与系统提示的 ``PROSE`` 全都看不到它；也没有任何一步机械核对过它。

所以这里锁四层，缺一层这条规则就会再漏一次：

1. 时间线必须**声明** ``player_role.replaces``（下游的口径来源）；
2. 提示词必须把点名的人喂进后续每一步；
3. 规则本身必须写在 **system** 里（``tavern/prompts.py:repair_prompt`` 重试时
   不重发 user prompt，只写在 user 里的规则第一次重试就消失）；
4. 校验与剔除必须是**机械**的，不能只靠模型自觉。
"""

from __future__ import annotations

import unittest

from tavern.worldgen import prompts
from tavern.worldgen.steps import (
    replaced_name_hits,
    validate_cast_excludes_replaced,
    validate_chapter_avoids_replaced_actors,
)

REPLACED = ["菜月昴"]


def _chapter_payload(**overrides) -> dict:
    """一个形状合法的章节检查点；只在本文件关心的字段上做覆盖。"""
    chapter = {
        "title": "再次来到王都",
        "subtitle": "王选篇开幕",
        "current_objective": "玩家小队查清王都的局势",
        "pacing_directive": "若迟迟不与近卫接触，市场里的流言会先一步散开",
        "hook_pool": ["值班室外那扇一直没开的门"],
        "milestones": [
            {
                "label": "玩家小队在贵族街值班室外确认了艾米莉娅的去向",
                "evidence_required": [
                    {"type": "clue_keyword_any", "match": ["值班室", "近卫"]}
                ],
            }
        ],
        "key_npcs": [{"ref": "npc_rem", "role": "随行的女仆，负责指路"}],
    }
    chapter.update(overrides)
    return {"chapter": chapter}


class PlayerPositionBlockTests(unittest.TestCase):
    """``<player_position>`` 块：说清谁被取代，以及他不能出现在哪。"""

    def test_no_replacement_means_no_block(self) -> None:
        """没有要取代的人就不该改变提示词——这条规则不能顺手删掉戏份多的人。"""
        self.assertEqual("", prompts.player_position_block([]))
        self.assertEqual("", prompts.player_position_block(None))

    def test_names_are_listed(self) -> None:
        block = prompts.player_position_block(REPLACED)
        self.assertIn("菜月昴", block)
        self.assertIn("<player_position>", block)
        self.assertIn("</player_position>", block)

    def test_block_forbids_the_card_and_the_actor_slot(self) -> None:
        block = prompts.player_position_block(REPLACED)
        self.assertIn("不得", block)
        self.assertIn("玩家小队", block)
        # 原文引用要照抄，名字出现在证据词里不算违规
        self.assertIn("evidence_required", block)

    def test_duplicate_names_are_listed_once(self) -> None:
        block = prompts.player_position_block(["菜月昴", "菜月昴", "  "])
        self.assertEqual(1, block.count("菜月昴 **不得**"))


class RuleLivesInSystemPromptTests(unittest.TestCase):
    """规则必须在 system 里。

    ``repair_prompt``（``tavern/prompts.py``）重试时**不重发 user prompt**——
    只带 system + 被拒输出 + 问题清单。规则若只写在 user prompt 里，
    第一次重试就消失了，而被取代的角色恰恰是最需要重试兜底的那类问题。
    """

    def test_every_consuming_step_carries_the_rule(self) -> None:
        """这些步骤都会收到 ``<player_position>`` 块，规则必须在 system 里兜住重试。"""
        for name in (
            "ARC_MAP_SYSTEM",
            "EXTRACT_CHAPTER_SYSTEM",
            "CAST_SYSTEM",
            "REGEN_MILESTONE_SYSTEM",
            "PROSE_SYSTEM",
        ):
            system = getattr(prompts, name)
            self.assertIn(
                "<player_position_rule>",
                system,
                msg=f"{name} 没有带玩家位置规则——重试时会失守",
            )
            self.assertIn("player_position", system, msg=name)

    def test_reflect_judges_by_the_replacement_not_by_the_source(self) -> None:
        """反省是事实核查员，它必须知道"谁的位置已经属于玩家"。

        否则它会看到「原文里是昴做的、草稿里是玩家小队做的」，判成
        ``contradicted`` 并**自动改写回昴**——把这条改编规则又推翻掉。
        """
        self.assertIn("<player_position_rule>", prompts.REFLECT_SYSTEM)
        self.assertIn("contradicted", prompts.REFLECT_SYSTEM)

        prompt = prompts.reflect_prompt(
            claims=[{"claim_id": "c1"}], passages_by_claim={}, replaced_names=REPLACED
        )
        self.assertIn("菜月昴", prompt)

        without = prompts.reflect_prompt(claims=[{"claim_id": "c1"}], passages_by_claim={})
        self.assertNotIn("<player_position>", without)

    def test_timeline_declares_the_field_and_the_wording_rule(self) -> None:
        """时间线是**声明者**：它不消费 ``<player_position>`` 块，但要产出 ``player_role``，
        并且缝线里的措辞（`merged.label`）就是后面章节的措辞来源。"""
        self.assertIn("player_role", prompts.TIMELINE_SYSTEM)
        self.assertIn("replaces", prompts.TIMELINE_SYSTEM)
        self.assertIn("merged", prompts.TIMELINE_SYSTEM)
        self.assertIn("玩家小队", prompts.TIMELINE_SYSTEM)


class PromptInjectionTests(unittest.TestCase):
    """点名的人要真的进到后续每一步的 prompt 里。"""

    def setUp(self) -> None:
        self.block = prompts.player_position_block(REPLACED)

    def test_extract_chapter_receives_the_block(self) -> None:
        prompt = prompts.extract_chapter_prompt(
            arc_title="第三章",
            candidate={"title": "王选篇"},
            passages=[{"rel_path": "01.md", "line_start": 1, "line_end": 2, "text": "正文"}],
            player_position=self.block,
        )
        self.assertIn("<player_position>", prompt)

    def test_cast_receives_the_block(self) -> None:
        prompt = prompts.cast_prompt(
            arc_title="第三章",
            referenced=["npc_rem"],
            passages=[{"rel_path": "01.md", "line_start": 1, "line_end": 2, "text": "正文"}],
            player_position=self.block,
        )
        self.assertIn("<player_position>", prompt)

    def test_arc_map_receives_the_block(self) -> None:
        prompt = prompts.arc_map_prompt(
            arc_title="第三章",
            outline=[{"index": 0, "title": "序章"}],
            head_samples=[],
            player_position=self.block,
        )
        self.assertIn("<player_position>", prompt)

    def test_prose_receives_the_block(self) -> None:
        prompt = prompts.prose_prompt(
            name="王选篇",
            description="测试",
            chapters=[],
            npcs=[],
            player_position=self.block,
        )
        self.assertIn("<player_position>", prompt)

    def test_empty_block_leaves_prompts_unchanged(self) -> None:
        """没有替换要求时，prompt 里不该凭空多出一个空块。"""
        prompt = prompts.cast_prompt(
            arc_title="第三章", referenced=[], passages=[], player_position=""
        )
        self.assertNotIn("<player_position>", prompt)


class ReplacedNameHitsTests(unittest.TestCase):
    def test_full_and_short_names_both_hit(self) -> None:
        self.assertEqual(["菜月昴"], replaced_name_hits("菜月昴", REPLACED))
        # 原文里常常只写名不写姓
        self.assertEqual(["菜月昴"], replaced_name_hits("昴被留在门外等待", REPLACED))

    def test_unrelated_text_misses(self) -> None:
        self.assertEqual([], replaced_name_hits("雷姆在走廊尽头", REPLACED))
        self.assertEqual([], replaced_name_hits("", REPLACED))
        self.assertEqual([], replaced_name_hits(None, REPLACED))


class CastValidatorTests(unittest.TestCase):
    """CAST 不得给被玩家取代的角色建卡。"""

    def _cast(self, *names: str) -> dict:
        return {
            "npcs": [
                {"slug": f"npc_{i}", "name": name} for i, name in enumerate(names)
            ]
        }

    def test_replaced_character_card_is_rejected(self) -> None:
        problems = validate_cast_excludes_replaced(self._cast("拉姆", "菜月昴"), REPLACED)
        self.assertEqual(1, len(problems), msg=problems)
        # 问题文案必须点名是谁：重试时不重发 user prompt，模型只能从这里知道
        self.assertIn("菜月昴", problems[0])
        self.assertIn("玩家", problems[0])

    def test_short_name_is_rejected_too(self) -> None:
        problems = validate_cast_excludes_replaced(self._cast("昴"), REPLACED)
        self.assertEqual(1, len(problems), msg=problems)

    def test_other_characters_pass(self) -> None:
        self.assertEqual([], validate_cast_excludes_replaced(self._cast("拉姆", "雷姆"), REPLACED))

    def test_without_a_replacement_nothing_is_flagged(self) -> None:
        self.assertEqual([], validate_cast_excludes_replaced(self._cast("菜月昴"), []))


class ChapterActorValidatorTests(unittest.TestCase):
    """章节检查点里被取代的角色不能当施动者，但原文引用要照抄。"""

    def test_milestone_with_the_protagonist_as_actor_is_rejected(self) -> None:
        payload = _chapter_payload()
        payload["chapter"]["milestones"][0]["label"] = "昴发现小巷中的对峙并抵达现场"
        problems = validate_chapter_avoids_replaced_actors(payload, REPLACED)
        self.assertTrue(problems, msg="里程碑把男主写成施动者却没被拦下")
        self.assertIn("玩家小队", problems[0])

    def test_current_objective_is_checked(self) -> None:
        problems = validate_chapter_avoids_replaced_actors(
            _chapter_payload(current_objective="陪昴走完王选的流程"), REPLACED
        )
        self.assertTrue(any("current_objective" in p for p in problems), msg=problems)

    def test_pacing_and_hooks_are_checked(self) -> None:
        problems = validate_chapter_avoids_replaced_actors(
            _chapter_payload(
                pacing_directive="若昴拒绝出门，雷姆会主动提出同行",
                hook_pool=["昴与骑士的对立"],
            ),
            REPLACED,
        )
        self.assertTrue(any("pacing_directive" in p for p in problems), msg=problems)
        self.assertTrue(any("hook_pool" in p for p in problems), msg=problems)

    def test_key_npc_role_is_checked(self) -> None:
        problems = validate_chapter_avoids_replaced_actors(
            _chapter_payload(key_npcs=[{"ref": "npc_rem", "role": "陪昴同行的人"}]),
            REPLACED,
        )
        self.assertTrue(any("key_npcs" in p for p in problems), msg=problems)

    def test_evidence_quotes_are_exempt(self) -> None:
        """``evidence_required[].match`` 是原文抄来的证据词，照抄才对。"""
        payload = _chapter_payload()
        payload["chapter"]["milestones"][0]["evidence_required"] = [
            {"type": "clue_keyword_any", "match": ["粘着昴的指头", "右手食指不在了"]}
        ]
        self.assertEqual([], validate_chapter_avoids_replaced_actors(payload, REPLACED))

    def test_a_clean_chapter_passes(self) -> None:
        self.assertEqual([], validate_chapter_avoids_replaced_actors(_chapter_payload(), REPLACED))

    def test_without_a_replacement_nothing_is_flagged(self) -> None:
        payload = _chapter_payload(current_objective="陪昴走完王选的流程")
        self.assertEqual([], validate_chapter_avoids_replaced_actors(payload, []))


if __name__ == "__main__":
    unittest.main()
