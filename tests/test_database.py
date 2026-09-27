from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_CLOSED,
    SESSION_PAUSED,
    SESSION_RUNNING,
)
from tavern.database import (
    DatabaseConflictError,
    InvalidTransitionError,
    TavernDatabase,
)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp_dir.name))
        self.session = await self.database.ensure_session(
            "qq",
            "group-100",
            "qq:group-100",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _start(self) -> dict:
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        return self.session

    async def _commit(
        self,
        session: dict,
        *,
        fact: str,
        user_id: str = "user-1",
        display_name: str = "旅客",
    ) -> dict:
        player = await self.database.ensure_player(
            session["id"],
            user_id,
            display_name,
        )
        state = dict(session["world_state"])
        state["scene_summary"] = fact
        facts = list(state.get("facts", []))
        facts.append(fact)
        state["facts"] = facts
        return await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=player["id"],
            player_user_id=player["user_id"],
            player_name=player["display_name"],
            player_input=f"尝试：{fact}",
            narrative=f"结果：{fact}",
            world_state=state,
            memories=[
                {
                    "scope": "world",
                    "scope_id": "",
                    "kind": "fact",
                    "content": fact,
                    "importance": 4,
                    "tags": ["测试"],
                }
            ],
            check_payload=None,
            model_payload={"mode": "resolve"},
            director_note="测试裁定",
            auto_snapshot_interval=5,
            store_model_payload=False,
        )

    async def test_group_can_keep_multiple_isolated_instances(self) -> None:
        first = await self._start()
        first = await self._commit(
            first,
            fact="一号副本已经取得铜钥匙",
        )
        second = await self.database.ensure_session(
            "qq",
            "group-100",
            "qq:group-100",
            DEFAULT_WORLD_SLUG,
            "admin-1",
            "border-tavern-second",
            "边境酒馆二号副本",
        )
        self.assertFalse(second["selected"])
        self.assertEqual(first["world_id"], second["world_id"])
        self.assertEqual(second["turn_no"], 0)
        self.assertNotIn(
            "一号副本已经取得铜钥匙",
            second["world_state"].get("facts", []),
        )
        self.assertEqual(
            len(
                await self.database.list_group_sessions(
                    "qq",
                    "group-100",
                )
            ),
            2,
        )

        second = await self.database.transition_session(
            second["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        first = await self.database.get_session(first["id"])
        self.assertEqual(first["state"], SESSION_PAUSED)
        self.assertIn(
            "一号副本已经取得铜钥匙",
            first["world_state"].get("facts", []),
        )
        self.assertFalse(first["selected"])
        self.assertTrue(second["selected"])
        selected = await self.database.get_session_by_group(
            "qq",
            "group-100",
        )
        self.assertEqual(selected["id"], second["id"])
        self.assertEqual(
            selected["instance_slug"],
            "border-tavern-second",
        )

    @unittest.skip(
        "B1 是刻意干净基线（database.py 注释：永不读写旧版 "
        "tavern.sqlite3/catalog.sqlite3；ARCHITECTURE.md：Schema 1—7 不受支持），"
        "该用例测试的是已被移除的 v1 迁移路径，属于过时用例。"
    )
    async def test_v1_session_schema_migrates_to_named_instance(self) -> None:
        legacy_dir = tempfile.TemporaryDirectory()
        try:
            path = Path(legacy_dir.name) / "tavern.sqlite3"
            now = "2026-01-01T00:00:00+00:00"
            # closing：sqlite3 连接上下文管理器不负责关闭，Windows 下
            # 不关闭会让临时目录清理因文件占用而失败。
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE worlds (
                        id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        system_prompt TEXT NOT NULL,
                        rules_json TEXT NOT NULL DEFAULT '{}',
                        opening_scene TEXT NOT NULL DEFAULT '',
                        initial_state_json TEXT NOT NULL DEFAULT '{}',
                        archived INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        unified_origin TEXT NOT NULL DEFAULT '',
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        state TEXT NOT NULL DEFAULT 'closed',
                        turn_no INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        world_state_json TEXT NOT NULL DEFAULT '{}',
                        history_floor_seq INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id)
                    );
                    CREATE TABLE players (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id)
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO worlds(
                        id, slug, name, system_prompt, initial_state_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "world_legacy",
                        "legacy-world",
                        "旧版世界",
                        "保持连续。",
                        json.dumps({"location": "旧址"}),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin, world_id,
                        state, world_state_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        "session_legacy",
                        "qq",
                        "legacy-group",
                        "qq:legacy-group",
                        "world_legacy",
                        json.dumps({"location": "旧址"}),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO players(
                        id, session_id, user_id, display_name,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "player_legacy",
                        "session_legacy",
                        "legacy-user",
                        "旧版玩家",
                        now,
                        now,
                    ),
                )

            migrated = TavernDatabase(Path(legacy_dir.name))
            session = await migrated.get_session_by_group(
                "qq",
                "legacy-group",
            )
            self.assertEqual(session["instance_slug"], "legacy-world")
            self.assertEqual(session["instance_name"], "旧版世界")
            self.assertTrue(session["selected"])
            self.assertEqual(session["state"], SESSION_PAUSED)
            players = await migrated.list_players(session["id"])
            self.assertEqual(
                [(item["user_id"], item["display_name"]) for item in players],
                [("legacy-user", "旧版玩家")],
            )
            roster = await migrated.list_roster(session["id"])
            self.assertEqual(len(roster), 1)
            self.assertEqual(roster[0]["card_status"], "approved")
            self.assertEqual(roster[0]["group_user_id"], "legacy-user")
            self.assertIsNotNone(migrated.migration_backup_path)
            self.assertTrue(
                (Path(legacy_dir.name) / "catalog.sqlite3").exists()
            )
            self.assertFalse(path.exists())
            retained = list(
                (
                    Path(legacy_dir.name) / "migration_backups"
                ).glob("backup_legacy_tavern_*.sqlite3")
            )
            self.assertEqual(len(retained), 1)
            self.assertRegex(
                retained[0].name,
                r"^backup_legacy_tavern_\d{14}(?:_\d{2})?\.sqlite3$",
            )
            storage = await migrated.get_storage_info(session["id"])
            story_dir = Path(legacy_dir.name) / storage["relative_path"]
            self.assertTrue((story_dir / "instance.sqlite3").exists())
            self.assertTrue(
                list((story_dir / "backups").glob("backup_*.zip"))
            )
        finally:
            legacy_dir.cleanup()

    async def test_multiplayer_turn_order_advances_atomically(self) -> None:
        session = await self._start()
        first = await self.database.join_turn_order(
            session["id"],
            "user-a",
            "甲",
            "user-a",
        )
        second = await self.database.join_turn_order(
            session["id"],
            "user-b",
            "乙",
            "user-b",
        )
        self.assertTrue(first["joined"])
        self.assertTrue(second["joined"])
        self.assertEqual(second["turn"]["current_user_id"], "user-a")

        current = await self.database.get_session(session["id"])
        current = await self._commit(
            current,
            fact="甲先检查柜台",
            user_id="user-a",
            display_name="甲",
        )
        turn = await self.database.get_turn_status(session["id"])
        self.assertEqual(turn["current_user_id"], "user-b")
        self.assertEqual(turn["round_no"], 1)

        with self.assertRaisesRegex(InvalidTransitionError, "当前轮到"):
            await self._commit(
                current,
                fact="甲试图连续行动",
                user_id="user-a",
                display_name="甲",
            )
        self.assertEqual(
            (await self.database.get_session(session["id"]))["turn_no"],
            1,
        )

        current = await self._commit(
            current,
            fact="乙查看门外",
            user_id="user-b",
            display_name="乙",
        )
        turn = await self.database.get_turn_status(session["id"])
        self.assertEqual(turn["current_user_id"], "user-a")
        self.assertEqual(turn["round_no"], 2)
        self.assertEqual(current["turn_no"], 2)

    async def test_leaving_or_skipping_current_player_keeps_queue_live(
        self,
    ) -> None:
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            session["id"], "user-b", "乙", "user-b"
        )
        skipped = await self.database.skip_turn(
            session["id"],
            "user-a",
            "user-a",
        )
        self.assertEqual(skipped["current_user_id"], "user-b")
        left = await self.database.leave_turn_order(
            session["id"],
            "user-b",
            "user-b",
        )
        self.assertTrue(left["removed"])
        self.assertEqual(left["turn"]["current_user_id"], "user-a")

    async def test_snapshot_restores_multiplayer_turn_token(self) -> None:
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            session["id"], "user-b", "乙", "user-b"
        )
        await self.database.create_snapshot(
            session["id"],
            "轮次起点",
            "admin-1",
        )
        await self.database.skip_turn(
            session["id"],
            "user-a",
            "user-a",
        )
        self.assertEqual(
            (await self.database.get_turn_status(session["id"]))[
                "current_user_id"
            ],
            "user-b",
        )
        await self.database.restore_snapshot(
            session["id"],
            "轮次起点",
            "admin-1",
        )
        restored = await self.database.get_turn_status(session["id"])
        self.assertEqual(restored["current_user_id"], "user-a")
        self.assertEqual(
            [item["user_id"] for item in restored["order"]],
            ["user-a", "user-b"],
        )

    async def test_snapshot_restores_players_enabled_after_declare_death(self) -> None:
        # 2026-08-24：declare_death 同时把 participants 设为 retired
        # 并把 players.enabled 置 0。旧 snapshot 只存 world_state_json
        # （含 __tavern_turn_order__），不存 players → 回退后 order 数组
        # 看起来恢复（梧桐仍在），但 commit_turn 用 players.enabled=1
        # 白名单过滤时静默剔除 → 回合永远轮不到死者。本测试锁定新行为：
        # snapshot 必须捕获 players，回退时同步还原 enabled。
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        await self.database.join_turn_order(
            session["id"], "user-b", "乙", "user-b"
        )
        # 种子 participants（join_turn_order 只创建 players，participants
        # 由角色卡审批流创建；这里直接 SQL 模拟审批通过后的状态）。
        with self.database._connect() as connection:
            for uid, name in [("user-a", "甲"), ("user-b", "乙")]:
                connection.execute(
                    """
                    INSERT INTO participants(
                        id, session_id, player_id, group_user_id,
                        private_user_id, private_origin, display_name,
                        character_name, character_code, card_status, ready,
                        participation_status, seat_reserved_at,
                        joined_round, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', '', ?, ?, ?, 'approved', 1,
                              'active', '2026-08-24T09:00:00', 1,
                              '2026-08-24T09:00:00', '2026-08-24T09:00:00')
                    """,
                    (
                        f"participant_{uid}",
                        session["id"],
                        # player_id 通过 players.user_id 解析
                        None,
                        uid,
                        name,
                        name,
                        name,
                    ),
                )
        snapshot = await self.database.create_snapshot(
            session["id"],
            "死亡前",
            "admin-1",
        )
        # 模拟 declare_death 路径：participants 设为 retired + exit_reason，
        # players.enabled 设为 0。两者必须同时回退。
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE participants SET participation_status='retired',
                  exit_reason='lethal_check_death', updated_at='2026-08-24T10:00:00'
                WHERE session_id=? AND group_user_id='user-b'
                """,
                (session["id"],),
            )
            connection.execute(
                """
                UPDATE players SET enabled=0, updated_at='2026-08-24T10:00:00'
                WHERE session_id=? AND user_id='user-b'
                """,
                (session["id"],),
            )
        # 回退到死亡前快照
        await self.database.restore_snapshot(
            session["id"], snapshot["id"], "admin-1"
        )
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT participation_status, exit_reason FROM participants
                WHERE session_id=? AND group_user_id='user-b'
                """,
                (session["id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["participation_status"], "active")
            self.assertEqual(row["exit_reason"], "")
            row = connection.execute(
                "SELECT enabled FROM players WHERE session_id=? AND user_id='user-b'",
                (session["id"],),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(
                int(row["enabled"]), 1,
                "死亡前的玩家 enabled 必须回退到 1，否则 commit_turn 过滤会静默剔除"
            )

    async def test_snapshot_restore_keeps_players_for_legacy_snapshots(self) -> None:
        # 2026-08-24：旧快照没有 players 字段，回退时不能 DELETE 现有
        # players 行（避免把活人的 enabled 一起清掉），也不能 INSERT 空
        # 列表把表清空——保持现状不变。
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        # 直接构造一个 workflow_json 不带 players 字段的旧快照
        with self.database._connect() as connection:
            world_row = connection.execute(
                "SELECT world_id FROM sessions WHERE id = ?",
                (session["id"],),
            ).fetchone()
            connection.execute(
                "INSERT INTO snapshots(id, session_id, name, kind, turn_no,"
                " session_revision, world_id, world_state_json, created_by,"
                " created_at) VALUES ('snap-legacy', ?, '旧存档', 'manual',"
                " 0, 0, ?, '{}', 'admin', '2026-08-24T09:00:00')",
                (session["id"], world_row["world_id"]),
            )
            connection.execute(
                "INSERT INTO snapshot_workflows(snapshot_id, workflow_json)"
                " VALUES ('snap-legacy', ?)",
                (json.dumps({
                    "format": "astrbot-tavern-workflow",
                    "version": 3,
                    "history_floor_seq": 0,
                    "event_anchor_seq": 0,
                    # 注意：没有 "players" 键
                    "participants": [],
                }),),
            )
        # 把 live players.enabled 改成 0（模拟管理员封禁）
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE players SET enabled=0 WHERE session_id=? AND user_id='user-a'",
                (session["id"],),
            )
        # 回退旧快照
        await self.database.restore_snapshot(
            session["id"], "snap-legacy", "admin-1"
        )
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM players WHERE session_id=? AND user_id='user-a'",
                (session["id"],),
            ).fetchone()
            self.assertEqual(
                int(row["enabled"]), 0,
                "旧快照回退不应触碰 players 表（没存就别瞎改）"
            )

    async def test_snapshot_restores_card_stats_json(self) -> None:
        # 2026-08-24 玩家反馈「快照回档后原本死亡的玩家属性值可能没有
        # 绑回去」：快照只存 participants（含 character_version_id），
        # 不存 character_card_versions → 回档后 version_id 恢复成快照
        # 时点值，但卡版本 stats_json 保持当前状态（玩家死后重新绑卡
        # 产生新版本），检定读到的 modifiers 是"新"版本的、旧属性值
        # 没绑回去。本测试锁定：快照收集含 character_cards +
        # character_card_versions，回档后 stats_json 还原。
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        # 种子 participant + 卡 + 卡版本（模拟审批通过后的绑定状态）
        with self.database._connect() as connection:
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name, archived,
                    deleted, current_version, created_at, updated_at
                ) VALUES (?, ?, (SELECT world_id FROM sessions WHERE id=?),
                          '甲', 0, 0, 1, '2026-08-24T09:00:00',
                          '2026-08-24T09:00:00')
                """,
                ("card-a", "user-a", session["id"]),
            )
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES (?, 'card-a', 1, 'tpl',
                          '{"name":"甲"}', ?, 'approved', '', 'admin',
                          '2026-08-24T09:00:00')
                """,
                ("cardver-a1", json.dumps(
                    {"modifiers": {"sword": 6}, "labels": {"sword": "剑术"}},
                    ensure_ascii=False,
                )),
            )
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id,
                    private_user_id, private_origin, display_name,
                    character_name, character_code, character_card_id,
                    character_version_id, card_status, ready,
                    participation_status, seat_reserved_at,
                    joined_round, created_at, updated_at
                ) VALUES (?, ?, NULL, 'user-a', '', '', '甲', '甲',
                          'jia', 'card-a', 'cardver-a1', 'approved', 1,
                          'active', '2026-08-24T09:00:00', 1,
                          '2026-08-24T09:00:00', '2026-08-24T09:00:00')
                """,
                (f"participant_{session['id']}_a", session["id"]),
            )
        # 快照：此时卡版本 stats.sword=6
        snapshot = await self.database.create_snapshot(
            session["id"], "死亡前", "admin-1"
        )
        # 模拟死亡后重新绑卡：卡版本 stats 改成 sword=-1
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE character_card_versions SET stats_json=? WHERE id='cardver-a1'",
                (json.dumps(
                    {"modifiers": {"sword": -1}, "labels": {"sword": "剑术"}},
                    ensure_ascii=False,
                ),),
            )
        # 回退到死亡前快照
        await self.database.restore_snapshot(
            session["id"], snapshot["id"], "admin-1"
        )
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT stats_json FROM character_card_versions WHERE id='cardver-a1'",
            ).fetchone()
            self.assertIsNotNone(row)
            stats = json.loads(row[0])
            self.assertEqual(
                stats["modifiers"]["sword"], 6,
                "回档后卡版本 stats_json 必须还原到快照时点，否则属性值没绑回去",
            )

    async def test_snapshot_legacy_does_not_touch_card_tables(self) -> None:
        # 2026-08-24：旧快照没有 character_cards / character_card_versions
        # 字段，回退时不能 DELETE 现有卡（避免把别的 session 共用的全局
        # 卡清掉）。快照 ids 为空 → 不删、不插，保持现状。
        session = await self._start()
        await self.database.join_turn_order(
            session["id"], "user-a", "甲", "user-a"
        )
        with self.database._connect() as connection:
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name, archived,
                    deleted, current_version, created_at, updated_at
                ) VALUES ('card-legacy', 'user-a',
                          (SELECT world_id FROM sessions WHERE id=?),
                          '甲', 0, 0, 1, '2026-08-24T09:00:00',
                          '2026-08-24T09:00:00')
                """,
                (session["id"],),
            )
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES ('cardver-legacy', 'card-legacy', 1, 'tpl',
                          '{"name":"甲"}',
                          '{"modifiers":{"sword":6}}', 'approved', '',
                          'admin', '2026-08-24T09:00:00')
                """
            )
            world_row = connection.execute(
                "SELECT world_id FROM sessions WHERE id=?",
                (session["id"],),
            ).fetchone()
            connection.execute(
                "INSERT INTO snapshots(id, session_id, name, kind, turn_no,"
                " session_revision, world_id, world_state_json, created_by,"
                " created_at) VALUES ('snap-legacy2', ?, '旧存档2', 'manual',"
                " 0, 0, ?, '{}', 'admin', '2026-08-24T09:00:00')",
                (session["id"], world_row["world_id"]),
            )
            connection.execute(
                "INSERT INTO snapshot_workflows(snapshot_id, workflow_json)"
                " VALUES ('snap-legacy2', ?)",
                (json.dumps({
                    "format": "astrbot-tavern-workflow",
                    "version": 3,
                    "history_floor_seq": 0,
                    "event_anchor_seq": 0,
                    "participants": [],
                    # 注意：没有 character_cards / character_card_versions 键
                }),),
            )
        # 把 live 卡版本改坏，验证旧快照回退不会动它
        with self.database._connect() as connection:
            connection.execute(
                "UPDATE character_card_versions SET stats_json='{\"modifiers\":{\"sword\":-1}}' WHERE id='cardver-legacy'",
            )
        await self.database.restore_snapshot(
            session["id"], "snap-legacy2", "admin-1"
        )
        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT stats_json FROM character_card_versions WHERE id='cardver-legacy'",
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("-1", row[0],
                "旧快照回退不应触碰卡版本表（没存就别瞎改）"
            )


    async def test_state_machine_and_closed_world_switch(self) -> None:
        session = await self._start()
        world = await self.database.save_world(
            {
                "slug": "another-world",
                "name": "另一个世界",
                "description": "测试",
                # 0.11.0 起校验要求显式声明世界协议版本，裸世界 v0 会被拒绝。
                "world_schema_version": 2,
                "system_prompt": "严格遵守因果。",
                "opening_scene": "新场景。",
                "rules": {"resolution": "d20"},
                "initial_state": {
                    "location": "新地点",
                    "facts": [],
                    "inventory": {},
                    "relationships": {},
                },
            },
            "admin-1",
        )
        with self.assertRaises(InvalidTransitionError):
            await self.database.transition_session(
                session["id"],
                SESSION_RUNNING,
                "admin-1",
                world["slug"],
            )
        session = await self.database.transition_session(
            session["id"],
            SESSION_CLOSED,
            "admin-1",
        )
        session = await self.database.transition_session(
            session["id"],
            SESSION_RUNNING,
            "admin-1",
            world["slug"],
        )
        self.assertEqual(session["world_id"], world["id"])
        self.assertEqual(session["turn_no"], 0)
        self.assertEqual(session["world_state"]["location"], "新地点")

    async def test_archived_world_stays_archived_until_explicit_restore(self) -> None:
        world = await self.database.get_world(DEFAULT_WORLD_SLUG)
        world = await self.database.archive_world(world["id"], "admin-1")
        self.assertTrue(world["archived"])
        # 用可移植载荷重存（保留 world_schema_version / minimum_plugin_version /
        # protocol / required_features），避免 v5 契约校验误伤。
        from tavern.world_import import world_import_payload

        payload = world_import_payload(world)
        payload.update(
            {
                "id": world["id"],
                "revision": world["revision"],
                "name": "已归档但可编辑",
            }
        )
        world = await self.database.save_world(payload, "admin-1")
        self.assertTrue(world["archived"])
        world = await self.database.restore_world(world["id"], "admin-1")
        self.assertFalse(world["archived"])

    async def test_single_turn_rollback_creates_new_history_branch(self) -> None:
        session = await self._start()
        session = await self._commit(session, fact="发现铜钥匙")
        session = await self._commit(session, fact="打开北侧门")
        self.assertEqual(session["turn_no"], 2)

        snapshots = await self.database.list_snapshots(session["id"])
        self.assertTrue(any(item["kind"] == "undo" for item in snapshots))

        restored = await self.database.restore_latest_auto(
            session["id"],
            "admin-1",
        )
        self.assertEqual(restored["state"], SESSION_PAUSED)
        self.assertEqual(restored["turn_no"], 1)
        self.assertNotIn(
            "打开北侧门",
            restored["world_state"].get("facts", []),
        )

        visible = await self.database.recent_events(session["id"], 100)
        self.assertEqual(
            [item["role"] for item in visible],
            ["player", "narrator", "system"],
        )
        self.assertTrue(
            any("发现铜钥匙" in item["content"] for item in visible)
        )
        self.assertFalse(
            any("打开北侧门" in item["content"] for item in visible)
        )
        restored = await self.database.transition_session(
            restored["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        restored = await self._commit(restored, fact="改走南侧门")
        visible = await self.database.recent_events(session["id"], 100)
        contents = [item["content"] for item in visible]
        self.assertTrue(any("改走南侧门" in item for item in contents))
        self.assertFalse(any("打开北侧门" in item for item in contents))

    async def test_optimistic_revision_rejects_double_commit(self) -> None:
        original = await self._start()
        await self._commit(original, fact="第一条结果")
        with self.assertRaises(DatabaseConflictError):
            await self._commit(original, fact="过期请求")
        current = await self.database.get_session(original["id"])
        self.assertEqual(current["turn_no"], 1)

    async def test_merge_backup_is_insert_only_and_preserves_live_data(self) -> None:
        session = await self._start()
        session = await self._commit(session, fact="保留这条时间线")
        bundle = await self.database.export_bundle()
        bundle["data"]["worlds"][0]["name"] = "合并后的世界名"

        counts = await self.database.import_bundle(
            bundle,
            "merge",
            "web:tester",
        )
        self.assertEqual(counts["audit_logs"], 0)
        current = await self.database.get_session(session["id"])
        self.assertNotEqual(current["world_name"], "合并后的世界名")
        self.assertEqual(len(await self.database.list_players(session["id"])), 1)
        self.assertEqual(len(await self.database.recent_events(session["id"], 20)), 2)

    async def test_merge_rejects_same_identity_with_different_id(self) -> None:
        bundle = await self.database.export_bundle()
        conflicting = copy.deepcopy(bundle)
        conflicting["data"]["worlds"][0]["id"] = "world_foreign"
        with self.assertRaisesRegex(ValueError, "取消合并"):
            await self.database.import_bundle(
                conflicting,
                "merge",
                "web:tester",
            )

    async def test_newer_backup_schema_is_rejected(self) -> None:
        bundle = await self.database.export_bundle()
        bundle["schema_version"] = 999
        with self.assertRaisesRegex(ValueError, "升级插件"):
            await self.database.import_bundle(
                bundle,
                "replace",
                "web:tester",
            )

    async def test_replace_backup_round_trip(self) -> None:
        session = await self._start()
        session = await self._commit(session, fact="可恢复事实")
        bundle = await self.database.export_bundle()

        other_dir = tempfile.TemporaryDirectory()
        try:
            other = TavernDatabase(Path(other_dir.name))
            counts = await other.import_bundle(
                bundle,
                "replace",
                "web:tester",
            )
            self.assertGreaterEqual(counts["worlds"], 1)
            restored = await other.get_session(session["id"])
            self.assertEqual(restored["turn_no"], 1)
            self.assertIn(
                "可恢复事实",
                restored["world_state"].get("facts", []),
            )
        finally:
            other_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
