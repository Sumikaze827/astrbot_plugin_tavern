"""世界包生成的 LLM 调用层。

为什么不能复用 ``TavernEngine._llm_generate_metered``
-----------------------------------------------------
该方法（``tavern/engine.py:2408``）在调用前会执行
``database.reserve_token_usage(session_id, ...)``，而 ``token_usage.session_id`` 带
``FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE``。生成作业不属于
任何对局，拿不到合法的 session_id，第一次调用就会撞外键。

因此这里**直接调用** ``context.llm_generate``；只复用 provider 的选择逻辑
（``config.provider_id`` + ``fallback_provider_ids`` → 健康度过滤 → 当前会话模型兜底）。

用量仍然记录，但落在作业的 ``steps[].usage`` 上（面板可见），不计入群额度。
若日后要让生成消耗计入额度，需要给 ``reserve_token_usage`` 加一个可空 session 的重载。

关于重试
--------
``tavern/prompts.py:1727`` 的 ``repair_prompt`` **刻意不重发原始 prompt**——它只带
修复指令和被拒的输出。所以**任何任务关键规则都必须写在 system prompt 里**：
只写在 user prompt 里的规则，第一次重试就静默消失了。这正是"章节线路悬空"这类
坏包的成因。本模块的重试严格沿用这一语义。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..resolution import extract_json_object

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 8000
DEFAULT_MAX_REPAIR = 2


@dataclass
class LLMResult:
    """一次（可能经过重试的）LLM 调用结果。"""

    text: str = ""
    provider_id: str = ""
    attempts: int = 1
    input_chars: int = 0
    output_chars: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


class WorldgenLLMError(RuntimeError):
    """生成阶段的 LLM 调用失败。"""


def _usage_of(response: Any) -> dict[str, Any]:
    """把 provider 返回的 usage 归一化。字段名在不同 provider 间不一致。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    result: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens",
                "completion_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            result[key] = value
    return result


def _completion_text(response: Any) -> str:
    return str(getattr(response, "completion_text", "") or "")


