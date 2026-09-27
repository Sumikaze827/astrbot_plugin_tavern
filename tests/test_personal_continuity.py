import unittest
from tavern.continuity import previous_action
from tavern.prompts import planning_prompt, checked_resolution_prompt


def event(seq, turn, role, actor, text):
    return dict(seq=seq, turn_no=turn, role=role, actor_id=actor,
                content=text, id=str(seq), session_id='s', meta={})


class PersonalContinuityTests(unittest.TestCase):
    def setUp(self):
        self.player = {'user_id': 'u1', 'runtime_state': {'current_location': '屋内'}}
        self.events = [event(1, 1, 'player', 'u1', '尝试开门'),
                       event(2, 1, 'narrator', 'narrator', '门未打开'),
                       event(3, 2, 'player', 'u2', '去街口'),
                       event(4, 2, 'narrator', 'narrator', '见到卫兵')]

    def test_pairs_own_decision_with_result_not_latest_global(self):
        p = previous_action(self.player, self.events, 2)
        self.assertEqual(p['decision'], '尝试开门')
        self.assertEqual(p['settled_narrative'], '门未打开')
        self.assertEqual(p['intervening_turns'], 1)

    def test_no_name_based_matching_or_invented_history(self):
        self.assertFalse(previous_action({'character_name': 'u1'}, self.events, 2)['available'])
        self.assertFalse(previous_action(self.player, [], 2)['available'])

    def test_unpaired_latest_does_not_resurrect_older_action(self):
        self.events.append(event(5, 3, 'player', 'u1', '未结算行动'))
        self.assertFalse(previous_action(self.player, self.events, 3)['available'])

    def test_ambiguous_results_fail_closed(self):
        self.events.append(event(5, 1, 'narrator', 'narrator', '另一个结果'))
        self.assertFalse(previous_action(self.player, self.events, 2)['available'])

    def test_future_and_budget(self):
        self.assertFalse(previous_action(self.player, self.events, 0)['available'])
        self.events[1]['content'] = '长' * 3000
        p = previous_action(self.player, self.events, 2)
        self.assertTrue(p['truncated'])
        self.assertEqual(len(p['settled_narrative']), 2400)

    def test_both_narration_paths_have_past_only_contract(self):
        kwargs = dict(world={}, session={'turn_no': 2, 'personal_history_events': self.events},
                      player=self.player, player_input='检查门锁', events=self.events[-2:], memories=[])
        prompts = [planning_prompt(**kwargs, allow_checks=True),
                   checked_resolution_prompt(**kwargs, check={}, dice={'outcome': 'success'})]
        for prompt in prompts:
            self.assertIn('past-settled-only', prompt)
            self.assertIn('门未打开', prompt)
            self.assertIn('不重演动作', prompt)
            self.assertIn('屋内', prompt)
