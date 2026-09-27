import unittest
from unittest.mock import AsyncMock
from tavern.engine import TavernEngine, TavernEngineError
from tavern.config import TavernConfig
from tavern.resolution import Resolution


class SplitChoiceSceneTests(unittest.IsolatedAsyncioTestCase):
    def test_choice_prompt_replaces_previous_actors_scene(self):
        from tavern.prompts import choice_generation_prompt
        prompt = choice_generation_prompt(world={},
            session={'world_state': {'location': '街口', 'scene_summary': '与卫兵一同返回'}},
            participant={'id': 'p2', 'runtime_state': {'current_location': '赃物库内'}}, events=[])
        runtime = prompt.split('<runtime_state>')[1].split('</runtime_state>')[0]
        self.assertIn('赃物库内', runtime)
        self.assertNotIn('街口', runtime)
        self.assertNotIn('与卫兵一同返回', runtime)

    async def run_scene(self, locations, moves=(), raw_only=False, fail=False, handoff=False):
        engine = object.__new__(TavernEngine)
        choices = [{"key": key, "text": "询问屋内罗姆爷", "actor_id": "p2", "risk": "safe"} for key in 'ABCD']
        engine._generate_choices = AsyncMock(return_value=choices)
        if fail:
            engine._generate_choices.side_effect = TavernEngineError('offline')
        roster = [{"id": 'p'+str(i+1), "character_name": name, "participation_status": "active",
                   "runtime_state": {"current_location": loc}}
                  for i, (name, loc) in enumerate(zip(('鲁迪', '柏辰'), locations))]
        bad = tuple(dict(c, text="和卫兵一起赶回赃物库") for c in choices)
        if handoff:
            roster[1]['group_user_id'] = 'u2'
        resolution = Resolution(mode='resolve', narrative='鲁迪在街口找到卫兵。', check=None,
            state_patch={}, memories=(), next_choices=() if raw_only else bad,
            group_decision=None, return_progress=None, npc_ops=(), clock_ops=(), ledger_ops=(),
            location_ops=tuple(moves), status_ops=(), assist_ops=(), director_note='', raw={'next_choices': list(bad)})
        result = await engine._ensure_next_choices(resolution=resolution, provider_ids=(), world={},
            session={'id': 's', 'turn_status': {'current_user_id': 'u1'} if handoff else {}}, participant=roster[1], roster=roster, events=[],
            candidate_state={'location': '街口', 'scene_summary': '鲁迪找到卫兵'}, config=TavernConfig())
        return engine, result, roster

    async def test_correct_actor_id_does_not_accept_wrong_scene(self):
        engine, result, _ = await self.run_scene(('街口', '赃物库内'))
        engine._generate_choices.assert_awaited_once()
        args = engine._generate_choices.await_args.kwargs
        self.assertEqual(args['participant']['runtime_state']['current_location'], '赃物库内')
        self.assertEqual(args['session']['world_state']['location'], '赃物库内')
        self.assertEqual(result.next_choices[0]['text'], '询问屋内罗姆爷')

    async def test_raw_choices_cannot_bypass_split_regeneration(self):
        engine, _, _ = await self.run_scene(('街口', '赃物库内'), raw_only=True)
        engine._generate_choices.assert_awaited_once()

    async def test_this_turn_movement_is_projected_before_branch(self):
        engine, _, roster = await self.run_scene(('赃物库内', '赃物库内'),
            moves=[{'target_id': 'p1', 'location': '街口'}])
        engine._generate_choices.assert_awaited_once()
        self.assertEqual(roster[0]['runtime_state']['current_location'], '赃物库内')

    async def test_same_scene_keeps_valid_embedded_choices(self):
        engine, _, _ = await self.run_scene(('赃物库内', '赃物库内'))
        engine._generate_choices.assert_not_awaited()

    async def test_equal_location_does_not_prove_same_interaction(self):
        engine, _, _ = await self.run_scene(('宅邸', '宅邸'), handoff=True)
        engine._generate_choices.assert_awaited_once()

    async def test_next_actor_movement_is_projected_by_alias(self):
        engine, _, _ = await self.run_scene(('街口', '赃物库内'),
            moves=[{'target_id': '柏辰', 'location': '后门'}])
        self.assertEqual(engine._generate_choices.await_args.kwargs['participant']['runtime_state']['current_location'], '后门')

    async def test_failure_falls_back_locally_without_remote_guard(self):
        _, result, _ = await self.run_scene(('街口', '赃物库内'), fail=True)
        self.assertTrue(all(c['actor_id']=='p2' for c in result.next_choices))
        self.assertFalse(any('卫兵' in c['text'] for c in result.next_choices))
