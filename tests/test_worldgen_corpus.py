"""语料解析、检索与续卷继承的数据完整性测试。

这三块有一个共同点：**出错时不会报错，只会静默给出错误结果**。

- 切块行号一旦漂移，生成内容里的每一个 ``文件:行号`` 引用都是错的，
  而反省机制正是靠这些引用证伪幻觉的。
- 检索后端一旦静默降级，反省会拿到无关段落，然后"确认"一堆幻觉。
- 继承一旦取错字段（例如取滑动窗口的 ``facts``），前 2/3 的剧情会无声蒸发。

因此这里的断言都针对**可证伪的具体事实**，而不是"跑通不报错"。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tavern.constants import SESSION_PREPARING
from tavern.database import TavernDatabase
from tavern.worldgen.continuity import (
    Confidence,
    derive_brief,
    inject_brief,
    render_brief_markdown,
)
from tavern.worldgen.service import derive_brief_async_safe
from tavern.worldgen.corpus import chunk_volume, load_volume
from tavern.worldgen.models import Citation
from tavern.worldgen.retriever import (
    DenseRetriever,
    Hit,
    LexicalRetriever,
    tokenize,
)

README = """## 第二章　『测试之卷』

- [序章　『开端』](00.md)
- [01　『中间』](01.md)
- [后记　『插图』](99.md)
"""

DOC0 = """# 『开端』

第一段：罗兹瓦尔宅邸的双胞胎女仆拉姆与雷姆站在走廊尽头。

第二段：她们手里握着契约书，等着某人开口。
"""

DOC1 = """# 『中间』

