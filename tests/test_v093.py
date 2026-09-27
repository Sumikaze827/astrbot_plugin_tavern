from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from tavern.api import ExtensionRegistry, HookRegistry, TavernPublicAPI
from tavern.config import TavernConfig
from tavern.card_wizard import choose_option, paged_options, preset_options
from tavern.constants import (
    CHARACTER_CARD_TEMPLATE_VERSION,
    DATABASE_SCHEMA_VERSION,
    DEFAULT_WORLD,
    DEFAULT_WORLD_SLUG,
    NPC_IMPORT_TEMPLATE_VERSION,
    PLUGIN_VERSION,
    SESSION_PREPARING,
    SESSION_RUNNING,
    TEMPLATE_BUNDLE_VERSION,
)
from tavern.database import TavernDatabase
from tavern.diagnostics import build_diagnostic_report
from tavern.engine import TavernBusyError, TavernEngine
from tavern.events import EventBroker
from tavern.narrative_quality import inspect_narrative
from tavern.operations import operation_key, recovery_summary
from tavern.lifecycle import card_template, format_choices, normalize_choices
from tavern.prompts import (
    checked_resolution_prompt,
    choice_generation_prompt,
    choice_repair_prompt,
    choice_system_prompt,
    planning_prompt,
    repair_prompt,
    system_prompt,
)
from tavern.resolution import CheckRequest, roll_check
from tavern.world_contract import WORLD_SCHEMA_VERSION, validate_world_contract
from tavern.world_migration import compare_world_contracts
from tavern.world_preflight import inspect_world_package
from tavern.world_import import world_edit_payload, world_import_payload
from tavern.stat_generation import (
    assess_preset_stack_migration,
    calculate_preset_stack_stats,
    format_preset_stack_result,
    sync_preset_stack_fields,
    validate_stat_generation_config,
)


ROOT = Path(__file__).resolve().parents[1]
TIDE_WORLD_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "where-winds-meet-tideless-script.world.json"
)


class FakeContext:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        return "provider-test"

    async def llm_generate(self, **kwargs):
        if '你是人物行为与对白检查器' in (kwargs.get('system_prompt') or ''):
            return SimpleNamespace(completion_text='{"ok":true,"reason":"fixture review"}')
        self.calls.append(kwargs)
        return SimpleNamespace(completion_text=self.outputs.pop(0))


