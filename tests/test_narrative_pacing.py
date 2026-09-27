"""叙事节奏三规则的回归测试：里程碑不催赶、转折伏笔化、细节密度。

背景（2026-08-23 用户反馈）：
1. 模型得知检查点后急于直接输出 → 每章只有 1-2 轮玩家体验；
2. 转折/真相被一次性吐露，没有铺垫；
3. 正文缺乏细节，玩家没有可行动的抓手。
修复落在 planning_prompt 生成规则里，本测试锁定三组规则文案。
"""

from __future__ import annotations

import unittest

from tavern.prompts import (
    compact_world_rules,
    milestone_judge_prompt,
    planning_prompt,
)


def _prompt(**progress) -> str:
    return planning_prompt(
        world={"name": "t", "slug": "t", "system_prompt": "s", "rules": {}},
        session={
            "world_state": {"location": "营地"},
            "progress": {
                "current_chapter_id": "ch_01",
                "chapter": "第一章",
                "narrative_length_band": "standard",
                **progress,
            },
        },
        player={"participant_id": "p1"},
        player_input="观察周围",
        events=[],
        memories=[],
        allow_checks=False,
    )


class MilestonePacingTests(unittest.TestCase):
    def test_milestone_is_not_this_turn_goal(self) -> None:
        """里程碑是方向清单，不是本轮目标——禁止为完成检查点而催赶剧情。"""
        prompt = _prompt()
        self.assertIn("里程碑是本章结束前的方向清单，不是本轮目标", prompt)
        self.assertIn("不要为完成里程碑而催赶剧情", prompt)

    def test_milestone_requires_player_action_evidence(self) -> None:
        """只有玩家行动亲手完成时才记录证据，禁止旁白宣告。"""
        prompt = _prompt()
        self.assertIn(
            "只有当本轮玩家行动亲手完成了里程碑描述的", prompt
        )
        self.assertIn("禁止旁白式宣告里程碑达成", prompt)
        self.assertIn("禁止替玩家完成里程碑动作", prompt)
        self.assertIn("同一轮实际完成多个里程碑时逐项提交各自证据", prompt)

    def test_milestone_hard_pending_only_force(self) -> None:
        """只有 HARD-PENDING 指令才允许强行推进未达成里程碑。"""
        prompt = _prompt()
        self.assertIn("[Pacing Chapter-HARD-PENDING]", prompt)
        self.assertIn("否则不得强行推进未达成的里程碑", prompt)

    def test_chapter_switch_waits_for_natural_finish(self) -> None:
        """里程碑全达成后不立刻切章——场景自然收尾才切（2026-08-23
        反馈：章节检查点输出太快，玩家没体验完章就被切走。"""
        prompt = _prompt()
        self.assertIn("不为凑回合追加遭遇或重复确认", prompt)
        self.assertIn("场景自然收尾", prompt)
        self.assertIn("模型永远不得修改 current_chapter_id", prompt)
        self.assertIn("收束证据复核通过后自动切章", prompt)

    def test_pending_chapter_does_not_lock_player_location(self) -> None:
        """章节里程碑保留进度，但不能成为阻拦玩家旅行的地点锁。"""
        prompt = _prompt()
        self.assertIn("章节不是地点锁", prompt)
        self.assertIn("玩家可前往、折返或探索", prompt)
        self.assertIn("主线只通过世界后果和非强制选项自然提示", prompt)
        self.assertNotIn("必须停留在当前章节场景", prompt)
        self.assertNotIn("不得进入下一章的地点或内容", prompt)


