"""反省机制的检索层。

设计取向
--------
用户选择「先纯本地词法，预留开关」，因此：

- :class:`LexicalRetriever` 是**唯一已实现**的后端：jieba 分词 + BM25Okapi，
  全程离线、零外部依赖（jieba / rank_bm25 已在运行时 venv 中）。
- :class:`DenseRetriever` 只留接口与构造参数，调用即抛 ``NotImplementedError``，
  并在异常信息里写清接入方式（AstrBot 自带 ``context.kb_manager``，配 bge-m3）。

为什么不直接用 SQLite FTS5
--------------------------
实测（README 里记录过）：FTS5 的 unicode61 分词器**不切中文**，整句会变成一个
token，``MATCH '女仆'`` 命中为空；即便用 jieba 预切词入库，FTS5 默认是隐式 **AND**，
``村庄 的 幼犬`` 这类多词查询会因个别词缺失而漏召回。BM25Okapi 按词项重叠打分，
天然是 OR 语义，更适合「模糊找回相关段落」这一用途。

索引粒度
--------
**按卷**构建。单卷最大约 815k 字（chapter030），在全内存下构建与查询都是毫秒级，
不必引入向量库。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .corpus import Passage, VolumeSource, chunk_volume
from .models import Citation, CitationCheck


def _squash(text: str) -> str:
    """压掉所有空白，用于引用回验的宽松比对。"""
    return "".join(str(text or "").split())

INDEX_CACHE_VERSION = 1
DEFAULT_TOP_K = 8

#: 中文停用词：BM25 会给这些高频虚词很高的文档频率，拉低有效词的区分度。
STOPWORDS = frozenset(
    "的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 那 他 她 它 们 而 及 与 或 但 被 把 让 从 对 为 之 其 于 以 已 并 等".split()
)


@dataclass(frozen=True)
class Hit:
    """一次检索命中。"""

    passage: Passage
    score: float

    @property
    def citation(self) -> str:
        return self.passage.citation


class Retriever(Protocol):
    """检索后端接口。"""

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> list[Hit]:
        ...

    def expand(self, passage: Passage, *, radius: int = 1) -> list[Passage]:
        """取相邻片段，用于把命中点扩成可读上下文。"""
        ...


#: 已注册到 jieba 的专有名词，避免重复 add_word。
_REGISTERED_LEXICON: set[str] = set()


def register_lexicon(words: Iterable[str]) -> int:
    """把专有名词加进 jieba 词典，返回新注册数量。

    实测（chapter030，1000+ 处样本）：jieba 对本书人名的切分**是稳定的**——
    ``罗兹瓦尔`` 恒切 ``罗兹 瓦尔``、``艾米莉娅`` 恒切 ``艾米莉 娅``、
    ``尤里乌斯`` 恒切 ``尤 里乌斯``，不随上下文变化。索引与查询使用同一分词器，
    因此**不会漏召回**。

    但仍值得注册，原因有二：

    1. 稳定性是实测结论而非保证。名字出现在罕见上下文（生僻字相邻、标点切分）时
       仍可能切法不同，一旦两侧不一致就是静默漏召回。
    2. 精度：``尤 里乌斯`` 让单字 ``尤`` 成为词元，会带来无关命中。

    典型用法是抽取阶段拿到 NPC 名录后回灌，例如
    ``register_lexicon([n["name"] for n in npc_candidates])``，再重建索引。
    """
    import jieba

    added = 0
    for word in words:
        token = str(word or "").strip()
        if len(token) < 2 or token in _REGISTERED_LEXICON:
            continue
        jieba.add_word(token)
        _REGISTERED_LEXICON.add(token)
        added += 1
    return added


def tokenize(text: str) -> list[str]:
    """jieba 分词 + 去停用词 + 丢弃纯标点。

    jieba 在首次调用时会构建词典缓存（约 0.3s），之后常驻。
    """
    import jieba

    tokens: list[str] = []
    for raw in jieba.cut(text):
        token = raw.strip().lower()
        if not token or token in STOPWORDS:
            continue
        # 丢掉纯标点/空白
        if not any(ch.isalnum() or "一" <= ch <= "鿿" for ch in token):
            continue
        tokens.append(token)
    return tokens


class LexicalRetriever:
    """jieba + BM25Okapi 的纯本地检索。"""

    def __init__(self, source: VolumeSource, passages: Sequence[Passage]) -> None:
        self.source = source
        self.passages: list[Passage] = list(passages)
        self._tokens: list[list[str]] = [tokenize(p.text) for p in self.passages]
        self._token_sets: list[set[str]] = [set(t) for t in self._tokens]
        self._bm25: Any = None
        self._by_doc: dict[int, list[int]] = {}
        for position, passage in enumerate(self.passages):
            self._by_doc.setdefault(passage.doc_index, []).append(position)

    # --- 构建 ---------------------------------------------------------

    @classmethod
    def build(cls, source: VolumeSource, *, chunk_chars: int = 600) -> "LexicalRetriever":
        return cls(source, chunk_volume(source, chunk_chars=chunk_chars))

    #: 分词后为空的片段（纯符号段）替换成这个哨兵词，避免 BM25Okapi 拿到空列表。
    EMPTY_DOC_TOKEN = "__empty__"

    @property
    def bm25(self) -> Any:
        if self._bm25 is None:
            from rank_bm25 import BM25Okapi

            corpus = [
                tokens or [self.EMPTY_DOC_TOKEN] for tokens in self._tokens
            ]
            self._bm25 = BM25Okapi(corpus)
        return self._bm25

    # --- 查询 ---------------------------------------------------------

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> list[Hit]:
        """按 OR 语义检索：命中任一词元即算候选，再按 BM25 分排序。

        为什么不能直接用 BM25 分数做阈值过滤：当某个词元出现在**约半数以上**的
        片段里（小语料，或本卷主角的名字），它的 IDF 会归零甚至转负，BM25 分数随之
        变成 0——但那些片段**确实包含该词**。若拿 ``score <= 0`` 当"没命中"，
        就会静默返回空结果。

        所以命中判定用**词元重叠**（真实的 OR 语义），BM25 只负责排序；
        分数同为 0 时退化为按重叠词数排序。
        """
        query_tokens = tokenize(query)
        if not query_tokens or not self.passages:
            return []
        query_set = set(query_tokens)

        candidates = [
            index
            for index, token_set in enumerate(self._token_sets)
            if token_set & query_set
        ]
        if not candidates:
            return []

        scores = self.bm25.get_scores(query_tokens)
        overlap = {index: len(self._token_sets[index] & query_set) for index in candidates}
        candidates.sort(key=lambda index: (float(scores[index]), overlap[index]), reverse=True)

        return [
            Hit(passage=self.passages[index], score=float(scores[index]))
            for index in candidates[: max(1, top_k)]
        ]

    def expand(self, passage: Passage, *, radius: int = 1) -> list[Passage]:
        positions = self._by_doc.get(passage.doc_index, [])
        if not positions:
            return [passage]
        # 找出目标片段在本文档内的位置
        anchor = None
        for offset, position in enumerate(positions):
            candidate = self.passages[position]
            if (
                candidate.line_start == passage.line_start
                and candidate.line_end == passage.line_end
            ):
                anchor = offset
                break
        if anchor is None:
            return [passage]
        low = max(0, anchor - radius)
        high = min(len(positions), anchor + radius + 1)
        return [self.passages[positions[i]] for i in range(low, high)]

    def stats(self) -> dict[str, object]:
        return {
            "backend": "lexical-bm25",
            "volume": self.source.slug,
            "arc_title": self.source.arc_title,
            "documents": len(self.source.docs),
            "passages": len(self.passages),
            "chars": self.source.total_chars,
        }

    # --- 出处回验 -----------------------------------------------------

    def fetch_span(self, rel_path: str, line_start: int, line_end: int) -> str:
        """取出原文指定行区间的**逐字**内容。行号 1-based、闭区间。

        返回空串表示该文件不存在或区间越界。
        """
        doc = self._doc_by_path.get(rel_path)
        if doc is None:
            return ""
        lines = doc.text.splitlines()
        if line_start < 1 or line_end < line_start or line_start > len(lines):
            return ""
        return "\n".join(lines[line_start - 1 : min(line_end, len(lines))])

    def verify_citation(self, citation: Citation) -> CitationCheck:
        """回验一条引用：引文必须能在它声称的行区间里逐字找到。

        这是整个反幻觉设计的**唯一硬证据**。模型编造的出处在这里过不去，
        而核验不过的引用一律不许用来推翻任何生成内容。

        采用**包含**而非相等判定：超长段落会被切成多片，每片沿用同一行区间，
        文本是区间的子串。
        """
        span = self.fetch_span(citation.rel_path, citation.line_start, citation.line_end)
        if not span:
            return CitationCheck(
                citation=citation,
                ok=False,
                reason=f"行区间不存在或越界：{citation.label}",
            )
        quote = (citation.quote or "").strip()
        if not quote:
            return CitationCheck(citation=citation, ok=False, actual_text=span, reason="引用没有给出原文")

        # 去掉原文里的换行与空白后再比对，避免模型把段间空行写错就判失败。
        normalized_span = _squash(span)
        normalized_quote = _squash(quote)
        if not normalized_quote:
            return CitationCheck(citation=citation, ok=False, actual_text=span, reason="引用全是空白")

        if normalized_quote in normalized_span:
            return CitationCheck(citation=citation, ok=True, actual_text=span)

        return CitationCheck(
            citation=citation,
            ok=False,
            actual_text=span,
            reason="引文未出现在其声称的行区间内（疑似编造或行号有误）",
        )

    @property
    def _doc_by_path(self) -> dict[str, Any]:
        cached = getattr(self, "_doc_index", None)
        if cached is None:
            cached = {doc.rel_path: doc for doc in self.source.docs}
            self._doc_index = cached
        return cached


class DenseRetriever:
    """向量检索占位后端（未实现）。

    用户当前选择纯本地词法。若日后要升级语义召回，接入点已经明确：

    1. AstrBot 自带 ``astrbot/core/knowledge_base/``（faiss + FTS5 + jieba + RRF），
       在插件内可直接用 ``self.context.kb_manager`` 访问；
    2. 需要一个可用的 embedding provider——目前 ``cmd_config.json`` 里已配好
       SiliconFlow ``BAAI/bge-m3``（1024 维），但**小说正文会发往第三方 API**，
       属于使用者的数据出境决定，不能由代码默认开启；
    3. 若要完全离线，需自行下载本地嵌入模型（此机 huggingface.co 不可达，
       历史上留下过空的 HF 缓存，不要默认走这条路）。

    实现时保持与 :class:`Retriever` 相同的接口，即可无缝替换。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "DenseRetriever 尚未实现。当前使用 LexicalRetriever（jieba + BM25，"
            "全离线）。启用向量检索前请先确认 embedding provider 与数据出境策略。"
        )

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> list[Hit]:  # pragma: no cover
        raise NotImplementedError

    def expand(self, passage: Passage, *, radius: int = 1) -> list[Passage]:  # pragma: no cover
        raise NotImplementedError


