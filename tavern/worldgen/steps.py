"""步骤协议：把「一次 LLM 调用 + 确定性校验 + 有界重试」封装成可复用的单元。

分工原则
--------
**模型提结构，Python 管标识符与引用。**

模型可以提议章节怎么切、里程碑写什么、NPC 有谁；但每一个 id 的格式、每一个引用
是否真实存在、每一个计数是否自洽，都由这里的校验函数判定。这样 lint 才能有意义地
失败——如果 id 由模型随手写而无人校验，坏包只会在运行时才炸。

校验函数只检查**模型输出的结构自洽**，不检查世界包本身——后者是
:mod:`tavern.worldgen.lint` 的职责。两者互补：

- steps 校验：模型这次说的东西是不是我要的形状（缺字段、id 格式、计数）
- lint 校验：组装出来的世界包是不是能跑（悬空引用、退出条件、属性预算）
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import PhaseId

CHAPTER_ID_RE = re.compile(r"^ch_\d{2}_[a-z0-9_]+$")
MILESTONE_ID_RE = re.compile(r"^m_\d{2}_\d{2}_[a-z0-9_]+$")
NPC_SLUG_RE = re.compile(r"^npc_[a-z0-9_]+$")

VALID_VERDICTS = {"supported", "contradicted", "unsupported", "partial"}
VALID_CARRYOVER_VERDICTS = {"confirmed", "demote", "drop"}
VALID_SEVERITIES = {"info", "warning", "blocking"}
VALID_RISKS = {"safe", "controlled", "dangerous", "desperate", "lethal"}


@dataclass(frozen=True)
class StepSpec:
    """一个步骤的静态描述。``system`` 必须自带全部任务关键规则。"""

    name: str
    phase: PhaseId
    system: str
    max_tokens: int = 8000
    max_repair: int = 2


# --- 小工具 ---------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def name_variants(name: str) -> list[str]:
    """一个原作人名在文本里可能的**写法**。

    同一个人，原文里写全名、名录里可能只写名——模型声明的是「菜月昴」，
    CAST 建出来的卡叫「昴」，里程碑里写的也是「昴」。所以匹配不能拿全名
    去比字符串相等，得把常见写法都列出来：

    - 译名取第一个音节段：「罗兹瓦尔·L·梅札斯」→「罗兹瓦尔」；
    - 三字以上的名字补末尾 1-2 字（中文/日文的名在末尾）：
      「菜月昴」→「月昴」「昴」。
    - **两个字的名字不补**：「拉姆」补出「姆」会误伤「雷姆」。
    """
    variants = [name]
    head = re.split(r"[·・\s]+", name)[0]
    if head and head != name:
        variants.append(head)
    if len(name) >= 3:
        variants.append(name[-2:])
        variants.append(name[-1:])
    return list(dict.fromkeys(v for v in variants if v))


def replaced_name_hits(text: Any, names: Sequence[str]) -> list[str]:
    """``text`` 里出现了哪些"由玩家取代的原作角色"的名字。

    用**包含匹配 + 写法变体**（见 :func:`name_variants`）。代价是短名字可能误伤，
    但被取代的角色是操作者点名要求替换的人——宁可在校验里多问一句「这个角色
    是不是他」，也不要把男主放进角色表。
    """
    haystack = _text(text)
    if not haystack:
        return []
    hits: list[str] = []
    for name in names:
        needle = _text(name)
        if not needle or needle in hits:
            continue
        for variant in name_variants(needle):
            if variant in haystack or haystack in variant:
                hits.append(needle)
                break
    return hits


def validate_cast_excludes_replaced(
    payload: Mapping[str, Any], replaces: Sequence[str]
) -> list[str]:
    """``CAST`` 不得给被玩家取代的角色建卡。

    提示词里的硬规则是**软**的，这里补一道机械核对：命中就返回问题，
    ``generate_json`` 会带着问题清单重试（见 ``tavern/prompts.py:repair_prompt``）。
    问题文案里**必须带上具体人名**——重试时不重发 user prompt，模型只能从
    问题清单里知道是谁。
    """
    names = [str(n) for n in (replaces or []) if _text(n)]
    if not names:
        return []
    problems: list[str] = []
    for index, raw in enumerate(_as_list(payload.get("npcs"))):
        item = _as_map(raw)
        label = f"npcs[{index}]（{_text(item.get('name'))}）"
        hits = replaced_name_hits(item.get("name"), names)
        if hits:
            problems.append(
                f"{label} 是**由玩家取代**的原作角色（{'、'.join(hits)}），"
                "不得建卡：玩家站在他的位置上。请删掉这一条，"
                "需要的话把他的作用改写成别的 NPC 或环境信息"
            )
    return problems


def validate_chapter_avoids_replaced_actors(
    payload: Mapping[str, Any], replaces: Sequence[str]
) -> list[str]:
    """章节检查点里，被玩家取代的角色不得充当**行动的施动者**。

    查的是玩家可见的指令性文本：标题、目标、节奏、钩子、里程碑 label、
    ``key_npcs[].role``。**不查** ``evidence_required[].match``——那是从原文
    照抄的证据词（可能是别的角色提到他的名字），照抄才对。
    """
    names = [str(n) for n in (replaces or []) if _text(n)]
    if not names:
        return []
    chapter = _as_map(payload.get("chapter"))
    if not chapter:
        return []

    problems: list[str] = []

    def check(value: Any, label: str) -> None:
        hits = replaced_name_hits(value, names)
        if hits:
            problems.append(
                f"{label} 把「{'、'.join(hits)}」写成了行动的施动者，"
                "但玩家已经取代了他的位置：改成「玩家小队」做这件事"
                "（名字只能出现在别人的台词或原文引用里）"
            )

    check(chapter.get("title"), "chapter.title")
    check(chapter.get("subtitle"), "chapter.subtitle")
    check(chapter.get("current_objective"), "chapter.current_objective")
    check(chapter.get("pacing_directive"), "chapter.pacing_directive")
    for index, hook in enumerate(_as_list(chapter.get("hook_pool"))):
        check(hook, f"hook_pool[{index}]")
    for index, raw in enumerate(_as_list(chapter.get("milestones"))):
        check(_as_map(raw).get("label"), f"milestones[{index}].label")
    for index, raw in enumerate(_as_list(chapter.get("key_npcs"))):
        check(_as_map(raw).get("role"), f"key_npcs[{index}].role")
    return problems


def _check_source(container: Mapping[str, Any], label: str, problems: list[str]) -> None:
    """引用字段的结构检查。**只查形状**——真实性由 ``verify_citation`` 回验。"""
    source = _as_map(container.get("source"))
    if not source:
        problems.append(f"{label} 缺少 source 引用")
        return
    if not _text(source.get("rel_path")):
        problems.append(f"{label} 的 source 缺少 rel_path")
    try:
        start = int(source.get("line_start") or 0)
        end = int(source.get("line_end") or 0)
    except (TypeError, ValueError):
        problems.append(f"{label} 的 source 行号不是整数")
        return
    if start < 1 or end < start:
        problems.append(f"{label} 的 source 行号区间非法：{start}-{end}")
    if not _text(source.get("quote")):
        problems.append(f"{label} 的 source 缺少 quote（必须给出原文原句）")


# --- 校验：卷级场景地图 ---------------------------------------------------


def validate_timeline(
    payload: Mapping[str, Any], *, requirements: str = ""
) -> list[str]:
    """时间线梳理的校验。

    除了结构完整性，这里还钉住两条**防退回**的规则——它们对应的正是实测踩过的坑：
    多周目作品里，最激烈的冲突被以"属于另一条时间线"为由整段丢掉。

    ``requirements`` 是操作者写下的生成要求。它只用于**把要求复述进问题清单**：
    重试时 ``llm._repair_prompt`` 不重发原始 prompt，模型看不到要求，而
    「谁该被玩家取代」正是从要求里读出来的。不复述的话，模型只能靠被拒输出猜。
    """
    problems: list[str] = []

    segments = _as_list(payload.get("segments"))
    merged = _as_list(payload.get("merged"))
    if not segments:
        problems.append("segments 为空：必须至少给出一条时间线")
    if not merged:
        problems.append(
            "merged 为空：必须给出缝好的新线。只列 segments 不算完成——"
            "那还是「哪些线可用」的旧思路，没有把情节重新缝起来"
        )
    if not problems and not _text(payload.get("structure")):
        problems.append("缺少 structure（本卷的时间结构）")

    # 玩家位置：这是**后面每一步的口径来源**（划章节、抽里程碑、建角色卡都读它）。
    # 缺了它，操作者写的「由玩家小队替换掉男主」就只停在这一步的叙述里，
    # 到 CAST 又会长出一张男主角色卡——chapter030 的真实事故。
    role = _as_map(payload.get("player_role"))
    if not role:
        brief = _text(requirements)[:200]
        problems.append(
            "缺少 player_role：必须声明玩家在本卷扮演谁的位置"
            "（replaces 写具体人名，可为空数组；note 说明为什么这样安排）"
            + (f"。操作者这次的要求是：「{brief}」——据此判断该由玩家取代谁" if brief else "")
        )
    else:
        if not isinstance(role.get("replaces"), list):
            problems.append(
                "player_role.replaces 必须是数组（没有要取代的角色就写空数组 []）"
            )
        if not _text(role.get("note")):
            problems.append(
                "player_role.note 必填：说明为什么这样安排玩家位置"
                "（replaces 为空时尤其要说明为什么不替换）"
            )

    segment_ids: set[str] = set()
    for index, raw in enumerate(segments):
        item = _as_map(raw)
        label = f"segments[{index}]"
        seg_id = _text(item.get("segment_id"))
        if not seg_id:
            problems.append(f"{label} 缺少 segment_id")
        else:
            segment_ids.add(seg_id)
        if not _text(item.get("label")):
            problems.append(f"{label} 缺少 label")
        if not _as_list(item.get("source_files")):
            problems.append(f"{label} 缺少 source_files")
        kind = _text(item.get("kind"))
        if kind not in {"main", "loop_iteration", "flashback", "side", "epilogue"}:
            problems.append(f"{label} 的 kind 非法：{kind!r}")

    beat_ids: set[str] = set()
    for index, raw in enumerate(merged):
        item = _as_map(raw)
        label = f"merged[{index}]"
        beat_id = _text(item.get("beat_id"))
        if not beat_id:
            problems.append(f"{label} 缺少 beat_id")
        elif beat_id in beat_ids:
            problems.append(f"{label} 的 beat_id 重复：{beat_id}")
        else:
            beat_ids.add(beat_id)

        if not _text(item.get("label")):
            problems.append(f"{label} 缺少 label")

        action = _text(item.get("merge_action"))
        if action not in {"keep", "adapt", "drop"}:
            problems.append(f"{label} 的 merge_action 非法：{action!r}")
            continue

        depends = bool(item.get("depends_on_reset"))
        # 依赖回溯的情节不可能"原样可用"——这是判据与处置的一致性。
        if depends and action == "keep":
            problems.append(
                f"{label} 声明依赖主角回溯（depends_on_reset=true），"
                "却选了 keep。这类情节必须 adapt（改写获知途径）或 drop"
            )
        # 改写过就必须说清怎么改，否则"自圆其说"是空话。
        if action == "adapt" and not _text(item.get("adaptation_note")):
            problems.append(
                f"{label} 选了 adapt 但没写 adaptation_note——"
                "必须说明原作里它怎么收场、新线里怎么让它自洽"
            )

    # 缝合后的自洽性：from_segments 必须指向真实存在的时间线
    for index, raw in enumerate(merged):
        item = _as_map(raw)
        for ref in _as_list(item.get("from_segments")):
            if segment_ids and str(ref) not in segment_ids:
                problems.append(f"merged[{index}] 引用了不存在的 segment：{ref}")
        # 每条 merged 都得说清它从哪来，否则无从追溯
        if not _as_list(item.get("from_segments")):
            problems.append(f"merged[{index}] 缺少 from_segments（这条情节来自哪条线）")

    return problems


def validate_arc_map(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    candidates = _as_list(payload.get("candidate_chapters"))
    if not candidates:
        problems.append("candidate_chapters 为空")
        return problems
    if not _text(payload.get("premise")):
        problems.append("缺少 premise（核心冲突）")
    if "unadapted" not in payload:
        problems.append(
            "缺少 unadapted 字段。必须显式声明哪些原作内容不改编及原因——"
            "悄悄丢掉整条线而不声明是不可接受的"
        )
    for index, raw in enumerate(candidates):
        item = _as_map(raw)
        label = f"candidate_chapters[{index}]"
        if not _text(item.get("title")):
            problems.append(f"{label} 缺少 title")
        if not _as_list(item.get("source_files")):
            problems.append(f"{label} 缺少 source_files")
    return problems


# --- 校验：章节检查点 -----------------------------------------------------


def validate_chapter(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    chapter = _as_map(payload.get("chapter"))
    if not chapter:
        problems.append("缺少 chapter 对象")
        return problems

    if not _text(chapter.get("title")):
        problems.append("chapter 缺少 title")
    if not _text(chapter.get("current_objective")):
        problems.append("chapter 缺少 current_objective")
    if not _text(chapter.get("pacing_directive")):
        problems.append("chapter 缺少 pacing_directive")

    try:
        min_turns = int(chapter.get("min_turns"))
    except (TypeError, ValueError):
        problems.append("chapter.min_turns 必须是整数")
        min_turns = 0
    try:
        max_turns = int(chapter.get("max_turns"))
    except (TypeError, ValueError):
        problems.append("chapter.max_turns 必须是整数")
        max_turns = 0
    if min_turns < 1:
        problems.append("chapter.min_turns 必须 >= 1")
    if max_turns < 1:
        problems.append("chapter.max_turns 必须 >= 1")
    if min_turns and max_turns and min_turns > max_turns:
        problems.append(f"min_turns({min_turns}) 大于 max_turns({max_turns})")

    milestones = _as_list(chapter.get("milestones"))
    if not milestones:
        problems.append("chapter.milestones 为空——章节将没有可完成的退出条件")
    for index, raw in enumerate(milestones):
        item = _as_map(raw)
        label = f"milestones[{index}]"
        if not _text(item.get("label")):
            problems.append(f"{label} 缺少 label")
        elif len(_text(item.get("label"))) < 8:
            problems.append(f"{label}.label 过短，无法区分「计划」与「完成」")
        evidence = _as_list(item.get("evidence_required"))
        if not evidence:
            problems.append(f"{label} 缺少 evidence_required")
        else:
            for ei, raw_ev in enumerate(evidence):
                matches = _as_list(_as_map(raw_ev).get("match"))
                if not [m for m in matches if _text(m)]:
                    problems.append(f"{label}.evidence_required[{ei}] 的 match 为空")
        _check_source(item, label, problems)

    for index, raw in enumerate(_as_list(chapter.get("key_npcs"))):
        item = _as_map(raw)
        label = f"key_npcs[{index}]"
        ref = _text(item.get("ref"))
        if not ref:
            problems.append(f"{label} 缺少 ref")
        elif not NPC_SLUG_RE.match(ref):
            problems.append(f"{label}.ref 需形如 npc_slug，实际 {ref!r}")
        if not _text(item.get("role")):
            problems.append(f"{label} 缺少 role")
        # 预写生死/地点会在切章时覆盖玩家成果——作者指南明确禁止
        pinned = {k for k in _as_map(item.get("state")) if k in {"location", "alive", "dead", "status"}}
        if pinned:
            problems.append(
                f"{label}.state 预写了 {sorted(pinned)}，会覆盖玩家已取得的成果；"
                "key_npcs 只写 ref 与 role，不要钉死地点或生死"
            )
    return problems


# --- 校验：NPC 名录 -------------------------------------------------------


def validate_cast(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    npcs = _as_list(payload.get("npcs"))
    if not npcs:
        problems.append("npcs 为空")
        return problems
    seen: set[str] = set()
    for index, raw in enumerate(npcs):
        item = _as_map(raw)
        label = f"npcs[{index}]"
        slug = _text(item.get("slug"))
        if not slug:
            problems.append(f"{label} 缺少 slug")
        elif not NPC_SLUG_RE.match(slug):
            problems.append(f"{label}.slug 需形如 npc_slug，实际 {slug!r}")
        elif slug in seen:
            problems.append(f"{label}.slug 重复：{slug}")
        else:
            seen.add(slug)
        if not _text(item.get("name")):
            problems.append(f"{label} 缺少 name")
        if not _text(item.get("prompt")):
            problems.append(f"{label} 缺少 prompt（扮演指示）")
        if not _as_list(item.get("limitations")):
            problems.append(
                f"{label} 缺少 limitations。能力边界是防止扮演模型凭空开挂的关键，必填"
            )
        _check_source(item, label, problems)
    return problems


# --- 校验：局部重写 -------------------------------------------------------


def validate_regen_milestone(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    milestone = _as_map(payload.get("milestone"))
    if not milestone:
        problems.append("缺少 milestone 对象")
        return problems
    if not _text(milestone.get("label")):
        problems.append("milestone 缺少 label")
    if not _as_list(milestone.get("evidence_required")):
        problems.append("milestone 缺少 evidence_required")
    _check_source(milestone, "milestone", problems)
    return problems


# --- 校验：前作影响确认 ---------------------------------------------------


def validate_verdicts(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    verdicts = _as_list(payload.get("verdicts"))
    if not verdicts:
        problems.append("verdicts 为空")
        return problems
    for index, raw in enumerate(verdicts):
        item = _as_map(raw)
        label = f"verdicts[{index}]"
        if not _text(item.get("impact_id")):
            problems.append(f"{label} 缺少 impact_id")
        verdict = _text(item.get("verdict"))
        if verdict not in VALID_CARRYOVER_VERDICTS:
            problems.append(f"{label}.verdict 非法：{verdict!r}")
            continue
        # confirmed 必须带引用——没有出处的"确认"就是编造
        if verdict == "confirmed":
            _check_source(item, label, problems)
    return problems


# --- 校验：散文文案 -------------------------------------------------------


def validate_prose(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if not _text(payload.get("opening_scene")):
        problems.append("缺少 opening_scene")
    elif len(_text(payload.get("opening_scene"))) > 900:
        problems.append("opening_scene 过长（超过约一屏），会挤压首回合上下文")

    choices = _as_list(payload.get("opening_choices"))
    if len(choices) != 4:
        problems.append(f"opening_choices 必须是 4 个，实际 {len(choices)}")
    keys: list[str] = []
    collective = 0
    for index, raw in enumerate(choices):
        item = _as_map(raw)
        label = f"opening_choices[{index}]"
        key = _text(item.get("key"))
        if key:
            keys.append(key)
        if not _text(item.get("text")):
            problems.append(f"{label} 缺少 text")
        risk = _text(item.get("risk"))
        if risk and risk not in VALID_RISKS:
            problems.append(f"{label}.risk 非法：{risk!r}")
        if item.get("collective"):
            collective += 1
    if len(set(keys)) != len(keys):
        problems.append(f"opening_choices 的 key 重复：{keys}")
    if collective > 2:
        problems.append(f"全队（collective）选项 {collective} 个，超过上限 2，会被自动降级")

    if not _text(payload.get("system_prompt")):
        problems.append("缺少 system_prompt")
    return problems


# --- 校验：职业预设 -------------------------------------------------------

BASE_ATTRIBUTE_TOTAL = 50
MIN_PROFESSIONS = 6
MAX_PROFESSIONS = 8


def validate_card(
    payload: Mapping[str, Any],
    *,
    attribute_keys: Sequence[str] | None = None,
) -> list[str]:
    """职业预设校验。属性预算不平是硬伤——插件建卡会直接失败。"""
    problems: list[str] = []
    professions = _as_list(payload.get("professions"))
    if not professions:
        problems.append("professions 为空")
        return problems
    if not (MIN_PROFESSIONS <= len(professions) <= MAX_PROFESSIONS):
        problems.append(
            f"职业数量应为 {MIN_PROFESSIONS}-{MAX_PROFESSIONS} 个，实际 {len(professions)}"
        )

    expected = {str(k) for k in (attribute_keys or [])}
    seen: set[str] = set()
    for index, raw in enumerate(professions):
        item = _as_map(raw)
        label = f"professions[{index}]"
        pid = _text(item.get("id"))
        if not pid:
            problems.append(f"{label} 缺少 id")
        elif pid in seen:
            problems.append(f"{label}.id 重复：{pid}")
        else:
            seen.add(pid)
        if not _text(item.get("name")):
            problems.append(f"{label} 缺少 name")
        if not _text(item.get("description")):
            problems.append(f"{label} 缺少 description（须写清擅长与短板）")

        base = _as_map(item.get("base_attributes"))
        if not base:
            problems.append(f"{label} 缺少 base_attributes")
            continue
        if expected:
            unknown = sorted(k for k in base if k not in expected)
            missing = sorted(expected - set(base))
            if unknown:
                problems.append(f"{label}.base_attributes 含未知属性 {unknown}")
            if missing:
                problems.append(f"{label}.base_attributes 缺少属性 {missing}")
        values = [v for v in base.values() if isinstance(v, int) and not isinstance(v, bool)]
        if len(values) != len(base):
            problems.append(f"{label}.base_attributes 必须全为整数")
        elif sum(values) != BASE_ATTRIBUTE_TOTAL:
            problems.append(
                f"{label} 基础属性合计 {sum(values)}，必须等于 {BASE_ATTRIBUTE_TOTAL}"
            )
    return problems


# --- 校验：反省判定 -------------------------------------------------------


def validate_reflect(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    findings = _as_list(payload.get("findings"))
    if not findings:
        problems.append("findings 为空")
        return problems
    for index, raw in enumerate(findings):
        item = _as_map(raw)
        label = f"findings[{index}]"
        if not _text(item.get("claim_id")):
            problems.append(f"{label} 缺少 claim_id")
        verdict = _text(item.get("verdict"))
        if verdict not in VALID_VERDICTS:
            problems.append(f"{label}.verdict 非法：{verdict!r}")
            continue
        severity = _text(item.get("severity"))
        if severity and severity not in VALID_SEVERITIES:
            problems.append(f"{label}.severity 非法：{severity!r}")
        # 判"与原文矛盾"必须拿出原文——否则就是在凭先验知识下结论
        if verdict == "contradicted":
            _check_source(item, label, problems)
    return problems


VALID_COVERAGE_VERDICTS = {"covered", "distorted", "omitted"}


def validate_coverage(payload: Mapping[str, Any]) -> list[str]:
    """覆盖判断的结构校验。

    这里的 `covered` 与 `distorted` **必须点名具体落点**——不点名就等于
    "我觉得差不多"，那正是漏判的来源。
    """
    problems: list[str] = []
    findings = _as_list(payload.get("findings"))
    if not findings:
        problems.append("findings 为空")
        return problems
    for index, raw in enumerate(findings):
        item = _as_map(raw)
        label = f"findings[{index}]"
        if not _text(item.get("claim_id")):
            problems.append(f"{label} 缺少 claim_id")
        verdict = _text(item.get("verdict"))
        if verdict not in VALID_COVERAGE_VERDICTS:
            problems.append(
                f"{label}.verdict 非法：{verdict!r}（只能是 covered/distorted/omitted）"
            )
            continue
        reason = _text(item.get("reason"))
        if not reason:
            problems.append(f"{label} 缺少 reason")
        # 判"有落点"却说不清落在哪一章/哪个里程碑 → 不算数
        if verdict in {"covered", "distorted"} and len(reason) < 8:
            problems.append(
                f"{label} 判 {verdict} 但 reason 没点名具体落点——"
                "必须指出是草稿里的哪一章或哪个里程碑承接了它"
            )
    return problems


# --- 注册表 ---------------------------------------------------------------

VALIDATORS: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "timeline": validate_timeline,
    "coverage": validate_coverage,
    "arc_map": validate_arc_map,
    "extract_chapter": validate_chapter,
    "cast": validate_cast,
    "card": validate_card,
    "regen_milestone": validate_regen_milestone,
    "confirm_carryover": validate_verdicts,
    "prose": validate_prose,
    "reflect": validate_reflect,
}

STEP_SPECS: dict[str, StepSpec] = {
    "timeline": StepSpec("timeline", PhaseId.TIMELINE, ""),
    "coverage": StepSpec("coverage", PhaseId.REFLECT, ""),
    "arc_map": StepSpec("arc_map", PhaseId.CHECKPOINT_EXTRACT, ""),
    "extract_chapter": StepSpec("extract_chapter", PhaseId.CHECKPOINT_EXTRACT, ""),
    "cast": StepSpec("cast", PhaseId.CHECKPOINT_EXTRACT, ""),
    "card": StepSpec("card", PhaseId.GENERATE, ""),
    "regen_milestone": StepSpec("regen_milestone", PhaseId.CHECKPOINT_APPROVE, ""),
    "confirm_carryover": StepSpec("confirm_carryover", PhaseId.INHERIT, ""),
    "prose": StepSpec("prose", PhaseId.GENERATE, ""),
    "reflect": StepSpec("reflect", PhaseId.REFLECT, ""),
}


def validator_for(name: str) -> Callable[[Mapping[str, Any]], list[str]]:
    return VALIDATORS.get(name, lambda payload: [])
