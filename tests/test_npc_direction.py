import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tavern.npc_direction import canonical_profile, prepare_direction, source_context, parse_intent, check_direction
from tavern.npc_scope import (anchor, scope_for_action, sanitize_npc_ops, co_located,
    validate_dependencies, record_perceptions)
from tavern.database_support import DatabaseConflictError


def fixture():
    policy = {'evidence_ids': ['gm:hall'], 'scenes': {'hall': {'public_hearing': True, 'whisper_same_zone': True}, 'corridor': {}},
        'location_map': {'立柱旁': {'scene_id': 'hall', 'zone_id': 'left', 'evidence_id': 'gm:hall'},
            '殿中央': {'scene_id': 'hall', 'zone_id': 'center', 'evidence_id': 'gm:hall'},
            '殿外': {'scene_id': 'corridor', 'zone_id': 'door', 'evidence_id': 'gm:hall'}}, 'relations': []}
    world = {'slug': 'test', 'rules': {'npc_direction': {'enabled': True, 'interaction': policy}}}
    player = {'id': 'p', 'user_id': 'u', 'runtime_state': {'current_location': '立柱旁'}}
    npcs = [{'id': 'felt', 'name': '菲鲁特', 'revision': 1, 'state_revision': 1,
        'public_profile': {'actor_complexity': 'full', 'personality': '保护家人'}, 'state': {'location': '殿中央'},
        'known_facts': ['罗姆爷是家人'], 'misconceptions': []},
        {'id': 'rom', 'name': '罗姆爷', 'revision': 1, 'state_revision': 1,
         'public_profile': {'actor_complexity': 'full'}, 'state': {'location': '殿外'},
         'known_facts': ['只有罗姆爷知道的秘密'], 'misconceptions': []}]
    return world, player, npcs


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.world, self.player, self.npcs = fixture()

    def scope(self, action='询问菲鲁特是否参选'):
        return scope_for_action(world=self.world, session={'id': 's', 'revision': 1}, player=self.player,
            action=action, npcs=self.npcs, personal_text='罗姆爷曾在内殿')

    def test_explicit_same_scene_different_text_and_outside_isolation(self):
        s = self.scope()
        self.assertEqual(s['decision_actors'], ['felt'])
        self.assertEqual([p['observer_id'] for p in s['perception_candidates']], ['felt'])
        self.assertIn('rom', s['context_actors'])

    def test_mention_not_addressee(self):
        self.assertFalse(self.scope('向同伴议论菲鲁特到底参不参选')['decision_actors'])

    def test_unknown_prefix_and_stale_do_not_authorize(self):
        self.player['runtime_state']['current_location'] = '立柱旁外面'
        self.assertFalse(self.scope()['perception_candidates'])
        self.player['runtime_state'] = {'current_location': '立柱旁', 'spatial': {'status': 'stale'}}
        self.assertFalse(self.scope()['perception_candidates'])

    def test_whisper_and_pending_move_not_delivered(self):
        self.assertFalse(self.scope('低声询问菲鲁特是否参选')['perception_candidates'])
        self.assertFalse(self.scope('走进大厅然后询问菲鲁特是否参选')['perception_candidates'])
        self.assertFalse(self.scope('凑近菲鲁特耳边询问菲鲁特是否参选')['perception_candidates'])

    def test_explicit_acoustic_edge_only_for_shout(self):
        policy = self.world['rules']['npc_direction']['interaction']
        policy['relations'] = [{'from': 'hall', 'to': 'corridor', 'shout_content': True, 'evidence_id': 'gm:hall'}]
        self.assertEqual(self.scope('高声询问罗姆爷是否愿意离开')['decision_actors'], ['rom'])
        self.assertNotIn('rom', self.scope('询问罗姆爷是否愿意离开')['decision_actors'])

    def test_remote_attempt_is_not_delivery(self):
        self.assertFalse(self.scope('使用传讯石告诉罗姆爷快跑')['perception_candidates'])
        self.assertFalse(self.scope('希望罗姆爷知道此事')['perception_candidates'])

    def test_other_player_location_not_overridden(self):
        self.player['runtime_state']['current_location'] = '偏廊石阶'
        self.assertFalse(self.scope()['decision_actors'])

    def test_model_cannot_award_evidence_or_remote_knowledge(self):
        # 越界条目就地剥离并留痕，不再抛错作废整轮（2026-09-20 线上事故）。
        cleaned, violations = sanitize_npc_ops(
            [{'npc_id': 'rom', 'known_facts': ['听见殿内全部对话']}], self.scope())
        self.assertEqual(cleaned[0]['known_facts'], [])
        self.assertEqual([v['kind'] for v in violations], ['knowledge_without_perception'])
        cleaned, violations = sanitize_npc_ops(
            [{'npc_id': 'felt', 'runtime_state': {'spatial': {'status': 'confirmed'}, 'status': 'active'}}],
            self.scope())
        self.assertNotIn('spatial', cleaned[0]['runtime_state'])
        self.assertEqual(cleaned[0]['runtime_state']['status'], 'active')
        self.assertEqual([v['kind'] for v in violations], ['self_granted_credentials'])

    def test_allowed_observer_keeps_authored_knowledge(self):
        cleaned, violations = sanitize_npc_ops(
            [{'npc_id': 'felt', 'known_facts': ['已听见本次提问']}], self.scope())
        self.assertEqual(cleaned[0]['known_facts'], ['已听见本次提问'])
        self.assertFalse(violations)

    def test_unmapped_scene_no_longer_voids_the_turn(self):
        """复现线上事故：玩家走进作者 location_map 未覆盖的场景后，
        任何 NPC 都无法被认证为在场；模型给在场 NPC 写 known_facts
        过去会连抛两次错、整轮裁定作废。现在只剥离条目。
        """
        self.player['runtime_state'] = {
            'current_location': '罗兹瓦尔城中住处（王城正门外青石窄街，深绿木门小院）'}
        self.npcs[0]['state'] = {'location': '罗兹瓦尔宅邸、王都'}
        scope = self.scope('问菲鲁特名册还差哪几处，当场把随员登记收尾')
        self.assertTrue(scope['enforce_knowledge'])
        self.assertFalse(scope['perception_candidates'])
        cleaned, violations = sanitize_npc_ops(
            [{'op': 'update', 'npc_id': 'felt', 'known_facts': ['得知名册进度'],
              'runtime_state': {'status': 'active'}}], scope)
        self.assertEqual(cleaned[0]['known_facts'], [])
        self.assertEqual(cleaned[0]['runtime_state'], {'status': 'active'})
        self.assertEqual(len(violations), 1)
        # 未开启知识闸门的副本完全不受影响。
        cleaned, violations = sanitize_npc_ops(
            [{'npc_id': 'felt', 'known_facts': ['自由记录']}], {'enforce_knowledge': False})
        self.assertEqual(cleaned[0]['known_facts'], ['自由记录'])
        self.assertFalse(violations)

    def test_co_located_by_plain_location_text(self):
        self.assertTrue(co_located(
            {'status': 'unknown', 'location_text': '窄街小院'},
            {'status': 'unknown', 'location_text': '窄街小院'}))
        self.assertTrue(co_located(
            {'status': 'unknown', 'location_text': '窄街小院东厢'},
            {'status': 'unknown', 'location_text': '窄街小院'}))
        self.assertFalse(co_located(
            {'status': 'unknown', 'location_text': '窄街小院'},
            {'status': 'unknown', 'location_text': '王座之间内殿'}))
        self.assertFalse(co_located(
            {'status': 'unknown', 'location_text': ''},
            {'status': 'unknown', 'location_text': ''}))
        # 已确认坐标仍以 scene_id 为准，文本相似不能覆盖。
        self.assertFalse(co_located(
            {'status': 'confirmed', 'scene_id': 'hall', 'location_text': '立柱旁'},
            {'status': 'confirmed', 'scene_id': 'corridor', 'location_text': '立柱旁'}))

    def test_core_wins_over_runtime(self):
        self.world['rules']['npc_direction']['core_profiles'] = {'菲鲁特': {'personality': '不抛弃家人'}}
        self.assertEqual(canonical_profile(self.npcs[0], self.world)['personality'], '不抛弃家人')

    def test_receipts_idempotent_and_version_conflict(self):
        with sqlite3.connect(':memory:') as c:
            c.executescript('CREATE TABLE session_characters(id TEXT,session_id TEXT,known_facts_json TEXT,revision INT,updated_at TEXT);'
                'CREATE TABLE session_character_states(character_id TEXT PRIMARY KEY,state_json TEXT,revision INT,updated_at TEXT);')
            for npc in self.npcs:
                c.execute('INSERT INTO session_characters VALUES (?,?,?,1,?)', (npc['id'], 's', '[]', 'now'))
                c.execute('INSERT INTO session_character_states VALUES (?,?,1,?)', (npc['id'], '{}', 'now'))
            scope = self.scope('告诉菲鲁特罗姆爷是叛徒')
            validate_dependencies(c, 's', scope)
            record_perceptions(c, 's', scope, 'evt1', 1, 'now')
            record_perceptions(c, 's', scope, 'evt1', 1, 'now')
            facts = json.loads(c.execute("SELECT known_facts_json FROM session_characters WHERE id='felt'").fetchone()[0])
            self.assertEqual(len(facts), 1)
            self.assertIn('不证明内容为真', facts[0])
            self.assertEqual(c.execute("SELECT known_facts_json FROM session_characters WHERE id='rom'").fetchone()[0], '[]')
            with self.assertRaises(DatabaseConflictError):
                validate_dependencies(c, 's', scope)

    def test_source_registry_scope_and_no_player_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'novel').mkdir()
            (root / 'novel' / 'a.md').write_text('她保护家人\n当前场景\n', encoding='utf-8')
            registry = {'test': {'root': str(root / 'novel'), 'chapters': {'ch': {'spans': [
                {'file': 'a.md', 'start': 1, 'end': 2, 'purpose': 'motivation', 'timeline': 'single_line_reference'},
                {'file': 'a.md', 'start': 1, 'end': 2, 'purpose': 'scene', 'timeline': 'future_loop'}]}}}}
            (root / 'npc_source_registry.json').write_text(json.dumps(registry), encoding='utf-8')
            self.assertEqual(len(source_context(root, 'test', 'ch', '../secrets')['passages']), 1)
            self.assertFalse(source_context(root, 'test', 'future', '')['passages'])


class DirectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.world, self.player, self.npcs = fixture()
        self.session = {'id': 's', 'revision': 1, 'turn_no': 0, 'progress': {'current_chapter_id': 'ch'}}
        payload = {'npc_id': 'felt', 'stance': 'conditional', 'goal': '保护家人', 'intent': '要求释放家人', 'condition': '能听到回应', 'reason': '重视家人'}
        self.engine = SimpleNamespace(database=SimpleNamespace(list_session_characters=AsyncMock(return_value=self.npcs), data_dir=None),
            _llm_generate_metered=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(payload))))
        self.config = SimpleNamespace(max_tokens=1000, request_timeout_seconds=1)
        async def generate(**kwargs):
            if kwargs['request_type'] == 'npc_interaction':
                from tavern.npc_interaction import empty_interaction
                routing = empty_interaction()
                if '询问菲鲁特是否参选' in kwargs['prompt']:
                    routing.update(mentioned_actor_ids=['felt'], addressed_actor_ids=['felt'],
                        speech_content='询问菲鲁特是否参选', status='explicit_speech')
                return SimpleNamespace(completion_text=json.dumps(routing))
            return SimpleNamespace(completion_text=json.dumps(payload))
        self.engine._llm_generate_metered.side_effect = generate

    async def prepare(self, action='询问菲鲁特是否参选'):
        await prepare_direction(self.engine, session_id='s', world=self.world, session=self.session,
            player=self.player, action=action, events=[], config=self.config, provider_ids=['model'])

    async def test_private_input_isolated_and_intent_not_result(self):
        await self.prepare()
        self.assertEqual(self.engine._llm_generate_metered.await_count, 2)
        prompt = self.engine._llm_generate_metered.call_args.kwargs['prompt']
        self.assertNotIn('只有罗姆爷知道的秘密', prompt)
        self.assertNotIn('player_attempt', prompt)
        self.assertEqual(self.session['npc_direction']['intents'][0]['npc_id'], 'felt')

    async def test_ordinary_or_disabled_no_extra_call(self):
        self.world['rules']['npc_direction']['enabled'] = False
        self.session['session_characters'] = ['legacy']
        await self.prepare()
        self.assertEqual(self.session['session_characters'], ['legacy'])
        self.engine._llm_generate_metered.assert_not_awaited()
        self.world['rules']['npc_direction']['enabled'] = True
        await self.prepare('整理自己的背包')
        self.assertEqual(self.engine._llm_generate_metered.await_count, 1)
        self.assertEqual(self.engine._llm_generate_metered.call_args.kwargs['request_type'], 'npc_interaction')

    async def test_unavailable_not_automatic_refusal(self):
        self.engine._llm_generate_metered.side_effect = TimeoutError()
        await self.prepare()
        self.assertEqual(self.session['npc_direction']['intents'], [])
        self.assertEqual(self.session['npc_direction']['status'], 'fallback_core_only')

    async def test_rejected_repair_rechecked_not_silently_accepted(self):
        direction = {'intents': [{'npc_id': 'felt'}], 'trigger': 'major'}
        self.engine._llm_generate_metered.side_effect = None
        self.engine._llm_generate_metered.return_value = SimpleNamespace(completion_text='{"ok":false,"reason":"无故反转"}')
        for _ in range(3):
            with self.assertRaises(ValueError):
                await check_direction(self.engine, direction=direction, narrative='反转', session_id='s', provider_id='model', config=self.config)
        self.assertEqual(self.engine._llm_generate_metered.await_count, 2)

    async def test_voice_checked_without_intents_or_keyword_trigger(self):
        direction = {'intents': [], 'voice_cast': [{'npc_id': 'felt', 'core': {'personality': '保护家人'}}]}
        self.engine._llm_generate_metered.side_effect = None
        self.engine._llm_generate_metered.return_value = SimpleNamespace(completion_text='{"ok":false,"reason":"把内部约束念成台词"}')
        with self.assertRaises(ValueError):
            await check_direction(self.engine, direction=direction, narrative='只写可核实的，不写猜测。',
                session_id='s', provider_id='model', config=self.config)
        self.assertEqual(direction['review_status'], 'rejected')

    async def test_followup_without_name_or_major_keyword(self):
        routing = {'mentioned_actor_ids': [], 'addressed_actor_ids': ['felt'], 'speech_mode': 'public',
            'speech_content': '那你呢？', 'pending_sequence': False, 'requires_delivery': False, 'status': 'explicit_speech'}
        intent = {'npc_id': 'felt', 'stance': 'wait', 'goal': '保护家人', 'intent': '回应',
            'condition': '当面', 'reason': '关切', 'expression': '不耐烦但认真回答'}
        self.engine._llm_generate_metered.side_effect = [SimpleNamespace(completion_text=json.dumps(routing)),
            SimpleNamespace(completion_text=json.dumps(intent))]
        await self.prepare('那你呢？')
        direction = self.session['npc_direction']
        self.assertEqual(direction['scope']['decision_actors'], ['felt'])
        self.assertEqual(direction['intents'][0]['expression'], '不耐烦但认真回答')

    async def test_router_failure_does_not_fall_back_to_regex(self):
        self.engine._llm_generate_metered.side_effect = TimeoutError()
        await self.prepare()
        self.assertEqual(self.session['npc_direction']['interaction_status'], 'unavailable')
        self.assertEqual(self.session['npc_direction']['scope']['decision_actors'], [])

    async def test_intent_schema_cannot_emit_state(self):
        p = {'npc_id': 'felt', 'stance': 'act', 'goal': '保护', 'intent': '尝试逃脱', 'condition': '挣脱成功', 'reason': '救人', 'state_patch': {'dead': True}}
        self.assertNotIn('state_patch', parse_intent(p, 'felt'))
        with self.assertRaises(ValueError):
            parse_intent(p, 'rom')


if __name__ == '__main__':
    unittest.main()
