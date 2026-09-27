"""反省机制：拿原文核对生成内容，并把结论落成具体修订。

为什么不能只靠模型自省
----------------------
让模型"自己检查一下写得对不对"是没有意义的——它检查时用的是同一套先验知识，
错的地方会以同样的方式再错一遍。所以这里的反省必须**有外部依据**：

1. 把草稿拆成一条条**可证伪的断言**（不是笼统地"评审整个包"）；
2. 为每条断言**检索原文**（词法检索，全离线）；
3. 让模型**只看检索到的段落**做判断；
4. **逐字回验**它给出的引用；核验不过的，结论作废并降级。

三层防线里，第 4 层是唯一不依赖模型自觉的一层。实测已确认它能挡住
「行号完全正确但内容是编的」这种最典型的幻觉形态。

分工：模型只输出**判断与建议**，实际修改由 Python 按 JSON 指针落实。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .llm import WorldgenLLM
from .models import Citation, ReflectionFinding
from .steps import validate_coverage, validate_reflect

LOGGER = logging.getLogger(__name__)

#: 单轮参与核对的断言上限。超过就分批，避免一次塞爆上下文。
MAX_CLAIMS_PER_BATCH = 12
#: 每条断言检索几段原文。
PASSAGES_PER_CLAIM = 4


# --- JSON 指针 ------------------------------------------------------------


def json_pointer_get(document: Any, pointer: str) -> Any:
    """按 ``/a/b/0/c`` 取值。取不到返回 ``None``。"""
    if pointer in ("", "/"):
        return document
    node = document
    for raw in pointer.strip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            try:
                index = int(token)
            except ValueError:
                return None
            if index < 0 or index >= len(node):
                return None
            node = node[index]
        else:
            return None
    return node


def json_pointer_set(document: Any, pointer: str, value: Any) -> bool:
    """按指针写值。成功返回 True。

    **由 Python 执行**——模型只给建议，不直接改草稿。这样即使模型给出越界指针，
    最坏结果也只是改不动，而不是把结构改坏。
    """
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.strip("/").split("/")]
    if not parts:
        return False
    node = document
    for raw in parts[:-1]:
        if isinstance(node, Mapping):
            if raw not in node:
                return False
            node = node[raw]
        elif isinstance(node, list):
            try:
                index = int(raw)
            except ValueError:
                return False
            if index < 0 or index >= len(node):
                return False
            node = node[index]
        else:
            return False

    last = parts[-1]
    if isinstance(node, Mapping):
        node[last] = value
        return True
    if isinstance(node, list):
        try:
            index = int(last)
        except ValueError:
            return False
        if index < 0 or index >= len(node):
            return False
        node[index] = value
        return True
    return False


# --- 断言抽取 -------------------------------------------------------------


@dataclass
class Claim:
    """一条可证伪的断言，绑定到草稿里的一个具体位置。"""

    claim_id: str
    path: str
    text: str
    kind: str
    #: 覆盖类断言专用：它要核对的**目标情节**（``MergedBeat.beat_id``）与出处。
    beat_id: str = ""
    from_segments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "claim_id": self.claim_id,
            "path": self.path,
            "text": self.text,
            "kind": self.kind,
        }
        if self.beat_id:
            data["beat_id"] = self.beat_id
        return data


def extract_claims(world: Mapping[str, Any], npcs: Mapping[str, Any] | None = None) -> list[Claim]:
    """**确定性**地遍历草稿，抽出所有值得核对的断言。

    不用模型做这一步——让模型挑"哪些内容需要检查"，它只会挑自己有把握的那些。
    """
    claims: list[Claim] = []

    def add(path: str, text: str, kind: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        claims.append(Claim(f"c{len(claims) + 1:03d}", path, body, kind))

    rules = world.get("rules") if isinstance(world.get("rules"), Mapping) else {}
    progress = rules.get("progress") if isinstance(rules.get("progress"), Mapping) else {}
    chapters = progress.get("chapters") or []

    for ci, chapter in enumerate(chapters):
        if not isinstance(chapter, Mapping):
            continue
        base = f"/rules/progress/chapters/{ci}"
        add(f"{base}/title", chapter.get("title"), "chapter_title")
        add(f"{base}/current_objective", chapter.get("current_objective"), "objective")
        add(f"{base}/pacing_directive", chapter.get("pacing_directive"), "pacing")

        for mi, milestone in enumerate(chapter.get("milestones") or []):
            if isinstance(milestone, Mapping):
                add(
                    f"{base}/milestones/{mi}/label",
                    milestone.get("label"),
                    "milestone",
                )
        for ni, npc in enumerate(chapter.get("key_npcs") or []):
            if isinstance(npc, Mapping):
                add(f"{base}/key_npcs/{ni}/role", npc.get("role"), "npc_role")

    initial = world.get("initial_state") if isinstance(world.get("initial_state"), Mapping) else {}
    for fi, fact in enumerate(initial.get("facts") or []):
        add(f"/initial_state/facts/{fi}", fact, "fact")

    if isinstance(npcs, Mapping):
        for ii, item in enumerate(npcs.get("items") or []):
            if not isinstance(item, Mapping):
                continue
            profile = item.get("profile") if isinstance(item.get("profile"), Mapping) else {}
            base = f"/items/{ii}"
            add(f"{base}/profile/identity", profile.get("identity"), "npc_identity")
            add(f"{base}/profile/public_background", profile.get("public_background"), "npc_background")
            add(f"{base}/prompt", item.get("prompt"), "npc_prompt")

    return claims


def coverage_claims(
    world: Mapping[str, Any],
    timeline: Any,
) -> list[Claim]:
    """把缝好的线里**每条必现情节**变成一条待核对断言。

    为什么要单独造这一批：上面的 :func:`extract_claims` 是**遍历草稿**抽断言的，
    所以被整段丢掉的剧情产出**零条断言**，永远进不了反思视野——反思只会说
    "你写的这句和原文矛盾"，说不出"你少写了整条线"。这是设计上的盲区。

    这里反过来，从**原作侧**造断言：这条情节该出现，它出现了吗？
    模型拿草稿里的章节/目标/里程碑去比，答 ``covered`` / ``distorted`` /
    ``omitted``。``omitted`` 不靠改字能修，所以不会被自动修复，只能回到审批门。
    """
    claims: list[Claim] = []
    required = getattr(timeline, "required_beats", None) or []
    if not required:
        return claims

    # 把草稿的可读摘要拼成一段，供模型对照"这条情节有没有落点"
    digest: list[str] = []
    rules = world.get("rules") if isinstance(world.get("rules"), Mapping) else {}
    progress = rules.get("progress") if isinstance(rules.get("progress"), Mapping) else {}
    for chapter in progress.get("chapters") or []:
        if not isinstance(chapter, Mapping):
            continue
        digest.append(f"【章】{chapter.get('title')}")
        digest.append(f"  目标：{chapter.get('current_objective')}")
        for milestone in chapter.get("milestones") or []:
            if isinstance(milestone, Mapping):
                digest.append(f"  里程碑：{milestone.get('label')}")
    body = "\n".join(digest) or "（草稿里没有任何章节）"

    for offset, beat in enumerate(required):
        claims.append(
            Claim(
                # 独立命名空间（cov 前缀），与一致性断言的 c 前缀分开——
                # 两批断言的编号各自从 1 起，混在一起会串号。
                claim_id=f"cov{offset + 1:03d}",
                path=f"/rules/progress/chapters/*#beat:{beat.beat_id}",
                # 断言正文 = 这条情节 + 待对照的草稿摘要
                text=f"必现情节「{beat.label}」——草稿里是否已有对应落点？\n\n草稿章节：\n{body}",
                kind="coverage",
                beat_id=beat.beat_id,
                from_segments=list(beat.from_segments),
            )
        )
    return claims


# --- 核对 ---------------------------------------------------------------


@dataclass
class ReflectionReport:
    """一轮反省的结论。"""

    mode: str = "source"
    rounds: int = 0
    findings: list[ReflectionFinding] = field(default_factory=list)
    claims_checked: int = 0
    citations_verified: int = 0
    citations_fabricated: int = 0
    applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 覆盖检查核过几条必现情节。
    coverage_checked: int = 0
    #: **被漏掉的关键情节**。这几条修不了，只能推回审批门让人定夺。
    coverage_omitted: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocking(self) -> list[ReflectionFinding]:
        return [f for f in self.findings if f.severity == "blocking"]

    @property
    def omissions(self) -> list[ReflectionFinding]:
        return [f for f in self.findings if f.coverage_verdict == "omitted"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "rounds": self.rounds,
            "claims_checked": self.claims_checked,
            "citations_verified": self.citations_verified,
            "citations_fabricated": self.citations_fabricated,
            "applied": list(self.applied),
            "warnings": list(self.warnings),
            "coverage_checked": self.coverage_checked,
            "coverage_omitted": list(self.coverage_omitted),
            "findings": [f.to_dict() for f in self.findings],
        }


def _build_queries(claims: Sequence[Claim]) -> dict[str, str]:
    """为每条断言生成检索词。

    **确定性的关键词抽取**，不让模型生成查询：模型生成的查询往往带上它自己
    以为的答案，反而检索到"印证自己"的段落。这里直接用断言本身的词面。
    """
    import re

    queries: dict[str, str] = {}
    for claim in claims:
        # 取断言里的实词片段（去标点、去空白），拼成检索词
        tokens = re.findall(r"[一-鿿]{2,6}", claim.text)
        queries[claim.claim_id] = " ".join(tokens[:12]) or claim.text[:40]
    return queries


def _retrieve_for_claims(
    claims: Sequence[Claim],
    retriever: Any,
    queries: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    passages_by_claim: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        query = queries.get(claim.claim_id) or claim.text[:40]
        hits = retriever.search(query, top_k=PASSAGES_PER_CLAIM)
        passages_by_claim[claim.claim_id] = [
            {
                "rel_path": hit.passage.file,
                "line_start": hit.passage.line_start,
                "line_end": hit.passage.line_end,
                "text": hit.passage.text,
            }
            for hit in hits
        ]
    return passages_by_claim


def _verdict_to_finding(
    raw: Mapping[str, Any],
    claim: Claim,
    retriever: Any,
) -> tuple[ReflectionFinding, bool]:
    """把模型的判定转成 finding，并**逐字回验**它的引用。

    Returns:
        ``(finding, citation_ok)``。``citation_ok=False`` 表示引用核验失败，
        该结论必须降级——不得据编造的出处推翻任何内容。
    """
    verdict = str(raw.get("verdict") or "unsupported").strip()
    severity = str(raw.get("severity") or "info").strip()
    reason = str(raw.get("reason") or "").strip()

    source = raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
    citation = Citation(
        rel_path=str(source.get("rel_path") or ""),
        line_start=int(source.get("line_start") or 0),
        line_end=int(source.get("line_end") or 0),
        quote=str(source.get("quote") or ""),
    )
    has_source = bool(citation.rel_path and citation.quote)

    citation_ok = False
    if has_source:
        check = retriever.verify_citation(citation)
        citation_ok = bool(check.ok)

    finding = ReflectionFinding(
        claim_path=claim.path,
        claim_text=claim.text,
        verdict=verdict,
        severity=severity,
        reason=reason,
        citations=[citation] if has_source else [],
    )

    # 判"与原文矛盾"却没有可核验的引用 → 降级为"依据不足"
    if verdict == "contradicted" and not citation_ok:
        finding.verdict = "unsupported"
        finding.fabricated_citation = has_source
        finding.severity = "warning" if severity == "blocking" else severity
        finding.reason = (
            (reason + " " if reason else "")
            + "[引用核验失败，结论降级：不得据未经核实的出处推翻生成内容]"
        )
    elif not citation_ok and has_source:
        finding.fabricated_citation = True

    suggested = raw.get("suggested_fix")
    if isinstance(suggested, Mapping) and suggested.get("path"):
        finding.suggested_fix = dict(suggested)

    return finding, citation_ok


async def reflect_source(
    *,
    llm: WorldgenLLM,
    retriever: Any,
    world: dict[str, Any],
    npcs: dict[str, Any] | None = None,
    rounds: int = 2,
    autofix_warnings: bool = False,
    preferred_provider: str = "",
    timeline: Any = None,
) -> ReflectionReport:
    """改编路径的反省：拿原文核对草稿。

    会**原地修改** ``world`` / ``npcs``：把通过核验的 ``blocking`` 建议落实。
    """
    report = ReflectionReport(mode="source")
    npcs_ref = npcs if isinstance(npcs, dict) else {}

    for round_index in range(1, max(1, rounds) + 1):
        claims = extract_claims(world, npcs_ref)
        if not claims:
            break

        pending = [
            f for f in report.findings if f.verdict in {"contradicted", "unsupported"}
        ]
        # 第二轮起只复核上一轮有问题的位置，避免重复烧 token
        if round_index > 1 and pending:
            bad_paths = {f.claim_path for f in pending}
            claims = [c for c in claims if c.path in bad_paths]
            if not claims:
                break

        report.rounds = round_index
        queries = _build_queries(claims)

        for start in range(0, len(claims), MAX_CLAIMS_PER_BATCH):
            batch = claims[start : start + MAX_CLAIMS_PER_BATCH]
            passages = _retrieve_for_claims(batch, retriever, queries)
            if not any(passages.values()):
                report.warnings.append(
                    "本批断言一条原文都没检索到，跳过核对（不猜）"
                )
                continue
            try:
                payload, _, problems = await llm.generate_json(
                    system_prompt=prompts.REFLECT_SYSTEM,
                    prompt=prompts.reflect_prompt(
                        claims=[c.to_dict() for c in batch],
                        passages_by_claim=passages,
                        # 玩家取代了谁：核查员据此把「原文里是他做的」判成既定前提，
                        # 而不是矛盾——否则改写会被自动修正回原作主角。
                        replaced_names=getattr(timeline, "replaced_characters", []),
                    ),
                    validate=validate_reflect,
                    request_type="worldgen_reflect",
                    preferred_provider=preferred_provider,
                )
            except Exception as exc:
                report.warnings.append(f"反省调用失败，跳过本批：{exc}")
                continue
            if problems:
                report.warnings.append("反省输出未过校验：" + "；".join(problems[:3]))

            by_id = {c.claim_id: c for c in batch}
            for raw in payload.get("findings") or []:
                if not isinstance(raw, Mapping):
                    continue
                claim = by_id.get(str(raw.get("claim_id") or ""))
                if claim is None:
                    continue
                finding, citation_ok = _verdict_to_finding(raw, claim, retriever)
                if citation_ok:
                    report.citations_verified += 1
                if finding.fabricated_citation:
                    report.citations_fabricated += 1
                report.findings.append(finding)

            report.claims_checked += len(batch)

    # --- 覆盖检查：拿缝好的线当清单，查"少了什么" ---
    if timeline is not None:
        await _check_coverage(llm=llm, world=world, timeline=timeline, report=report,
                              preferred_provider=preferred_provider)

    # --- 落实修订（由 Python 执行）---
    _apply_fixes(world, npcs_ref, report, autofix_warnings=autofix_warnings)
    return report


async def _check_coverage(
    *,
    llm: WorldgenLLM,
    world: dict[str, Any],
    timeline: Any,
    report: ReflectionReport,
    preferred_provider: str = "",
) -> None:
    """逐条核对"必现情节"在草稿里有没有落点。

    与上面的核对是**互补**的，不是重复：上面查"你写的对不对"（断言由草稿遍历而来，
    漏写的产出零条断言，永远查不到），这里查"你少写了什么"（清单由原作侧给出）。
    """
    claims = coverage_claims(world, timeline)
    if not claims:
        return
    report.claims_checked += len(claims)

    for start in range(0, len(claims), MAX_CLAIMS_PER_BATCH):
        batch = claims[start : start + MAX_CLAIMS_PER_BATCH]
        try:
            payload, _, problems = await llm.generate_json(
                system_prompt=prompts.COVERAGE_SYSTEM,
                prompt=prompts.coverage_prompt(claims=[c.to_dict() for c in batch]),
                validate=validate_coverage,
                request_type="worldgen_coverage",
                preferred_provider=preferred_provider,
            )
        except Exception as exc:
            report.warnings.append(f"覆盖检查失败，跳过本批：{exc}")
            continue
        if problems:
            report.warnings.append("覆盖检查输出未过校验：" + "；".join(problems[:3]))

        by_id = {c.claim_id: c for c in batch}
        for raw in payload.get("findings") or []:
            if not isinstance(raw, Mapping):
                continue
            claim = by_id.get(str(raw.get("claim_id") or ""))
            if claim is None:
                continue
            verdict = str(raw.get("verdict") or "omitted").strip()
            if verdict not in {"covered", "distorted", "omitted"}:
                verdict = "omitted"
            report.findings.append(
                ReflectionFinding(
                    claim_path=claim.path,
                    claim_text=claim.text,
                    # 复用同一套 verdict 词表：遗漏就是"草稿与应有的内容矛盾"
                    verdict="contradicted" if verdict == "omitted" else "supported",
                    severity={
                        "omitted": "blocking",
                        "distorted": "warning",
                        "covered": "info",
                    }[verdict],
                    reason=str(raw.get("reason") or ""),
                    coverage_verdict=verdict,
                    beat_id=claim.beat_id,
                )
            )
            report.coverage_checked += 1
            if verdict == "omitted":
                report.coverage_omitted.append(
                    {
                        "beat_id": claim.beat_id,
                        "label": claim.text.split("\n", 1)[0],
                        "from_segments": list(claim.from_segments),
                        "reason": str(raw.get("reason") or ""),
                        "suggestion": str(raw.get("suggestion") or ""),
                    }
                )


def _apply_fixes(
    world: dict[str, Any],
    npcs: dict[str, Any],
    report: ReflectionReport,
    *,
    autofix_warnings: bool,
) -> None:
    """把建议落到草稿上。

    只有 ``blocking``（以及与原文明确冲突）的才自动改；``warning`` 默认只报告，
    除非显式开启。全部通过 JSON 指针写入，指针无效就跳过——最坏结果是改不动，
    而不是把结构改坏。
    """
    for finding in report.findings:
        # 覆盖类结论**永远不自动修复**：一条情节被漏掉，改几个字是补不回来的，
        # 只能加章节或由操作者确认放弃。这里显式挡一道，免得日后有人给覆盖
        # 判定也挂上 suggested_fix，把它"修"成一句敷衍过去的话。
        if finding.is_coverage:
            continue
        if finding.severity != "blocking" and not (
            autofix_warnings and finding.severity == "warning"
        ):
            continue
        fix = finding.suggested_fix
        if not fix or not fix.get("path"):
            continue
        # 只有核验通过的"与原文矛盾"才允许自动改写
        if finding.verdict != "contradicted" or finding.fabricated_citation:
            continue
        path = str(fix["path"])
        target = world if path.startswith("/rules") or path.startswith("/initial_state") else npcs
        if json_pointer_set(target, path, fix.get("value")):
            finding.applied = True
            report.applied.append(path)


def summarize(report: ReflectionReport) -> str:
    """给面板/日志用的一行摘要。"""
    parts = [
        f"核对 {report.claims_checked} 条断言",
        f"引用通过 {report.citations_verified}",
    ]
    if report.citations_fabricated:
        parts.append(f"引用编造 {report.citations_fabricated}")
    if report.blocking:
        parts.append(f"阻断级 {len(report.blocking)}")
    if report.applied:
        parts.append(f"已自动修订 {len(report.applied)}")
    return "，".join(parts)