# --- 索引缓存 -------------------------------------------------------------


def _cache_path(cache_dir: Path, slug: str) -> Path:
    return cache_dir / f"index-{slug}.json"


def _source_fingerprint(source: VolumeSource) -> str:
    """用篇目数 + 总字数 + 各篇字符数做指纹，语料变了就失效。"""
    parts = [source.slug, source.arc_title, str(len(source.docs)), str(source.total_chars)]
    parts.extend(f"{doc.rel_path}:{len(doc.text)}" for doc in source.docs)
    return "|".join(parts)


def load_or_build_index(
    source: VolumeSource,
    cache_dir: str | Path,
    *,
    chunk_chars: int = 600,
    force: bool = False,
) -> tuple[LexicalRetriever, bool]:
    """带磁盘缓存的索引构建。

    Returns:
        ``(retriever, from_cache)``。缓存命中时省掉全卷 jieba 分词。
    """
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _cache_path(directory, source.slug)
    fingerprint = _source_fingerprint(source)

    if not force and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("version") == INDEX_CACHE_VERSION
                and payload.get("fingerprint") == fingerprint
            ):
                passages = [
                    Passage(
                        doc_index=item["d"],
                        doc_title=item["t"],
                        file=item["f"],
                        line_start=item["a"],
                        line_end=item["b"],
                        text=item["x"],
                    )
                    for item in payload["passages"]
                ]
                retriever = LexicalRetriever.__new__(LexicalRetriever)
                retriever.source = source
                retriever.passages = passages
                retriever._tokens = [list(t) for t in payload["tokens"]]
                retriever._token_sets = [set(t) for t in retriever._tokens]
                retriever._bm25 = None
                retriever._by_doc = {}
                for position, passage in enumerate(passages):
                    retriever._by_doc.setdefault(passage.doc_index, []).append(position)
                return retriever, True
        except (OSError, ValueError, KeyError, TypeError):
            # 缓存损坏就当没有，重建即可。
            pass

    retriever = LexicalRetriever.build(source, chunk_chars=chunk_chars)
    payload = {
        "version": INDEX_CACHE_VERSION,
        "fingerprint": fingerprint,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passages": [
            {
                "d": p.doc_index,
                "t": p.doc_title,
                "f": p.file,
                "a": p.line_start,
                "b": p.line_end,
                "x": p.text,
            }
            for p in retriever.passages
        ],
        "tokens": retriever._tokens,
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # 缓存写不进去不影响本次使用。
        pass
    return retriever, False


def stale_index_size(cache_dir: str | Path) -> int:
    """缓存目录占用字节数，供面板展示/清理。"""
    directory = Path(cache_dir)
    if not directory.is_dir():
        return 0
    return sum(p.stat().st_size for p in directory.glob("index-*.json") if p.is_file())
