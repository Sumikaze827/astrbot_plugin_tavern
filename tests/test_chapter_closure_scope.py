import unittest
from tavern.prompts import chapter_closure_prompt


class ClosureScopeTests(unittest.TestCase):
    def prompt(self, terminal=False):
        chapter = {"id": "route", "title": "寻路", "current_objective": "获得可靠路线",
                   "milestones": [{"id": "route_known", "label": "卫兵提供地址"}],
                   "exits_when": {"all_milestones": ["route_known"]},
                   "next_chapter_id": "trade"}
        world = {"rules": {"progress": {"chapters": [chapter,
                 {"id": "trade", "current_objective": "谈妥徽章交换", "milestones": []}]}}}
        return chapter_closure_prompt(world=world, chapter=chapter,
                                      events=[], terminal=terminal)

    def test_route_goal_and_next_stage_are_supplied(self):
        prompt = self.prompt()
        for value in ("route_known", "卫兵提供地址", "谈妥徽章交换", "all_milestones"):
            self.assertIn(value, prompt)

    def test_nonterminal_scope_does_not_require_global_closure(self):
        prompt = self.prompt()
        self.assertNotIn("仅发现下一地点或得到路线不算", prompt)
        self.assertIn("不要求每个队员重复敲门", prompt)
        self.assertIn("若目标要求实际到达、交付或脱险", prompt)
        self.assertIn("quote 必须是该事件", prompt)

    def test_terminal_still_requires_actual_ending(self):
        prompt = self.prompt(terminal=True)
        self.assertIn("最终方案已经实际执行", prompt)
        self.assertIn("主线结果、直接后果和队伍整体去向", prompt)
        self.assertNotIn("实际得到这些结果即可", prompt)
