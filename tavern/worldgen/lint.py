"""世界包确定性静态校验（不调用任何模型）。

为什么需要这个模块
------------------
``tavern/world_preflight.py::inspect_world_package`` 是插件现有的体检门，但它对
章节/里程碑几乎不做结构校验——只在收集 ``key_npcs`` 时顺带读了 ``chapters``
（world_preflight.py:287-303）。以下检查在现有体检里**全部缺失**，导致坏包静默通过：

- 里程碑 id 是否带 ``m_XX_YY`` 数字段（引擎靠它解析章节归属）；
- ``exits_when.all_milestones`` 是否引用本章已定义的里程碑；
- ``next_chapter_id`` 是否悬空（悬空 = 永不切章）；
- ``progress.total_milestones`` 与实际里程碑数量是否一致；
- 章节/里程碑 id 是否重复；
- ``min_turns`` / ``max_turns`` 是否为正整数且 ``min <= max``。

这些正是 ``worlds/WORLD_GENERATOR_PROMPT.md`` §1.4「必须避开的坑」里记录过的
真实线上故障。本模块把它们从「文档里的告诫」变成「代码里的门禁」。

返回值形状与 ``world_preflight._issue`` 保持一致
（``{level, path, code, message, detail}``），便于面板统一渲染两套报告。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

# --- 常量：与协议/文档对齐 -------------------------------------------------

CHAPTER_ID_RE = re.compile(r"^ch_\d{2}_[a-z0-9_]+$")
MILESTONE_ID_RE = re.compile(r"^m_(\d{2})_(\d{2})_[a-z0-9_]+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CHAPTER_NUMBER_RE = re.compile(r"^ch_(\d{2})_")

MAX_PACKAGE_BYTES = 200 * 1024
MAX_OPENING_SCENE_CHARS = 500
MAX_SYSTEM_PROMPT_CHARS = 30000
MAX_COLLECTIVE_CHOICES = 2
MIN_PROFESSIONS = 6
EXPECTED_BASE_TOTAL = 50
EXPECTED_EFFECTIVE_TOTAL = 60

REQUIRED_TOP_KEYS = (
    "world_schema_version",
    "minimum_plugin_version",
    "protocol",
    "slug",
    "name",
    "description",
    "system_prompt",
    "opening_scene",
    "rules",
    "initial_state",
)

# 泛词：写进 evidence 的 match 里等于没写，无法把「计划/提及」与「真正完成」区分开。
VAGUE_EVIDENCE_WORDS = (
    "调查",
    "了解",
    "尝试",
    "准备",
    "思考",
    "询问",
    "前往",
    "发现",
    "探索",
    "关注",
)

# ``key_npcs[].state`` 里一旦出现这些键，就有覆盖玩家已取得成果的风险。
# 文档明确要求：不要预写可能与玩家成果冲突的固定地点、生死或立场。
CLOBBERING_STATE_KEYS = (
    "location",
    "alive",
    "dead",
    "status",
    "health",
    "hp",
    "whereabouts",
    "state",
)


def _issue(
    level: str,
    path: str,
    code: str,
    message: str,
    *,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "level": level,
        "path": path,
        "code": code,
        "message": message,
        "detail": dict(detail or {}),
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


# --- 各段校验 -------------------------------------------------------------


def _lint_envelope(world: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    for key in REQUIRED_TOP_KEYS:
        if key not in world:
            issues.append(
                _issue("error", "$", "envelope.missing_key", f"缺少必填顶层字段：{key}")
            )

    if world.get("world_schema_version") != 5:
        issues.append(
            _issue(
                "error",
                "world_schema_version",
                "envelope.schema_version",
                f"world_schema_version 必须为 5，实际为 {world.get('world_schema_version')!r}",
            )
        )

    version = _text(world.get("minimum_plugin_version"))
    if version and version.lstrip("v") != "0.12.0":
        issues.append(
            _issue(
                "warning",
                "minimum_plugin_version",
                "envelope.min_plugin_version",
                f"minimum_plugin_version 期望 v0.12.0，实际为 {version!r}",
            )
        )

    slug = _text(world.get("slug"))
    if not slug:
        issues.append(_issue("error", "slug", "envelope.slug_missing", "slug 不能为空"))
    elif not SLUG_RE.match(slug):
        issues.append(
            _issue(
                "error",
                "slug",
                "envelope.slug_format",
                f"slug 只能用小写字母/数字/下划线/连字符且不超过 64 字符：{slug!r}",
            )
        )
    if len(slug) > 40:
        issues.append(
            _issue(
                "warning",
                "slug",
                "envelope.slug_long",
                f"slug 偏长（{len(slug)} 字符），建议不超过 40",
            )
        )

    prompt = _text(world.get("system_prompt"))
    if not prompt:
        issues.append(
            _issue("error", "system_prompt", "envelope.system_prompt_empty", "system_prompt 不能为空")
        )
    elif len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
        issues.append(
            _issue(
                "warning",
                "system_prompt",
                "envelope.system_prompt_too_long",
                f"system_prompt 超过 {MAX_SYSTEM_PROMPT_CHARS} 字符（{len(prompt)}），常驻上下文会被稀释",
            )
        )


def _lint_progress(
    world: Mapping[str, Any], issues: list[dict[str, Any]]
) -> tuple[list[Any], dict[str, list[str]]]:
    """校验 progress 骨架，返回 (chapters 列表, {chapter_id: [milestone_id]})。"""
    rules = _as_mapping(world.get("rules"))
    progress = _as_mapping(rules.get("progress"))
    if not progress:
        issues.append(
            _issue(
                "error",
                "rules.progress",
                "progress.missing",
                "缺少 rules.progress——切章机制完全不会工作",
            )
        )
        return [], {}

    chapters = _as_sequence(progress.get("chapters"))
    if not chapters:
        issues.append(
            _issue(
                "error",
                "rules.progress.chapters",
                "progress.no_chapters",
                "rules.progress.chapters 为空或缺失",
            )
        )
        return [], {}

    chapter_milestones: dict[str, list[str]] = {}
    seen_chapter_ids: set[str] = set()

    for index, raw in enumerate(chapters):
        base = f"rules.progress.chapters[{index}]"
        chapter = _as_mapping(raw)
        if not chapter:
            issues.append(
                _issue("error", base, "chapter.not_object", "章节必须是对象")
            )
            continue

        cid = _text(chapter.get("id"))

        # --- 章节 id 格式与唯一性 ---
        if not cid:
            issues.append(_issue("error", f"{base}.id", "chapter.id_missing", "章节缺少 id"))
        else:
            if not CHAPTER_ID_RE.match(cid):
                issues.append(
                    _issue(
                        "error",
                        f"{base}.id",
                        "chapter.id_format",
                        f"章节 id 需形如 ch_XX_slug（小写），实际 {cid!r}；"
                        "缺数字段会让引擎无法定位章节序号",
                    )
                )
            if cid in seen_chapter_ids:
                issues.append(
                    _issue("error", f"{base}.id", "chapter.id_duplicate", f"章节 id 重复：{cid}")
                )
            seen_chapter_ids.add(cid)

        # --- 体验门槛 ---
        min_turns = chapter.get("min_turns")
        max_turns = chapter.get("max_turns")
        if not isinstance(min_turns, int) or isinstance(min_turns, bool):
            # 省略时引擎按最低 1 回合处理（比作者预期更宽松，不会卡死）——warning。
            issues.append(
                _issue(
                    "warning",
                    f"{base}.min_turns",
                    "chapter.min_turns_missing",
                    "未显式声明正整数 min_turns。引擎会按最低 1 回合处理，"
                    "短于作者预期的体验门槛——建议各章显式写明",
                )
            )
        elif min_turns < 1:
            issues.append(
                _issue("error", f"{base}.min_turns", "chapter.min_turns_invalid", "min_turns 必须 >= 1")
            )
        if isinstance(max_turns, int) and not isinstance(max_turns, bool):
            if max_turns < 1:
                issues.append(
                    _issue("error", f"{base}.max_turns", "chapter.max_turns_invalid", "max_turns 必须 >= 1")
                )
            elif isinstance(min_turns, int) and not isinstance(min_turns, bool) and min_turns > max_turns:
                issues.append(
                    _issue(
                        "error",
                        f"{base}",
                        "chapter.turn_range_inverted",
                        f"min_turns({min_turns}) 大于 max_turns({max_turns})",
                    )
                )

        # --- 里程碑 ---
        milestones = _as_sequence(chapter.get("milestones"))
        if not milestones:
            issues.append(
                _issue(
                    "error",
                    f"{base}.milestones",
                    "chapter.no_milestones",
                    "章节没有任何里程碑，无法构成可完成的退出条件",
                )
            )

        chapter_ms_ids: list[str] = []
        chapter_number = CHAPTER_NUMBER_RE.match(cid).group(1) if CHAPTER_NUMBER_RE.match(cid) else ""

        for mi, raw_ms in enumerate(milestones):
            mbase = f"{base}.milestones[{mi}]"
            ms = _as_mapping(raw_ms)
            if not ms:
                issues.append(_issue("error", mbase, "milestone.not_object", "里程碑必须是对象"))
                continue
            mid = _text(ms.get("id"))
            if not mid:
                issues.append(_issue("error", f"{mbase}.id", "milestone.id_missing", "里程碑缺少 id"))
                continue
            chapter_ms_ids.append(mid)

            match = MILESTONE_ID_RE.match(mid)
            if not match:
                # 注意：引擎按**精确字符串**匹配里程碑 id（engine.py:1103
                # _milestone_ledger_completed），并不解析 m_XX_YY 数字段。
                # 因此这是作者指南的命名约定，不是硬性运行要求——降为 warning。
                issues.append(
                    _issue(
                        "warning",
                        f"{mbase}.id",
                        "milestone.id_format",
                        f"里程碑 id {mid!r} 未采用 m_XX_YY_slug 约定。"
                        "引擎按精确字符串匹配，不会因此失效；但数字段便于跨章定位与人工核对，"
                        "建议补成 m_XX_YY_slug",
                    )
                )
            elif chapter_number and match.group(1) != chapter_number:
                issues.append(
                    _issue(
                        "warning",
                        f"{mbase}.id",
                        "milestone.chapter_prefix_mismatch",
                        f"里程碑 {mid} 的数字段与所属章节 {cid} 不一致",
                    )
                )

            label = _text(ms.get("label"))
            if not label:
                issues.append(
                    _issue(
                        "error",
                        f"{mbase}.label",
                        "milestone.label_missing",
                        "里程碑必须写 label：完成的结果及必要限定",
                    )
                )
            elif len(label) < 8:
                issues.append(
                    _issue(
                        "warning",
                        f"{mbase}.label",
                        "milestone.label_too_abstract",
                        f"label 过短（{len(label)} 字），难以区分「计划/提及」与「真正完成」：{label!r}",
                    )
                )

            evidence = _as_sequence(ms.get("evidence_required"))
            if not evidence:
                issues.append(
                    _issue(
                        "warning",
                        f"{mbase}.evidence_required",
                        "milestone.no_evidence",
                        "没有 evidence_required，裁判缺少参考信号",
                    )
                )
            else:
                for ei, raw_ev in enumerate(evidence):
                    ev = _as_mapping(raw_ev)
                    words = [_text(w) for w in _as_sequence(ev.get("match")) if _text(w)]
                    if not words:
                        issues.append(
                            _issue(
                                "warning",
                                f"{mbase}.evidence_required[{ei}]",
                                "milestone.evidence_empty_match",
                                "evidence 条目没有 match 词",
                            )
                        )
                        continue
                    vague = [w for w in words if w in VAGUE_EVIDENCE_WORDS]
                    if vague:
                        issues.append(
                            _issue(
                                "warning",
                                f"{mbase}.evidence_required[{ei}]",
                                "milestone.evidence_vague",
                                f"match 含泛词 {vague}，无法作为完成信号；"
                                "应写正文可能出现的具体动作、物证与结果",
                            )
                        )

            if "auto_complete" in ms:
                issues.append(
                    _issue(
                        "warning",
                        f"{mbase}.auto_complete",
                        "milestone.auto_complete_deprecated",
                        "auto_complete 是旧包兼容字段，不能代替实际事件证据",
                    )
                )

        chapter_milestones[cid or base] = chapter_ms_ids

        # --- exits_when ---
        exits = _as_mapping(chapter.get("exits_when"))
        all_ms = [_text(x) for x in _as_sequence(exits.get("all_milestones")) if _text(x)]
        if not all_ms:
            # 引擎有容错：漏配时回退为「本章全部 milestones 完成才切章」
            # （engine.py:1262-1270，注释明确写着「避免章节永远卡死」）。
            # 所以不会卡死，但退出条件比作者本意更严，容易滞留——记为 warning。
            issues.append(
                _issue(
                    "warning",
                    f"{base}.exits_when.all_milestones",
                    "chapter.exits_when_missing",
                    "缺少 exits_when.all_milestones。引擎会回退为「本章全部里程碑完成才切章」"
                    "（engine.py:1262-1270），不会卡死，但退出条件比显式声明更严，容易滞留；"
                    "务必显式列出切章前必须完成的里程碑",
                )
            )
        else:
            undefined = [m for m in all_ms if m not in chapter_ms_ids]
            if undefined:
                issues.append(
                    _issue(
                        "error",
                        f"{base}.exits_when.all_milestones",
                        "chapter.exits_when_undefined_milestone",
                        f"exits_when 引用了本章未定义的里程碑：{undefined}——这些门槛永远无法满足",
                    )
                )
            unlisted = [m for m in chapter_ms_ids if m not in all_ms]
            if unlisted:
                issues.append(
                    _issue(
                        "warning",
                        f"{base}.exits_when.all_milestones",
                        "chapter.milestone_not_in_exits",
                        f"本章有里程碑未进入退出集（将不参与切章判定）：{unlisted}",
                    )
                )

        # --- key_npcs ---
        for ni, raw_npc in enumerate(_as_sequence(chapter.get("key_npcs"))):
            nbase = f"{base}.key_npcs[{ni}]"
            npc = _as_mapping(raw_npc)
            if not _text(npc.get("ref")):
                issues.append(
                    _issue("error", f"{nbase}.ref", "npc.ref_missing", "key_npcs 条目缺少 ref")
                )
            if not _text(npc.get("role")):
                issues.append(
                    _issue(
                        "warning",
                        f"{nbase}.role",
                        "npc.role_missing",
                        "key_npcs[].role 应写该 NPC 在本章的动机、作用与承接方式",
                    )
                )
            state = _as_mapping(npc.get("state"))
            clobbering = [k for k in state if k in CLOBBERING_STATE_KEYS]
            if clobbering:
                issues.append(
                    _issue(
                        "warning",
                        f"{nbase}.state",
                        "npc.state_clobber_risk",
                        f"state 预写了 {clobbering}，切章时会覆盖玩家已取得的成果；"
                        "不要预写固定地点、生死或立场",
                    )
                )

    # --- 全局：里程碑 id 唯一性 / 章节图连通性 / 计数一致性 ---
    all_milestone_ids = [m for ids in chapter_milestones.values() for m in ids]
    duplicates = sorted({m for m in all_milestone_ids if all_milestone_ids.count(m) > 1})
    if duplicates:
        issues.append(
            _issue(
                "error",
                "rules.progress.chapters",
                "milestone.id_duplicate",
                f"里程碑 id 跨章节重复：{duplicates}",
            )
        )

    _lint_chapter_graph(progress, chapters, issues)
    _lint_milestone_ledger(progress, chapters, all_milestone_ids, issues)
    return chapters, chapter_milestones


def _lint_chapter_graph(
    progress: Mapping[str, Any],
    chapters: Sequence[Any],
    issues: list[dict[str, Any]],
) -> None:
    """next_chapter_id 悬空 / 缺省 / 章节图可达性——这些都会导致永不切章。"""
    defined = {_text(_as_mapping(c).get("id")) for c in chapters}
    defined.discard("")

    outgoing: dict[str, str] = {}
    terminals: list[str] = []

    for index, raw in enumerate(chapters):
        chapter = _as_mapping(raw)
        cid = _text(chapter.get("id"))
        if not cid:
            continue
        base = f"rules.progress.chapters[{index}]"
        nxt = _text(chapter.get("next_chapter_id"))
        if nxt:
            if nxt not in defined:
                issues.append(
                    _issue(
                        "error",
                        f"{base}.next_chapter_id",
                        "chapter.next_dangling",
                        f"next_chapter_id={nxt!r} 未在 chapters 中定义——该章永不切章",
                    )
                )
            else:
                outgoing[cid] = nxt
        else:
            terminals.append(cid)

    if not terminals:
        issues.append(
            _issue(
                "error",
                "rules.progress.chapters",
                "chapter.no_terminal",
                "没有任何终章（所有章节都指向下一章），章节图成环或无法收束",
            )
        )

    # 从 current_chapter_id 出发能否走到终点
    start = _text(progress.get("current_chapter_id"))
    if start and start not in defined:
        issues.append(
            _issue(
                "error",
                "rules.progress.current_chapter_id",
                "progress.current_chapter_dangling",
                f"current_chapter_id={start!r} 未在 chapters 中定义",
            )
        )
    elif start:
        visited: set[str] = set()
        cursor: str | None = start
        while cursor and cursor not in visited:
            visited.add(cursor)
            cursor = outgoing.get(cursor)
        if cursor:
            issues.append(
                _issue(
                    "error",
                    "rules.progress.chapters",
                    "chapter.cycle",
                    f"章节图从 {start} 出发进入环：{' -> '.join(sorted(visited))}",
                )
            )
        unreachable = sorted(defined - visited)
        if unreachable:
            issues.append(
                _issue(
                    "warning",
                    "rules.progress.chapters",
                    "chapter.unreachable",
                    f"从 current_chapter_id 出发走不到的章节：{unreachable}",
                )
            )


def _lint_milestone_ledger(
    progress: Mapping[str, Any],
    chapters: Sequence[Any],
    all_milestone_ids: list[str],
    issues: list[dict[str, Any]],
) -> None:
    """total_milestones 必须与实际定义数量一致——文档明确点名过这个错误。"""
    declared = progress.get("total_milestones")
    actual = len(all_milestone_ids)
    if isinstance(declared, int) and not isinstance(declared, bool):
        if declared != actual:
            issues.append(
                _issue(
                    "error",
                    "rules.progress.total_milestones",
                    "progress.total_milestones_mismatch",
                    f"total_milestones={declared} 与实际定义数量 {actual} 不一致；"
                    "不要把别的世界包的数字当模板常量",
                    detail={"declared": declared, "actual": actual},
                )
            )
    else:
        issues.append(
            _issue(
                "error",
                "rules.progress.total_milestones",
                "progress.total_milestones_missing",
                "缺少 total_milestones（须与实际里程碑总数一致）",
            )
        )

    completed = progress.get("completed_milestones")
    if completed not in (0, None):
        issues.append(
            _issue(
                "warning",
                "rules.progress.completed_milestones",
                "progress.completed_not_zero",
                f"新包 completed_milestones 初始应为 0，实际 {completed!r}",
            )
        )


def _lint_key_npc_refs(
    chapter_milestones: Mapping[str, list[str]],
    chapters: Sequence[Any],
    world: Mapping[str, Any],
    npc_slugs: Sequence[str] | None,
    issues: list[dict[str, Any]],
) -> None:
    """key_npcs[].ref 必须能对上 NPC 包里的 slug，否则运行时找不到人。"""
    refs: dict[str, str] = {}
    for index, raw in enumerate(chapters):
        for ni, raw_npc in enumerate(_as_sequence(_as_mapping(raw).get("key_npcs"))):
            ref = _text(_as_mapping(raw_npc).get("ref"))
            if ref:
                refs.setdefault(ref, f"rules.progress.chapters[{index}].key_npcs[{ni}].ref")

    if not refs:
        issues.append(
            _issue(
                "warning",
                "rules.progress.chapters",
                "npc.no_key_npcs",
                "所有章节都没有 key_npcs，NPC 无法按章同步状态",
            )
        )
        return

    if npc_slugs is None:
        issues.append(
            _issue(
                "info",
                "rules.progress.chapters",
                "npc.refs_unverified",
                f"未提供 NPC 包，跳过 ref 校验。当前引用了 {len(refs)} 个 NPC：{sorted(refs)}",
            )
        )
        return

    known = {_text(s) for s in npc_slugs}
    known.discard("")
    missing = sorted(r for r in refs if r not in known)
    if missing:
        issues.append(
            _issue(
                "error",
                "rules.progress.chapters",
                "npc.ref_not_found",
                f"key_npcs 引用了 NPC 包中不存在的 slug：{missing}",
                detail={"missing": missing, "known": sorted(known)},
            )
        )
    unused = sorted(known - set(refs))
    if unused:
        issues.append(
            _issue(
                "info",
                "world_npcs",
                "npc.unreferenced",
                f"NPC 包中有 {len(unused)} 个 NPC 未被任何章节引用：{unused}",
            )
        )


def _lint_card(world: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    """建卡规范：职业预设、属性预算、单一自由文本项。"""
    rules = _as_mapping(world.get("rules"))
    card = _as_mapping(rules.get("character_card"))
    if not card:
        issues.append(
            _issue(
                "warning",
                "rules.character_card",
                "card.missing",
                "缺少 rules.character_card，玩家建卡会退化到默认行为",
            )
        )
        return

    stats = _as_mapping(card.get("stats"))
    attributes = _as_sequence(stats.get("attributes"))
    attribute_keys: list[str] = []
    for ai, raw in enumerate(attributes):
        key = _text(_as_mapping(raw).get("key"))
        if not key:
            issues.append(
                _issue(
                    "error",
                    f"rules.character_card.stats.attributes[{ai}].key",
                    "card.attribute_key_missing",
                    "属性缺少 key（检定靠它匹配，缺失会报『属性不属于当前世界』）",
                )
            )
            continue
        attribute_keys.append(key)

    duplicates = sorted({k for k in attribute_keys if attribute_keys.count(k) > 1})
    if duplicates:
        issues.append(
            _issue(
                "error",
                "rules.character_card.stats.attributes",
                "card.attribute_key_duplicate",
                f"属性 key 重复：{duplicates}",
            )
        )

    if stats.get("mode") != "preset":
        issues.append(
            _issue(
                "warning",
                "rules.character_card.stats.mode",
                "card.mode_not_preset",
                f"stats.mode 期望 preset，实际 {stats.get('mode')!r}",
            )
        )

    base_budget = stats.get("base_budget")
    if base_budget is not None and base_budget != EXPECTED_BASE_TOTAL:
        issues.append(
            _issue(
                "warning",
                "rules.character_card.stats.base_budget",
                "card.base_budget_unexpected",
                f"base_budget 期望 {EXPECTED_BASE_TOTAL}，实际 {base_budget!r}",
            )
        )
    effective = stats.get("effective_total")
    if effective is not None and effective != EXPECTED_EFFECTIVE_TOTAL:
        issues.append(
            _issue(
                "warning",
                "rules.character_card.stats.effective_total",
                "card.effective_total_unexpected",
                f"effective_total 期望 {EXPECTED_EFFECTIVE_TOTAL}，实际 {effective!r}",
            )
        )

    expected_keys = set(attribute_keys)
    professions = _as_sequence(card.get("profession_presets"))
    if not professions:
        issues.append(
            _issue(
                "error",
                "rules.character_card.profession_presets",
                "card.no_professions",
                "没有职业预设；文档要求至少 6 个",
            )
        )
    else:
        if len(professions) < MIN_PROFESSIONS:
            issues.append(
                _issue(
                    "warning",
                    "rules.character_card.profession_presets",
                    "card.too_few_professions",
                    f"职业预设仅 {len(professions)} 个，文档要求至少 {MIN_PROFESSIONS} 个"
                    "（少于时须给出题材或规模理由）",
                )
            )
        seen_prof_ids: set[str] = set()
        for pi, raw in enumerate(professions):
            pbase = f"rules.character_card.profession_presets[{pi}]"
            prof = _as_mapping(raw)
            pid = _text(prof.get("id"))
            if not pid:
                issues.append(_issue("error", f"{pbase}.id", "card.profession_id_missing", "职业缺少 id"))
            elif pid in seen_prof_ids:
                issues.append(
                    _issue("error", f"{pbase}.id", "card.profession_id_duplicate", f"职业 id 重复：{pid}")
                )
            seen_prof_ids.add(pid)

            if not _text(prof.get("name")):
                issues.append(_issue("warning", f"{pbase}.name", "card.profession_name_missing", "职业缺少显示名"))

            base = _as_mapping(prof.get("base_attributes"))
            if not base:
                issues.append(
                    _issue(
                        "error",
                        f"{pbase}.base_attributes",
                        "card.profession_no_attributes",
                        "职业缺少 base_attributes",
                    )
                )
                continue

            unknown = sorted(k for k in base if expected_keys and k not in expected_keys)
            if unknown:
                issues.append(
                    _issue(
                        "error",
                        f"{pbase}.base_attributes",
                        "card.profession_unknown_attribute",
                        f"职业属性 {unknown} 不在 stats.attributes 中——检定会报『属性不属于当前世界』",
                        detail={"unknown": unknown, "known": sorted(expected_keys)},
                    )
                )

            values = [v for v in base.values() if isinstance(v, int) and not isinstance(v, bool)]
            if len(values) != len(base):
                issues.append(
                    _issue(
                        "error",
                        f"{pbase}.base_attributes",
                        "card.profession_non_integer",
                        "职业基础属性必须全为整数",
                    )
                )
            elif sum(values) != EXPECTED_BASE_TOTAL:
                issues.append(
                    _issue(
                        "error",
                        f"{pbase}.base_attributes",
                        "card.profession_budget",
                        f"职业 {pid or pi} 基础属性合计 {sum(values)}，必须等于 {EXPECTED_BASE_TOTAL}",
                        detail={"sum": sum(values), "expected": EXPECTED_BASE_TOTAL},
                    )
                )

            missing_attrs = sorted(expected_keys - set(base))
            if missing_attrs:
                issues.append(
                    _issue(
                        "error",
                        f"{pbase}.base_attributes",
                        "card.profession_missing_attribute",
                        f"职业 {pid or pi} 缺少属性：{missing_attrs}",
                    )
                )

    # 主副属性必须不同（若配置要求）
    if stats.get("require_distinct_bonus_attributes"):
        primary = _text(stats.get("primary_attribute"))
        secondary = _text(stats.get("secondary_attribute"))
        if primary and secondary and primary == secondary:
            issues.append(
                _issue(
                    "error",
                    "rules.character_card.stats",
                    "card.bonus_attributes_identical",
                    "主属性与副属性不能相同",
                )
            )

    _lint_card_fields(card, issues)


def _lint_card_fields(card: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    fields = _as_sequence(card.get("fields"))
    if not fields:
        issues.append(
            _issue("error", "rules.character_card.fields", "card.no_fields", "角色卡没有任何字段")
        )
        return

    free_text: list[str] = []
    for index, raw in enumerate(fields):
        field = _as_mapping(raw)
        base = f"rules.character_card.fields[{index}]"
        key = _text(field.get("key"))
        if not key:
            issues.append(_issue("error", f"{base}.key", "card.field_key_missing", "字段缺少 key"))
        ftype = _text(field.get("type"))
        if ftype == "text" and not field.get("required"):
            free_text.append(key or base)

    if len(free_text) > 1:
        issues.append(
            _issue(
                "error",
                "rules.character_card.fields",
                "card.multiple_free_text",
                f"存在 {len(free_text)} 个可选自由文本字段：{free_text}；"
                "文档要求最多保留一个可留空的『补充说明』",
            )
        )
    elif not free_text:
        issues.append(
            _issue(
                "warning",
                "rules.character_card.fields",
                "card.no_free_text",
                "没有可选的自由文本项（建议保留一个可留空的补充说明）",
            )
        )
    else:
        for index, raw in enumerate(fields):
            field = _as_mapping(raw)
            if _text(field.get("key")) == free_text[0]:
                limit = field.get("max_chars")
                if isinstance(limit, int) and limit > 300:
                    issues.append(
                        _issue(
                            "warning",
                            f"rules.character_card.fields[{index}].max_chars",
                            "card.free_text_too_long",
                            f"补充说明上限 {limit} 字，建议不超过 300",
                        )
                    )


def _lint_opening(world: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    rules = _as_mapping(world.get("rules"))
    scene = _text(world.get("opening_scene"))
    if scene and len(scene) > MAX_OPENING_SCENE_CHARS:
        issues.append(
            _issue(
                "warning",
                "opening_scene",
                "opening.too_long",
                f"开场 {len(scene)} 字，超过约一屏（{MAX_OPENING_SCENE_CHARS} 字）会挤压首回合上下文",
            )
        )

    choices = _as_sequence(rules.get("opening_choices"))
    if not choices:
        issues.append(
            _issue(
                "warning",
                "rules.opening_choices",
                "opening.no_choices",
                "没有 opening_choices；引擎会静默回退到通用 A–D 文案（不报错，但失去题材贴合）",
            )
        )
        return

    collective = 0
    keys: list[str] = []
    for index, raw in enumerate(choices):
        choice = _as_mapping(raw)
        base = f"rules.opening_choices[{index}]"
        key = _text(choice.get("key"))
        if key:
            keys.append(key)
        if not _text(choice.get("text")):
            issues.append(_issue("error", f"{base}.text", "opening.choice_text_missing", "选项缺少 text"))
        if choice.get("collective"):
            collective += 1

    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        issues.append(
            _issue("error", "rules.opening_choices", "opening.choice_key_duplicate", f"选项 key 重复：{duplicates}")
        )

    if collective > MAX_COLLECTIVE_CHOICES:
        issues.append(
            _issue(
                "error",
                "rules.opening_choices",
                "opening.too_many_collective",
                f"全队（collective）选项 {collective} 个，超过上限 {MAX_COLLECTIVE_CHOICES}，"
                "会被自动降级为个人选项",
            )
        )

    if len(choices) != 4:
        issues.append(
            _issue(
                "warning",
                "rules.opening_choices",
                "opening.choice_count",
                f"开场选项 {len(choices)} 个，约定为 A–D 四个",
            )
        )


def _lint_size(world: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    try:
        size = len(json.dumps(world, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        issues.append(
            _issue("error", "$", "package.not_serializable", f"世界包无法序列化为 JSON：{exc}")
        )
        return
    if size > MAX_PACKAGE_BYTES:
        issues.append(
            _issue(
                "warning",
                "$",
                "package.too_large",
                f"世界包 {size / 1024:.1f} KB，超过 {MAX_PACKAGE_BYTES // 1024} KB 上限",
            )
        )


# --- 公开入口 -------------------------------------------------------------


def lint_world_package(
    world: Mapping[str, Any],
    *,
    npc_slugs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """对世界包做确定性结构校验。

    Args:
        world: 待校验的 v5 世界包（dict）。
        npc_slugs: 配套 NPC 包的 slug 列表；提供时会校验 ``key_npcs[].ref``
            是否都能对上，并在缺包时降级为 info 级提示。

    Returns:
        ``{"ok", "errors", "warnings", "infos", "issues"}``。
        ``ok`` 仅在没有任何 error 级问题时为 True。
    """
    issues: list[dict[str, Any]] = []

    if not isinstance(world, Mapping):
        issues.append(_issue("error", "$", "package.not_object", "世界包必须是 JSON 对象"))
        return _summarize(issues)

    _lint_envelope(world, issues)
    _lint_resolution(world, issues)
    chapters, chapter_milestones = _lint_progress(world, issues)
    if chapters:
        _lint_key_npc_refs(chapter_milestones, chapters, world, npc_slugs, issues)
    _lint_card(world, issues)
    _lint_opening(world, issues)
    _lint_size(world, issues)

    return _summarize(issues)


def _lint_resolution(world: Mapping[str, Any], issues: list[dict[str, Any]]) -> None:
    """``rules.resolution.mode`` 必须是运行时会认的模式名。

    为什么这条得单独拦：``world_contract`` 认不出某个模式时**不报错，直接
    降级成 ``none``**，于是整包世界一次检定都不摇——章节里写的 difficulty、
    危险档强制检定、里程碑的 evidence 全部失效，玩家那边只表现为"这游戏从不
    掷骰"。生成器曾经写死 ``"d20"``（那是 ``dice_system`` 的值，不是模式），
    一路生产出无检定的世界，没有一道闸门发现。

    合法取值与 ``world_contract.RESOLUTION_MODES`` 同源。

    注意**整个 ``rules.resolution`` 缺席不等于错**：``world_contract`` 会按角色卡
    是否有属性表推出 attribute / dice_only。会掉进 none 的只有两种写法——
    块在、但 ``mode`` 空，或者 ``mode`` 写了个认不出的值。
    """
    from ..world_contract import RESOLUTION_MODES

    raw_resolution = _as_mapping(world.get("rules")).get("resolution")
    if not isinstance(raw_resolution, Mapping):
        return  # 缺席：运行时自行推断，不算错
    mode = _text(raw_resolution.get("mode"))
    if not mode:
        issues.append(
            _issue(
                "error",
                "rules.resolution.mode",
                "resolution.mode_missing",
                "声明了 rules.resolution 却没有 mode，运行时会按 none 处理"
                "（一次检定都不摇）",
            )
        )
        return
    if mode.lower() not in RESOLUTION_MODES:
        issues.append(
            _issue(
                "error",
                "rules.resolution.mode",
                "resolution.mode_unknown",
                f"未知检定模式 {mode!r}；运行时认不出会**静默降级为 none**"
                f"（不摇点）。合法取值：{sorted(RESOLUTION_MODES)}",
                detail={"mode": mode, "allowed": sorted(RESOLUTION_MODES)},
            )
        )


def _summarize(issues: list[dict[str, Any]]) -> dict[str, Any]:
    errors = sum(1 for item in issues if item["level"] == "error")
    warnings = sum(1 for item in issues if item["level"] == "warning")
    infos = sum(1 for item in issues if item["level"] == "info")
    return {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "issues": issues,
    }