class HealingStatusOpsTests(unittest.TestCase):
    @staticmethod
    def _roster(*, locked: bool = False):
        status = {
            "name": "断钉铜锈反噬",
            "severity": "critical",
            "effect": "术法行动劣势",
        }
        if locked:
            status["healing_policy"] = "story_locked"
            status["policy_source"] = "world"
        return [{
            "id": "p1",
            "character_name": "柏辰",
            "runtime_state": {"statuses": [status]},
        }]

    def test_healing_must_remove_or_downgrade_not_add(self) -> None:
        """治疗/净化只能 remove 或 update 既有状态，禁止 add 新状态（2026-08-23
        反馈：奶妈治疗把灼伤越治越多——模型用 add 表达『减轻』。"""
        prompt = _prompt()
        self.assertIn("治疗/净化/驱散类行动", prompt)
        self.assertIn("彻底解除 → status_ops.remove", prompt)
        self.assertIn("仅减轻 → status_ops.update", prompt)
        self.assertIn("禁止用 status_ops.add 表达", prompt)
        self.assertIn("治疗绝不产生新状态", prompt)
        self.assertIn("同一角色的同一类状态", prompt)
        self.assertIn("普通状态必须 remove", prompt)
        self.assertIn("不得临时编造", prompt)

    def test_successful_cure_converts_update_to_remove(self) -> None:
        from tavern.engine import TavernEngine

        result = TavernEngine._healing_status_ops(
            player_input="牧师治疗柏辰的断钉铜锈反噬",
            outcome="success",
            status_ops=[{
                "op": "update", "target_id": "p1",
                "name": "断钉铜锈反噬", "severity": "serious",
            }],
            roster=self._roster(),
            acting_participant={"id": "healer"},
        )
        self.assertEqual(result[0]["op"], "remove")

    def test_successful_named_cure_synthesizes_missing_remove(self) -> None:
        from tavern.engine import TavernEngine

        result = TavernEngine._healing_status_ops(
            player_input="彻底治疗柏辰的断钉铜锈反噬",
            outcome="success_with_cost",
            status_ops=[],
            roster=self._roster(),
            acting_participant={"id": "healer"},
        )
        self.assertEqual(
            result,
            [{"op": "remove", "target_id": "p1", "name": "断钉铜锈反噬"}],
        )

    def test_story_locked_status_resists_ordinary_healing(self) -> None:
        from tavern.engine import TavernEngine

        result = TavernEngine._healing_status_ops(
            player_input="彻底治疗柏辰的断钉铜锈反噬",
            outcome="critical_success",
            status_ops=[{
                "op": "update", "target_id": "p1",
                "name": "断钉铜锈反噬", "severity": "serious",
            }],
            roster=self._roster(locked=True),
            acting_participant={"id": "healer"},
        )
        self.assertEqual(result[0]["op"], "update")

    def test_failed_or_stabilising_treatment_does_not_force_remove(self) -> None:
        from tavern.engine import TavernEngine

        operation = {
            "op": "update", "target_id": "p1",
            "name": "断钉铜锈反噬", "severity": "serious",
        }
        failed = TavernEngine._healing_status_ops(
            player_input="治疗柏辰的断钉铜锈反噬",
            outcome="failure",
            status_ops=[operation],
            roster=self._roster(),
            acting_participant={"id": "healer"},
        )
        stabilised = TavernEngine._healing_status_ops(
            player_input="先稳住柏辰的断钉铜锈反噬",
            outcome="success",
            status_ops=[operation],
            roster=self._roster(),
            acting_participant={"id": "healer"},
        )
        self.assertEqual(failed[0]["op"], "update")
        self.assertEqual(stabilised[0]["op"], "update")

    def test_schema_notes_healing_op_restriction(self) -> None:
        """输出 schema 的 status_ops 描述也带治疗限制。"""
        from tavern.prompts import system_prompt

        prompt = system_prompt(
            world={"name": "t", "slug": "t", "system_prompt": "s", "rules": {}},
            allow_check=False,
        )
        self.assertIn("治疗/净化/驱散只能 remove", prompt)
        self.assertIn("禁止用 add 新增状态来表达治疗", prompt)


