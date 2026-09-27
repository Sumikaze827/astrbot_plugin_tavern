from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import yaml

from tavern.constants import (
    CHARACTER_CARD_TEMPLATE_VERSION,
    DATABASE_SCHEMA_VERSION,
    NPC_IMPORT_TEMPLATE_VERSION,
    PLUGIN_VERSION,
    TEMPLATE_BUNDLE_VERSION,
)
from tavern.database import TavernDatabase
from tavern.prompts import system_prompt
from tavern.rule_runtime import RuleRuntime
from tavern.world_contract import validate_world_contract
from tavern.world_import import canonical_import_payload, world_import_payload
from tavern.world_preflight import inspect_world_package


ROOT = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class V011ContractTests(unittest.TestCase):
    def test_release_versions_are_synchronized(self) -> None:
        metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        manifest = load_json("templates/template-manifest.json")
        self.assertEqual(PLUGIN_VERSION, "0.12.0")
        self.assertEqual(metadata["version"], "0.12.0")
        self.assertEqual(DATABASE_SCHEMA_VERSION, 12)
        self.assertEqual(TEMPLATE_BUNDLE_VERSION, "4.0.0")
        self.assertEqual(CHARACTER_CARD_TEMPLATE_VERSION, 6)
        self.assertEqual(NPC_IMPORT_TEMPLATE_VERSION, 2)
        self.assertEqual(manifest["compatible_plugin_version"], PLUGIN_VERSION)

    def test_v5_packages_and_aelvion_pass_strict_preflight(self) -> None:
        for relative in (
            "worlds/aelvion-ashen-crown.json",
            "templates/world-package-capabilities.template.json",
            "templates/world-package-interaction-rules.template.json",
            "templates/world-package-v5-full-example.json",
        ):
            report = inspect_world_package(load_json(relative))
            self.assertTrue(report["compatible"], (relative, report["issues"]))
            self.assertGreater(report["summary"]["entity_count"], 0)

    def test_v2_v3_v4_remain_supported(self) -> None:
        for relative in (
            "templates/world-package.template.json",
            "templates/world-package-preset-stack.template.json",
            "tests/fixtures/where-winds-meet-tideless-script.world.json",
        ):
            validate_world_contract(load_json(relative))

    def test_disabled_chat_experience_module_needs_no_feature_declaration(self) -> None:
        """显式关掉的 chat_experience 模块是空转残块，不算"声明了数据"。

        真实事故：世界编辑器关掉「启用群聊体验策略」后保存，仍会写一块
        ``enabled: false`` 的数据，同时删掉 ``protocol.features.chat_experience``。
        服务端只要看到这块数据非空就要求声明功能版本——于是**关掉开关保存必然
        失败**，报错还指向 chat_experience，而玩家想改的是人数上限/角色卡。
        """
        world = load_json("templates/world-package-v5-full-example.json")
        world["rules"]["chat_experience"] = {
            "enabled": False,
            "safety": {"enabled": True},
            "multiplayer": {"spotlight": "round_robin"},
        }
        world["protocol"]["features"].pop("chat_experience", None)
        validate_world_contract(world)

    def test_enabled_chat_experience_still_requires_the_feature(self) -> None:
        """开关开着就必须声明功能版本——上面那条放宽不能把这道门也拆了。"""
        world = load_json("templates/world-package-v5-full-example.json")
        world["rules"]["chat_experience"] = {"enabled": True, "safety": {"enabled": True}}
        world["protocol"]["features"].pop("chat_experience", None)
        with self.assertRaisesRegex(ValueError, "chat_experience"):
            validate_world_contract(world)

    def test_chat_experience_without_an_enabled_flag_requires_the_feature(self) -> None:
        """忘写 ``enabled`` 字段 = 没关掉，仍按"声明了数据"处理。"""
        world = load_json("templates/world-package-v5-full-example.json")
        world["rules"]["chat_experience"] = {"safety": {"enabled": True}}
        world["protocol"]["features"].pop("chat_experience", None)
        with self.assertRaisesRegex(ValueError, "chat_experience"):
            validate_world_contract(world)

    def test_capability_progression_cycle_is_rejected(self) -> None:
        world = load_json("templates/world-package-capabilities.template.json")
        world["rules"]["capabilities"]["transitions"].append(
            {
                "transition_id": "cycle_back",
                "from": ["capability:advanced"],
                "operations": [
                    {"op": "grant_reference", "target_ref": "capability:basic"}
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "环"):
            validate_world_contract(world)

    def test_interaction_rules_are_world_defined_and_dry_run_is_side_effect_free(self) -> None:
        world = load_json("templates/world-package-interaction-rules.template.json")
        runtime = RuleRuntime(world)
        context = {
            "action": {"refs": {"custom:source.kind": "author_defined_source"}},
            "target": {"refs": {"custom:target.kind": "author_defined_target"}},
            "actor": {"capabilities": []},
            "state": {"refs": {"custom:check_modifier": 0}},
        }
        result = runtime.resolve_action_intent(
            {
                "actor_ref": "character:test",
                "action_type": "freeform",
                "declared_intent": "测试世界自定义关系",
            },
            context,
            dry_run=True,
        )
        self.assertEqual(result["state"], context["state"])
        self.assertIn("world_defined_relation", {
            item["rule_id"] for item in result["receipt"]["matched_rules"]
        })
        self.assertTrue(result["narrative_projection"])
        self.assertEqual(result["changes"][0]["after"], 2)

    def test_capability_projection_and_cost_commit(self) -> None:
        world = load_json("templates/world-package-v5-full-example.json")
        runtime = RuleRuntime(world)
        context = {
            "actor": {
                "capabilities": [
                    {"capability_ref": "capability:basic", "available": True}
                ],
                "refs": {"resource:focus": 3},
            },
            "state": {"refs": {"resource:focus": 3}},
        }
        projection = runtime.capability_projection(context)
        self.assertEqual(projection[0]["capability_ref"], "capability:basic")
        prompt = system_prompt(world, capability_projection=projection)
        self.assertIn("<available_capabilities>", prompt)
        result = runtime.resolve_action_intent(
            {
                "actor_ref": "character:test",
                "action_type": "freeform",
                "capability_ref": "capability:basic",
                "declared_intent": "使用能力",
            },
            context,
            dry_run=False,
        )
        self.assertEqual(result["state"]["refs"]["resource:focus"], 2)
        self.assertEqual(result["receipt"]["status"], "completed")

    def test_portable_hash_shape_excludes_catalog_number_and_order(self) -> None:
        world = load_json("worlds/aelvion-ashen-crown.json")
        world.update({"display_no": 81, "sort_order": -5, "created_at": "x"})
        portable = world_import_payload(world)
        self.assertNotIn("display_no", portable)
        self.assertNotIn("sort_order", portable)
        self.assertNotIn("created_at", portable)


class V011DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp.name))

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_stable_display_number_and_independent_sort_order(self) -> None:
        base = load_json("templates/world-package-v5-full-example.json")
        base.update({"slug": "stable-number-one", "name": "编号一"})
        first = await self.database.save_world(base, "admin")
        self.assertEqual(world_import_payload(base), world_import_payload(first))
        original_no = first["display_no"]
        edited = await self.database.save_world(
            {**first, "description": "编辑不改变编号"}, "admin"
        )
        self.assertEqual(edited["display_no"], original_no)
        moved = await self.database.set_world_sort_order(
            first["id"], 100, "admin"
        )
        self.assertEqual(moved["display_no"], original_no)
        self.assertEqual(moved["sort_order"], 100)
        await self.database.archive_world(first["id"], "admin")
        base.update({"slug": "stable-number-two", "name": "编号二"})
        second = await self.database.save_world(base, "admin")
        self.assertGreater(second["display_no"], original_no)

    async def test_reimporting_the_same_package_is_not_a_conflict(self) -> None:
        """同一份世界包导入两次不能报「内容版本相同但内容不同」。

        真实事故：``save_world`` 会把顶层的 ``world_schema_version`` /
        ``capabilities`` **复制进 rules** 再落库，回读时 ``_world`` 又只从
        ``rules`` 取这两项。于是「导入 → 再导入同一份文件」的 hash 必然不等，
        面板弹出假冲突，把用户推向「覆盖为新修订」——两份内容其实一模一样。
        """
        raw = load_json("templates/world-package-v5-full-example.json")
        raw = dict(raw)
        # 生成器产出的包**只在顶层**写这两项，rules 里没有——这正是会翻车的形状。
        # 官方模板恰好两边都写了，拿它当夹具测不出东西（已踩过）。
        raw["rules"] = {
            key: value
            for key, value in raw["rules"].items()
            if key not in {"world_schema_version", "capabilities"}
        }
        raw.update({"slug": "roundtrip-world", "name": "往返一致"})
        await self.database.save_world(world_import_payload(raw), "admin")
        stored = await self.database.get_world("roundtrip-world")

        fingerprint = lambda payload: json.dumps(  # noqa: E731
            payload, ensure_ascii=False, sort_keys=True
        )
        # 先证明旧算法确实会翻车，否则这条测试就是空转。
        self.assertNotEqual(
            fingerprint(world_import_payload(raw)),
            fingerprint(world_import_payload(stored)),
            msg="夹具不再是会触发假冲突的形状，这条测试已经失去意义",
        )
        self.assertEqual(
            fingerprint(canonical_import_payload(raw)),
            fingerprint(canonical_import_payload(stored)),
            msg="同一份包往返后仍算出不同指纹 → 第二次导入会报假冲突",
        )

    async def test_canonical_payload_keeps_extensions_verbatim(self) -> None:
        """规整只搬运那两项，作者自定义的扩展字段必须原样保留。"""
        raw = load_json("templates/world-package-v5-full-example.json")
        raw.update({"slug": "ext-world", "name": "扩展", "my_extension": {"a": 1}})
        portable = canonical_import_payload(raw)
        self.assertEqual({"a": 1}, portable["my_extension"])
        self.assertEqual(portable["world_schema_version"],
                         portable["rules"]["world_schema_version"])
        self.assertEqual(portable["capabilities"], portable["rules"]["capabilities"])

    async def test_action_commit_is_idempotent_and_receipted(self) -> None:
        session = await self.database.ensure_session(
            "test", "group", "test:group", "aelvion-ashen-crown", "admin",
            "receipt-test", "凭证测试",
        )
        intent = {
            "actor_ref": "character:test",
            "action_type": "freeform",
            "declared_intent": "观察现场",
        }
        first = await self.database.resolve_action_intent(
            session["id"], intent, {}, dry_run=False,
            operation_id="idempotent-v011", actor_id="admin",
        )
        second = await self.database.resolve_action_intent(
            session["id"], intent, {}, dry_run=False,
            operation_id="idempotent-v011", actor_id="admin",
        )
        self.assertEqual(first["receipt"]["receipt_id"], second["receipt"]["receipt_id"])
        receipt = await self.database.get_resolution_receipt(
            first["receipt"]["receipt_id"]
        )
        self.assertEqual(receipt["operation_id"], "idempotent-v011")
        self.assertEqual(len(receipt["content_hash"]), 64)

    async def test_running_contract_stays_frozen_and_upgrade_uses_new_clone(self) -> None:
        session = await self.database.ensure_session(
            "test", "upgrade-group", "test:upgrade", "aelvion-ashen-crown", "admin",
            "original", "原副本",
        )
        frozen = await self.database.get_instance_config(session["id"])
        current = await self.database.get_world("aelvion-ashen-crown")
        updated = await self.database.save_world(
            {**current, "description": current["description"] + "（新修订）"},
            "admin",
        )
        unchanged = await self.database.get_instance_config(session["id"])
        self.assertEqual(unchanged["world_revision"], frozen["world_revision"])
        clone = await self.database.clone_session(
            session["id"], "admin",
            instance_slug="upgraded", instance_name="升级分支",
            candidate_world_ref=updated["id"],
        )
        clone_config = await self.database.get_instance_config(clone["id"])
        self.assertEqual(clone["state"], "closed")
        self.assertEqual(clone_config["world_revision"], updated["revision"])
        self.assertEqual(
            clone_config["phase_meta"]["branched_from_session_id"], session["id"]
        )

    async def test_schema9_upgrade_creates_backup_and_deterministic_backfill(self) -> None:
        legacy_dir = Path(self.temp.name) / "legacy"
        legacy_dir.mkdir()
        path = legacy_dir / "catalog_v090.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE tavern_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO tavern_meta(key, value) VALUES ('schema_version', '9');
                CREATE TABLE worlds (
                    id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL,
                    system_prompt TEXT NOT NULL, rules_json TEXT NOT NULL,
                    extensions_json TEXT NOT NULL DEFAULT '{}',
                    opening_scene TEXT NOT NULL, initial_state_json TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO worlds VALUES (
                    'world_legacy', 'legacy-v4', '旧世界', '', '规则',
                    '{"world_schema_version":4}', '{}', '', '{}', 0, 1,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                );
                """
            )
        upgraded = TavernDatabase(legacy_dir)
        self.assertIsNotNone(upgraded.migration_backup_path)
        self.assertTrue(upgraded.migration_backup_path.is_file())
        with closing(sqlite3.connect(path)) as connection:
            schema = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='schema_version'"
            ).fetchone()[0]
            number, order = connection.execute(
                "SELECT display_no, sort_order FROM worlds WHERE id='world_legacy'"
            ).fetchone()
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM world_snapshots"
            ).fetchone()[0]
        self.assertEqual(schema, "12")
        self.assertEqual((number, order), (1, 1))
        self.assertEqual(snapshots, 2)


if __name__ == "__main__":
    unittest.main()
