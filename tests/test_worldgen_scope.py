import asyncio
import unittest
from tavern.worldgen.scope import normalize_scope, count_problems, arc_count_problems
from tavern.worldgen.models import JobState
from test_worldgen_service import ServiceHarness


class ScopeValidationTests(unittest.TestCase):
    def test_blank_and_exact_counts(self):
        self.assertEqual(normalize_scope({'target_chapters': ''}), {})
        self.assertEqual(normalize_scope({'target_chapters': '4', 'target_milestones': '8'}),
                         {'target_chapters': 4, 'target_milestones': 8})

    def test_invalid_and_impossible_counts(self):
        for value in (0, -1, True, '2.5', 'abc', 97):
            with self.assertRaises(ValueError):
                normalize_scope({'target_milestones': value})
        with self.assertRaises(ValueError):
            normalize_scope({'target_chapters': 4, 'target_milestones': 3})

    def test_counts_are_not_approval_item_count(self):
        chapters = [{'milestones': [{}, {}]}, {'milestones': [{}]}]
        self.assertEqual(count_problems(chapters, {'target_chapters': 2, 'target_milestones': 3}), [])
        self.assertTrue(count_problems(chapters, {'target_milestones': 5}))
        self.assertTrue(arc_count_problems({'candidate_chapters': [{}, {}]}, {'target_chapters': 1}, 12))


class ScopeWorkflowTests(ServiceHarness):
    def test_exact_contract_reaches_delivery(self):
        async def run():
            job_id = await self._create(target_chapters=2, target_milestones=2)
            self.assertIs(self.store.load(job_id).state, JobState.AWAITING_APPROVAL)
            await self.service.approve(job_id, submit=True, actor='admin')
            await self._drain(job_id)
            record = self.store.load(job_id)
            self.assertIs(record.state, JobState.SUCCEEDED, record.error)
            self.assertIn('恰好 2', self.llm.prompt_for('worldgen_arc_map'))
            self.assertIn('本章必须恰好 1', self.llm.prompt_for('worldgen_extract_chapter'))
            self.assertIn('story_size_contract', self.llm.prompt_for('worldgen_prose'))
        asyncio.run(run())

    def test_mismatched_proposal_cannot_be_approved_through(self):
        async def run():
            job_id = await self._create(target_chapters=2, target_milestones=4)
            await self.service.approve(job_id, submit=True, actor='admin')
            await self._drain(job_id)
            record = self.store.load(job_id)
            self.assertIs(record.state, JobState.AWAITING_APPROVAL)
            self.assertIn('数量不符合', record.message)
            self.assertNotIn('worldgen_prose', self.llm.calls)
        asyncio.run(run())

    def test_global_revision_keeps_old_and_clears_stale_approvals(self):
        async def run():
            old = await self._create(target_chapters=2, target_milestones=2)
            original = self.store.read_artifact_json(old, '10_checkpoints.draft.json')
            self.service._spawn = lambda _: None
            new = await self.service.approve(old, actor='admin', global_feedback='合并日常，强化村民冲突',
                                             scope_changes={'target_chapters': 1, 'target_milestones': 2})
            self.assertNotEqual(new.job_id, old)
            self.assertEqual(new.request['revises_job_id'], old)
            self.assertEqual(new.request['target_chapters'], 1)
            self.assertNotIn('章节恰好 2', new.request['requirements'])
            self.assertIn('合并日常', new.request['requirements'])
            self.assertEqual(original, self.store.read_artifact_json(old, '10_checkpoints.draft.json'))
            self.assertFalse(self.store.has_artifact(new.job_id, '11_checkpoints.approved.json'))
            self.assertNotIn('_npc_details', new.request)
        asyncio.run(run())
