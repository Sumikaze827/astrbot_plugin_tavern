"""小说卷素材源的解析。

语料约定
--------
``D:/Project/GitbookReader/re0-ch/`` 下每个目录是一个「卷/章」（arc），目录内：

- ``README.md`` —— 该卷的目录。形如 ``- [序章　『赎罪的开端』](00.md)``，
  既给出卷标题（``## 第二章　『动荡的一周』``）也给出**有序**的篇目清单。
  这是唯一可靠的顺序来源：文件名 ``NN.md`` 虽然多数有序，但存在
  ``99.md``（后记）这类不参与正篇的条目。
- ``NN.md`` —— 正篇。首行为 ``# 『标题』``。

因此**不要靠猜文件名排序**，一律以 README 的链接顺序为准。

所有切块都保留 ``file`` 与 ``line_start`` / ``line_end``，因为反省阶段
（reflect）要求模型引用原文出处，没有行号就无法证伪幻觉。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: ``- [标题](文件.md)``
TOC_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[(?P<title>[^\]]+)\]\((?P<href>[^)]+\.md)\)\s*$")
#: ``## 第二章　『动荡的一周』``
ARC_TITLE_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)
MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s*")
#: 分隔线：原文用 ``------`` 切场景。
DIVIDER_RE = re.compile(r"^\s*-{3,}\s*$")
#: 图片表格等非正文行。
NON_PROSE_RE = re.compile(r"^\s*[|!<]")

DEFAULT_CHUNK_CHARS = 600
DEFAULT_CHUNK_OVERLAP = 120


class CorpusError(RuntimeError):
    """素材源不可用（目录缺失、README 缺失、无可读正篇）。"""


@dataclass(frozen=True)
class SourceDoc:
    """一篇正篇。"""

    index: int
    title: str
    path: Path
    rel_path: str
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


@dataclass(frozen=True)
class Passage:
    """检索命中的原文片段。``file``/``line_start`` 用于强制引用出处。"""

    doc_index: int
    doc_title: str
    file: str
    line_start: int
    line_end: int
    text: str

    @property
    def citation(self) -> str:
        """形如 ``37.md:88-102``，供模型在反省结论里引用。"""
        if self.line_start == self.line_end:
            return f"{self.file}:{self.line_start}"
        return f"{self.file}:{self.line_start}-{self.line_end}"


@dataclass
class VolumeSource:
    """一个卷的全部素材。"""

    slug: str
    directory: Path
    arc_title: str = ""
    docs: list[SourceDoc] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(doc.text) for doc in self.docs)

    def outline(self) -> list[dict[str, object]]:
        """给「检查点提取」阶段用的篇目大纲。"""
        return [
            {
                "index": doc.index,
                "title": doc.title,
                "file": doc.rel_path,
                "chars": len(doc.text),
                "line_count": len(doc.lines),
            }
            for doc in self.docs
        ]


def _read_text(path: Path) -> str:
    # 语料是 UTF-8；个别文件可能带 BOM。
    return path.read_text(encoding="utf-8-sig")


def _parse_toc(readme_text: str) -> list[tuple[str, str]]:
    """从 README 解析 ``[(标题, 文件名)]``，保持链接出现顺序。"""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in readme_text.splitlines():
        match = TOC_ENTRY_RE.match(line)
        if not match:
            continue
        title = match.group("title").strip()
        href = match.group("href").strip()
        if href in seen:
            continue
        seen.add(href)
        entries.append((title, href))
    return entries


def _strip_markdown_noise(text: str) -> str:
    """去掉标题标记、分隔线与表格/图片行，但**保持行号不变**。

    行号必须与原文严格对齐，否则 reflect 阶段引用的 ``文件:行号`` 就是错的。
    因此这里只做「整行替换为空串」级别的清洗，绝不删除行。
    """
    out: list[str] = []
    for line in text.splitlines():
        if DIVIDER_RE.match(line) or NON_PROSE_RE.match(line):
            out.append("")
            continue
        out.append(MARKDOWN_HEADING_RE.sub("", line))
    return "\n".join(out)


def _doc_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return MARKDOWN_HEADING_RE.sub("", stripped).strip() or fallback
        break
    return fallback


def load_volume(directory: str | Path, *, include_extras: bool = False) -> VolumeSource:
    """读取一个卷目录。

    Args:
        directory: 形如 ``.../re0-ch/chapter020`` 的目录。
        include_extras: 是否纳入 ``99.md`` 这类后记/插图条目。默认排除，
            因为它们不含可改编的剧情。

    Raises:
        CorpusError: 目录不存在、README 缺失或解析不出任何正篇。
    """
    root = Path(directory)
    if not root.is_dir():
        raise CorpusError(f"素材目录不存在：{root}")

    readme = root / "README.md"
    if not readme.is_file():
        raise CorpusError(f"缺少 README.md（卷目录的唯一顺序来源）：{readme}")

    readme_text = _read_text(readme)
    arc_title = ""
    match = ARC_TITLE_RE.search(readme_text)
    if match:
        arc_title = match.group("title").strip()

    entries = _parse_toc(readme_text)
    if not entries:
        raise CorpusError(f"README.md 里解析不到任何篇目链接：{readme}")

    docs: list[SourceDoc] = []
    for title, href in entries:
        if not include_extras and _is_extra(href, title):
            continue
        path = root / href
        if not path.is_file():
            # 目录里列了但文件缺失——跳过而不是整体失败，便于处理不完整的语料。
            continue
        raw = _read_text(path)
        cleaned = _strip_markdown_noise(raw)
        docs.append(
            SourceDoc(
                index=len(docs),
                title=_doc_title(raw, title),
                path=path,
                rel_path=href,
                text=cleaned,
            )
        )

    if not docs:
        raise CorpusError(f"目录里没有可读的正篇：{root}")

    return VolumeSource(slug=root.name, directory=root, arc_title=arc_title, docs=docs)


def _is_extra(href: str, title: str) -> bool:
    """后记 / 插图 / 幕间等非正篇条目。"""
    stem = href.split(".")[0]
    if stem == "99":
        return True
    for keyword in ("后记", "插图", "附录", "版权"):
        if keyword in title:
            return True
    return False


def chunk_volume(
    source: VolumeSource,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Passage]:
    """把整卷切成带行号的片段。

    按**段落边界**累积，不做定长硬切——硬切会把一句话劈开，既伤检索也伤引用。
    段落超过 ``chunk_chars`` 时按句末标点回退切分。
    """
    passages: list[Passage] = []
    for doc in source.docs:
        lines = doc.lines
        # 先找出所有非空段落的 (起始行, 结束行, 文本)
        paragraphs: list[tuple[int, int, str]] = []
        start: int | None = None
        buffer: list[str] = []
        for offset, line in enumerate(lines):
            if line.strip():
                if start is None:
                    start = offset
                buffer.append(line)
            elif start is not None:
                paragraphs.append((start, offset - 1, "\n".join(buffer).strip()))
                start, buffer = None, []
        if start is not None:
            paragraphs.append((start, len(lines) - 1, "\n".join(buffer).strip()))

        current: list[tuple[int, int, str]] = []
        current_len = 0

        def emit(start_line: int, end_line: int, body: str) -> None:
            """落一块。

            ``body`` 对未切分的块是**原文行区间的精确切片**（含段间空行），
            这样 ``verify_citation`` 才能用 ``lines[start-1:end]`` 逐字回验；
            对超长段落切出来的片，它是该区间的子串——两种情况下"引用是区间文本
            的子串"都成立。
            """
            text = body.strip("\n")
            if not text.strip():
                return
            passages.append(
                Passage(
                    doc_index=doc.index,
                    doc_title=doc.title,
                    file=doc.rel_path,
                    line_start=start_line + 1,  # 1-based，便于人读
                    line_end=end_line + 1,
                    text=text,
                )
            )

        def flush() -> None:
            nonlocal current, current_len
            if not current:
                return
            start_line = current[0][0]
            end_line = current[-1][1]
            emit(start_line, end_line, "\n".join(lines[start_line : end_line + 1]))
            # 保留尾部若干段落做重叠，避免跨块线索被切断
            if overlap > 0:
                tail: list[tuple[int, int, str]] = []
                tail_len = 0
                for item in reversed(current):
                    if tail_len >= overlap:
                        break
                    tail.insert(0, item)
                    tail_len += len(item[2])
                current = tail
                current_len = tail_len
            else:
                current, current_len = [], 0

        for para in paragraphs:
            start_line, end_line, body = para
            if len(body) > chunk_chars:
                # 超长段落：按句末标点回退切。切出的片无法对应完整行，
                # 因此每片沿用该段落自身的行区间，文本是区间的子串。
                for piece_start, piece_end, piece in _split_long_paragraph(
                    start_line, end_line, body, chunk_chars
                ):
                    current.append((piece_start, piece_end, piece))
                    current_len += len(piece)
                    if current_len >= chunk_chars:
                        flush()
                continue
            if current_len + len(body) > chunk_chars and current:
                flush()
            current.append(para)
            current_len += len(body)

        # 收尾：清掉重叠残留后落最后一块
        flush()
        # flush() 会把尾块也落盘（current 非空时），无需重复

    # 相邻块可能因重叠而完全重复，去重保持稳定顺序
    unique: list[Passage] = []
    seen: set[tuple[str, int, int]] = set()
    for passage in passages:
        key = (passage.file, passage.line_start, passage.line_end)
        if key in seen:
            continue
        seen.add(key)
        unique.append(passage)
    return unique


_SENTENCE_END = "。！？…"


def _split_long_paragraph(
    start_line: int, end_line: int, body: str, chunk_chars: int
) -> list[tuple[int, int, str]]:
    """把超长段落按句末标点切成若干片，行号按比例回推（近似但单调）。"""
    pieces: list[str] = []
    buffer = ""
    for char in body:
        buffer += char
        if char in _SENTENCE_END and len(buffer) >= chunk_chars:
            pieces.append(buffer)
            buffer = ""
    if buffer.strip():
        pieces.append(buffer)

    if len(pieces) <= 1:
        return [(start_line, end_line, body)]

    span = max(1, end_line - start_line + 1)
    total = sum(len(p) for p in pieces) or 1
    out: list[tuple[int, int, str]] = []
    cursor = start_line
    for piece in pieces:
        length = round(span * len(piece) / total)
        piece_end = min(end_line, cursor + max(0, length - 1))
        out.append((cursor, piece_end, piece))
        cursor = piece_end + 1
    # 末片对齐到段落末尾，保证覆盖完整
    if out:
        out[-1] = (out[-1][0], end_line, out[-1][2])
    return out


def looks_like_volume(directory: str | Path) -> bool:
    """一个目录像不像"卷"：有 README.md，且至少有一篇编号正文。

    只看 README 有没有正篇链接——``re0-7`` 那种散装 HTML 目录会被排除掉。
    """
    path = Path(directory)
    readme = path / "README.md"
    if not readme.is_file():
        return False
    try:
        return bool(_parse_toc(_read_text(readme)))
    except OSError:
        return False


def list_novels(root: str | Path) -> list[dict[str, object]]:
    """列出一部部**小说**（每部下含若干卷），供面板两级选择。

    语料根有两种摆法，两种都要认：

    - ``<root>/<小说>/<卷>/README.md``——多部小说并排（推荐）；
    - ``<root>/<卷>/README.md``——根目录本身就是唯一一部小说。

    后者是兼容老配置的退路：直接把 ``corpus_root`` 指到某一部的目录上，
    面板会把它当成"只有一部小说"，不会因为层数对不上而列空。
    """
    base = Path(root)
    if not base.is_dir():
        return []

    novels: list[dict[str, object]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        # **它自己就是一卷的目录，不是"一部"**。
        # re0-ch/chapter999 既有一份 README，又装着若干子卷（EX 篇），
        # 不加这条判断它会被当成一部小说，把整卷的层级关系搞乱。
        if looks_like_volume(child):
            continue
        volumes = _volume_children(child)
        if not volumes:
            continue
        novels.append(_novel_entry(child, volumes))
    if novels:
        return novels

    # 退路：根目录自己就是一部小说（corpus_root 直接指向某一部）。
    volumes = _volume_children(base)
    return [_novel_entry(base, volumes)] if volumes else []


def _volume_children(directory: Path) -> list[Path]:
    return [
        sub for sub in sorted(directory.iterdir())
        if sub.is_dir() and looks_like_volume(sub)
    ]


def _novel_entry(directory: Path, volumes: Sequence[Path]) -> dict[str, object]:
    return {
        "slug": directory.name,
        "title": _novel_title(directory),
        "directory": str(directory),
        "volume_count": len(volumes),
    }


def _novel_title(directory: Path) -> str:
    """给一"部"起标题：优先 README 的一级标题，其次目录名。"""
    readme = directory / "README.md"
    if readme.is_file():
        try:
            match = ARC_TITLE_RE.search(_read_text(readme))
            if match:
                title = match.group("title").strip()
                if title:
                    return title
        except OSError:
            pass
    return directory.name


def list_volumes(root: str | Path) -> list[dict[str, object]]:
    """列出语料根目录下所有可用卷，供面板下拉选择。"""
    base = Path(root)
    if not base.is_dir():
        return []
    items: list[dict[str, object]] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        readme = child / "README.md"
        if not readme.is_file():
            continue
        title = ""
        try:
            text = _read_text(readme)
            match = ARC_TITLE_RE.search(text)
            if match:
                title = match.group("title").strip()
            entries = _parse_toc(text)
        except OSError:
            entries = []
        items.append(
            {
                "slug": child.name,
                "title": title or child.name,
                "directory": str(child),
                "doc_count": len(entries),
            }
        )
    return items