阿拉姆村的孩子们在村口玩耍，幼犬从第二天起对所有外来者龇牙。
"""


def _write_corpus(root: Path) -> Path:
    volume = root / "chapter020"
    volume.mkdir(parents=True, exist_ok=True)
    (volume / "README.md").write_text(README, encoding="utf-8")
    (volume / "00.md").write_text(DOC0, encoding="utf-8")
    (volume / "01.md").write_text(DOC1, encoding="utf-8")
    (volume / "99.md").write_text("# 『后记』\n\n插图页。\n", encoding="utf-8")
    return volume


class CorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.volume_dir = _write_corpus(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_readme_is_the_ordering_source(self) -> None:
        source = load_volume(self.volume_dir)
        self.assertEqual("第二章　『测试之卷』", source.arc_title)
        # 99.md（后记）默认排除，不参与改编
        self.assertEqual(["00.md", "01.md"], [d.rel_path for d in source.docs])
        self.assertEqual("『开端』", source.docs[0].title)

    def test_chunk_text_lives_inside_its_claimed_span(self) -> None:
        """切块文本必须是其声称行区间的子串——引用可信的前提。

        这是 ``verify_citation`` 的实际契约：拿 ``lines[start-1:end]`` 去包含引用文本。
        未切分的块应当**逐字相等**（含段间空行），超长段落切出的片则是子串。
        """
        source = load_volume(self.volume_dir)
        passages = chunk_volume(source)
        self.assertTrue(passages)

        by_file = {doc.rel_path: doc.text.splitlines() for doc in source.docs}

        for passage in passages:
            lines = by_file[passage.file]
            span = "\n".join(lines[passage.line_start - 1 : passage.line_end])
            self.assertIn(
                passage.text,
                span,
                msg=f"{passage.citation} 的切块文本不在其声称的行区间内",
            )

    def test_span_bounds_are_tight(self) -> None:
        """行区间不得含首尾空行——否则引用会带上无关的空白，且说明存在偏移。"""
        source = load_volume(self.volume_dir)
        by_file = {doc.rel_path: doc.text.splitlines() for doc in source.docs}
        for passage in chunk_volume(source):
            lines = by_file[passage.file]
            head = lines[passage.line_start - 1]
            tail = lines[passage.line_end - 1]
            self.assertTrue(head.strip(), msg=f"{passage.citation} 起始行是空行")
            self.assertTrue(tail.strip(), msg=f"{passage.citation} 结束行是空行")

    def test_unsplit_chunk_is_verbatim(self) -> None:
        """普通块必须与原文逐字一致，这样引用才能原样回验。"""
        source = load_volume(self.volume_dir)
        by_file = {doc.rel_path: doc.text.splitlines() for doc in source.docs}
        checked = 0
        for passage in chunk_volume(source):
            lines = by_file[passage.file]
            span = "\n".join(lines[passage.line_start - 1 : passage.line_end]).strip("\n")
            if passage.text == span:
                checked += 1
        self.assertGreater(checked, 0, msg="没有任何块与原文逐字一致，切块可能已失去保真性")

    def test_citation_is_one_based(self) -> None:
        """行号是 1-based：第一块必须从第 1 行开始，且该行是正文而非空行。"""
        source = load_volume(self.volume_dir)
        passages = chunk_volume(source)
        first = passages[0]
        self.assertEqual(1, first.line_start)
        self.assertTrue(first.citation.startswith("00.md:1"))
        raw = source.docs[0].text.splitlines()
        self.assertTrue(raw[first.line_start - 1].strip())

    def test_include_extras_flag(self) -> None:
        source = load_volume(self.volume_dir, include_extras=True)
        self.assertIn("99.md", [d.rel_path for d in source.docs])

    def test_missing_readme_raises(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(Exception):
            load_volume(empty)


class RetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.volume_dir = _write_corpus(self.root)
        self.retriever = LexicalRetriever.build(load_volume(self.volume_dir))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_two_char_cjk_name_is_retrievable(self) -> None:
        """两字中文名必须能检索到。

        这是词法检索最容易静默失效的地方：SQLite FTS5 的 unicode61 分词器不切中文，
        会把整句当成一个 token，导致 ``拉姆`` 零命中；trigram 又对 <3 字查询零命中。
        本实现走 jieba 词元 + BM25，不存在该问题——本测试锁定这一点。
        """
        hits = self.retriever.search("拉姆", top_k=5)
        self.assertTrue(hits, msg="两字人名检索零命中，词法检索已失效")
        self.assertTrue(any("拉姆" in h.passage.text for h in hits))

    def test_single_char_name_is_retrievable(self) -> None:
        hits = self.retriever.search("昴", top_k=5)
        # 语料里没有"昴"，但检索不应崩溃；有则必须命中
        for hit in hits:
            self.assertIn("昴", hit.passage.text)

    def test_multi_token_query_uses_or_semantics(self) -> None:
        """部分匹配也应有结果——FTS5 的隐式 AND 会在这里漏召回。"""
        hits = self.retriever.search("契约书 幼犬", top_k=5)
        self.assertTrue(hits)
        combined = " ".join(h.passage.text for h in hits)
        self.assertTrue("契约书" in combined or "幼犬" in combined)

    def test_hits_carry_verifiable_citations(self) -> None:
        """检索命中携带的出处必须能用原文行区间回验。"""
        hits = self.retriever.search("双胞胎女仆", top_k=3)
        self.assertTrue(hits)
        source = load_volume(self.volume_dir)
        lines_by_file = {d.rel_path: d.text.splitlines() for d in source.docs}
        for hit in hits:
            lines = lines_by_file[hit.passage.file]
            span = "\n".join(lines[hit.passage.line_start - 1 : hit.passage.line_end])
            self.assertIn(hit.passage.text, span)

    def test_expand_returns_neighbours(self) -> None:
        hits = self.retriever.search("契约书", top_k=1)
        self.assertTrue(hits)
        expanded = self.retriever.expand(hits[0].passage, radius=1)
        self.assertGreaterEqual(len(expanded), 1)

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual([], self.retriever.search("", top_k=3))
        self.assertEqual([], self.retriever.search("的了是", top_k=3))

    def test_dense_backend_fails_loudly(self) -> None:
        """占位后端必须显式报错，绝不能静默降级成空结果。"""
        with self.assertRaises(NotImplementedError):
            DenseRetriever()

    def test_stats_shape(self) -> None:
        stats = self.retriever.stats()
        self.assertEqual("lexical-bm25", stats["backend"])
        self.assertGreater(int(stats["passages"]), 0)


class CitationVerificationTests(unittest.TestCase):
    """引用回验是整个反幻觉设计里唯一的硬证据，必须挡住编造。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.volume_dir = _write_corpus(self.root)
        self.retriever = LexicalRetriever.build(load_volume(self.volume_dir))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_real_quote_verifies(self) -> None:
        span = self.retriever.fetch_span("00.md", 3, 3)
        self.assertTrue(span.strip())
        check = self.retriever.verify_citation(Citation("00.md", 3, 3, span))
        self.assertTrue(check.ok, msg=check.reason)

    def test_fabricated_quote_with_correct_lines_is_rejected(self) -> None:
        """最关键的一例：行号完全正确，但引文是编的。

        模型最常见的幻觉形态就是"行号看着对、内容自己编"。只要这一条挡得住，
        反省机制就不会拿假证据推翻真实内容。
        """
        check = self.retriever.verify_citation(
            Citation("00.md", 3, 3, "拉姆当场拔刀砍断了契约书")
        )
        self.assertFalse(check.ok)
        self.assertIn("未出现", check.reason)

    def test_wrong_line_range_is_rejected(self) -> None:
        real = self.retriever.fetch_span("00.md", 3, 3)
        check = self.retriever.verify_citation(Citation("01.md", 1, 2, real))
        self.assertFalse(check.ok)

    def test_missing_file_and_out_of_range_are_rejected(self) -> None:
        for citation in (
            Citation("nope.md", 1, 2, "任意"),
            Citation("00.md", 99999, 100000, "任意"),
        ):
            self.assertFalse(self.retriever.verify_citation(citation).ok)

    def test_empty_quote_is_rejected(self) -> None:
        self.assertFalse(self.retriever.verify_citation(Citation("00.md", 3, 3, "")).ok)

    def test_whitespace_differences_are_tolerated(self) -> None:
        """引用跨段时模型常把空行写丢，不应因此判失败。"""
        span = self.retriever.fetch_span("00.md", 3, 5)
        squashed = span.replace("\n\n", "\n")
        self.assertNotEqual(span, squashed)
        self.assertTrue(self.retriever.verify_citation(Citation("00.md", 3, 5, squashed)).ok)

    def test_fetch_span_returns_verbatim_source(self) -> None:
        source = load_volume(self.volume_dir)
        lines = source.docs[0].text.splitlines()
        self.assertEqual("\n".join(lines[2:5]), self.retriever.fetch_span("00.md", 3, 5))


