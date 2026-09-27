from __future__ import annotations

import hashlib
import importlib
import io
import json
import re
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs(data_dir: Path) -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    message_components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")
    web = types.ModuleType("astrbot.api.web")

    class Logger:
        def debug(self, *args, **kwargs):
            return None

        info = debug
        warning = debug
        exception = debug

    class AstrBotConfig(dict):
        def save_config(self):
            self.save_calls = getattr(self, "save_calls", 0) + 1
            return None

    class AstrMessageEvent:
        pass

    class Plain:
        def __init__(self, text):
            self.text = text

    class At:
        def __init__(self, name, qq):
            self.name = name
            self.qq = qq

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(Plain(text))
            return self

        def at(self, name, qq):
            self.chain.append(At(name, qq))
            return self

    class Filter:
        class EventMessageType:
            GROUP_MESSAGE = "group"

        class CommandGroup:
            def __init__(self, name, alias=None, **kwargs):
                self.name = name
                self.alias = alias or set()
                self.priority = kwargs.get("priority", 0)
                self.commands = {}

            def command(self, name, alias=None, **kwargs):
                def decorator(function):
                    self.commands[name] = {
                        "alias": alias or set(),
                        "priority": kwargs.get("priority", 0),
                        "handler": function,
                    }
                    return function

                return decorator

        @staticmethod
        def command_group(name, alias=None, **kwargs):
            def decorator(_function):
                return Filter.CommandGroup(name, alias, **kwargs)

            return decorator

        @staticmethod
        def event_message_type(*args, **kwargs):
            def decorator(function):
                function.__astrbot_event_priority__ = kwargs.get(
                    "priority",
                    0,
                )
                return function

            return decorator

        @staticmethod
        def on_astrbot_loaded(*args, **kwargs):
            return lambda function: function

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return data_dir

    class PluginUploadFile:
        pass

    def response(value=None, *args, **kwargs):
        return value

    api.AstrBotConfig = AstrBotConfig
    api.logger = Logger()
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain
    event.filter = Filter()
    message_components.At = At
    message_components.Plain = Plain
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    web.PluginUploadFile = PluginUploadFile
    web.error_response = response
    web.file_response = response
    web.json_response = response
    web.stream_response = response
    web.request = SimpleNamespace(username=None)

    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.message_components"] = message_components
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.web"] = web


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple] = []
        self.sent_messages: list[tuple[str, object]] = []
        self.platform_names: dict[str, str] = {}

    def register_web_api(self, *args):
        self.routes.append(args)

    def get_all_providers(self):
        return [
            SimpleNamespace(
                meta=lambda: SimpleNamespace(
                    id="story-main",
                    name="主叙事模型",
                    model="narrative-pro",
                )
            ),
            SimpleNamespace(
                meta=lambda: {
                    "id": "vision-model",
                    "provider_name": "图片模型",
                    "model_name": "vision-pro",
                }
            ),
        ]

    def get_platform_inst(self, platform_id):
        name = self.platform_names.get(str(platform_id))
        if not name:
            return None
        return SimpleNamespace(
            meta=lambda: SimpleNamespace(id=str(platform_id), name=name)
        )

    async def send_message(self, origin, chain):
        self.sent_messages.append((origin, chain))
        return True


class PluginShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        _install_astrbot_stubs(Path(self.temp_dir.name))
        sys.path.insert(0, str(ROOT.parent))
        for name in list(sys.modules):
            if name == "astrbot_plugin_tavern" or name.startswith(
                "astrbot_plugin_tavern."
            ):
                sys.modules.pop(name)
        module = importlib.import_module("astrbot_plugin_tavern.main")
        self.module = module
        self.context = FakeContext()
        self.config = sys.modules["astrbot.api"].AstrBotConfig(
            {
                "security": {
                    "admin_ids": ["admin-1"],
                    "allowed_group_ids": ["group-shell"],
                    "require_group_whitelist": True,
                    "public_status": True,
                }
            }
        )
        self.plugin = module.TavernPlugin(self.context, self.config)

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()
        sys.modules["astrbot.api.web"].request.username = None
        if str(ROOT.parent) in sys.path:
            sys.path.remove(str(ROOT.parent))
        self.temp_dir.cleanup()

    async def _enable_timer_announcements(self, session_id: str) -> None:
        instance = await self.plugin.database.get_instance_config(session_id)
        rules = dict(instance.get("time_rules") or {})
        rules["announce_timeouts"] = True
        await self.plugin.database.execute_write(
            "UPDATE instance_configs SET time_rules_json=? WHERE session_id=?",
            (json.dumps(rules, ensure_ascii=False), session_id),
        )

    async def test_plugin_registers_native_web_routes(self) -> None:
        paths = {route[0] for route in self.context.routes}
        self.assertGreaterEqual(len(paths), 20)
        self.assertIn("/astrbot_plugin_tavern/overview", paths)
        self.assertIn("/astrbot_plugin_tavern/worlds/restore", paths)
        self.assertIn("/astrbot_plugin_tavern/sessions/turn-order", paths)
        self.assertIn("/astrbot_plugin_tavern/settings/save", paths)
        self.assertIn("/astrbot_plugin_tavern/providers", paths)
        self.assertIn("/astrbot_plugin_tavern/groups/remark", paths)
        self.assertIn("/astrbot_plugin_tavern/backup/import/<mode>", paths)
        self.assertIn("/astrbot_plugin_tavern/events", paths)

    async def test_web_card_import_uses_source_owner(self) -> None:
        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"
        request.method = "POST"
        request.json = AsyncMock(return_value={"session_id": "target", "source_participant_id": "source", "user_id": "spoofed"})
        db = self.plugin.database
        db.list_web_importable_cards = AsyncMock(return_value=[{
            'id': 'source', 'group_user_id': 'real-owner', 'display_name': '玩家',
        }])
        db.reserve_participant = AsyncMock()
        db.import_character_card = AsyncMock(return_value={'id': 'new-card'})
        db.write_audit = AsyncMock()
        result = await self.plugin.web_console.session_card_import()
        self.assertEqual(result['participant']['id'], 'new-card')
        db.reserve_participant.assert_awaited_once_with('target', 'real-owner', '玩家')
        db.import_character_card.assert_awaited_once_with('target', 'real-owner', 'source')
        self.assertIn('/astrbot_plugin_tavern/sessions/card-import', [r[0] for r in self.context.routes])

    async def test_web_card_import_requires_login(self) -> None:
        request = sys.modules["astrbot.api.web"].request
        request.username = None
        self.plugin.database.list_web_importable_cards = AsyncMock()
        await self.plugin.web_console.session_card_import()
        self.plugin.database.list_web_importable_cards.assert_not_awaited()

    async def test_extensions_route_registers_async_handler_and_returns_catalog(self) -> None:
        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"
        route = next(
            item
            for item in self.context.routes
            if item[0] == "/astrbot_plugin_tavern/extensions"
        )
        self.assertTrue(callable(route[1]))
        response = await route[1]()
        self.assertIn("dice_system", response["kinds"])
        self.assertIn("d20", response["kinds"]["dice_system"])
        self.assertFalse(response["partial"])

    async def test_extensions_endpoint_isolates_one_unserializable_item(self) -> None:
        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"

        class BadName:
            def __str__(self):
                raise RuntimeError("broken extension metadata")

        class PartialRegistry:
            @staticmethod
            def list():
                return {"admin_action": ["healthy", BadName()]}

        original = self.plugin.web_console._extension_registry
        self.plugin.web_console._extension_registry = PartialRegistry()
        try:
            response = await self.plugin.web_console.extensions()
        finally:
            self.plugin.web_console._extension_registry = original
        self.assertEqual(response["kinds"]["admin_action"], ["healthy"])
        self.assertTrue(response["partial"])
        self.assertEqual(len(response["errors"]), 1)

    async def test_timer_notice_mentions_target_and_shows_remaining_time(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-timer-notice",
            "qq:group-timer-notice",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self._enable_timer_announcements(session["id"])
        await self.plugin._send_timer_notice(
            {
                "kind": "reminder",
                "session_id": session["id"],
                "timer_type": "turn",
                "remaining_seconds": 90,
                "targets": [
                    {
                        "user_id": "target-user",
                        "display_name": "白鸦",
                    },
                    {
                        "user_id": "target-user",
                        "display_name": "重复目标",
                    },
                ],
            }
        )
        self.assertEqual(len(self.context.sent_messages), 1)
        origin, chain = self.context.sent_messages[0]
        self.assertEqual(origin, "qq:group-timer-notice")
        self.assertFalse(any(hasattr(component, "qq") for component in chain.chain))
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("行动回合剩余 1分30秒", text)
        self.assertIn("@白鸦", text)
        self.assertIn("请及时完成本回合操作", text)

    async def test_turn_dm_reminder_goes_private_without_group_mention(
        self,
    ) -> None:
        """2026-09-20：未选择私信催办发私聊，且不受群内公告开关限制。"""
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        session = await self.plugin.database.ensure_session(
            "qq",
            "dm-reminder-group",
            "qq:GroupMessage:dm-reminder-group",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        # 刻意不开 announce_timeouts：私信催办是独立开关。
        await self.plugin._send_timer_notice(
            {
                "kind": "dm_reminder",
                "session_id": session["id"],
                "timer_type": "turn",
                "stage": 1,
                "elapsed_seconds": 305,
                "remaining_seconds": 3000,
                "targets": [
                    {
                        "user_id": "1579093646",
                        "display_name": "五条悟",
                        "private_user_id": "",
                        "private_origin": "",
                    }
                ],
            }
        )
        self.assertEqual(len(self.context.sent_messages), 1)
        origin, chain = self.context.sent_messages[0]
        # 没有既存私聊来源时按 platform:FriendMessage:user 构造，绝不发群。
        self.assertEqual(origin, "qq:FriendMessage:1579093646")
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("轮到五条悟行动了", text)
        self.assertIn("5分", text)
        self.assertNotIn("@", text)

    async def test_turn_dm_reminder_prefers_recorded_private_origin(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        session = await self.plugin.database.ensure_session(
            "qq",
            "dm-reminder-known",
            "qq:GroupMessage:dm-reminder-known",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin._send_timer_notice(
            {
                "kind": "dm_reminder",
                "session_id": session["id"],
                "timer_type": "turn",
                "stage": 2,
                "elapsed_seconds": 1810,
                "remaining_seconds": 600,
                "targets": [
                    {
                        "user_id": "1579093646",
                        "display_name": "五条悟",
                        "private_origin": "qq:FriendMessage:recorded",
                    }
                ],
            }
        )
        origin, chain = self.context.sent_messages[0]
        self.assertEqual(origin, "qq:FriendMessage:recorded")
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("已经等了", text)
        self.assertIn("30分", text)

    async def test_turn_dm_reminder_without_any_private_target_is_skipped(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        session = await self.plugin.database.ensure_session(
            "qq",
            "dm-reminder-notarget",
            "qq:GroupMessage:dm-reminder-notarget",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin._send_timer_notice(
            {
                "kind": "dm_reminder",
                "session_id": session["id"],
                "timer_type": "turn",
                "stage": 1,
                "elapsed_seconds": 305,
                "remaining_seconds": 600,
                "targets": [],
            }
        )
        self.assertEqual(self.context.sent_messages, [])

    async def test_qq_official_timer_notice_uses_portable_text_mention(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        self.context.platform_names["alice"] = "qq_official"
        session = await self.plugin.database.ensure_session(
            "alice",
            "official-group",
            "alice:GroupMessage:official-group",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self._enable_timer_announcements(session["id"])
        await self.plugin._send_timer_notice(
            {
                "kind": "reminder",
                "session_id": session["id"],
                "timer_type": "turn",
                "remaining_seconds": 60,
                "targets": [
                    {
                        "user_id": "MEMBER_OPENID_1",
                        "display_name": "白鸦",
                    }
                ],
            }
        )
        self.assertEqual(len(self.context.sent_messages), 1)
        origin, chain = self.context.sent_messages[0]
        self.assertEqual(origin, "alice:GroupMessage:official-group")
        self.assertFalse(
            any(hasattr(component, "qq") for component in chain.chain)
        )
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("@白鸦", text)
        self.assertNotIn("qqbot-at-user", text)
        self.assertIn("行动回合剩余 1分", text)

    async def test_card_countdown_notice_is_private_without_group_mention(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        session = await self.plugin.database.ensure_session(
            "qq",
            "card-timer-group",
            "qq:GroupMessage:card-timer-group",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin._send_timer_notice(
            {
                "kind": "reminder",
                "session_id": session["id"],
                "timer_type": "card_completion",
                "remaining_seconds": 600,
                "targets": [
                    {
                        "user_id": "card-user",
                        "display_name": "建卡玩家",
                        "private_origin": "qq:FriendMessage:card-user",
                    }
                ],
            }
        )
        self.assertEqual(len(self.context.sent_messages), 1)
        origin, chain = self.context.sent_messages[0]
        self.assertEqual(origin, "qq:FriendMessage:card-user")
        self.assertFalse(
            any(hasattr(component, "qq") for component in chain.chain)
        )
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("角色卡创建剩余 10分", text)

    async def test_full_backup_zip_requires_safe_verified_members(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.web_console import (
            _verify_backup_archive,
        )

        payload = b'{"format":"astrbot-tavern-backup"}'
        checksum = hashlib.sha256(payload).hexdigest()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bundle.json", payload)
            archive.writestr(
                "checksum.sha256",
                f"{checksum}  bundle.json\n",
            )
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            self.assertEqual(
                _verify_backup_archive(archive),
                {"bundle.json": checksum},
            )

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../bundle.json", payload)
            archive.writestr(
                "checksum.sha256",
                f"{checksum}  ../bundle.json\n",
            )
        unsafe.seek(0)
        with zipfile.ZipFile(unsafe) as archive:
            with self.assertRaises(ValueError):
                _verify_backup_archive(archive)

    async def test_full_backup_zip_restores_independent_save_files(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import DEFAULT_WORLD_SLUG

        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"
        session = await self.plugin.database.ensure_session(
            "qq",
            "group-backup",
            "qq:group-backup",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.create_snapshot(
            session["id"],
            "导出前手动存档",
            "admin-1",
        )
        storage = await self.plugin.database.get_storage_info(session["id"])
        story_dir = Path(self.temp_dir.name) / storage["relative_path"]
        original_save = next((story_dir / "saves").glob("save_*.zip"))

        exported = await self.plugin.web_console.backup_export()
        self.assertTrue(Path(exported).exists())
        with zipfile.ZipFile(exported) as archive:
            self.assertIn("bundle.json", archive.namelist())
            self.assertIn("catalog.sqlite3", archive.namelist())
            self.assertTrue(
                any(
                    name.endswith(f"/saves/{original_save.name}")
                    for name in archive.namelist()
                )
            )

        original_save.unlink()
        self.assertFalse(original_save.exists())
        upload_type = sys.modules[
            "astrbot.api.web"
        ].PluginUploadFile

        class Upload(upload_type):
            filename = Path(exported).name

            async def save(self, destination):
                shutil.copyfile(exported, destination)

        async def uploaded_files():
            return {"file": Upload()}

        request.files = uploaded_files
        result = await self.plugin.web_console.backup_import("replace")
        self.assertIn("imported", result)
        self.assertTrue(original_save.exists())

    async def test_web_console_lists_configured_model_providers(self) -> None:
        request = sys.modules["astrbot.api.web"].request
        request.username = "dashboard-admin"
        response = await self.plugin.web_console.providers()
        self.assertEqual(
            [item["id"] for item in response["items"]],
            ["story-main", "vision-model"],
        )
        self.assertEqual(response["items"][0]["name"], "主叙事模型")
        self.assertEqual(response["items"][1]["model"], "vision-pro")

    async def test_closed_session_reopens_current_world_without_reset(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        config = TavernConfig.from_mapping(self.config)
        event = SimpleNamespace(unified_msg_origin="qq:group-shell")
        response = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="aelvion-ashen-crown",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("酒馆已开启", response)
        session = await self.plugin.database.get_session_by_group(
            "qq",
            "group-shell",
        )
        original_world = session["world_id"]

        await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(matched=True, action="close"),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="aelvion-ashen-crown",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        reopened = await self.plugin.database.get_session_by_group(
            "qq",
            "group-shell",
        )
        self.assertEqual(reopened["world_id"], original_world)

    async def test_ready_prompt_uses_continue_for_existing_story(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        participant = {
            "character_name": "那个男人",
            "display_name": "Merlin",
        }
        with (
            patch.object(
                self.plugin.database,
                "set_participant_ready",
                AsyncMock(return_value=participant),
            ),
            patch.object(
                self.plugin.database,
                "opening_preflight",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "blockers": [],
                        "resume_mode": True,
                    }
                ),
            ),
        ):
            response = await self.plugin._handle_command(
                event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                command=ParsedCommand(matched=True, action="ready"),
                config=TavernConfig.from_mapping(self.config),
                group_id="group-shell",
                platform_id="qq",
                sender_id="user-ready",
            )
        self.assertIn("/酒馆 继续", response)
        self.assertNotIn("/酒馆 开演", response)

    async def test_continue_does_not_recover_a_paused_session(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PAUSED,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PAUSED,
            "admin-1",
        )

        with patch.object(
            self.plugin.database,
            "resume_session_timers",
            AsyncMock(side_effect=AssertionError("不应恢复任何计时")),
        ):
            response = await self.plugin._handle_command(
                event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                command=ParsedCommand(matched=True, action="resume"),
                config=TavernConfig.from_mapping(self.config),
                group_id="group-shell",
                platform_id="qq",
                sender_id="admin-1",
            )

        current = await self.plugin.database.get_session(session["id"])
        self.assertEqual(current["state"], SESSION_PAUSED)
        self.assertIn("/酒馆 恢复", response)
        self.assertIn("没有恢复任何计时", response)

    async def test_recover_only_enters_resume_preparation_lobby(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PAUSED,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        with self.plugin.database._connect() as connection:
            connection.execute(
                "UPDATE sessions SET turn_no = 3 WHERE id = ?",
                (session["id"],),
            )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PAUSED,
            "admin-1",
        )

        with patch.object(
            self.plugin.database,
            "resume_session_timers",
            AsyncMock(side_effect=AssertionError("恢复准备阶段不得恢复计时")),
        ):
            response = await self.plugin._handle_command(
                event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                command=ParsedCommand(matched=True, action="recover"),
                config=TavernConfig.from_mapping(self.config),
                group_id="group-shell",
                platform_id="qq",
                sender_id="admin-1",
            )

        current = await self.plugin.database.get_session(session["id"])
        self.assertEqual(current["state"], SESSION_PREPARING)
        self.assertIn("恢复准备大厅", response)
        self.assertIn("/酒馆 准备", response)
        self.assertIn("/酒馆 继续", response)

    async def test_continue_rejects_a_new_story_preparation_lobby(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )

        with patch.object(
            self.plugin.database,
            "activate_story",
            AsyncMock(side_effect=AssertionError("新故事不能由继续开演")),
        ):
            response = await self.plugin._handle_command(
                event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                command=ParsedCommand(matched=True, action="resume"),
                config=TavernConfig.from_mapping(self.config),
                group_id="group-shell",
                platform_id="qq",
                sender_id="admin-1",
            )

        self.assertIn("/酒馆 开演", response)

    async def test_resume_replays_saved_story_choices_and_timer(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        with self.plugin.database._connect() as connection:
            connection.execute(
                "UPDATE sessions SET turn_no = 20 WHERE id = ?",
                (session["id"],),
            )
        preparing = await self.plugin.database.get_session(session["id"])
        running = {**preparing, "state": "running"}
        current = {
            "id": "participant-current",
            "character_name": "那个男人",
            "display_name": "Merlin",
        }
        old_text = "暂停前保存的原始长选项" * 8
        choices = [
            {
                "key": key,
                "text": old_text if key == "A" else f"原始选项 {key}",
                "risk": "safe",
            }
            for key in ("A", "B", "C", "D")
        ]
        result = {
            "started": True,
            "session": running,
            "current_participant": current,
            "choice_set": {
                "choices": choices,
                "reroll_count": 0,
            },
            "vote": None,
        }
        with (
            patch.object(
                self.plugin.database,
                "activate_story",
                AsyncMock(return_value=result),
            ),
            patch.object(
                self.plugin.database,
                "resume_session_timers",
                AsyncMock(return_value=1),
            ),
            patch.object(
                self.plugin.database,
                "active_vote",
                AsyncMock(return_value=None),
            ),
            patch.object(
                self.plugin.database,
                "recent_events",
                AsyncMock(
                    return_value=[
                        {
                            "role": "narrator",
                            "content": "这是暂停前最后一段正式剧情。",
                        }
                    ]
                ),
            ),
            patch.object(
                self.plugin.database,
                "list_timers",
                AsyncMock(
                    return_value=[
                        {
                            "timer_type": "turn",
                            "status": "active",
                            "remaining_seconds": 87,
                        }
                    ]
                ),
            ),
        ):
            response = await self.plugin._handle_command(
                event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                command=ParsedCommand(matched=True, action="resume"),
                config=TavernConfig.from_mapping(self.config),
                group_id="group-shell",
                platform_id="qq",
                sender_id="admin-1",
            )
        self.assertIn("这是暂停前最后一段正式剧情", response)
        self.assertIn(old_text, response)
        self.assertIn("剩余 1 分 27 秒", response)

    def test_group_id_prefers_unified_event_accessor(self) -> None:
        event = SimpleNamespace(
            get_group_id=lambda: "canonical-group",
            message_obj=SimpleNamespace(group_id="adapter-field"),
        )
        self.assertEqual(
            self.plugin._group_id(event),
            "canonical-group",
        )

    async def test_card_import_player_dispatch(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand
        session = await self.plugin.database.ensure_session(
            "qq", "group-shell", "qq:group-shell", "aelvion-ashen-crown", "admin-1",
        )
        self.plugin.database.list_importable_cards = AsyncMock(return_value=[
            {'id': 'old-participant', 'instance_name': '旧本', 'character_name': '本人角色'},
        ])
        self.plugin.database.import_character_card = AsyncMock(return_value={
            'character_name': '本人角色', 'auto_approved': True,
        })
        args = dict(event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                    config=TavernConfig.from_mapping(self.config), group_id="group-shell",
                    platform_id="qq", sender_id="ordinary-user")
        listing = await self.plugin._handle_command(
            command=ParsedCommand(matched=True, action="card_import"), **args,
        )
        self.assertIn('/酒馆 导入角色卡 old-participant', listing)
        response = await self.plugin._handle_command(
            command=ParsedCommand(matched=True, action="card_import", argument="old-participant"), **args,
        )
        self.assertIn('角色卡已导入', response)
        self.plugin.database.import_character_card.assert_awaited_once_with(session['id'], 'ordinary-user', 'old-participant')

    async def test_checkpoint_admin_dispatch(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import SESSION_RUNNING
        from astrbot_plugin_tavern.tavern.security import ParsedCommand
        session = await self.plugin.database.ensure_session(
            "qq", "group-shell", "qq:group-shell", "aelvion-ashen-crown", "admin-1",
        )
        await self.plugin.database.transition_session(session["id"], SESSION_RUNNING, "admin-1")
        self.plugin.database.adjust_checkpoint = AsyncMock(return_value="已调整")
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(matched=True, action="checkpoint_complete", argument="章节"),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell", platform_id="qq", sender_id="admin-1",
        )
        self.assertEqual(response, "已调整")
        self.plugin.database.adjust_checkpoint.assert_awaited_once_with(session["id"], True, "章节", "admin-1")

    async def test_checkpoint_commands_require_admin(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        for action in ("checkpoint_complete", "checkpoint_revert"):
            response = await self.plugin._handle_command(
                event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
                command=ParsedCommand(matched=True, action=action, argument="章节"),
                config=TavernConfig.from_mapping(self.config),
                group_id="group-shell", platform_id="qq", sender_id="intruder",
            )
            self.assertIsNone(response)

    async def test_unauthorized_command_is_audited_without_execution(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        config = TavernConfig.from_mapping(self.config)
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(matched=True, action="start"),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="intruder",
        )
        self.assertIsNone(response)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq",
                "group-shell",
            )
        )
        audit = await self.plugin.database.list_audit("", 20, 0)
        denied = [
            item
            for item in audit
            if item["action"] == "security.command_denied"
        ]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["actor_id"], "intruder")
        self.assertEqual(
            denied[0]["detail"]["reason"],
            "sender_not_authorized",
        )

    async def test_authorized_start_auto_binds_then_requires_selection(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        group_id = "group-auto-bound"
        response = await self.plugin._handle_command(
            event=SimpleNamespace(
                unified_msg_origin=f"qq-instance:{group_id}"
            ),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id=group_id,
            platform_id="qq-instance",
            sender_id="admin-1",
        )

        self.assertIn("本群还没有酒馆副本", response)
        self.assertNotIn("酒馆已开启", response)
        self.assertIn("平台实例 ID：qq-instance", response)
        self.assertIn(f"群 ID：{group_id}", response)
        self.assertIn(
            group_id,
            self.config["security"]["allowed_group_ids"],
        )
        self.assertEqual(self.config.save_calls, 1)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq-instance",
                group_id,
            )
        )
        audit = await self.plugin.database.list_audit("", 20, 0)
        bindings = [
            item
            for item in audit
            if item["action"] == "security.group_auto_allowed"
        ]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["actor_id"], "admin-1")
        self.assertEqual(
            bindings[0]["detail"]["source"],
            "authorized_group_command",
        )

        started = await self.plugin._handle_command(
            event=SimpleNamespace(
                unified_msg_origin=f"qq-instance:{group_id}"
            ),
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="aelvion-ashen-crown",
            ),
            config=TavernConfig.from_mapping(self.config),
            group_id=group_id,
            platform_id="qq-instance",
            sender_id="admin-1",
        )
        self.assertIn("酒馆已开启", started)
        self.assertIn("副本标识：aelvion-ashen-crown", started)

    def test_instance_list_includes_intro_and_paginates_five_at_a_time(
        self,
    ) -> None:
        worlds = [
            {
                "slug": f"world-{index}",
                "name": f"测试世界 {index}",
                "description": f"这是第 {index} 个世界的简介。",
            }
            for index in range(1, 8)
        ]

        first_page = self.module.format_instance_list([], worlds, page=1)
        self.assertIn("第 1/2 页", first_page)
        self.assertEqual(first_page.count("简介："), 5)
        self.assertIn("这是第 1 个世界的简介", first_page)
        self.assertIn("这是第 5 个世界的简介", first_page)
        self.assertNotIn("这是第 6 个世界的简介", first_page)
        self.assertIn("/酒馆 开启 第2页", first_page)

        second_page = self.module.format_instance_list([], worlds, page=2)
        self.assertIn("第 2/2 页", second_page)
        self.assertEqual(second_page.count("简介："), 2)
        self.assertNotIn("这是第 5 个世界的简介", second_page)
        self.assertIn("这是第 6 个世界的简介", second_page)
        self.assertIn("这是第 7 个世界的简介", second_page)
        self.assertIn("/酒馆 开启 第1页", second_page)
        self.assertEqual(
            self.module.parse_instance_list_page("第 2 页"),
            2,
        )
        self.assertIsNone(
            self.module.parse_instance_list_page("aelvion-ashen-crown")
        )

    async def test_start_page_argument_only_reads_requested_page(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        base_world = await self.plugin.database.get_world(
            "aelvion-ashen-crown"
        )
        for internal_key in (
            "id", "revision", "display_no", "sort_order",
            "created_at", "updated_at", "archived",
        ):
            base_world.pop(internal_key, None)
        for index in range(1, 7):
            await self.plugin.database.save_world(
                {
                    **base_world,
                    "slug": f"page-world-{index}",
                    "name": f"分页世界 {index}",
                    "description": f"分页简介 {index}",
                    "system_prompt": "保持因果一致。",
                    "opening_scene": "故事尚未开始。",
                },
                "admin-1",
            )

        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="第2页",
            ),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )

        self.assertIn("第 2/2 页", response)
        self.assertEqual(response.count("简介："), 2)
        self.assertNotIn("酒馆已开启", response)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq",
                "group-shell",
            )
        )

    async def test_start_without_argument_only_lists_existing_instances(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import SESSION_RUNNING
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        first = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            "aelvion-ashen-crown",
            "admin-1",
            "main-copy",
            "主线副本",
        )
        first = await self.plugin.database.transition_session(
            first["id"],
            SESSION_RUNNING,
            "admin-1",
        )
        second = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            "aelvion-ashen-crown",
            "admin-1",
            "second-copy",
            "二周目副本",
        )
        before = {
            item["id"]: (
                item["state"],
                item["revision"],
                item["selected"],
            )
            for item in (first, second)
        }

        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )

        self.assertIn("请选择酒馆副本", response)
        self.assertIn("主线副本", response)
        self.assertIn("（main-copy）", response)
        self.assertIn("二周目副本", response)
        self.assertIn("（second-copy）", response)
        self.assertIn("简介：", response)
        self.assertIn("灰月异象", response)
        self.assertNotIn("酒馆已开启", response)
        after_items = await self.plugin.database.list_group_sessions(
            "qq",
            "group-shell",
        )
        after = {
            item["id"]: (
                item["state"],
                item["revision"],
                item["selected"],
            )
            for item in after_items
        }
        self.assertEqual(after, before)

    async def test_unlisted_group_cannot_be_bound_by_non_admin(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        group_id = "group-intruder"
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin=f"qq:{group_id}"),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id=group_id,
            platform_id="qq",
            sender_id="intruder",
        )

        self.assertIsNone(response)
        self.assertNotIn(
            group_id,
            self.config["security"]["allowed_group_ids"],
        )
        self.assertIsNone(
            await self.plugin.database.get_session_by_group("qq", group_id)
        )

    async def test_missing_admin_configuration_returns_setup_error(self) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        self.config["security"]["admin_ids"] = []
        response = await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-new"),
            command=ParsedCommand(matched=True, action="start"),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-new",
            platform_id="qq",
            sender_id="first-user",
        )

        self.assertIn("酒馆尚未初始化", response)
        self.assertIn("管理员 ID", response)
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq",
                "group-new",
            )
        )

    def test_management_commands_use_native_command_group(self) -> None:
        group = self.module.TavernPlugin.tavern
        self.assertEqual(group.name, "酒馆")
        self.assertGreater(
            group.commands["开启"]["priority"],
            self.module.TavernPlugin.on_group_message.__astrbot_event_priority__,
        )
        self.assertEqual(
            set(group.commands),
            {
                "导入角色卡",
                "完成",
                "回退",
                "开启",
                "开演",
                "暂停",
                "恢复",
                "继续",
                "关闭",
                "完结",
                "强制终止",
                "维护",
                "状态",
                "主持",
                "安全暂停",
                "存档",
                "删档",
                "读档",
                "回滚",
                "世界列表",
                "副本列表",
                "加入",
                "建卡",
                "填写",
                "上一步",
                "修改",
                "当前步骤",
                "预览",
                "重填数值",
                "建卡提醒",
                "确认建卡",
                "取消建卡",
                "角色",
                "准备",
                "强制全员准备",
                "阵容",
                "审核",
                "选择",
                "灵感",
                "灵感重投",
                "重整选项",
                "投票",
                "全队",
                "暂离",
                "返回队列",
                "申请返场",
                "退出",
                "顺序",
                "跳过",
                "强制下一位",
                "倒计时",
                "用量",
                "限额",
                "删除副本",
                "帮助",
            },
        )
        self.assertEqual(group.commands["开启"]["alias"], {"启动"})
        self.assertEqual(group.commands["恢复"]["alias"], set())
        self.assertEqual(group.commands["继续"]["alias"], set())
        self.assertEqual(group.commands["顺序"]["alias"], {"轮次"})
        self.assertEqual(group.commands["副本列表"]["alias"], {"副本"})
        self.assertEqual(group.commands["建卡提醒"]["alias"], set())
        self.assertEqual(group.commands["强制下一位"]["alias"], set())

    async def test_chat_review_lists_views_and_approves_pending_card(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-shell",
            "qq:group-shell",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        session = await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        for index in range(2):
            user_id = f"review-user-{index + 1}"
            reserved = await self.plugin.database.reserve_participant(
                session["id"],
                user_id,
                f"待审玩家{index + 1}",
            )
            origin = f"qq:friend-{user_id}"
            bound = await self.plugin.database.bind_card_code(
                reserved["binding_code"],
                user_id,
                origin,
            )
            used_select_values: set[str] = set()
            for field in bound["template"]["fields"]:
                if field["key"] == "name":
                    value = f"待审角色{index + 1}"
                elif field["key"] == "code":
                    value = f"R{index + 1}"
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
                elif field.get("private"):
                    value = "审核用私密内容"
                else:
                    value = "审核用公开内容"
                await self.plugin.database.fill_card_draft(origin, value)
            await self.plugin.database.confirm_card_draft(origin)

        class NativeReviewEvent:
            message_str = "酒馆 审核"
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "admin-1"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        native_event = NativeReviewEvent()
        native_listed = [
            item async for item in self.plugin.tavern_review(native_event)
        ]
        self.assertTrue(native_event.stopped)
        self.assertIn("待审核角色卡", native_listed[0])

        event = SimpleNamespace(unified_msg_origin="qq:group-shell")
        config = TavernConfig.from_mapping(self.config)
        listed = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(matched=True, action="review"),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("待审核角色卡", listed)
        self.assertIn("待审角色1", listed)
        self.assertIn("待审角色2", listed)
        review_reference = re.search(
            r"审核号：(R-[A-Z0-9]{8})",
            listed,
        ).group(1)

        detail = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="review",
                argument="查看 1",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("角色卡审核详情", detail)
        self.assertIn("审核用公开内容", detail)
        self.assertIn("已填写，群聊中隐藏", detail)
        self.assertNotIn("审核用私密内容", detail)
        self.assertIn("最终属性", detail)

        approved = await self.plugin._handle_command(
            event=event,
            command=ParsedCommand(
                matched=True,
                action="review",
                argument=f"{review_reference} 通过 群聊审核通过",
            ),
            config=config,
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )
        self.assertIn("已通过", approved)
        self.assertIn("剩余待审核：1 人", approved)
        roster = await self.plugin.database.list_roster(session["id"])
        self.assertEqual(
            sum(item["card_status"] == "approved" for item in roster),
            1,
        )

    @unittest.skip("旧四属性手填默认模板已被职业预设数值取代；通用数值向导由独立世界包夹具覆盖")
    async def test_private_stat_prompt_and_native_reset_keep_profile(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-stat-prompt",
            "qq:group-stat-prompt",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"],
            "prompt-user",
            "提示玩家",
        )

        class Event:
            unified_msg_origin = "qq:friend-prompt-user"
            message_obj = SimpleNamespace(group_id="")

            def __init__(self, message: str) -> None:
                self.message_str = message
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "prompt-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event(f"酒馆 建卡 {reserved['binding_code']}")
        _ = [item async for item in self.plugin.tavern_card(event)]
        draft = await self.plugin.database.card_draft_for_private(
            event.unified_msg_origin
        )
        stat_fields = [
            item for item in draft["template"]["fields"]
            if item.get("stat_key")
        ]
        first_stat_step = draft["template"]["fields"].index(stat_fields[0])
        response = ""
        for field in draft["template"]["fields"][:first_stat_step]:
            if field["key"] == "name":
                value = "提示角色"
            elif field["key"] == "code":
                value = "TIP"
            else:
                value = "不会被数值重填删除"
            event.message_str = f"酒馆 填写 {value}"
            response = [
                item async for item in self.plugin.tavern_card_fill(event)
            ][0]
        self.assertIn("接下来开始填写角色数值", response)
        self.assertIn("总预算：10 点", response)

        for value in ("5", "4"):
            event.message_str = f"酒馆 填写 {value}"
            response = [
                item async for item in self.plugin.tavern_card_fill(event)
            ][0]
        self.assertIn("当前可填：0—1", response)

        event.message_str = "酒馆 重填数值"
        reset = [
            item
            async for item in self.plugin.tavern_card_stats_reset(event)
        ][0]
        self.assertIn("角色数值已重置", reset)
        self.assertIn("当前可填：0—5", reset)
        stored = await self.plugin.database.card_draft_for_private(
            event.unified_msg_origin
        )
        self.assertEqual(
            stored["fields"]["background"],
            "不会被数值重填删除",
        )
        self.assertFalse(
            any(key.startswith("stat_") for key in stored["fields"])
        )

    async def test_private_card_countdown_notice_can_toggle_at_any_time(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-card-notice-toggle",
            "qq:GroupMessage:group-card-notice-toggle",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"],
            "toggle-user",
            "提示开关玩家",
        )
        private_origin = "qq:FriendMessage:toggle-user"
        await self.plugin.database.bind_card_code(
            reserved["binding_code"],
            "toggle-user",
            private_origin,
        )

        class Event:
            unified_msg_origin = private_origin
            message_obj = SimpleNamespace(group_id="")

            def __init__(self, message: str) -> None:
                self.message_str = message
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "toggle-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event("/酒馆 建卡提醒 关")
        disabled = [
            item
            async for item in self.plugin.tavern_card_timer_notice(event)
        ][0]
        self.assertTrue(event.stopped)
        self.assertIn("建卡倒计时提示已关闭", disabled)
        self.assertIn("建卡计时仍会继续", disabled)

        event.message_str = "/酒馆 建卡提醒"
        status = [
            item
            async for item in self.plugin.tavern_card_timer_notice(event)
        ][0]
        self.assertIn("建卡倒计时提示已关闭", status)

        event.message_str = "／酒馆 建卡提醒 开"
        enabled = [
            item
            async for item in self.plugin.tavern_card_timer_notice(event)
        ][0]
        self.assertIn("建卡倒计时提示已开启", enabled)
        self.assertIn("每 2 分钟私聊提示", enabled)

    async def test_native_start_uses_stripped_text_and_real_event_ids(
        self,
    ) -> None:
        class Event:
            message_str = "酒馆 开启"
            unified_msg_origin = "qq-live:group-live"
            message_obj = SimpleNamespace(group_id="adapter-group")

            def __init__(self) -> None:
                self.stopped = False

            def get_group_id(self):
                return "group-live"

            def get_platform_id(self):
                return "qq-live"

            def get_sender_id(self):
                return "admin-1"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        selection_responses = [
            item async for item in self.plugin.tavern_start(event)
        ]

        self.assertTrue(event.stopped)
        self.assertEqual(len(selection_responses), 1)
        self.assertIn(
            "平台实例 ID：qq-live",
            selection_responses[0],
        )
        self.assertIn("群 ID：group-live", selection_responses[0])
        self.assertNotIn("酒馆已开启", selection_responses[0])
        self.assertIn(
            "group-live",
            self.config["security"]["allowed_group_ids"],
        )
        self.assertIsNone(
            await self.plugin.database.get_session_by_group(
                "qq-live",
                "group-live",
            )
        )

        event.message_str = "酒馆 开启 aelvion-ashen-crown"
        started_responses = [
            item async for item in self.plugin.tavern_start(event)
        ]
        self.assertEqual(len(started_responses), 1)
        self.assertIn("酒馆已开启", started_responses[0])

    async def test_private_native_card_commands_accept_halfwidth_slash(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-private-command",
            "qq:group-private-command",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"],
            "private-user",
            "私聊玩家",
        )

        class Event:
            unified_msg_origin = "qq:friend-private-user"
            message_obj = SimpleNamespace(group_id="")

            def __init__(self, message: str) -> None:
                self.message_str = message
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "private-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event(f"酒馆 建卡 {reserved['binding_code']}")
        bound = [item async for item in self.plugin.tavern_card(event)]

        self.assertTrue(event.stopped)
        self.assertEqual(len(bound), 1)
        self.assertIn("私聊身份绑定成功", bound[0])
        self.assertIsNotNone(
            await self.plugin.database.card_draft_for_private(
                event.unified_msg_origin
            )
        )

        event.message_str = "酒馆 预览"
        halfwidth_preview = [
            item async for item in self.plugin.tavern_card_preview(event)
        ]
        self.assertIn("角色卡预览", halfwidth_preview[0])

        event.message_str = "／酒馆 预览"
        fullwidth_preview = [
            item async for item in self.plugin.on_private_message(event)
        ]
        self.assertIn("角色卡预览", fullwidth_preview[0])

    async def test_private_card_ignores_empty_framework_event(self) -> None:
        """已有草稿时，空的非命令事件不能被当成一次字段填写。"""
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-private-empty",
            "qq:group-private-empty",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"], SESSION_PREPARING, "admin-1"
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"], "empty-user", "空事件玩家"
        )
        private_origin = "qq:friend-empty-user"
        await self.plugin.database.bind_card_code(
            reserved["binding_code"], "empty-user", private_origin
        )
        before = await self.plugin.database.card_draft_for_private(
            private_origin
        )

        class Event:
            unified_msg_origin = private_origin
            message_obj = SimpleNamespace(group_id="")

            def __init__(self, message: str) -> None:
                self.message_str = message
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "empty-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        empty_event = Event("")
        responses = [
            item async for item in self.plugin.on_private_message(empty_event)
        ]
        self.assertEqual(responses, [])
        self.assertFalse(empty_event.stopped)
        after = await self.plugin.database.card_draft_for_private(
            private_origin
        )
        self.assertEqual(after["current_step"], before["current_step"])
        self.assertEqual(after["fields"], before["fields"])

        # 玩家确实发送了空的“填写”命令时，返回用法而非字段不能为空。
        fill_event = Event("酒馆 填写")
        responses = [
            item async for item in self.plugin.tavern_card_fill(fill_event)
        ]
        self.assertTrue(fill_event.stopped)
        self.assertIn("请在“填写”后输入内容", responses[0])
        self.assertNotIn("不能为空", responses[0])

    async def test_native_command_preserves_full_trailing_argument(
        self,
    ) -> None:
        class Event:
            message_str = "酒馆 开启 aelvion-ashen-crown"
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            def get_group_id(self):
                return "group-shell"

            def get_platform_id(self):
                return "qq"

            def get_sender_id(self):
                return "admin-1"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        _ = [item async for item in self.plugin.tavern_start(event)]
        event.message_str = "酒馆 存档 旧塔 之前"
        responses = [item async for item in self.plugin.tavern_save(event)]

        self.assertTrue(event.stopped)
        self.assertEqual(len(responses), 1)
        self.assertIn("只有正式运行中的故事可以创建新剧情存档", responses[0])

    async def test_group_listener_intercepts_stripped_unknown_command(
        self,
    ) -> None:
        class Event:
            message_str = "酒馆 不存在"
            is_at_or_wake_command = True
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            def get_group_id(self):
                return "group-shell"

            def get_platform_id(self):
                return "qq"

            def get_sender_id(self):
                return "admin-1"

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        responses = [
            item async for item in self.plugin.on_group_message(event)
        ]

        self.assertTrue(event.stopped)
        self.assertEqual(len(responses), 1)
        self.assertIn("未知命令：不存在", responses[0])

    async def test_group_listener_ignores_unprefixed_chat_and_strips_jg(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.config import TavernConfig
        from astrbot_plugin_tavern.tavern.security import ParsedCommand

        await self.plugin._handle_command(
            event=SimpleNamespace(unified_msg_origin="qq:group-shell"),
            command=ParsedCommand(
                matched=True,
                action="start",
                argument="aelvion-ashen-crown",
            ),
            config=TavernConfig.from_mapping(self.config),
            group_id="group-shell",
            platform_id="qq",
            sender_id="admin-1",
        )

        class Event:
            is_at_or_wake_command = False
            unified_msg_origin = "qq:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

    async def test_jg_choice_uses_active_sends_without_intermediate_yield(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.constants import SESSION_RUNNING

        self.plugin.database.get_session_by_group = AsyncMock(
            return_value={"id": "session_hotfix", "state": SESSION_RUNNING}
        )
        self.plugin.database.active_vote = AsyncMock(return_value=None)
        self.plugin.engine.process_choice = AsyncMock(
            return_value=SimpleNamespace(
                story_text="【故事推进】\n修复后的故事正文。",
                turn_text="【回合秩序】第1轮结束 · 第2轮：下一位",
                # EngineReply 的常规字段：投递层会先看 assessment_text
                # （裁判结论）再发正文。桩件漏了它 → on_group_message 里
                # 直接 AttributeError → 玩家看到的是"叙事引擎内部错误"。
                assessment_text="",
                text="",
            )
        )

        class Event:
            message_str = "jg C"
            is_at_or_wake_command = False
            unified_msg_origin = "qq:GroupMessage:group-shell"
            message_obj = SimpleNamespace(group_id="group-shell")

            def __init__(self) -> None:
                self.stopped = False

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "choice-user"

            @staticmethod
            def get_sender_name():
                return "选择玩家"

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        yielded = [
            item async for item in self.plugin.on_group_message(event)
        ]

        self.assertTrue(event.stopped)
        self.assertEqual(yielded, [])
        self.plugin.engine.process_choice.assert_awaited_once()
        # B1 统一为纯文本链路：叙事与选项各发送一次，不再额外尝试富卡片。
        self.assertEqual(len(self.context.sent_messages), 2)
        texts = [
            "".join(
                str(component.text)
                for component in chain.chain
                if hasattr(component, "text")
            )
            for _, chain in self.context.sent_messages
        ]
        self.assertIn("修复后的故事正文", texts[0])
        self.assertIn("第2轮", texts[1])
        self.assertTrue(
            all(
                origin == event.unified_msg_origin
                for origin, _ in self.context.sent_messages
            )
        )

    async def test_confirmed_card_notifies_its_bound_group(self) -> None:
        from astrbot_plugin_tavern.tavern.constants import (
            DEFAULT_WORLD_SLUG,
            SESSION_PREPARING,
        )

        session = await self.plugin.database.ensure_session(
            "qq",
            "group-card-created",
            "qq:GroupMessage:group-card-created",
            DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.plugin.database.transition_session(
            session["id"],
            SESSION_PREPARING,
            "admin-1",
        )
        reserved = await self.plugin.database.reserve_participant(
            session["id"],
            "card-created-user",
            "建卡玩家",
        )
        private_origin = "qq:FriendMessage:card-created-user"
        await self.plugin.database.bind_card_code(
            reserved["binding_code"],
            "card-created-user",
            private_origin,
        )
        draft = await self.plugin.database.card_draft_for_private(
            private_origin
        )
        used_select_values: set[str] = set()
        for field in draft["template"]["fields"]:
            if field["key"] == "name":
                value = "同步角色"
            elif field["key"] == "code":
                value = "SYNC"
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
                value = "角色资料"
            await self.plugin.database.fill_card_draft(
                private_origin,
                value,
            )

        class Event:
            message_str = "酒馆 确认建卡"
            unified_msg_origin = private_origin
            message_obj = SimpleNamespace(group_id="")

            def __init__(self) -> None:
                self.stopped = False

            @staticmethod
            def get_group_id():
                return ""

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_sender_id():
                return "card-created-user"

            def get_message_str(self):
                return self.message_str

            def stop_event(self):
                self.stopped = True

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        responses = [
            item async for item in self.plugin.tavern_card_confirm(event)
        ]

        self.assertTrue(event.stopped)
        self.assertIn("角色卡已确认", responses[0])
        self.assertEqual(len(self.context.sent_messages), 1)
        origin, chain = self.context.sent_messages[0]
        self.assertEqual(origin, "qq:GroupMessage:group-card-created")
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertEqual(
            text,
            "【酒馆】角色卡同步角色已建立，等待审核。",
        )

    async def test_group_token_quota_has_group_web_routes(self) -> None:
        paths = {route[0] for route in self.context.routes}
        self.assertIn(
            "/astrbot_plugin_tavern/groups/token-usage",
            paths,
        )
        self.assertIn(
            "/astrbot_plugin_tavern/groups/token-quota",
            paths,
        )

    async def test_message_result_upgrades_at_marker_to_segment(self) -> None:
        """行动回合头部艾特标记在支持艾特的平台升级为真实 At 段。"""

        captured: dict[str, object] = {}

        class Event:
            unified_msg_origin = "qq:GroupMessage:group-shell"

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def plain_result(value):
                return value

            @staticmethod
            def chain_result(chain):
                captured["chain"] = chain
                return chain

        event = Event()
        result = await self.plugin._message_result(
            event, "🎯 <<AT:123456789:白鸦>> 的行动回合"
        )
        self.assertIs(result, captured["chain"])
        chain = result
        self.assertEqual(len(chain), 3)
        self.assertEqual(str(chain[0].text), "🎯 ")
        self.assertTrue(hasattr(chain[1], "qq"))
        self.assertEqual(str(chain[1].qq), "123456789")
        self.assertEqual(str(chain[1].name), "白鸦")
        self.assertEqual(str(chain[2].text), " 的行动回合")

    async def test_message_result_falls_back_to_at_name_without_chain_result(
        self,
    ) -> None:
        """事件不提供 chain_result 时回退为 @名字 纯文本，不泄漏标记。"""

        class Event:
            unified_msg_origin = "qq:GroupMessage:group-shell"

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        result = await self.plugin._message_result(
            event, "🎯 <<AT:123456789:白鸦>> 的行动回合"
        )
        self.assertIn("@白鸦", str(result))
        self.assertNotIn("<<AT:", str(result))

    async def test_message_result_falls_back_to_at_name_on_non_mention_platform(
        self,
    ) -> None:
        """webchat 等无艾特能力的平台回退为 @名字 纯文本。"""

        class Event:
            unified_msg_origin = "webchat:GroupMessage:group-shell"

            @staticmethod
            def get_platform_id():
                return "webchat"

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def plain_result(value):
                return value

            @staticmethod
            def chain_result(chain):
                raise AssertionError("不支持艾特的平台不应生成 At 段")

        event = Event()
        result = await self.plugin._message_result(
            event, "🎯 <<AT:123456789:白鸦>> 的行动回合"
        )
        self.assertIn("@白鸦", str(result))
        self.assertNotIn("<<AT:", str(result))

    async def test_message_result_plain_text_unchanged_without_marker(self) -> None:
        class Event:
            unified_msg_origin = "qq:GroupMessage:group-shell"

            @staticmethod
            def get_platform_id():
                return "qq"

            @staticmethod
            def get_group_id():
                return "group-shell"

            @staticmethod
            def plain_result(value):
                return value

        event = Event()
        result = await self.plugin._message_result(
            event, "🎯 【白鸦的行动回合】"
        )
        self.assertEqual(str(result), "🎯 【白鸦的行动回合】")

    async def test_deliver_text_upgrades_at_marker_on_mention_platform(self) -> None:
        """jg/自由演绎的回合秩序行在支持艾特的平台升级为真实 At 段。"""

        from astrbot_plugin_tavern.tavern.platform_delivery import (
            send_text as deliver_text,
        )

        self.context.sent_messages.clear()
        result = await deliver_text(
            self.context,
            "qq:GroupMessage:group-shell",
            "下一位：<<AT:123456789:白鸦>>",
            proactive=False,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.context.sent_messages), 1)
        _, chain = self.context.sent_messages[0]
        self.assertEqual(len(chain.chain), 2)
        self.assertTrue(hasattr(chain.chain[1], "qq"))
        self.assertEqual(str(chain.chain[1].qq), "123456789")

    async def test_deliver_text_falls_back_to_at_name_without_mention_support(
        self,
    ) -> None:
        from astrbot_plugin_tavern.tavern.platform_delivery import (
            send_text as deliver_text,
        )

        self.context.sent_messages.clear()
        result = await deliver_text(
            self.context,
            "webchat:GroupMessage:group-shell",
            "下一位：<<AT:123456789:白鸦>>",
            proactive=False,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.context.sent_messages), 1)
        _, chain = self.context.sent_messages[0]
        self.assertFalse(any(hasattr(component, "qq") for component in chain.chain))
        text = "".join(
            str(component.text)
            for component in chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("@白鸦", text)
        self.assertNotIn("<<AT:", text)
