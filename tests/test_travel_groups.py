import unittest
from tavern.travel_groups import cohorts, settle_travel, authorized_movement_groups
from tavern.resolution import validate_resolution


class TravelTests(unittest.TestCase):
    def test_freeform_remote_join_degrades_without_teleport_or_exception(self):
        groups = [['a', 'b'], ['c']]
        result = settle_travel(self.resolution({'mode': 'join', 'members': ['a', 'c'],
            'reason': '想找远处队友治疗'}, ops=[]), self.roster, groups, 'a', acceptance_mode=True)
        self.assertEqual(result.raw['_travel_groups'], groups)
        self.assertFalse(result.location_ops)
        self.assertNotIn('travel_change', result.raw)
        self.assertIn('_travel_adjustment', result.raw)

    def setUp(self):
        self.roster = [{'id': pid, 'group_user_id': pid, 'participation_status': 'active',
            'runtime_state': {'current_location': loc}} for pid, loc in [('a', '客厅'), ('b', '客厅'), ('c', '城外')]]

    def resolution(self, change=None, ops=None):
        p = {'mode': 'resolve', 'narrative': '众人走入院子。', 'participants': ['a'],
            'location_ops': ops if ops is not None else [{'target_id': 'a', 'location': '院子'}]}
        if change:
            p['travel_change'] = change
        return validate_resolution(p)

    def test_default_follows_same_cohort_not_remote_player(self):
        groups = cohorts(self.roster)
        r = settle_travel(self.resolution(), self.roster, groups, 'a')
        self.assertEqual({x['target_id']: x['location'] for x in r.location_ops}, {'a': '院子', 'b': '院子'})
        self.assertEqual(r.raw['_travel_groups'], [['a', 'b'], ['c']])

    def test_explicit_split_persists_even_at_same_location(self):
        r = settle_travel(self.resolution({'mode': 'split', 'members': ['a'], 'reason': '我独自去院子，其余留守'}),
            self.roster, cohorts(self.roster), 'a')
        groups = cohorts(self.roster, r.raw['_travel_groups'])
        r2 = settle_travel(self.resolution(), self.roster, groups, 'a')
        self.assertEqual([x['target_id'] for x in r2.location_ops], ['a'])

    def test_join_requires_actual_same_location(self):
        pending = settle_travel(self.resolution({'mode': 'join', 'members': ['a', 'c'], 'reason': '会合'}),
            self.roster, [['a'], ['b'], ['c']], 'a')
        self.assertEqual(pending.raw['_travel_groups'], [['a'], ['b'], ['c']])
        self.assertNotIn('travel_change', pending.raw)
        self.assertEqual([op['target_id'] for op in pending.location_ops], ['a'])
        r = settle_travel(self.resolution({'mode': 'join', 'members': ['a', 'b'], 'reason': '明确会合后同行'}),
            self.roster, [['a'], ['b'], ['c']], 'a')
        self.assertEqual(len(r.location_ops), 2)

    def test_unknown_locations_do_not_create_group(self):
        for p in self.roster:
            p['runtime_state'] = {}
        self.assertEqual(cohorts(self.roster), [['a'], ['b'], ['c']])

    def test_conflicting_locations_and_remote_ops_rejected(self):
        for ops in ([{'target_id': 'c', 'location': '院子'}],
                    [{'target_id': 'a', 'location': '院子'}, {'target_id': 'b', 'location': '楼上'}]):
            with self.assertRaises(ValueError):
                settle_travel(self.resolution(ops=ops), self.roster, cohorts(self.roster), 'a')

    def test_talking_does_not_move_anyone(self):
        r = settle_travel(self.resolution(ops=[]), self.roster, cohorts(self.roster), 'a')
        self.assertFalse(r.location_ops)

    def test_inactive_members_not_carried(self):
        self.roster[1]['participation_status'] = 'away'
        self.assertEqual(cohorts(self.roster, [['a', 'b'], ['c']]), [['a'], ['c']])

    def test_passed_vote_can_split_but_not_implicitly_merge(self):
        groups = authorized_movement_groups(self.roster, [['a', 'b'], ['c']],
            [{'target_id': 'a', 'location': '城外'}])
        self.assertEqual(groups, [['a'], ['b'], ['c']])

    def test_stale_cohort_diverged_by_other_path_is_healed(self):
        """同行记录与权威位置不符时必须就地拆开，不能卡死后续移动。

        回归：主持推进（dm_beat）单独把 a 挪走后不会经过 settle_travel，
        travel_groups 仍写着 [['a','b']]。旧实现会让 a 之后每次普通移动都
        抛「同行记录与实际位置冲突」，而 a 无从修复，整轮反复失败。
        """
        self.roster[0]['runtime_state']['current_location'] = '院子'
        self.assertEqual(cohorts(self.roster, [['a', 'b'], ['c']]), [['a'], ['b'], ['c']])
        r = settle_travel(self.resolution(), self.roster, cohorts(self.roster, [['a', 'b'], ['c']]), 'a')
        self.assertEqual([x['target_id'] for x in r.location_ops], ['a'])

    def test_unknown_location_member_is_not_carried(self):
        """小队成员位置未确认时不得被一起传送；未知不等于同场。"""
        self.roster[1]['runtime_state'] = {}
        self.assertEqual(cohorts(self.roster, [['a', 'b'], ['c']]), [['a'], ['b'], ['c']])

    def test_empty_saved_list_bootstraps_from_locations(self):
        """空列表（已初始化但无内容）按未初始化处理，不能把全队拆散。"""
        self.assertEqual(cohorts(self.roster, []), [['a', 'b'], ['c']])

    def test_dm_beat_style_partial_move_keeps_state_consistent(self):
        """任意路径移动后，重建的小队必须与位置一致，下次普通移动不再冲突。"""
        groups = authorized_movement_groups(self.roster, [['a', 'b'], ['c']],
            [{'target_id': 'a', 'location': '院子'}])
        moved = [dict(p) for p in self.roster]
        moved[0]['runtime_state'] = {'current_location': '院子'}
        self.assertEqual(cohorts(moved, groups), [['a'], ['b'], ['c']])
        settle_travel(self.resolution(), moved, cohorts(moved, groups), 'a')

    def test_cohort_members_without_id_are_ignored(self):
        roster = [{'participation_status': 'active'}, *self.roster]
        self.assertEqual(cohorts(roster, [['a', 'b'], ['c'], [None]]), [['a', 'b'], ['c']])
