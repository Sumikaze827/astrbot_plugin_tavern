"""删除世界包（连带它展开的所有副本）。

为什么这些用例必须对着**真实数据库**跑：外键是开的
（``PRAGMA foreign_keys = ON``），而三张表挡住了天真的删除——

    sessions.world_id        → ON DELETE NO ACTION   （有本就删不掉）
    world_snapshots.world_id → ON DELETE RESTRICT    （有快照就删不掉）
    character_cards.world_id → ON DELETE NO ACTION   （有卡就删不掉）

用手搓的假数据库测这件事毫无意义：假实现里根本不会有这些约束，
"删不掉"和"删干净了"都测不出来。所以这里建真库、跑真 SQL。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_CLOSED,
    SESSION_RUNNING,
)
from tavern.database import (
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)


def _world_payload(slug: str, name: str, **extra) -> dict:
    payload = {
        "slug": slug,
        "name": name,
        "description": "测试世界",
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
    }
    payload.update(extra)
    return payload


class WorldDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp_dir.name))
        self.world = await self.database.save_world(
            _world_payload("doomed-world", "注定被删的世界"), "admin-1"
        )
        self.session = await self.database.ensure_session(
            "qq",
            "group-900",
            "qq:group-900",
            "doomed-world",
            "admin-1",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _flatten(self) -> dict:
        """把副本推到可删状态（closed）。"""
        session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        session = await self.database.transition_session(
            session["id"], SESSION_CLOSED, "admin-1"
        )
        self.session = session
        return session

    async def _rows(self, sql: str, *args) -> int:
        def run() -> int:
            with self.database._connect() as connection:
                return connection.execute(sql, args).fetchone()[0]

        return await self.database._run(run)

    # --- 预览 -------------------------------------------------------------

    async def test_impact_lists_the_sessions_this_world_spawned(self) -> None:
        impact = await self.database.world_deletion_impact(self.world["id"])
        self.assertEqual("doomed-world", impact["world"]["slug"])
        self.assertEqual(1, impact["session_count"])
        self.assertEqual(self.session["id"], impact["sessions"][0]["id"])
        self.assertEqual("注定被删的世界", impact["confirm_name"])

    async def test_preview_is_read_only(self) -> None:
        """预览不能顺手改任何东西——面板每次打开都会调它。"""
        before = await self._rows("SELECT COUNT(*) FROM sessions WHERE world_id = ?",
                                  self.world["id"])
        await self.database.world_deletion_impact(self.world["id"])
        after = await self._rows("SELECT COUNT(*) FROM sessions WHERE world_id = ?",
                                 self.world["id"])
        self.assertEqual(before, after)
        self.assertIsNotNone(await self.database.get_world(self.world["id"]))

    async def test_running_session_blocks_deletion(self) -> None:
        """跑着的本连着真人的一局游戏，不能删。"""
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        impact = await self.database.world_deletion_impact(self.world["id"])
        self.assertFalse(impact["can_delete"])
        self.assertEqual(1, len(impact["blocked_sessions"]))

        with self.assertRaises(InvalidTransitionError):
            await self.database.delete_world(
                self.world["id"], "admin-1", "注定被删的世界"
            )
        # 拒绝之后世界和本都还在
        self.assertIsNotNone(await self.database.get_world(self.world["id"]))
        self.assertEqual(
            1, await self._rows("SELECT COUNT(*) FROM sessions WHERE world_id = ?",
                                self.world["id"])
        )

    async def test_confirm_name_must_match(self) -> None:
        await self._flatten()
        for bad in ("", "  ", "别的名字"):
            with self.assertRaises(ValueError, msg=bad):
                await self.database.delete_world(self.world["id"], "admin-1", bad)
        self.assertIsNotNone(await self.database.get_world(self.world["id"]))

    async def test_unknown_world_is_rejected(self) -> None:
        with self.assertRaises(DatabaseNotFoundError):
            await self.database.world_deletion_impact("no-such-world")

    # --- 真删 -------------------------------------------------------------

    async def test_delete_removes_world_and_every_session(self) -> None:
        """核心用例：本连带它的全部记录一起消失。"""
        session_id = (await self._flatten())["id"]
        # 先造点副本数据，验证它们真的被级联清掉。
        # 这里直接插 events / players，不走 commit_turn——它要十几个必填参数，
        # 而本用例要证的只是"会话级联清干净"，不需要一整套回合流程。
        await self.database.ensure_player(session_id, "user-1", "旅客")

        def seed() -> None:
            with self.database._connect() as connection:
                connection.execute(
                    "INSERT INTO events(seq, id, session_id, turn_no, role,"
                    " actor_id, actor_name, content, meta_json, created_at)"
                    " VALUES(1, 'ev-1', ?, 1, 'player', 'user-1', '旅客',"
                    " '观察四周', '{}', '2026-01-01T00:00:00')",
                    (session_id,),
                )

        await self.database._run(seed)
        self.assertGreater(
            await self._rows("SELECT COUNT(*) FROM events WHERE session_id = ?", session_id),
            0,
            "前置条件：本里应该有回合记录",
        )

        result = await self.database.delete_world(
            self.world["id"], "admin-1", "注定被删的世界"
        )
        self.assertTrue(result["deleted"])
        self.assertEqual(1, result["session_count"])
        self.assertEqual(session_id, result["sessions"][0]["session_id"])

        # 世界没了
        with self.assertRaises(DatabaseNotFoundError):
            await self.database.get_world(self.world["id"])
        # 本没了
        self.assertEqual(
            0, await self._rows("SELECT COUNT(*) FROM sessions WHERE id = ?", session_id)
        )
        # 本下面的记录也跟着没了（级联）
        for table in ("events", "players", "snapshots", "story_ledger", "participants"):
            self.assertEqual(
                0,
                await self._rows(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", session_id
                ),
                msg=f"{table} 里还留着已删副本的记录",
            )

    async def test_delete_clears_the_restrict_tables_too(self) -> None:
        """world_snapshots 是 RESTRICT、character_cards 是 NO ACTION——
        不清掉它们，世界行根本删不动。这里验证确实清了。"""
        world_id = self.world["id"]
        await self._flatten()

        def seed() -> None:
            with self.database._connect() as connection:
                connection.execute(
                    "INSERT INTO world_snapshots(id, world_id, world_revision,"
                    " content_hash, snapshot_json, created_at)"
                    " VALUES('ws-1', ?, 1, 'h', '{}', '2026-01-01T00:00:00')",
                    (world_id,),
                )
                connection.execute(
                    "INSERT INTO character_cards(id, owner_user_id, world_id,"
                    " display_name, archived, deleted, current_version,"
                    " created_at, updated_at)"
                    " VALUES('cc-1', 'user-1', ?, '测试卡', 0, 0, 1,"
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                    (world_id,),
                )

        await self.database._run(seed)
        # save_world 自己也会落一份快照，所以这里只断言"确实有"。
        self.assertGreaterEqual(
            await self._rows("SELECT COUNT(*) FROM world_snapshots WHERE world_id = ?", world_id),
            1,
            "前置条件：世界应该有快照（否则 RESTRICT 拦不住，这条用例就白测了）",
        )

        await self.database.delete_world(world_id, "admin-1", "注定被删的世界")

        for table in ("world_snapshots", "character_cards"):
            self.assertEqual(
                0,
                await self._rows(f"SELECT COUNT(*) FROM {table} WHERE world_id = ?", world_id),
                msg=f"{table} 没被清掉",
            )

    async def test_orphan_tables_without_fk_are_cleaned(self) -> None:
        """有几张表有 session_id 却**没有**外键，不会随本一起级联。

        ``timer_instances`` 是这里最要命的一个：不清掉的话，死掉的副本的
        倒计时还会照常触发，在群里弹出指向一个已经不存在的本的提醒。
        实测生产库上这一个世界就留下 426 条。
        """
        session_id = (await self._flatten())["id"]

        def seed() -> None:
            with self.database._connect() as connection:
                connection.execute(
                    "INSERT INTO timer_instances(id, session_id, participant_id,"
                    " timer_type, status, deadline_at, remaining_seconds, reminder_at,"
                    " reminder_sent, action_json, created_at, updated_at)"
                    " VALUES('ti-1', ?, 'p-1', 'turn', 'active', '2026-01-02T00:00:00',"
                    " 60, '2026-01-01T23:59:00', 0, '{}', '2026-01-01T00:00:00',"
                    " '2026-01-01T00:00:00')",
                    (session_id,),
                )
                connection.execute(
                    "INSERT INTO operation_receipts(operation_id, session_id,"
                    " operation_type, request_json, result_json, status,"
                    " created_at, updated_at)"
                    " VALUES('op-1', ?, 'turn', '{}', '{}', 'completed',"
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                    (session_id,),
                )

        await self.database._run(seed)
        self.assertEqual(
            1, await self._rows("SELECT COUNT(*) FROM timer_instances WHERE session_id = ?", session_id)
        )

        await self.database.delete_world(
            self.world["id"], "admin-1", "注定被删的世界"
        )

        for table in ("timer_instances", "operation_receipts"):
            self.assertEqual(
                0,
                await self._rows(f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", session_id),
                msg=f"{table} 里还留着已删副本的孤儿行",
            )

    async def test_single_session_delete_also_cleans_orphans(self) -> None:
        """同一个洞在**单本删除**上也存在——顺手一起堵掉。

        不单独修的话，玩家自己删一个本，它的倒计时照样会继续触发。
        """
        session_id = (await self._flatten())["id"]

        def seed() -> None:
            with self.database._connect() as connection:
                connection.execute(
                    "INSERT INTO timer_instances(id, session_id, participant_id,"
                    " timer_type, status, deadline_at, remaining_seconds, reminder_at,"
                    " reminder_sent, action_json, created_at, updated_at)"
                    " VALUES('ti-2', ?, 'p-1', 'turn', 'active', '2026-01-02T00:00:00',"
                    " 60, '2026-01-01T23:59:00', 0, '{}', '2026-01-01T00:00:00',"
                    " '2026-01-01T00:00:00')",
                    (session_id,),
                )

        await self.database._run(seed)
        instance_name = self.session["instance_name"]
        await self.database.delete_session(session_id, "admin-1", instance_name)
        self.assertEqual(
            0,
            await self._rows("SELECT COUNT(*) FROM timer_instances WHERE session_id = ?", session_id),
            "单本删除后仍留下孤儿计时器",
        )

    async def test_delete_is_audited(self) -> None:
        await self._flatten()
        await self.database.delete_world(self.world["id"], "admin-1", "注定被删的世界")
        count = await self._rows(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'world.delete'"
        )
        self.assertEqual(1, count)

    async def test_world_without_sessions_deletes_cleanly(self) -> None:
        """没展开过本的世界也要能删——面板上大多是这种。"""
        lonely = await self.database.save_world(
            _world_payload("lonely-world", "没人玩过的世界"), "admin-1"
        )
        result = await self.database.delete_world(
            lonely["id"], "admin-1", "没人玩过的世界"
        )
        self.assertEqual(0, result["session_count"])
        with self.assertRaises(DatabaseNotFoundError):
            await self.database.get_world(lonely["id"])


class WorldDeleteRouteTests(unittest.TestCase):
    """两个新路由必须真的挂上去——绑错了只会静默 404/500。"""

    def test_routes_are_registered(self) -> None:
        from tests.test_worldgen_routes import _console, _install_astrbot_stubs

        _install_astrbot_stubs()
        tmp = tempfile.TemporaryDirectory()
        try:
            console, _ = _console(Path(tmp.name))
            for suffix in ("delete-preview", "delete"):
                path = f"/astrbot_plugin_tavern/worlds/{suffix}"
                self.assertIn(path, console.routes, msg=f"路由没注册：{path}")
                handler = console.routes[path][0]
                self.assertTrue(
                    callable(handler), msg=f"{path} 绑的不是可调用对象"
                )
                self.assertIn("POST", console.routes[path][1] or [])
        finally:
            tmp.cleanup()


class DefaultWorldGuardTests(unittest.TestCase):
    """默认世界不能删——这条在控制台路由里拦（要读插件配置）。"""

    def test_route_refuses_default_world(self) -> None:
        import asyncio

        from tests.test_worldgen_routes import _console, _install_astrbot_stubs

        _install_astrbot_stubs()
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        console, _ = _console(Path(tmp.name))

        payload = {"id": "whatever"}
        state = {"default": DEFAULT_WORLD_SLUG}

        class FakeConfig:
            default_world_slug = DEFAULT_WORLD_SLUG

        async def run() -> None:
            async def _p():
                return dict(payload)

            console._payload = _p
            console.plugin_config = {}

            class World(dict):
                pass

            async def get_world(_id):
                return {"id": "w1", "slug": DEFAULT_WORLD_SLUG, "name": "王都第一日"}

            console.database.get_world = get_world
            result = await console.world_delete()
            self.assertIn("默认世界", str(result))

        asyncio.run(run())
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