class WorldgenLLM:
    """provider 选择 + 直连 ``llm_generate`` + JSON 提取与有界重试。"""

    def __init__(
        self,
        context: Any,
        database: Any,
        config_provider: Callable[[], Any],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.context = context
        self.database = database
        self._config_provider = config_provider
        self.logger = logger or LOGGER
        self._cached_providers: list[str] = []

    # --- provider 选择 ---------------------------------------------------

    async def select_providers(
        self, *, refresh: bool = False, preferred: str = ""
    ) -> list[str]:
        """按 面板选的模型 → 插件主模型 → 备用模型 → 当前会话模型 给出候选。

        ``preferred`` 是**这次生成**在面板上选的模型。它只是插到最前面，
        不做硬绑定：万一那个模型此刻不可用，后面还有整条链子兜底，
        不至于因为选了一个掉线的模型就整个作业失败。
        """
        chosen = str(preferred or "").strip()
        if not chosen and self._cached_providers and not refresh:
            return list(self._cached_providers)

        primary = ""
        fallbacks: tuple[str, ...] = ()
        try:
            config = self._config_provider()
            primary = str(getattr(config, "provider_id", "") or "").strip()
            fallbacks = tuple(getattr(config, "fallback_provider_ids", ()) or ())
        except Exception as exc:  # 配置读取失败不应阻断生成
            self.logger.warning("读取生成模型配置失败：%s", exc)

        ordered: list[str] = []
        for candidate in (chosen, primary, *fallbacks):
            name = str(candidate or "").strip()
            if name and name not in ordered:
                ordered.append(name)

        healthy: list[str] = []
        if ordered:
            try:
                healthy = list(await self.database.filter_healthy_providers(ordered))
            except Exception as exc:
                self.logger.warning("provider 健康度过滤失败，改用完整顺序：%s", exc)
                healthy = ordered

        if not healthy:
            # 兜底：当前会话模型。世界包生成不绑定任何群，所以不传 umo。
            try:
                provider = await self.context.get_using_provider_async()
                provider_id = str(getattr(provider, "provider_config", {}).get("id", "") or "")
                if not provider_id:
                    provider_id = str(getattr(provider, "provider_id", "") or "")
                if provider_id:
                    healthy = [provider_id]
            except Exception as exc:
                self.logger.warning("取当前会话模型失败：%s", exc)

        if not healthy:
            raise WorldgenLLMError(
                "没有可用的叙事模型：请在插件配置里设置 model.provider_id，"
                "或确保 AstrBot 已配置对话模型"
            )

        self._cached_providers = healthy
        return list(healthy)

    # --- 调用 -------------------------------------------------------------

    async def generate(
        self,
        *,
        system_prompt: str,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        request_type: str = "worldgen",
        preferred_provider: str = "",
    ) -> LLMResult:
        """调用一次模型，按 provider 顺序逐个尝试。"""
        providers = await self.select_providers(preferred=preferred_provider)
        input_chars = len(system_prompt or "") + len(prompt or "")
        last_error = ""

        for provider_id in providers:
            started = time.time()
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt or None,
                    max_tokens=max_tokens,
                )
                text = _completion_text(response)
                if not text.strip():
                    last_error = f"{provider_id} 返回空内容"
                    self.logger.warning("生成请求 %s：%s", request_type, last_error)
                    continue
                return LLMResult(
                    text=text,
                    provider_id=provider_id,
                    attempts=1,
                    input_chars=input_chars,
                    output_chars=len(text),
                    usage={
                        **_usage_of(response),
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "request_type": request_type,
                    },
                )
            except Exception as exc:
                last_error = f"{provider_id} 调用失败：{exc}"
                self.logger.warning("生成请求 %s：%s", request_type, last_error)
                continue

        return LLMResult(
            provider_id=providers[0] if providers else "",
            input_chars=input_chars,
            error=last_error or "所有 provider 均不可用",
        )

    async def generate_json(
        self,
        *,
        system_prompt: str,
        prompt: str,
        validate: Callable[[dict[str, Any]], list[str]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_repair: int = DEFAULT_MAX_REPAIR,
        request_type: str = "worldgen",
        preferred_provider: str = "",
    ) -> tuple[dict[str, Any], LLMResult, list[str]]:
        """要求模型返回 JSON 对象，并做确定性校验与有界重试。

        重试时**只替换 user prompt**，system prompt 原样保留——契约必须活过重试。

        Returns:
            ``(解析出的对象, 调用记录, 最后一轮的校验问题)``。

        Raises:
            WorldgenLLMError: 调用失败，或重试耗尽仍拿不到合法 JSON。
        """
        problems: list[str] = []
        current_prompt = prompt
        result = LLMResult()

        for attempt in range(1, max_repair + 2):
            result = await self.generate(
                system_prompt=system_prompt,
                prompt=current_prompt,
                max_tokens=max_tokens,
                request_type=request_type,
                preferred_provider=preferred_provider,
            )
            if not result.ok:
                raise WorldgenLLMError(result.error or "模型调用失败")

            try:
                payload = extract_json_object(result.text)
            except ValueError as exc:
                problems = [f"未返回合法 JSON 对象：{exc}"]
                payload = {}

            if payload:
                problems = list(validate(payload)) if validate else []
                if not problems:
                    result.attempts = attempt
                    return payload, result, []
                # 校验不过且有重试额度：带上问题重发
                if attempt <= max_repair:
                    current_prompt = _repair_prompt(prompt, result.text, problems)
                    continue
                result.attempts = attempt
                return payload, result, problems

            if attempt <= max_repair:
                current_prompt = _repair_prompt(prompt, result.text, problems)
                continue

        result.attempts = max_repair + 1
        raise WorldgenLLMError(
            "模型多次未返回合法 JSON："
            + ("；".join(problems) if problems else "无更多信息")
        )


def _repair_prompt(original_prompt: str, rejected: str, problems: Sequence[str]) -> str:
    """构造修复请求。

    语义与 ``tavern/prompts.py:1727`` 的 ``repair_prompt`` 一致：**不重发原始 prompt**，
    只给问题清单与被拒输出。这正是"契约必须写在 system prompt 里"的原因——
    留在这里的只有数据，重发与否不影响规则。
    """
    issue_lines = "\n".join(f"- {item}" for item in problems) or "- 输出不符合要求的 JSON 结构"
    return (
        "你上一次的输出不符合约定，请重新输出**一个完整的 JSON 对象**。\n\n"
        "上一次的问题：\n"
        f"{issue_lines}\n\n"
        "上一次的输出（供你定位问题，不要照抄）：\n"
        f"```\n{rejected[:4000]}\n```\n\n"
        "要求：只输出 JSON 对象本身，不要解释、不要 Markdown 代码块标记。"
        "严格遵守 system prompt 中给出的字段契约与硬性规则。"
    )


def dumps(value: Any, *, limit: int = 20000) -> str:
    """把数据渲染进 prompt 的安全 JSON 序列化（超长截断，避免撑爆上下文）。"""
    text = json.dumps(value, ensure_ascii=False, indent=1)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原始长度 {len(text)} 字符）"