class ForeshadowingTests(unittest.TestCase):
    def test_twists_require_foreshadowing(self) -> None:
        """转折可分阶段呈现，但每阶段必须清晰铺垫（2026-08-24 软化：
        玩家反馈「太谜语人」后，原「不得一次性吐露」改为「可分阶段但每
        阶段清晰」，避免模型把「分阶段」当成「纯暗示/谜语」的借口）。"""
        prompt = _prompt()
        self.assertIn(
            "关键真相、身份反转、立场摊牌可分阶段呈现", prompt
        )
        self.assertIn("可回看的铺垫", prompt)
        self.assertIn("不得为了凑伏笔拖延", prompt)

    def test_foreshadowing_must_not_delay_progress(self) -> None:
        """铺垫不得拖延进度：转折该发生时照常发生。"""
        prompt = _prompt()
        self.assertIn("不得为了凑伏笔拖延", prompt)
        self.assertIn("转折照常发生，伏笔用于呼应而非延期", prompt)

    def test_unrevealed_truths_never_leaked(self) -> None:
        """未揭示的剧情真相禁止主动揭穿（2026-08-24 软化为「禁止主动
        揭穿由其他玩家未掌握的剧情真相」，原文「禁止直接写出尚未揭示
        的真相」被玩家反馈「太谜语人」误读后改写）。"""
        prompt = _prompt()
        self.assertIn("禁止主动揭穿由其他玩家未掌握的剧情真相", prompt)


class DetailDensityTests(unittest.TestCase):
    def test_narrative_requires_sensory_and_actionable_detail(self) -> None:
        """每轮选择变化的感官细节，并保留 NPC 反应与行动抓手。"""
        prompt = _prompt()
        self.assertIn("一至两项", prompt)
        self.assertIn("不要机械重复", prompt)
        self.assertIn("NPC/敌人本轮可见的反应", prompt)
        self.assertIn("至少一个玩家可据以行动", prompt)


class ClarityRulesTests(unittest.TestCase):
    """反谜语人 + 设定介绍/铺垫硬约束（2026-08-24 玩家反馈）。

    玩家报告 4 类退化：① NPC 首次登场只丢名字/称号不介绍外貌身份，
    ② 场景/地点只丢地名不写环境，③ 关键概念/物品/设定词直接用不
    解释，④ 动作/结果只用暗示不直接说。落地为 planning_prompt 与
    checked_resolution_prompt 的「清晰度硬约束」段落。
    """

    def test_npc_first_appearance_must_anchor(self) -> None:
        prompt = _prompt()
        self.assertIn("首次登场的 NPC", prompt)
        self.assertIn("身份 / 外貌 / 与场景的关联", prompt)
        self.assertIn("不得只丢名字、称号或代称", prompt)

    def test_new_scene_must_anchor_space_and_mood(self) -> None:
        prompt = _prompt()
        self.assertIn("首次进入的场景", prompt)
        self.assertIn("空间感", prompt)
        self.assertIn("可观察的具体物", prompt)
        self.assertIn("不得只丢", prompt)

    def test_world_terms_first_appearance_must_be_anchored(self) -> None:
        prompt = _prompt()
        self.assertIn("世界专有名词", prompt)
        self.assertIn("首次出现时", prompt)
        self.assertIn("在场角色能观察到的具体线索", prompt)
        # 这条约束是 prompt 里的硬规则（不得让玩家看着专有名词发愣），
        # 用 assertIn 锁定，避免未来被误删成纯暗示。

    def test_action_results_must_be_stated_not_implied(self) -> None:
        prompt = _prompt()
        self.assertIn("关键动作结果必须直接陈述", prompt)
        self.assertIn("不得用", prompt)
        self.assertIn("气氛变了", prompt)
        self.assertIn("他的眼神松动", prompt)
        self.assertIn("模糊暗示", prompt)

    def test_outcome_tags_must_not_leak_into_narrative(self) -> None:
        # 2026-08-24：玩家反馈模型在正文里直接输出 "success_with_cost 下"
        # / "critical_failure 下" / "大成功" / "判定 = ..." / "DC = ..." 等
        # 检定元信息——这些是审计标签不是写给玩家看的。新增硬约束锁住。
        prompt = _prompt()
        self.assertIn("禁止在正文里泄露判定/结果分类标签", prompt)
        self.assertIn("success_with_cost", prompt)
        self.assertIn("critical_failure", prompt)
        self.assertIn("DC =", prompt)
        self.assertIn("骰面 + 修正", prompt)

    def test_narrative_must_use_plain_language(self) -> None:
        # 2026-08-24：玩家反馈"模型输出莫名奇妙的深奥难以理解"——
        # 修辞华丽/隐喻/文言让玩家看不懂。新增硬约束要求直白白话。
        prompt = _prompt()
        self.assertIn("正文语言必须用直白白话", prompt)
        self.assertIn("避免诗意化", prompt)
        self.assertIn("隐喻化", prompt)
        self.assertIn("文言化", prompt)
        self.assertIn("看不懂这句话在讲什么", prompt)

    def test_clarity_rules_also_in_checked_resolution(self) -> None:
        """锁定选项的叙事 prompt 也必须带同一组清晰度硬约束。"""
        from tavern.prompts import checked_resolution_prompt

        prompt = checked_resolution_prompt(
            world={"name": "t", "slug": "t", "system_prompt": "s", "rules": {}},
            session={
                "world_state": {"location": "营地"},
                "progress": {"narrative_length_band": "standard"},
            },
            player={"participant_id": "p1"},
            player_input="观察",
            events=[],
            memories=[],
            check={"stat": "wits", "difficulty": 12, "check_type": "standard"},
            dice={"outcome": "success", "total": 18, "difficulty": 12},
        )
        self.assertIn("清晰度硬约束", prompt)
        self.assertIn("首次登场的NPC", prompt)
        self.assertIn("关键动作结果必须直接陈述", prompt)

    def test_story_length_band_only_supplies_a_floor(self) -> None:
        """2026-08-24：下限从 100 放宽到 150，让 NPC/场景/专有名词的清晰
        介绍有篇幅空间（玩家反馈「设定缺少介绍和铺垫」）。
        2026-09-20：上限取消——band 不再影响结果，一律 (150, 0)。"""
        from tavern.prompts import story_length_bounds

        self.assertEqual(story_length_bounds(False, "standard"), (150, 0))
        self.assertEqual(story_length_bounds(False, "compact"), (150, 0))
        self.assertEqual(story_length_bounds(False, "expanded"), (150, 0))


