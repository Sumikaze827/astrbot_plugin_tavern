import unittest
from tavern.npc_interaction import empty_interaction, parse_interaction


class InteractionTests(unittest.TestCase):
    def payload(self, **kwargs):
        p = empty_interaction()
        p.update(addressed_actor_ids=['a'], speech_content='你还好吗', status='explicit_speech')
        p.update(kwargs)
        return p

    def test_exact_speech_does_not_include_private_plan(self):
        p = parse_interaction(self.payload(), '你还好吗？我心里打算离开', {'a'})
        self.assertEqual(p['speech_content'], '你还好吗')

    def test_invented_or_history_speech_rejected(self):
        with self.assertRaises(ValueError):
            parse_interaction(self.payload(), '点头', {'a'})

    def test_unknown_actor_rejected(self):
        with self.assertRaises(ValueError):
            parse_interaction(self.payload(addressed_actor_ids=['other']), '你还好吗', {'a'})

    def test_movement_remote_and_private_group_not_authorized(self):
        for change in ({'pending_sequence': True}, {'requires_delivery': True},
                       {'speech_mode': 'private_channel'},
                       {'speech_mode': 'whisper', 'addressed_actor_ids': ['a', 'b']}):
            p = parse_interaction(self.payload(**change), '你还好吗', {'a', 'b'})
            self.assertEqual(p['status'], 'unconfirmed')
