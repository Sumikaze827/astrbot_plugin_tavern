import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tavern.action_contract import turn_contract
from tavern.prompts import repair_prompt
from tavern.npc_direction import check_direction


class ContractTests(unittest.TestCase):
    def test_repair_keeps_action_scene_and_dice_not_full_history(self):
        original = ('<recent_history>' + 'UNRELATED' * 10000 + '</recent_history>'
            '<player_input trust="untrusted">"去别邸谈出人出粮数目"</player_input>'
            '<acting_scene>{"current_location":"城中住处"}</acting_scene>'
            '<authoritative_check>{"result":{"outcome":"success"}}</authoritative_check>')
        repaired = repair_prompt('{}', '跳过谈判', original)
        self.assertIn('去别邸谈出人出粮数目', repaired)
        self.assertIn('城中住处', repaired)
        self.assertIn('success', repaired)
        self.assertNotIn('UNRELATED', repaired)
        self.assertLess(len(repaired), 2500)

    def test_freeform_and_previous_action_preserved(self):
        result = turn_contract('<player_freeform_action>"等她答复"</player_freeform_action>'
            '<previous_personal_action>{"settled_narrative":"她正在看信"}</previous_personal_action>')
        self.assertEqual(result['player_freeform_action'], '等她答复')
        self.assertEqual(result['previous_personal_action']['settled_narrative'], '她正在看信')


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_meeting_review_without_npc_intent(self):
        engine = SimpleNamespace(_llm_generate_metered=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({'ok': False, 'reason': '去别邸谈补给却直接出现在战场，未兑现会面'}))))
        direction = {'intents': [], 'turn_contract': {'player_input': '去别邸谈补给'}}
        config = SimpleNamespace(max_tokens=1000, request_timeout_seconds=1)
        with self.assertRaises(ValueError):
            await check_direction(engine, direction=direction, narrative='白鲸尚未现身。',
                session_id='s', provider_id='p', config=config)
        self.assertIn('去别邸谈补给', engine._llm_generate_metered.call_args.kwargs['prompt'])
        engine._llm_generate_metered.return_value = SimpleNamespace(completion_text='{"ok":true,"reason":"已交代谈判结果"}')
        await check_direction(engine, direction=direction, narrative='会面得到了明确答复。',
            session_id='s', provider_id='p', config=config)
        self.assertEqual(direction['checks'], 2)
        self.assertTrue(direction['last_check_ok'])