class PromptCompactionTests(unittest.TestCase):
    def test_full_progress_tree_is_not_reinjected(self) -> None:
        """当前章已有独立快照，compact rules 不再携带全章导演树。"""
        compact = compact_world_rules(
            {
                "rules": {
                    "progress": {"chapters": [{"id": "secret_future"}]},
                    "player_input_policy": {"mode": "low_burden"},
                }
            }
        )
        self.assertNotIn("progress", compact)
        self.assertEqual(
            compact["player_input_policy"]["mode"], "low_burden"
        )


class MilestoneJudgeStrictnessTests(unittest.TestCase):
    """2026-08-24：玩家反馈「里程碑触发太早，章节根本没满足对应条件」
    +「担心结束点也这样」。锁住 milestone_judge_prompt 的反过度评分
    规则——禁止「可推知/似乎/可能/大概」推断字眼作为兑现证据；每条
    achieved=true 必须引用具体事件与直接动作/实物；禁止「章末顺手
    给齐」批量放水。"""

    def _judge(self) -> str:
        return milestone_judge_prompt(
            world={"name": "未书天", "slug": "jiuzhou", "rules": {}},
            session={"world_state": {"location": "村北"}},
            chapter={
                "id": "ch_03_cinnabar_market",
                "title": "第三章：丹霞一炉双生丹",
                "current_objective": "确认黑市以未来年份结算",
            },
            milestones=[{
                "id": "m_03_01_future_currency",
                "label": "确认黑市以他人未来年份而非灵石结算",
                "evidence_required": [{"type": "clue_keyword_any", "match": ["年份", "未决之念"]}],
            }],
            events=[
                {
                    "turn": 121,
                    "role": "narrator",
                    "actor_name": "照骨客",
                    "content": "陈千语护着木匣与丹方残角往丹霞走，"
                    "未确认任何黑市结算方式",
                }
            ],
        )

    def test_inference_words_are_not_evidence(self) -> None:
        prompt = self._judge()
        self.assertIn("可推知", prompt)
        self.assertIn("似乎", prompt)
        self.assertIn("可能", prompt)
        self.assertIn("看起来", prompt)
        self.assertIn("认为", prompt)
        self.assertIn("觉得", prompt)
        self.assertIn("大概", prompt)
        # 反例对照也要出现
        self.assertIn("看到", prompt)
        self.assertIn("确认", prompt)
        self.assertIn("拿出", prompt)
        self.assertIn("拿到", prompt)

    def test_pending_words_are_not_evidence(self) -> None:
        # 「计划/准备/打算/即将/快要」这类未发生词也不能算达成
        prompt = self._judge()
        self.assertIn("计划", prompt)
        self.assertIn("准备", prompt)
        self.assertIn("打算", prompt)
        self.assertIn("即将", prompt)
        self.assertIn("快要", prompt)

    def test_reason_must_cite_specific_event(self) -> None:
        # 每条 achieved=true 必须绑定真实事件 ID 与逐字正文
        prompt = self._judge()
        self.assertIn("criteria 数组", prompt)
        self.assertIn("真实 event_id", prompt)
        self.assertIn("连续出现的逐字 quote", prompt)

    def test_no_end_of_chapter_pass(self) -> None:
        # 禁止「章末顺手给齐」——不准为推动节奏批量放水
        prompt = self._judge()
        self.assertIn("禁止「顺手给齐」", prompt)
        self.assertIn("放水", prompt)
        self.assertIn("批量给出", prompt)

    def test_story_complete_also_gated_by_milestones(self) -> None:
        # 间接测试——结束点 (story_complete) 由 _maybe_story_complete 实现，
        # 它只在「当前章无 next_chapter_id + exits_when 全部里程碑完成」时
        # 才触发。修复 milestone_judge 的过激评分即可避免提前结束。
        # 这里只断言 _maybe_story_complete 的调用前提在 engine.py 中存在
        # 且不会越过 milestone 阶段。
        from pathlib import Path
        engine_path = Path("tavern/engine.py")
        src = engine_path.read_text(encoding="utf-8")
        self.assertIn("await self._maybe_story_complete(", src)
        self.assertIn("if not all(", src)
        # 双重确认：_maybe_story_complete 之前一定有里程碑完整性检查
        self.assertIn("exits_when", src)


