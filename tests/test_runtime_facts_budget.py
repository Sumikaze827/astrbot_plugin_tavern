import copy
import json
import unittest
from tavern.prompts import (_world_state_projection, _runtime_sections,
    choice_generation_prompt, RESOLUTION_SCHEMA)
from tavern.constants import DEFAULT_WORLD


class RuntimeFactsBudgetTests(unittest.TestCase):
    def setUp(self):
        self.state = {'facts': [f'fact-{i:03d}' for i in range(200)], 'location': '客厅',
            'travel_groups': [['a', 'b']], 'statuses': ['受伤'], 'time': '翌日清晨'}

    def test_recent_eighty_only_without_mutation(self):
        original = copy.deepcopy(self.state)
        projected = _world_state_projection(self.state)
        self.assertEqual(projected['facts'], self.state['facts'][120:])
        projected['facts'].append('test')
        self.assertEqual(self.state, original)
        for key in ('travel_groups', 'statuses', 'time', 'location'):
            self.assertEqual(projected[key], original[key])

    def test_small_empty_and_missing(self):
        self.assertEqual(_world_state_projection(None), {})
        self.assertEqual(_world_state_projection({'facts': []}), {'facts': []})
        self.assertEqual(_world_state_projection({'facts': ['a']}), {'facts': ['a']})

    def test_narrative_and_choices_share_budget(self):
        session = {'world_state': self.state, 'scene_clocks': [{'trigger_text': '明早兑现旧约'}]}
        prompts = [_runtime_sections(world={}, session=session, player={}, events=[], memories=[]),
            choice_generation_prompt(world=DEFAULT_WORLD, session=session, participant={}, events=[])]
        for prompt in prompts:
            start = prompt.index('<runtime_state')
            payload = json.loads(prompt[prompt.index('>', start) + 1:prompt.index('</runtime_state>', start)])
            self.assertEqual(payload['facts'], self.state['facts'][-80:])
        self.assertIn('明早兑现旧约', prompts[0])
        self.assertEqual(len(self.state['facts']), 200)

    def test_write_guidance_is_in_shared_resolution_schema(self):
        guidance = RESOLUTION_SCHEMA['state_patch']['facts_add'][0]
        for rule in ('影响后续判断', '不写家具', '同一事不重复记', '未执行计划'):
            self.assertIn(rule, guidance)
