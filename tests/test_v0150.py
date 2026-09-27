"""v0.12.0-A15 回归测试。

覆盖：
1. 缺陷修复：角色卡修订（含改名）批准后同步 players 表，回合状态
   （get_turn_status）与行动选项（participants）不再显示不同名字。
2. 缺陷修复：check_context 对超长角色卡文本字段（背景/能力/秘密等，
   模板允许 500-800 字）截断而非抛「内容超过 500 字符上限」。
3. 缺陷修复：剧情搜索（story 范围）真正检索事件正文。
4. 新增功能：默认世界 elemental 示例合法且体检通过。
5. 新增功能：auto_backup / webhook 配置解析与钳制。
6. 新增功能：完整备份 ZIP 导出与保留清理。
"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tavern.config import TavernConfig
from tavern.constants import DEFAULT_WORLD, DEFAULT_WORLD_SLUG
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now
from tavern.security import clean_text, truncate_text
from tavern.backup_service import build_backup_archive, prune_backups


class A15ConfigTests(unittest.TestCase):
    def test_truncate_text_cuts_without_raising(self) -> None:
        self.assertEqual(truncate_text("abc", max_chars=2), "ab")
        self.assertEqual(truncate_text("abc", max_chars=10), "abc")
        self.assertEqual(truncate_text("  a\u0000b  ", max_chars=10), "ab")
        with self.assertRaises(ValueError):
            clean_text("abc", max_chars=2)

    def test_config_parses_auto_backup_and_webhook(self) -> None:
        config = TavernConfig.from_mapping(
            {
                "auto_backup": {
                    "enabled": True,
                    "interval_hours": 6,
                    "keep_count": 3,
                },
                "webhook": {
                    "enabled": True,
                    "urls": ["https://example.com/hook"],
                    "secret": "s3cret",
                    "events": ["turn", "backup"],
                    "timeout_seconds": 5,
                },
            }
        )
        self.assertTrue(config.auto_backup_enabled)
        self.assertEqual(config.auto_backup_interval_hours, 6)
        self.assertEqual(config.auto_backup_keep_count, 3)
        self.assertTrue(config.webhook_enabled)
        self.assertEqual(config.webhook_urls, ("https://example.com/hook",))
        self.assertEqual(config.webhook_secret, "s3cret")
        self.assertEqual(config.webhook_events, ("turn", "backup"))
        self.assertEqual(config.webhook_timeout_seconds, 5)

        default = TavernConfig.from_mapping({})
        self.assertFalse(default.auto_backup_enabled)
        self.assertEqual(default.auto_backup_interval_hours, 24.0)
        self.assertEqual(default.auto_backup_keep_count, 7)
        self.assertFalse(default.webhook_enabled)
        self.assertEqual(default.webhook_urls, ())

        clamped = TavernConfig.from_mapping(
            {
                "auto_backup": {"interval_hours": 0, "keep_count": 9999},
                "webhook": {"timeout_seconds": 0},
            }
        )
        self.assertGreaterEqual(clamped.auto_backup_interval_hours, 1.0)
        self.assertLessEqual(clamped.auto_backup_keep_count, 365)
        self.assertGreaterEqual(clamped.webhook_timeout_seconds, 1.0)

    def test_default_world_elemental_is_valid(self) -> None:
        from tavern.elemental import parse, validate

        parsed = parse(DEFAULT_WORLD)
        self.assertIn("火", parsed["elements"])
        self.assertIn("npc:林语者莎芮", parsed["affinities"])
        self.assertTrue(parsed["reactions"])
        self.assertEqual(validate(DEFAULT_WORLD), [])
        from tavern.world_preflight import inspect_world_package

        report = inspect_world_package(DEFAULT_WORLD)
        self.assertTrue(report["compatible"])


class A15DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data_dir = Path(self.temp.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq", "group-a15", "qq:group-a15", DEFAULT_WORLD_SLUG, "admin-1"
        )
        await self.database.transition_session(
            self.session["id"], "preparing", "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _seed_card(
        self,
        user_id: str,
        name: str,
        *,
        background: str = "背景",
    ) -> tuple[str, str, str]:
        """建参与者 + 角色卡 + 版本，返回 (participant_id, card_id, version_id)。"""
        reserved = await self.database.reserve_participant(
            self.session["id"], user_id, name
        )
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pid = reserved["id"]
            card_id, ver_id = new_id("card"), new_id("cardver")
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name,
                    archived, deleted, current_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, 1, ?, ?)
                """,
                (
                    card_id,
                    user_id,
                    (await self.database.get_world(DEFAULT_WORLD_SLUG))["id"],
                    name,
                    now,
                    now,
                ),
            )
            profile = json.dumps(
                {
                    "name": name,
                    "code": "A15",
                    "background": background,
                    "specialties": ["火焰抗性"],
                },
                ensure_ascii=False,
            )
            stats = json.dumps(
                {"modifiers": {"体魄": 2}, "labels": {"体魄": "体魄"}},
                ensure_ascii=False,
            )
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES (?, ?, 1, 6, ?, ?, 'approved', '', 'admin-1', ?)
                """,
                (ver_id, card_id, profile, stats, now),
            )
            connection.execute(
                """
                UPDATE participants SET character_card_id = ?,
                    character_version_id = ?, card_status = 'approved',
                    participation_status = 'active'
                WHERE id = ?
                """,
                (card_id, ver_id, pid),
            )
            connection.execute("COMMIT")
        return pid, card_id, ver_id

    async def test_revision_approval_syncs_players_table(self) -> None:
        """改名/改卡修订批准后，players.character_name 与 participants 一致。"""
        pid, card_id, base_ver = await self._seed_card("user-r", "旧名A")
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cand_ver = new_id("cardver")
            profile = json.dumps({"name": "新名B", "code": "A15"},
                                 ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES (?, ?, 2, 6, ?, '{}', 'pending_review', '', '', ?)
                """,
                (cand_ver, card_id, profile, now),
            )
            request_id = new_id("cardedit")
            connection.execute(
                """
                INSERT INTO card_revision_requests(
                    id, session_id, participant_id, character_card_id,
                    base_version_id, candidate_version_id, status,
                    request_note, review_note, requested_by,
                    reviewed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', '', 'admin-1',
                          '', ?, ?)
                """,
                (
                    request_id,
                    self.session["id"],
                    pid,
                    card_id,
                    base_ver,
                    cand_ver,
                    now,
                    now,
                ),
            )
            connection.execute("COMMIT")

        await self.database.review_card_revision(
            request_id, True, "admin-1", "改名"
        )
        with self.database._connect() as connection:
            player = connection.execute(
                """
                SELECT p.character_name
                FROM participants pt JOIN players p ON p.id = pt.player_id
                WHERE pt.id = ?
                """,
                (pid,),
            ).fetchone()
            participant = connection.execute(
                "SELECT character_name FROM participants WHERE id = ?",
                (pid,),
            ).fetchone()
        self.assertEqual(player["character_name"], "新名B")
        self.assertEqual(participant["character_name"], "新名B")

    async def test_check_context_tolerates_long_profile_fields(self) -> None:
        """>500 字的角色卡字段不再让检定上下文编译抛错。"""
        long_background = "长" * 700
        pid, _, _ = await self._seed_card(
            "user-c", "长文玩家", background=long_background
        )
        self.assertGreater(len(long_background), 500)
        context = await self.database.check_context(
            self.session["id"],
            "user-c",
            "体魄",
            proposed_advantages=["专长：火焰抗性"],
        )
        self.assertEqual(context["participant_id"], pid)
        self.assertIn("火焰抗性", "".join(context["advantages"]))

    async def test_story_search_finds_narrative_events(self) -> None:
        """story 检索范围现在会命中剧情正文。"""
        now = utc_now()
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, 1, 'story', '', '', ?, '{}', ?)
                """,
                (new_id("event"), self.session["id"],
                 "边境的篝火映照着斑驳的城墙，风里带着铁锈味。", now),
            )
            connection.execute("COMMIT")
        result = await self.database.search_sessions(
            "篝火", "story", 1, 20
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], self.session["id"])

    async def test_backup_archive_round_trip_and_prune(self) -> None:
        """自动备份生成完整 ZIP，并按保留份数清理旧备份。"""
        export_dir = self.data_dir / "exports"
        path1 = await build_backup_archive(
            data_dir=self.data_dir,
            database=self.database,
            export_dir=export_dir,
        )
        self.assertTrue(path1.exists())
        with zipfile.ZipFile(path1) as archive:
            names = set(archive.namelist())
            self.assertIn("bundle.json", names)
            self.assertIn("catalog.sqlite3", names)
            self.assertIn("checksum.sha256", names)
        path2 = await build_backup_archive(
            data_dir=self.data_dir,
            database=self.database,
            export_dir=export_dir,
        )
        self.assertNotEqual(path1, path2)
        removed = prune_backups(export_dir, keep_count=1)
        self.assertIn(path1 if path1 != path2 else path2, removed)
        remaining = list(export_dir.glob("backup_tavern_*.zip"))
        self.assertEqual(len(remaining), 1)


if __name__ == "__main__":
    unittest.main()
