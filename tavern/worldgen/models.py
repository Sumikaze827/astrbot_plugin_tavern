"""世界包生成 agent 的共享数据结构。

本模块只放数据结构与常量，不放逻辑——逻辑在各自的功能模块里。

与既有代码的关系：
- 检索命中的片段类型复用 :class:`tavern.worldgen.corpus.Passage` 与
  :class:`tavern.worldgen.retriever.Hit`，不在这里重复定义。
- 校验问题沿用 ``tavern/world_preflight.py::_issue`` 的
  ``{level, path, code, message, detail}`` 形状（lint 直接返回 dict），
  以便面板统一渲染体检与 lint 两套报告。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    """作业状态。``AWAITING_APPROVAL`` 是唯一会长期停留的状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_ATTENTION = "needs_attention"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class PhaseId(str, Enum):
    """阶段机。顺序即 ``PHASE_ORDER``。"""

    INTAKE = "intake"
    SOURCING = "sourcing"
    TIMELINE = "timeline"  # 理清时间线并缝成一条（原创跳过）
    CHECKPOINT_EXTRACT = "checkpoint_extract"
    CHECKPOINT_APPROVE = "checkpoint_approve"  # 用户审批门，不调 LLM
    INHERIT = "inherit"
    GENERATE = "generate"
    REFLECT = "reflect"
    COVERAGE_APPROVE = "coverage_approve"  # 遗漏复核门，仅在反省发现遗漏时停
    GATE = "gate"  # lint + preflight
    EMIT = "emit"


PHASE_ORDER: tuple[PhaseId, ...] = (
    PhaseId.INTAKE,
    PhaseId.SOURCING,
    PhaseId.TIMELINE,
    PhaseId.CHECKPOINT_EXTRACT,
    PhaseId.CHECKPOINT_APPROVE,
    PhaseId.INHERIT,
    PhaseId.GENERATE,
    PhaseId.REFLECT,
    PhaseId.COVERAGE_APPROVE,
    PhaseId.GATE,
    PhaseId.EMIT,
)

PHASE_LABELS: dict[PhaseId, str] = {
    PhaseId.INTAKE: "解析请求",
    PhaseId.SOURCING: "准备素材索引",
    PhaseId.TIMELINE: "理清时间线",
    PhaseId.CHECKPOINT_EXTRACT: "提取关键节点",
    PhaseId.CHECKPOINT_APPROVE: "等待你审批检查点",
    PhaseId.INHERIT: "继承前作影响",
    PhaseId.GENERATE: "生成世界包",
    PhaseId.REFLECT: "对照原文反省",
    PhaseId.COVERAGE_APPROVE: "等待你确认遗漏",
    PhaseId.GATE: "静态体检",
    PhaseId.EMIT: "落盘交付",
}


class SourceKind(str, Enum):
    """两类世界。"""

    ADAPTATION = "adaptation"  # 改编自小说某卷
    ORIGIN = "origin"  # 原创故事


# --- 出处引用 -------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """一条指向原文的引用。``quote`` 必须能在 ``rel_path`` 的行区间里逐字找到。"""

    rel_path: str
    line_start: int
    line_end: int
    quote: str = ""

    @property
    def label(self) -> str:
        if self.line_start == self.line_end:
            return f"{self.rel_path}:{self.line_start}"
        return f"{self.rel_path}:{self.line_start}-{self.line_end}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "quote": self.quote,
        }


@dataclass(frozen=True)
class CitationCheck:
    """引用核验结果。``ok=False`` 的引用一律不可信——不得据此推翻任何内容。"""

    citation: Citation
    ok: bool
    actual_text: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation": self.citation.to_dict(),
            "label": self.citation.label,
            "ok": self.ok,
            "reason": self.reason,
        }


