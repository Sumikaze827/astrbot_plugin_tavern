"""统一错误分类与失败上报助手（v0.12.0）。

背景（对应规划 F2）：历史上「空 message_id 误判发送成功」「定时器轮询异常被静默
吞掉」等问题，根源在于关键路径缺少统一的失败语义：哪些是预期内可恢复的瞬时错误、
哪些是应上抛的数据一致性错误、哪些是预期内的策略拒绝。

本模块提供：
- ``TavernError``：插件错误基类，派生语义分类。
- ``TransientError``：瞬时错误（网络抖动、模型超时、平台限流）——按 warning 记录，
  允许重试与降级。
- ``DataIntegrityError``：数据一致性错误（并发冲突、脏状态）——应上抛并由调用方
  决定回滚，日志按 exception 记录。
- ``PolicyRejection``：预期内的策略拒绝（权限、白名单、配额、状态机非法跳转）——
  按 info 记录，不视为故障。
- ``report_failure()``：统一失败上报入口：按语义分类记录日志、返回归一化消息，
  便于富消息发送 / 模型调用 / DB 写入三类关键路径保持一致的可观测性。

用法示例：:

    from .errors import report_failure, TransientError

    try:
        await fn(...)
    except Exception as exc:
        report_failure(
            logger, stage="rich_send", operation="ark",
            exc=exc, transient=True, context={"origin": origin},
        )
        # 继续降级逻辑……
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

__all__ = [
    "TavernError",
    "TransientError",
    "DataIntegrityError",
    "PolicyRejection",
    "EconomyDisabledError",
    "InsufficientFundsError",
    "EconomyConflictError",
    "provider_failure_is_permanent",
    "report_failure",
]


class TavernError(Exception):
    """AI 酒馆插件错误基类。"""


class TransientError(TavernError):
    """瞬时、可恢复错误（网络抖动、模型超时、平台限流）。

    调用方应重试或降级，而不是把错误扩散给用户。
    """


class DataIntegrityError(TavernError):
    """数据一致性错误（并发冲突、脏状态、半提交）。

    调用方应中止当前操作并回滚，必要时交由上层恢复流程处理。
    """


class PolicyRejection(TavernError):
    """预期内的策略拒绝（权限、白名单、配额、非法状态跳转）。

    这是正常业务分支，不属于故障，无需告警。
    """


class EconomyDisabledError(PolicyRejection):
    """经济系统未启用或世界包未接入。"""


class InsufficientFundsError(PolicyRejection):
    """余额不足（货币不允许负数时）。"""


class EconomyConflictError(DataIntegrityError):
    """经济操作冲突（重复 operation_id 与既有结果不一致等）。"""


# 服务商配置类失败的标记（2026-09-20）。这些失败说明「这个 provider_id /
# 模型 ID / 密钥」在当前服务商上根本不可用，重试同一个 provider_id 永远不会
# 成功，与网络抖动、限流、超时这类瞬时故障必须区分对待。
#
# 触发场景：火山方舟的 Coding Plan 端点只服务加入编码计划的模型，配置了
# ``huoshan/deepseek-v4-1-flash-260910`` 会稳定返回
# ``404 UnsupportedModel: The requested model does not support the coding
# plan feature``。旧实现把它记成普通失败，每 5→10→20→40 分钟熔断到期就再打
# 一次，线上连续 20 次失败、0 次成功。
_PROVIDER_CONFIG_ERROR_MARKERS = (
    "notfounderror",
    "unsupportedmodel",
    "does not support",
    "model not found",
    "model does not exist",
    "invalid model",
    "unknown model",
    "authenticationerror",
    "permissiondeniederror",
    "invalid_api_key",
    "invalid api key",
    "incorrect api key",
    "no permission",
)


def provider_failure_is_permanent(reason: str) -> bool:
    """失败原因是否属于「换模型/换密钥才能解决」的配置类错误。

    只接受原因字符串（熔断器拿到的就是归一化后的文本），因此按标记匹配。
    判定为 True 时调用方应把服务商标记为 ``invalid``（长熔断），而不是按
    瞬时故障做指数退避重试。
    """
    text = str(reason or "").casefold()
    if not text:
        return False
    return any(marker in text for marker in _PROVIDER_CONFIG_ERROR_MARKERS)


def _classify(exc: BaseException) -> tuple[str, int]:
    """把异常映射到（类别标签, 日志级别）。

    - TransientError → ("transient", logging.WARNING)
    - DataIntegrityError → ("integrity", logging.ERROR)
    - PolicyRejection → ("policy", logging.INFO)
    - 其他 → ("unknown", logging.ERROR)
    """
    if isinstance(exc, TransientError):
        return "transient", logging.WARNING
    if isinstance(exc, DataIntegrityError):
        return "integrity", logging.ERROR
    if isinstance(exc, PolicyRejection):
        return "policy", logging.INFO
    return "unknown", logging.ERROR


def report_failure(
    logger: Any,
    *,
    stage: str,
    operation: str,
    exc: BaseException,
    context: Optional[Mapping[str, Any]] = None,
    transient: Optional[bool] = None,
) -> str:
    """统一失败上报：按语义分类记录日志并返回归一化消息。

    参数：
        logger: 模块级 logger（任何带 info/warning/exception 的对象）。
        stage: 失败发生的阶段，如 ``rich_send`` / ``model_call`` / ``db_write``。
        operation: 具体操作，如 ``ark`` / ``llm_generate`` / ``cast_vote``。
        exc: 捕获到的异常。
        context: 附加上下文键值（不含密钥；调用方自行保证脱敏）。
        transient: 显式覆盖瞬时性分类（如 ``True`` 表示可重试）。
    """
    category, level = _classify(exc)
    if transient is not None:
        category = "transient" if transient else "integrity"
        level = logging.WARNING if transient else logging.ERROR
    details = "".join(
        f" {key}={value}" for key, value in (context or {}).items()
    )
    message = f"AI 酒馆 {stage}.{operation} 失败（{category}）"
    if details:
        message += f" [{details.strip()}]"
    if level == logging.INFO:
        logger.info("%s: %s", message, exc)
    elif level == logging.WARNING:
        logger.warning("%s: %s", message, exc)
    else:
        logger.exception("%s: %s", message, exc)
    return message
