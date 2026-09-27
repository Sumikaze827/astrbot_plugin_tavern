"""世界包生成 agent 的控制台端点。

以 mixin 的形式挂到 :class:`tavern.web_console.TavernWebConsole` 上——
``web_console.py`` 已经有 16 万字节，再往里塞十几个 handler 只会让它更难读。

约定与 ``web_console`` 保持一致：每个 handler

1. 先 ``self._username()`` 鉴权（**不能漏**，这是控制台的既有纪律）；
2. ``payload = await self._payload()`` 取请求体；
3. 成功 ``json_response({...})``，失败一律 ``self._handle_error(exc)``。

命名注意：handler **不能**叫 ``worldgen``。控制台既有的 ``extensions`` 路由
就踩过这个坑——同名属性把注册表对象当成了 HTTP handler，``GET /extensions``
直接 500。所以这里统一加 ``worldgen_`` 前缀，服务实例走 ``_worldgen_service``。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api.web import json_response

from .corpus import CorpusError, list_novels, list_volumes, load_volume
from .lint import lint_world_package
from .models import JobState
from .service import WorldgenError, derive_brief_async_safe, slugify

#: 已经收场的副本状态：后果已经定型。**不再用来过滤**——没完结的档也可以被继承，
#: 只是面板会标出来，提醒操作者"后果只到目前為止"。现在只用它排序（收场的靠前）。
ENDED_SESSION_STATES = frozenset({"finished", "closed"})

#: 面板能直接读的产物白名单。**不开放任意文件名**——作业目录里还有
#: ``job.json`` 等内部状态，让前端点名读取等于开了一个任意文件读。
_ARTIFACT_WHITELIST = frozenset(
    {
        "00_source_plan.json",
        "05_timeline.json",
        "10_checkpoints.draft.json",
        "11_checkpoints.approved.json",
        "20_continuity.json",
        "40_world.draft.json",
        "50_npcs.draft.json",
        "60_reflection.json",
        "65_omissions.json",
        "70_lint.json",
        # 交付产物本身。**必须开放**：面板的「导入这个世界」要把它俩按顺序
        # 喂给既有的 worlds/import 与 characters/import 两条通道。这两份也就
        # 是写进 worlds/ 的那两个文件（世界市场本来就能读到），不构成新的暴露。
        "80_world.json",
        "80_npcs.json",
    }
)

def _derive_world_slug(*, kind: str, novel: str, volume_ref: str, name: str) -> str:
    """给世界包推一个**有意义**的标识。

    不能直接用 :func:`slugify`——它按 ASCII 过滤，中文标题会被整条剃光，
    只剩它的兜底值 ``"chapter"``，每个中文世界都撞成同一个 slug（然后第二个
    就写不出去）。改编走 ``小说-卷``（``re0-ch-chapter030``），既稳定又能一眼
    看出改编自哪一卷；原创才退到标题的 ASCII 部分。
    """
    if kind == "adaptation" and novel and volume_ref:
        candidate = slugify(f"{novel}-{volume_ref}")
        if candidate and candidate != "chapter":
            return candidate[:64]
    candidate = slugify(name)
    if candidate and candidate != "chapter":
        return candidate[:64]
    # 中文原创且没给出可用的 ASCII：用时间戳保证唯一，别让两个作业抢同一个文件名。
    return "world-" + time.strftime("%Y%m%d-%H%M%S")


class WorldgenConsoleRoutes:
    """挂在 ``TavernWebConsole`` 上的世界包生成端点。"""

    def _worldgen(self) -> Any:
        """取生成服务。未启用时给一句人话，而不是 ``AttributeError``。"""
        service = getattr(self, "_worldgen_service", None)
        if service is None:
            raise ValueError("世界包生成服务未启用（请检查插件配置的 worldgen.enabled）")
        return service

    def _worldgen_config(self) -> Any:
        from ..config import TavernConfig

        return TavernConfig.from_mapping(self.plugin_config)

    # --- 语料 ---------------------------------------------------------------

    async def worldgen_corpus(self):
        """列出可改编的**小说**（每部下含若干卷）。"""
        try:
            self._username()
            service = self._worldgen()
            root = Path(service.corpus_root)
            if not root.is_dir():
                return json_response(
                    {
                        "root": str(root),
                        "exists": False,
                        "novels": [],
                        "hint": "语料根目录不存在，请在插件配置的 worldgen.corpus_root 里指定",
                    }
                )
            novels = list_novels(root)
            return json_response(
                {
                    "root": str(root),
                    "exists": True,
                    "novels": novels,
                    "hint": "" if novels else "这个目录下没有找到可改编的小说"
                    "（需要 <小说>/<卷>/README.md 结构）",
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_volumes(self):
        """列出某一部小说下的全部卷。"""
        try:
            self._username()
            service = self._worldgen()
            payload = await self._payload()
            novel = self._find_novel(service, str(payload.get("novel") or ""))
            return json_response(
                {
                    "novel": novel["slug"],
                    "title": novel["title"],
                    "volumes": list_volumes(str(novel["directory"])),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_corpus_volume(self):
        """看一卷的目录结构（不改任何东西，纯预览）。"""
        try:
            self._username()
            service = self._worldgen()
            payload = await self._payload()
            volume = self._find_volume(
                service,
                str(payload.get("novel") or ""),
                str(payload.get("volume_ref") or ""),
            )
            source = load_volume(str(volume["directory"]))
            return json_response(
                {
                    "volume_ref": volume["slug"],
                    # 卷标题字段叫 arc_title（VolumeSource 上没有 title）。
                    "title": source.arc_title or str(volume.get("title") or ""),
                    "documents": len(source.docs),
                    "chars": source.total_chars,
                    "outline": source.outline()[:200],
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_index(self):
        """为某一卷建/刷检索索引。**幂等**，已建好会直接命中缓存。"""
        try:
            self._username()
            service = self._worldgen()
            payload = await self._payload()
            volume = self._find_volume(
                service,
                str(payload.get("novel") or ""),
                str(payload.get("volume_ref") or ""),
            )
            # 全卷分词是同步阻塞的，必须挪出事件循环——否则建索引期间
            # 整个控制台（连同正在跑的副本）都会僵住。
            retriever, built = await asyncio.to_thread(
                service.build_index, str(volume["slug"]), str(volume["directory"])
            )
            return json_response(
                {
                    "volume_ref": volume["slug"],
                    "rebuilt": built,
                    "passages": len(getattr(retriever, "passages", []) or []),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    # --- 语料定位 -----------------------------------------------------------
    # 一律**按名单查**，不拼接用户给的字符串去拼路径。
    # 名字对不上就是错，不存在 "…/…" 这类输入能拼出目录之外的地方。

    def _find_novel(self, service: Any, slug: str) -> dict[str, Any]:
        cleaned = str(slug or "").strip()
        if not cleaned:
            raise ValueError("请先选择小说")
        for novel in list_novels(service.corpus_root):
            if str(novel["slug"]) == cleaned:
                return dict(novel)
        raise CorpusError(f"找不到这部小说：{cleaned}")

    def _find_volume(self, service: Any, novel_slug: str, volume_ref: str) -> dict[str, Any]:
        cleaned = str(volume_ref or "").strip()
        if not cleaned:
            raise ValueError("请先选择卷")
        novel = self._find_novel(service, novel_slug)
        for volume in list_volumes(str(novel["directory"])):
            if str(volume["slug"]) == cleaned:
                return dict(volume)
        raise CorpusError(f"《{novel['slug']}》里找不到这一卷：{cleaned}")

    # --- 前作继承 -----------------------------------------------------------

    async def worldgen_sessions(self):
        """列出可继承前作的存档，供挑一个。

        **没完结的也列**。原先只给 ``finished`` / ``closed``，理由是"后果还没
        发生完"——但跑到一半的档往往已经攒下了大量已结算的影响（谁死了、谁反目、
        拿了什么），而玩家正是想从这里接着往下编。是否够用交给他判断，
        面板会把状态标出来（``ended`` 字段）。

        排序仍然是最近的在前，但**已完结的排在未完结的前面**——同一个时间里，
        已收场的档是更稳的继承来源。

        ``list_sessions`` 返回的键是 ``id`` / ``instance_name`` / ``state``
        （不是 ``session_id`` / ``title``）——照抄 panel 那套字段名会全列空。
        """
        try:
            self._username()
            self._worldgen()
            sessions = await self.database.list_sessions()
            options = []
            for item in sessions:
                session_id = str(item.get("id") or "").strip()
                if not session_id:
                    continue
                state = str(item.get("state") or "").strip()
                options.append(
                    {
                        "session_id": session_id,
                        "title": str(
                            item.get("instance_name") or item.get("name") or session_id
                        ),
                        "world_slug": str(
                            item.get("world_name") or item.get("world_slug") or ""
                        ),
                        "state": state,
                        # 未完结的档继承进去，后果只到"目前为止"——面板据此提示
                        "ended": state in ENDED_SESSION_STATES,
                        "updated_at": str(item.get("updated_at") or ""),
                    }
                )
            options.sort(
                key=lambda entry: (entry["ended"], entry["updated_at"]),
                reverse=True,
            )
            return json_response({"sessions": options})
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_continuity(self):
        """预览"某次游玩留下了什么影响"。**只读**，不写任何东西。"""
        try:
            self._username()
            self._worldgen()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("请提供 session_id")

            brief = await derive_brief_async_safe(self.database, session_id)
            return json_response({"brief": brief.to_dict(), "empty": brief.is_empty})
        except Exception as exc:
            return self._handle_error(exc)

    # --- 作业 ---------------------------------------------------------------

    async def worldgen_jobs(self):
        """列出全部生成作业。"""
        try:
            self._username()
            service = self._worldgen()
            limit = 50
            records = service.store.list_jobs(limit=limit)
            return json_response(
                {
                    "jobs": [self._job_summary(record) for record in records],
                    "stats": {
                        "total": len(records),
                        "awaiting_approval": sum(
                            1 for r in records if r.state is JobState.AWAITING_APPROVAL
                        ),
                        "running": sum(1 for r in records if r.state is JobState.RUNNING),
                        "bytes": service.store.total_bytes(),
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    @staticmethod
    def _job_summary(record: Any) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "state": record.state.value,
            "phase": record.phase.value,
            "progress": record.progress,
            "message": record.message,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "actor": record.actor,
            "kind": str(record.request.get("kind") or ""),
            "slug": str(record.request.get("slug") or ""),
            "name": str(record.request.get("name") or ""),
            "error": record.error,
            "outputs": dict(record.outputs),
        }

    async def worldgen_job(self):
        """一个作业的完整状态 + 检查点提案。

        **不返回 steps** —— 那里面逐次调用都带 token 用量，几十条一叠会把
        面板首屏撑爆；作业详情里只给计数，明细走 ``worldgen_job_steps``。
        """
        try:
            self._username()
            service = self._worldgen()
            payload = await self._payload()
            job_id = str(payload.get("job_id") or "").strip()
            record = service.store.load(job_id)

            result = self._job_summary(record)
            result["steps"] = len(record.steps)
            result["artifacts"] = service.store.list_artifacts(job_id)
            result["lint"] = dict(record.lint)
            result["preflight"] = dict(record.preflight)
            result["resume_from_phase"] = record.resume_from_phase
            result["request"] = dict(record.request)

            if service.store.has_artifact(job_id, "10_checkpoints.draft.json"):
                result["proposal"] = service.store.read_artifact_json(
                    job_id, "10_checkpoints.draft.json"
                )
            if service.store.has_artifact(job_id, "11_checkpoints.approved.json"):
                result["decisions"] = service.store.read_artifact_json(
                    job_id, "11_checkpoints.approved.json"
                )
            if service.store.has_artifact(job_id, "60_reflection.json"):
                result["reflection"] = service.store.read_artifact_json(
                    job_id, "60_reflection.json"
                )
            # 遗漏复核门：把"哪几条关键剧情没落点"直接顶到前面，面板不用再翻产物。
            omissions: list[dict[str, Any]] = []
            if service.store.has_artifact(job_id, "65_omissions.json"):
                payload = service.store.read_artifact_json(job_id, "65_omissions.json")
                if isinstance(payload, Mapping):
                    omissions = list(payload.get("omissions") or [])
            elif isinstance(result.get("reflection"), Mapping):
                omissions = list(result["reflection"].get("coverage_omitted") or [])
            result["omissions"] = omissions
            return json_response({"job": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_artifact(self):
        """读一个白名单内的产物文件。"""
        try:
            self._username()
            service = self._worldgen()
            payload = await self._payload()
            job_id = str(payload.get("job_id") or "").strip()
            name = str(payload.get("name") or "").strip()
            if name not in _ARTIFACT_WHITELIST:
                raise ValueError(f"不允许读取的产物：{name}")
            if not service.store.exists(job_id):
                raise ValueError(f"作业不存在：{job_id}")
            if not service.store.has_artifact(job_id, name):
                raise ValueError(f"该作业尚未产出：{name}")
            return json_response(
                {"name": name, "content": service.store.read_artifact_json(job_id, name)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    # --- 动作 ---------------------------------------------------------------

    async def worldgen_create(self):
        """创建生成作业。**毫秒级返回**，真正的生成在后台跑。"""
        try:
            actor = self._actor()
            service = self._worldgen()
            payload = await self._payload()

            kind = str(payload.get("kind") or "adaptation").strip()
            if kind not in {"adaptation", "origin"}:
                raise ValueError("kind 只能是 adaptation 或 origin")

            request: dict[str, Any] = {"kind": kind}
            # 生成要求：会一路带进取材/编剧提示词，是玩家对这次生成的**意图**。
            # 空着也能跑，只是模型自由发挥。
            requirements = str(
                payload.get("requirements") or payload.get("description") or ""
            ).strip()
            for key in ('target_chapters', 'target_milestones'):
                if key in payload:
                    request[key] = payload[key]
            if requirements:
                request["requirements"] = requirements
                # 原创路径的提示词读的是 preferences，这里对齐一下。
                request["preferences"] = requirements

            provider_id = str(payload.get("provider_id") or "").strip()
            if provider_id:
                request["provider_id"] = provider_id

            if kind == "origin":
                request["concept"] = str(payload.get("concept") or "").strip()
                if not request["concept"]:
                    raise ValueError("原创模式需要提供 concept（你想要个什么故事）")
                derived_name = request["concept"][:20] or "原创世界"
            else:
                novel = self._find_novel(service, str(payload.get("novel") or ""))
                volume = self._find_volume(
                    service, str(novel["slug"]), str(payload.get("volume_ref") or "")
                )
                request["novel"] = str(novel["slug"])
                request["volume_ref"] = str(volume["slug"])
                try:
                    source = await asyncio.to_thread(
                        load_volume, str(volume["directory"])
                    )
                    derived_name = str(
                        source.arc_title or volume.get("title") or volume["slug"]
                    )
                except Exception:
                    derived_name = str(volume["title"] or volume["slug"])
                # 指定了前作才做继承；没指定就按原剧情走。
                session_id = str(payload.get("session_id") or "").strip()
                if session_id:
                    request["session_id"] = session_id

            # 名字与 slug 由面板自动推，这里只是兜底：真为空也要能落一个合法标识，
            # 否则装配阶段会因为 slug 非法整个作业失败。
            name = str(payload.get("name") or "").strip() or derived_name
            slug = str(payload.get("slug") or "").strip() or _derive_world_slug(
                kind=kind,
                novel=str(request.get("novel") or ""),
                volume_ref=str(request.get("volume_ref") or ""),
                name=name,
            )
            request["name"] = name
            request["slug"] = slug
            request["description"] = str(payload.get("description") or "").strip() or name

            config = self._worldgen_config()
            record = await service.create(request, actor=actor)
            return json_response(
                {
                    "job": self._job_summary(record),
                    "output_dir": str(service.output_dir),
                    "max_chapters": config.worldgen_max_chapters,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_approve(self):
        """对检查点提案下判断：批准 / 驳回 / 改写，可选提交。"""
        try:
            actor = self._actor()
            service = self._worldgen()
            payload = await self._payload()
            job_id = str(payload.get("job_id") or "").strip()

            raw = payload.get("decisions")
            decisions: dict[str, dict[str, Any]] = {}
            if isinstance(raw, Mapping):
                for item_id, value in raw.items():
                    if isinstance(value, Mapping):
                        decisions[str(item_id)] = {
                            "status": str(value.get("status") or ""),
                            "note": str(value.get("note") or ""),
                            "edited": value.get("edited"),
                        }
                    else:
                        decisions[str(item_id)] = {"status": str(value or "")}

            record = await service.approve(
                job_id,
                decisions=decisions,
                submit=bool(payload.get("submit")),
                actor=actor,
                # 遗漏复核门上的两个选择：确认就这样交付 / 补章节重跑。
                accept_omissions=bool(payload.get("accept_omissions")),
                regenerate=bool(payload.get("regenerate")),
                global_feedback=str(payload.get("global_feedback") or ""),
                scope_changes={k: payload.get(k) for k in ('target_chapters', 'target_milestones')}
                    if payload.get('global_feedback') else None,
            )
            return json_response({"job": self._job_summary(record)})
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_resume(self):
        """继续一个被中断或停在审批门的作业。"""
        try:
            actor = self._actor()
            service = self._worldgen()
            payload = await self._payload()
            job_id = str(payload.get("job_id") or "").strip()
            record = await service.resume(job_id, actor=actor)
            return json_response({"job": self._job_summary(record)})
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_cancel(self):
        try:
            actor = self._actor()
            service = self._worldgen()
            payload = await self._payload()
            job_id = str(payload.get("job_id") or "").strip()
            record = await service.cancel(job_id, actor=actor)
            return json_response({"job": self._job_summary(record)})
        except Exception as exc:
            return self._handle_error(exc)

    async def worldgen_delete(self):
        """删掉一个**已结束**的作业及其产物。

        不允许删还在跑的作业——先取消再删，否则后台任务会往一个已经
        消失的目录里继续写产物。
        """
        try:
            self._username()
            service = self._worldgen()
            payload = await self._payload()
            job_id = str(payload.get("job_id") or "").strip()
            record = service.store.load(job_id)
            if not record.state.terminal:
                raise ValueError(
                    f"作业还在进行中（{record.state.value}），请先取消再删除"
                )
            service.store.delete(job_id)
            return json_response({"deleted": job_id})
        except Exception as exc:
            return self._handle_error(exc)

    # --- 校验 ---------------------------------------------------------------

    async def worldgen_lint(self):
        """独立跑一遍结构校验。**不改任何东西**，纯粹给人看体检报告。"""
        try:
            self._username()
            payload = await self._payload()
            world = payload.get("world")
            if not isinstance(world, Mapping):
                raise ValueError("请提供 world（世界包 JSON 对象）")

            npcs = payload.get("npcs")
            slugs: list[str] | None = None
            if isinstance(npcs, Mapping):
                items = npcs.get("items")
                if isinstance(items, list):
                    slugs = [
                        str(item.get("slug"))
                        for item in items
                        if isinstance(item, Mapping) and item.get("slug")
                    ]
            report = lint_world_package(dict(world), npc_slugs=slugs)
            return json_response({"report": report})
        except Exception as exc:
            return self._handle_error(exc)


__all__ = ["WorldgenConsoleRoutes", "WorldgenError"]