# --- 时间线 ---------------------------------------------------------------
#
# 为什么需要这一层
# ----------------
# 多周目/多时间线的卷（例如《从零》第二章的死亡回归），原作的"正史"是**最后那条线**，
# 前面几条都以主角死亡收场。改编时如果只按"哪条线是正史"取材，就会把前面几周目里
# 最激烈的冲突整段丢掉——实测发生过：雷姆怀疑昴并对峙的那一段被以"属于另一条时间线"
# 为由移除了。
#
# 正确的判据**不是**"它属于哪条线"，而是：
#
#     这个事件本身，是否依赖主角具备回溯/重置能力？
#
# 不依赖的（"雷姆起疑并当面质问"）可以并进新线里**真实地发生一次**；
# 依赖的（"靠记得上一周目的细节提前避开陷阱"）才需要改写或放弃。
#
# 所以这里产出的不是"哪些线可用"，而是一条**缝好的线**：把各条线里可用的情节
# 抽出来、按新的因果顺序排好，每条都写明"原作里它怎么收场、我们怎么让它自洽"。


class SegmentKind(str, Enum):
    """一条时间线在原作里的角色。"""

    MAIN = "main"                      # 主线（多周目作品里指最终那条）
    LOOP_ITERATION = "loop_iteration"  # 多周目中的一轮
    FLASHBACK = "flashback"
    SIDE = "side"                      # 支线 / 番外
    EPILOGUE = "epilogue"


class MergeAction(str, Enum):
    """一条情节如何进入缝好的新线。"""

    KEEP = "keep"    # 原样并入
    ADAPT = "adapt"  # 改写后并入（原作里依赖回溯，或以死亡收场）
    DROP = "drop"    # 不并入


@dataclass
class TimelineSegment:
    """原作里的一条时间线。"""

    segment_id: str
    label: str
    kind: SegmentKind = SegmentKind.MAIN
    source_files: list[str] = field(default_factory=list)
    summary: str = ""
    #: 这一段的结尾是不是"主角死亡 → 时间重置"。是最容易被误判成"不可改编"的特征。
    ends_with_reset: bool = False
    key_beats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimelineSegment":
        try:
            kind = SegmentKind(str(data.get("kind") or SegmentKind.MAIN.value))
        except ValueError:
            kind = SegmentKind.MAIN
        return cls(
            segment_id=str(data.get("segment_id") or ""),
            label=str(data.get("label") or ""),
            kind=kind,
            source_files=[str(x) for x in (data.get("source_files") or [])],
            summary=str(data.get("summary") or ""),
            ends_with_reset=bool(data.get("ends_with_reset")),
            key_beats=[str(x) for x in (data.get("key_beats") or [])],
        )


@dataclass
class MergedBeat:
    """缝进新线的一条情节。**这是覆盖检查的清单单位。**"""

    beat_id: str
    label: str
    from_segments: list[str] = field(default_factory=list)
    #: 关键判据：这条情节是否依赖主角能回溯。依赖的必须改写或丢弃。
    depends_on_reset: bool = False
    merge_action: MergeAction = MergeAction.KEEP
    #: 原作里它怎么收场、我们怎么让它在新线里自洽。ADAPT 时必填。
    adaptation_note: str = ""

    @property
    def must_appear(self) -> bool:
        return self.merge_action is not MergeAction.DROP

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["merge_action"] = self.merge_action.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MergedBeat":
        try:
            action = MergeAction(str(data.get("merge_action") or MergeAction.KEEP.value))
        except ValueError:
            action = MergeAction.KEEP
        return cls(
            beat_id=str(data.get("beat_id") or ""),
            label=str(data.get("label") or ""),
            from_segments=[str(x) for x in (data.get("from_segments") or [])],
            depends_on_reset=bool(data.get("depends_on_reset")),
            merge_action=action,
            adaptation_note=str(data.get("adaptation_note") or ""),
        )


