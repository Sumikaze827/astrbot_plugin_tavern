"""authored 属性模式：按玩家自拟设定生成属性分配（2026-09-20 新功能）。

需求：角色卡不再由世界包预设职业与属性，改成玩家在最后一段自拟设定里写背景，
插件据此生成该玩家的属性分配。

与 preset_stack 的区别只在**数字从哪来**：那边是预设选项 stat_bonus 的确定性
求和，这边是一次模型调用。写入字段（`stat_<key>`、`resolved_stat_total`、
`stat_generation_snapshot`）与下游完全一致，所以检定与能力衡量不用改。

本测试覆盖纯逻辑部分（配置校验、提示词、解析、兜底、落字段）。
"""

from __future__ import annotations

import unittest

from tavern.stat_generation import (
    AUTHORED_MODE,
    apply_authored_allocation,
    authored_fallback_allocation,
    authored_stat_config,
    authored_stat_prompt,
    parse_authored_allocation,
    uses_authored_stats,
    uses_generated_stats,
    uses_preset_stack_stats,
    validate_authored_stat_config,
)

ATTRIBUTES = [
    ("strength", "力量"),
    ("agility", "敏捷"),
    ("body", "体质"),
    ("mind", "智力"),
    ("will", "意志"),
]


def template(**overrides) -> dict:
    generation = {
        "mode": AUTHORED_MODE,
        "source_field": "background",
        "expected_total": 60,
        "min_per_stat": 3,
        "max_per_stat": 18,
    }
    generation.update(overrides)
    return {
        "fields": [
            {"key": "name", "type": "text", "required": True, "label": "姓名"},
            {
                "key": "background",
                "type": "long_text",
                "required": True,
                "label": "自拟设定",
            },
        ],
        "stats": {
            "mode": AUTHORED_MODE,
            "budget": 60,
            "attributes": [
                {
                    "key": key,
                    "label": label,
                    "minimum": 3,
                    "maximum": 18,
                    "description": f"{label}的说明",
                }
                for key, label in ATTRIBUTES
            ],
            "stat_generation": generation,
            "modifier_table": {"3": -2, "8": 0, "13": 2, "18": 4},
        },
    }


class ModeDetectionTests(unittest.TestCase):
    def test_authored_is_detected_and_not_confused_with_preset_stack(self) -> None:
        tpl = template()
        self.assertTrue(uses_authored_stats(tpl))
        self.assertTrue(uses_generated_stats(tpl))
        self.assertFalse(uses_preset_stack_stats(tpl))


