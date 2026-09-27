import unittest
from unittest.mock import patch
from tavern.resolution import _outcome_for_roll, CheckRequest, roll_check, roll_opposed_check
from tavern.world_contract import world_contract


class MarginTests(unittest.TestCase):
    def test_boundaries_ignore_natural_dice_and_legacy_flags(self):
        legacy = {'natural_1_critical': True, 'natural_20_critical': True, 'cost_success_min_margin': -4}
        for die in (1, 10, 20):
            for margin, expected in [(-10, 'critical_failure'), (-9, 'failure'), (-2, 'failure'),
                                     (-1, 'failure'), (0, 'success'), (9, 'success'), (10, 'critical_success')]:
                self.assertEqual(_outcome_for_roll(die, margin, legacy), (expected, None))

    def test_reported_charisma_roll(self):
        with patch('tavern.resolution.secrets.randbelow', return_value=0):
            result = roll_check(CheckRequest(stat='魅力', reason='交涉', difficulty=9, modifier=6))
        self.assertEqual((result.total, result.margin, result.outcome), (7, -2, 'failure'))
        self.assertIsNone(result.critical)

    def test_opposed_uses_totals_and_keeps_defender_tie(self):
        for modifier, expected in [(15, 'success'), (9, 'failure'), (-1, 'critical_failure')]:
            with patch('tavern.resolution.secrets.randbelow', side_effect=[0, 9]):
                result = roll_opposed_check(CheckRequest(stat='魅力', reason='交涉', difficulty=9,
                    modifier=modifier, opponent_modifier=0))
            self.assertEqual(result.outcome, expected)
            self.assertIsNone(result.critical)

    def test_old_world_policy_is_normalized(self):
        from tavern.constants import DEFAULT_WORLD
        import copy
        world = copy.deepcopy(DEFAULT_WORLD)
        world.setdefault('rules', {}).setdefault('resolution', {})['outcome_policy'] = {
            'natural_1_critical': True, 'natural_20_critical': True, 'cost_success_min_margin': -4}
        policy = world_contract(world)['resolution']['outcome_policy']
        self.assertFalse(policy['natural_1_critical'])
        self.assertFalse(policy['natural_20_critical'])
        self.assertEqual(policy['cost_success_min_margin'], 0)