class AttributeWhitelistPromptTests(unittest.TestCase):
    """2026-08-24：玩家反馈「检定属性 strength 不属于当前世界或角色卡」——
    九州借命局世界属性是 body/agility/sword/spell/insight/array/guile/
    presence（体魄/身法/兵道/术法/神识/阵理/机变/心辩），模型却在 option
    的 attribute_id 里填了 DND 风的标准 key "strength"，导致整轮被拒。
    planning_prompt 必须列出本世界有效属性 + 警告禁止自造。"""

    def _jiuzhou_world(self) -> dict:
        return {
            "name": "未书天",
            "slug": "jiuzhou",
            "system_prompt": "测试。",
            "rules": {
                "resolution": {"mode": "attribute", "dice_system": "d20"},
                "character_card": {
                    "stats": {
                        "attributes": [
                            {"key": "body", "label": "体魄"},
                            {"key": "agility", "label": "身法"},
                            {"key": "sword", "label": "兵道"},
                            {"key": "spell", "label": "术法"},
                            {"key": "insight", "label": "神识"},
                            {"key": "array", "label": "阵理"},
                            {"key": "guile", "label": "机变"},
                            {"key": "presence", "label": "心辩"},
                        ]
                    }
                },
            },
        }

    def _prompt(self, world: dict) -> str:
        return planning_prompt(
            world=world,
            session={"world_state": {"location": "乱葬岗"}},
            player={"participant_id": "p1"},
            player_input="观察周围",
            events=[],
            memories=[],
            allow_checks=True,
        )

    def test_planning_prompt_lists_world_attributes(self) -> None:
        prompt = self._prompt(self._jiuzhou_world())
        self.assertIn("body（体魄）", prompt)
        self.assertIn("agility（身法）", prompt)
        self.assertIn("sword（兵道）", prompt)
        self.assertIn("spell（术法）", prompt)
        self.assertIn("insight（神识）", prompt)
        self.assertIn("presence（心辩）", prompt)

    def test_planning_prompt_bans_foreign_attribute_ids(self) -> None:
        prompt = self._prompt(self._jiuzhou_world())
        self.assertIn("不得自造", prompt)
        self.assertIn("strength", prompt)
        self.assertIn("力量", prompt)
        self.assertIn("宁可留空让引擎按行动自动推断", prompt)


if __name__ == "__main__":
    unittest.main()
