"""世界包生成服务：阶段机、审批门、作业生命周期。

设计要点
--------
**审批门不占用协程。** 走到 ``CHECKPOINT_APPROVE`` 时任务直接退出，状态写进
``job.json``；操作者提交审批后重新起一个任务从 ``resume_from_phase`` 继续。
这样关浏览器、重载控制台、重启 bot 都不会丢掉审批状态，也不会挂着一个等几天的协程。

**每个请求都在毫秒级返回。** ``create`` 只做「落盘 + 起后台任务」，
真正的生成在 ``asyncio.create_task`` 里跑。控制台的 axios 客户端**没有设置超时**，
任何阻塞式长请求都会把界面挂死。

**进度走两条通道。** SSE（``EventBroker``）负责实时感，轮询 ``worldgen/job``
负责权威性——``EventBroker`` 的订阅队列 ``maxsize=50``，满了会静默丢最旧的，
所以 SSE 只能当"锦上添花"，不能当唯一来源。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import emit as emit_module
from . import prompts
from .scope import count_problems, arc_count_problems
from .continuity import derive_brief, inject_brief, render_brief_markdown
from .llm import WorldgenLLM, WorldgenLLMError
from .models import (
    ApprovalDecisions,
    CheckpointProposal,
    Citation,
    ContinuityBrief,
    Decision,
    JobRecord,
    JobState,
    NpcCandidate,
    PHASE_LABELS,
    PHASE_ORDER,
    PhaseId,
    PhaseResult,
    ProposalChapter,
    ProposalMilestone,
    SourceKind,
    StepRecord,
    Timeline,
)
from .store import JobStore, JobStoreError, new_job_id
from .steps import (
    validate_cast_excludes_replaced,
    validate_chapter_avoids_replaced_actors,
    validate_timeline,
    validator_for,
)
from .corpus import CorpusError, list_volumes, load_volume
from .retriever import load_or_build_index

LOGGER = logging.getLogger(__name__)

MAX_CHAPTERS_DEFAULT = 12
DEFAULT_PROFESSION_COUNT = 7
#: 时间线梳理的抽样篇数。要看出"多周目"得让模型看到足够多篇的开头——
#: 各篇反复出现同一个早晨，才是结构信号。
TIMELINE_SAMPLE_COUNT = 14


def _citations_of(raw: Any) -> list[Citation]:
    """把模型给的 ``source`` 转成 :class:`Citation` 列表。

    容忍单个对象与列表两种写法；缺 rel_path 的直接丢，不制造半截引用。
    """
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    result: list[Citation] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        rel_path = str(item.get("rel_path") or "").strip()
        if not rel_path:
            continue
        try:
            start = int(item.get("line_start") or 0)
            end = int(item.get("line_end") or 0)
        except (TypeError, ValueError):
            start = end = 0
        result.append(
            Citation(
                rel_path=rel_path,
                line_start=start,
                line_end=end,
                quote=str(item.get("quote") or ""),
            )
        )
    return result


def coverage_report(
    proposal: CheckpointProposal,
    *,
    timeline: Timeline | None,
    volume_files: Sequence[str],
) -> dict[str, Any]:
    """**确定性**地核对：原作的内容是不是被落下了。

    这是补上"反思看不见遗漏"那口洞的第一层。反思只能检查**草稿写了什么**
    （断言由草稿遍历而来），所以被整段丢掉的剧情产出零条断言，永远进不了视野。
    这里反过来查：**原作有什么、草稿接住了什么**，差集就是遗漏。

    两层：

    - ``missing_files`` —— 篇目级。原卷的每一篇，要么被某章取材，要么在
      ``unadapted`` 里点名。都没有 = 静默蒸发。
    - ``missing_beats`` —— 情节级。缝好的线里标了必现（``keep`` / ``adapt``）的
      每条情节，都要有章节在 ``beats`` 里认领。

    只报告，不擅自修改——这几项的去留是用户的决定（面板会摆给他看）。
    """
    claimed_files: set[str] = set()
    claimed_beats: set[str] = set()
    for chapter in proposal.chapters:
        claimed_files.update(str(f) for f in chapter.source_files)
        claimed_beats.update(str(b) for b in chapter.beats)

    # 模型在 unadapted 里点名放弃的，也算"有交代"，不算遗漏。
    declared_files: set[str] = set()
    declared_beats: set[str] = set()
    for item in proposal.unadapted:
        if not isinstance(item, Mapping):
            continue
        for key in ("source", "file", "rel_path", "source_files"):
            value = item.get(key)
            if isinstance(value, str):
                declared_files.add(value.strip())
            elif isinstance(value, (list, tuple)):
                declared_files.update(str(x).strip() for x in value)
        for key in ("beat_id", "beats"):
            value = item.get(key)
            if isinstance(value, str):
                declared_beats.add(value.strip())
            elif isinstance(value, (list, tuple)):
                declared_beats.update(str(x).strip() for x in value)

    missing_files = sorted(
        f for f in volume_files
        if f and f not in claimed_files and f not in declared_files
    )

    missing_beats: list[dict[str, Any]] = []
    uncovered_segments: list[str] = []
    if timeline is not None:
        for beat in timeline.required_beats:
            if beat.beat_id in claimed_beats or beat.beat_id in declared_beats:
                continue
            missing_beats.append(
                {
                    "beat_id": beat.beat_id,
                    "label": beat.label,
                    "merge_action": beat.merge_action.value,
                    "from_segments": list(beat.from_segments),
                    "adaptation_note": beat.adaptation_note,
                }
            )
        # 整条时间线一条情节都没被认领 = 整条线蒸发，单独点名
        claimed_segments = {
            seg
            for chapter in proposal.chapters
            for beat in timeline.merged
            if beat.beat_id in (chapter.beats or [])
            for seg in beat.from_segments
        }
        uncovered_segments = sorted(
            s.segment_id
            for s in timeline.segments
            if s.segment_id not in claimed_segments
            and any(s.source_files)
        )

    ok = not missing_files and not missing_beats
    return {
        "ok": ok,
        "missing_files": missing_files,
        "missing_beats": missing_beats,
        "uncovered_segments": uncovered_segments,
        "claimed_files": sorted(claimed_files),
        "claimed_beats": sorted(claimed_beats),
        "volume_files": [f for f in volume_files if f],
        "checked_beats": len(timeline.required_beats) if timeline else 0,
    }


def _spread_samples(docs: Sequence[Any], count: int) -> list[Any]:
    """从全卷**均匀**取样，而不是只看开头几篇。

    多周目作品的重复场景往往出现在中段；只取前 3 篇会把整卷看成线性的。
    """
    total = len(docs)
    if total <= count:
        return list(docs)
    step = total / count
    picked = [docs[min(total - 1, int(index * step))] for index in range(count)]
    # 去重但保持顺序（步长算出来可能撞同一篇）
    seen: set[int] = set()
    unique: list[Any] = []
    for index, doc in enumerate(picked):
        if id(doc) in seen:
            continue
        seen.add(id(doc))
        unique.append(doc)
    return unique


class WorldgenError(RuntimeError):
    """服务层错误，``problems`` 可回炉修复。"""

    def __init__(self, message: str, problems: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


class WorldgenService:
    """把一个生成请求推进成两个世界包文件。"""

    def __init__(
        self,
        *,
        context: Any,
        database: Any,
        broker: Any,
        store: JobStore,
        plugin_config: Any,
        corpus_root: str | Path,
        index_dir: str | Path,
        output_dir: str | Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self.context = context
        self.database = database
        self.broker = broker
        self.store = store
        self.plugin_config = plugin_config
        self.corpus_root = Path(corpus_root)
        self.index_dir = Path(index_dir)
        self.output_dir = Path(output_dir)
        self.logger = logger or LOGGER

        self._llm = WorldgenLLM(context, database, self._config)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._retrievers: dict[str, Any] = {}
        self._stopping = False

    # --- 配置 -----------------------------------------------------------

    def _config(self) -> Any:
        if callable(self.plugin_config):
            return self.plugin_config()
        return self.plugin_config

    def _pref(self, key: str, default: Any) -> Any:
        """读一条 worldgen 配置。

        优先认 :class:`tavern.config.TavernConfig` 的扁平字段（``worldgen_<key>``，
        与 ``world_market_*`` 同一套命名），退回到嵌套 ``worldgen`` 映射。
        两种都读不到就用默认值——配置缺失绝不能让生成炸掉。
        """
        try:
            config = self._config()
        except Exception:
            return default
        try:
            flat = getattr(config, f"worldgen_{key}", None)
            if flat is not None:
                return flat
        except Exception:
            pass
        try:
            nested = getattr(config, "worldgen", None)
            if isinstance(nested, Mapping):
                return nested.get(key, default)
            if nested is not None:
                return getattr(nested, key, default)
        except Exception:
            pass
        return default

    def _load_timeline(self, record: JobRecord) -> Timeline | None:
        """读回已梳理的时间线。没有（原创、或旧作业）就返回 ``None``。"""
        if not self.store.has_artifact(record.job_id, "05_timeline.json"):
            return None
        try:
            return Timeline.from_dict(
                self.store.read_artifact_json(record.job_id, "05_timeline.json")
            )
        except Exception as exc:
            self.logger.warning("读取时间线失败，按无时间线继续：%s", exc)
            return None

    @staticmethod
    def _preferred_provider(record: JobRecord) -> str:
        """这次作业在面板上选的模型。没选就返回空串，走插件默认那条链。"""
        return str(record.request.get("provider_id") or "").strip()

    # --- 索引（面板预热用）-----------------------------------------------

    def build_index(self, volume_ref: str, directory: str | Path) -> tuple[Any, bool]:
        """为某卷建/刷检索索引。

        **同步阻塞**（jieba 全卷分词，冷建约数秒），调用方要用
        ``asyncio.to_thread`` 包起来，否则会把事件循环卡住。
        已建好且原文没变时直接命中磁盘缓存，此时 ``rebuilt=False``。

        Returns:
            ``(retriever, rebuilt)``。
        """
        source = load_volume(directory)
        retriever, from_cache = load_or_build_index(source, self.index_dir)
        self._retrievers[f"volume:{volume_ref}"] = retriever
        return retriever, not from_cache

    # --- 进度 -----------------------------------------------------------

    async def _publish(self, record: JobRecord) -> None:
        """推一条进度。**每个步骤边界一条**，绝不逐 token 推。"""
        try:
            await self.broker.publish(
                {
                    "type": "worldgen",
                    "job_id": record.job_id,
                    "state": record.state.value,
                    "phase": record.phase.value,
                    "phase_label": PHASE_LABELS.get(record.phase, record.phase.value),
                    "progress": record.progress,
                    "message": record.message,
                }
            )
        except Exception as exc:  # 进度推送失败绝不影响生成
            self.logger.debug("推送进度失败：%s", exc)

    async def _advance(self, record: JobRecord, phase: PhaseId, message: str = "") -> None:
        record.phase = phase
        record.progress = _progress_of(phase)
        record.message = message or PHASE_LABELS.get(phase, phase.value)
        self.store.save(record)
        await self._publish(record)

    def _record_step(self, record: JobRecord, step: str, result: Any) -> None:
        record.steps.append(
            StepRecord(
                step=step,
                phase=record.phase.value,
                at=_now(),
                provider_id=str(getattr(result, "provider_id", "") or ""),
                attempts=int(getattr(result, "attempts", 1) or 1),
                input_chars=int(getattr(result, "input_chars", 0) or 0),
                output_chars=int(getattr(result, "output_chars", 0) or 0),
                usage=dict(getattr(result, "usage", {}) or {}),
            )
        )

    # --- 作业生命周期 ---------------------------------------------------

    async def create(self, request: Mapping[str, Any], actor: str = "") -> JobRecord:
        """创建作业。**毫秒级返回**，生成在后台任务里跑。"""
        from .scope import normalize_scope, scope_prompt
        request = dict(request)
        request.update(normalize_scope(request, min(12, int(self._pref("max_chapters", MAX_CHAPTERS_DEFAULT)))))
        if request.get('target_chapters') or request.get('target_milestones'):
            request['requirements'] = str(request.get('requirements') or '') + scope_prompt(request)
            request['preferences'] = request['requirements']
        kind = str(request.get("kind") or SourceKind.ADAPTATION.value)
        record = JobRecord(
            job_id=new_job_id(),
            state=JobState.QUEUED,
            phase=PhaseId.INTAKE,
            actor=actor,
            request=dict(request),
        )
        record.request["kind"] = kind
        self.store.create(record)
        await self._publish(record)
        self._spawn(record.job_id)
        return record

    def _spawn(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self.run(job_id), name=f"tavern-worldgen-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))

    async def run(self, job_id: str) -> None:
        """阶段机主循环。走到审批门就退出，由 ``approve`` 重新拉起。"""
        try:
            record = self.store.load(job_id)
        except JobStoreError as exc:
            self.logger.warning("作业不存在，无法运行：%s", exc)
            return

        if record.state is JobState.CANCELLED:
            return

        start = self.store.resume_phase(record) if record.resume_from_phase else record.phase
        record.state = JobState.RUNNING
        record.resume_from_phase = ""
        self.store.save(record)

        phases = PHASE_ORDER[PHASE_ORDER.index(start) :]
        for phase in phases:
            if record.state is JobState.CANCELLED:
                return
            await self._advance(record, phase)
            try:
                result = await self._PHASES[phase](self, record)
            except Exception as exc:
                self.logger.exception("作业 %s 在 %s 阶段失败", job_id, phase.value)
                record.state = JobState.FAILED
                record.error = f"{phase.value} 阶段失败：{exc}"
                record.message = record.error
                self.store.save(record)
                await self._publish(record)
                return

            if result is PhaseResult.PARK:
                # 任务就此退出——状态留在 job.json 里，等操作者提交审批。
                #
                # resume_from_phase 记的是**当前阶段**而不是下一阶段：提交后要重新
                # 进入审批阶段处理驳回队列（局部重写），处理完才继续往下走。
                # 记成下一阶段会让被驳回的条目被静默跳过。
                record.state = JobState.AWAITING_APPROVAL
                record.resume_from_phase = phase.value
                self.store.save(record)
                await self._publish(record)
                return
            if result is PhaseResult.FAIL:
                record.state = JobState.NEEDS_ATTENTION
                self.store.save(record)
                await self._publish(record)
                return

        record.state = JobState.SUCCEEDED
        record.progress = 1.0
        record.message = "生成完成"
        self.store.save(record)
        await self._publish(record)

    async def approve(
        self,
        job_id: str,
        *,
        decisions: Mapping[str, Any] | None = None,
        actor: str = "",
        submit: bool = False,
        accept_omissions: bool = False,
        regenerate: bool = False,
        global_feedback: str = "",
        scope_changes: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        """记录审批结果；``submit=True`` 时重新拉起任务继续往下走。

        Args:
            accept_omissions: 反省发现关键剧情遗漏、把作业退回来之后，
                操作者确认"就这样交付"。此时**直接跳到体检阶段**——
                草稿还是那份草稿，重跑一遍生成既费 token，也会再次撞上
                同一批遗漏，转成死循环。
            regenerate: 操作者选择"补章节"而不是接受。此时从头重跑生成段，
                并且**不再因为同样的遗漏退回**（本轮已经处理过了）。
        """
        record = self.store.load(job_id)
        if global_feedback or scope_changes is not None:
            if record.state is not JobState.AWAITING_APPROVAL:
                raise WorldgenError("整体修改只能在等待审批时发起")
            if not str(global_feedback).strip():
                raise ValueError("请填写整体修改意见")
            if len(global_feedback) > 6000:
                raise ValueError("整体修改意见不能超过 6000 字")
            # New revision job: no stale approvals, cached chapter drafts or
            # omission waivers survive. The previous proposal remains intact.
            revised = {k: v for k, v in record.request.items()
                       if not k.startswith('_') and k not in {'accept_omissions', 'target_chapters', 'target_milestones'}}
            import re
            prior = re.sub(r'<story_size_contract>.*?</story_size_contract>', '', str(revised.get('requirements') or ''), flags=re.S)
            proposal = self.store.read_artifact_json(job_id, '10_checkpoints.draft.json')
            summary = [{'title': c.get('title'), 'milestones': [m.get('label') for m in c.get('milestones', [])]}
                       for c in proposal.get('chapters', [])]
            revised['requirements'] = (prior + '\n【本轮整体修改意见，优先于旧方案】\n' + global_feedback.strip()
                                       + '\n【旧方案，仅供识别问题，不是必须保留的结构】\n' + str(summary)[:12000])
            revised['preferences'] = revised['requirements']
            revised.update(scope_changes if scope_changes is not None else {
                k: record.request[k] for k in ('target_chapters', 'target_milestones') if k in record.request})
            revised['revises_job_id'] = job_id
            return await self.create(revised, actor=actor)
        if accept_omissions:
            record.request["accept_omissions"] = True
        if regenerate:
            # 让这一轮重新生成，同时别再被同一批遗漏弹回来
            record.request["accept_omissions"] = True
        if record.state is not JobState.AWAITING_APPROVAL and not submit:
            # 允许在生成中反复调整审批意见（不提交就不推进）
            pass

        stored = ApprovalDecisions.from_dict(
            self.store.read_artifact_json(job_id, "11_checkpoints.approved.json")
            if self.store.has_artifact(job_id, "11_checkpoints.approved.json")
            else {}
        )
        if decisions:
            for item_id, raw in decisions.items():
                if not isinstance(raw, Mapping):
                    continue
                try:
                    status = Decision(str(raw.get("status") or "pending"))
                except ValueError:
                    status = Decision.PENDING
                stored.record(
                    item_id,
                    status,
                    actor=actor,
                    note=str(raw.get("note") or ""),
                    edited=dict(raw.get("edited") or {}) or None,
                )

        if submit:
            stored.submitted = True
            stored.round += 1
        self.store.write_artifact(job_id, "11_checkpoints.approved.json", stored.to_dict())

        if submit and record.state is JobState.AWAITING_APPROVAL:
            if record.resume_from_phase == PhaseId.COVERAGE_APPROVE.value:
                # 遗漏复核门：只有两条路——补章节重跑，或确认就这样交付。
                # 两条都要打上 accept_omissions，否则重跑后又会撞上同一批遗漏。
                record.request["accept_omissions"] = True
                record.resume_from_phase = (
                    PhaseId.GENERATE.value if regenerate
                    else PhaseId.COVERAGE_APPROVE.value
                )
                if regenerate:
                    record.state = JobState.QUEUED
            elif accept_omissions and not regenerate:
                record.resume_from_phase = PhaseId.GATE.value
            elif regenerate:
                record.request["accept_omissions"] = True
                record.resume_from_phase = PhaseId.GENERATE.value
                record.state = JobState.QUEUED
            else:
                record.resume_from_phase = record.resume_from_phase or PhaseId.INHERIT.value
            self.store.save(record)
            self._spawn(job_id)
        return self.store.load(job_id)

    async def cancel(self, job_id: str, actor: str = "") -> JobRecord:
        record = self.store.load(job_id)
        record.state = JobState.CANCELLED
        record.message = f"已由 {actor or '操作者'}取消"
        self.store.save(record)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        await self._publish(record)
        return record

    async def resume(self, job_id: str, actor: str = "") -> JobRecord:
        record = self.store.load(job_id)
        if record.state.terminal:
            raise WorldgenError(f"作业已结束（{record.state.value}），无法继续")
        record.resume_from_phase = record.resume_from_phase or record.phase.value
        record.state = JobState.QUEUED
        self.store.save(record)
        self._spawn(job_id)
        return record

    async def reaper_loop(self, *, interval: float = 300.0) -> None:
        """长驻清理任务：把进程死掉留下的 ``running`` 作业标成待处理。

        **不自动重跑**——重跑要花 token，必须由操作者点头。
        与 ``main.py`` 里既有的四个轮询任务一样：异常就地重试，绝不另起新循环。
        """
        while not self._stopping:
            try:
                recovered = self.store.recover_orphans()
                for job_id in recovered:
                    self.logger.warning("发现中断的生成作业：%s（已标记为待处理）", job_id)
            except Exception as exc:
                self.logger.warning("清理中断作业失败：%s", exc)
            await asyncio.sleep(interval)

    async def shutdown(self) -> None:
        self._stopping = True
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    # --- 各阶段 ---------------------------------------------------------

    async def _phase_intake(self, record: JobRecord) -> PhaseResult:
        request = record.request
        kind = str(request.get("kind") or SourceKind.ADAPTATION.value)
        if kind == SourceKind.ORIGIN.value:
            source = {"kind": "origin", "concept": str(request.get("concept") or "")}
            if not source["concept"].strip():
                raise WorldgenError("原创模式需要提供 concept（故事构想）")
            record.source = source
            self.store.save(record)
            return PhaseResult.OK

        root = Path(str(request.get("corpus_root") or self.corpus_root))
        volume_ref = str(request.get("volume_ref") or "").strip()
        if not volume_ref:
            raise WorldgenError("改编模式需要提供 volume_ref")
        directory = root / volume_ref
        if not directory.is_dir():
            available = [v["slug"] for v in list_volumes(root)]
            raise WorldgenError(
                f"卷目录不存在：{directory}。可用卷：{available}"
            )
        record.source = {
            "kind": "adaptation",
            "corpus_root": str(root),
            "volume_ref": volume_ref,
            "directory": str(directory),
        }
        self.store.save(record)
        return PhaseResult.OK

    async def _phase_sourcing(self, record: JobRecord) -> PhaseResult:
        if record.source.get("kind") != "adaptation":
            return PhaseResult.OK
        directory = record.source.get("directory")
        source = await asyncio.to_thread(load_volume, directory)
        retriever, from_cache = await asyncio.to_thread(
            load_or_build_index, source, self.index_dir
        )
        self._retrievers[record.job_id] = retriever
        plan = {
            "arc_title": source.arc_title,
            "doc_count": len(source.docs),
            "chars": source.total_chars,
            "outline": source.outline(),
            "index_from_cache": from_cache,
        }
        record.source["plan"] = plan
        self.store.write_artifact(record.job_id, "00_source_plan.json", plan)
        self.store.save(record)
        self.logger.info(
            "作业 %s：卷 %s，%d 篇 / %d 字，索引%s",
            record.job_id, source.arc_title, len(source.docs), source.total_chars,
            "命中缓存" if from_cache else "已重建",
        )
        return PhaseResult.OK

    # --- 步骤 0：时间线梳理 -------------------------------------------------

    async def _phase_timeline(self, record: JobRecord) -> PhaseResult:
        """理清时间结构，并把多条线里的情节**缝成一条**。

        原创没有原作可理，直接跳过。
        """
        if record.source.get("kind") != "adaptation":
            return PhaseResult.OK

        source = await asyncio.to_thread(
            load_volume, str(record.source.get("directory") or "")
        )
        # 抽样给得比卷级地图多：判断"多周目"靠的正是各篇开头反复出现的同一场景。
        samples = [
            {"file": doc.rel_path, "text": doc.text[:900]}
            for doc in _spread_samples(source.docs, TIMELINE_SAMPLE_COUNT)
        ]
        requirements = str(record.request.get("requirements") or "")
        payload, result, problems = await self._llm.generate_json(
            system_prompt=prompts.TIMELINE_SYSTEM,
            prompt=prompts.timeline_prompt(
                arc_title=source.arc_title,
                outline=source.outline(),
                samples=samples,
                requirements=requirements,
            ),
            # 把操作者的要求复述进问题清单：重试不重发 user prompt，模型得靠
            # 问题清单才知道"谁该被玩家取代"。
            validate=lambda payload: validate_timeline(payload, requirements=requirements),
            request_type="worldgen_timeline",
            preferred_provider=self._preferred_provider(record),
        )
        self._record_step(record, "timeline", result)

        timeline = Timeline.from_dict(dict(payload))
        self.store.write_artifact(record.job_id, "05_timeline.json", timeline.to_dict())

        confirmed = timeline.required_beats
        self.logger.info(
            "作业 %s 时间线：%s，%d 条线 / %d 条情节（其中必现 %d）",
            record.job_id, timeline.structure, len(timeline.segments),
            len(timeline.merged), len(confirmed),
        )
        if not confirmed:
            raise WorldgenError(
                "时间线梳理没有产出任何可用情节——它把整卷都判成不可改编了",
                problems,
            )
        record.source["timeline"] = {
            "structure": timeline.structure,
            "segments": len(timeline.segments),
            "merged": len(timeline.merged),
            "required": len(confirmed),
            "dropped": len(timeline.dropped),
        }
        self.store.save(record)
        return PhaseResult.OK

    async def _phase_checkpoint_extract(self, record: JobRecord) -> PhaseResult:
        if record.source.get("kind") == "origin":
            return await self._extract_origin(record)

        source = await asyncio.to_thread(load_volume, record.source["directory"])
        retriever = self._retrievers.get(record.job_id)
        if retriever is None:
            retriever, _ = await asyncio.to_thread(
                load_or_build_index, source, self.index_dir
            )
            self._retrievers[record.job_id] = retriever

        max_chapters = int(self._pref("max_chapters", MAX_CHAPTERS_DEFAULT))
        timeline = self._load_timeline(record)
        # 玩家位置：时间线声明了玩家取代了哪些原作角色，后面每一步都得按同一口径走。
        # 这条意图原先只喂给 timeline/arc_map，于是"谁有角色卡""每个里程碑由谁完成"
        # 两步完全看不到它——chapter030 的男主就是这样又长出一张卡的。
        player_position = prompts.player_position_block(
            timeline.replaced_characters if timeline else [],
            (timeline.player_role.note if timeline else ""),
        )

        # --- 1) 卷级场景地图 ---
        head_samples = [
            {"file": doc.rel_path, "text": doc.text[:1200]}
            for doc in source.docs[:3]
        ]
        payload, result, problems = await self._llm.generate_json(
            system_prompt=prompts.ARC_MAP_SYSTEM,
            prompt=prompts.arc_map_prompt(
                arc_title=source.arc_title,
                outline=source.outline(),
                head_samples=head_samples,
                requirements=str(record.request.get("requirements") or ""),
                merged_timeline=[b.to_dict() for b in timeline.merged] if timeline else None,
                player_position=player_position,
            ),
            validate=lambda p: validator_for("arc_map")(p) + arc_count_problems(p, record.request, max_chapters),
            request_type="worldgen_arc_map",
            preferred_provider=self._preferred_provider(record),
        )
        self._record_step(record, "arc_map", result)

        candidates = [
            c for c in (payload.get("candidate_chapters") or []) if isinstance(c, Mapping)
        ]
        if len(candidates) > max_chapters:
            raise WorldgenError(f"候选章节超过 {max_chapters} 章上限，需要合并重写，不能直接截断结尾")
        wanted = record.request.get('target_chapters')
        total = record.request.get('target_milestones')
        if wanted and len(candidates) != int(wanted):
            raise WorldgenError(f"章节规划未满足数量要求：要求 {wanted}，实际 {len(candidates)}；请调整整体方案后重试")
        if total and len(candidates) > int(total):
            raise WorldgenError("章节数超过里程碑总数，请减少章节数后重试")
        if not candidates:
            raise WorldgenError("卷级分析没有产出任何候选章节", problems)

        # --- 2) 逐章抽取 ---
        chapters: list[ProposalChapter] = []
        rejected: list[str] = []
        for index, candidate in enumerate(candidates):
            quota = (int(total) // len(candidates) + (index < int(total) % len(candidates))) if total else None
            chapter_requirements = str(record.request.get('requirements') or '')
            if quota is not None:
                chapter_requirements += f"\n本章必须恰好 {quota} 个里程碑；将关联成果合并，保留核心冲突和收束。"
            await self._advance(
                record,
                PhaseId.CHECKPOINT_EXTRACT,
                f"正在提取第 {index + 1}/{len(candidates)} 个检查点",
            )
            passages = self._passages_for_candidate(retriever, source, candidate)
            try:
                chapter_payload, chapter_result, chapter_problems = await self._llm.generate_json(
                    system_prompt=prompts.EXTRACT_CHAPTER_SYSTEM,
                    prompt=prompts.extract_chapter_prompt(
                        arc_title=source.arc_title,
                        candidate=candidate,
                        passages=passages,
                        already_approved=[c.chapter_id for c in chapters],
                        player_position=player_position,
                    ) + '\n【操作者整体要求】\n' + chapter_requirements,
                    # 机械核对：被玩家取代的角色不能当施动者。提示词是劝告，这里是门禁——
                    # 校验不过会带着问题清单重试，问题文案里点名了是谁。
                    validate=lambda payload: validator_for("extract_chapter")(payload)
                    + validate_chapter_avoids_replaced_actors(
                        payload, timeline.replaced_characters if timeline else []
                    ) + ([f"本章必须恰好 {quota} 个里程碑"] if quota is not None and len((payload.get('chapter') or {}).get('milestones') or []) != quota else []),
                    request_type="worldgen_extract_chapter",
                    preferred_provider=self._preferred_provider(record),
                )
            except WorldgenLLMError as exc:
                rejected.append(f"候选「{candidate.get('title')}」抽取失败：{exc}")
                continue
            self._record_step(record, f"extract_chapter[{index}]", chapter_result)
            chapter = self._chapter_from_payload(
                chapter_payload.get("chapter"), index, retriever
            )
            if chapter is None:
                rejected.append(
                    f"候选「{candidate.get('title')}」未产出有效章节"
                    + ("：" + "；".join(chapter_problems[:2]) if chapter_problems else "")
                )
                continue
            chapters.append(chapter)

        if not chapters:
            raise WorldgenError("没有任何候选章节通过抽取", rejected)

        # --- 3) NPC 名录 ---
        referenced: list[str] = []
        for chapter in chapters:
            for npc in chapter.key_npcs:
                ref = str(npc.get("ref") or "")
                if ref and ref not in referenced:
                    referenced.append(ref)
        cast_passages = self._passages_for_query(
            retriever, " ".join(referenced) or source.arc_title
        )
        try:
            cast_payload, cast_result, cast_problems = await self._llm.generate_json(
                system_prompt=prompts.CAST_SYSTEM,
                prompt=prompts.cast_prompt(
                    arc_title=source.arc_title,
                    referenced=referenced,
                    passages=cast_passages,
                    player_position=player_position,
                ),
                validate=lambda payload: validator_for("cast")(payload)
                + validate_cast_excludes_replaced(
                    payload, timeline.replaced_characters if timeline else []
                ),
                request_type="worldgen_cast",
                preferred_provider=self._preferred_provider(record),
            )
            self._record_step(record, "cast", cast_result)
            # 先剔除、再建候选：候选是名录装配失败时的兜底来源，若在这里留下
            # 被取代的角色，兜底路径会把刚剔掉的卡又装回去。
            npc_details, replaced_dropped = emit_module.drop_replaced_npcs(
                [item for item in (cast_payload.get("npcs") or []) if isinstance(item, Mapping)],
                timeline.replaced_characters if timeline else [],
            )
            npc_candidates = [
                NpcCandidate(
                    item_id=f"cp_npc_{i:02d}",
                    slug=str(item.get("slug") or ""),
                    name=str(item.get("name") or ""),
                    arc_role=str(item.get("personality") or ""),
                    confidence=0.7,
                )
                for i, item in enumerate(npc_details)
                if item.get("slug")
            ]
            record.request["_npc_details"] = npc_details
            if replaced_dropped:
                # 提示词 + 校验都没拦住时的最后一道：机械剔除，并记在作业上让面板显示。
                record.request["_replaced_npcs"] = replaced_dropped
                self.logger.warning(
                    "作业 %s：%d 个 NPC 是被玩家取代的原作角色，已从名录剔除（%s）",
                    record.job_id,
                    len(replaced_dropped),
                    "、".join(f"{d['name']}←{d['matched']}" for d in replaced_dropped),
                )
        except WorldgenLLMError as exc:
            self.logger.warning("NPC 名录抽取失败：%s", exc)
            npc_candidates = []
            rejected.append(f"NPC 名录抽取失败：{exc}")

        proposal = CheckpointProposal(
            job_id=record.job_id,
            source_kind=SourceKind.ADAPTATION,
            volume_ref=record.source.get("volume_ref", ""),
            volume_title=source.arc_title,
            chapters=chapters,
            npc_candidates=npc_candidates,
            unadapted=[
                dict(item)
                for item in (payload.get("unadapted") or [])
                if isinstance(item, Mapping)
            ],
            open_questions=[
                str(q) for q in (payload.get("open_questions") or []) if str(q).strip()
            ],
            stats={
                "candidate_count": len(candidates),
                "chapter_count": len(chapters),
                "rejected": rejected,
            },
            timeline=timeline,
        )
        # 覆盖闭合在**审批门之前**跑：这是让用户在做决定之前就看到
        # "哪几篇没人取材、哪几条关键情节没落点"的唯一时机。
        proposal.coverage = coverage_report(
            proposal,
            timeline=timeline,
            volume_files=[doc.rel_path for doc in source.docs],
        )
        if proposal.coverage.get("missing_files") or proposal.coverage.get("missing_beats"):
            self.logger.warning(
                "作业 %s 覆盖不完整：%d 篇未取材 / %d 条关键情节无落点",
                record.job_id,
                len(proposal.coverage["missing_files"]),
                len(proposal.coverage["missing_beats"]),
            )
        self.store.write_artifact(record.job_id, "10_checkpoints.draft.json", proposal.to_dict())
        self.store.save(record)
        return PhaseResult.OK

    async def _extract_origin(self, record: JobRecord) -> PhaseResult:
        concept = str(record.source.get("concept") or "")
        payload, result, problems = await self._llm.generate_json(
            system_prompt=prompts.ORIGIN_PLAN_SYSTEM,
            prompt=prompts.origin_plan_prompt(
                concept=concept, preferences=record.request.get("preferences")
            ),
            validate=lambda p: count_problems(p.get('chapters') or [], record.request),
            request_type="worldgen_origin_plan",
            preferred_provider=self._preferred_provider(record),
        )
        self._record_step(record, "origin_plan", result)
        chapters: list[ProposalChapter] = []
        for index, raw in enumerate(payload.get("chapters") or []):
            if not isinstance(raw, Mapping):
                continue
            chapters.append(
                ProposalChapter(
                    item_id=f"cp_ch_{index + 1:02d}",
                    chapter_id=f"ch_{index + 1:02d}_{slugify(raw.get('title'))}",
                    title=str(raw.get("title") or f"第 {index + 1} 章"),
                    subtitle=str(raw.get("subtitle") or ""),
                    min_turns=int(raw.get("min_turns") or 1),
                    max_turns=int(raw.get("max_turns") or 12),
                    current_objective=str(raw.get("current_objective") or ""),
                    pacing_directive=str(raw.get("pacing_directive") or ""),
                    hook_pool=[str(h) for h in (raw.get("hook_pool") or [])],
                    key_npcs=[dict(n) for n in (raw.get("key_npcs") or []) if isinstance(n, Mapping)],
                    milestones=[
                        ProposalMilestone(
                            item_id=f"cp_m_{index + 1:02d}_{mi + 1:02d}",
                            milestone_id=f"m_{index + 1:02d}_{mi + 1:02d}_{slugify(m.get('label'))}",
                            label=str(m.get("label") or ""),
                            evidence_required=list(m.get("evidence_required") or []),
                            confidence=0.7,
                        )
                        for mi, m in enumerate(raw.get("milestones") or [])
                        if isinstance(m, Mapping)
                    ],
                )
            )
        if not chapters:
            raise WorldgenError("原创大纲没有产出任何章节", problems)
        proposal = CheckpointProposal(
            job_id=record.job_id,
            source_kind=SourceKind.ORIGIN,
            volume_title=str(payload.get("premise") or "")[:60],
            chapters=chapters,
            stats={"chapter_count": len(chapters)},
        )
        self.store.write_artifact(record.job_id, "10_checkpoints.draft.json", proposal.to_dict())
        self.store.save(record)
        return PhaseResult.OK

    async def _phase_checkpoint_approve(self, record: JobRecord) -> PhaseResult:
        """审批门。只要没人提交，就一直停在这里。"""
        if not self.store.has_artifact(record.job_id, "11_checkpoints.approved.json"):
            self.store.write_artifact(
                record.job_id, "11_checkpoints.approved.json", ApprovalDecisions().to_dict()
            )
        decisions = ApprovalDecisions.from_dict(
            self.store.read_artifact_json(record.job_id, "11_checkpoints.approved.json")
        )
        # 先处理驳回队列，再看是否已提交——顺序反了的话，提交后会直接放行，
        # 驳回意见就被静默丢掉了。
        proposal = CheckpointProposal.from_dict(
            self.store.read_artifact_json(record.job_id, "10_checkpoints.draft.json")
        )
        from .scope import count_problems
        size_problems = count_problems([c.to_dict() for c in proposal.chapters], record.request)
        if size_problems:
            record.message = '数量不符合要求，需整体修改后重新提案：' + '；'.join(size_problems)
            return PhaseResult.PARK
        queue = decisions.regen_queue(proposal)
        if queue:
            await self._advance(
                record, PhaseId.CHECKPOINT_APPROVE,
                f"正在按你的意见重写 {len(queue)} 项检查点",
            )
            await self._regenerate(record, proposal, decisions, queue)
            # 重写后这些条目回到待审批，且需要操作者重新提交
            for item_id in queue:
                decisions.decisions.pop(item_id, None)
            decisions.submitted = False
            self.store.write_artifact(
                record.job_id, "11_checkpoints.approved.json", decisions.to_dict()
            )
            return PhaseResult.PARK

        if decisions.submitted:
            return PhaseResult.OK

        # 没人提交就一直停在这里
        return PhaseResult.PARK

    async def _regenerate(
        self,
        record: JobRecord,
        proposal: CheckpointProposal,
        decisions: ApprovalDecisions,
        queue: Sequence[str],
    ) -> None:
        """只重写被驳回的条目——一次调用一条，不必整卷重抽。"""
        retriever = self._retrievers.get(record.job_id)
        for item_id in queue:
            note = decisions.note_of(item_id)
            for chapter in proposal.chapters:
                for milestone in chapter.milestones:
                    if milestone.item_id != item_id:
                        continue
                    if retriever is None:
                        continue
                    passages = self._passages_for_query(
                        retriever, (note or milestone.label)[:60]
                    )
                    try:
                        payload, result, _ = await self._llm.generate_json(
                            system_prompt=prompts.REGEN_MILESTONE_SYSTEM,
                            prompt=prompts.regen_milestone_prompt(
                                chapter_context={
                                    "chapter_id": chapter.chapter_id,
                                    "title": chapter.title,
                                    "current_objective": chapter.current_objective,
                                    "sibling_milestones": [
                                        m.milestone_id
                                        for m in chapter.milestones
                                        if m.item_id != item_id
                                    ],
                                },
                                rejected={
                                    "milestone_id": milestone.milestone_id,
                                    "label": milestone.label,
                                },
                                note=note,
                                passages=passages,
                                player_position=prompts.player_position_block(
                                    proposal.timeline.replaced_characters
                                    if proposal.timeline
                                    else [],
                                    (
                                        proposal.timeline.player_role.note
                                        if proposal.timeline
                                        else ""
                                    ),
                                ),
                            ),
                            # 操作者驳回的理由常常正是「这里不该由男主来做」——
                            # 重写的那一条同样要过机械核对。
                            validate=lambda payload: validator_for("regen_milestone")(payload)
                            + validate_chapter_avoids_replaced_actors(
                                {"chapter": {"milestones": [payload.get("milestone") or {}]}},
                                (
                                    proposal.timeline.replaced_characters
                                    if proposal.timeline
                                    else []
                                ),
                            ),
                            request_type="worldgen_regen_milestone",
                            preferred_provider=self._preferred_provider(record),
                        )
                    except WorldgenLLMError as exc:
                        self.logger.warning("重写 %s 失败：%s", item_id, exc)
                        continue
                    self._record_step(record, f"regen[{item_id}]", result)
                    fresh = payload.get("milestone") or {}
                    if fresh.get("label"):
                        # id 保持不变——重写的是内容，不是身份
                        milestone.label = str(fresh["label"])
                        if fresh.get("evidence_required"):
                            milestone.evidence_required = list(fresh["evidence_required"])
        self.store.write_artifact(record.job_id, "10_checkpoints.draft.json", proposal.to_dict())

    async def _phase_inherit(self, record: JobRecord) -> PhaseResult:
        session_id = str(record.request.get("carry_from_session_id") or "").strip()
        if not session_id:
            record.source["continuity"] = {"mode": "canon"}
            self.store.save(record)
            return PhaseResult.OK

        brief = await derive_brief_async_safe(self.database, session_id)
        self.store.write_artifact(record.job_id, "20_continuity.json", brief.to_dict())
        self.store.write_artifact(
            record.job_id, "50_continuity.md", render_brief_markdown(brief)
        )
        record.source["continuity"] = {
            "mode": "continuation",
            "session_id": session_id,
            "impacts": len(brief.player_impacts),
            "needs_review": brief.needs_review,
        }
        self.store.save(record)
        return PhaseResult.OK

    async def _phase_generate(self, record: JobRecord) -> PhaseResult:
        proposal = CheckpointProposal.from_dict(
            self.store.read_artifact_json(record.job_id, "10_checkpoints.draft.json")
        )
        decisions = ApprovalDecisions.from_dict(
            self.store.read_artifact_json(record.job_id, "11_checkpoints.approved.json")
        )
        chapters = _apply_decisions(proposal, decisions)
        if not chapters:
            raise WorldgenError("没有任何已批准的章节，无法生成")

        name = str(record.request.get("name") or proposal.volume_title or "未命名世界")
        description = str(
            record.request.get("description") or proposal.volume_title or name
        )

        # --- 职业预设 ---
        professions: list[dict[str, Any]] = []
        try:
            card_payload, card_result, card_problems = await self._llm.generate_json(
                system_prompt=prompts.CARD_SYSTEM,
                prompt=prompts.card_prompt(
                    name=name,
                    description=description,
                    attribute_keys=[k for k, _, _ in emit_module.DEFAULT_ATTRIBUTES],
                    attribute_labels={k: lb for k, lb, _ in emit_module.DEFAULT_ATTRIBUTES},
                    profession_count=int(
                        self._pref("default_profession_count", DEFAULT_PROFESSION_COUNT)
                    ),
                ),
                validate=lambda p: validator_for("card")(
                    p, attribute_keys=[k for k, _, _ in emit_module.DEFAULT_ATTRIBUTES]
                ),
                request_type="worldgen_card",
                preferred_provider=self._preferred_provider(record),
            )
            self._record_step(record, "card", card_result)
            professions = [
                dict(p) for p in (card_payload.get("professions") or []) if isinstance(p, Mapping)
            ]
        except WorldgenLLMError as exc:
            raise WorldgenError(f"职业预设生成失败，无法装配世界包：{exc}") from exc

        # --- 散文文案 ---
        # 名录里的被取代角色在这里再收敛一次（幂等）：作业可能从本阶段 resume，
        # 那时 `_npc_details` 是早先写下的，抽取阶段的那道剔除没有经过。
        npc_details = record.request.get("_npc_details") or []
        generate_timeline = self._load_timeline(record)
        npc_details, replaced_dropped = emit_module.drop_replaced_npcs(
            npc_details, generate_timeline.replaced_characters if generate_timeline else []
        )
        if replaced_dropped:
            record.request["_npc_details"] = npc_details
            record.request["_replaced_npcs"] = replaced_dropped
        brief = None
        if self.store.has_artifact(record.job_id, "20_continuity.json"):
            brief = ContinuityBrief.from_dict(
                self.store.read_artifact_json(record.job_id, "20_continuity.json")
            )
        continuity_facts = [
            _impact_statement(i) for i in (brief.confirmed_impacts() if brief else [])
        ]
        # 开场白与系统提示也要按玩家位置写：玩家就是主角，别把被取代的人写成行动主体。
        payload, result, problems = await self._llm.generate_json(
            system_prompt=prompts.PROSE_SYSTEM,
            prompt=prompts.prose_prompt(
                name=name,
                description=description,
                chapters=[c.to_dict() for c in chapters],
                npcs=npc_details,
                continuity_facts=continuity_facts,
                player_position=prompts.player_position_block(
                    generate_timeline.replaced_characters if generate_timeline else [],
                    (generate_timeline.player_role.note if generate_timeline else ""),
                ),
            ) + '\n【操作者整体要求】\n' + str(record.request.get('requirements') or ''),
            validate=validator_for("prose"),
            request_type="worldgen_prose",
            preferred_provider=self._preferred_provider(record),
        )
        self._record_step(record, "prose", result)
        if problems:
            self.logger.warning("散文未完全过校验：%s", problems[:3])

        record.request["_prose"] = dict(payload)
        record.request["_professions"] = professions
        record.request["_resolved_name"] = name
        record.request["_resolved_description"] = description
        self.store.save(record)

        draft = emit_module.build_world(
            slug=str(record.request.get("slug") or slugify(name)),
            name=name,
            description=description,
            chapters=chapters,
            prose=payload,
            professions=professions,
            brief=brief,
        )
        npc_payload = emit_module.build_npcs(
            slug=draft["slug"],
            npcs=npc_details or [
                {"slug": c.slug, "name": c.name, "prompt": c.arc_role}
                for c in proposal.npc_candidates
            ] or [{"slug": "npc_placeholder", "name": "占位", "prompt": "占位角色。"}],
        )
        # 章节引用与 NPC 名录是两步独立模型调用，判据相反（章节照原文给每个
        # 人名写 ref，名录只收"玩家真能交互的"），必须在这里对一次账。
        draft, dropped_refs = emit_module.reconcile_npc_refs(draft, npc_payload)
        if dropped_refs:
            # 记在作业上而不是静默丢弃：这是"世界比原文窄"的一次真实取舍，
            # 操作者应当看得见（面板的交付产物区会列出来）。
            record.request["_dropped_npc_refs"] = dropped_refs
            self.logger.warning(
                "作业 %s：%d 条章节 NPC 引用没有对应角色卡，已摘除（%s）",
                record.job_id,
                len(dropped_refs),
                "、".join(sorted({d["ref"] for d in dropped_refs})[:8]),
            )
        self.store.write_artifact(record.job_id, "40_world.draft.json", draft)
        self.store.write_artifact(record.job_id, "50_npcs.draft.json", npc_payload)
        return PhaseResult.OK

    async def _phase_reflect(self, record: JobRecord) -> PhaseResult:
        if not self.store.has_artifact(record.job_id, "40_world.draft.json"):
            return PhaseResult.OK

        world = self.store.read_artifact_json(record.job_id, "40_world.draft.json")
        npcs = self.store.read_artifact_json(record.job_id, "50_npcs.draft.json")
        retriever = self._retrievers.get(record.job_id)

        from .reflect import reflect_source, summarize

        if retriever is None:
            report = {"mode": "consistency", "findings": [], "warnings": ["没有素材索引，跳过原文核对"]}
        else:
            reflection = await reflect_source(
                llm=self._llm,
                retriever=retriever,
                world=world,
                npcs=npcs,
                rounds=int(self._pref("reflect_rounds", 2)),
                autofix_warnings=bool(self._pref("reflect_autofix_warnings", False)),
                preferred_provider=self._preferred_provider(record),
                # 缝好的线是覆盖检查的清单——没有它，反思依旧看不见"少写了什么"。
                timeline=self._load_timeline(record),
            )
            report = reflection.to_dict()
            self.logger.info("作业 %s 反省：%s", record.job_id, summarize(reflection))

        self.store.write_artifact(record.job_id, "60_reflection.json", report)
        self.store.write_artifact(record.job_id, "40_world.draft.json", world)
        return PhaseResult.OK

    async def _phase_coverage_approve(self, record: JobRecord) -> PhaseResult:
        """遗漏复核门：反省发现关键剧情没落点时，在这里停下来问操作者。

        为什么单独一个门，而不是退回检查点审批门：退回检查点门的话，
        操作者一按"继续"就会重新生成 → 再次发现同一批遗漏 → 再次退回，
        直接转成死循环。这里是一个**单向**的门——过了就是"确认过、继续交付"。
        """
        if record.request.get("accept_omissions"):
            return PhaseResult.OK

        report_path = "60_reflection.json"
        omissions: list[dict[str, Any]] = []
        if self.store.has_artifact(record.job_id, report_path):
            report = self.store.read_artifact_json(record.job_id, report_path) or {}
            omissions = list(report.get("coverage_omitted") or [])

        if not omissions:
            return PhaseResult.OK

        self.store.write_artifact(
            record.job_id, "65_omissions.json", {"omissions": omissions}
        )
        record.state = JobState.AWAITING_APPROVAL
        record.message = (
            f"反省发现 {len(omissions)} 条关键剧情没有落点："
            "「补章节」会重新生成，「就这样交付」直接体检落盘"
        )
        self.store.save(record)
        await self._publish(record)
        self.logger.warning(
            "作业 %s 覆盖检查发现 %d 条遗漏，停在复核门",
            record.job_id, len(omissions),
        )
        return PhaseResult.PARK

    async def _phase_gate(self, record: JobRecord) -> PhaseResult:
        world = self.store.read_artifact_json(record.job_id, "40_world.draft.json")
        npcs = self.store.read_artifact_json(record.job_id, "50_npcs.draft.json")
        from .scope import count_problems
        size_problems = count_problems(world.get('rules', {}).get('progress', {}).get('chapters', []), record.request)
        if size_problems:
            raise WorldgenError('交付数量不符合要求', size_problems)

        # 对账（幂等）：本轮修复之前起草的作业，草稿里可能还留着没有角色卡的
        # 章节引用、或者运行时不认的检定模式。在这里再收敛一次并**写回**，
        # 于是被这道闸门打回的作业 resume 一下就能过——不必把二十来分钟的
        # 生成整段重跑。
        world, dropped_refs = emit_module.reconcile_npc_refs(world, npcs)
        world, mode_note = emit_module.reconcile_resolution_mode(world)

        # 被玩家取代的角色同样在这里再收敛一次。理由和上面一样：本阶段修复之前
        # 起草的作业（例如 chapter030 那份），草稿里的 NPC 包还留着男主的卡，
        # resume 一下就该干净地交付，不必把二十来分钟的生成整段重跑。
        gate_timeline = self._load_timeline(record)
        gate_replaces = gate_timeline.replaced_characters if gate_timeline else []
        npc_items = list(npcs.get("items") or []) if isinstance(npcs, Mapping) else []
        kept_items, replaced_dropped = emit_module.drop_replaced_npcs(npc_items, gate_replaces)
        if replaced_dropped:
            npcs = {**dict(npcs), "items": kept_items}
            self.store.write_artifact(record.job_id, "50_npcs.draft.json", npcs)
            record.request["_replaced_npcs"] = replaced_dropped
            # 名录变了，章节引用要跟着重新对账
            world, more_dropped = emit_module.reconcile_npc_refs(world, npcs)
            dropped_refs = list(dropped_refs) + list(more_dropped)
            self.logger.warning(
                "作业 %s：交付前从 NPC 包剔除 %d 个被玩家取代的原作角色（%s）",
                record.job_id,
                len(replaced_dropped),
                "、".join(d["name"] for d in replaced_dropped),
            )

        if dropped_refs or mode_note:
            self.store.write_artifact(record.job_id, "40_world.draft.json", world)
        if dropped_refs:
            record.request["_dropped_npc_refs"] = dropped_refs
        if mode_note:
            record.request["_resolution_mode_fix"] = mode_note
            self.logger.warning(
                "作业 %s：检定模式不是运行时可认的值，已修正（%s）",
                record.job_id, mode_note,
            )

        report: dict[str, Any]
        try:
            gates = emit_module.run_gates(world, npcs)
            report = {
                "ok": True,
                "lint": gates["lint"],
                "preflight_summary": gates["preflight"].get("summary", {}),
            }
        except emit_module.EmitError as exc:
            report = {"ok": False, "problems": exc.problems}
            self.store.write_artifact(record.job_id, "70_lint.json", report)
            record.lint = {"ok": False, "problems": list(exc.problems)}
            record.error = str(exc)
            record.message = "交付闸门未通过，需要回炉"
            self.store.save(record)
            return PhaseResult.FAIL
        if dropped_refs:
            report["dropped_npc_refs"] = dropped_refs
        if mode_note:
            report["resolution_mode_fix"] = mode_note
        # 面板的「交付产物」区读的是 record 上的这两个字段。此前它们**从来没被
        # 写过**，于是那一行永远显示「结构校验 0 项错误 / 0 项警告」、协议体检
        # 永远显示「未通过」——通过与否全靠嘴说，数字全是默认值。
        record.lint = dict(gates["lint"])
        record.preflight = {
            "compatible": bool(gates["preflight"].get("compatible")),
            "summary": dict(gates["preflight"].get("summary") or {}),
        }
        self.store.write_artifact(record.job_id, "70_lint.json", report)
        self.store.save(record)
        return PhaseResult.OK

    async def _phase_emit(self, record: JobRecord) -> PhaseResult:
        world = self.store.read_artifact_json(record.job_id, "40_world.draft.json")
        npcs = self.store.read_artifact_json(record.job_id, "50_npcs.draft.json")
        slug = str(world.get("slug") or "")
        paths = emit_module.write_packages(
            output_dir=self.output_dir,
            slug=slug,
            world=world,
            npcs=npcs,
            overwrite=bool(self._pref("allow_overwrite", False)),
        )
        self.store.write_artifact(record.job_id, "80_world.json", world)
        self.store.write_artifact(record.job_id, "80_npcs.json", npcs)
        record.outputs = paths
        record.message = f"已交付：{Path(paths['world']).name} / {Path(paths['npcs']).name}"
        self.store.save(record)
        return PhaseResult.OK

    # --- 辅助 -----------------------------------------------------------

    def _passages_for_candidate(
        self, retriever: Any, source: Any, candidate: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        files = [str(f) for f in (candidate.get("source_files") or [])]
        picked: list[dict[str, Any]] = []
        for passage in retriever.passages:
            if passage.file in files:
                picked.append(
                    {
                        "rel_path": passage.file,
                        "line_start": passage.line_start,
                        "line_end": passage.line_end,
                        "text": passage.text,
                    }
                )
        # 候选没给文件或文件不匹配时，退回检索
        if not picked:
            return self._passages_for_query(retriever, str(candidate.get("title") or ""))
        return picked[:20]

    def _passages_for_query(self, retriever: Any, query: str, *, top_k: int = 6) -> list[dict[str, Any]]:
        hits = retriever.search(query or "", top_k=top_k)
        return [
            {
                "rel_path": h.passage.file,
                "line_start": h.passage.line_start,
                "line_end": h.passage.line_end,
                "text": h.passage.text,
            }
            for h in hits
        ]

    def _chapter_from_payload(
        self, payload: Any, index: int, retriever: Any
    ) -> ProposalChapter | None:
        if not isinstance(payload, Mapping):
            return None
        title = str(payload.get("title") or "").strip()
        if not title:
            return None
        milestones = [
            ProposalMilestone(
                item_id=f"cp_m_{index + 1:02d}_{mi + 1:02d}",
                milestone_id=f"m_{index + 1:02d}_{mi + 1:02d}_{slugify(m.get('label'))}",
                label=str(m.get("label") or ""),
                evidence_required=list(m.get("evidence_required") or []),
                # 里程碑的原文出处。**以前这里不填**，于是 models 里那个
                # ``citations`` 字段永远是空的，面板上的引用行跟着空白一片。
                citations=_citations_of(m.get("source")),
                confidence=0.7,
            )
            for mi, m in enumerate(payload.get("milestones") or [])
            if isinstance(m, Mapping)
        ]
        if not milestones:
            return None
        return ProposalChapter(
            item_id=f"cp_ch_{index + 1:02d}",
            chapter_id=f"ch_{index + 1:02d}_{slugify(title)}",
            title=title,
            subtitle=str(payload.get("subtitle") or ""),
            min_turns=int(payload.get("min_turns") or 1),
            max_turns=int(payload.get("max_turns") or 12),
            current_objective=str(payload.get("current_objective") or ""),
            pacing_directive=str(payload.get("pacing_directive") or ""),
            hook_pool=[str(h) for h in (payload.get("hook_pool") or [])],
            key_npcs=[
                {"ref": str(n.get("ref")), "role": str(n.get("role") or "")}
                for n in (payload.get("key_npcs") or [])
                if isinstance(n, Mapping) and n.get("ref")
            ],
            milestones=milestones,
            rationale=str(payload.get("rationale") or ""),
            confidence=0.7,
            # 取材篇目与承接情节**必须留下**：覆盖检查全靠它们。
            # 这两项以前在这里被直接丢掉，于是"整篇没被任何章覆盖"
            # 这件事在流程里完全无从查起。
            source_files=[
                str(x) for x in (payload.get("source_files") or []) if str(x).strip()
            ],
            beats=[str(x) for x in (payload.get("beats") or []) if str(x).strip()],
            source_spans=_citations_of(payload.get("source")),
        )

    #: 阶段分发表。放在类体末尾，便于一眼看清阶段全集。
    _PHASES: dict[PhaseId, Any] = {}


WorldgenService._PHASES = {
    PhaseId.INTAKE: WorldgenService._phase_intake,
    PhaseId.SOURCING: WorldgenService._phase_sourcing,
    PhaseId.TIMELINE: WorldgenService._phase_timeline,
    PhaseId.CHECKPOINT_EXTRACT: WorldgenService._phase_checkpoint_extract,
    PhaseId.CHECKPOINT_APPROVE: WorldgenService._phase_checkpoint_approve,
    PhaseId.INHERIT: WorldgenService._phase_inherit,
    PhaseId.GENERATE: WorldgenService._phase_generate,
    PhaseId.REFLECT: WorldgenService._phase_reflect,
    PhaseId.COVERAGE_APPROVE: WorldgenService._phase_coverage_approve,
    PhaseId.GATE: WorldgenService._phase_gate,
    PhaseId.EMIT: WorldgenService._phase_emit,
}


# --- 模块级辅助 -----------------------------------------------------------


def _next_phase(phase: PhaseId) -> PhaseId:
    index = PHASE_ORDER.index(phase)
    return PHASE_ORDER[min(index + 1, len(PHASE_ORDER) - 1)]


def _progress_of(phase: PhaseId) -> float:
    return round(PHASE_ORDER.index(phase) / max(1, len(PHASE_ORDER) - 1), 3)


def slugify(value: Any) -> str:
    import re

    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return text[:24] or "chapter"


def _apply_decisions(
    proposal: CheckpointProposal, decisions: ApprovalDecisions
) -> list[ProposalChapter]:
    """把审批结果落到提案上：被批准的进生成，被驳回的剔除，编辑过的覆盖。"""
    result: list[ProposalChapter] = []
    for chapter in proposal.chapters:
        status = decisions.status_of(chapter.item_id)
        if status is Decision.REJECTED:
            continue
        edited = decisions.edited_of(chapter.item_id)
        if edited:
            if edited.get("title"):
                chapter.title = str(edited["title"])
            if edited.get("current_objective"):
                chapter.current_objective = str(edited["current_objective"])

        kept: list[ProposalMilestone] = []
        for milestone in chapter.milestones:
            m_status = decisions.status_of(milestone.item_id)
            if m_status is Decision.REJECTED:
                continue
            m_edited = decisions.edited_of(milestone.item_id)
            if m_edited:
                if m_edited.get("label"):
                    milestone.label = str(m_edited["label"])
                if m_edited.get("evidence_required"):
                    milestone.evidence_required = list(m_edited["evidence_required"])
            kept.append(milestone)
        if not kept:
            continue
        chapter.milestones = kept
        result.append(chapter)
    return result


def _impact_statement(impact: Any) -> str:
    subject = str(getattr(impact, "subject", "") or "").strip()
    kind = str(getattr(impact, "kind", "") or "")
    if not subject:
        return ""
    if kind == "pc_death":
        return f"{subject}已经阵亡，不会再登场"
    if kind == "saved_npc":
        return f"{subject}已被队伍带离险境"
    return f"{subject}：{getattr(impact, 'detail', '')}"


async def derive_brief_async_safe(database: Any, session_id: str) -> ContinuityBrief:
    """派生续卷简报；失败时返回空简报而不是让整个作业崩掉。

    **连接必须在 worker 线程里开**：``derive_brief`` 要的是一个已经打开的
    连接（它刻意不依赖 database 对象，好让测试直接对着临时库跑）。早先这里
    把 ``database._connect`` 这个**工厂**当连接传了进去，于是
    ``connection.execute(...)`` 直接 ``AttributeError``——被这个函数自己的
    ``except`` 兜成"派生失败"，**继承功能从来没有真正跑通过**：每次生成都
    静默拿到一份空简报，按原剧情走。面板上看到的就是那句
    「⚠️ 派生失败：'function' object has no attribute 'execute'」。
    """
    def derive() -> ContinuityBrief:
        with database._connect() as connection:
            return derive_brief(connection, session_id)

    try:
        return await database._run(derive)
    except Exception as exc:
        LOGGER.warning("派生续卷简报失败（%s）：%s，按原剧情继续", session_id, exc)
        return ContinuityBrief(source_session_id=session_id, warnings=[f"派生失败：{exc}"])


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S")
