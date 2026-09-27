import unittest

from tavern.engine import TavernEngine


class HealingTargetTests(unittest.TestCase):
    def run_cure(self, text, ops=(), outcome="success", statuses=None):
        roster = [
            {"id": "p1", "character_name": "甲", "character_code": "alpha",
             "runtime_state": {"statuses": statuses or [{"name": "中毒"}]}},
            {"id": "p2", "character_name": "乙",
             "runtime_state": {"statuses": [{"name": "中毒"}]}},
        ]
        return TavernEngine._healing_status_ops(
            player_input=text, outcome=outcome, status_ops=ops,
            roster=roster, acting_participant=roster[0],
        )

    def test_unnamed_treatment_is_not_party_wide(self):
        self.assertEqual(self.run_cure("治疗中毒"), [])

    def test_successful_targeted_generic_treatment(self):
        self.assertEqual(self.run_cure("请牧师治疗甲"),
                         [{"op": "remove", "target_id": "p1", "name": "中毒"}])

    def test_no_check_wish_is_not_completion(self):
        self.assertEqual(self.run_cure("我想治疗甲的中毒", outcome=""), [])

    def test_failure_does_not_synthesize_cure(self):
        self.assertEqual(self.run_cure("治疗甲的中毒", outcome="failure"), [])

    def test_negated_partial_does_not_block_cure(self):
        self.assertEqual(self.run_cure("不要只缓解，请彻底治疗甲的中毒")[0]["op"], "remove")

    def test_partial_treatment_remains_partial(self):
        op = {"op": "update", "target_id": "p1", "name": "中毒", "severity": "minor"}
        self.assertEqual(self.run_cure("治疗甲，先缓解中毒", [op]), [op])

    def test_alias_and_no_check_explicit_treatment(self):
        result = self.run_cure("治疗甲", [{"op": "update", "target_id": "alpha", "name": "中毒"}], outcome="")
        self.assertEqual(result, [{"op": "remove", "target_id": "p1", "name": "中毒"}])

    def test_story_lock_cannot_be_removed_by_ordinary_cure(self):
        status = {"name": "中毒", "policy_source": "world", "healing_policy": "story_locked"}
        self.assertEqual(self.run_cure("治疗甲的中毒", [{"op": "remove", "target_id": "p1", "name": "中毒"}], statuses=[status]), [])

    def test_runtime_invented_lock_does_not_block_cure(self):
        self.assertEqual(self.run_cure("治疗甲的中毒", statuses=[{"name": "中毒", "healing_policy": "permanent"}])[0]["op"], "remove")

    def test_generic_cure_does_not_guess_among_multiple_statuses(self):
        self.assertEqual(self.run_cure("治疗甲", statuses=[{"name": "中毒"}, {"name": "暴露"}]), [])

    def test_named_cure_preserves_other_statuses(self):
        self.assertEqual(self.run_cure("治疗甲的中毒", statuses=[{"name": "中毒"}, {"name": "暴露"}]),
                         [{"op": "remove", "target_id": "p1", "name": "中毒"}])