class V093ContractTests(unittest.TestCase):
    def test_versions_are_single_current_baseline(self) -> None:
        metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text("utf-8"))
        self.assertEqual(PLUGIN_VERSION, "0.12.0")
        self.assertEqual(metadata["version"], "0.12.0")
        self.assertEqual(metadata["author"], "Ghostberry")
        self.assertEqual(DATABASE_SCHEMA_VERSION, 12)

    def test_builtin_world_passes_strict_preflight(self) -> None:
        report = inspect_world_package(DEFAULT_WORLD)
        self.assertTrue(report["compatible"])
        self.assertEqual(DEFAULT_WORLD["world_content_version"], "3.0.0")
        self.assertEqual(report["summary"]["schema_version"], 5)
        self.assertEqual(report["summary"]["stats_mode"], "preset")
        self.assertEqual(report["summary"]["migrations"], 0)

    def test_template_manifest_tracks_runtime_interfaces(self) -> None:
        manifest = json.loads(
            (ROOT / "templates/template-manifest.json").read_text("utf-8")
        )
        self.assertEqual(manifest["template_bundle_version"], TEMPLATE_BUNDLE_VERSION)
        self.assertEqual(manifest["compatible_plugin_version"], PLUGIN_VERSION)
        self.assertEqual(manifest["world_schema_version"], WORLD_SCHEMA_VERSION)
        self.assertEqual(
            manifest["character_card_template_version"],
            CHARACTER_CARD_TEMPLATE_VERSION,
        )
        self.assertEqual(
            manifest["npc_import_template_version"], NPC_IMPORT_TEMPLATE_VERSION
        )
        for relative_path in manifest["files"].values():
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

    def test_generic_world_template_passes_current_preflight(self) -> None:
        world = json.loads(
            (ROOT / "templates/world-package.template.json").read_text("utf-8")
        )
        metadata = world["template_metadata"]
        self.assertEqual(metadata["template_bundle_version"], TEMPLATE_BUNDLE_VERSION)
        self.assertEqual(metadata["compatible_plugin_version"], PLUGIN_VERSION)
        self.assertEqual(world["world_schema_version"], WORLD_SCHEMA_VERSION)
        self.assertEqual(
            world["rules"]["character_card"]["version"],
            CHARACTER_CARD_TEMPLATE_VERSION,
        )
        report = inspect_world_package(world)
        self.assertTrue(report["compatible"], report["issues"])
        self.assertEqual(report["summary"]["stats_mode"], "manual")
        self.assertEqual(report["summary"]["resolution_mode"], "attribute")

    def test_preset_stack_template_and_tide_world_pass_full_preflight(self) -> None:
        template_world = json.loads(
            (
                ROOT
                / "templates"
                / "world-package-preset-stack.template.json"
            ).read_text("utf-8")
        )
        template_report = inspect_world_package(template_world)
        self.assertTrue(template_report["compatible"], template_report["issues"])
        self.assertEqual(template_report["summary"]["stats_mode"], "preset_stack")
        self.assertEqual(
            validate_stat_generation_config(card_template(template_world))[
                "combination_count"
            ],
            8,
        )

        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        report = inspect_world_package(tide)
        self.assertTrue(report["compatible"], report["issues"])
        self.assertEqual(tide["world_content_version"], "2.0.0")
        self.assertEqual(tide["minimum_plugin_version"], "0.10.0")
        self.assertEqual(report["summary"]["schema_version"], 4)
        self.assertEqual(report["summary"]["stats_mode"], "preset_stack")
        self.assertEqual(report["summary"]["preset_stack_combinations"], 630)
        validation = validate_stat_generation_config(card_template(tide))
        self.assertEqual(validation["combination_count"], 7 * 9 * 10)

    def test_world_import_keeps_preset_stack_minimum_plugin_version(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        imported = world_import_payload(tide)
        self.assertEqual(imported["minimum_plugin_version"], "0.10.0")
        self.assertEqual(imported["world_content_version"], "2.0.0")
        self.assertEqual(imported["template_metadata"], tide["template_metadata"])
        validate_world_contract(imported)

    def test_preset_stack_editor_contract_survives_normalization(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        normalized = card_template(tide)
        self.assertEqual(normalized["stats"]["mode"], "preset_stack")
        self.assertEqual(
            normalized["stat_generation"],
            normalized["stats"]["stat_generation"],
        )
        validate_stat_generation_config(normalized)

    def test_world_editor_merge_keeps_contract_and_extension_fields(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        current = {
            **tide,
            "id": "world-test",
            "revision": 3,
            "card_template": card_template(tide),
            "player_limits": {"maximum": 8},
            "characters": [{"name": "runtime-only"}],
        }
        submitted = {
            "id": "world-test",
            "revision": 3,
            "slug": tide["slug"],
            "name": "编辑后的名称",
            "description": tide["description"],
            "system_prompt": tide["system_prompt"],
            "opening_scene": tide["opening_scene"],
            "rules": tide["rules"],
            "initial_state": tide["initial_state"],
        }
        merged = world_edit_payload(submitted, current)
        self.assertEqual(merged["minimum_plugin_version"], "0.10.0")
        self.assertEqual(merged["world_content_version"], "2.0.0")
        self.assertEqual(merged["template_metadata"], tide["template_metadata"])
        self.assertNotIn("card_template", merged)
        self.assertNotIn("player_limits", merged)
        self.assertNotIn("characters", merged)
        validate_world_contract(merged)

    def test_tide_reference_combination_is_exact_and_traceable(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        template = card_template(tide)
        fields = {
            "origin_region": "江南",
            "social_identity": "斥候",
            "martial_flow": "牵丝·玉",
        }
        resolved = calculate_preset_stack_stats(
            template,
            fields,
            require_complete=True,
        )
        self.assertEqual(
            resolved["raw"],
            {"ti": 5, "yu": 5, "min": 9, "shi": 7, "jin": 5},
        )
        self.assertEqual(resolved["effective_total"], 31)
        self.assertEqual(len(resolved["sources"]), 3)
        sync_preset_stack_fields(template, fields, require_complete=True)
        self.assertEqual(fields["stat_min"], 9)
        self.assertEqual(fields["resolved_stat_total"], 31)
        self.assertEqual(
            len(fields["stat_generation_snapshot"]["sources"]), 3
        )

        fields["martial_flow"] = "鸣金·虹"
        changed = sync_preset_stack_fields(
            template,
            fields,
            require_complete=True,
        )
        self.assertEqual(changed["raw"]["min"], 7)
        self.assertEqual(changed["raw"]["shi"], 8)
        self.assertEqual(changed["raw"]["jin"], 6)

    def test_preset_stack_rejects_invalid_bonus_and_never_silently_migrates(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        broken = copy.deepcopy(tide)
        broken["rules"]["character_card"]["preset_sets"][
            "origin_regions"
        ][0]["stat_bonus"] = {"unknown": 2}
        report = inspect_world_package(broken)
        self.assertFalse(report["compatible"])
        self.assertTrue(
            any("未知属性" in item["message"] for item in report["issues"])
        )

        template = card_template(tide)
        profile = {
            "origin_region": "江南",
            "social_identity": "斥候",
            "martial_flow": "牵丝·玉",
        }
        safe = assess_preset_stack_migration(
            template,
            profile,
            {"raw": {"ti": 5, "yu": 5, "min": 9, "shi": 7, "jin": 5}},
        )
        unsafe = assess_preset_stack_migration(
            template,
            profile,
            {"raw": {"ti": 5, "yu": 5, "min": 8, "shi": 8, "jin": 5}},
        )
        self.assertEqual(safe["status"], "snapshot_backfill_safe")
        self.assertEqual(unsafe["status"], "admin_confirmation_required")

    def test_preset_stack_prompt_skips_manual_stat_pages(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        template = card_template(tide)
        self.assertFalse(
            any(str(item.get("key") or "").startswith("stat_") for item in template["fields"])
        )
        fields = {
            "origin_region": "江南",
            "social_identity": "斥候",
            "martial_flow": "牵丝·玉",
        }
        resolved = sync_preset_stack_fields(
            template,
            fields,
            require_complete=True,
        )
        prompt = format_preset_stack_result(resolved)
        self.assertIn("【角色五维已自动生成】", prompt)
        self.assertIn("体 5｜御 5｜敏 9｜势 7｜劲 5", prompt)
        self.assertNotIn("角色数值 1/5", prompt)

    def test_npc_template_matches_current_import_contract(self) -> None:
        payload = json.loads(
            (ROOT / "templates/npc-import.template.json").read_text("utf-8")
        )
        metadata = payload["template_metadata"]
        self.assertEqual(metadata["template_bundle_version"], TEMPLATE_BUNDLE_VERSION)
        self.assertEqual(
            metadata["npc_import_template_version"], NPC_IMPORT_TEMPLATE_VERSION
        )
        self.assertTrue(str(payload.get("world_slug") or "").strip())
        self.assertIsInstance(payload.get("items"), list)
        self.assertTrue(payload["items"])
        for item in payload["items"]:
            self.assertTrue(str(item.get("name") or "").strip())
            self.assertTrue(str(item.get("role") or "").strip())
            self.assertIsInstance(item.get("profile"), dict)
            self.assertIsInstance(item.get("prompt"), str)

    def test_presets_are_revealed_only_at_the_current_step(self) -> None:
        template = card_template(DEFAULT_WORLD)
        self.assertEqual(preset_options(template, template["fields"][0], {}), [])
        species_field = next(
            item for item in template["fields"] if item["key"] == "species"
        )
        species_page = paged_options(template, species_field, {})
        species_names = {item["label"] for item in species_page["options"]}
        self.assertEqual(species_page["total_pages"], 2)
        self.assertIn("人类", species_names)
        self.assertNotIn("骑士", species_names)

    def test_preset_paging_and_stable_id_selection(self) -> None:
        template = card_template(DEFAULT_WORLD)
        profession = next(
            item for item in template["fields"] if item["key"] == "profession"
        )
        page = paged_options(template, profession, {})
        self.assertEqual(len(page["items"]), 5)
        selected = choose_option(template, profession, {}, "knight")
        self.assertEqual(selected["value"], "骑士")

    def test_risk_policy_overrides_model_supplied_dc(self) -> None:
        choices = copy.deepcopy(DEFAULT_WORLD["rules"]["opening_choices"])
        choices[1]["difficulty"] = 25
        choices[1]["check"]["difficulty"] = 25
        normalized = normalize_choices(choices, DEFAULT_WORLD)
        self.assertEqual(normalized[1]["difficulty"], 9)
        self.assertEqual(normalized[2]["difficulty"], 13)

    def test_safe_check_is_deterministically_normalized_without_retry(self) -> None:
        choices = copy.deepcopy(DEFAULT_WORLD["rules"]["opening_choices"])
        choices[0]["check"] = {
            "required": True,
            "attribute_id": "perception",
            "difficulty": 25,
        }
        choices[0]["requires_check"] = True
        normalized = normalize_choices(choices, DEFAULT_WORLD)
        self.assertFalse(normalized[0]["requires_check"])
        self.assertIsNone(normalized[0]["check"])

    def test_attribute_label_survives_storage_normalization(self) -> None:
        normalized = normalize_choices(
            DEFAULT_WORLD["rules"]["opening_choices"], DEFAULT_WORLD
        )
        restored = normalize_choices(normalized)
        checked = next(item for item in restored if item["requires_check"])
        self.assertNotEqual(checked["check_stat"], checked["check_label"])
        self.assertIn(
            f'需“{checked["check_label"]}”检定',
            format_choices("测试角色", restored),
        )
        legacy = copy.deepcopy(normalized)
        for item in legacy:
            if not item.get("requires_check"):
                continue
            item["check_label"] = item["check_stat"]
            if item.get("check"):
                item["check"].pop("attribute_label", None)
        recovered = normalize_choices(legacy, DEFAULT_WORLD)
        recovered_checked = next(
            item for item in recovered if item["requires_check"]
        )
        self.assertNotEqual(
            recovered_checked["check_stat"],
            recovered_checked["check_label"],
        )

    def test_context_compiler_omits_authoring_payloads_and_duplicates(self) -> None:
        narrative_system = system_prompt(DEFAULT_WORLD, allow_check=False)
        self.assertNotIn('"character_card"', narrative_system)
        self.assertNotIn('"professions"', narrative_system)
        self.assertNotIn("<resident_characters>", narrative_system)
        self.assertLess(len(narrative_system), 18000)

        participant = {
            "id": "participant-1",
            "character_name": "测试角色",
            "card_profile": {"background": "简短背景"},
            "card_stats": {"perception": 8},
            "runtime_state": {"statuses": []},
            "draft_profile": {"should_not_leak": "X" * 5000},
            "binding_code": "secret-code",
        }
        choice_prompt = choice_generation_prompt(
            world=DEFAULT_WORLD,
            session={"world_state": {"location": "鸦渡镇"}},
            participant=participant,
            events=[],
        )
        self.assertNotIn("should_not_leak", choice_prompt)
        self.assertNotIn("secret-code", choice_prompt)
        self.assertNotIn('"character_card"', choice_prompt)
        choice_system = choice_system_prompt(DEFAULT_WORLD)
        self.assertLess(len(choice_system) + len(choice_prompt), 18000)
        worst_repair = choice_repair_prompt(
            "X" * 8000,
            "选项结构错误",
            world=DEFAULT_WORLD,
            participant=participant,
        )
        self.assertLess(len(choice_system) + len(worst_repair), 11000)

    def test_repair_prompt_does_not_repeat_original_context(self) -> None:
        marker = "DO_NOT_REPEAT_CONTEXT"
        repaired = repair_prompt("{bad}", "字段错误", marker * 10000)
        self.assertNotIn(marker, repaired)
        self.assertLess(len(repaired), 2000)

    def test_plain_language_visual_anchors_cover_all_narrative_paths(self) -> None:
        system = system_prompt(DEFAULT_WORLD, allow_check=False)
        self.assertIn("陌生专名、古称或专业词第一次出现时", system)
        self.assertIn("不得只用生僻建筑术语代替物体与空间", system)

        planning = planning_prompt(
            world=DEFAULT_WORLD,
            session={"world_state": {"location": "测试大厅"}},
            player={"id": "p1", "character_name": "测试角色"},
            player_input="观察门口",
            events=[],
            memories=[],
            allow_checks=False,
        )
        self.assertIn("殿前的高台和石阶（丹墀）", planning)
        self.assertIn("每个新场景先让玩家分清主要空间", planning)

        checked = checked_resolution_prompt(
            world=DEFAULT_WORLD,
            session={"world_state": {"location": "测试大厅"}},
            player={"id": "p1", "character_name": "测试角色"},
            player_input="推开石门",
            events=[],
            memories=[],
            check={"risk": "controlled"},
            dice={"outcome": "success"},
        )
        self.assertIn("陌生物体必须先用日常名称建立画面", checked)
        self.assertIn("斜撑石架或神像底座", checked)

        choice = choice_generation_prompt(
            world=DEFAULT_WORLD,
            session={"world_state": {"location": "测试大厅"}},
            participant={
                "id": "p1",
                "character_name": "测试角色",
                "card_profile": {},
                "card_stats": {},
                "runtime_state": {},
            },
            events=[],
        )
        self.assertIn("不得让玩家先查词才能知道自己在选择什么", choice)

        repaired = repair_prompt("{}", "字段错误", "unused")
        self.assertIn("不得为了缩短文字删掉已有的通俗解释", repaired)

        authoring = (ROOT / "docs" / "WORLD_AUTHORING.md").read_text("utf-8")
        practices = (
            ROOT / "docs" / "WORLD_AUTHORING_BEST_PRACTICES.md"
        ).read_text("utf-8")
        self.assertIn("叙事可读性与画面锚点", authoring)
        self.assertIn("陌生题材的白话化测试", practices)

    def test_low_latency_defaults_remain_current(self) -> None:
        config = TavernConfig.from_mapping({})
        self.assertEqual(config.temperature, 0.5)
        self.assertEqual(config.max_tokens, 1400)
        self.assertEqual(config.recent_turns, 6)
        self.assertEqual(config.memory_limit, 6)
        budget = DEFAULT_WORLD["rules"]["context_budget"]
        self.assertEqual(
            {key: budget[key] for key in ("recent_turns", "memories", "active_npcs", "ledger_items")},
            {"recent_turns": 6, "memories": 6, "active_npcs": 6, "ledger_items": 8},
        )

    def test_machine_roll_format_matches_public_receipt(self) -> None:
        request = CheckRequest(
            stat="魅力",
            reason="交涉",
            difficulty=9,
            modifier=1,
        )
        with patch("tavern.resolution.secrets.randbelow", return_value=17):
            dice = roll_check(request)
        text = TavernEngine._format_dice_result(dice, "魅力")
        self.assertEqual(
            text,
            "🎲【魅力检定】d20 · 常规\n"
            "· 属性修正 = +1\n"
            "· 骰子(d20) = 18\n"
            "· 检定结果 = 骰子 18 + 修正 +1 = 19\n"
            "· 难度阈值(DC) = 9\n"
            "· 判定 = 大成功（结果 19 ≥ 难度 9，余量 +10）",
        )

    def test_builtin_d20_is_registered_in_runtime_engine(self) -> None:
        registry = ExtensionRegistry()
        engine = TavernEngine(
            context=FakeContext([]),
            database=object(),
            config_provider=lambda: TavernConfig.from_mapping({}),
            broker=EventBroker(),
            extensions=registry,
        )
        self.assertIsNotNone(registry.resolve("dice_system", "d20"))
        unavailable = copy.deepcopy(DEFAULT_WORLD)
        unavailable["rules"]["resolution"]["dice_system"] = "percentile"
        with self.assertRaisesRegex(Exception, "没有注册该骰制"):
            engine.validate_world_runtime(unavailable)
        request = CheckRequest(
            stat="魅力", reason="交涉", difficulty=9, modifier=1
        )
        with patch("tavern.resolution.secrets.randbelow", return_value=17):
            dice = roll_check(request)
        notices: list[str] = []
        asyncio.run(
            engine._publish_locked_check_progress(notices.append, dice, "魅力")
        )
        # 0.13.x：先回执确认收到、再出骰面（避免「先摇点后通知」倒置）。
        self.assertEqual(
            notices,
            [
                "【酒馆】已收到你的选择，正在投骰并生成后续内容……",
                "🎲【魅力检定】d20 · 常规\n"
                "· 属性修正 = +1\n"
                "· 骰子(d20) = 18\n"
                "· 检定结果 = 骰子 18 + 修正 +1 = 19\n"
                "· 难度阈值(DC) = 9\n"
                "· 判定 = 大成功（结果 19 ≥ 难度 9，余量 +10）",
            ],
        )

    def test_old_or_missing_world_protocol_is_rejected(self) -> None:
        for value in (None, 1):
            world = copy.deepcopy(DEFAULT_WORLD)
            if value is None:
                world.pop("world_schema_version", None)
                world["rules"].pop("world_schema_version", None)
            else:
                world["world_schema_version"] = value
                world["rules"]["world_schema_version"] = value
            with self.assertRaisesRegex(ValueError, "仅接受世界包协议 v2、v3、v4 或 v5"):
                validate_world_contract(world)

    def test_world_migration_never_hot_replaces(self) -> None:
        candidate = copy.deepcopy(DEFAULT_WORLD)
        report = compare_world_contracts(DEFAULT_WORLD, candidate)
        self.assertFalse(report["safe_for_live_replace"])
        self.assertTrue(report["safe_for_clone"])

    def test_quality_and_recovery_guards_remain_active(self) -> None:
        quality = inspect_narrative("风吹过大厅。", [{"text": "观察"}, {"text": "观察"}])
        self.assertFalse(quality["passed"])
        recovery = recovery_summary(
            [], session_state="running", has_active_choices=False, has_active_vote=False
        )
        self.assertEqual(recovery["recommended_action"], "rebuild_choices")

    def test_operation_key_is_stable_without_plaintext(self) -> None:
        first = operation_key(
            "s", "turn", turn_no=2, actor_id="u", source_id="m", payload={"input": "秘密"}
        )
        second = operation_key(
            "s", "turn", turn_no=2, actor_id="u", source_id="m", payload={"input": "秘密"}
        )
        self.assertEqual(first, second)
        self.assertNotIn("秘密", first)

    def test_extension_registry_is_named_and_collision_safe(self) -> None:
        registry = ExtensionRegistry()
        provider = lambda payload: payload
        registry.register_dice_system("percentile", provider)
        self.assertIs(registry.resolve("dice_system", "percentile"), provider)
        with self.assertRaisesRegex(ValueError, "已注册"):
            registry.register_dice_system("percentile", provider)

    def test_logo_and_static_cache_version_are_current(self) -> None:
        html = (ROOT / "pages/console/index.html").read_text("utf-8")
        style = (ROOT / "pages/console/style.css").read_text("utf-8")
        logo = (ROOT / "logo.svg").read_text("utf-8")
        self.assertIn("style.css?v=0.12.0", html)
        self.assertIn("app.js?v=0.12.0", html)
        self.assertIn("button-secondary", html)
        self.assertIn(".button-secondary", style)
        self.assertIn("viewBox=\"0 0 256 256\"", logo)

    def test_readme_and_changelog_have_separate_roles(self) -> None:
        readme = (ROOT / "README.md").read_text("utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
        self.assertNotIn("## v0.8.0", readme)
        for section in changelog.split("\n## v")[1:]:
            self.assertIn("### 更新摘要", section)
            self.assertIn("### 详细更新说明", section)


class V093DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = TavernDatabase(self.root)
        self.session = await self.database.ensure_session(
            "qq", "group-v090", "qq:group-v090", DEFAULT_WORLD_SLUG, "admin"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_new_database_ignores_old_filename(self) -> None:
        self.assertEqual(self.database.path.name, "catalog_v090.sqlite3")
        self.assertFalse((self.root / "catalog.sqlite3").exists())
        with self.database._connect() as connection:
            schema = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(schema), DATABASE_SCHEMA_VERSION)

    async def test_private_card_message_is_consumed_once(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
        )
        reserved = await self.database.reserve_participant(
            self.session["id"], "user-idempotent", "测试玩家"
        )
        origin = "private:user-idempotent"
        await self.database.bind_card_code(
            reserved["binding_code"], "user-idempotent", origin
        )
        first = await self.database.fill_card_draft(
            origin, "灰鸦", source_event_id="private-message-1"
        )
        repeated = await self.database.fill_card_draft(
            origin, "灰鸦", source_event_id="private-message-1"
        )
        self.assertEqual(first["current_step"], 1)
        self.assertEqual(repeated["current_step"], 1)
        self.assertTrue(repeated["duplicate"])

    async def test_tide_card_auto_generates_and_persists_sources(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        await self.database.save_world(tide, "admin")
        session = await self.database.ensure_session(
            "qq",
            "group-tide-v093",
            "qq:group-tide-v093",
            tide["slug"],
            "admin",
        )
        await self.database.transition_session(
            session["id"], SESSION_PREPARING, "admin"
        )
        reserved = await self.database.reserve_participant(
            session["id"], "user-tide", "潮生"
        )
        origin = "private:user-tide"
        await self.database.bind_card_code(
            reserved["binding_code"], "user-tide", origin
        )
        draft = await self.database.card_draft_for_private(origin)
        selected = {
            "name": "潮生",
            "code": "CS",
            "origin_region": "江南",
            "social_identity": "斥候",
            "martial_flow": "牵丝·玉",
        }
        generated_result = None
        while draft["current_step"] < len(draft["template"]["fields"]):
            field = draft["template"]["fields"][draft["current_step"]]
            key = str(field["key"])
            value = selected.get(key)
            if value is None:
                options = preset_options(draft["template"], field, draft["fields"])
                if options:
                    option_index = 1 if key == "life_skill_secondary" else 0
                    value = options[min(option_index, len(options) - 1)]["value"]
                else:
                    value = "测试资料"
            draft = await self.database.fill_card_draft(origin, str(value))
            generated_result = draft.get("stat_generation_result") or generated_result

        self.assertIsNotNone(generated_result)
        self.assertEqual(
            generated_result["raw"],
            {"ti": 5, "yu": 5, "min": 9, "shi": 7, "jin": 5},
        )
        self.assertFalse(
            any(
                str(item.get("key") or "").startswith("stat_")
                for item in draft["template"]["fields"]
            )
        )
        submitted = await self.database.confirm_card_draft(origin)
        roster = await self.database.list_roster(session["id"])
        card = next(item for item in roster if item["id"] == submitted["id"])
        self.assertEqual(card["card_stats"]["raw"]["min"], 9)
        self.assertEqual(card["card_stats"]["modifiers"]["min"], 2)
        self.assertEqual(
            len(card["card_stats"]["stat_generation_snapshot"]["sources"]),
            3,
        )
        modifier = await self.database.authoritative_modifier(
            session["id"], "user-tide", "敏"
        )
        self.assertTrue(modifier["matched"])
        self.assertEqual(modifier["modifier"], 2)
        await self.database.review_character_card(
            session["id"], submitted["id"], True, "admin"
        )
        revision = await self.database.request_card_revision(
            session["id"],
            submitted["id"],
            {"martial_flow": "鸣金·虹"},
            {},
            "user-tide",
            "更换主修流派",
        )
        await self.database.review_card_revision(
            revision["id"], True, "admin", "同意重算"
        )
        updated_roster = await self.database.list_roster(session["id"])
        updated = next(
            item for item in updated_roster if item["id"] == submitted["id"]
        )
        self.assertEqual(updated["card_stats"]["raw"]["min"], 7)
        self.assertEqual(updated["card_stats"]["raw"]["shi"], 8)
        self.assertEqual(updated["card_stats"]["raw"]["jin"], 6)

    async def test_timer_policy_and_play_time_are_off_by_default(self) -> None:
        policy = await self.database.get_timer_policy(self.session["id"])
        self.assertFalse(policy["global_enabled"])
        self.assertTrue(all(not value for value in policy["switches"].values()))
        config = await self.database.get_instance_config(self.session["id"])
        rules = config["time_rules"]
        for key in (
            "preparation_timeout_seconds", "ready_timeout_seconds",
            "turn_timeout_seconds",
            "vote_round_one_seconds", "standby_timeout_seconds",
        ):
            self.assertIsNone(rules[key])
        self.assertFalse(rules["announce_timeouts"])

    async def test_enabling_all_timer_categories_is_explicit(self) -> None:
        policy = await self.database.set_timer_policy(
            self.session["id"], "all", True, "admin"
        )
        self.assertTrue(policy["global_enabled"])
        self.assertTrue(all(policy["effective"].values()))

    async def test_configured_reminder_fires_once(self) -> None:
        await self.database.set_timer_policy(self.session["id"], "all", True, "admin")
        with self.database._connect() as connection:
            timer_id = self.database._create_timer(
                connection,
                session_id=self.session["id"],
                participant_id="",
                timer_type="turn",
                timeout_seconds=120,
                reminder_seconds=30,
                action={"actor_user_id": "user"},
            )
            connection.execute(
                "UPDATE timer_instances SET reminder_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), timer_id),
            )
        first = await self.database.process_due_timers()
        second = await self.database.process_due_timers()
        self.assertEqual([item["kind"] for item in first], ["reminder"])
        self.assertEqual(second, [])

    async def test_current_backup_only_and_round_trip(self) -> None:
        bundle = await self.database.export_bundle()
        self.assertEqual(bundle["schema_version"], DATABASE_SCHEMA_VERSION)
        old = copy.deepcopy(bundle)
        old["schema_version"] = 7
        with self.assertRaisesRegex(ValueError, "仅接受 Schema 9"):
            self.database.validate_bundle(old)
        self.database.validate_bundle(bundle)

        restored = TavernDatabase(self.root / "restored")
        result = await restored.import_bundle(bundle, "replace", "admin")
        self.assertEqual(result["sessions"], 1)
        recovered = await restored.get_session_by_group("qq", "group-v090")
        self.assertEqual(recovered["id"], self.session["id"])

    async def test_world_unknown_extensions_survive_editor_roundtrip(self) -> None:
        world = await self.database.get_world(DEFAULT_WORLD_SLUG)
        world["third_party_extension"] = {"mode": "custom", "order": [3, 1, 2]}
        saved = await self.database.save_world(world, "admin")
        stripped = {key: value for key, value in saved.items() if key != "third_party_extension"}
        stripped["description"] = "编辑器修改后的简介"
        saved_again = await self.database.save_world(stripped, "admin")
        self.assertEqual(
            saved_again["third_party_extension"],
            {"mode": "custom", "order": [3, 1, 2]},
        )

    async def test_preset_stack_import_then_console_save_round_trip(self) -> None:
        tide = json.loads(TIDE_WORLD_PATH.read_text("utf-8"))
        imported = await self.database.save_world(
            world_import_payload(tide), "admin"
        )
        # Simulate the visual editor: it submits editable fields but omits the
        # package extension envelope returned by the import file.
        submitted = {
            "id": imported["id"],
            "revision": imported["revision"],
            "slug": imported["slug"],
            "name": imported["name"],
            "description": "管理台保存后的简介",
            "system_prompt": imported["system_prompt"],
            "opening_scene": imported["opening_scene"],
            "rules": imported["rules"],
            "initial_state": imported["initial_state"],
        }
        saved = await self.database.save_world(
            world_edit_payload(submitted, imported), "admin"
        )
        restored = await self.database.get_world(saved["id"])
        self.assertEqual(restored["minimum_plugin_version"], "0.10.0")
        self.assertEqual(restored["world_content_version"], "2.0.0")
        self.assertEqual(
            restored["rules"]["character_card"]["stat_generation"]["mode"],
            "preset_stack",
        )
        self.assertEqual(restored["description"], "管理台保存后的简介")
        self.assertTrue(inspect_world_package(restored)["compatible"])

    async def test_operation_receipt_is_idempotent(self) -> None:
        created = await self.database.reserve_operation(
            "turn:test", self.session["id"], "turn", {"turn_no": 1}
        )
        duplicate = await self.database.reserve_operation(
            "turn:test", self.session["id"], "turn", {"turn_no": 1}
        )
        self.assertTrue(created["created"])
        self.assertFalse(duplicate["created"])

    async def test_diagnostics_redact_private_origin(self) -> None:
        report = await build_diagnostic_report(self.database, self.session["id"])
        self.assertEqual(report["session"]["unified_origin"], "[REDACTED]")
        self.assertEqual(report["database_schema_version"], DATABASE_SCHEMA_VERSION)

    async def test_public_api_does_not_expose_connection(self) -> None:
        hooks = HookRegistry()
        api = TavernPublicAPI(self.database, hooks, ExtensionRegistry())
        session = await api.get_current_session("qq", "group-v090")
        self.assertEqual(session["id"], self.session["id"])
        self.assertFalse(hasattr(api, "connect"))

        report = await api.export_diagnostic(
            self.session["id"],
            {"api_key": "secret-value", "safe_note": "visible"},
        )
        self.assertEqual(report["extension_context"]["api_key"], "[REDACTED]")
        self.assertEqual(report["extension_context"]["safe_note"], "visible")

    async def test_character_revision_activates_only_after_review(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
        )
        reserved = await self.database.reserve_participant(
            self.session["id"], "user-card", "测试玩家"
        )
        origin = "private:user-card"
        await self.database.bind_card_code(
            reserved["binding_code"], "user-card", origin
        )
        draft = await self.database.card_draft_for_private(origin)
        values = {
            "name": "灰鸦",
            "code": "HY",
            "profession": "骑士",
            "primary_attribute": "力量",
            "secondary_attribute": "意志",
        }
        for field in draft["template"]["fields"]:
            key = field["key"]
            value = values.get(key)
            if value is None:
                options = preset_options(draft["template"], field, {})
                if options:
                    value = options[0]["value"]
            if value is None:
                value = "原始资料"
            await self.database.fill_card_draft(origin, str(value))
        submitted = await self.database.confirm_card_draft(origin)
        await self.database.review_character_card(
            self.session["id"], submitted["id"], True, "admin"
        )
        before = await self.database.get_participant(
            self.session["id"], participant_ref=submitted["id"]
        )
        request = await self.database.request_card_revision(
            self.session["id"],
            submitted["id"],
            {"background": "修订后的冒险经历"},
            {},
            "user-card",
            "修正背景",
        )
        pending = await self.database.get_participant(
            self.session["id"], participant_ref=submitted["id"]
        )
        self.assertEqual(
            pending["character_version_id"], before["character_version_id"]
        )
        await self.database.review_card_revision(
            request["id"], True, "admin", "同意修改"
        )
        roster = await self.database.list_roster(self.session["id"])
        updated = next(item for item in roster if item["id"] == submitted["id"])
        self.assertEqual(updated["card_version_no"], 2)
        self.assertEqual(
            updated["card_profile"]["background"], "修订后的冒险经历"
        )

    async def test_committed_hook_isolated_from_failures(self) -> None:
        hooks = HookRegistry()
        seen: list[str] = []

        async def good(payload):
            seen.append(payload["session_id"])

        def bad(payload):
            raise RuntimeError("extension failed")

        hooks.subscribe("story_generated", bad)
        hooks.subscribe("story_generated", good)
        broker = EventBroker(hooks=hooks)
        await broker.publish(
            {"hook": "story_generated", "session_id": self.session["id"]}
        )
        self.assertEqual(seen, [self.session["id"]])

    async def test_transport_redelivery_consumes_one_turn(self) -> None:
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin"
        )
        output = json.dumps(
            {
                "mode": "resolve",
                "narrative": "只推进一次。",
                "check": None,
                "state_patch": {"scene_summary": "只推进一次。"},
                "memories": [],
                "director_note": "测试",
            },
            ensure_ascii=False,
        )
        context = FakeContext([output])
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0, json_repair_attempts=0),
            broker=EventBroker(),
        )
        event = SimpleNamespace(
            unified_msg_origin="qq:group-v090", message_id="event-once"
        )
        first = await engine.process(
            event=event,
            session_id=self.session["id"],
            sender_id="user",
            sender_name="旅客",
            content="观察窗外",
        )
        self.assertEqual(first.session["turn_no"], 1)
        with self.assertRaises(TavernBusyError):
            await engine.process(
                event=event,
                session_id=self.session["id"],
                sender_id="user",
                sender_name="旅客",
                content="观察窗外",
            )
        self.assertEqual(len(context.calls), 1)


if __name__ == "__main__":
    unittest.main()
