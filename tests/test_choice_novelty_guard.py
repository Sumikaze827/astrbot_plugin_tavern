"""选项不得抢跑尚未登场的章节专名（2026-09-20）。

线上实例：ch_06 刚开局、正文 86k 字里从没出现过「魔女教」「白鲸」，而选项
已经写成「派人去梅瑟斯领方向打听魔女教和归途白鲸的传闻」「探她是否愿就
白鲸威胁共商行动」。原因是章节目标/里程碑标签属于主持人视角的推进方向，
却以 plugin-authoritative 的身份注入了选项提示。
"""

from __future__ import annotations

import unittest

from tavern.engine import TavernEngine


def _world(label_words) -> dict:
    return {
        "rules": {
            "progress": {
                "chapters": [
                    {
                        "id": "ch_06",
                        "milestones": [
                            {
                                "id": "m_06_01",
                                "label": "获知梅瑟斯领面临魔女教威胁与归途白鲸风险",
                                "evidence_required": [
                                    {"type": "clue_keyword_any",
                                     "match": list(label_words)}
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }


class ChoiceNoveltyGuardTests(unittest.TestCase):
    def test_undisclosed_terms_only_lists_terms_absent_from_story(self):
        world = _world(["魔女教", "白鲸", "商路", "拉姆"])
        known = "艾米莉娅在正屋和拉姆核对名册，鲁迪准备出门。"
        terms = TavernEngine._undisclosed_chapter_terms(
            world, {"current_chapter_id": "ch_06"}, known
        )
        self.assertEqual(terms, ["魔女教", "白鲸", "商路"])
        # 正文一旦引入，就不再算「未登场」。
        terms_after = TavernEngine._undisclosed_chapter_terms(
            world,
            {"current_chapter_id": "ch_06"},
            known + "\n鲁迪听到了魔女教的消息。",
        )
        self.assertNotIn("魔女教", terms_after)

    def test_missing_chapter_config_is_a_noop(self):
        self.assertEqual(
            TavernEngine._undisclosed_chapter_terms({}, {}, "任意正文"), []
        )
        self.assertEqual(
            TavernEngine._undisclosed_chapter_terms(
                _world(["魔女教"]), {"current_chapter_id": "ch_99"}, "正文"
            ),
            [],
        )

    def test_guard_rejects_leaked_options_only(self):
        world = _world(["魔女教", "白鲸", "商路"])
        known = "众人回到小院，艾米莉娅在核对名册。"
        terms = TavernEngine._undisclosed_chapter_terms(
            world, {"current_chapter_id": "ch_06"}, known
        )
        leaked = [
            {"text": "向艾米莉娅提议：派人去梅瑟斯领打听魔女教和归途白鲸的传闻"},
            {"text": "去库珥修落脚处，探她是否愿就白鲸威胁共商行动"},
        ]
        for option in leaked:
            with self.assertRaises(ValueError):
                TavernEngine._reject_leaked_choice_terms([option], terms)
        # 用已知信息、把「去问」本身作为行动的写法必须放行。
        TavernEngine._reject_leaked_choice_terms(
            [
                {"text": "去找卫兵值班所的人打听最近贵族街有什么不对劲"},
                {"text": "向艾米莉娅提议派人沿官道走一趟探路，先准备行装"},
            ],
            terms,
        )

    def test_guard_is_silent_when_nothing_is_undisclosed(self):
        TavernEngine._reject_leaked_choice_terms(
            [{"text": "去王城客院探望菲鲁特"}], []
        )


if __name__ == "__main__":
    unittest.main()
