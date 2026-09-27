from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tavern.chat_experience import (
    normalize_chat_experience,
    validate_chat_experience,
)
from tavern.constants import DATABASE_SCHEMA_VERSION, DEFAULT_WORLD_SLUG
from tavern.database import TavernDatabase
from tavern.platform_delivery import (
    capabilities_for,
    capability_matrix,
    split_text,
)
from tavern.world_preflight import inspect_world_package


ROOT = Path(__file__).resolve().parents[1]


class B1WorldContractTests(unittest.TestCase):
    def test_legacy_world_uses_disabled_noop_policy(self) -> None:
        policy = normalize_chat_experience({"rules": {}})
        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["delivery"]["proactive_fallback"], "next_event")

    def test_enabled_policy_rejects_unknown_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "proactive_fallback"):
            validate_chat_experience(
                {
                    "rules": {
                        "chat_experience": {
                            "enabled": True,
                            "delivery": {"proactive_fallback": "magic"},
                        }
                    }
                }
            )

    def test_builtin_world_declares_chat_experience_feature(self) -> None:
        world = json.loads(
            (ROOT / "worlds" / "aelvion-ashen-crown.json").read_text("utf-8")
        )
        report = inspect_world_package(world)
        self.assertTrue(report["compatible"], report.get("errors"))
        self.assertEqual(
            report["summary"]["feature_versions"]["chat_experience"], "1.0"
        )
        self.assertTrue(
            world["rules"]["chat_experience"]["continuity"][
                "preserve_npc_intent"
            ]
        )
        self.assertEqual(world["minimum_plugin_version"], "0.12.0")


class B1PlatformTests(unittest.TestCase):
    def test_capability_matrix_is_text_only_and_has_no_retired_rest_adapter(self) -> None:
        names = {item["platform"] for item in capability_matrix()}
        self.assertIn("aiocqhttp", names)
        self.assertIn("telegram", names)
        self.assertNotIn("qq_restapi", names)
        self.assertTrue(capabilities_for("custom-instance:GroupMessage:1").proactive_send)

    def test_long_text_split_preserves_content_order(self) -> None:
        source = "第一段。" * 180 + "\n\n" + "第二段。" * 180
        parts = split_text(source, 300)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(item) <= 301 for item in parts))
        self.assertEqual("".join(parts).replace("\n", ""), source.replace("\n", ""))


class B1DeliveryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "b1-group", "qq:b1-group", DEFAULT_WORLD_SLUG, "admin"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_outbox_deduplicates_and_survives_database_reload(self) -> None:
        first = await self.database.queue_delivery(
            session_id=self.session["id"],
            origin="qq:b1-group",
            kind="timer.turn.reminder",
            text="轮到白鸦行动",
            reason="adapter rejected proactive send",
            dedupe_key="turn:1",
        )
        second = await self.database.queue_delivery(
            session_id=self.session["id"],
            origin="qq:b1-group",
            kind="timer.turn.reminder",
            text="轮到白鸦行动",
            reason="duplicate",
            dedupe_key="turn:1",
        )
        self.assertEqual(first["id"], second["id"])
        reloaded = TavernDatabase(Path(self.temp.name))
        pending = await reloaded.list_deliveries(origin="qq:b1-group")
        self.assertEqual([item["id"] for item in pending], [first["id"]])
        sent = await reloaded.finish_delivery(
            first["id"], success=True, delivered_on_reply=True
        )
        self.assertEqual(sent["status"], "delivered_on_reply")

    async def test_schema_12_contains_delivery_outbox(self) -> None:
        with closing(sqlite3.connect(self.database.path)) as connection:
            version = int(
                connection.execute(
                    "SELECT value FROM tavern_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_outbox'"
            ).fetchone()
        self.assertEqual(version, DATABASE_SCHEMA_VERSION)
        self.assertIsNotNone(table)

    async def test_a24_schema_11_backup_remains_import_compatible(self) -> None:
        bundle = await self.database.export_bundle()
        for schema_version in (9, 10, 11, DATABASE_SCHEMA_VERSION):
            candidate = {**bundle, "schema_version": schema_version}
            self.database.validate_bundle(candidate)


class B1UiStaticTests(unittest.TestCase):
    def test_console_exposes_b1_controls_and_accessible_motion_fallback(self) -> None:
        app = (ROOT / "pages" / "console" / "app.js").read_text("utf-8")
        css = (ROOT / "pages" / "console" / "style.css").read_text("utf-8")
        html = (ROOT / "pages" / "console" / "index.html").read_text("utf-8")
        self.assertIn('apiGet("deliveries"', app)
        self.assertIn("多人群聊体验", app)
        self.assertIn("prefers-reduced-motion:reduce", css)
        self.assertIn("settings-disclosure", css)
        self.assertIn("style.css?v=0.12.0", html)
        self.assertNotIn("qq_restapi", app.casefold())
        self.assertNotIn("富卡片", app)


if __name__ == "__main__":
    unittest.main()