# --- 继承 -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE worlds (id TEXT PRIMARY KEY, slug TEXT, name TEXT, rules_json TEXT);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, world_id TEXT, state TEXT, turn_no INTEGER,
    world_state_json TEXT, instance_slug TEXT, instance_name TEXT
);
CREATE TABLE participants (
    id TEXT PRIMARY KEY, session_id TEXT, player_id TEXT, display_name TEXT,
    character_name TEXT, character_code TEXT, participation_status TEXT, exit_reason TEXT
);
CREATE TABLE story_ledger (
    id TEXT PRIMARY KEY, session_id TEXT, stable_key TEXT, kind TEXT, title TEXT,
    description TEXT, status TEXT, visibility TEXT, source_event_id TEXT, completed_event_id TEXT
);
CREATE TABLE session_characters (
    id TEXT PRIMARY KEY, session_id TEXT, stable_key TEXT, name TEXT,
    lifecycle_status TEXT, persistent INTEGER, first_turn INTEGER, last_turn INTEGER
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY, id TEXT, session_id TEXT, turn_no INTEGER, role TEXT,
    actor_id TEXT, actor_name TEXT, content TEXT, meta_json TEXT
);
"""


def _seed(connection: sqlite3.Connection) -> None:
    # rules_json 存的是 **rules 对象本身**（不是 {rules: {...}} 的包装）——
    # 真库就这么存的，认错形状会静默读成"世界没声明任何里程碑"。
    rules = {
        "progress": {
            "chapters": [
                {
                    "id": "ch_01",
                    "milestones": [
                        {"id": "m_01_01_trust", "label": "取得信任"},
                        # 这一条账本里没有，必须落进「未达成」
                        {"id": "m_03_01_unreached", "label": "抵达终局"},
                    ],
                }
            ]
        }
    }
    connection.execute(
        "INSERT INTO worlds VALUES ('w1','test-world','测试世界',?)",
        (json.dumps(rules, ensure_ascii=False),),
    )
    # facts 刻意塞满 200 条，模拟被滑动窗口截断的真实状态
    state = {
        "facts": [f"事实{i}" for i in range(200)],
        "relationships": {"甲→乙": {"信任": 3}},
        "inventory": {"participant_1": {"回城木牌": 1}},
    }
    connection.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        ("sess_1", "w1", "finished", 120, json.dumps(state, ensure_ascii=False), "s", "测试副本"),
    )
    connection.execute(
        "INSERT INTO participants VALUES (?,?,?,?,?,?,?,?)",
        ("p1", "sess_1", "pl1", "玩家甲", "甲", "甲", "active", ""),
    )
    connection.execute(
        "INSERT INTO participants VALUES (?,?,?,?,?,?,?,?)",
        ("p2", "sess_1", "pl2", "玩家乙", "乙", "乙", "retired", "lethal_check_death"),
    )
    connection.execute(
        "INSERT INTO story_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l1", "sess_1", "m_01_01_trust", "milestone", "取得信任", "", "completed", "public", "e1", "e1"),
    )
    connection.execute(
        "INSERT INTO story_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l2", "sess_1", "m_02_01_status", "milestone", "确立身份", "", "pending", "public", "", ""),
    )
    connection.execute(
        "INSERT INTO story_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l3", "sess_1", "clue_badge", "clue", "徽章的来历", "", "completed", "public", "e2", "e2"),
    )
    connection.execute(
        "INSERT INTO story_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l4", "sess_1", "secret_x", "clue", "隐藏线索", "", "completed", "private", "e3", "e3"),
    )
    # 生产库里非里程碑账本 1312 条 active / 22 条 completed——线索记下来就是 active，
    # 只认 completed 等于把玩家一路攒的认知全丢了。
    connection.execute(
        "INSERT INTO story_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l5", "sess_1", "clue_ram", "clue", "拉姆透露的异常线索", "", "active", "public", "e1", ""),
    )
    connection.execute(
        "INSERT INTO story_ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l6", "sess_1", "clue_dead", "clue", "已作废的线索", "", "archived", "public", "e1", ""),
    )
    connection.execute(
        "INSERT INTO session_characters VALUES (?,?,?,?,?,?,?,?)",
        ("sc1", "sess_1", "npc_ram", "拉姆", "active", 1, 10, 100),
    )
    connection.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (1, "e1", "sess_1", 5, "player", "pl1", "玩家甲", "我救下了拉姆并把她带出宅邸", "{}"),
    )
    connection.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (2, "e2", "sess_1", 9, "narrator", "", "叙事", "拉姆脱离了险境。", "{}"),
    )
    connection.commit()


class ContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        _seed(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_death_comes_from_exit_reason(self) -> None:
        """死亡只认 participants.exit_reason。

        session_characters.lifecycle_status 在生产库里全是 'active'，
        玩家死亡从不落那张表——依赖它会得出"无人阵亡"的错误结论。
        """
        brief = derive_brief(self.connection, "sess_1")
        self.assertEqual(1, len(brief.dead_pcs))
        self.assertEqual("乙", brief.dead_pcs[0].character_name)
        self.assertEqual("阵亡", brief.dead_pcs[0].occupancy)
        # 阵亡本身必须成为一条 CONFIRMED 影响
        death_impacts = [i for i in brief.player_impacts if i.kind == "pc_death"]
        self.assertEqual(1, len(death_impacts))
        self.assertIs(Confidence.CONFIRMED, death_impacts[0].confidence)

    def test_facts_window_is_not_used_as_source(self) -> None:
        """world_state_json.facts 是 200 条滑动窗口，绝不能当作事实来源。"""
        brief = derive_brief(self.connection, "sess_1")
        carried = json.dumps(brief.carried_facts, ensure_ascii=False)
        self.assertNotIn("事实0", carried)
        self.assertNotIn("事实199", carried)
        # 但应给出该字段已被截断的警告
        self.assertTrue(any("滑动窗口" in w for w in brief.warnings))

    def test_milestones_split_by_completion(self) -> None:
        brief = derive_brief(
            self.connection, "sess_1", declared_milestones=["m_01_01_trust", "m_02_01_status"]
        )
        self.assertEqual(["m_01_01_trust"], brief.completed_milestones)
        self.assertEqual(["m_02_01_status"], brief.uncompleted_milestones)

    def test_private_ledger_entries_are_excluded(self) -> None:
        brief = derive_brief(self.connection, "sess_1")
        keys = [f["stable_key"] for f in brief.carried_facts]
        self.assertIn("clue_badge", keys)
        self.assertNotIn("secret_x", keys)

    def test_relationships_and_inventory_are_carried(self) -> None:
        brief = derive_brief(self.connection, "sess_1")
        self.assertTrue(brief.relationship_deltas)
        self.assertTrue(brief.lost_or_spent_items)

    def test_active_ledger_entries_are_carried(self) -> None:
        """``active`` 的公开线索必须继承。

        生产库实测：非里程碑账本行 ``active`` 1312 条、``completed`` 仅 22 条。
        线索一经记录就是 ``active``，一直立到本子结束。早先只认 ``completed``，
        某档 59 条线索里只继承了 1 条——玩家一路攒下的认知几乎全丢。
        """
        brief = derive_brief(self.connection, "sess_1")
        keys = [f["stable_key"] for f in brief.carried_facts]
        self.assertIn("clue_ram", keys, "active 线索被丢掉了")
        self.assertIn("clue_badge", keys, "completed 线索仍应继承")

    def test_archived_ledger_entries_are_not_carried(self) -> None:
        """作废的线索不能继承——放宽状态过滤不等于什么都收。"""
        brief = derive_brief(self.connection, "sess_1")
        keys = [f["stable_key"] for f in brief.carried_facts]
        self.assertNotIn("clue_dead", keys)

    def test_declared_milestones_are_read_from_the_world_package(self) -> None:
        """不传 ``declared_milestones`` 也要能算出「未达成」——自己去世界包读。

        原先只有显式传参才算得出未达成项，而两个真实调用点（面板路由、
        ``_phase_inherit``）都不传，于是「未达成里程碑（可续用）」永远为空——
        那恰恰是续卷最该知道的信息。
        """
        brief = derive_brief(self.connection, "sess_1")
        self.assertEqual(["m_01_01_trust"], brief.completed_milestones)
        self.assertEqual(["m_03_01_unreached"], brief.uncompleted_milestones)

    def test_milestone_labels_cover_both_sides(self) -> None:
        """已完成的标签来自账本 title，未完成的来自世界包声明。"""
        brief = derive_brief(self.connection, "sess_1")
        self.assertEqual("取得信任", brief.milestone_labels["m_01_01_trust"])
        self.assertEqual("抵达终局", brief.milestone_labels["m_03_01_unreached"])

    def test_explicit_declared_milestones_still_win(self) -> None:
        """显式传参优先——测试与未来调用方都靠这条。"""
        brief = derive_brief(
            self.connection, "sess_1", declared_milestones=["m_09_09_only"]
        )
        self.assertEqual(["m_09_09_only"], brief.uncompleted_milestones)

    def test_world_without_rules_json_degrades_quietly(self) -> None:
        """老库/精简库没有 ``rules_json`` 列也只是少一项，不能抛。"""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA.replace(", rules_json TEXT", ""))
        connection.execute("INSERT INTO worlds VALUES ('w1','old','老世界')")
        connection.execute(
            "INSERT INTO sessions VALUES ('s1','w1','finished',1,'{}','s','老副本')"
        )
        brief = derive_brief(connection, "s1")
        connection.close()
        self.assertEqual([], brief.uncompleted_milestones)
        self.assertEqual([], brief.completed_milestones)

    def test_missing_session_raises(self) -> None:
        with self.assertRaises(ValueError):
            derive_brief(self.connection, "nope")

    def test_markdown_renders(self) -> None:
        markdown = render_brief_markdown(derive_brief(self.connection, "sess_1"))
        self.assertIn("续卷简报", markdown)
        self.assertIn("玩家乙", markdown)
        # 里程碑要渲染成人话，不能是一串 m_01_01_trust
        self.assertIn("取得信任", markdown)
        self.assertIn("抵达终局", markdown)


class DeriveBriefAsyncTests(unittest.IsolatedAsyncioTestCase):
    """``derive_brief_async_safe`` 必须真的**打开**连接再交给 ``derive_brief``。

    回归：它曾把 ``database._connect`` 这个**工厂**当成连接传进去，
    ``derive_brief`` 里第一句 ``connection.execute(...)`` 就 AttributeError，
    又被这个函数自己的 ``except`` 兜成"派生失败"——**继承功能从来没跑通过**，
    每次生成都静默拿到一份空简报、按原剧情走。面板上只看到一行
    「⚠️ 派生失败：'function' object has no attribute 'execute'」。

    为什么单元测试没拦住：路由测试用的假 database 连 ``_connect`` / ``_run``
    都没有，那条路径压根没被走到。所以这里必须对着**真库**跑。
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp_dir.name))
        self.world = await self.database.save_world(
            {
                "slug": "carry-over",
                "name": "要继承的世界",
                "description": "测试世界",
                "world_schema_version": 2,
                "system_prompt": "严格遵守因果。",
                "opening_scene": "新场景。",
                "rules": {"resolution": "d20"},
                "initial_state": {
                    "location": "新地点",
                    "facts": [],
                    "inventory": {},
                    "relationships": {},
                },
            },
            "admin-1",
        )
        self.session = await self.database.ensure_session(
            "qq", "group-carry", "qq:group-carry", "carry-over", "admin-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_brief_is_derived_not_swallowed(self) -> None:
        # 报名要在准备大厅里进行（直接开会话是 running，不接受加入）。
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        await self.database.reserve_participant(
            self.session["id"], "user-1", "甲"
        )
        brief = await derive_brief_async_safe(self.database, self.session["id"])

        failures = [w for w in brief.warnings if "派生失败" in w]
        self.assertEqual([], failures, msg=f"派生又失败了：{failures}")
        self.assertEqual(self.session["id"], brief.source_session_id)
        # 真的读到了库里的东西，而不是返回一份空壳
        self.assertEqual(1, len(brief.roster))
        self.assertEqual("甲", brief.roster[0].display_name)
        self.assertFalse(brief.is_empty)

    async def test_unknown_session_is_reported_not_raised(self) -> None:
        """取不到就如实报在 warnings 里，不能让整个生成作业崩掉。"""
        brief = await derive_brief_async_safe(self.database, "no-such-session")
        self.assertTrue(any("派生失败" in w for w in brief.warnings))
        self.assertTrue(brief.is_empty)


class BriefPanelContractTests(unittest.TestCase):
    """面板的继承预览只能读 ``to_dict()`` 真的发出来的键。

    这个 bug 已经咬过两次了：``list_sessions`` 返回 ``id``/``instance_name``/
    ``state``，面板照抄 ``session_id``/``title`` → 下拉永远空；继承预览读
    ``deaths``/``relationships``/``inventory``/``completed``，而简报发的是
    ``dead_pcs``/``relationship_deltas``/``lost_or_spent_items``/
    ``completed_milestones`` → 「没有可继承的具体条目」。

    两次都不会报错，只会静静显示"没有"。所以这里把契约钉住：
    ``worldgenBriefMarkup`` 里读的每个 ``brief.xxx`` 都必须是真键。
    """

    @classmethod
    def setUpClass(cls) -> None:
        plugin_root = Path(__file__).resolve().parent.parent
        source = (plugin_root / "pages" / "console" / "app.js").read_text("utf-8")
        start = source.index("function worldgenBriefMarkup()")
        end = source.index("\nfunction ", start + 1)
        cls.markup = source[start:end]

    def test_markup_reads_only_real_brief_keys(self) -> None:
        import re

        from tavern.worldgen.models import ContinuityBrief

        real = set(ContinuityBrief().to_dict())
        read = set(re.findall(r"\bbrief\.([A-Za-z_][A-Za-z0-9_]*)", self.markup))
        self.assertTrue(read, "一个字段都没解析出来，说明取函数体的方式失效了")
        unknown = sorted(read - real)
        self.assertEqual(
            [],
            unknown,
            f"面板读了简报不存在的字段 {unknown}；简报实际有 {sorted(real)}",
        )

    def test_markup_shows_the_sections_that_actually_carry_content(self) -> None:
        """这四个字段在生产库里是**有数据**的，面板必须提到它们。

        只断言"键合法"不够——把整段换成空串也能过。所以再钉一次"展示面覆盖了
        真正装货的那几个字段"。
        """
        for field in (
            "roster",
            "completed_milestones",
            "uncompleted_milestones",
            "carried_facts",
            "milestone_labels",
        ):
            self.assertIn(
                f"brief.{field}", self.markup, msg=f"继承预览没展示 {field}"
            )


class InjectionTests(unittest.TestCase):
    def _world(self) -> dict:
        return {
            "slug": "next",
            "system_prompt": "原系统提示。",
            "initial_state": {"location": "宅邸", "facts": ["既有一件事"]},
            "rules": {
                "progress": {
                    "chapters": [
                        {
                            "id": "ch_01_a",
                            "key_npcs": [
                                {"ref": "npc_ram", "role": "女仆", "state": {"location": "宅邸"}}
                            ],
                            "milestones": [],
                        }
                    ]
                }
            },
        }

    def _brief(self) -> object:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _seed(connection)
        brief = derive_brief(connection, "sess_1")
        connection.close()
        return brief

    def test_suspected_impacts_are_not_injected(self) -> None:
        """安全性质：未经确认的推断**不得**进入世界包。

        正则扫出来的 saved_npc 只是候选，若直接注进去，叙事模型会把一个
        可能不存在的剧情当成既定事实，且玩家无从纠正。
        """
        brief = self._brief()
        world, conflicts = inject_brief(brief, self._world(), npc_names={"npc_ram": "拉姆"})
        facts = world["initial_state"]["facts"]

        # 已确认的阵亡要注入
        self.assertTrue(any("已经阵亡" in f for f in facts), msg=facts)
        # 未确认的「救下拉姆」不得注入
        self.assertFalse(any("拉姆" in f for f in facts), msg=facts)
        self.assertEqual([], conflicts)
        self.assertTrue(brief.needs_review)

    def test_injection_touches_all_three_targets(self) -> None:
        brief = self._brief()
        # 操作者在面板上把「救下拉姆」确认为事实
        for impact in brief.player_impacts:
            if impact.kind == "saved_npc":
                impact.confidence = Confidence.CONFIRMED

        world, conflicts = inject_brief(brief, self._world(), npc_names={"npc_ram": "拉姆"})

        # system_prompt：只加规则，保留原文
        self.assertIn("续卷前提", world["system_prompt"])
        self.assertIn("原系统提示。", world["system_prompt"])
        self.assertIn("不得重演", world["system_prompt"])

        # initial_state：拿到硬事实，且写成「世界状态」而非历史事件
        facts = world["initial_state"]["facts"]
        self.assertIn("既有一件事", facts)
        self.assertTrue(any("拉姆" in f for f in facts), msg=facts)
        self.assertFalse(
            any("第 5 回合" in f or "玩家" in f for f in facts),
            msg=f"事实被写成了历史事件叙述，应写成世界状态：{facts}",
        )

        # key_npcs[].state：冲突被上报且章节被打标
        self.assertTrue(conflicts, msg="拉姆已被救出，却仍钉死 location=宅邸，应报冲突")
        self.assertTrue(world["rules"]["progress"]["chapters"][0]["inheritance_conflict"])

    def test_saved_npc_subject_is_not_greedy(self) -> None:
        """宾语抽取必须截在名字处，不能把后续动词一起吞掉。"""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _seed(connection)
        brief = derive_brief(connection, "sess_1")
        subjects = {i.subject for i in brief.player_impacts if i.kind == "saved_npc"}
        self.assertIn("拉姆", subjects, msg=f"实际抽到：{subjects}")
        connection.close()

    def test_conflict_missed_without_npc_names(self) -> None:
        """不传 npc_names 就查不出冲突——记录这个已知限制，避免误以为已覆盖。"""
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _seed(connection)
        brief = derive_brief(connection, "sess_1")
        _, conflicts = inject_brief(brief, self._world())
        self.assertEqual([], conflicts)
        connection.close()

    def test_injection_does_not_mutate_input(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        _seed(connection)
        brief = derive_brief(connection, "sess_1")
        original = self._world()
        snapshot = json.dumps(original, ensure_ascii=False, sort_keys=True)
        inject_brief(brief, original)
        self.assertEqual(snapshot, json.dumps(original, ensure_ascii=False, sort_keys=True))
        connection.close()

    def test_empty_brief_is_a_noop(self) -> None:
        from tavern.worldgen.models import ContinuityBrief

        world = self._world()
        snapshot = json.dumps(world, ensure_ascii=False, sort_keys=True)
        updated, conflicts = inject_brief(ContinuityBrief(), world)
        self.assertEqual([], conflicts)
        self.assertEqual(snapshot, json.dumps(updated, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
