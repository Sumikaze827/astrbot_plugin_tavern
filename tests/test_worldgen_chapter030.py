"""用真实语料（chapter030）跑一遍完整链路。

和 ``test_worldgen_service`` 的区别：那边用一个 2 篇文档的玩具语料，验证的是
**流程语义**（审批门停不停、驳回重不重写）。这里用的是生产语料——91 篇 /
80 万字 / 1801 个块——验证的是**真实规模下不会塌**：

1. 目录排序只认 README，文件名不参与排序；
2. 分块保留精确行号，引用能逐字回验（这是反幻觉的最后一道防线，
   在玩具语料上过了不代表在真语料上过）；
3. 检索能命中真实专有名词，而不是给一堆无关块；
4. 装配出的两个包能过闸门。

模型仍然是假的——这里要证的不是"模型写得好不好"，而是"给它再大的语料，
确定性部分不会出错"。真实模型跑批由面板发起。

语料不在时整个模块跳过，CI 上不会红。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.worldgen.corpus import chunk_volume, list_volumes, load_volume
from tavern.worldgen.emit import build_npcs, build_world, run_gates, write_packages
from tavern.worldgen.models import ProposalChapter, ProposalMilestone
from tavern.worldgen.retriever import load_or_build_index

#: 生产语料根。可用 ``TAVERN_WORLDGEN_CORPUS`` 覆盖。
CORPUS_ROOT = Path(
    os.environ.get("TAVERN_WORLDGEN_CORPUS") or "D:/Project/GitbookReader/re0-ch"
)
VOLUME = "chapter030"

ATTR_KEYS = [
    "strength", "agility", "vitality", "intellect",
    "willpower", "perception", "charisma",
]
BASE_SETS = [
    [11, 9, 10, 4, 6, 6, 4], [6, 12, 9, 4, 6, 8, 5], [5, 6, 7, 11, 8, 8, 5],
    [6, 7, 7, 12, 6, 8, 4], [4, 6, 6, 10, 7, 10, 7], [4, 5, 6, 9, 8, 7, 11],
    [5, 8, 6, 6, 7, 7, 11], [4, 11, 5, 8, 6, 10, 6],
]


def _professions() -> list[dict]:
    return [
        {
            "id": f"prof_{index}",
            "name": f"职业{index}",
            "description": "擅长某些事，短板明确。",
            "base_attributes": dict(zip(ATTR_KEYS, values)),
        }
        for index, values in enumerate(BASE_SETS)
    ]


@unittest.skipUnless(
    (CORPUS_ROOT / VOLUME / "README.md").is_file(),
    f"真实语料不存在：{CORPUS_ROOT / VOLUME}",
)
class Chapter030Tests(unittest.TestCase):
    """真实语料上的确定性验证。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_volume(CORPUS_ROOT / VOLUME)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.index_dir = Path(cls._tmp.name)
        cls.retriever, _ = load_or_build_index(cls.source, cls.index_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # --- 语料 -------------------------------------------------------------

    def test_readme_is_the_only_ordering_source(self) -> None:
        """顺序**只认 README 的链接列表**，文件名不参与排序。

        chapter030 的 README 把 ``85.md``（幕間 『骑士们的疑惑』）排在 25 和 26
        之间——文件名顺序是 85，作者排的位置是第 29 篇。这正是一个把
        "按文件名排序"和"按 README 排序"区分开的活样本：如果实现里用了
        ``sorted(files)``，85 会掉到卷尾，幕間就跑到全书最后去了。
        """
        self.assertGreater(len(self.source.docs), 50)

        stems = [Path(doc.rel_path).stem for doc in self.source.docs]
        self.assertIn("85", stems)
        interlude = stems.index("85")
        # 幕間排在中间，不是卷尾——按文件名排序会让它变成最后一篇
        self.assertNotEqual(len(stems) - 1, interlude)
        self.assertLess(interlude, 40)
        # 它前面确实是 25.md
        self.assertEqual("25", stems[interlude - 1])

    def test_every_document_has_loadable_text(self) -> None:
        for doc in self.source.docs:
            self.assertTrue(doc.text.strip(), msg=doc.rel_path)
            self.assertGreater(len(doc.lines), 0, msg=doc.rel_path)

    # --- 分块与引用 -------------------------------------------------------

    def test_chunks_keep_exact_lines(self) -> None:
        """块文本必须**逐字**等于它声称的行范围——否则引用回验必然失败。

        行号是 **1-based 闭区间**（与 ``LexicalRetriever.fetch_span`` 同一约定），
        所以切片是 ``lines[start-1:end]``。
        """
        passages = chunk_volume(self.source)
        self.assertGreater(len(passages), 500)
        for passage in passages[:300]:
            doc = self.source.docs[passage.doc_index]
            expected = "\n".join(
                doc.lines[passage.line_start - 1 : passage.line_end]
            )
            self.assertEqual(expected, passage.text, msg=passage.citation)

    def test_chunk_line_numbers_agree_with_fetch_span(self) -> None:
        """分块与引用回验必须用同一套行号约定，否则回验会全体误判。

        这两处分别在 ``corpus.chunk_volume`` 和 ``retriever.fetch_span`` 里
        各写了一遍切片，任何一边改了都是静默的全量失败——这里把它钉死。
        """
        passages = chunk_volume(self.source)
        for passage in passages[:100]:
            span = self.retriever.fetch_span(
                passage.file, passage.line_start, passage.line_end
            )
            self.assertTrue(span, msg=passage.citation)
            self.assertEqual(passage.text, span, msg=passage.citation)

    def test_real_citation_verifies_and_fake_one_does_not(self) -> None:
        """引用回验在真语料上的行为——这是挡住编造出处的那道关。"""
        from tavern.worldgen.models import Citation

        passage = self.retriever.passages[len(self.retriever.passages) // 2]
        good = Citation(
            rel_path=passage.file,
            line_start=passage.line_start,
            line_end=passage.line_end,
            quote=passage.text[:60],
        )
        self.assertTrue(self.retriever.verify_citation(good).ok)

        fake = Citation(
            rel_path=passage.file,
            line_start=passage.line_start,
            line_end=passage.line_end,
            quote="这一段原文里根本不存在的句子，用来试探回验会不会放行。",
        )
        self.assertFalse(self.retriever.verify_citation(fake).ok)

    # --- 检索 -------------------------------------------------------------

    def test_retrieval_finds_real_proper_nouns(self) -> None:
        """检索要能命中真实人名，而不是返回一堆无关块。"""
        hits = self.retriever.search("拉姆 雷姆 宅邸", top_k=5)
        self.assertTrue(hits, msg="检索一条都没命中——词法检索退化了吗？")
        blob = " ".join(hit.passage.text for hit in hits)
        self.assertTrue(
            any(name in blob for name in ("拉姆", "雷姆", "昴")),
            msg=f"命中的段落里没有相关人名：{[h.passage.citation for h in hits]}",
        )

    def test_retrieval_is_ordered_and_bounded(self) -> None:
        hits = self.retriever.search("王选 骑士", top_k=4)
        self.assertLessEqual(len(hits), 4)
        scores = [hit.score for hit in hits]
        self.assertEqual(sorted(scores, reverse=True), scores)

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual([], self.retriever.search("", top_k=5))

    # --- 装配与闸门 -------------------------------------------------------

    def test_assembled_packages_pass_both_gates(self) -> None:
        """真语料上装配出的两个包必须过闸门——这是交付前提。"""
        chapters = [
            ProposalChapter(
                item_id="cp_1",
                chapter_id="ch_01_arrival",
                title="第一章：陌生的天花板",
                subtitle="醒来",
                min_turns=1,
                max_turns=12,
                current_objective="确认自己身在何处并找到愿意回应的人",
                pacing_directive="若无人回应则局势逐步收紧，但不得封锁退路",
                hook_pool=["走廊尽头的脚步声"],
                key_npcs=[{"ref": "npc_ram", "role": "冷面女仆"}],
                milestones=[
                    ProposalMilestone(
                        "cp_m1", "m_01_01_trust",
                        "队伍取得可行动的确切情报并已互相传达",
                        evidence_required=[
                            {"type": "clue_keyword_any", "match": ["确认位置"]}
                        ],
                    )
                ],
            ),
            ProposalChapter(
                item_id="cp_2",
                chapter_id="ch_02_mansion",
                title="第二章：宅邸",
                subtitle="四天",
                min_turns=1,
                max_turns=15,
                current_objective="在宅邸里活过第一天",
                pacing_directive="异常逐日加重",
                hook_pool=["结界薄弱点"],
                key_npcs=[{"ref": "npc_ram", "role": "陪同"}],
                milestones=[
                    ProposalMilestone(
                        "cp_m2", "m_02_01_signal",
                        "确认宅邸存在一处无法解释的异常并记录",
                        evidence_required=[
                            {"type": "clue_keyword_any", "match": ["异常记录"]}
                        ],
                    )
                ],
            ),
        ]
        world = build_world(
            slug="rezero-chapter030-e2e",
            name="从零开始的异世界生活 第三十章",
            description="由世界包生成 agent 从 chapter030 装配，用于端到端验证。",
            chapters=chapters,
            prose={
                "opening_scene": "你在陌生的房间里醒来，走廊望不到头。",
                "system_prompt": "本世界的稳定规律：不存在死亡回归。",
                "opening_choices": [
                    {"key": "A", "text": "起身查看房间", "risk": "safe"},
                    {"key": "B", "text": "敲门问有没有人", "risk": "safe"},
                    {"key": "C", "text": "检查随身物品", "risk": "safe"},
                    {"key": "D", "text": "全队一起下楼", "risk": "safe", "collective": True},
                ],
            },
            professions=_professions(),
        )
        npcs = build_npcs(
            slug="rezero-chapter030-e2e",
            npcs=[
                {
                    "slug": "npc_ram",
                    "name": "拉姆",
                    "identity": "罗兹瓦尔宅邸的双胞胎女仆之一",
                    "appearance": "粉色短发，前刘海盖住右眼",
                    "personality": "直率、嘴硬",
                    "public_background": "宅邸的女仆",
                    "location": "主楼走廊",
                    "capabilities": ["清扫", "对外交涉"],
                    "limitations": ["厨艺弱于妹妹"],
                    "prompt": "她想要维持宅邸的秩序。",
                }
            ],
        )
        gates = run_gates(world, npcs)
        self.assertEqual(0, gates["lint"]["errors"], msg=gates["lint"]["issues"])
        self.assertTrue(gates["preflight"].get("compatible"), msg=gates["preflight"])

    def test_write_packages_produces_two_files(self) -> None:
        """**默认产出两个包**：世界包 + NPC 包。"""
        world = build_world(
            slug="rezero-e2e-write",
            name="写入验证",
            description="验证默认产出两个文件。",
            chapters=[
                ProposalChapter(
                    item_id="c", chapter_id="ch_01_a", title="t", min_turns=1,
                    max_turns=5, current_objective="o", pacing_directive="p",
                    milestones=[
                        ProposalMilestone("m", "m_01_01_x", "取得确切情报并已传达")
                    ],
                )
            ],
            prose={"opening_scene": "s", "system_prompt": "r"},
            professions=_professions(),
        )
        npcs = build_npcs(
            slug="rezero-e2e-write",
            npcs=[{"slug": "npc_ram", "name": "拉姆", "identity": "女仆"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_packages(
                output_dir=tmp, slug="rezero-e2e-write", world=world, npcs=npcs
            )
            self.assertTrue(Path(paths["world"]).is_file())
            self.assertTrue(Path(paths["npcs"]).is_file())
            payload = json.loads(Path(paths["npcs"]).read_text(encoding="utf-8"))
            self.assertEqual(2, payload["template_version"])
            self.assertEqual("rezero-e2e-write", payload["world_slug"])


@unittest.skipUnless(
    (CORPUS_ROOT / VOLUME / "README.md").is_file(),
    f"真实语料不存在：{CORPUS_ROOT / VOLUME}",
)
class VolumeListingTests(unittest.TestCase):
    def test_lists_sibling_volumes(self) -> None:
        volumes = list_volumes(CORPUS_ROOT)
        # 键名是 slug / doc_count——面板直接吃这个结构，钉死免得两边改歪。
        refs = {item["slug"] for item in volumes}
        self.assertIn(VOLUME, refs)
        entry = next(item for item in volumes if item["slug"] == VOLUME)
        self.assertGreater(int(entry["doc_count"]), 50)
        self.assertTrue(entry["title"])
        self.assertTrue(Path(str(entry["directory"])).is_dir())


if __name__ == "__main__":
    unittest.main()
