"""世界包生成的控制台端点测试。

为什么值得单独测：这些 handler 是**唯一**从浏览器打到生成服务的通路。
``_register_routes`` 里一张表把路径绑到方法名上，绑错了不会报错——只会在
某个路径上返回 500 或者干脆不响应（``extensions`` 路由就踩过这个坑：
同名属性把注册表对象当成了 HTTP handler）。所以这里既验证"绑定是对的"，
也验证每个 handler 的鉴权、入参校验和危险输入拦截。

实现方式与 ``test_plugin_shell`` 一致：装一套 ``astrbot`` 桩，直接调 handler。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ATTR_KEYS = [
    "strength", "agility", "vitality", "intellect",
    "willpower", "perception", "charisma",
]
BASE_SETS = [
    [11, 9, 10, 4, 6, 6, 4], [6, 12, 9, 4, 6, 8, 5], [5, 6, 7, 11, 8, 8, 5],
    [6, 7, 7, 12, 6, 8, 4], [4, 6, 6, 10, 7, 10, 7], [4, 5, 6, 9, 8, 7, 11],
    [5, 8, 6, 6, 7, 7, 11], [4, 11, 5, 8, 6, 10, 6],
]

README = "## 第二章　『测试之卷』\n\n- [01　『开端』](01.md)\n"
DOC1 = "# 『开端』\n\n拉姆与雷姆站在走廊尽头，她们是罗兹瓦尔宅邸的双胞胎女仆。\n"

#: 插件路由统一挂在 ``/{plugin_name}/`` 前缀下。
ROUTE_PREFIX = "/astrbot_plugin_tavern/"

#: 控制台注册的 worldgen 路由全集（不含前缀）。掉一条就会在下面被断言出来。
EXPECTED_SUFFIXES = (
    "corpus",
    "volumes",
    "corpus-volume",
    "index",
    "sessions",
    "continuity",
    "jobs",
    "job",
    "artifact",
    "create",
    "approve",
    "resume",
    "cancel",
    "delete",
    "lint",
)
EXPECTED_ROUTES = {f"{ROUTE_PREFIX}worldgen/{s}" for s in EXPECTED_SUFFIXES}


def _install_astrbot_stubs() -> None:
    """装最小可用的 astrbot 桩，让 web_console 能 import 进来。"""
    if "astrbot" in sys.modules:
        return

    def response(value=None, *args, **kwargs):
        return value

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    web = types.ModuleType("astrbot.api.web")
    web.PluginUploadFile = type("PluginUploadFile", (), {})
    web.error_response = response
    web.file_response = response
    web.json_response = response
    web.stream_response = response
    web.request = SimpleNamespace(username=None)

    astrbot.api = api
    api.web = web
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.web", web)


_install_astrbot_stubs()

from tavern.config import TavernConfig  # noqa: E402
from tavern.web_console import TavernWebConsole  # noqa: E402
from tavern.worldgen.service import WorldgenService  # noqa: E402
from tavern.worldgen.store import JobStore  # noqa: E402


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_json(self, *, system_prompt, prompt, validate=None, request_type="", **kwargs):
        self.calls.append(request_type)
        payload = _RESPONSES[request_type]()
        problems = list(validate(payload)) if validate else []
        return payload, SimpleNamespace(
            provider_id="fake", attempts=1, input_chars=10, output_chars=10, usage={}
        ), problems


def _timeline() -> dict:
    """一条线，一条必现情节。缝合逻辑本身的测试见 test_worldgen_timeline。"""
    return {
        "structure": "linear",
        "note": "线性",
        "segments": [
            {"segment_id": "tl_01", "label": "全卷", "kind": "main",
             "source_files": ["01.md"], "summary": "开端", "ends_with_reset": False,
             "key_beats": ["与双子初见"]},
        ],
        "merged": [
            {"beat_id": "b01", "label": "与双子初见", "from_segments": ["tl_01"],
             "depends_on_reset": False, "merge_action": "keep"},
        ],
        "dropped": [],
        "coherence_notes": [],
    }


def _arc_map() -> dict:
    return {
        "arc_title": "第二章　『测试之卷』",
        "premise": "一行人住进宅邸。",
        "candidate_chapters": [
            {"order": 1, "title": "陌生的天花板", "source_files": ["01.md"],
             "beats": ["b01"], "why": "开场"}
        ],
        "unadapted": [],
    }


def _chapter() -> dict:
    return {
        "chapter": {
            "title": "陌生的天花板",
            "subtitle": "测试",
            "source_files": ["01.md"],
            "beats": ["b01"],
            "rationale": "测试",
            "min_turns": 1,
            "max_turns": 10,
            "current_objective": "确认自己身在何处并找到愿意回应的人",
            "pacing_directive": "若无人回应则局势逐步收紧",
            "hook_pool": ["走廊尽头的脚步声"],
            "milestones": [
                {
                    "label": "队伍取得可行动的确切情报并已互相传达",
                    "evidence_required": [
                        {"type": "clue_keyword_any", "match": ["确认位置"]}
                    ],
                    "source": {"rel_path": "01.md", "line_start": 1, "line_end": 1,
                               "quote": "拉姆与雷姆站在走廊尽头"},
                }
            ],
            "key_npcs": [{"ref": "npc_ram", "role": "指路"}],
        }
    }


def _cast() -> dict:
    return {
        "npcs": [
            {
                "slug": "npc_ram", "name": "拉姆", "identity": "双胞胎女仆之一",
                "appearance": "粉发", "personality": "嘴硬", "public_background": "宅邸女仆",
                "location": "走廊", "capabilities": ["清扫"], "limitations": ["厨艺弱"],
                "prompt": "她只答与工作有关的问题。",
                "source": {"rel_path": "01.md", "line_start": 1, "line_end": 1,
                           "quote": "拉姆与雷姆站在走廊尽头"},
            }
        ]
    }


def _card() -> dict:
    return {
        "professions": [
            {"id": f"prof_{i}", "name": f"职业{i}", "description": "擅长若干事务。",
             "base_attributes": dict(zip(ATTR_KEYS, values))}
            for i, values in enumerate(BASE_SETS)
        ],
        "attribute_labels": {k: k for k in ATTR_KEYS},
    }


def _prose() -> dict:
    return {
        "opening_scene": "你在陌生的房间里醒来。",
        "opening_choices": [
            {"key": "A", "text": "查看房间", "risk": "safe"},
            {"key": "B", "text": "敲门", "risk": "safe"},
            {"key": "C", "text": "检查物品", "risk": "safe"},
            {"key": "D", "text": "全队下楼", "risk": "safe", "collective": True},
        ],
        "system_prompt": "本世界不存在死亡回归。",
        "chapters": [{"chapter_id": "ch_01_unknown_ceiling", "pacing_directive": "逐步收紧"}],
    }


def _reflect() -> dict:
    return {"findings": [{"claim_id": "c001", "verdict": "supported", "severity": "info",
                          "reason": "与原文一致"}]}


def _origin_plan() -> dict:
    return {
        "premise": "一座会在夜里改变结构的宅邸。",
        "chapters": [
            {
                "title": "第一夜",
                "subtitle": "陌生",
                "min_turns": 1,
                "max_turns": 8,
                "current_objective": "在结构改变前找到同伴",
                "pacing_directive": "走廊每夜改变一次",
                "hook_pool": ["墙上的刻痕"],
                "milestones": [
                    {"label": "找到至少一名同伴并确认其身份",
                     "evidence_required": [
                         {"type": "clue_keyword_any", "match": ["确认身份"]}
                     ]}
                ],
                "key_npcs": [{"ref": "npc_guide", "name": "引路人", "role": "指路"}],
            }
        ],
    }


def _coverage_all_covered() -> dict:
    """默认：所有必现情节都有落点。要测遗漏时用 overrides 覆盖它。"""
    return {"findings": [
        {"claim_id": "cov001", "verdict": "covered",
         "reason": "由「第一章：陌生的天花板」的里程碑承接"},
    ]}


_RESPONSES = {
    "worldgen_timeline": _timeline,
    "worldgen_arc_map": _arc_map,
    "worldgen_extract_chapter": _chapter,
    "worldgen_cast": _cast,
    "worldgen_card": _card,
    "worldgen_prose": _prose,
    "worldgen_reflect": _reflect,
    "worldgen_coverage": _coverage_all_covered,
    "worldgen_origin_plan": _origin_plan,
}


class FakeDatabase:
    """照抄 ``list_sessions`` 的**真实**键名。

    这里曾经用的是 ``session_id`` / ``title`` / ``status``——那是我凭空想的字段名，
    真实返回的是 ``id`` / ``instance_name`` / ``state``。假数据用错名字，
    于是"面板下拉是空的"这个真 bug 在测试里完全看不出来。
    """

    def __init__(self) -> None:
        self.sessions = [
            {"id": "session_done", "instance_name": "已经跑完的档",
             "world_name": "王都第一日", "state": "finished",
             "updated_at": "2026-09-15T10:00:00"},
            {"id": "session_closed", "instance_name": "自己关掉的档",
             "world_name": "雪原", "state": "closed",
             "updated_at": "2026-09-14T10:00:00"},
            # 还在跑的档**不该**出现在继承列表里
            {"id": "session_live", "instance_name": "正在跑的档",
             "world_name": "夜宅", "state": "running",
             "updated_at": "2026-09-16T10:00:00"},
        ]

    async def list_sessions(self):
        return list(self.sessions)


class FakeContext:
    """记下 ``register_web_api`` 收到的路由表，供断言直接对照。"""

    def __init__(self) -> None:
        self.routes: dict[str, tuple] = {}

    def register_web_api(self, path, handler, methods=None, desc=""):
        # 真实签名把方法与描述也传进来；顺序可能不同，按关键字兜住。
        self.routes[str(path)] = (handler, methods, desc)


def _bind_request_username(username: str) -> None:
    """把 ``username`` 写到 ``web_console`` **实际持有**的那个 request 对象上。

    ``web_console`` 的 ``request`` 是 ``from astrbot.api.web import request``
    时绑定的**模块级名字**。而 ``test_plugin_shell`` / ``test_v0111`` /
    ``test_v0160`` 各自造一套桩，**整个替换** ``sys.modules["astrbot.api.web"]``
    ——替换之后，按老写法往 ``sys.modules[...]`` 里写 username 就是写给另一个
    对象，``web_console`` 手里那个始终是空串。

    症状极具迷惑性：全套 worldgen 路由（含"危险输入必须被拦"这类守卫用例）
    一起变成 ``需要登录 AstrBot 管理后台``，而**单跑这个模块全绿**——只有
    ``discover`` 按字母序跑到 ``test_plugin_shell`` 之后才复现。
    """
    from tavern import web_console

    web_console.request.username = username or None


def _console(root: Path, *, payload: dict | None = None, username: str = "admin"):
    """装一个带真实 WorldgenService 的控制台。"""
    corpus = root / "corpus"
    volume = corpus / "chapter020"
    volume.mkdir(parents=True, exist_ok=True)
    (volume / "README.md").write_text(README, encoding="utf-8")
    (volume / "01.md").write_text(DOC1, encoding="utf-8")

    service = WorldgenService(
        context=SimpleNamespace(),
        database=FakeDatabase(),
        broker=SimpleNamespace(publish=lambda event: asyncio.sleep(0)),
        store=JobStore(root=root / "jobs"),
        plugin_config=TavernConfig(),
        corpus_root=corpus,
        index_dir=root / "index",
        output_dir=root / "worlds",
    )
    service._llm = FakeLLM()

    context = FakeContext()
    console = TavernWebConsole(
        context=context,
        plugin_config={},
        database=FakeDatabase(),
        broker=SimpleNamespace(publish=lambda event: asyncio.sleep(0)),
        data_dir=root,
        logger=SimpleNamespace(exception=lambda *a, **k: None,
                               warning=lambda *a, **k: None,
                               info=lambda *a, **k: None,
                               debug=lambda *a, **k: None),
        allow_group=lambda **kwargs: False,
        config_lock=asyncio.Lock(),
        worldgen=service,
    )
    # 鉴权走**真的** ``_username``：它是 staticmethod，``_actor()`` 又通过
    # classmethod 调它，桩实例属性盖不住——只有把 ``request.username`` 设对，
    # 两条路径才都会按真实逻辑走。
    _bind_request_username(username)
    # 请求体桩掉，直接驱动 handler。

    if payload is not None:
        async def _payload():
            return dict(payload)
        console._payload = _payload
    console.routes = context.routes
    return console, service


class RouteBindingTests(unittest.TestCase):
    """绑定正确性——这是 ``extensions`` 那个坑的同类问题。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.console, _ = _console(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_every_route_reaches_a_coroutine_handler(self) -> None:
        """对着**真实注册表**验证：路径绑到的是协程函数，不是一个普通对象。

        ``extensions`` 路由当年把注册表对象绑了上去，``GET /extensions`` 直接 500。
        属性名与 handler 名撞车时就是这个症状，所以这里断言的是注册表里的实物。
        """
        registered = self.console.routes
        for path in sorted(EXPECTED_ROUTES):
            self.assertIn(path, registered, msg=f"路由没注册：{path}")
            handler = registered[path][0]
            self.assertTrue(
                asyncio.iscoroutinefunction(handler),
                msg=f"{path} 绑定的不是协程函数（请求时会炸成 500）：{handler!r}",
            )

    def test_no_worldgen_handler_shadows_an_attribute(self) -> None:
        """handler 名不能和实例属性撞车。"""
        service = self.console._worldgen_service
        for suffix in EXPECTED_SUFFIXES:
            path = f"{ROUTE_PREFIX}worldgen/{suffix}"
            handler = self.console.routes[path][0]
            self.assertIsNot(
                handler,
                service,
                msg=f"{path} 的 handler 绑成了服务实例（extensions 那个坑）",
            )
            # 绑定方法每次访问都是新对象，比 __func__ 才是比"同一个函数"。
            self.assertIs(
                handler.__func__,
                getattr(self.console, f"worldgen_{suffix.replace('-', '_')}").__func__,
            )

    def test_read_routes_declare_get(self) -> None:
        """只读端点必须是 GET，动作端点必须是 POST——写错了面板调不通。"""
        get_suffixes = {"corpus", "sessions", "jobs"}
        for suffix in EXPECTED_SUFFIXES:
            methods = self.console.routes[f"{ROUTE_PREFIX}worldgen/{suffix}"][1]
            expected = "GET" if suffix in get_suffixes else "POST"
            self.assertIn(expected, methods or [], msg=f"worldgen/{suffix}")


class GuardTests(unittest.TestCase):
    """危险输入必须被拦下，而不是交给下游。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_requires_login(self) -> None:
        console, _ = _console(self.root, username="")
        result = asyncio.run(console.worldgen_jobs())
        self.assertIn("登录", str(result))

    def _route_mixin(self):
        from tavern.worldgen.routes import WorldgenConsoleRoutes

        return WorldgenConsoleRoutes

    def _service(self, root: Path):
        console, service = _console(root)
        return console, service

    def test_lookup_rejects_anything_not_on_the_list(self) -> None:
        """语料定位**按名单查**，不拼路径——越界、绝对路径、未知名字一律拒。

        这一条替代了原先的"_resolve_volume 做路径校验"：改成查名单之后，
        根本不存在"拼出一个目录之外的路径"这种可能，因为路径从来不来自输入。
        """
        from tavern.worldgen.corpus import CorpusError

        console, service = self._service(self.root)
        for bad in ("../secrets", "..\\secrets", "a/b", "..", "", "   ", "/etc"):
            # 空/纯空白给 ValueError（"请先选择"），其余按名单查不到给 CorpusError。
            # 两种都是拒绝，这里关心的是"绝不落到文件系统上"。
            with self.assertRaises((ValueError, CorpusError), msg=f"没拦住：{bad!r}"):
                console._find_novel(service, bad)

    def test_two_jobs_in_the_same_second_do_not_collide(self) -> None:
        """连点两次"开始生成"不能撞 id——原来只精确到秒，第二个必然失败。"""
        from tavern.worldgen.store import JobStore, new_job_id
        from tavern.worldgen.models import JobRecord

        ids = {new_job_id() for _ in range(50)}
        self.assertEqual(50, len(ids), msg="同一秒内生成的作业 id 有重复")

        store = JobStore(root=self.root / "jobs")
        first = store.create(JobRecord(job_id=new_job_id()))
        second = store.create(JobRecord(job_id=new_job_id()))
        self.assertNotEqual(first.job_id, second.job_id)

    def test_unknown_novel_is_rejected(self) -> None:
        from tavern.worldgen.corpus import CorpusError

        console, service = self._service(self.root)
        with self.assertRaises(CorpusError):
            console._find_novel(service, "does-not-exist")

    def test_normal_novel_and_volume_resolve(self) -> None:
        console, service = self._service(self.root)
        novel = console._find_novel(service, "corpus")
        self.assertEqual("corpus", novel["slug"])
        volume = console._find_volume(service, "corpus", "chapter020")
        self.assertEqual("chapter020", volume["slug"])
        self.assertTrue(Path(str(volume["directory"])).is_dir())

    def test_artifact_whitelist_blocks_arbitrary_reads(self) -> None:
        """作业目录里还有 job.json 等内部状态，不能让人点名读。"""
        for bad in ("job.json", "../../etc/passwd", "40_world.draft.json.bak", ""):
            console, _ = _console(self.root, payload={"job_id": "wg_abcdef", "name": bad})
            result = asyncio.run(console.worldgen_artifact())
            self.assertIn("不允许读取", str(result), msg=bad)

    def test_delivered_packages_are_readable(self) -> None:
        """交付产物必须能点名读——面板的「导入这个世界」靠它把两份喂给导入通道。

        真实事故：交付写成 ``worlds/*.json`` 之后，面板只给了一个
        「去世界市场导入」，而世界市场只收带 ``slug`` 的世界包，
        ``xxx-npcs.json`` 会被扫描直接跳过。于是世界进去了、七个 NPC 全留在
        门外，玩家看到的是一具零常驻 NPC 的空壳。
        """
        from tavern.worldgen.routes import _ARTIFACT_WHITELIST

        for name in ("80_world.json", "80_npcs.json"):
            self.assertIn(name, _ARTIFACT_WHITELIST)

    def test_delivery_panel_imports_both_packages(self) -> None:
        """面板必须有一条**同时**导入世界与 NPC 的路。

        这里钉的是契约的另一半：名单开放了，但面板只导世界，等于没修。
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "pages" / "console" / "app.js"
        ).read_text("utf-8")
        markup = source[
            source.index("function worldgenOutputsMarkup(job)") :
            source.index("\nfunction ", source.index("function worldgenOutputsMarkup(job)") + 1)
        ]
        self.assertIn("import-delivery", markup, msg="交付面板没有一键导入按钮")

        action = source[
            source.index("async function worldgenAction(action)") :
        ]
        self.assertIn('"80_world.json"', action)
        self.assertIn('"80_npcs.json"', action)
        self.assertIn('"characters/import"', action, msg="NPC 包没有被导入")

    def test_delivery_import_fails_loudly_instead_of_skipping_npcs(self) -> None:
        """世界进去了但 NPC 没进去时必须报错，不能弹一句「成功了」。

        原先的写法是 ``if (item && items.length) { ...导入 NPC... }``——条件不满足
        就**静默跳过**，随后照样弹「已导入 …与 0 个常驻 NPC」。玩家看到的是成功
        提示和一个空世界，只能靠数 NPC 才发现。
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "pages" / "console" / "app.js"
        ).read_text("utf-8")
        action = source[source.index("async function worldgenAction(action)") :]
        self.assertNotIn(
            "if (item && items.length)",
            action,
            msg="NPC 导入又被写成静默跳过了",
        )
        # 三条失败路径都要显式抛错：没拿到世界 / NPC 包是空的 / 一个都没建出来
        self.assertIn("没找到这个世界的记录", action)
        self.assertIn("items 为空", action)
        self.assertIn("没有新增任何角色", action)

    def test_missing_service_reports_plainly(self) -> None:
        console, _ = _console(self.root)
        console._worldgen_service = None
        result = asyncio.run(console.worldgen_jobs())
        self.assertIn("未启用", str(result))


class CreateValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_unknown_kind(self) -> None:
        console, _ = _console(self.root, payload={"kind": "nonsense"})
        self.assertIn("kind", str(asyncio.run(console.worldgen_create())))

    def test_adaptation_requires_novel_then_volume(self) -> None:
        """改编是两级：先选小说，再选卷。缺一步都给得出人话提示。"""
        console, _ = _console(self.root, payload={"kind": "adaptation"})
        self.assertIn("小说", str(asyncio.run(console.worldgen_create())))

        console, _ = _console(
            self.root, payload={"kind": "adaptation", "novel": "corpus"}
        )
        self.assertIn("卷", str(asyncio.run(console.worldgen_create())))

    def test_origin_requires_concept(self) -> None:
        console, _ = _console(self.root, payload={"kind": "origin"})
        self.assertIn("concept", str(asyncio.run(console.worldgen_create())))

    def test_name_and_slug_are_derived_when_omitted(self) -> None:
        """名字和 slug 不用玩家填——服务端也要能自己推出来，不能落空。"""
        from tavern.worldgen.emit import SLUG_RE

        async def run() -> None:
            console, service = _console(
                self.root,
                payload={
                    "kind": "adaptation", "novel": "corpus", "volume_ref": "chapter020"
                },
            )
            result = await console.worldgen_create()
            job_id = result["job"]["job_id"]
            record = service.store.load(job_id)
            self.assertTrue(record.request.get("name"))
            slug = str(record.request.get("slug") or "")
            self.assertTrue(SLUG_RE.match(slug), msg=f"派生的 slug 非法：{slug!r}")
            await service.cancel(job_id, actor="test")

        asyncio.run(run())

    def test_requirements_and_provider_are_recorded(self) -> None:
        async def run() -> None:
            console, service = _console(
                self.root,
                payload={
                    "kind": "origin",
                    "concept": "一座夜里会改变结构的宅邸",
                    "requirements": "偏悬疑，不要轻松日常，第一章就要有人失踪",
                    "provider_id": "some-model",
                },
            )
            result = await console.worldgen_create()
            job_id = result["job"]["job_id"]
            record = service.store.load(job_id)
            self.assertIn("悬疑", record.request.get("requirements", ""))
            # 原创路径的提示词读 preferences，两边都要落上
            self.assertIn("悬疑", record.request.get("preferences", ""))
            self.assertEqual("some-model", record.request.get("provider_id"))
            await service.cancel(job_id, actor="test")

        asyncio.run(run())


class FlowTests(unittest.TestCase):
    """从建到交付走一遍，验证端点之间串得起来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.console, self.service = _console(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _set_payload(self, payload: dict) -> None:
        async def _payload():
            return dict(payload)
        self.console._payload = _payload

    def test_full_flow_via_handlers(self) -> None:
        async def run() -> None:
            self._set_payload({
                "kind": "adaptation",
                "novel": "corpus",
                "volume_ref": "chapter020",
                "slug": "route-demo",
                "name": "路由验证",
                "description": "通过控制台端点跑一遍。",
            })
            created = await self.console.worldgen_create()
            job_id = created["job"]["job_id"]
            self.assertTrue(job_id.startswith("wg_"))

            task = self.service._tasks.get(job_id)
            if task is not None:
                await task

            # 停在审批门
            detail = await self._detail(job_id)
            self.assertEqual("awaiting_approval", detail["job"]["state"])
            self.assertTrue(detail["job"]["proposal"]["chapters"])

            # 全部批准并提交
            self._set_payload({"job_id": job_id, "submit": True, "decisions": {}})
            await self.console.worldgen_approve()
            task = self.service._tasks.get(job_id)
            if task is not None:
                await task

            detail = await self._detail(job_id)
            self.assertEqual("succeeded", detail["job"]["state"], msg=detail["job"].get("error"))
            outputs = detail["job"]["outputs"]
            self.assertTrue(Path(outputs["world"]).is_file())
            self.assertTrue(Path(outputs["npcs"]).is_file())

        asyncio.run(run())

    async def _detail(self, job_id: str) -> dict:
        self._set_payload({"job_id": job_id})
        return await self.console.worldgen_job()

    def test_job_list_and_unknown_job(self) -> None:
        async def run() -> None:
            listed = await self.console.worldgen_jobs()
            self.assertIn("jobs", listed)
            self.assertIn("stats", listed)

            self._set_payload({"job_id": "wg_doesnotexist"})
            result = await self.console.worldgen_job()
            self.assertIsInstance(result, str)

        asyncio.run(run())

    def test_cancel_then_delete_but_not_before(self) -> None:
        """跑着的作业不许删——删了后台任务会往消失的目录里继续写。"""

        async def run() -> None:
            self._set_payload({
                "kind": "origin", "concept": "一座夜里会改变结构的宅邸",
                "slug": "route-origin", "name": "夜宅",
            })
            created = await self.console.worldgen_create()
            job_id = created["job"]["job_id"]
            task = self.service._tasks.get(job_id)
            if task is not None:
                await task

            # 停在审批门 = 未结束，删除必须被拒
            self._set_payload({"job_id": job_id})
            self.assertIn("先取消", str(await self.console.worldgen_delete()))

            await self.console.worldgen_cancel()
            result = await self.console.worldgen_delete()
            self.assertEqual(job_id, result["deleted"])

        asyncio.run(run())

    def test_corpus_listing_is_two_level(self) -> None:
        """先小说、再卷——两级都要列得出来。"""

        async def run() -> None:
            corpus = await self.console.worldgen_corpus()
            self.assertTrue(corpus["exists"])
            self.assertEqual(["corpus"], [n["slug"] for n in corpus["novels"]])
            self.assertEqual(1, corpus["novels"][0]["volume_count"])

            self._set_payload({"novel": "corpus"})
            volumes = await self.console.worldgen_volumes()
            self.assertEqual(["chapter020"], [v["slug"] for v in volumes["volumes"]])
            self.assertEqual(1, volumes["volumes"][0]["doc_count"])

        asyncio.run(run())

    def test_sessions_list_includes_unfinished_but_marks_them(self) -> None:
        """没完结的档**也要列出来**，但要标清状态。

        原先只给 finished/closed，理由是"后果还没发生完"；玩家要的是能自己挑——
        跑到一半的档往往已经攒下大量已结算的影响。所以现在全列，
        用 ``ended`` 标记 + 排序（完结的靠前）来提示，而不是藏起来。
        键名还必须对得上 ``list_sessions`` 的真实返回结构。
        """

        async def run() -> None:
            result = await self.console.worldgen_sessions()
            by_id = {s["session_id"]: s for s in result["sessions"]}

            self.assertIn("session_done", by_id)
            self.assertIn("session_closed", by_id)
            self.assertIn("session_live", by_id, msg="进行中的档也应该能选")

            self.assertTrue(by_id["session_done"]["ended"])
            self.assertTrue(by_id["session_closed"]["ended"])
            self.assertFalse(by_id["session_live"]["ended"])
            self.assertEqual("running", by_id["session_live"]["state"])

            # 已完结的排在未完结的前面；同为已完结的按时间倒序
            ids = [s["session_id"] for s in result["sessions"]]
            self.assertEqual(["session_done", "session_closed", "session_live"], ids)

            titles = {s["session_id"]: s["title"] for s in result["sessions"]}
            self.assertEqual("已经跑完的档", titles["session_done"])

        asyncio.run(run())

    def test_continuity_preview_is_read_only(self) -> None:
        async def run() -> None:
            self._set_payload({"session_id": ""})
            self.assertIn("session_id", str(await self.console.worldgen_continuity()))

        asyncio.run(run())

    def test_lint_endpoint_reports_errors(self) -> None:
        async def run() -> None:
            self._set_payload({"world": {"slug": "x", "name": "n"}})
            result = await self.console.worldgen_lint()
            self.assertFalse(result["report"]["ok"])
            self.assertTrue(result["report"]["issues"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
