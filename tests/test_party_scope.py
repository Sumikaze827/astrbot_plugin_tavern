import json
import sqlite3
import unittest

from tavern.engine import TavernEngine
from tavern.party_scope import (
    filter_movement,
    local_voters,
    resolve_companions,
    validate_movement,
)
from tavern.prompts import _runtime_sections


class PartyScopeTests(unittest.TestCase):
    def setUp(self):
        self.roster = [{'id': 'a', 'group_user_id': 'u1', 'participation_status': 'active'},
                       {'id': 'b', 'group_user_id': 'u2', 'participation_status': 'active'}]

    def test_split_cannot_replace_shared_location(self):
        ops = [{'target_id': 'a', 'location': '藏经阁'}, {'target_id': 'b', 'location': '祖庭'}]
        self.assertEqual(TavernEngine._guard_shared_location_patch({'location': '藏经阁'}, ops, self.roster), {})

    def test_same_destination_can_replace_shared_location(self):
        ops = [{'target_id': p['id'], 'location': '殿堂'} for p in self.roster]
        self.assertEqual(TavernEngine._guard_shared_location_patch({'location': '殿堂'}, ops, self.roster), {'location': '殿堂'})

    def test_personal_movement_cannot_move_other_player(self):
        with self.assertRaises(ValueError):
            validate_movement([{'target_id': 'b', 'location': '祖庭'}], self.roster, {'u1'})

    def test_authorized_vote_can_move_its_members(self):
        ops = [{'target_id': p['id'], 'location': '殿堂'} for p in self.roster]
        self.assertEqual(len(validate_movement(ops, self.roster, {'u1', 'u2'})), 2)

    def test_duplicate_or_ambiguous_movement_rejected(self):
        op = {'target_id': 'a', 'location': '祖庭'}
        with self.assertRaises(ValueError):
            validate_movement([op, op], self.roster, {'u1'})
        for p in self.roster:
            p['character_name'] = '同名'
        with self.assertRaises(ValueError):
            validate_movement([{'target_id': '同名', 'location': '祖庭'}], self.roster, {'u1', 'u2'})

    def test_local_voters_exclude_remote_and_unknown(self):
        with sqlite3.connect(':memory:') as c:
            c.row_factory = sqlite3.Row
            c.execute('CREATE TABLE participants(id,session_id,group_user_id)')
            c.execute('CREATE TABLE character_runtime_states(participant_id,session_id,state_json)')
            for uid, loc in [('u1', '殿堂'), ('u2', '殿堂'), ('u3', '祖庭'), ('u4', '')]:
                c.execute('INSERT INTO participants VALUES (?,?,?)', (uid, 's', uid))
                c.execute('INSERT INTO character_runtime_states VALUES (?,?,?)', (uid, 's', json.dumps({'current_location': loc})))
            self.assertEqual(local_voters(c, 's', 'u1', ['u1', 'u2', 'u3', 'u4']), ['u1', 'u2'])
            with self.assertRaises(ValueError):
                local_voters(c, 's', 'u4', ['u1', 'u2', 'u3', 'u4'])
            with self.assertRaises(ValueError):
                local_voters(c, 's', 'u3', ['u1', 'u2', 'u3', 'u4'])

    def test_resolve_companions_keeps_actor_first_and_drops_unknown(self):
        """模型点错的名字丢弃即可，不该让整轮失败。"""
        self.roster[0]['character_name'] = '鲁迪'
        self.roster.append({'id': 'c', 'group_user_id': 'u3',
                            'participation_status': 'standby'})
        resolved = resolve_companions(['b', '鲁迪', '不存在的名字', 'c'], self.roster, 'u1')
        # c 是 standby（已退场/待命），不算同行；'不存在的名字' 丢弃。
        self.assertEqual(resolved, ['u1', 'u2'])
        # 模型没点名任何人 → 只有行动者自己，等价于原来的个人行动。
        self.assertEqual(resolve_companions([], self.roster, 'u1'), ['u1'])
        # 行动者本人不会被重复计入，且始终排在首位。
        self.assertEqual(resolve_companions(['u1', 'b'], self.roster, 'u1'), ['u1', 'u2'])

    def test_filter_movement_drops_undeclared_companions(self):
        """没写进 participants 的人不许移动——丢弃而不是报错。

        正文已经生成，为一条越界的移动记录让整轮作废对玩家没有意义；
        状态以引擎为准，名单外的人保持原位。
        """
        ops = [{'target_id': 'a', 'location': '大殿'},
               {'target_id': 'b', 'location': '大殿'}]
        kept = filter_movement(ops, self.roster, {'u1'})
        self.assertEqual([item['target_id'] for item in kept], ['a'])

        both = filter_movement(ops, self.roster, {'u1', 'u2'})
        self.assertEqual([item['target_id'] for item in both], ['a', 'b'])

    def test_filter_movement_normalizes_and_dedupes(self):
        ops = [{'target_id': 'u2', 'location': '钟楼'},
               {'target_id': 'b', 'location': '钟楼'},
               {'target_id': '凑不上的人', 'location': '别处'}]
        kept = filter_movement(ops, self.roster, {'u1', 'u2'})
        # target_id 归一化成 participant_id；同一角色只保留第一个位置；
        # 匹配不到任何成员的目标直接丢弃。
        self.assertEqual(kept, ({'target_id': 'b', 'location': '钟楼'},))

    def test_narrow_party_movement_follows_the_model_declaration(self):
        from tavern.resolution import validate_resolution

        def resolution(participants, location_ops):
            return validate_resolution({
                'mode': 'resolve',
                'narrative': '一行人推门而入。',
                'location_ops': location_ops,
                'participants': participants,
            })

        ops = [{'target_id': 'a', 'location': '大殿'},
               {'target_id': 'b', 'location': '大殿'}]

        # 模型只点名自己 → 别人的移动记录被丢弃，等价于原来的个人行动。
        solo = TavernEngine._narrow_party_movement(
            resolution(['a'], ops), self.roster, 'u1')
        self.assertEqual([item['target_id'] for item in solo.location_ops], ['a'])

        # 模型点名 A、B 一起 → 两人的移动都保留（叙事与状态一致）。
        together = TavernEngine._narrow_party_movement(
            resolution(['a', 'b'], ops), self.roster, 'u1')
        self.assertEqual(
            [item['target_id'] for item in together.location_ops], ['a', 'b']
        )

        # 收窄必须先于共享场景守卫：模型多写一条越权移动不能带出共享转场。
        patch = TavernEngine._guard_shared_location_patch(
            {'location': '大殿'}, solo.location_ops, self.roster)
        self.assertEqual(patch, {})

    def test_context_separates_narrator_and_character_knowledge(self):
        prompt = _runtime_sections(world={}, session={}, player={}, events=[], memories=[])
        self.assertIn('全局历史不等于角色已知', prompt)
        self.assertIn('会合只代表同场', prompt)