@dataclass
class PlayerRole:
    """玩家在本卷扮演谁的位置。

    ``replaces`` 里是**由玩家取代的原作角色**（人名，不是 slug）——他们的位置
    属于玩家，因此不能建 NPC 卡、不能出现在章节 ``key_npcs``、不能被写成行动的
    施动者。后面每一步（划章节 / 抽里程碑 / 建角色卡 / 写开场白）都读这一项，
    它决定了整个演员表的口径。

    ``replaces`` 允许为空（本卷不替换任何人），但那时 ``note`` 必须说明原因。
    """

    replaces: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"replaces": list(self.replaces), "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlayerRole":
        return cls(
            replaces=[
                str(x).strip() for x in (data.get("replaces") or []) if str(x).strip()
            ],
            note=str(data.get("note") or ""),
        )


@dataclass
class Timeline:
    """一卷的时间线梳理结果。"""

    structure: str = "linear"  # linear | loop | parallel | frame
    note: str = ""
    segments: list[TimelineSegment] = field(default_factory=list)
    merged: list[MergedBeat] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    coherence_notes: list[str] = field(default_factory=list)
    #: 玩家位置（玩家取代了哪些原作角色）。旧作业的产物里没有这一项，默认空。
    player_role: PlayerRole = field(default_factory=PlayerRole)

    @property
    def required_beats(self) -> list[MergedBeat]:
        """必须在新线里出现的情节——覆盖检查查的就是这些。"""
        return [b for b in self.merged if b.must_appear]

    @property
    def replaced_characters(self) -> list[str]:
        """由玩家取代的原作角色名。给提示词与机械核对用。"""
        return list(self.player_role.replaces)

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure": self.structure,
            "note": self.note,
            "player_role": self.player_role.to_dict(),
            "segments": [s.to_dict() for s in self.segments],
            "merged": [b.to_dict() for b in self.merged],
            "dropped": list(self.dropped),
            "coherence_notes": list(self.coherence_notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Timeline":
        role = data.get("player_role")
        return cls(
            structure=str(data.get("structure") or "linear"),
            note=str(data.get("note") or ""),
            segments=[
                TimelineSegment.from_dict(s)
                for s in (data.get("segments") or [])
                if isinstance(s, Mapping)
            ],
            merged=[
                MergedBeat.from_dict(b)
                for b in (data.get("merged") or [])
                if isinstance(b, Mapping)
            ],
            dropped=[dict(d) for d in (data.get("dropped") or []) if isinstance(d, Mapping)],
            coherence_notes=[str(x) for x in (data.get("coherence_notes") or [])],
            player_role=PlayerRole.from_dict(role) if isinstance(role, Mapping) else PlayerRole(),
        )


# --- 检查点提案 -----------------------------------------------------------


@dataclass
class ProposalMilestone:
    """里程碑提案条目。``item_id`` 是作业内稳定标识，供逐条审批使用。"""

    item_id: str
    milestone_id: str
    label: str
    evidence_required: list[dict[str, Any]] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    adaptation_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["citations"] = [c.to_dict() for c in self.citations]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposalMilestone":
        return cls(
            item_id=str(data.get("item_id") or ""),
            milestone_id=str(data.get("milestone_id") or ""),
            label=str(data.get("label") or ""),
            evidence_required=list(data.get("evidence_required") or []),
            citations=[
                Citation(
                    rel_path=str(c.get("rel_path") or ""),
                    line_start=int(c.get("line_start") or 0),
                    line_end=int(c.get("line_end") or 0),
                    quote=str(c.get("quote") or ""),
                )
                for c in (data.get("citations") or [])
                if isinstance(c, dict)
            ],
            confidence=float(data.get("confidence") or 0.0),
            adaptation_note=str(data.get("adaptation_note") or ""),
        )


@dataclass
class ProposalChapter:
    """章节提案。里程碑与 NPC 都是它的子项。"""

    item_id: str
    chapter_id: str
    title: str
    subtitle: str = ""
    source_spans: list[Citation] = field(default_factory=list)
    #: 这一章取材于哪些原始篇目。**覆盖检查靠它**——以前这个信息在
    #: ``_chapter_from_payload`` 里被直接丢掉了，于是"整篇没被任何章覆盖"
    #: 这件事无从查起。
    source_files: list[str] = field(default_factory=list)
    #: 这一章承接了时间线里的哪些关键情节（``MergedBeat.beat_id``）。
    beats: list[str] = field(default_factory=list)
    milestones: list[ProposalMilestone] = field(default_factory=list)
    key_npcs: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    confidence: float = 0.0
    min_turns: int = 1
    max_turns: int = 12
    current_objective: str = ""
    pacing_directive: str = ""
    narrative_length_band: str = "standard"
    hook_pool: list[str] = field(default_factory=list)
    #: 与原作不同、需要用户拍板的改编取舍（例如「死亡回归循环未改编」）。
    unadapted_notes: list[str] = field(default_factory=list)
    #: 与前作玩家成果冲突，必须回炉审批。
    inheritance_conflict: bool = False

    def child_item_ids(self) -> list[str]:
        return [m.item_id for m in self.milestones]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "chapter_id": self.chapter_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "source_spans": [c.to_dict() for c in self.source_spans],
            "source_files": list(self.source_files),
            "beats": list(self.beats),
            "milestones": [m.to_dict() for m in self.milestones],
            "key_npcs": list(self.key_npcs),
            "rationale": self.rationale,
            "confidence": self.confidence,
            "min_turns": self.min_turns,
            "max_turns": self.max_turns,
            "current_objective": self.current_objective,
            "pacing_directive": self.pacing_directive,
            "narrative_length_band": self.narrative_length_band,
            "hook_pool": list(self.hook_pool),
            "unadapted_notes": list(self.unadapted_notes),
            "inheritance_conflict": self.inheritance_conflict,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposalChapter":
        return cls(
            item_id=str(data.get("item_id") or ""),
            chapter_id=str(data.get("chapter_id") or ""),
            title=str(data.get("title") or ""),
            subtitle=str(data.get("subtitle") or ""),
            source_spans=[
                Citation(
                    rel_path=str(c.get("rel_path") or ""),
                    line_start=int(c.get("line_start") or 0),
                    line_end=int(c.get("line_end") or 0),
                    quote=str(c.get("quote") or ""),
                )
                for c in (data.get("source_spans") or [])
                if isinstance(c, dict)
            ],
            source_files=[str(x) for x in (data.get("source_files") or [])],
            beats=[str(x) for x in (data.get("beats") or [])],
            milestones=[
                ProposalMilestone.from_dict(m)
                for m in (data.get("milestones") or [])
                if isinstance(m, dict)
            ],
            key_npcs=list(data.get("key_npcs") or []),
            rationale=str(data.get("rationale") or ""),
            confidence=float(data.get("confidence") or 0.0),
            min_turns=int(data.get("min_turns") or 1),
            max_turns=int(data.get("max_turns") or 12),
            current_objective=str(data.get("current_objective") or ""),
            pacing_directive=str(data.get("pacing_directive") or ""),
            narrative_length_band=str(data.get("narrative_length_band") or "standard"),
            hook_pool=list(data.get("hook_pool") or []),
            unadapted_notes=list(data.get("unadapted_notes") or []),
            inheritance_conflict=bool(data.get("inheritance_conflict")),
        )


@dataclass
class NpcCandidate:
    """NPC 提案条目。"""

    item_id: str
    slug: str
    name: str
    arc_role: str = ""
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "slug": self.slug,
            "name": self.name,
            "arc_role": self.arc_role,
            "citations": [c.to_dict() for c in self.citations],
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NpcCandidate":
        return cls(
            item_id=str(data.get("item_id") or ""),
            slug=str(data.get("slug") or ""),
            name=str(data.get("name") or ""),
            arc_role=str(data.get("arc_role") or ""),
            citations=[
                Citation(
                    rel_path=str(c.get("rel_path") or ""),
                    line_start=int(c.get("line_start") or 0),
                    line_end=int(c.get("line_end") or 0),
                    quote=str(c.get("quote") or ""),
                )
                for c in (data.get("citations") or [])
                if isinstance(c, dict)
            ],
            confidence=float(data.get("confidence") or 0.0),
        )


@dataclass
class CheckpointProposal:
    """交给用户审批的完整检查点提案。"""

    job_id: str = ""
    source_kind: SourceKind = SourceKind.ADAPTATION
    volume_ref: str = ""
    volume_title: str = ""
    chapters: list[ProposalChapter] = field(default_factory=list)
    npc_candidates: list[NpcCandidate] = field(default_factory=list)
    #: 刻意未改编的原作内容及原因——防止模型悄悄丢掉整条线还不吭声。
    unadapted: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    #: 改编路径的时间线梳理结果（原创为 ``None``）。
    timeline: Timeline | None = None
    #: **确定性覆盖闭合结果**（见 ``service.coverage_report``）。
    #: 哪些原篇没被任何章覆盖、哪些关键情节没落点，在这里摆给用户看。
    coverage: dict[str, Any] = field(default_factory=dict)

    def all_item_ids(self) -> list[str]:
        ids: list[str] = []
        for chapter in self.chapters:
            ids.append(chapter.item_id)
            ids.extend(chapter.child_item_ids())
        ids.extend(n.item_id for n in self.npc_candidates)
        return ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_kind": self.source_kind.value,
            "volume_ref": self.volume_ref,
            "volume_title": self.volume_title,
            "chapters": [c.to_dict() for c in self.chapters],
            "npc_candidates": [n.to_dict() for n in self.npc_candidates],
            "unadapted": list(self.unadapted),
            "open_questions": list(self.open_questions),
            "stats": dict(self.stats),
            "timeline": self.timeline.to_dict() if self.timeline else None,
            "coverage": dict(self.coverage),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckpointProposal":
        return cls(
            job_id=str(data.get("job_id") or ""),
            source_kind=SourceKind(str(data.get("source_kind") or "adaptation")),
            volume_ref=str(data.get("volume_ref") or ""),
            volume_title=str(data.get("volume_title") or ""),
            chapters=[
                ProposalChapter.from_dict(c)
                for c in (data.get("chapters") or [])
                if isinstance(c, dict)
            ],
            npc_candidates=[
                NpcCandidate.from_dict(n)
                for n in (data.get("npc_candidates") or [])
                if isinstance(n, dict)
            ],
            unadapted=list(data.get("unadapted") or []),
            open_questions=list(data.get("open_questions") or []),
            stats=dict(data.get("stats") or {}),
            timeline=(
                Timeline.from_dict(data["timeline"])
                if isinstance(data.get("timeline"), Mapping)
                else None
            ),
            coverage=dict(data.get("coverage") or {}),
        )


class Decision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


@dataclass
class ApprovalDecisions:
    """逐条审批结果。键是 ``item_id``。"""

    round: int = 0
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    submitted: bool = False

    def status_of(self, item_id: str) -> Decision:
        raw = (self.decisions.get(item_id) or {}).get("status")
        try:
            return Decision(str(raw))
        except ValueError:
            return Decision.PENDING

    def note_of(self, item_id: str) -> str:
        return str((self.decisions.get(item_id) or {}).get("note") or "")

    def edited_of(self, item_id: str) -> dict[str, Any]:
        return dict((self.decisions.get(item_id) or {}).get("edited") or {})

    def record(
        self,
        item_id: str,
        status: Decision,
        *,
        actor: str = "",
        note: str = "",
        edited: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {"status": status.value}
        if actor:
            entry["actor"] = actor
        if note:
            entry["note"] = note
        if edited:
            entry["edited"] = edited
        self.decisions[item_id] = entry

    def regen_queue(self, proposal: CheckpointProposal) -> list[str]:
        """需要重新生成的 ``item_id``：被驳回或被打回编辑的条目。"""
        queue: list[str] = []
        for item_id in proposal.all_item_ids():
            if self.status_of(item_id) is Decision.REJECTED:
                queue.append(item_id)
        return queue

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "decisions": dict(self.decisions),
            "submitted": self.submitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalDecisions":
        return cls(
            round=int(data.get("round") or 0),
            decisions=dict(data.get("decisions") or {}),
            submitted=bool(data.get("submitted")),
        )


# --- 前作继承 -------------------------------------------------------------


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    SUSPECTED = "suspected"
    DEMOTED = "demoted"


@dataclass
class PlayerImpact:
    """前作中玩家造成、需要继承到新卷的一项影响。"""

    impact_id: str
    kind: str  # saved_npc / killed_npc / acquired_item / lost_item / changed_relation / ...
    subject: str
    detail: str
    actor: str = ""
    turn_no: int = 0
    confidence: Confidence = Confidence.SUSPECTED
    evidence_event_ids: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    reason: str = ""
    #: 描述这条影响对后续剧情意味着什么（「后续不得再把拉姆写成被困于宅邸」）。
    downstream_effect: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "impact_id": self.impact_id,
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
            "actor": self.actor,
            "turn_no": self.turn_no,
            "confidence": self.confidence.value,
            "evidence_event_ids": list(self.evidence_event_ids),
            "citations": [c.to_dict() for c in self.citations],
            "reason": self.reason,
            "downstream_effect": self.downstream_effect,
        }


@dataclass
class RosterEntry:
    """前作参战名单。"""

    player_id: str = ""
    display_name: str = ""
    character_name: str = ""
    character_code: str = ""
    participation_status: str = ""
    exit_reason: str = ""
    occupancy: str = ""  # 存活 / 阵亡 / 离队 / 未知

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NpcLifecycleEntry:
    stable_key: str = ""
    name: str = ""
    lifecycle_status: str = ""
    persistent: bool = False
    first_turn: int = 0
    last_turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContinuityBrief:
    """从前作 session 派生出的续卷简报。

    注意：**不取 ``world_state_json.facts``**——它是 200 条滑动窗口，前 2/3 早已
    被挤掉。所有事实来自 ``events`` 与 ``story_ledger``。
    """

    brief_version: int = 1
    source_session_id: str = ""
    source_world_slug: str = ""
    source_world_name: str = ""
    derived_at: str = ""
    turn_range: tuple[int, int] = (0, 0)
    state: str = ""
    roster: list[RosterEntry] = field(default_factory=list)
    dead_pcs: list[RosterEntry] = field(default_factory=list)
    retired_pcs: list[RosterEntry] = field(default_factory=list)
    npc_lifecycle: list[NpcLifecycleEntry] = field(default_factory=list)
    completed_milestones: list[str] = field(default_factory=list)
    uncompleted_milestones: list[str] = field(default_factory=list)
    #: ``milestone_id -> 人话标签``。上面两个字段是**机器 key**
    #: （``m_01_01_trust``），面板直接渲染会全是天书；标签来自账本 title
    #: （已完成的）与世界包声明（未完成的）两处。
    milestone_labels: dict[str, str] = field(default_factory=dict)
    carried_facts: list[dict[str, Any]] = field(default_factory=list)
    player_impacts: list[PlayerImpact] = field(default_factory=list)
    relationship_deltas: list[dict[str, Any]] = field(default_factory=list)
    lost_or_spent_items: list[dict[str, Any]] = field(default_factory=list)
    unfinished_goals: list[str] = field(default_factory=list)
    #: 有 ``suspected`` 项需要人工确认。
    needs_review: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.player_impacts or self.completed_milestones or self.roster)

    def confirmed_impacts(self) -> list[PlayerImpact]:
        return [i for i in self.player_impacts if i.confidence is Confidence.CONFIRMED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_version": self.brief_version,
            "source_session_id": self.source_session_id,
            "source_world_slug": self.source_world_slug,
            "source_world_name": self.source_world_name,
            "derived_at": self.derived_at,
            "turn_range": list(self.turn_range),
            "state": self.state,
            "roster": [r.to_dict() for r in self.roster],
            "dead_pcs": [r.to_dict() for r in self.dead_pcs],
            "retired_pcs": [r.to_dict() for r in self.retired_pcs],
            "npc_lifecycle": [n.to_dict() for n in self.npc_lifecycle],
            "completed_milestones": list(self.completed_milestones),
            "uncompleted_milestones": list(self.uncompleted_milestones),
            "milestone_labels": dict(self.milestone_labels),
            "carried_facts": list(self.carried_facts),
            "player_impacts": [i.to_dict() for i in self.player_impacts],
            "relationship_deltas": list(self.relationship_deltas),
            "lost_or_spent_items": list(self.lost_or_spent_items),
            "unfinished_goals": list(self.unfinished_goals),
            "needs_review": self.needs_review,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContinuityBrief":
        turn_range = data.get("turn_range") or [0, 0]
        return cls(
            brief_version=int(data.get("brief_version") or 1),
            source_session_id=str(data.get("source_session_id") or ""),
            source_world_slug=str(data.get("source_world_slug") or ""),
            source_world_name=str(data.get("source_world_name") or ""),
            derived_at=str(data.get("derived_at") or ""),
            turn_range=(int(turn_range[0]), int(turn_range[1])),
            state=str(data.get("state") or ""),
            roster=[RosterEntry(**r) for r in (data.get("roster") or []) if isinstance(r, dict)],
            dead_pcs=[RosterEntry(**r) for r in (data.get("dead_pcs") or []) if isinstance(r, dict)],
            retired_pcs=[
                RosterEntry(**r) for r in (data.get("retired_pcs") or []) if isinstance(r, dict)
            ],
            npc_lifecycle=[
                NpcLifecycleEntry(**n)
                for n in (data.get("npc_lifecycle") or [])
                if isinstance(n, dict)
            ],
            completed_milestones=list(data.get("completed_milestones") or []),
            uncompleted_milestones=list(data.get("uncompleted_milestones") or []),
            milestone_labels={
                str(k): str(v) for k, v in (data.get("milestone_labels") or {}).items()
            },
            carried_facts=list(data.get("carried_facts") or []),
            player_impacts=[
                PlayerImpact(
                    impact_id=str(i.get("impact_id") or ""),
                    kind=str(i.get("kind") or ""),
                    subject=str(i.get("subject") or ""),
                    detail=str(i.get("detail") or ""),
                    actor=str(i.get("actor") or ""),
                    turn_no=int(i.get("turn_no") or 0),
                    confidence=Confidence(str(i.get("confidence") or "suspected")),
                    evidence_event_ids=list(i.get("evidence_event_ids") or []),
                    reason=str(i.get("reason") or ""),
                    downstream_effect=str(i.get("downstream_effect") or ""),
                )
                for i in (data.get("player_impacts") or [])
                if isinstance(i, dict)
            ],
            relationship_deltas=list(data.get("relationship_deltas") or []),
            lost_or_spent_items=list(data.get("lost_or_spent_items") or []),
            unfinished_goals=list(data.get("unfinished_goals") or []),
            needs_review=bool(data.get("needs_review")),
            warnings=list(data.get("warnings") or []),
        )


# --- 反省 -----------------------------------------------------------------


@dataclass
class ReflectionFinding:
    """反省阶段对一条生成内容给出的判定。"""

    claim_path: str
    claim_text: str
    verdict: str  # supported / contradicted / unsupported / partial
    severity: str  # info / warning / blocking
    reason: str = ""
    citations: list[Citation] = field(default_factory=list)
    #: 引用核验失败的条目标记，用于识别模型编造出处。
    fabricated_citation: bool = False
    suggested_fix: dict[str, Any] | None = None
    applied: bool = False
    #: 覆盖类判定（covered / distorted / omitted），非覆盖类为空。
    #: **遗漏不可能靠改字修复**——只能加章节或确认放弃，所以这类结论
    #: 一律不进自动修复，而是推回审批门由操作者定夺。
    coverage_verdict: str = ""
    beat_id: str = ""

    @property
    def is_coverage(self) -> bool:
        return bool(self.coverage_verdict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_path": self.claim_path,
            "claim_text": self.claim_text,
            "verdict": self.verdict,
            "severity": self.severity,
            "reason": self.reason,
            "citations": [c.to_dict() for c in self.citations],
            "fabricated_citation": self.fabricated_citation,
            "suggested_fix": self.suggested_fix,
            "applied": self.applied,
            "coverage_verdict": self.coverage_verdict,
            "beat_id": self.beat_id,
        }


# --- 作业 -----------------------------------------------------------------


@dataclass
class StepRecord:
    """一次 LLM 步骤的记录，用于面板展示与排查。"""

    step: str
    phase: str
    at: str
    provider_id: str = ""
    attempts: int = 1
    input_chars: int = 0
    output_chars: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobRecord:
    """一个生成作业的完整状态。持久化为 ``job.json``。"""

    job_id: str = ""
    schema: int = 1
    created_at: str = ""
    updated_at: str = ""
    state: JobState = JobState.QUEUED
    phase: PhaseId = PhaseId.INTAKE
    actor: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    lint: dict[str, Any] = field(default_factory=dict)
    preflight: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    progress: float = 0.0
    message: str = ""
    error: str = ""
    resume_from_phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "schema": self.schema,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state.value,
            "phase": self.phase.value,
            "actor": self.actor,
            "request": dict(self.request),
            "source": dict(self.source),
            "artifacts": dict(self.artifacts),
            "steps": [s.to_dict() for s in self.steps],
            "lint": dict(self.lint),
            "preflight": dict(self.preflight),
            "outputs": dict(self.outputs),
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "resume_from_phase": self.resume_from_phase,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        record = cls(
            job_id=str(data.get("job_id") or ""),
            schema=int(data.get("schema") or 1),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            actor=str(data.get("actor") or ""),
            request=dict(data.get("request") or {}),
            source=dict(data.get("source") or {}),
            artifacts=dict(data.get("artifacts") or {}),
            lint=dict(data.get("lint") or {}),
            preflight=dict(data.get("preflight") or {}),
            outputs=dict(data.get("outputs") or {}),
            progress=float(data.get("progress") or 0.0),
            message=str(data.get("message") or ""),
            error=str(data.get("error") or ""),
            resume_from_phase=str(data.get("resume_from_phase") or ""),
        )
        try:
            record.state = JobState(str(data.get("state") or "queued"))
        except ValueError:
            record.state = JobState.NEEDS_ATTENTION
        try:
            record.phase = PhaseId(str(data.get("phase") or "intake"))
        except ValueError:
            record.phase = PhaseId.INTAKE
        record.steps = [
            StepRecord(
                step=str(s.get("step") or ""),
                phase=str(s.get("phase") or ""),
                at=str(s.get("at") or ""),
                provider_id=str(s.get("provider_id") or ""),
                attempts=int(s.get("attempts") or 1),
                input_chars=int(s.get("input_chars") or 0),
                output_chars=int(s.get("output_chars") or 0),
                usage=dict(s.get("usage") or {}),
                problems=list(s.get("problems") or []),
                ok=bool(s.get("ok", True)),
            )
            for s in (data.get("steps") or [])
            if isinstance(s, dict)
        ]
        return record


class PhaseResult(str, Enum):
    """阶段处理器返回值。"""

    OK = "ok"
    PARK = "park"  # 停在审批门，任务退出
    FAIL = "fail"


# --- 生成产物 -------------------------------------------------------------


@dataclass
class WorldDraft:
    """生成中的世界包草稿：世界包本体 + 配套 NPC 包。"""

    world: dict[str, Any] = field(default_factory=dict)
    npcs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"world": self.world, "npcs": self.npcs}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldDraft":
        return cls(
            world=dict(data.get("world") or {}),
            npcs=dict(data.get("npcs") or {}),
        )
