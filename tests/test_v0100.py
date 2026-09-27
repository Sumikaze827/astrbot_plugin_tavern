from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_PREPARING
from tavern.database import TavernDatabase
from tavern.presets import (
    check_character_knowledge,
    check_content_permission,
    resolve_character_presets,
)
from tavern.world_contract import validate_world_contract


ROOT = Path(__file__).resolve().parents[1]
TIDE = ROOT / "tests/fixtures/where-winds-meet-tideless-script.world.json"


class V0100ContractTests(unittest.TestCase):
    def test_tide_v4_has_generic_presets_and_world_boundaries(self) -> None:
        world = json.loads(TIDE.read_text("utf-8"))
        contract = validate_world_contract(world)
        self.assertEqual(contract["version"], 4)
        card = world["rules"]["character_card"]
        dimensions = {item["id"]: item for item in card["preset_dimensions"]}
        self.assertEqual(len(dimensions), 10)
        self.assertIn("gender", dimensions)
        self.assertIn("personality", dimensions)
        self.assertNotIn(
            "knowledge_boundary", {item["key"] for item in card["fields"]}
        )
        self.assertNotIn(
            "content_boundaries", {item["key"] for item in card["fields"]}
        )
        self.assertFalse(world["rules"]["content_boundary"]["player_may_relax"])

    def test_preset_boundaries_are_structured_and_conservative(self) -> None:
        world = json.loads(TIDE.read_text("utf-8"))
        origin = next(
            item
            for item in world["rules"]["character_card"]["preset_dimensions"]
            if item["id"] == "origin_region"
        )["options"][0]
        fields = {
            "_preset_refs": {
                "origin_region": {"id": origin["id"], "snapshot": origin}
            }
        }
        resolved = resolve_character_presets(world, fields)
        self.assertEqual(
            resolved["knowledge"]["domains"]["出身地区地理与风俗"],
            "familiar",
        )
        self.assertFalse(
            check_character_knowledge(resolved, "导演秘密")["allowed"]
        )
        self.assertFalse(
            check_content_permission(resolved, ["露骨性内容"])["allowed"]
        )


class V0100DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v010", "qq:group-v010", DEFAULT_WORLD_SLUG, "admin"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_expired_card_code_is_reissued_from_group_and_private(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
        )
        first = await self.database.reserve_participant(
            self.session["id"], "user-group-renew", "群补发"
        )
        expired_at = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE card_binding_codes SET expires_at=? WHERE code=?",
                (expired_at, first["binding_code"]),
            )
        renewed = await self.database.reserve_participant(
            self.session["id"], "user-group-renew", "群补发"
        )
        self.assertTrue(renewed["binding_code_reissued"])
        self.assertNotEqual(renewed["binding_code"], first["binding_code"])

        second = await self.database.reserve_participant(
            self.session["id"], "user-private-renew", "私聊补发"
        )
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE card_binding_codes SET expires_at=? WHERE code=?",
                (expired_at, second["binding_code"]),
            )
        private = await self.database.bind_card_code(
            second["binding_code"], "user-private-renew", "private:user-private-renew"
        )
        self.assertTrue(private["binding_code_reissued"])
        self.assertNotEqual(private["binding_code"], second["binding_code"])

    async def test_dm_beat_preserves_player_turn_and_enters_backup(self) -> None:
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE sessions SET state='running' WHERE id=?",
                (self.session["id"],),
            )
        before = await self.database.get_session(self.session["id"])
        turn_before = await self.database.get_turn_status(self.session["id"])
        control = await self.database.enable_dm_mode(
            self.session["id"], "dm-user", "dm-user"
        )
        result = await self.database.commit_dm_beat(
            session_id=self.session["id"],
            expected_revision=int(before["revision"]),
            dm_user_id="dm-user",
            instruction="城门关闭",
            narrative="城门在沉重的轰鸣声中缓缓闭合。",
            world_state=before["world_state"],
            direct=True,
        )
        turn_after = await self.database.get_turn_status(self.session["id"])
        self.assertEqual(turn_after["current_user_id"], turn_before["current_user_id"])
        self.assertEqual(turn_after["round_no"], turn_before["round_no"])
        self.assertEqual(result["beat_no"], 1)
        self.assertEqual(control["mode"], "dm")
        bundle = await self.database.export_bundle()
        self.assertEqual(len(bundle["data"]["dm_control_states"]), 1)


if __name__ == "__main__":
    unittest.main()
