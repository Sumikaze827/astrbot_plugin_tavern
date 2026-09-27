import tempfile
import unittest
from pathlib import Path

from tavern.constants import DEFAULT_WORLD_SLUG, SESSION_RUNNING, MUTATING_ACTIONS
from tavern.database import TavernDatabase
from tavern.security import parse_tavern_command
from test_chapter_progress_realign import _custom_world, _seed_instance_world, _seed_progress


class ManualCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = TavernDatabase(Path(self.temp.name))
        self.session = await self.db.ensure_session('qq', 'cp-test', 'qq:cp-test', DEFAULT_WORLD_SLUG, 'admin')
        self.sid = self.session['id']
        await self.db.transition_session(self.sid, SESSION_RUNNING, 'admin')
        with self.db._connect() as conn:
            _seed_instance_world(conn, self.sid, _custom_world())
            _seed_progress(conn, self.sid, {'current_chapter_id': 'ch_01_trace', 'chapter': '第一章', 'chapter_entered_at_turn': 0})
            conn.execute('UPDATE sessions SET turn_no = 20 WHERE id = ?', (self.sid,))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def adjust(self, complete, argument):
        return await self.db.adjust_checkpoint(self.sid, complete, argument, 'admin')

    async def progress(self):
        return (await self.db.get_session_rule_state(self.sid))['progress']

    async def test_complete_and_revert_milestone(self):
        await self.adjust(True, '里程碑')
        self.assertEqual((await self.progress())['completed_milestones'], 1)
        await self.adjust(False, '里程碑')
        p = await self.progress()
        self.assertEqual(p['completed_milestones'], 0)
        self.assertEqual(p['milestone_evidence_since_turn'], 20)
        self.assertEqual(p['chapter_entered_at_turn'], 20)
        self.assertEqual(len(await self.db.list_snapshots(self.sid)), 2)

    async def test_chapter_skip_and_reopen_dependencies(self):
        await self.adjust(True, '章节')
        p = await self.progress()
        self.assertEqual(p['current_chapter_id'], 'ch_02_center')
        self.assertEqual(p['completed_milestones'], 3)
        await self.adjust(False, '章节')
        p = await self.progress()
        self.assertEqual(p['current_chapter_id'], 'ch_01_trace')
        self.assertEqual(p['completed_milestones'], 0)

    async def test_terminal_requires_narrated_ending(self):
        await self.adjust(True, '章节')
        await self.adjust(True, '章节')
        p = await self.progress()
        self.assertTrue(p['ending_pending'])
        self.assertFalse(p.get('ending_narrated'))
        self.assertFalse(p.get('story_complete'))
        await self.adjust(False, '章节')
        self.assertFalse((await self.progress()).get('ending_pending'))

    async def test_explicit_id_and_invalid_reference_atomic(self):
        await self.adjust(True, '里程碑 m_01_02_silent_list_revealed')
        before = await self.progress()
        saves = len(await self.db.list_snapshots(self.sid))
        for argument in ['章节 ch_02_center', '里程碑 不存在', '错误']:
            with self.assertRaises(ValueError):
                await self.adjust(True, argument)
        self.assertEqual(await self.progress(), before)
        self.assertEqual(len(await self.db.list_snapshots(self.sid)), saves)

    async def test_parsing(self):
        for verb, action in [('完成', 'checkpoint_complete'), ('回退', 'checkpoint_revert')]:
            parsed = parse_tavern_command('/酒馆 ' + verb + ' 里程碑')
            self.assertEqual(parsed.action, action)
            self.assertEqual(parsed.argument, '里程碑')
            self.assertIn(action, MUTATING_ACTIONS)

    async def test_bad_successor_rolls_back_ledger_and_snapshot(self):
        world = _custom_world()
        world['rules']['progress']['chapters'][0]['next_chapter_id'] = 'missing'
        with self.db._connect() as conn:
            _seed_instance_world(conn, self.sid, world)
        with self.assertRaises(ValueError):
            await self.adjust(True, '章节')
        self.assertEqual((await self.progress())['completed_milestones'], 0)
        self.assertEqual(await self.db.list_snapshots(self.sid), [])

    async def test_preserves_story_and_turn(self):
        before = await self.db.get_session(self.sid)
        await self.adjust(True, '章节')
        after = await self.db.get_session(self.sid)
        self.assertEqual(before['world_state'], after['world_state'])
        self.assertEqual(before['turn_no'], after['turn_no'])
        self.assertGreater(after['revision'], before['revision'])
