"""作业状态落盘。

为什么用文件而不是数据库表
--------------------------
建表意味着 ``DATABASE_SCHEMA_VERSION`` 从 12 升到 13、新增 ``_migrate_schema_13``、
接入迁移链、补迁移测试。代价不小，换来的是存一堆 100KB 级、schema 还在演进的嵌套
JSON——而且这些内容**本来就必须是文件**：面板要能查看和下载它们，生成产物也按约定
只落文件。

所以作业状态全部落在
``<plugin_data>/worldgen/jobs/<job_id>/`` 下，一个作业一个目录：

    job.json                  作业记录本身
    00_source_plan.json       卷解析结果
    10_checkpoints.draft.json 检查点提案
    11_checkpoints.approved.json 审批结果
    20_continuity.json        续卷简报
    50_continuity.md          续卷简报的可读版
    30_chapters/*.json        逐章草稿
    40_world.draft.json       世界包草稿
    50_npcs.draft.json        NPC 包草稿
    60_reflection.json        反省结论
    70_lint.json              静态校验报告
    80_world.json / 80_npcs.json 最终产物（同时复制到 worlds/）

若日后确需跨作业查询，``list_jobs()`` 就是唯一需要加索引的接缝，接口不变。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import JobRecord, JobState, PhaseId

LOGGER = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^wg_[A-Za-z0-9_\-]{6,64}$")
#: 允许读写产物的文件名白名单。作业产物名由代码确定，不接受任意路径。
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-.]{0,80}$")


class JobStoreError(RuntimeError):
    """作业存储操作失败。"""


#: 同一时间戳内的递增序号。**不能只靠时钟**：Windows 上 ``time.time()``
#: 的推进粒度约 15ms，连点两次"开始生成"完全可能落在同一个刻度里，
#: 那样算出来的 id 一模一样，第二个作业直接撞 ``作业已存在``。
_id_lock = threading.Lock()
_id_stamp = ""
_id_seq = 0


def new_job_id(now: float | None = None) -> str:
    """生成作业 id。用时间戳保证可读与可排序，不依赖随机数。

    时间戳之后跟一个**进程内单调递增**的序号，时钟不动也不会重复：
    同一个作业秒里的第二次调用得到 ``_0001``。同一进程内不可能撞号。
    """
    global _id_stamp, _id_seq

    moment = time.time() if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(moment))
    with _id_lock:
        if stamp == _id_stamp:
            _id_seq += 1
        else:
            _id_stamp = stamp
            _id_seq = 0
        seq = _id_seq
    return f"wg_{stamp}_{seq:04d}"


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再原子替换——崩溃时不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class JobStore:
    """作业目录的读写封装。"""

    root: Path
    logger: logging.Logger | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.logger = self.logger or LOGGER

    # --- 路径 ---------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(str(job_id or "")):
            raise JobStoreError(f"非法作业 id：{job_id!r}")
        return self.root / job_id

    def artifact_path(self, job_id: str, name: str) -> Path:
        """解析产物路径。``name`` 支持 ``30_chapters/ch_01.json`` 这类一层子目录。"""
        safe = str(name or "").replace("\\", "/").strip("/")
        parts = [p for p in safe.split("/") if p]
        if not parts or len(parts) > 2:
            raise JobStoreError(f"非法产物名：{name!r}")
        for part in parts:
            if not ARTIFACT_NAME_RE.match(part):
                raise JobStoreError(f"非法产物名片段：{part!r}")
        return self.job_dir(job_id).joinpath(*parts)

    # --- 作业记录 -----------------------------------------------------

    def create(self, record: JobRecord) -> JobRecord:
        directory = self.job_dir(record.job_id)
        if directory.exists():
            raise JobStoreError(f"作业已存在：{record.job_id}")
        directory.mkdir(parents=True, exist_ok=True)
        record.created_at = record.created_at or _now()
        record.updated_at = record.created_at
        self.save(record)
        return record

    def save(self, record: JobRecord) -> None:
        """保存作业记录。

        **产物索引由磁盘合并而来，不信任调用方手里那份。**
        各阶段一律是这个写法：``write_artifact(...)`` 之后紧跟 ``save(record)``，
        而那个 ``record`` 是**写产物之前**读到的——它身上的 ``artifacts`` 少一条，
        直接落盘就把刚登记的那条抹掉了。实测后果是 ``artifacts`` 永远是空的，
        面板上"这个作业产出了什么"一直显示不出来（文件其实好好躺在磁盘上）。

        产物只增不减（删除作业会连目录一起删），所以合并是安全的。
        """
        record.updated_at = _now()
        try:
            current = self.load(record.job_id)
        except JobStoreError:
            current = None
        if current is not None and current.artifacts:
            merged = dict(current.artifacts)
            merged.update(record.artifacts)
            record.artifacts = merged
        _atomic_write(self.job_dir(record.job_id) / "job.json", record.to_json())

    def load(self, job_id: str) -> JobRecord:
        path = self.job_dir(job_id) / "job.json"
        if not path.is_file():
            raise JobStoreError(f"作业不存在：{job_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JobStoreError(f"作业记录损坏：{job_id}（{exc}）") from exc
        if not isinstance(payload, dict):
            raise JobStoreError(f"作业记录不是对象：{job_id}")
        return JobRecord.from_dict(payload)

    def exists(self, job_id: str) -> bool:
        try:
            return (self.job_dir(job_id) / "job.json").is_file()
        except JobStoreError:
            return False

    def list_jobs(self, *, limit: int = 50) -> list[JobRecord]:
        """按更新时间倒序列出作业。损坏的作业记录跳过而不是整体失败。"""
        if not self.root.is_dir():
            return []
        records: list[JobRecord] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not JOB_ID_RE.match(child.name):
                continue
            try:
                records.append(self.load(child.name))
            except JobStoreError as exc:
                self.logger.warning("跳过损坏的作业目录 %s：%s", child.name, exc)
        records.sort(key=lambda r: r.updated_at or r.created_at, reverse=True)
        return records[: max(1, limit)]

    def delete(self, job_id: str) -> None:
        directory = self.job_dir(job_id)
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)

    # --- 产物 ---------------------------------------------------------

    def write_artifact(self, job_id: str, name: str, payload: Any) -> dict[str, Any]:
        """写入产物并登记到作业记录里。"""
        path = self.artifact_path(job_id, name)
        text = (
            payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
        )
        _atomic_write(path, text)

        record = self.load(job_id)
        entry = {
            "bytes": len(text.encode("utf-8")),
            "at": _now(),
            "name": name,
        }
        record.artifacts[name] = entry
        self.save(record)
        return entry

    def read_artifact(self, job_id: str, name: str) -> str:
        path = self.artifact_path(job_id, name)
        if not path.is_file():
            raise JobStoreError(f"产物不存在：{name}")
        return path.read_text(encoding="utf-8")

    def read_artifact_json(self, job_id: str, name: str) -> Any:
        return json.loads(self.read_artifact(job_id, name))

    def has_artifact(self, job_id: str, name: str) -> bool:
        try:
            return self.artifact_path(job_id, name).is_file()
        except JobStoreError:
            return False

    def list_artifacts(self, job_id: str) -> list[str]:
        return sorted(self.load(job_id).artifacts)

    def write_chapter(self, job_id: str, chapter_id: str, payload: Any) -> dict[str, Any]:
        return self.write_artifact(job_id, f"30_chapters/{chapter_id}.json", payload)

    def iter_chapters(self, job_id: str) -> Iterator[tuple[str, Any]]:
        directory = self.job_dir(job_id) / "30_chapters"
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                yield path.stem, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.logger.warning("跳过损坏的章节草稿 %s：%s", path.name, exc)

    # --- 崩溃恢复 -----------------------------------------------------

    def recover_orphans(self) -> list[str]:
        """把进程中途死掉留下的 ``running`` 作业标成待处理。

        **不自动重跑**：重跑会消耗 token 而没有征得同意。这里只把状态改掉并把
        ``resume_from_phase`` 指回它当时所在的阶段，由操作者决定是否继续。

        Returns:
            被改动的作业 id 列表。
        """
        recovered: list[str] = []
        for record in self.list_jobs(limit=500):
            if record.state is not JobState.RUNNING:
                continue
            try:
                record.resume_from_phase = record.phase.value
                record.state = JobState.NEEDS_ATTENTION
                record.message = "进程中断，作业停在「%s」阶段，可手动继续" % record.phase.value
                self.save(record)
                recovered.append(record.job_id)
            except Exception as exc:  # 恢复失败不能拖垮启动流程
                self.logger.warning("恢复作业 %s 失败：%s", record.job_id, exc)
        return recovered

    def resume_phase(self, record: JobRecord) -> PhaseId:
        """决定从哪个阶段继续。"""
        raw = record.resume_from_phase or record.phase.value
        try:
            return PhaseId(raw)
        except ValueError:
            return record.phase

    def total_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def prune(self, *, keep: int = 20) -> list[str]:
        """只保留最近 ``keep`` 个已完结作业，返回被删的 id。"""
        removed: list[str] = []
        terminal = [
            r
            for r in self.list_jobs(limit=500)
            if r.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
        ]
        for record in terminal[max(1, keep) :]:
            self.delete(record.job_id)
            removed.append(record.job_id)
        return removed


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
