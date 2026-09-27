import json
import tempfile
import unittest
from pathlib import Path
import test_freeform as fixtures
from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_PREPARING
from tavern.database import TavernDatabase


class CardImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session('qq', 'g', 'qq:g', DEFAULT_WORLD_SLUG, 'admin', 'source', '旧本')
        await self.database.transition_session(self.session['id'], SESSION_PREPARING, 'admin')
        self.source = await fixtures.FreeformTurnTests._make_character(self, 'user', '旧角色', '旧代号')
        self.target = await self.database.ensure_session('qq', 'g', 'qq:g', DEFAULT_WORLD_SLUG, 'admin', 'target', '新本')
        await self.database.transition_session(self.target['id'], SESSION_PREPARING, 'admin')
        self.reserved = await self.database.reserve_participant(self.target['id'], 'user', '玩家')

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def do_import(self):
        return await self.database.import_character_card(self.target['id'], 'user', self.source['id'])

    async def test_list_and_import_independent_card(self):
        cards = await self.database.list_importable_cards(self.target['id'], 'user')
        self.assertEqual([c['id'] for c in cards], [self.source['id']])
        with self.database._connect() as c:
            c.execute("UPDATE character_runtime_states SET state_json=? WHERE participant_id=?", (json.dumps({'current_location': '旧地点', 'debuff': '诅咒', 'inventory': ['旧物']}), self.source['id']))
        result = await self.do_import()
        self.assertEqual(result['character_name'], '旧角色')
        self.assertFalse(result['ready'])
        self.assertNotEqual(result['character_card_id'], self.source['character_card_id'])
        roster = await self.database.list_roster(self.target['id'])
        runtime = roster[0]['runtime_state']
        self.assertNotEqual(runtime.get('current_location'), '旧地点')
        self.assertNotIn('debuff', runtime)
        self.assertNotIn('inventory', runtime)
        self.assertEqual(roster[0]['card_profile']['name'], '旧角色')
        with self.database._connect() as c:
            self.assertEqual(c.execute('SELECT current_version FROM character_cards WHERE id=?', (self.source['character_card_id'],)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM timer_instances WHERE participant_id=? AND timer_type='card_code' AND status='active'", (result['id'],)).fetchone()[0], 0)

    async def test_no_overwrite_on_retry(self):
        await self.do_import()
        with self.assertRaises(ValueError):
            await self.do_import()

    async def test_ownership_checked_on_import_and_list(self):
        self.assertEqual(await self.database.list_importable_cards(self.target['id'], 'other'), [])
        await self.database.reserve_participant(self.target['id'], 'other', '其他玩家')
        with self.assertRaises(ValueError):
            await self.database.import_character_card(self.target['id'], 'other', self.source['id'])

    async def test_started_then_recovered_lobby_rejected(self):
        with self.database._connect() as c:
            c.execute("UPDATE instance_configs SET phase_meta_json=? WHERE session_id=?", (json.dumps({'started_at':'earlier'}), self.target['id']))
        with self.assertRaises(ValueError):
            await self.do_import()

    async def test_mismatched_template_is_atomic(self):
        with self.database._connect() as c:
            world = json.loads(c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (self.target['id'],)).fetchone()[0])
            world.setdefault('rules', {}).setdefault('character_card', {})['stats'] = {'budget': 999}
            c.execute('UPDATE instance_configs SET world_snapshot_json=? WHERE session_id=?', (json.dumps(world), self.target['id']))
        with self.assertRaises(ValueError):
            await self.do_import()
        with self.database._connect() as c:
            fields = c.execute('SELECT fields_json FROM character_card_drafts WHERE participant_id=?', (self.reserved['id'],)).fetchone()[0]
            self.assertEqual(json.loads(fields), {})

    async def test_draft_not_overwritten(self):
        with self.database._connect() as c:
            c.execute('UPDATE character_card_drafts SET fields_json=? WHERE participant_id=?', ('{"name":"现有草稿"}', self.reserved['id']))
        with self.assertRaises(ValueError):
            await self.do_import()

    async def test_target_approval_policy_not_inherited(self):
        with self.database._connect() as c:
            world = json.loads(c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (self.target['id'],)).fetchone()[0])
            world.setdefault('rules', {}).setdefault('character_card', {})['auto_approve'] = False
            c.execute('UPDATE instance_configs SET world_snapshot_json=? WHERE session_id=?', (json.dumps(world), self.target['id']))
        result = await self.do_import()
        self.assertFalse(result['auto_approved'])
        self.assertEqual(result['card_status'], 'pending_review')

    async def test_wrong_group_hidden_and_denied(self):
        with self.database._connect() as c:
            c.execute('UPDATE sessions SET group_id=? WHERE id=?', ('elsewhere', self.session['id']))
        self.assertEqual(await self.database.list_importable_cards(self.target['id'], 'user'), [])
        with self.assertRaises(ValueError):
            await self.do_import()

    async def test_running_target_denied(self):
        with self.database._connect() as c:
            c.execute("UPDATE sessions SET state='running' WHERE id=?", (self.target['id'],))
        with self.assertRaises(ValueError):
            await self.do_import()

    def test_command_is_player_action(self):
        from tavern.security import parse_tavern_command
        from tavern.constants import PLAYER_ACTIONS, MUTATING_ACTIONS
        command = parse_tavern_command('/酒馆 导入角色卡 participant_123')
        self.assertEqual(command.action, 'card_import')
        self.assertEqual(command.argument, 'participant_123')
        self.assertIn(command.action, PLAYER_ACTIONS)
        self.assertIn(command.action, MUTATING_ACTIONS)
