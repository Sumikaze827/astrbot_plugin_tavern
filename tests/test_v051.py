from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from tavern import storage as storage_module
from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_FINISHED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import DatabaseConflictError, TavernDatabase


class V051StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq_official",
            "2FC1441BD4EF524E6AB9BB195BFF6C6F",
            "qq_official:group:test",
            DEFAULT_WORLD_SLUG,
            "admin",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _commit(self, *, interval: int = 5) -> dict:
        session = await self.database.transition_session(
            self.session["id"],
            SESSION_RUNNING,
            "admin",
        )
        player = await self.database.ensure_player(
            session["id"],
            "user-1",
            "旅客",
        )
        state = dict(session["world_state"])
        state["scene_summary"] = "旅客检查了酒馆门锁。"
        self.session = await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=player["id"],
            player_user_id=player["user_id"],
            player_name=player["display_name"],
            player_input="检查门锁",
            narrative="门锁上留有新鲜划痕。",
            world_state=state,
            memories=[],
            check_payload=None,
            model_payload=None,
            director_note="0.5.1 storage test",
            auto_snapshot_interval=interval,
            store_model_payload=False,
        )
        return self.session

    def _story_dir(self, session: dict | None = None) -> Path:
        target = session or self.session
        with closing(sqlite3.connect(self.database.path)) as connection:
            relative = connection.execute(
                """
                SELECT relative_path FROM story_storage
                WHERE session_id = ?
                """,
                (target["id"],),
            ).fetchone()[0]
        return self.data_dir / str(relative)

    async def _confirm_card(
        self,
        session_id: str,
        *,
        user_id: str,
        origin: str,
        name: str,
        code: str,
    ) -> dict:
        reserved = await self.database.reserve_participant(
            session_id,
            user_id,
            name,
        )
        await self.database.bind_card_code(
            reserved["binding_code"],
            f"private-{user_id}",
            origin,
        )
        draft = await self.database.card_draft_for_private(origin)
        self.assertIsNotNone(draft)
        used_select_values: set[str] = set()
        for field in draft["template"]["fields"]:
            if field["key"] == "name":
                value = name
            elif field["key"] == "code":
                value = code
            elif field.get("type") == "preset_select" and field.get("options"):
                values = [
                    str(item.get("value") or item.get("label") or item)
                    if isinstance(item, dict) else str(item)
                    for item in field["options"]
                ]
                value = next(
                    (item for item in values if item not in used_select_values),
                    values[0],
                )
                used_select_values.add(value)
            elif field.get("type") == "integer":
                value = str(field.get("default", 0))
            else:
                value = "无"
            await self.database.fill_card_draft(origin, value)
        return await self.database.confirm_card_draft(origin)

    async def test_each_group_and_playthrough_has_recoverable_files(
        self,
    ) -> None:
        story_dir = self._story_dir()
        self.assertRegex(
            story_dir.name,
            r"^aelvion-ashen-crown_\d{14}_i-[a-z0-9-]{6,8}$",
        )
        self.assertEqual(story_dir.parent.name, "stories")
        self.assertRegex(
            story_dir.parent.parent.name,
            r"^qq_official_g_[a-f0-9]{16}$",
        )
        for relative in (
            "manifest.json",
            "instance.sqlite3",
            "saves",
            "backups",
        ):
            self.assertTrue((story_dir / relative).exists(), relative)
        group_manifest = story_dir.parent.parent / "group.json"
        self.assertTrue(group_manifest.exists())
        manifest = json.loads(
            (story_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["session"]["id"], self.session["id"])
        self.assertEqual(manifest["session"]["playthrough_no"], 1)
        self.assertRegex(
            manifest["storage"]["database_sha256"],
            r"^[a-f0-9]{64}$",
        )
        verified = await self.database.verify_storage(self.session["id"])
        self.assertTrue(verified["ok"], verified)
        with closing(sqlite3.connect(story_dir / "instance.sqlite3")) as connection:
            session_ids = [
                row[0] for row in connection.execute("SELECT id FROM sessions")
            ]
            kind = connection.execute(
                """
                SELECT value FROM tavern_meta WHERE key = 'storage_kind'
                """
            ).fetchone()[0]
        self.assertEqual(session_ids, [self.session["id"]])
        self.assertEqual(kind, "instance")

    async def test_sync_closes_sqlite_before_windows_atomic_replace(
        self,
    ) -> None:
        real_connect = sqlite3.connect
        real_replace = storage_module.os.replace
        temporary_connections: list[object] = []

        class TrackingConnection:
            def __init__(self, database, *args, **kwargs) -> None:
                object.__setattr__(
                    self,
                    "_raw",
                    real_connect(database, *args, **kwargs),
                )
                object.__setattr__(self, "_path", Path(database))
                object.__setattr__(self, "closed", False)
                if self._path.name.startswith(".instance."):
                    temporary_connections.append(self)

            def __getattr__(self, name):
                return getattr(self._raw, name)

            def __setattr__(self, name, value) -> None:
                if name in {"_raw", "_path", "closed"}:
                    object.__setattr__(self, name, value)
                else:
                    setattr(self._raw, name, value)

            def __enter__(self):
                self._raw.__enter__()
                return self

            def __exit__(self, exc_type, exc, traceback):
                return self._raw.__exit__(exc_type, exc, traceback)

            def backup(self, target, *args, **kwargs):
                raw_target = getattr(target, "_raw", target)
                return self._raw.backup(raw_target, *args, **kwargs)

            def close(self) -> None:
                self._raw.close()
                self.closed = True

        def tracked_connect(database, *args, **kwargs):
            return TrackingConnection(database, *args, **kwargs)

        def windows_replace(source, destination) -> None:
            source_path = Path(source)
            if (
                source_path.name.startswith(".instance.")
                and any(
                    not connection.closed
                    for connection in temporary_connections
                    if connection._path == source_path
                )
            ):
                error = PermissionError(
                    32,
                    "Windows sharing violation",
                    str(source_path),
                )
                error.winerror = 32
                raise error
            real_replace(source, destination)

        with (
            mock.patch.object(
                storage_module.sqlite3,
                "connect",
                side_effect=tracked_connect,
            ),
            mock.patch.object(
                storage_module.os,
                "replace",
                side_effect=windows_replace,
            ),
        ):
            result = self.database.storage.sync_session(
                self.session["id"]
            )

        self.assertTrue(temporary_connections)
        self.assertTrue(
            all(
                connection.closed
                for connection in temporary_connections
            )
        )
        self.assertTrue(Path(result["database"]).exists())

    async def test_locked_cleanup_does_not_hide_sync_failure(self) -> None:
        real_replace = storage_module.os.replace
        real_unlink = Path.unlink
        replacement_error = PermissionError(
            32,
            "replace remained locked",
        )
        replacement_error.winerror = 32

        def failing_replace(source, destination) -> None:
            if Path(source).name.startswith(".instance."):
                raise replacement_error
            real_replace(source, destination)

        def failing_unlink(path, *args, **kwargs) -> None:
            if path.name.startswith(".instance."):
                cleanup_error = PermissionError(
                    32,
                    "cleanup remained locked",
                    str(path),
                )
                cleanup_error.winerror = 32
                raise cleanup_error
            real_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(
                storage_module.os,
                "replace",
                side_effect=failing_replace,
            ),
            mock.patch.object(
                Path,
                "unlink",
                autospec=True,
                side_effect=failing_unlink,
            ),
            mock.patch.object(storage_module.time, "sleep"),
        ):
            with self.assertRaises(PermissionError) as raised:
                self.database.storage.sync_session(
                    self.session["id"]
                )

        self.assertIs(raised.exception, replacement_error)
        with closing(sqlite3.connect(self.database.path)) as connection:
            row = connection.execute(
                """
                SELECT sync_status, last_error
                FROM story_storage WHERE session_id = ?
                """,
                (self.session["id"],),
            ).fetchone()
        self.assertEqual(row[0], "error")
        self.assertIn("replace remained locked", row[1])

    async def test_group_remark_is_searchable_and_written_to_group_json(
        self,
    ) -> None:
        saved = await self.database.save_group_remark(
            self.session["platform_id"],
            self.session["group_id"],
            "周六固定团",
            "web:admin",
            1,
        )
        self.assertEqual(saved["remark"], "周六固定团")
        result = await self.database.search_sessions(
            "周六固定团",
            "group",
            1,
            20,
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["group_remark"], "周六固定团")
        story_result = await self.database.search_sessions(
            "灰烬",
            "story",
            1,
            20,
        )
        self.assertEqual(story_result["total"], 1)
        clamped_result = await self.database.search_sessions(
            "",
            "all",
            999,
            20,
        )
        self.assertEqual(clamped_result["page"], 1)
        self.assertEqual(len(clamped_result["items"]), 1)
        group_payload = json.loads(
            (
                self._story_dir().parent.parent / "group.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(group_payload["remark"], "周六固定团")
        with self.assertRaises(DatabaseConflictError):
            await self.database.save_group_remark(
                self.session["platform_id"],
                self.session["group_id"],
                "过期覆盖",
                "web:other",
                1,
            )

    async def test_roster_exposes_complete_draft_and_submitted_card(
        self,
    ) -> None:
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "user-card",
            "建卡玩家",
        )
        origin = "qq-private:user-card"
        await self.database.bind_card_code(
            reserved["binding_code"],
            "private-user-card",
            origin,
        )
        config = await self.database.get_instance_config(self.session["id"])
        template = config["character_card_template"]
        self.assertTrue(template["fields"])
        self.assertEqual(
            [item["label"] for item in template["stats"]["attributes"]],
            ["力量", "体质", "灵巧", "感知", "智力", "意志", "魅力", "魔力", "信仰", "生存"],
        )

        await self.database.fill_card_draft(origin, "银栎")
        draft_item = (await self.database.list_roster(self.session["id"]))[0]
        self.assertEqual(draft_item["draft_profile"]["name"], "银栎")
        self.assertEqual(draft_item["draft_template_version"], template["version"])
        self.assertEqual(draft_item["draft_step"], 1)

        field_values = {
            "code": "YL",
            "appearance": "银灰短发，穿旧皮甲。",
            "background": "来自边境驿站，熟悉商路。",
            "personality": "谨慎而直接。",
            "goal": "找出酒馆异响的来源。",
            "belief": "承诺必须兑现。",
            "bond": "仍在边境等待的妹妹。",
            "specialties": "追踪，谈判",
            "flaws": "多疑",
            "weakness": "不擅长正面对抗。",
            "knowledge_boundary": "只了解边境常识。",
            "secret": "曾经隐瞒一封来信。",
            "content_boundaries": "不描写虐待动物。",
        }
        used_select_values: set[str] = set()
        for field in template["fields"][1:]:
            if field.get("type") == "preset_select" and field.get("options"):
                values = [
                    str(item.get("value") or item.get("label") or item)
                    if isinstance(item, dict) else str(item)
                    for item in field["options"]
                ]
                value = next(
                    (item for item in values if item not in used_select_values),
                    values[0],
                )
                used_select_values.add(value)
            else:
                value = field_values.get(field["key"], "测试内容")
            await self.database.fill_card_draft(origin, value)
        confirmed = await self.database.confirm_card_draft(origin)
        self.assertFalse(confirmed["auto_approved"])

        pending_item = (await self.database.list_roster(self.session["id"]))[0]
        self.assertEqual(pending_item["card_profile"]["name"], "银栎")
        self.assertEqual(pending_item["card_profile"]["secret"], "曾经隐瞒一封来信。")
        self.assertEqual(len(pending_item["card_stats"]["raw"]), 10)
        self.assertEqual(pending_item["card_stats"]["raw"]["strength"], 15)
        self.assertEqual(pending_item["card_version_no"], 1)
        self.assertEqual(
            pending_item["card_template_version"],
            template["version"],
        )
        self.assertEqual(
            pending_item["card_version_status"],
            "pending_review",
        )
        self.assertEqual(pending_item["runtime_revision"], 1)

        await self.database.review_character_card(
            self.session["id"],
            pending_item["id"],
            True,
            "admin",
            "完整参数审核通过",
        )
        approved_item = (await self.database.list_roster(self.session["id"]))[0]
        self.assertEqual(approved_item["card_version_status"], "approved")
        self.assertEqual(
            approved_item["card_review_note"],
            "完整参数审核通过",
        )
        self.assertEqual(approved_item["card_reviewed_by"], "admin")

    async def test_private_origin_can_bind_again_in_second_playthrough(
        self,
    ) -> None:
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        origin = "qq-private:repeat-player"
        first_card = await self._confirm_card(
            self.session["id"],
            user_id="repeat-player",
            origin=origin,
            name="第一轮角色",
            code="R1",
        )
        self.assertEqual(first_card["card_status"], "pending_review")
        await self.database.finalize_session(
            self.session["id"],
            "admin",
            termination_type="completed",
            reason="第一轮完成",
        )
        replay = await self.database.ensure_session(
            self.session["platform_id"],
            self.session["group_id"],
            self.session["unified_origin"],
            self.session["world_id"],
            "admin",
            self.session["instance_slug"],
            self.session["instance_name"],
        )
        replay = await self.database.transition_session(
            replay["id"],
            SESSION_PREPARING,
            "admin",
        )
        reserved = await self.database.reserve_participant(
            replay["id"],
            "repeat-player",
            "第二轮玩家",
        )
        rebound = await self.database.bind_card_code(
            reserved["binding_code"],
            "private-repeat-player",
            origin,
        )
        self.assertEqual(rebound["session_id"], replay["id"])
        self.assertNotEqual(rebound["id"], first_card["id"])
        active_draft = await self.database.card_draft_for_private(origin)
        self.assertIsNotNone(active_draft)
        self.assertEqual(active_draft["session_id"], replay["id"])

    @unittest.skip("旧四属性手填默认模板已被职业预设数值取代；数值预算兼容由自定义世界包测试覆盖")
    async def test_stat_budget_clamps_progressively_and_can_reset_only_stats(
        self,
    ) -> None:
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "stat-player",
            "数值玩家",
        )
        origin = "qq-private:stat-player"
        bound = await self.database.bind_card_code(
            reserved["binding_code"],
            "private-stat-player",
            origin,
        )
        template = bound["template"]
        stat_fields = [
            item for item in template["fields"] if item.get("stat_key")
        ]
        first_stat_step = template["fields"].index(stat_fields[0])

        for field in template["fields"][:first_stat_step]:
            value = (
                "数值角色"
                if field["key"] == "name"
                else (
                    "STAT"
                    if field["key"] == "code"
                    else "保留的文字角色资料"
                )
            )
            await self.database.fill_card_draft(origin, value)

        await self.database.fill_card_draft(origin, "5")
        await self.database.fill_card_draft(origin, "4")
        with self.assertRaisesRegex(ValueError, "0—1"):
            await self.database.fill_card_draft(origin, "2")
        await self.database.fill_card_draft(origin, "1")
        completed = await self.database.fill_card_draft(origin, "0")
        self.assertTrue(completed["complete"])
        self.assertEqual(
            {
                key: completed["fields"][key]
                for key in (
                    "stat_body",
                    "stat_agility",
                    "stat_will",
                    "stat_knowledge",
                )
            },
            {
                "stat_body": 5,
                "stat_agility": 4,
                "stat_will": 1,
                "stat_knowledge": 0,
            },
        )

        reset = await self.database.reset_card_draft_stats(origin)
        self.assertEqual(reset["current_step"], first_stat_step)
        self.assertEqual(
            reset["fields"]["background"],
            "保留的文字角色资料",
        )
        self.assertFalse(
            any(key.startswith("stat_") for key in reset["fields"])
        )

        for value in ("2", "2", "3", "3"):
            reset = await self.database.fill_card_draft(origin, value)
        self.assertTrue(reset["complete"])
        self.assertEqual(reset["fields"]["name"], "数值角色")
        self.assertEqual(
            sum(
                int(reset["fields"][item["key"]])
                for item in stat_fields
            ),
            10,
        )

    async def test_card_fields_forbid_whitespace_and_cap_identity_at_twelve(
        self,
    ) -> None:
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "compact-card-player",
            "紧凑卡玩家",
        )
        origin = "qq-private:compact-card-player"
        bound = await self.database.bind_card_code(
            reserved["binding_code"],
            "private-compact-card-player",
            origin,
        )
        identity_fields = {
            item["key"]: item
            for item in bound["template"]["fields"]
            if item["key"] in {"name", "code"}
        }
        self.assertEqual(identity_fields["name"]["max_chars"], 12)
        self.assertEqual(identity_fields["code"]["max_chars"], 12)

        with self.assertRaisesRegex(ValueError, "不能包含空格"):
            await self.database.fill_card_draft(origin, "角色 名")
        with self.assertRaisesRegex(ValueError, "12 字符上限"):
            await self.database.fill_card_draft(origin, "甲" * 13)
        await self.database.fill_card_draft(origin, "甲" * 12)

        with self.assertRaisesRegex(ValueError, "不能包含空格"):
            await self.database.fill_card_draft(origin, "CODE 1")
        with self.assertRaisesRegex(ValueError, "12 字符上限"):
            await self.database.fill_card_draft(origin, "A" * 13)
        await self.database.fill_card_draft(origin, "A" * 12)

        used_select_values: set[str] = set()
        for field in bound["template"]["fields"][2:]:
            if field.get("type") == "preset_select" and field.get("options"):
                values = [
                    str(item.get("value") or item.get("label") or item)
                    if isinstance(item, dict) else str(item)
                    for item in field["options"]
                ]
                value = next(
                    (item for item in values if item not in used_select_values),
                    values[0],
                )
                used_select_values.add(value)
            else:
                value = (
                    str(field.get("default", 0))
                    if field.get("type") == "integer"
                    else "无"
                )
            await self.database.fill_card_draft(origin, value)

        with closing(sqlite3.connect(self.database.path)) as connection:
            row = connection.execute(
                """
                SELECT d.id, d.fields_json
                FROM character_card_drafts d
                JOIN participants pt ON pt.id = d.participant_id
                WHERE pt.private_origin = ?
                """,
                (origin,),
            ).fetchone()
            fields = json.loads(row[1])
            fields["background"] = "旧草稿 含空格"
            connection.execute(
                """
                UPDATE character_card_drafts
                SET fields_json = ? WHERE id = ?
                """,
                (
                    json.dumps(fields, ensure_ascii=False),
                    row[0],
                ),
            )
            connection.commit()
        confirmed = await self.database.confirm_card_draft(origin)
        self.assertEqual(confirmed["card_status"], "pending_review")
        roster_item = (await self.database.list_roster(self.session["id"]))[0]
        self.assertEqual(roster_item["card_profile"]["background"], "旧草稿 含空格")

    async def test_quick_restore_points_and_timestamped_files_are_layered(
        self,
    ) -> None:
        await self._commit(interval=1)
        story_dir = self._story_dir()
        snapshots = await self.database.list_snapshots(self.session["id"])
        self.assertTrue(any(item["kind"] == "undo" for item in snapshots))
        backup_names = [
            item.name for item in (story_dir / "backups").glob("*.zip")
        ]
        self.assertEqual(len(backup_names), 1)
        self.assertRegex(
            backup_names[0],
            r"^backup_aelvion-ashen-crown_\d{14}(?:_\d{2})?\.zip$",
        )
        await self.database.create_snapshot(
            self.session["id"],
            "进入地下室之前",
            "admin",
        )
        save_files = list((story_dir / "saves").glob("*.zip"))
        self.assertEqual(len(save_files), 1)
        self.assertRegex(
            save_files[0].name,
            r"^save_aelvion-ashen-crown_\d{14}(?:_\d{2})?\.zip$",
        )
        with zipfile.ZipFile(save_files[0]) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "instance.sqlite3",
                    "manifest.json",
                    "group_snapshot.json",
                    "checksum.sha256",
                },
            )
            checksum = archive.read("checksum.sha256").decode("utf-8")
        self.assertIn("instance.sqlite3", checksum)
        self.assertIn("manifest.json", checksum)

    async def test_finished_story_replay_gets_new_directory_and_id(
        self,
    ) -> None:
        first_dir = self._story_dir()
        finished = await self.database.finalize_session(
            self.session["id"],
            "admin",
            termination_type="completed",
            reason="首轮完成",
        )
        self.assertEqual(finished["state"], SESSION_FINISHED)
        replay = await self.database.ensure_session(
            self.session["platform_id"],
            self.session["group_id"],
            self.session["unified_origin"],
            self.session["world_id"],
            "admin",
            self.session["instance_slug"],
            self.session["instance_name"],
        )
        self.assertNotEqual(replay["id"], self.session["id"])
        self.assertRegex(
            replay["instance_slug"],
            r"^aelvion-ashen-crown-run-\d{14}(?:-\d{2})?$",
        )
        replay_dir = self._story_dir(replay)
        self.assertNotEqual(first_dir, replay_dir)
        self.assertTrue(first_dir.exists())
        self.assertTrue(replay_dir.exists())
        manifest = json.loads(
            (replay_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["session"]["playthrough_no"], 2)
        self.assertTrue(list((first_dir / "saves").glob("save_*.zip")))

    async def test_import_rebuilds_untrusted_story_paths(self) -> None:
        bundle = await self.database.export_bundle()
        self.assertEqual(len(bundle["data"]["story_storage"]), 1)
        restored_dir = tempfile.TemporaryDirectory()
        try:
            restored_root = Path(restored_dir.name)
            escape_target = restored_root.parent / (
                f"outside-{restored_root.name}"
            )
            bundle["data"]["story_storage"][0]["relative_path"] = (
                f"../{escape_target.name}"
            )
            restored = TavernDatabase(restored_root)
            await restored.import_bundle(bundle, "replace", "web:admin")
            storage = await restored.get_storage_info(self.session["id"])
            relative = Path(storage["relative_path"])
            self.assertEqual(relative.parts[0], "groups")
            self.assertNotIn("..", relative.parts)
            self.assertTrue(
                (restored_root / relative / "instance.sqlite3").exists()
            )
            self.assertFalse(escape_target.exists())
            with restored._connect() as connection:
                connection.execute(
                    """
                    UPDATE story_storage SET relative_path = ?
                    WHERE session_id = ?
                    """,
                    (f"../{escape_target.name}", self.session["id"]),
                )
            repaired = await restored.get_storage_info(self.session["id"])
            repaired_relative = Path(repaired["relative_path"])
            self.assertEqual(repaired_relative.parts[0], "groups")
            self.assertNotIn("..", repaired_relative.parts)
            self.assertFalse(escape_target.exists())
        finally:
            restored_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
