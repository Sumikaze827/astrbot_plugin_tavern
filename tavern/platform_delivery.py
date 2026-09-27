"""Platform-neutral text delivery helpers for v0.12.0.

The story engine only emits text.  Adapter differences are represented as
capabilities and delivery results instead of platform-specific branches in
the game rules.  Unknown adapters receive conservative defaults: event
replies remain available, while proactive delivery is attempted only when a
caller explicitly asks for it and its outcome is reported honestly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    platform: str
    text_reply: bool = True
    group_conversation: bool = True
    private_conversation: bool = True
    # Unified origins may start with a user-defined platform instance id.
    # Unknown ids optimistically attempt AstrBot's standard text API; callers
    # still persist any unconfirmed or failed send instead of claiming success.
    proactive_send: bool = True
    mentions: bool = False
    threads: bool = False
    max_text_length: int = 3500
    identity_scope: str = "adapter_instance"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    status: str
    reason: str = ""
    attempted_parts: int = 0
    sent_parts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The table describes transport behavior only.  Business features never
# branch on these names; they ask for a capability instead.
_CAPABILITIES: dict[str, PlatformCapabilities] = {
    "aiocqhttp": PlatformCapabilities("aiocqhttp", proactive_send=True, mentions=True),
    "qq_official": PlatformCapabilities("qq_official", mentions=True, max_text_length=1800),
    "qq_official_webhook": PlatformCapabilities("qq_official_webhook", mentions=True, max_text_length=1800),
    "telegram": PlatformCapabilities("telegram", proactive_send=True, mentions=True, threads=True, max_text_length=3900),
    "lark": PlatformCapabilities("lark", proactive_send=True, mentions=True, threads=True),
    "slack": PlatformCapabilities("slack", proactive_send=True, mentions=True, threads=True),
    "discord": PlatformCapabilities("discord", proactive_send=True, mentions=True, threads=True, max_text_length=1900),
    "misskey": PlatformCapabilities("misskey", proactive_send=True, max_text_length=2800),
    "satori": PlatformCapabilities("satori", proactive_send=True, mentions=True),
    "dingtalk": PlatformCapabilities("dingtalk", mentions=True),
    "kook": PlatformCapabilities("kook", mentions=True, threads=True),
    "wecom": PlatformCapabilities("wecom", mentions=True),
    "wecom_ai_bot": PlatformCapabilities("wecom_ai_bot", mentions=True),
    "weixin_official_account": PlatformCapabilities("weixin_official_account", group_conversation=False, max_text_length=1900),
    "weixin_oc": PlatformCapabilities("weixin_oc", group_conversation=False, max_text_length=1900),
    "line": PlatformCapabilities("line", max_text_length=4500),
    "matrix": PlatformCapabilities("matrix", mentions=True, threads=True),
    "mattermost": PlatformCapabilities("mattermost", mentions=True, threads=True),
    "vocechat": PlatformCapabilities("vocechat", mentions=True),
    "webchat": PlatformCapabilities("webchat", proactive_send=True, mentions=False),
}


def normalize_platform(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        # Older AstrBot OneBot unified origins commonly used the short
        # ``qq`` prefix.  Keep that identity mapping while removing the
        # retired qq_restapi transport itself.
        "qq": "aiocqhttp",
        "qqofficial": "qq_official",
        "qqofficial_webhook": "qq_official_webhook",
        "onebot": "aiocqhttp",
        "onebot_v11": "aiocqhttp",
    }
    return aliases.get(text, text or "unknown")


def platform_from_origin(origin: Any) -> str:
    return normalize_platform(str(origin or "").split(":", 1)[0])


def private_notice_origin(
    session: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    *,
    allow_constructed: bool = False,
) -> str:
    """私信通知的发送目标。

    优先使用玩家私聊过机器人时记录的 ``private_origin``（唯一可靠来源）。
    ``allow_constructed`` 打开时才按 AstrBot 的统一来源格式
    ``platform:FriendMessage:<user>`` 兜底构造——轮次催办需要它，否则从未
    私聊过机器人的玩家永远收不到提醒；建卡进度等既有私信保持原样，
    只认已经建立的私聊通道，绝不在拿不到私聊目标时退回群聊。
    """
    for target in targets or ():
        if not isinstance(target, Mapping):
            continue
        origin = str(target.get("private_origin") or "").strip()
        if origin:
            return origin
    if not allow_constructed:
        return ""
    platform = str(session.get("platform_id") or "").strip()
    if not platform:
        return ""
    for target in targets or ():
        if not isinstance(target, Mapping):
            continue
        user_id = str(
            target.get("private_user_id") or target.get("user_id") or ""
        ).strip()
        if user_id:
            return f"{platform}:FriendMessage:{user_id}"
    return ""


def capabilities_for(platform_or_origin: Any) -> PlatformCapabilities:
    raw = str(platform_or_origin or "")
    platform = platform_from_origin(raw) if ":" in raw else normalize_platform(raw)
    return _CAPABILITIES.get(platform, PlatformCapabilities(platform))


def capability_matrix() -> list[dict[str, Any]]:
    return [item.to_dict() for item in sorted(_CAPABILITIES.values(), key=lambda row: row.platform)]


def split_text(text: Any, maximum: int) -> list[str]:
    """Split text without breaking paragraphs or losing content."""

    value = str(text or "").strip()
    limit = max(256, int(maximum or 3500))
    if not value:
        return []
    if len(value) <= limit:
        return [value]
    parts: list[str] = []
    remaining = value
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        window = remaining[: limit + 1]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"), window.rfind("；"))
        if cut < limit // 3:
            cut = limit
        else:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [part for part in parts if part]


# 回合秩序行内嵌的艾特标记：<<AT:QQ号:显示名>>
# 它是文本管线里的临时载体：真正发送时被解析为平台 At 段（QQ 上触发真实
# @提醒），平台不支持艾特时回退为 "@显示名" 纯文本。只用于数字 QQ 号。
AT_MENTION_RE = re.compile(r"<<AT:(\d+):([^<>]+)>>")


def at_display_name(name: Any, user_id: Any) -> str:
    """为回合秩序行的行动者构造艾特标记展示名。

    仅当 user_id 是数字 QQ 号且 name 非空时，返回艾特标记包裹的名字
    （<<AT:QQ:名字>>），由投递层升级为真实 At 段触发 @提醒；其余情况
    原样返回，保证非 QQ / 测试 / 候补占位场景的显示完全不受影响。
    """
    text = str(name or "").strip()
    uid = str(user_id or "").strip()
    if text and uid.isdigit() and "<" not in text and ">" not in text:
        return f"<<AT:{uid}:{text}>>"
    return text or str(user_id or "") or ""


def split_mention_parts(content: str) -> list[str | tuple[str, str]]:
    """把文本中的艾特标记拆成 [文本片段..., (qq, 名字), 文本片段...] 序列。"""
    value = str(content or "")
    parts: list[str | tuple[str, str]] = []
    position = 0
    for match in AT_MENTION_RE.finditer(value):
        if match.start() > position:
            parts.append(value[position : match.start()])
        parts.append((match.group(1), match.group(2)))
        position = match.end()
    if position < len(value):
        parts.append(value[position:])
    return parts


def render_mentions(content: str, *, mention_capable: bool) -> str:
    """平台不支持艾特时把标记替换为 @名字；支持时原样保留，交给段生成。"""
    value = str(content or "")
    if mention_capable:
        return value
    return AT_MENTION_RE.sub(lambda match: f"@{match.group(2)}", value)


async def send_text(
    context: Any,
    origin: Any,
    text: Any,
    *,
    proactive: bool,
) -> DeliveryResult:
    target = str(origin or "").strip()
    content = str(text or "").strip()
    if not target:
        return DeliveryResult(False, "invalid_target", "没有可用的会话来源")
    if not content:
        return DeliveryResult(False, "empty", "消息内容为空")
    sender = getattr(context, "send_message", None)
    if not callable(sender):
        return DeliveryResult(False, "unavailable", "AstrBot 文本发送接口不可用")
    capabilities = capabilities_for(target)
    if proactive and not capabilities.proactive_send:
        return DeliveryResult(
            False,
            "queued_required",
            "当前平台未声明可靠的主动推送能力，将等待下一次会话消息补发",
        )
    parts = split_text(content, capabilities.max_text_length)
    sent_parts = 0
    try:
        from astrbot.api.event import MessageChain

        mention_capable = bool(capabilities.mentions)
        for part in parts:
            if not mention_capable or not AT_MENTION_RE.search(part):
                chain = MessageChain().message(
                    render_mentions(part, mention_capable=mention_capable)
                )
            else:
                chain = MessageChain()
                for item in split_mention_parts(part):
                    if isinstance(item, tuple):
                        chain.at(item[1], item[0])
                    else:
                        chain.message(item)
            result = await sender(target, chain)
            # None is deliberately not treated as success.  Several adapters
            # use it when a proactive request is skipped.
            if result is False or result is None:
                return DeliveryResult(
                    False,
                    "rejected",
                    "平台未确认消息已发送",
                    len(parts),
                    sent_parts,
                )
            sent_parts += 1
    except Exception as exc:
        return DeliveryResult(
            False,
            "exception",
            f"发送异常：{type(exc).__name__}: {str(exc)[:160]}",
            len(parts),
            sent_parts,
        )
    return DeliveryResult(True, "sent", "", len(parts), sent_parts)


__all__ = [
    "AT_MENTION_RE",
    "DeliveryResult",
    "PlatformCapabilities",
    "at_display_name",
    "capabilities_for",
    "capability_matrix",
    "normalize_platform",
    "platform_from_origin",
    "render_mentions",
    "send_text",
    "split_mention_parts",
    "split_text",
]
