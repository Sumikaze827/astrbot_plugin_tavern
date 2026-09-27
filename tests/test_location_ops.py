"""Per-character location persistence regression tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now
from tavern.engine import TavernEngine
from tavern.resolution import validate_resolution


def _insert_participant(connection, session_id: str, pid: str, name: str) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO participants(
            id, session_id, player_id, group_user_id, private_user_id,
            private_origin, display_name, character_card_id,
            character_version_id, character_name, character_code, aliases_json,
            card_status, ready, participation_status, seat_reserved_at,
            joined_round, consecutive_timeouts, exit_reason, created_at,
            updated_at, action_locked
        ) VALUES (?, ?, NULL, ?, '', '', ?, NULL, NULL, ?, ?, '[]',
                  'approved', 1, 'active', ?, 1, 0, '', ?, ?, 0)
        """,
        (pid, session_id, pid, name, name, name, now, now, now),
    )
    connection.execute(
        """
        INSERT INTO character_runtime_states(
            id, session_id, participant_id, character_card_id,
            state_json, revision, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, ?, 1, ?, ?)
        """,
        (
            new_id("runtime"),
            session_id,
            pid,
            json.dumps(
                {"current_location": "客栈前厅", "statuses": []},
                ensure_ascii=False,
            ),
            now,
            now,
        ),
    )


class LocationOpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-location", "qq:group-location", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        self.mover = new_id("participant")
        self.stayer = new_id("participant")
        with self.database._connect() as connection:
            _insert_participant(
                connection, self.session["id"], self.mover, "移动者"
            )
            _insert_participant(
                connection, self.session["id"], self.stayer, "留守者"
            )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _location(self, participant_id: str) -> str:
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM character_runtime_states "
                "WHERE participant_id=?",
                (participant_id,),
            ).fetchone()
        return json.loads(row[0]).get("current_location", "")

    def test_validation_filters_incomplete_location_operations(self) -> None:
        resolution = validate_resolution(
            {
                "mode": "resolve",
                "narrative": "移动者去了后院。",
                "location_ops": [
                    {"target_id": self.mover, "location": "客栈后院"},
                    {"target_id": self.stayer, "location": ""},
                ],
            }
        )
        self.assertEqual(
            resolution.location_ops,
            ({"target_id": self.mover, "location": "客栈后院"},),
        )

    def test_moving_one_character_does_not_move_others(self) -> None:
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (self.session["id"],)
            ).fetchone()
            participant = connection.execute(
                "SELECT * FROM participants WHERE id=?", (self.mover,)
            ).fetchone()
            result = self.database._apply_v05_turn_ops(
                connection,
                session=session,
                participant=participant,
                new_turn=2,
                acting_round=1,
                source_event_id="event_location_test",
                workflow={
                    "location_ops": [
                        {"target_id": self.mover, "location": "客栈后院"}
                    ],
                    "status_ops": [],
                },
                check_payload={},
                now=now,
            )
            connection.execute("COMMIT")

        self.assertEqual(self._location(self.mover), "客栈后院")
        self.assertEqual(self._location(self.stayer), "客栈前厅")
        self.assertEqual(
            result["locations"],
            [{"target_id": self.mover, "location": "客栈后院"}],
        )

    def test_partial_move_cannot_change_shared_location(self) -> None:
        roster = [
            {"id": self.mover, "participation_status": "active"},
            {"id": self.stayer, "participation_status": "active"},
        ]
        guarded = TavernEngine._guard_shared_location_patch(
            {"location": "客栈后院", "time": "午后"},
            ({"target_id": self.mover, "location": "客栈后院"},),
            roster,
        )
        self.assertNotIn("location", guarded)
        self.assertEqual(guarded["time"], "午后")

    def test_full_party_move_can_change_shared_location(self) -> None:
        roster = [
            {"id": self.mover, "participation_status": "active"},
            {"id": self.stayer, "participation_status": "active"},
        ]
        guarded = TavernEngine._guard_shared_location_patch(
            {"location": "城门"},
            (
                {"target_id": self.mover, "location": "城门"},
                {"target_id": self.stayer, "location": "城门"},
            ),
            roster,
        )
        self.assertEqual(guarded["location"], "城门")


if __name__ == "__main__":
    unittest.main()