class ConfigValidationTests(unittest.TestCase):
    def test_valid_config_reports_reachable_range(self) -> None:
        report = validate_authored_stat_config(template())
        self.assertEqual(report["floor"], 15)
        self.assertEqual(report["ceiling"], 90)
        self.assertEqual(report["attribute_count"], 5)

    def test_source_field_must_exist(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_authored_stat_config(template(source_field="nope"))
        self.assertIn("不存在的建卡字段", str(ctx.exception))

    def test_source_field_must_be_freeform(self) -> None:
        tpl = template()
        tpl["fields"][1]["type"] = "preset_select"
        with self.assertRaises(ValueError) as ctx:
            validate_authored_stat_config(tpl)
        self.assertIn("自由输入文本字段", str(ctx.exception))

    def test_source_field_must_be_required(self) -> None:
        tpl = template()
        tpl["fields"][1]["required"] = False
        with self.assertRaises(ValueError) as ctx:
            validate_authored_stat_config(tpl)
        self.assertIn("必须设为必填", str(ctx.exception))

    def test_unreachable_total_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_authored_stat_config(template(expected_total=200))
        self.assertIn("不可达", str(ctx.exception))

    def test_missing_source_field_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_authored_stat_config(template(source_field=""))
        self.assertIn("source_field", str(ctx.exception))


class PromptTests(unittest.TestCase):
    def test_prompt_carries_setting_attributes_and_budget(self) -> None:
        prompt = authored_stat_prompt(
            template(), {"background": "退役剑士，左臂有旧伤，识字不多。"}
        )
        self.assertIn("退役剑士", prompt)
        self.assertIn("力量", prompt)
        self.assertIn("总和必须正好等于 60", prompt)
        self.assertIn('"attributes"', prompt)
        # 属性区间必须写清楚，否则模型没有可依据的边界
        self.assertIn("3—18", prompt)


class DisplayTests(unittest.TestCase):
    """生成结果只报基础值：主/副属性加点还没选，不能说成最终值。"""

    def test_authored_result_is_rendered_as_base_values(self) -> None:
        from tavern.stat_generation import format_authored_stat_result

        tpl = template()
        fields = {"name": "五条悟", "background": "六眼"}
        resolved = apply_authored_allocation(
            tpl,
            fields,
            authored_fallback_allocation(tpl),
            reason="按设定给感知偏高",
        )
        text = format_authored_stat_result(resolved)
        self.assertIn("基础合计：60", text)
        self.assertIn("力量 12", text)
        self.assertIn("分配依据：按设定给感知偏高", text)
        self.assertNotIn("最终总和", text)
        self.assertIn("主属性", text)

    def test_card_prompt_dispatches_authored_result(self) -> None:
        """authored 的字段形状与 preset_stack 不同，走错渲染会打出总和 0。"""
        try:
            from tavern.presentation import format_card_prompt
        except ImportError as exc:  # 精简测试环境没有 astrbot
            self.skipTest(f"presentation 依赖 astrbot：{exc}")

        tpl = template()
        draft = {
            "template": tpl,
            "fields": {"name": "五条悟", "background": "六眼"},
            "current_step": 0,
            "world": {},
            "stat_generation_result": {
                "mode": AUTHORED_MODE,
                "raw": {"strength": 12, "agility": 12},
                "labels": {"strength": "力量", "agility": "敏捷"},
                "total": 24,
                "reason": "按设定",
            },
        }
        text = format_card_prompt(draft)
        self.assertIn("基础合计：24", text)
        self.assertIn("分配依据：按设定", text)
        self.assertNotIn("总和：0", text)


class ParseTests(unittest.TestCase):
    def test_valid_allocation_passes(self) -> None:
        parsed = parse_authored_allocation(
            template(),
            {
                "attributes": {
                    "strength": 16,
                    "agility": 14,
                    "body": 12,
                    "mind": 9,
                    "will": 9,
                },
                "reason": "剑士",
            },
        )
        self.assertEqual(sum(parsed.values()), 60)
        self.assertEqual(parsed["strength"], 16)

    def test_bad_allocations_are_rejected(self) -> None:
        cases = {
            "缺项": {"strength": 16, "agility": 14, "body": 12, "mind": 9},
            "越界": {
                "strength": 16,
                "agility": 14,
                "body": 12,
                "mind": 9,
                "will": 99,
            },
            "总和不对": {
                "strength": 16,
                "agility": 14,
                "body": 12,
                "mind": 9,
                "will": 10,
            },
            "非整数": {
                "strength": 16,
                "agility": 14,
                "body": 12,
                "mind": 9,
                "will": "9",
            },
            "布尔": {
                "strength": True,
                "agility": 14,
                "body": 12,
                "mind": 9,
                "will": 9,
            },
        }
        for label, attributes in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    parse_authored_allocation(
                        template(), {"attributes": attributes}
                    )


class FallbackTests(unittest.TestCase):
    def test_fallback_always_satisfies_the_contract(self) -> None:
        allocation = authored_fallback_allocation(template())
        self.assertEqual(sum(allocation.values()), 60)
        self.assertEqual(set(allocation), {key for key, _ in ATTRIBUTES})
        for value in allocation.values():
            self.assertTrue(3 <= value <= 18)

    def test_fallback_is_deterministic(self) -> None:
        self.assertEqual(
            authored_fallback_allocation(template()),
            authored_fallback_allocation(template()),
        )

    def test_fallback_respects_tight_bounds(self) -> None:
        tpl = template(expected_total=25, min_per_stat=5, max_per_stat=5)
        allocation = authored_fallback_allocation(tpl)
        self.assertEqual(allocation, {key: 5 for key, _ in ATTRIBUTES})


class ApplyTests(unittest.TestCase):
    def test_writes_base_where_profession_base_used_to_be(self) -> None:
        """基础值必须落在 profession_base_stats —— 加点逻辑读的就是这个键。"""
        tpl = template()
        fields = {"name": "五条悟", "background": "六眼与无下限术式"}
        resolved = apply_authored_allocation(
            tpl,
            fields,
            authored_fallback_allocation(tpl),
            reason="按设定给感知偏高",
            provider_id="p/x",
        )
        self.assertIn("profession_base_stats", fields)
        for key, _ in ATTRIBUTES:
            self.assertIn(f"stat_{key}", fields)
        self.assertEqual(fields["profession_base_stats"]["strength"], 12)
        self.assertEqual(fields["resolved_stat_total"], 60)
        snapshot = fields["stat_generation_snapshot"]
        self.assertEqual(snapshot["mode"], AUTHORED_MODE)
        self.assertEqual(snapshot["source_field"], "background")
        self.assertEqual(snapshot["reason"], "按设定给感知偏高")
        self.assertEqual(snapshot["provider_id"], "p/x")
        self.assertEqual(snapshot["total"], 60)
        self.assertEqual(snapshot["base_stats"]["strength"], 12)
        self.assertEqual(resolved["base"]["strength"], 12)

    def test_snapshot_keeps_the_setting_that_justified_the_numbers(self) -> None:
        """数字之后会被用来衡量玩家能力，必须能看出按哪段设定算的。"""
        tpl = template()
        fields = {"name": "五条悟", "background": "  六眼，超强感知  "}
        apply_authored_allocation(
            tpl, fields, authored_fallback_allocation(tpl)
        )
        self.assertEqual(
            fields["stat_generation_snapshot"]["source_text"],
            "六眼，超强感知",
        )

    def test_reapplying_replaces_previous_values(self) -> None:
        tpl = template()
        fields = {"name": "五条悟", "background": "六眼"}
        apply_authored_allocation(
            tpl,
            fields,
            {"strength": 18, "agility": 15, "body": 12, "mind": 3, "will": 12},
        )
        apply_authored_allocation(
            tpl, fields, authored_fallback_allocation(tpl)
        )
        self.assertEqual(fields["stat_strength"], 12)
        self.assertEqual(fields["resolved_stat_total"], 60)

    def test_config_reports_source_field(self) -> None:
        self.assertEqual(authored_stat_config(template())["source_field"], "background")


class PlayerBonusStillAppliesTests(unittest.TestCase):
    """主/副属性加点仍由玩家选：authored 只替代职业基础值那一块。

    世界形态照搬 re0：基础 50 ＋ 主 +7 ＋ 副 +3 ＝ 60，主副由玩家选。
    """

    def world(self) -> dict:
        card = template(expected_total=50)
        card["fields"] = [
            {"key": "name", "type": "text", "required": True, "label": "姓名"},
            {
                "key": "background",
                "type": "long_text",
                "required": True,
                "label": "自拟设定",
            },
            {
                "key": "primary_attribute",
                "type": "preset_select",
                "required": True,
                "label": "主属性",
            },
            {
                "key": "secondary_attribute",
                "type": "preset_select",
                "required": True,
                "label": "副属性",
            },
        ]
        card["stats"]["base_budget"] = 50
        card["stats"]["primary_bonus"] = 7
        card["stats"]["secondary_bonus"] = 3
        card["stats"]["bonus_choices"] = [
            {"field": "primary_attribute", "bonus": 7},
            {"field": "secondary_attribute", "bonus": 3},
        ]
        card["stats"]["total_validation"] = {
            "base_total": 50,
            "final_total": 60,
        }
        for attribute in card["stats"]["attributes"]:
            attribute["minimum"] = 0
            attribute["maximum"] = 20
        return {"rules": {"character_card": card}}

    def test_base_from_model_then_player_bonuses(self) -> None:
        from tavern.card_lifecycle import validate_card_revision
        from tavern.lifecycle import card_template, resolve_profession_stats

        world = self.world()
        tpl = card_template(world)
        fields = {
            "name": "五条悟",
            "background": "六眼与无下限术式",
            "primary_attribute": "力量",
            "secondary_attribute": "敏捷",
        }
        base = {
            "strength": 10,
            "agility": 9,
            "body": 11,
            "mind": 10,
            "will": 10,
        }
        apply_authored_allocation(tpl, fields, base, reason="按设定")
        self.assertEqual(fields["profession_base_stats"], base)
        self.assertEqual(fields["resolved_stat_total"], 50)

        resolved = resolve_profession_stats(tpl, fields, require_complete=True)
        self.assertEqual(resolved["raw"]["strength"], 17)  # 10 + 7
        self.assertEqual(resolved["raw"]["agility"], 12)  # 9 + 3
        self.assertEqual(resolved["effective_total"], 60)

        # 改卡路径同样保留加点结果
        revised = validate_card_revision(
            world, fields, {"raw": resolved["raw"]}
        )
        self.assertEqual(revised["profile"]["stat_strength"], 17)
        self.assertEqual(revised["stats"]["raw"]["agility"], 12)

    def test_bonus_requires_primary_and_secondary(self) -> None:
        from tavern.card_lifecycle import validate_card_revision
        from tavern.lifecycle import card_template, resolve_profession_stats

        world = self.world()
        tpl = card_template(world)
        fields = {"name": "五条悟", "background": "设定"}
        apply_authored_allocation(
            tpl, fields, {"strength": 10, "agility": 10, "body": 10, "mind": 10, "will": 10}
        )
        with self.assertRaises(ValueError):
            resolve_profession_stats(tpl, fields, require_complete=True)
        with self.assertRaises(ValueError):
            validate_card_revision(world, fields, {})

    def test_missing_base_gives_an_actionable_error(self) -> None:
        """自拟设定还没生成基础值时，不能只报「请先选择预设」。"""
        from tavern.lifecycle import card_template, resolve_profession_stats

        tpl = card_template(self.world())
        with self.assertRaises(ValueError) as ctx:
            resolve_profession_stats(
                tpl,
                {
                    "name": "五条悟",
                    "background": "设定",
                    "primary_attribute": "力量",
                    "secondary_attribute": "敏捷",
                },
                require_complete=True,
            )
        self.assertIn("自拟设定尚未生成基础属性", str(ctx.exception))

    def test_source_field_may_be_the_last_field(self) -> None:
        """自拟设定排在主/副属性之后（原卡的最后一段）时，也要能算完。

        这种情况下玩家选主副属性时基础值还没生成，所以建卡流程必须先跳过加点
        解析（仓储层的 base_ready 分支），等自拟设定填完再一次性算出来。
        这里只锁定「基础值就绪后，字段顺序不影响解析结果」这一半契约。
        """
        from tavern.card_lifecycle import validate_card_revision
        from tavern.lifecycle import card_template, resolve_profession_stats

        world = self.world()
        world["rules"]["character_card"]["fields"] = [
            {"key": "name", "type": "text", "required": True, "label": "姓名"},
            {
                "key": "primary_attribute",
                "type": "preset_select",
                "required": True,
                "label": "主属性",
            },
            {
                "key": "secondary_attribute",
                "type": "preset_select",
                "required": True,
                "label": "副属性",
            },
            {
                "key": "background",
                "type": "long_text",
                "required": True,
                "label": "自拟设定",
            },
        ]
        tpl = card_template(world)
        self.assertEqual(
            [item["key"] for item in tpl["fields"]],
            ["name", "primary_attribute", "secondary_attribute", "background"],
        )
        fields = {
            "name": "五条悟",
            "primary_attribute": "力量",
            "secondary_attribute": "敏捷",
            "background": "六眼与无下限术式",
        }
        apply_authored_allocation(
            tpl,
            fields,
            {
                "strength": 10,
                "agility": 9,
                "body": 11,
                "mind": 10,
                "will": 10,
            },
        )
        resolved = resolve_profession_stats(tpl, fields, require_complete=True)
        self.assertEqual(resolved["raw"]["strength"], 17)
        revised = validate_card_revision(world, fields, {"raw": resolved["raw"]})
        self.assertEqual(revised["profile"]["stat_strength"], 17)


class PromptWordingTests(unittest.TestCase):
    """authored 的卡不该再对玩家说「职业」——那里已经没有职业了。"""

    def template(self) -> dict:
        return PlayerBonusStillAppliesTests().world()

    def test_step_prompts_say_self_authored_not_profession(self) -> None:
        try:
            from tavern.presentation import format_card_prompt
        except ImportError as exc:  # 精简测试环境没有 astrbot
            self.skipTest(f"presentation 依赖 astrbot：{exc}")
        from tavern.lifecycle import card_template, resolve_profession_stats

        template = card_template(self.template())
        fields = {
            "name": "五条悟",
            "background": "六眼与无下限术式",
            "primary_attribute": "力量",
        }
        apply_authored_allocation(
            template,
            fields,
            {
                "strength": 10,
                "agility": 9,
                "body": 11,
                "mind": 10,
                "will": 10,
            },
        )
        # 主属性已经选好，下一步是选副属性
        draft = {
            "template": template,
            "fields": fields,
            "current_step": 3,
            "world": {},
        }
        text = format_card_prompt(draft)
        self.assertIn("基础值来源：按你的自拟设定生成", text)
        self.assertNotIn("职业", text)

        # 基础值还没生成时也不能说「请先重新选择职业」
        unresolved = {"name": "五条悟", "background": "六眼"}
        draft = {
            "template": template,
            "fields": unresolved,
            "current_step": 2,
            "world": {},
        }
        text = format_card_prompt(draft)
        self.assertNotIn("请先重新选择职业", text)
        self.assertIn("请先填写你的自拟设定", text)

        # 选主属性那一步：报的是自拟设定来的基础值，且数值正确
        fields = {"name": "五条悟", "background": "六眼与无下限术式"}
        apply_authored_allocation(
            template,
            fields,
            {
                "strength": 10,
                "agility": 9,
                "body": 11,
                "mind": 10,
                "will": 10,
            },
        )
        draft = {
            "template": template,
            "fields": fields,
            "current_step": 2,
            "world": {},
        }
        text = format_card_prompt(draft)
        self.assertIn("基础属性（来源：自拟设定）", text)
        self.assertIn("力量：10", text)
        self.assertNotIn("当前职业", text)
        resolved = resolve_profession_stats(
            template, {**fields, "primary_attribute": "力量"}, require_complete=False
        )
        self.assertEqual(resolved["raw"]["strength"], 17)


if __name__ == "__main__":
    unittest.main()
