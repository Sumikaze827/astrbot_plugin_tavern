from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .card_wizard import field_visible, preset_options
from .security import clean_text
from .stat_generation import (
    calculate_preset_stack_stats,
    modifier_from_table,
    stat_generation_config,
    uses_authored_stats,
    uses_preset_stack_stats,
    validate_stat_generation_config,
)
from .world_contract import attribute_lookup, stats_mode, world_contract
from .presets import dimension_fields, normalize_preset_dimensions


CARD_UNCREATED = "uncreated"
CARD_DRAFT = "draft"
CARD_PENDING = "pending_review"
CARD_APPROVED = "approved"
CARD_REJECTED = "rejected"
CARD_STATUSES = {
    CARD_UNCREATED,
    CARD_DRAFT,
    CARD_PENDING,
    CARD_APPROVED,
    CARD_REJECTED,
}

PARTICIPANT_RESERVED = "reserved"
PARTICIPANT_ACTIVE = "active"
PARTICIPANT_STANDBY = "standby"
PARTICIPANT_AWAY = "away"
PARTICIPANT_RETIRED = "retired"
PARTICIPANT_ARCHIVED = "archived"
PARTICIPANT_STATUSES = {
    PARTICIPANT_RESERVED,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_STANDBY,
    PARTICIPANT_AWAY,
    PARTICIPANT_RETIRED,
    PARTICIPANT_ARCHIVED,
}

SEAT_HOLDING_STATUSES = {
    PARTICIPANT_RESERVED,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_STANDBY,
    PARTICIPANT_AWAY,
}

CHOICE_KEYS = ("A", "B", "C", "D")

DEFAULT_TIME_RULES: dict[str, Any] = {
    "card_code_ttl_seconds": 30 * 60,
    "card_draft_ttl_seconds": 7 * 24 * 60 * 60,
    "card_completion_timeout_seconds": None,
    "preparation_timeout_seconds": None,
    "ready_timeout_seconds": None,
    "turn_timeout_seconds": None,
    "turn_reminder_seconds": None,
    # 2026-09-20：玩家迟迟不选时私信催办（第一次、第二次）。
    # 关闭开关即完全停用；两个阈值按「回合计时器创建时刻」起算。
    "turn_dm_reminder_enabled": True,
    "turn_dm_reminder_seconds": 5 * 60,
    "turn_dm_reminder_repeat_seconds": 30 * 60,
    "max_consecutive_timeouts": -1,
    "standby_timeout_seconds": None,
    "delegation_ttl_seconds": None,
    "vote_round_one_seconds": None,
    "vote_round_two_seconds": None,
    "vote_reminder_seconds": None,
    "all_idle_pause_seconds": None,
    "pause_stops_clock": True,
    "announce_timeouts": False,
    "turn_timeout_action": "hold",
    "card_timeout_action": "remind",
    "ready_timeout_action": "remind",
}

DEFAULT_CARD_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "name",
        "label": "角色姓名",
        "required": True,
        "private": False,
        "max_chars": 12,
    },
    {
        "key": "code",
        "label": "副本代号",
        "required": True,
        "private": False,
        "max_chars": 12,
    },
    {
        "key": "appearance",
        "label": "外貌特征",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "background",
        "label": "角色背景",
        "required": True,
        "private": False,
        "max_chars": 800,
    },
    {
        "key": "personality",
        "label": "性格与行事方式",
        "required": True,
        "private": False,
        "max_chars": 400,
    },
    {
        "key": "goal",
        "label": "当前目标",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "belief",
        "label": "核心信念",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "bond",
        "label": "重要羁绊",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "specialties",
        "label": "专长标签（2—4 个，以逗号分隔）",
        "required": True,
        "private": False,
        "max_chars": 240,
    },
    {
        "key": "flaws",
        "label": "缺陷标签（1—2 个，以逗号分隔）",
        "required": True,
        "private": False,
        "max_chars": 200,
    },
    {
        "key": "weakness",
        "label": "弱点或限制",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "knowledge_boundary",
        "label": "知识边界",
        "required": True,
        "private": False,
        "max_chars": 400,
    },
    {
        "key": "secret",
        "label": "私人秘密",
        "required": False,
        "private": True,
        "max_chars": 600,
    },
    {
        "key": "content_boundaries",
        "label": "个人内容边界",
        "required": False,
        "private": True,
        "max_chars": 600,
    },
)

DEFAULT_CARD_STATS: dict[str, Any] = {
    "budget": 10,
    "attributes": [
        {
            "key": "body",
            "label": "体魄",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "agility",
            "label": "敏捷",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "will",
            "label": "意志",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "knowledge",
            "label": "学识",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
    ],
    "modifier_table": {
        "0": -3,
        "1": -2,
        "2": -1,
        "3": 0,
        "4": 1,
        "5": 2,
    },
}

DEFAULT_OPENING_CHOICES: tuple[dict[str, Any], ...] = (
    {
        "key": "A",
        "text": "先观察周围环境，确认眼前最明显的异常",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "B",
        "text": "与当前场景中最容易接触的人交谈，询问公开信息",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "C",
        "text": "检查自己能够合理接触的物品与随身资源",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "D",
        "text": "保持警戒，暂不冒进，等待局势显露更多线索",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
)

DEFAULT_PROGRESS: dict[str, Any] = {
    "chapter": "序章",
    "current_objective": "等待剧情目标",
    "completed_milestones": 0,
    "total_milestones": 0,
}

DEFAULT_CONTENT_BOUNDARIES: dict[str, Any] = {
    "character_death": "yes",
    "player_conflict": "consent",
    "romance": "fade_to_black",
    "horror": "moderate",
    "sexual_content": "blocked",
}

DEFAULT_NPC_POLICY: dict[str, Any] = {
    "enabled": True,
    "auto_register": True,
    "max_new_per_turn": 3,
    "require_named_or_relevant": True,
    "generated_requires_review": True,
    "archive_after_inactive_rounds": 12,
}

DEFAULT_CONTEXT_BUDGET: dict[str, Any] = {
    "recent_turns": 6,
    "memories": 6,
    "active_npcs": 6,
    "ledger_items": 8,
    "locked_facts_always_include": True,
}

DEFAULT_DICE_RULES: dict[str, Any] = {
    "advantage": "2d20_keep_high",
    "disadvantage": "2d20_keep_low",
    "stacking": False,
    "opposites_cancel": True,
    "outcome_bands": True,
    "visibility": "public",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deadline_after(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))
    ).isoformat(timespec="seconds")


def _bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _optional_seconds(value: Any, default: int | None) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "unlimited",
        "none",
    }:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed == -1:
        return None
    if parsed == 0 or parsed < -1:
        raise ValueError("时间值必须大于 0，或使用 -1/留空表示不限时")
    return min(365 * 24 * 60 * 60, parsed)


def _dm_reminder_seconds(value: Any, default: int) -> int | None:
    """私信催办阈值（秒）。

    与其它 ``*_seconds`` 不同：**键缺失时用默认值**（新功能不需要每个世界包
    或每个旧副本显式声明就能生效），而显式留空 / null / -1 表示关闭这一档。
    """
    if value is None or value is False:
        return None
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "unlimited",
        "none",
    }:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed == -1:
        return None
    if parsed <= 0:
        raise ValueError("私信催办时间必须大于 0，或使用 -1/留空表示关闭")
    return min(365 * 24 * 60 * 60, parsed)


# 可以设为「不限时」的时长字段。普通时长用 _optional_seconds 归一化；
# 私信催办两档语义不同（键缺失时用默认值，显式 -1/空表示关闭这一档），
# 分开列出但一起参与落盘序列化。
_DURATION_SECONDS_KEYS = (
    "card_code_ttl_seconds",
    "card_draft_ttl_seconds",
    "card_completion_timeout_seconds",
    "preparation_timeout_seconds",
    "ready_timeout_seconds",
    "turn_timeout_seconds",
    "turn_reminder_seconds",
    "standby_timeout_seconds",
    "delegation_ttl_seconds",
    "vote_round_one_seconds",
    "vote_round_two_seconds",
    "vote_reminder_seconds",
    "all_idle_pause_seconds",
)
_DM_REMINDER_SECONDS_KEYS = (
    "turn_dm_reminder_seconds",
    "turn_dm_reminder_repeat_seconds",
)
OPTIONAL_SECONDS_KEYS = _DURATION_SECONDS_KEYS + _DM_REMINDER_SECONDS_KEYS


def time_rules_for_storage(rules: Any = None) -> dict[str, Any]:
    """把归一化后的时间规则转成可安全落盘的形式（不限时写 -1，不写 null）。

    2026-09-20 线上：勾选「不限时」后普通回读没问题，但**重启 AstrBot 后勾选
    消失**。原因是落盘写了 ``null``，而 AstrBot 的
    ``AstrBotConfig.check_config_integrity`` 把 ``null`` 当作「未设置」，启动时
    用 ``_conf_schema.json`` 的 default 回填并回写文件：
    ``turn_dm_reminder_seconds`` 的 default 是 300、``turn_dm_reminder_repeat_seconds``
    是 1800，于是这两项被冲成默认值。

    其它时长字段只是碰巧没坏——它们的 schema default 本来就是 ``-1``，而 ``-1``
    不是 ``None``，能活过那次检查，读回来同样表示不限时。

    文档里 ``-1`` 与留空同为不限时，``_optional_seconds`` / ``_dm_reminder_seconds``
    对两者的处理完全一致，所以统一写 ``-1`` 不改变任何运行时行为。

    Args:
        rules: ``normalize_time_rules`` 的输出（不限时表示为 None）。

    Returns:
        可写入插件配置的副本；可选时长字段的 None 已替换为 -1。
    """
    stored = dict(rules) if isinstance(rules, Mapping) else {}
    for key in OPTIONAL_SECONDS_KEYS:
        if key in stored and stored[key] is None:
            stored[key] = -1
    return stored


def normalize_time_rules(value: Any = None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result = dict(DEFAULT_TIME_RULES)
    for key in _DURATION_SECONDS_KEYS:
        result[key] = _optional_seconds(source.get(key), result[key])
    raw_timeout_limit = source.get(
        "max_consecutive_timeouts",
        DEFAULT_TIME_RULES["max_consecutive_timeouts"],
    )
    try:
        timeout_limit = int(raw_timeout_limit)
    except (TypeError, ValueError):
        timeout_limit = int(DEFAULT_TIME_RULES["max_consecutive_timeouts"])
    if timeout_limit == 0 or timeout_limit < -1:
        raise ValueError("连续超时次数必须为 1—20，或 -1 表示永不自动转候补")
    result["max_consecutive_timeouts"] = (
        -1 if timeout_limit == -1 else min(20, max(1, timeout_limit))
    )
    result["pause_stops_clock"] = bool(
        source.get("pause_stops_clock", True)
    )
    result["announce_timeouts"] = bool(
        source.get("announce_timeouts", False)
    )
    result["turn_dm_reminder_enabled"] = bool(
        source.get(
            "turn_dm_reminder_enabled",
            DEFAULT_TIME_RULES["turn_dm_reminder_enabled"],
        )
    )
    for key in _DM_REMINDER_SECONDS_KEYS:
        result[key] = (
            _dm_reminder_seconds(source[key], DEFAULT_TIME_RULES[key])
            if key in source
            else DEFAULT_TIME_RULES[key]
        )
    timeout_action = str(
        source.get("turn_timeout_action", "hold")
    ).strip()
    result["turn_timeout_action"] = (
        timeout_action
        if timeout_action in {"skip", "hold"}
        else "hold"
    )
    for key, allowed, default in (
        (
            "card_timeout_action",
            {"standby", "release", "remind"},
            "remind",
        ),
        (
            "ready_timeout_action",
            {"standby", "remind"},
            "remind",
        ),
    ):
        action = str(source.get(key, default)).strip()
        result[key] = action if action in allowed else default

    turn_seconds = result["turn_timeout_seconds"]
    reminder = result["turn_reminder_seconds"]
    if (
        turn_seconds is not None
        and reminder is not None
        and reminder >= turn_seconds
    ):
        result["turn_reminder_seconds"] = max(1, turn_seconds // 3)
    return result


def world_time_rules(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    if not isinstance(rules, Mapping):
        return normalize_time_rules({})
    return normalize_time_rules(rules.get("time_rules"))


def turn_dm_reminder_thresholds(
    rules: Mapping[str, Any] | None,
) -> list[int]:
    """私信催办的阈值（秒），升序去重。

    2026-09-20 新增：玩家 5 分钟没选先私信一次，30 分钟仍未选再私信一次。
    阈值相同或为负时按去重/忽略处理，保证最多两次、且越晚的一次越靠后。
    """
    normalized = normalize_time_rules(rules or {})
    values: list[int] = []
    for key in (
        "turn_dm_reminder_seconds",
        "turn_dm_reminder_repeat_seconds",
    ):
        try:
            seconds = int(normalized.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if seconds > 0 and seconds not in values:
            values.append(seconds)
    return sorted(values)


def due_dm_reminder_stage(
    elapsed_seconds: float,
    thresholds: Sequence[int],
    already_sent: Sequence[int] = (),
) -> int | None:
    """返回本轮该发的催办档位（1 起），没有则 None。

    同一档位只发一次；若一次轮询跨过了多档（例如副本被暂停后恢复），
    只发最新的那一档，避免连发两条私信。
    """
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError):
        return None
    sent: set[int] = set()
    for item in already_sent or ():
        try:
            sent.add(int(item))
        except (TypeError, ValueError):
            continue
    crossed = [
        index
        for index, seconds in enumerate(thresholds, start=1)
        if index not in sent and elapsed >= float(seconds)
    ]
    return max(crossed) if crossed else None


def normalize_progress(value: Any = None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    completed = _bounded_int(
        raw.get("completed_milestones"),
        0,
        0,
        1_000_000,
    )
    total = _bounded_int(
        raw.get("total_milestones"),
        0,
        0,
        1_000_000,
    )
    if total and completed > total:
        completed = total
    # 章节追踪与结局标记必须原样保留，否则切章/结局检查点每次都会丢失
    # current_chapter_id（read 与 save 双侧都被 normalize 剥掉），导致
    # 章节永远卡在迁移分支、结局公告永远不触发。
    extra: dict[str, Any] = {}
    for key in (
        "current_chapter_id",
        "chapter_entered_at_turn",
        "milestone_evidence_since_turn",
        "narrative_length_band",
        "story_complete",
        "story_complete_at_turn",
        # 通用收尾状态机：里程碑齐全 -> ending_pending；只有经过专用输出
        # 校验并与回合原子提交后才写 ending_narrated / ending_event_id。
        "ending_pending",
        "ending_narrated",
        "ending_narrated_at_turn",
        "ending_event_id",
        # 里程碑达成即时公告的幂等清单 + 模型裁判判定冷却回合（2026-08-23
        # 里程碑改模型判定 + 独立公告，这两个字段必须原样保留否则会丢失）。
        "announced_milestones",
        "last_milestone_judge_at_turn",
        # 最近一次章节/终章收束裁判的事件 ID、逐字证据与理由，供审计。
        "last_chapter_closure",
    ):
        if raw.get(key) is not None:
            extra[key] = raw[key]
    return {
        "chapter": clean_text(
            raw.get("chapter") or DEFAULT_PROGRESS["chapter"],
            max_chars=160,
        ),
        "current_objective": clean_text(
            raw.get("current_objective")
            or DEFAULT_PROGRESS["current_objective"],
            max_chars=400,
        ),
        "completed_milestones": completed,
        "total_milestones": total,
        **extra,
    }


def world_session_modules(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    state = world.get("initial_state")
    state = state if isinstance(state, Mapping) else {}
    progress = rules.get("progress")
    if not isinstance(progress, Mapping):
        progress = state.get("progress")
    content_boundaries = dict(DEFAULT_CONTENT_BOUNDARIES)
    if isinstance(rules.get("content_boundaries"), Mapping):
        content_boundaries.update(dict(rules["content_boundaries"]))
    if isinstance(rules.get("content_boundary"), Mapping):
        boundary = dict(rules["content_boundary"])
        content_boundaries.update(boundary)
        # Protocol v4 uses hard_denials; the runtime's older policy key is
        # retained as a compatibility projection for all narrative paths.
        if boundary.get("hard_denials"):
            content_boundaries["hard_limits"] = list(
                boundary.get("hard_denials") or []
            )
    npc_policy = dict(DEFAULT_NPC_POLICY)
    if isinstance(rules.get("npc_policy"), Mapping):
        npc_policy.update(dict(rules["npc_policy"]))
    npc_policy["max_new_per_turn"] = _bounded_int(
        npc_policy.get("max_new_per_turn"),
        3,
        0,
        3,
    )
    npc_policy["archive_after_inactive_rounds"] = _bounded_int(
        npc_policy.get("archive_after_inactive_rounds"),
        12,
        1,
        10_000,
    )
    context_budget = dict(DEFAULT_CONTEXT_BUDGET)
    if isinstance(rules.get("context_budget"), Mapping):
        context_budget.update(dict(rules["context_budget"]))
    for key, default, maximum in (
        ("recent_turns", 6, 50),
        ("memories", 6, 40),
        ("active_npcs", 6, 40),
        ("ledger_items", 8, 100),
    ):
        context_budget[key] = _bounded_int(
            context_budget.get(key),
            default,
            0,
            maximum,
        )
    dice_rules = dict(DEFAULT_DICE_RULES)
    if isinstance(rules.get("dice_rules"), Mapping):
        dice_rules.update(dict(rules["dice_rules"]))
    visibility = str(dice_rules.get("visibility") or "public").lower()
    dice_rules["visibility"] = (
        visibility
        if visibility in {"public", "immersive", "hidden"}
        else "public"
    )
    return {
        "progress": normalize_progress(progress),
        "content_boundaries": content_boundaries,
        "npc_policy": npc_policy,
        "context_budget": context_budget,
        "dice_rules": dice_rules,
        "recovery": {
            "state": "idle",
            "message": "",
            "operation_id": "",
            "updated_at": utc_now(),
        },
    }


def initial_character_runtime_state() -> dict[str, Any]:
    return {
        "inspiration": 1,
        "inspiration_max": 3,
        "statuses": [],
        "equipment": {},
        "known_clues": [],
        "npc_relationships": {},
        "temporary_traits": [],
        "reputation": {},
        "current_location": "",
    }


def player_limits(world: Mapping[str, Any]) -> dict[str, int]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw = rules.get("player_limits")
    raw = raw if isinstance(raw, Mapping) else {}
    maximum = _bounded_int(raw.get("maximum"), 4, 1, 32)
    minimum = _bounded_int(raw.get("minimum_start"), 2, 1, maximum)
    recommended_min = _bounded_int(
        raw.get("recommended_min"),
        min(2, maximum),
        1,
        maximum,
    )
    recommended_max = _bounded_int(
        raw.get("recommended_max"),
        maximum,
        recommended_min,
        maximum,
    )
    return {
        "minimum_start": minimum,
        "maximum": maximum,
        "recommended_min": recommended_min,
        "recommended_max": recommended_max,
    }


def card_template(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw = rules.get("character_card")
    raw = raw if isinstance(raw, Mapping) else {}
    fields_raw = raw.get("fields")
    fields: list[dict[str, Any]] = []
    if isinstance(fields_raw, Sequence) and not isinstance(
        fields_raw, (str, bytes)
    ):
        seen: set[str] = set()
        for item in fields_raw[:30]:
            if not isinstance(item, Mapping):
                continue
            key = re.sub(
                r"[^a-zA-Z0-9_-]",
                "",
                str(item.get("key") or "").strip(),
            )[:40]
            if not key or key in seen:
                continue
            seen.add(key)
            fields.append(
                {
                    "key": key,
                    "label": clean_text(
                        item.get("label") or key,
                        max_chars=4000,
                    ),
                    "required": bool(item.get("required", True)),
                    "private": bool(item.get("private", False)),
                    "max_chars": _bounded_int(
                        item.get("max_chars"),
                        500,
                        10,
                        4000,
                    ),
                    "type": (
                        str(item.get("type") or "text").lower()
                        if str(item.get("type") or "text").lower()
                        in {"text", "textarea", "integer", "select",
                            "preset_select", "multi_select", "boolean",
                            "derived"}
                        else "text"
                    ),
                    **({"options": list(item.get("options") or [])}
                       if item.get("options") else {}),
                    **({"options_source": str(item.get("options_source"))}
                       if item.get("options_source") else {}),
                    **({"value_field": str(item.get("value_field"))}
                       if item.get("value_field") else {}),
                    **({"label_field": str(item.get("label_field"))}
                       if item.get("label_field") else {}),
                    **({"preset_source": str(item.get("preset_source"))}
                       if item.get("preset_source") else {}),
                    **({"description_field": str(item.get("description_field"))}
                       if item.get("description_field") else {}),
                    **({"page_size": _bounded_int(
                        item.get("page_size"), 5, 1, 10
                    )} if item.get("page_size") is not None else {}),
                    **({"visible_when": dict(item.get("visible_when") or {})}
                       if isinstance(item.get("visible_when"), Mapping) else {}),
                    **({"clear_on_change": list(item.get("clear_on_change") or [])}
                       if isinstance(item.get("clear_on_change"), Sequence)
                       and not isinstance(item.get("clear_on_change"), (str, bytes))
                       else {}),
                    **({"must_differ_from": str(item.get("must_differ_from"))}
                       if item.get("must_differ_from") else {}),
                    **({"min_choices": _bounded_int(
                        item.get("min_choices"), 0, 0, 100
                    )} if item.get("min_choices") is not None else {}),
                    **({"max_choices": _bounded_int(
                        item.get("max_choices"), 1, 1, 100
                    )} if item.get("max_choices") is not None else {}),
                    **({"display_order": _bounded_int(
                        item.get("display_order"), 1000, -100000, 100000
                    )} if item.get("display_order") is not None else {}),
                }
            )
    if not fields:
        fields = [dict(item) for item in DEFAULT_CARD_FIELDS]
    dimensions = normalize_preset_dimensions(raw)
    if dimensions:
        generated = dimension_fields(raw)
        generated_by_key = {str(item["key"]): item for item in generated}
        merged: list[dict[str, Any]] = []
        consumed: set[str] = set()
        for item in fields:
            key = str(item.get("key") or "")
            if key in generated_by_key:
                merged.append({**item, **generated_by_key[key]})
                consumed.add(key)
            else:
                merged.append(item)
        pending = [item for item in generated if str(item["key"]) not in consumed]
        if pending:
            insert_at = next(
                (index + 1 for index, item in enumerate(merged)
                 if str(item.get("key") or "") == "code"),
                0,
            )
            merged[insert_at:insert_at] = pending
        fields = merged
    for field in fields:
        if str(field.get("key") or "") in {"name", "code"}:
            field["max_chars"] = min(
                12,
                int(field.get("max_chars", 12) or 12),
            )
    char_limit = _bounded_int(raw.get("field_char_limit"), 0, 0, 4000)
    if char_limit:
        for field in fields:
            key = str(field.get("key") or "")
            if key in {"name", "code"}:
                continue
            if str(field.get("type") or "") == "integer":
                continue
            field["max_chars"] = min(
                int(field.get("max_chars", 4000) or 4000),
                char_limit,
            )
    stats_raw = raw.get("stats")
    stats_raw = stats_raw if isinstance(stats_raw, Mapping) else {}
    generation_raw = raw.get("stat_generation")
    if not isinstance(generation_raw, Mapping):
        nested_generation = stats_raw.get("stat_generation")
        generation_raw = (
            nested_generation
            if isinstance(nested_generation, Mapping)
            else {}
        )
    generation_mode = str(generation_raw.get("mode") or "").lower()
    mode = (
        generation_mode
        if generation_mode in {"preset_stack", "authored"}
        else stats_mode(stats_raw)
    )
    attributes_raw = stats_raw.get("attributes")
    attributes: list[dict[str, Any]] = []
    if isinstance(attributes_raw, Sequence) and not isinstance(
        attributes_raw,
        (str, bytes),
    ):
        for item in attributes_raw[:20]:
            if not isinstance(item, Mapping):
                continue
            key = re.sub(
                r"[^a-zA-Z0-9_-]",
                "",
                str(item.get("key") or "").strip(),
            )[:40]
            if not key or any(entry["key"] == key for entry in attributes):
                continue
            minimum = _bounded_int(item.get("minimum"), 0, -100, 100)
            maximum = _bounded_int(
                item.get("maximum"),
                5,
                minimum,
                100,
            )
            attributes.append(
                {
                    "key": key,
                    "label": clean_text(
                        item.get("label") or key,
                        max_chars=4000,
                    ),
                    "minimum": minimum,
                    "maximum": maximum,
                    "default": _bounded_int(
                        item.get("default"),
                        minimum,
                        minimum,
                        maximum,
                    ),
                }
            )
    if not attributes and mode != "none":
        attributes = [
            dict(item)
            for item in DEFAULT_CARD_STATS["attributes"]
        ]
    table_raw = stats_raw.get("modifier_table")
    table_raw = table_raw if isinstance(table_raw, Mapping) else {}
    modifier_table: dict[str, int] = {}
    for raw_value, raw_modifier in table_raw.items():
        try:
            value_key = str(int(raw_value))
            modifier = int(raw_modifier)
        except (TypeError, ValueError):
            continue
        modifier_table[value_key] = max(-10, min(10, modifier))
    if not modifier_table and mode != "none":
        modifier_table = dict(DEFAULT_CARD_STATS["modifier_table"])
    budget = _bounded_int(
        stats_raw.get("budget"),
        int(DEFAULT_CARD_STATS["budget"]),
        0,
        2000,
    )
    profession_mode = mode == "preset"
    # Profession-preset mode: keep the 10 attributes for checks/preview but do
    # NOT generate 10 manual stat-entry questions (doc §4.2).
    for attribute in (attributes if mode == "manual" else []):
        field_key = f"stat_{attribute['key']}"
        existing_field = next(
            (
                item
                for item in fields
                if str(item.get("key") or "") == field_key
            ),
            None,
        )
        if existing_field is not None:
            fields.remove(existing_field)
        fields.append(
            {
                "key": field_key,
                "label": clean_text(
                    (
                        existing_field.get("label")
                        if existing_field is not None
                        else ""
                    )
                    or (
                        f"{attribute['label']}数值"
                        f"（{attribute['minimum']}—{attribute['maximum']}，"
                        f"总预算 {budget}）"
                    ),
                    max_chars=4000,
                ),
                "required": True,
                "private": False,
                "max_chars": 12,
                "type": "integer",
                "minimum": attribute["minimum"],
                "maximum": attribute["maximum"],
                "default": attribute["default"],
                "stat_key": attribute["key"],
            }
        )
    preset_sets_raw = raw.get("preset_sets")
    preset_sets = (
        {
            str(key): list(value)
            for key, value in preset_sets_raw.items()
            if isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
        }
        if isinstance(preset_sets_raw, Mapping)
        else {}
    )
    profession_presets = list(raw.get("profession_presets") or [])
    origin_region_presets = list(raw.get("origin_region_presets") or [])
    social_identity_presets = list(raw.get("social_identity_presets") or [])
    if profession_presets:
        preset_sets.setdefault("profession_presets", profession_presets)
    if origin_region_presets:
        preset_sets.setdefault("origin_region_presets", origin_region_presets)
    if social_identity_presets:
        preset_sets.setdefault("social_identity_presets", social_identity_presets)
    normalized_generation = {
        "mode": str(generation_raw.get("mode") or mode).lower(),
        "base_stats": dict(generation_raw.get("base_stats") or {}),
        "bonus_sources": list(
            generation_raw.get("bonus_sources") or []
        ),
        "bonus_source_rules": dict(
            generation_raw.get("bonus_source_rules") or {}
        ),
        "expected_total": generation_raw.get(
            "expected_total", budget
        ),
        "min_per_stat": generation_raw.get("min_per_stat"),
        "max_per_stat": generation_raw.get("max_per_stat"),
        "allow_manual_edit": bool(
            generation_raw.get("allow_manual_edit", False)
        ),
        # authored 模式：属性由叙事模型按玩家自拟设定分配。
        # source_field 指向那段自由输入文本，guide 是作者给的分配倾向。
        # 这里是白名单，漏掉它们等于配置被静默丢弃。
        "source_field": str(
            generation_raw.get("source_field")
            or generation_raw.get("background_field")
            or ""
        ).strip(),
        "guide": str(generation_raw.get("guide") or "").strip(),
    }
    return {
        "version": _bounded_int(raw.get("version"), 1, 1, 100000),
        "auto_approve": bool(raw.get("auto_approve", False)),
        "edit_requires_review": bool(
            raw.get("edit_requires_review", True)
        ),
        "fields": fields,
        "stats": {
            "mode": mode,
            "budget": budget,
            "attributes": attributes,
            "modifier_table": modifier_table,
            "input_mode": str(stats_raw.get("input_mode") or ""),
            "allocation_mode": str(
                stats_raw.get("allocation_mode") or ""
            ),
            "primary_bonus": _bounded_int(
                stats_raw.get("primary_bonus"), 7, 0, 100
            ),
            "secondary_bonus": _bounded_int(
                stats_raw.get("secondary_bonus"), 3, 0, 100
            ),
            "allocation": dict(stats_raw.get("allocation") or {}),
            "total_validation": dict(stats_raw.get("total_validation") or {}),
            "preset_selector": dict(stats_raw.get("preset_selector") or {}),
            "bonus_choices": list(stats_raw.get("bonus_choices") or []),
            "stat_generation": dict(normalized_generation),
        },
        # Keep the canonical v4 declaration available to editor/API clients.
        # Runtime helpers also accept the nested compatibility copy above.
        "stat_generation": dict(normalized_generation),
        "profession_presets": profession_presets,
        "origin_region_presets": origin_region_presets,
        "social_identity_presets": social_identity_presets,
        "preset_sets": preset_sets,
        "preset_dimensions": dimensions,
        "knowledge_profiles": dict(raw.get("knowledge_profiles") or {}),
        "content_profiles": dict(raw.get("content_profiles") or {}),
        "profession_mode": profession_mode,
    }


def card_stat_allocation(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None = None,
    current_step: int | None = None,
) -> dict[str, Any]:
    """Return authoritative progress for none, manual, or preset stats."""

    stats_config = template.get("stats") or {}
    mode = stats_mode(stats_config)
    if mode == "none":
        return {"mode": "none", "stat_fields": [], "current": None, "values": {}, "used": 0, "budget": 0, "remaining": 0, "complete": True}
    if uses_preset_stack_stats(template):
        safe_fields = fields if isinstance(fields, Mapping) else {}
        resolved = calculate_preset_stack_stats(
            template,
            safe_fields,
            require_complete=False,
        )
        config = stat_generation_config(template)
        budget = int(config.get("expected_total") or 0)
        return {
            "mode": "preset_stack",
            "stat_fields": [],
            "current": None,
            "values": resolved["raw"] if resolved else {},
            "base_values": resolved["base"] if resolved else dict(config.get("base_stats") or {}),
            "used": resolved["effective_total"] if resolved else 0,
            "budget": budget,
            "remaining": 0 if resolved else budget,
            "complete": resolved is not None,
            "resolved": resolved,
        }
    if uses_profession_preset_stats(template):
        # Profession-preset mode: stats are derived, never manually allocated.
        safe_fields = fields if isinstance(fields, Mapping) else {}
        try:
            resolved = resolve_profession_stats(
                template, safe_fields, require_complete=False
            )
        except ValueError:
            resolved = None
        total_validation = stats_config.get("total_validation") or {}
        final_total = int(total_validation.get("final_total", stats_config.get("budget", 0)))
        return {
            "mode": "preset",
            "stat_fields": [],
            "current": None,
            "values": resolved["raw"] if resolved else {},
            "base_values": resolved["base"] if resolved else {},
            "used": resolved["effective_total"] if resolved else 0,
            "budget": final_total,
            "remaining": (
                max(0, final_total - resolved["effective_total"])
                if resolved else final_total
            ),
            "resolved": resolved,
        }

    field_values = fields if isinstance(fields, Mapping) else {}
    definitions = template.get("fields")
    definitions = (
        list(definitions)
        if isinstance(definitions, Sequence)
        and not isinstance(definitions, (str, bytes))
        else []
    )
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    attributes_raw = stats.get("attributes")
    attributes = (
        list(attributes_raw)
        if isinstance(attributes_raw, Sequence)
        and not isinstance(attributes_raw, (str, bytes))
        else []
    )
    attributes_by_key = {
        str(item.get("key") or ""): item
        for item in attributes
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }
    stat_fields: list[dict[str, Any]] = []
    for index, definition in enumerate(definitions):
        if not isinstance(definition, Mapping):
            continue
        stat_key = str(definition.get("stat_key") or "")
        attribute = attributes_by_key.get(stat_key)
        if not stat_key or not isinstance(attribute, Mapping):
            continue
        stat_fields.append(
            {
                "step": index,
                "field_key": str(definition.get("key") or f"stat_{stat_key}"),
                "stat_key": stat_key,
                "label": str(attribute.get("label") or stat_key),
                "description": str(attribute.get("description") or ""),
                "minimum": int(attribute.get("minimum", 0)),
                "maximum": int(attribute.get("maximum", 0)),
                "default": int(attribute.get("default", 0)),
            }
        )

    values: dict[str, int] = {}
    for item in stat_fields:
        field_key = item["field_key"]
        if field_key not in field_values:
            continue
        try:
            values[field_key] = int(field_values[field_key])
        except (TypeError, ValueError):
            continue

    budget = int(stats.get("budget", 0) or 0)
    used = sum(values.values())
    allocation = stats.get("allocation") or {}
    rule = str(allocation.get("rule") or "maximum")
    target = int(allocation.get("total", budget))
    total_ok = (True if rule == "none" else used <= target if rule == "maximum" else used == target if rule == "exact" else int(allocation.get("minimum_total", 0)) <= used <= int(allocation.get("maximum_total", budget)))
    result: dict[str, Any] = {
        "mode": "manual",
        "budget": budget,
        "used": used,
        "remaining": budget - used,
        "values": values,
        "stat_fields": stat_fields,
        "first_step": stat_fields[0]["step"] if stat_fields else len(definitions),
        "complete": bool(stat_fields)
        and all(item["field_key"] in values for item in stat_fields)
        and total_ok,
        "total_ok": total_ok,
        "allocation_rule": rule,
        "current": None,
    }

    if current_step is None:
        return result
    current = next(
        (item for item in stat_fields if item["step"] == int(current_step)),
        None,
    )
    if not current:
        return result

    current_value = values.get(current["field_key"])
    used_before = used - (current_value if current_value is not None else 0)
    reserved_minimum = sum(
        item["minimum"]
        for item in stat_fields
        if item["step"] > current["step"]
        and item["field_key"] not in values
    )
    effective_maximum = min(
        current["maximum"],
        budget - used_before - reserved_minimum,
    )
    current_position = next(
        index
        for index, item in enumerate(stat_fields, start=1)
        if item["step"] == current["step"]
    )
    result["current"] = {
        **current,
        "position": current_position,
        "total": len(stat_fields),
        "used_before": used_before,
        "remaining_before": budget - used_before,
        "reserved_minimum": reserved_minimum,
        "effective_maximum": effective_maximum,
    }
    return result


PROFESSION_PRESET_STAT_MODE = (
    "automatic_profession_base_plus_two_fixed_bonus_choices"
)


def uses_profession_preset_stats(
    template: Mapping[str, Any],
) -> bool:
    """Return True when the card template opts into the profession-preset
    stat mode (fixed 50 base + primary +7 / secondary +3 = 60)."""
    stats = template.get("stats")
    if not isinstance(stats, Mapping):
        return False
    return bool(
        stats_mode(stats) == "preset"
        or stats.get("input_mode") == PROFESSION_PRESET_STAT_MODE
        or stats.get("allocation_mode")
        == "profession_base_plus_primary7_secondary3"
    )


def find_profession_preset(template: Mapping[str, Any], profession_ref: str) -> Mapping[str, Any]:
    reference = str(profession_ref or "").strip().casefold()
    for preset in template.get("profession_presets") or []:
        if not isinstance(preset, Mapping):
            continue
        aliases = preset.get("aliases")
        aliases = aliases if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)) else []
        candidates = {str(preset.get("id") or "").strip().casefold(), str(preset.get("key") or "").strip().casefold(), str(preset.get("name") or "").strip().casefold(), *(str(item).strip().casefold() for item in aliases)}
        if reference and reference in candidates:
            return preset
    raise ValueError(f"不存在预设“{profession_ref}”，请从提示中的可选项选择")


def attribute_maps(
    template: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    attributes = template.get("stats", {}).get("attributes", [])
    label_to_key: dict[str, str] = {}
    key_to_label: dict[str, str] = {}
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            continue
        key = str(attribute.get("key") or "")
        label = str(attribute.get("label") or key)
        if not key:
            continue
        label_to_key[label] = key
        label_to_key[key] = key
        key_to_label[key] = label
    return label_to_key, key_to_label


def resolve_profession_stats(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Compute final preset stats from the frozen world contract.

    The preset base total, choice bonuses and final total are authoritative
    world-package data. Preview, confirmation and audit paths reuse this
    resolver so persisted values cannot drift from that declaration.
    """
    stats_config = template.get("stats") or {}
    selector = stats_config.get("preset_selector") or {}
    selector_field = str(selector.get("field") or "profession")
    profession_ref = str(fields.get(selector_field) or "")
    authored_base = fields.get("profession_base_stats")
    authored_base = dict(authored_base) if isinstance(authored_base, Mapping) else {}
    if uses_authored_stats(template) and authored_base:
        # authored 模式：基础分配由叙事模型按玩家自拟设定生成，落在
        # profession_base_stats 里。**主/副属性加点仍由玩家选择**，所以下面
        # 那条加点/合计/修正的路径完全复用——这里只是不查职业预设。
        preset: Mapping[str, Any] = {}
        profession_name = "自拟设定"
        base_source = authored_base
    else:
        if uses_authored_stats(template) and not authored_base:
            # 自拟设定还没生成基础值时走到这里：给出可执行的提示，而不是
            # 让作者去猜"请先选择预设"是什么意思。
            raise ValueError("自拟设定尚未生成基础属性，请先填写自拟设定")
        if not profession_ref:
            raise ValueError("请先选择预设")
        preset = find_profession_preset(template, profession_ref)
        profession_name = str(preset.get("name") or profession_ref)
        base_source = (
            preset.get("base_attributes")
            or preset.get("attributes")
            or {}
        )
    attribute_defs = template["stats"]["attributes"]
    attribute_keys = [str(item["key"]) for item in attribute_defs]
    base: dict[str, int] = {}
    for key in attribute_keys:
        if key not in base_source:
            raise ValueError(
                f"职业“{profession_name}”缺少属性：{key}"
            )
        base[key] = int(base_source[key])
    validation = stats_config.get("total_validation") or {}
    base_total = int(validation.get("base_total", stats_config.get("base_budget", 50)))
    final_total = int(validation.get("final_total", stats_config.get("budget", base_total)))
    bonus_choices = stats_config.get("bonus_choices") or []
    primary_cfg = next((x for x in bonus_choices if isinstance(x, Mapping) and x.get("field") == "primary_attribute"), {})
    secondary_cfg = next((x for x in bonus_choices if isinstance(x, Mapping) and x.get("field") == "secondary_attribute"), {})
    primary_bonus = int(primary_cfg.get("bonus", stats_config.get("primary_bonus", 7)))
    secondary_bonus = int(secondary_cfg.get("bonus", stats_config.get("secondary_bonus", 3)))
    if sum(base.values()) != base_total:
        raise ValueError(f"预设“{profession_name}”基础属性总和不是{base_total}")
    label_to_key, key_to_label = attribute_maps(template)
    primary_label = str(fields.get("primary_attribute") or "")
    secondary_label = str(fields.get("secondary_attribute") or "")
    if require_complete and not primary_label:
        raise ValueError("尚未选择主属性")
    if require_complete and not secondary_label:
        raise ValueError("尚未选择副属性")
    primary_key = (
        label_to_key.get(primary_label) if primary_label else None
    )
    secondary_key = (
        label_to_key.get(secondary_label) if secondary_label else None
    )
    if primary_label and primary_key is None:
        raise ValueError("主属性不在可选属性列表中")
    if secondary_label and secondary_key is None:
        raise ValueError("副属性不在可选属性列表中")
    if (
        primary_key is not None
        and secondary_key is not None
        and primary_key == secondary_key
    ):
        raise ValueError("主属性与副属性不能相同")
    effective = dict(base)
    if primary_key:
        effective[primary_key] += primary_bonus
    if secondary_key:
        effective[secondary_key] += secondary_bonus
    if require_complete and sum(effective.values()) != final_total:
        raise ValueError(f"最终属性总和必须为{final_total}")
    attribute_definitions = {
        str(item["key"]): item for item in attribute_defs
    }
    for key, value in effective.items():
        definition = attribute_definitions[key]
        minimum = int(definition["minimum"])
        maximum = int(definition["maximum"])
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{definition['label']}最终值{value}"
                f"超出允许范围{minimum}—{maximum}"
            )
    modifier_table = template["stats"].get("modifier_table", {})
    modifiers = {
        key: modifier_from_table(modifier_table, value)
        for key, value in effective.items()
    }
    return {
        "mode": "preset",
        "profession_id": str(preset.get("id") or preset.get("key") or profession_ref),
        "profession": profession_name,
        "base": base,
        "raw": effective,
        "labels": key_to_label,
        "modifiers": modifiers,
        "primary": {
            "attribute": primary_key or "",
            "label": primary_label,
            "bonus": primary_bonus if primary_key else 0,
        },
        "secondary": {
            "attribute": secondary_key or "",
            "label": secondary_label,
            "bonus": secondary_bonus if secondary_key else 0,
        },
        "base_total": base_total,
        "bonus_total": ((primary_bonus if primary_key else 0) + (secondary_bonus if secondary_key else 0)),
        "effective_total": sum(effective.values()),
        "modifier_table": dict(modifier_table),
    }


def next_fillable_card_step(
    template: Mapping[str, Any],
    fields_def: list[Mapping[str, Any]],
    start_step: int,
    values: Mapping[str, Any] | None = None,
) -> int:
    step = start_step
    while step < len(fields_def):
        definition = fields_def[step]
        if not isinstance(definition, Mapping):
            step += 1
            continue
        key = str(definition.get("key") or "")
        if not field_visible(definition, values):
            step += 1
            continue
        if (
            (uses_profession_preset_stats(template) or uses_preset_stack_stats(template))
            and (
                key.startswith("stat_")
                or definition.get("skip_manual_prompt")
            )
        ):
            step += 1
            continue
        break
    return step


def repair_profession_preset_draft(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    current_step: int,
) -> tuple[dict[str, Any], int]:
    """Recompute profession-preset stat fields for a legacy/partial draft.

    Reused when an old draft already carries hand-filled ``stat_*`` fields so
    the values are overwritten with the formula-derived ones and the cursor is
    moved to the first non-attribute field.
    """
    if not uses_profession_preset_stats(template):
        return fields, current_step
    profession = fields.get("profession")
    if not profession:
        return fields, current_step
    resolved = resolve_profession_stats(
        template, fields, require_complete=False
    )
    fields["profession_base_stats"] = resolved["base"]
    for key, value in resolved["raw"].items():
        fields[f"stat_{key}"] = value
    fields_def = template["fields"]
    repaired_step = next_fillable_card_step(
        template, fields_def, current_step, fields
    )
    return fields, repaired_step


def validate_card_template_config(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("玩家角色卡模板必须是 JSON 对象")
    try:
        version = int(value.get("version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("角色卡模板 version 必须是大于 0 的整数") from exc
    if version < 1:
        raise ValueError("角色卡模板 version 必须是大于 0 的整数")
    fields = value.get("fields")
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise ValueError("角色卡模板必须包含 fields 数组")
    keys: list[str] = []
    for item in fields:
        if not isinstance(item, Mapping):
            raise ValueError("角色卡 fields 的每一项都必须是对象")
        key = str(item.get("key") or "").strip()
        if not key:
            raise ValueError("角色卡字段 key 不能为空")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
            raise ValueError(f"角色卡字段 key 非法：{key}")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("角色卡字段 key 不能重复")
    for required in ("name", "code"):
        if required not in keys:
            raise ValueError(f"角色卡缺少必需字段 {required}")
    for item in fields:
        key = str(item.get("key") or "").strip()
        if key not in {"name", "code"}:
            continue
        try:
            max_chars = int(item.get("max_chars", 12))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "角色姓名与副本代号 max_chars 必须是整数"
            ) from exc
        if max_chars > 12:
            raise ValueError(
                "角色姓名与副本代号最多只能设置为 12 个字符"
            )

    normalized_template = card_template(
        {"rules": {"character_card": dict(value)}}
    )
    normalized_fields = normalized_template.get("fields") or []
    field_key_set = {
        str(item.get("key") or "")
        for item in normalized_fields
        if isinstance(item, Mapping)
    }
    dependency_graph: dict[str, set[str]] = {key: set() for key in field_key_set}
    for field in normalized_fields:
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        condition = field.get("visible_when")
        if isinstance(condition, Mapping):
            for dependency in condition:
                dependency_key = str(dependency)
                if dependency_key not in field_key_set:
                    raise ValueError(
                        f"字段 {key} 的 visible_when 引用了不存在的字段："
                        f"{dependency_key}"
                    )
                dependency_graph[key].add(dependency_key)
        different = str(field.get("must_differ_from") or "")
        if different and different not in field_key_set:
            raise ValueError(
                f"字段 {key} 的 must_differ_from 引用了不存在的字段："
                f"{different}"
            )
        for target in field.get("clear_on_change") or []:
            target_key = str(target)
            if target_key not in field_key_set:
                raise ValueError(
                    f"字段 {key} 的 clear_on_change 引用了不存在的字段："
                    f"{target_key}"
                )
        if str(field.get("type") or "") in {"select", "preset_select"}:
            source_field = dict(field)
            source_field.pop("visible_when", None)
            options = preset_options(normalized_template, source_field, {})
            if field.get("required") and not options:
                raise ValueError(f"必填预设字段 {key} 没有任何有效选项")
            ids = [str(item.get("id") or "").casefold() for item in options]
            if len(ids) != len(set(ids)):
                raise ValueError(f"预设字段 {key} 存在重复的稳定 ID")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"角色卡条件字段形成循环依赖：{node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependency_graph.get(node, set()):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for field_key in dependency_graph:
        visit(field_key)

    stats = value.get("stats")
    stats = stats if isinstance(stats, Mapping) else {"mode": "none"}
    mode = stats_mode(stats)
    if uses_preset_stack_stats(normalized_template):
        mode = "preset_stack"
    attributes = stats.get("attributes")
    if not isinstance(attributes, Sequence) or isinstance(
        attributes,
        (str, bytes),
    ) or (mode != "none" and not attributes):
        raise ValueError("启用数值时必须包含 stats.attributes")
    if mode == "none":
        if attributes:
            raise ValueError("stats.mode=none 时不得声明角色属性")
        return
    attribute_keys: set[str] = set()
    minimum_budget = 0
    maximum_budget = 0
    for item in attributes:
        if not isinstance(item, Mapping):
            raise ValueError("属性定义必须是 JSON 对象")
        key = str(item.get("key") or "").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
            raise ValueError(f"属性 key 非法：{key or '空'}")
        if key in attribute_keys:
            raise ValueError("属性 key 不能重复")
        attribute_keys.add(key)
        try:
            minimum = int(item.get("minimum"))
            maximum = int(item.get("maximum"))
            default = int(item.get("default"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"属性 {key} 的范围必须是整数") from exc
        if minimum > maximum or not minimum <= default <= maximum:
            raise ValueError(f"属性 {key} 的默认值不在合法范围内")
        minimum_budget += minimum
        maximum_budget += maximum
    try:
        budget = int(stats.get("budget"))
    except (TypeError, ValueError) as exc:
        raise ValueError("属性预算必须是整数") from exc
    if mode == "manual" and not minimum_budget <= budget <= maximum_budget:
        raise ValueError(
            f"属性预算必须介于 {minimum_budget} 与 {maximum_budget} 之间"
        )

    # Preset stat mode. Totals and bonuses come from the world package.
    if mode == "preset_stack":
        validate_stat_generation_config(normalized_template)

    if mode == "preset":
        field_keys = {
            str(item.get("key") or "")
            for item in fields
            if isinstance(item, Mapping)
        }
        for required_field in (
            "profession",
            "primary_attribute",
            "secondary_attribute",
        ):
            if required_field not in field_keys:
                raise ValueError(
                    f"职业预设模式必须包含“{required_field}”字段"
                )
        presets = value.get("profession_presets")
        presets = presets if isinstance(presets, list) else []
        if not presets:
            raise ValueError("职业预设模式至少需要一个职业预设")
        attr_index = {
            str(item.get("key") or ""): item
            for item in attributes
            if isinstance(item, Mapping)
        }
        for preset in presets:
            if not isinstance(preset, Mapping):
                raise ValueError("职业预设必须是 JSON 对象")
            name = str(preset.get("name") or "")
            if not name:
                raise ValueError("职业预设缺少名称")
            base_source = (
                preset.get("base_attributes")
                or preset.get("attributes")
                or {}
            )
            if not isinstance(base_source, Mapping):
                raise ValueError(f"职业“{name}”缺少基础属性")
            for key, definition in attr_index.items():
                if key not in base_source:
                    raise ValueError(
                        f"职业“{name}”缺少属性：{key}"
                    )
                try:
                    value_int = int(base_source[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"职业“{name}”属性 {key} 必须是整数"
                    ) from exc
                amin = int(definition["minimum"])
                amax = int(definition["maximum"])
                if not amin <= value_int <= amax:
                    raise ValueError(
                        f"职业“{name}”属性 {key} 超出允许范围"
                        f"{amin}—{amax}"
                    )
            if sum(int(v) for v in base_source.values()) != int((stats.get("total_validation") or {}).get("base_total", stats.get("base_budget", 50))):
                raise ValueError(
                    f"职业“{name}”基础属性总和不符合 total_validation.base_total"
                )
        configured_bonus_ceiling = max(
            (
                max(0, int(item.get("bonus", 0)))
                for item in (stats.get("bonus_choices") or [])
                if isinstance(item, Mapping)
            ),
            default=0,
        )
        if not configured_bonus_ceiling:
            configured_bonus_ceiling = max(
                0,
                int(stats.get("primary_bonus", 7)),
            )
        for key, definition in attr_index.items():
            amax = int(definition["maximum"])
            maximum_base = max(
                int(
                    (preset.get("base_attributes") or {}).get(key, 0)
                )
                for preset in presets
                if isinstance(preset, Mapping)
            )
            if maximum_base + configured_bonus_ceiling > amax:
                raise ValueError(
                    f"属性 {definition.get('label', key)} 最大值 {amax}"
                    "不足以容纳世界包声明的预设加成"
                )


def _shares_bigram(left: str, right: str) -> bool:
    """两个中文短语是否共享任意 2 字子串（如「远程攻击」↔「借箭远射」）。"""
    a = str(left or "")
    b = str(right or "")
    bigrams = {
        a[i : i + 2]
        for i in range(len(a) - 1)
        if not a[i].isspace() and not a[i + 1].isspace()
    }
    return any(t and t in b for t in bigrams)


_ACTION_WORD_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    # 常见动作词 → 属性 key 域。按词义匹配选项文字，作为属性 description
    # 词面匹配失败时的补充（「射箭→敏捷」「安抚→魅力」这类语义同域）。
    # 2026-08-23：补近战/身法/呼喝/圣印等动词（突入、直取、击溃、掀、
    # 接战、冲锋、腾挪、呼喝、护印、奥术……），修复「致命却免检」——
    # 模型漏配检定时，这些词能把高风险行动推回正确属性。
    # 2026-08-23（第二次）：再补高频措辞（盾线/清剿/重击/震断/抵住/压制/
    # 祷文/加持/呼名/法术/突进/检查……），覆盖「以圣银链环残片抵住门缝
    # 压制死卫」这类精确动词仍漏的选项。注意刻意不收录「圣光/圣银/重盾」
    # 这类「工具/材质词」——它们是手段不是动作（「以圣光呼喝」仍是言语
    # 行动→魅力、「引动圣银护印加护」仍是神圣→意志），会翻转既有判定；
    # 这些词留给 _force_check_attribute 兜底层。
    "strength": ("挥", "砍", "劈", "砸", "推", "撞", "破门", "拖", "拽", "冲撞", "强攻", "冲锋", "突击", "接战", "迎战", "突入", "直取", "击溃", "破阵", "交锋", "搏斗", "缠斗", "击退", "斩", "掀", "袭", "揍", "扑", "盾线", "清剿", "重击", "猛击", "震断", "撬", "抵挡", "抵住", "压制", "劈砍", "斩杀", "撞击", "歼灭", "扛住", "顶住"),
    "agility": ("射", "箭", "弓", "闪避", "躲", "潜", "跳", "翻", "滚", "跑", "逃", "追", "轻身", "腾挪", "侧身", "疾走", "飞扑", "攀爬", "钻", "绕", "突进", "切入", "俯身", "疾行", "接应"),
    "vitality": ("扛", "忍", "耐", "撑", "恢复", "顶住", "硬抗", "包扎", "救治"),
    "intellect": ("推理", "逻辑", "破译", "解读", "研究", "分析", "机关", "古代文字", "想通", "奥术", "咒文", "仪式", "法阵", "铭文", "法术", "冰霜", "秘法", "轰击", "战术", "阵法"),
    "willpower": ("抗", "坚守", "坚定", "祷告", "祝福", "信仰", "低语", "恐惧", "护印", "圣印", "净化", "驱散", "祷文", "祷词", "加持", "庇护", "守护", "镇压", "抗死潮"),
    "perception": ("察觉", "识破", "搜寻", "观察", "查看", "辨认", "监听", "望", "留意", "探查", "侦察", "探路", "勘探", "检查", "确认", "审视", "瞄准", "识别", "打量"),
    "charisma": ("说", "谈", "劝", "哄", "骗", "安抚", "交涉", "质问", "求", "喊", "问", "魅", "稳住", "道出", "坦言", "告知", "呼喝", "喝令", "喊话", "说服", "震慑", "魅惑", "呼名", "唤醒", "统率", "威慑", "谈判", "命令", "指挥"),
}


# 行动性质粗分类：精确词表（_ACTION_WORD_ATTRIBUTES）漏网时的最后一层
# 兜底。单字粗词误报风险较高，只作「这个动作大致属于哪一类」判断，用于
# 危险/绝境/致命选项强制补检定——宁可按行动性质给最贴切的属性摇点，
# 也不让高风险的行动静默变成免检（2026-08-23 用户选择「风险档强制检定」）。
# 刻意不用「击」「突」「冲」等单字（会误伤「目击」「突然」「冲动」），
# 只保留几乎无歧义的近战/身法动词。
_ACTION_NATURE_FALLBACK: dict[str, tuple[str, ...]] = {
    # 近战/搏斗粗词 → 力量
    "strength": ("攻", "杀", "挡", "守", "顶", "架", "格", "踢", "踹", "搏", "劈砍"),
    # 身法/潜行粗词 → 敏捷
    "agility": ("闪", "潜", "腾", "跃", "避", "攀", "钻", "掠"),
    # 神圣/护持粗词 → 意志
    "willpower": ("护印", "护持", "护佑", "加护", "祝圣"),
}


# 风险档强制检定的最终归类层词表（2026-08-23 第二次反馈「危险选项还没摇点」）。
# 危险/绝境/致命选项在精确推断（_extract_check_attribute）返回 None 时，由
# _force_check_attribute 用本表宽泛扫描。与精确词表的区别：这里允许收录
# 「工具/材质/目标」词（圣银/圣光/重盾/银链/亡灵/执念……），因为走到这层
# 说明文本里已经没有任何精确动作动词了，宁可按最贴切的行动性质摇点，也
# 不让高风险行动静默免检（用户选择「风险档强制检定」）。仍完全扫描不到
# 任何行动信号（纯被动/观察/静止）才返回 None。
_FORCED_ATTRIBUTE_CUES: dict[str, tuple[str, ...]] = {
    "strength": ("盾线", "重盾", "盾牌", "清剿", "重击", "猛击", "震断", "撬", "抵挡", "抵住", "压制", "劈砍", "斩杀", "撞击", "掀翻", "歼灭", "清理", "扛住", "顶住", "搏斗", "缠斗", "冲锋", "冲击", "强攻", "迎战", "接战", "击溃", "击倒", "击退", "砸开", "撞开", "推开", "拖拽", "挥砍", "格挡", "挡住"),
    "agility": ("突进", "切入", "俯身", "疾行", "疾冲", "翻滚", "闪避", "侧身", "潜行", "腾跃", "攀爬", "躲避", "追赶", "逃脱", "接应", "钻", "绕", "掠", "包抄", "迂回", "穿插", "侧翼", "绕后", "退"),
    "vitality": ("硬抗", "包扎", "救治", "扛伤", "忍耐", "恢复", "顶住", "止血", "疗伤"),
    "intellect": ("法术", "冰霜", "秘法", "奥术", "咒文", "法阵", "铭文", "仪式", "轰击", "推理", "分析", "破译", "解读", "战术", "阵法", "机关", "魔法", "元素"),
    "willpower": ("圣银", "圣光", "圣职", "祷文", "祷词", "加持", "庇护", "守护", "镇压", "共鸣", "银链", "银辉", "祝福", "净化", "驱散", "低语", "抗死潮", "坚守", "信仰", "祷告", "护印", "圣印", "神圣施法", "镇魂", "驱亡"),
    "perception": ("检查", "确认", "审视", "打量", "瞄准", "识别", "察觉", "观察", "搜寻", "辨认", "探查", "侦察", "识破", "留意", "查看", "倾听"),
    "charisma": ("呼名", "唤醒", "呼喝", "喊话", "喝令", "交涉", "质问", "说服", "安抚", "魅惑", "威慑", "谈判", "统率", "稳住", "道出", "坦言", "告知", "求", "劝", "喊", "呼"),
}


def _force_check_attribute(text: str) -> str | None:
    """风险档强制检定的归类层：精确推断失败后的宽泛行动信号扫描。

    危险/绝境/致命选项在 _extract_check_attribute 返回 None 时调用。
    用 _FORCED_ATTRIBUTE_CUES 按属性优先级做宽泛子串扫描，返回第一个
    命中的属性 key；仍扫不到就交给字符重叠 / 世界兜底（见下）。
    """
    source = str(text or "")
    if not source:
        return None
    for key, words in _FORCED_ATTRIBUTE_CUES.items():
        if any(word in source for word in words):
            return key
    return None


# 字符级语义重叠用的常见虚词/助词。这些字对「属性领域」没有区分度，
# 算重叠时剔除，避免「的/了/在/与」这类字撑高噪音。
_ATTR_STOPWORDS = frozenset(
    "的了着与和或且并或者是而在把用来以往向从被让对为这那其之此类等就都"
    "还要很将会最更于以内在上下前后左右么呢吧啊那这什如可可否能不没也"
)


def _attr_char_score(description: str, source: str) -> float:
    """选项文字与属性描述的字符级语义重叠率（世界驱动，自动适配任意新本）。

    去虚词后，统计「选项文字与描述共有的唯一字符数 ÷ 描述唯一字符数」。
    不依赖本世界的专有词（圣银/银链/死卫……）——只要作者把属性描述写清楚
    （力量=近战/破门/冲击…），任何新本的选项都能与对应属性靠字面重叠匹配。
    用作风险档强制检定的第三层兜底（分数低也接受，只是选「最贴切的」）。
    """
    ds = {
        c for c in str(description)
        if c not in _ATTR_STOPWORDS and not c.isspace()
    }
    if not ds:
        return 0.0
    ss = {
        c for c in str(source)
        if c not in _ATTR_STOPWORDS and not c.isspace()
    }
    if not ss:
        return 0.0
    return len(ds & ss) / len(ds)


def _best_char_attribute(
    contract: Mapping[str, Any],
    text: str,
) -> str | None:
    """风险档兜底：世界属性描述里字符重叠最高的属性。

    精确词表、宽泛行动词都扫不到时的第三层。返回分数最高的属性 key；
    全部 0 重叠（文本与所有属性描述毫无字面关联）返回 None，交给
    _fallback_attribute 兜底。分数低也照用——宁可摇点，不静默免检。
    """
    best: str | None = None
    best_score = 0.0
    for item in contract.get("attributes", []):
        key = str(item.get("key") or "")
        description = str(item.get("description") or "")
        if not key or not description:
            continue
        score = _attr_char_score(description, text)
        if score > best_score:
            best_score = score
            best = key
    return best


def _fallback_attribute(contract: Mapping[str, Any]) -> str | None:
    """风险档最后兜底：世界声明的第一个允许属性。

    精确/宽泛/字符重叠全失败（罕见：纯被动文本被模型标了危险）时，
    用世界允许属性表里第一个真实属性，保证骰子能摇起来、绝不给「通用」。
    世界一个属性都没声明（退化配置，attribute 模式也摇不了）时返回 None，
    由调用方回退免检——绝不用硬编码的 "strength" 撞 allowed_attributes。
    """
    allowed = contract.get("resolution", {}).get("allowed_attributes", [])
    for item in contract.get("attributes", []):
        key = str(item.get("key") or "")
        if key and (not allowed or key in allowed):
            return key
    return None


# 精确词表/宽泛行动词返回的标准属性 key → 世界声明的标准标签。
# 当世界用自定义 key（如 moxie/tech/sympathy）时，推断出的标准 key
# 必须翻译成世界实际 key，否则过不了 allowed_attributes 校验。
_STANDARD_ATTR_LABELS: dict[str, str] = {
    "strength": "力量",
    "agility": "敏捷",
    "vitality": "体质",
    "intellect": "智力",
    "willpower": "意志",
    "perception": "感知",
    "charisma": "魅力",
}


def _translate_standard_key(
    contract: Mapping[str, Any],
    key: str,
) -> str | None:
    """把标准属性 key 翻译成世界实际使用的属性 key。

    已是世界允许 key 则原样返回；否则按标准标签（力量/敏捷/…）在世界
    属性表里找同标签项；找不到（世界用完全自定义的标签/领域）按语义
    别名表（agility↔dexterity、intellect↔intelligence/wits、…）兜底，
    仍然找不到返回 None，交由下一层（字符重叠/世界兜底）决定。

    2026-08-24：补语义别名兜底——标准标签里「敏捷」常被世界作者改写成
    「身手/灵巧/迅捷」（dexterity key），仅按 label 等值匹配会漏掉，
    导致「agility」这条翻译成世界属性的常见路径失效，玩家会看到
    「【agility检定】」这种与世界预设不一致的属性公告。
    """
    allowed = contract.get("resolution", {}).get("allowed_attributes", [])
    if allowed and key in allowed:
        # 白名单存在且 key 已在白名单里 → 原样返回。空白名单不短路——
        # 「世界没声明 allowed_attributes」≠「任何 key 都允许」，
        # 让下方按 attributes/语义别名翻译；两者都找不到时返回 None，
        # 避免「agility」这种英文标准 key 在世界无 attributes 时漏到
        # 玩家眼前变成「【agility检定】」公告（2026-08-24 玩家反馈）。
        return key
    label = _STANDARD_ATTR_LABELS.get(key)
    if label:
        for item in contract.get("attributes", []):
            if str(item.get("label") or "") == label or key in str(
                item.get("key") or ""
            ):
                return str(item.get("key") or "")
    # 语义别名兜底：世界作者的 key 与标准 key 命名习惯不同（dexterity
    # 对应 agility、intelligence 对应 intellect、wits 对应 intellect 等），
    # 但描述的是同一维度；按别名表在世界属性表里找语义最贴近的 key。
    alias_map = _STANDARD_ATTR_ALIASES.get(key, ())
    if alias_map:
        for item in contract.get("attributes", []):
            item_key = str(item.get("key") or "").lower()
            if item_key in alias_map or item_key == key:
                return str(item.get("key") or "")
    return None


# 2026-08-24：标准 key 的语义别名——世界作者 key 命名习惯与标准标签不
# 一致时（dexterity/agility 同义、intelligence/intellect/wits 同义等），
# 仍按语义兜底翻译，避免「agility」这种英文标准 key 出现在玩家眼前。
_STANDARD_ATTR_ALIASES: dict[str, tuple[str, ...]] = {
    "strength": ("strength", "might", "power"),
    "agility": ("dexterity", "agility", "reflexes", "speed",
                "quickness"),
    "vitality": ("constitution", "vitality", "endurance", "stamina"),
    "intellect": ("intelligence", "intellect", "wits", "reason",
                  "logic"),
    "willpower": ("willpower", "faith", "spirit"),
    "perception": ("perception", "awareness", "senses", "spot"),
    "charisma": ("charisma", "charm"),
}


# 致命选项的已知后果兜底明示。lethal 投骰前必须披露失败后果（引擎硬校验），
# 但模型（doubao）常漏配——兜底填一条通用警示，让玩家选 lethal 前仍能看到
# 风险提示，而不是整轮被卡死（2026-08-23 反馈：C 致命选项无后果直接报错）。
_LETHAL_DEFAULT_CONSEQUENCE = "行动风险极高，失败可能造成致命伤，具体代价由剧情裁定"


def _extract_check_attribute(
    contract: Mapping[str, Any],
    text: str,
) -> str | None:
    """从选项文字里推断检定属性（引擎兜底用）。

    模型常把检定写进选项文字（如「以智识检定解读」）而不是 check 字段，
    或干脆不写。按三层信号推断：
    1. 文字里的「X检定」显式提及（key / label / 首字，覆盖「智识→智力」）；
    2. 选项文字与属性 description 词面匹配（子串或共享 2 字子串）；
    3. 常见动作词映射（射/箭→敏捷、安抚→魅力、推理→智力等）。
    推不出来返回 None——宁可免检，也不给「通用」这种无属性检定。
    4. 行动性质粗分类（_ACTION_NATURE_FALLBACK）——精确词表漏网时的
       最后一层兜底，按动作大类回退到最贴切属性（近战→力量、身法→敏捷、
       神圣→意志），让危险/致命选项不会静默免检。
    """
    source = str(text or "")
    if not source:
        return None
    match = re.search(r"([一-鿿]{1,4})检定", source)
    if match:
        word = re.sub(
            r"^(以|用|通过|进行|的|之|靠|凭)",
            "",
            match.group(1),
        )
        if word:
            for item in contract.get("attributes", []):
                key = str(item.get("key") or "")
                label = str(item.get("label") or "")
                if not key:
                    continue
                if key in word or (
                    label and (label in word or word[0] == label[0])
                ):
                    return key
    for item in contract.get("attributes", []):
        key = str(item.get("key") or "")
        label = str(item.get("label") or "")
        description = str(item.get("description") or "")
        if not key:
            continue
        for candidate in (label, description):
            if candidate and (
                candidate in source or source in candidate
            ):
                return key
        if description and _shares_bigram(description, source):
            return key
    for key, words in _ACTION_WORD_ATTRIBUTES.items():
        if any(word in source for word in words):
            return key
    for key, words in _ACTION_NATURE_FALLBACK.items():
        if any(word in source for word in words):
            return key
    return None


def normalize_choices(value: Any, world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("行动选项必须是数组")
    contract = world_contract(world) if world is not None else None
    aliases = {"low":"safe","standard":"controlled","high":"dangerous","safe":"safe","controlled":"controlled","dangerous":"dangerous","desperate":"desperate","lethal":"lethal"}
    by_key = {}
    for item in value:
        if not isinstance(item, Mapping): continue
        key = str(item.get("key") or "").strip().upper()
        if key not in CHOICE_KEYS or key in by_key: continue
        raw_text = str(item.get("text") or "").strip()
        if len(raw_text) > 50: raise ValueError("每个行动选项正文不得超过 50 字")
        text = clean_text(raw_text, max_chars=50)
        if not text: continue
        risk = aliases.get(str(item.get("danger_id") or item.get("risk") or "controlled").lower(), str(item.get("danger_id") or item.get("risk") or "controlled").lower())
        check = item.get("check"); check = check if isinstance(check, Mapping) else {}
        required = bool(check.get("required", item.get("requires_check", False)))
        stat = clean_text(check.get("attribute_id", item.get("check_stat")), max_chars=40)
        label = clean_text(
            check.get(
                "attribute_label",
                item.get("check_label", stat),
            ),
            max_chars=40,
        ) or stat
        consequence = clean_text(check.get("known_consequences", item.get("known_consequences")), max_chars=300)
        if risk == "lethal" and not consequence:
            # 模型漏填致命后果：引擎兜底给一条通用警示，避免显示期空着、
            # 投骰期又被硬校验卡死（2026-08-23 反馈）。
            consequence = _LETHAL_DEFAULT_CONSEQUENCE
        risk_label = {"safe":"安全","controlled":"可控","dangerous":"危险","desperate":"绝境","lethal":"致命"}.get(risk, risk)
        if (
            contract is not None
            and risk != "safe"
            and contract["resolution"]["mode"] in {"dice_only", "attribute"}
        ):
            if risk in {"dangerous", "desperate", "lethal"}:
                # 风险档强制检定（2026-08-23 用户拍板）：危险/绝境/致命选项
                # 必然摇点——不依赖本世界的专有措辞，新本换词也不会失效。
                # 属性只是「选哪个骰子」：模型已带则用；否则精确推断 →
                # 宽泛行动词 → 世界属性描述字符重叠 → 世界首个允许属性。
                # 绝不给「通用」，也绝不让高风险行动静默免检。
                required = True
                if not stat:
                    auto_stat = _extract_check_attribute(contract, text)
                    if auto_stat is not None:
                        auto_stat = _translate_standard_key(
                            contract, auto_stat
                        ) or None
                    if auto_stat is None:
                        auto_stat = _force_check_attribute(text)
                        if auto_stat is not None:
                            auto_stat = _translate_standard_key(
                                contract, auto_stat
                            ) or None
                    if auto_stat is None:
                        auto_stat = _best_char_attribute(contract, text)
                    if auto_stat is None:
                        auto_stat = _fallback_attribute(contract)
                    if auto_stat is not None:
                        stat = auto_stat
                        label = ""
                if contract["resolution"]["mode"] == "attribute" and not stat:
                    # 退化世界兜底：attribute 模式但一个属性都没声明 → 无法摇点，
                    # 回退免检（否则 attribute_lookup 抛「必须使用世界属性 ID」）。
                    required = False
            elif not required:
                # 可控档：仍按精确推断自动补检定（推不出就不补，避免误摇）。
                auto_stat = _extract_check_attribute(contract, text)
                if auto_stat is not None:
                    required = True
                    stat = auto_stat
                    label = ""
        if contract is not None:
            danger_map = {str(x.get("id")):str(x.get("label")) for x in contract["danger_levels"]}
            if risk not in danger_map: raise ValueError(f"世界包不允许危险度：{risk}")
            risk_label = danger_map[risk]
            mode = contract["resolution"]["mode"]
            if mode in {"none","narrative"}: required=False; stat=label=""
            elif required and mode == "attribute":
                matched = attribute_lookup(contract, stat)
                if matched is None:
                    generic=contract["resolution"]["generic_check"]
                    if not generic.get("enabled",False): raise ValueError("需要属性检定时必须使用当前世界声明的属性 ID")
                    stat=""; label=str(generic.get("label") or "通用")
                else:
                    stat,label=matched
                    if stat not in contract["resolution"]["allowed_attributes"]: raise ValueError(f"世界包不允许属性检定：{stat}")
            elif required and mode == "dice_only": stat=label=""
        check_type=str(check.get("type",item.get("check_type","standard")) or "standard").lower()
        if check_type not in {"standard","leader","group","resistance","opposed"}: check_type="standard"
        declared_difficulty = check.get("difficulty", item.get("difficulty"))
        if contract is not None and required:
            mapped_difficulty = contract["resolution"]["difficulty_policy"].get(risk)
            if mapped_difficulty is None:
                if risk == "safe":
                    # safe 是插件定义的免检路径。模型误附 check 时直接
                    # 规范化为免检，避免为一个可确定修复的问题再请求模型。
                    required = False
                    stat = label = ""
                    difficulty = _bounded_int(declared_difficulty, 12, 5, 25)
                else:
                    raise ValueError(f"危险度 {risk} 不允许配置检定")
            else:
                difficulty = _bounded_int(mapped_difficulty, 12, 5, 25)
        else:
            difficulty=_bounded_int(declared_difficulty,12,5,25)
        adv=check.get("advantage_sources",item.get("advantage_sources")); dis=check.get("disadvantage_sources",item.get("disadvantage_sources"))
        row={"key":key,"text":text,"actor_id":clean_text(item.get("actor_id"),max_chars=128),"risk":risk,"danger_id":risk,"risk_label":risk_label,"requires_check":required,"collective":bool(item.get("collective",False)),"check_type":check_type,"check_stat":stat,"check_label":label,"difficulty":difficulty,"known_consequences":consequence,"advantage_sources":[clean_text(x,max_chars=120) for x in (adv if isinstance(adv,list) else [])[:8] if clean_text(x,max_chars=120)],"disadvantage_sources":[clean_text(x,max_chars=120) for x in (dis if isinstance(dis,list) else [])[:8] if clean_text(x,max_chars=120)]}
        row["check"]={"required":True,"attribute_id":stat,"attribute_label":label,"type":check_type,"difficulty":difficulty,"known_consequences":consequence} if required else None
        row["vote_scope"] = "local" if row["collective"] and item.get("vote_scope") == "local" else "party"
        if row["vote_scope"] == "local" and not row["text"].startswith("【现场小队】"):
            row["text"] = "【现场小队】" + row["text"][:44]
        by_key[key]=row
    if set(by_key)!=set(CHOICE_KEYS): raise ValueError("每回合必须提供 A、B、C、D 四个有效选项")
    result=[by_key[k] for k in CHOICE_KEYS]
    # 2026-08-22：不再强制每组至少一个 safe——致死/高风险选项该出现就出现，
    # 由剧情决定风险梯度，而非每组必须塞一个无风险退路。
    # 0.11.3：collective 输出校验——每轮最多 MAX_TEAM_CHOICES 个全队行动，
    # 超出的标记降级为个人选项（防止模型误标导致玩家只能投票、无法行动）。
    collective_indexes = [i for i, item in enumerate(result) if item["collective"]]
    for index in reversed(collective_indexes[MAX_TEAM_CHOICES:]):
        result[index]["collective"] = False
    return result


def normalize_choices_compat(value: Any, world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value,(str,bytes)): raise ValueError("行动选项必须是数组")
    items=[dict(x) for x in value if isinstance(x,Mapping)]
    if len(items)!=len(value): raise ValueError("行动选项必须全部是对象")
    assign=len(items)==4 and not any(str(x.get("key") or "").strip() for x in items)
    for i,item in enumerate(items):
        raw=chr(ord("A")+i) if assign else str(item.get("key") or "")
        match=re.fullmatch(r"(?:选项\s*)?([ABCD])(?:\s*[.、:：)）])?",unicodedata.normalize("NFKC",raw).strip().upper())
        if match: item["key"]=match.group(1)
    return normalize_choices(items,world)


def opening_choices(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules=world.get("rules"); rules=rules if isinstance(rules,Mapping) else {}
    try: return normalize_choices(rules.get("opening_choices"),world)
    except ValueError: return normalize_choices([dict(x) for x in DEFAULT_OPENING_CHOICES],world)


def fallback_choices(state: Mapping[str, Any], world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    location=clean_text(str(state.get("location") or "当前地点")[:12],max_chars=12); summary=clean_text(str(state.get("scene_summary") or "眼前局势")[:16],max_chars=16)
    return normalize_choices([{"key":"A","text":f"谨慎观察{location}，确认与“{summary}”有关的可见线索","risk":"safe","requires_check":False},{"key":"B","text":"向在场角色询问公开信息，不作强迫或结果预设","risk":"safe","requires_check":False},{"key":"C","text":"使用角色已经拥有的能力或物品作一次有限尝试","risk":"controlled","requires_check":False},{"key":"D","text":"保持警戒并暂缓冒险行动，为下一步搜集更多信息","risk":"safe","requires_check":False}],world)


def parse_choice_input(value: str) -> tuple[str | None, str]:
    """解析回合输入：A-D 选择返回 ``(key, flavor)``；否则返回 ``(None, "")``。

    ``key is None`` 表示玩家没有选择任何 A/B/C/D 选项，整段输入应按
    自由情景演绎处理（``jg <自由行动>`` / ``/酒馆 选择 <自由演绎>``）。
    需要强制选项的调用方（如投票）应在 ``key is None`` 时自行报错。
    """
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Da-d])(?:\s+(.+))?", text, re.S)
    if not match:
        return None, ""
    key = match.group(1).upper()
    flavor = clean_text(match.group(2) or "", max_chars=160)
    return key, flavor


_DURATION_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*"
    r"(秒|s|sec|分钟|分|min|m|小时|时|h|天|d)\s*$",
    re.I,
)


def parse_duration(value: str, *, maximum_days: int = 365) -> int:
    match = _DURATION_PATTERN.fullmatch(str(value or ""))
    if not match:
        raise ValueError("时间格式示例：30分钟、2小时、1天")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {
        "秒": 1,
        "s": 1,
        "sec": 1,
        "分钟": 60,
        "分": 60,
        "min": 60,
        "m": 60,
        "小时": 3600,
        "时": 3600,
        "h": 3600,
        "天": 86400,
        "d": 86400,
    }[unit]
    seconds = int(amount * multiplier)
    if seconds <= 0:
        raise ValueError("时间必须大于 0")
    return min(maximum_days * 86400, seconds)


def safe_exit_narrative(
    world: Mapping[str, Any],
    character_name: str,
    *,
    forced: bool = False,
) -> str:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    templates = rules.get("safe_exit_templates")
    if isinstance(templates, Sequence) and not isinstance(
        templates, (str, bytes)
    ):
        usable = [
            clean_text(item, max_chars=300)
            for item in templates
            if clean_text(item, max_chars=300)
        ]
        if usable:
            return usable[0].replace("{character}", character_name)
    if forced:
        return (
            f"{character_name}暂时离开了队伍，去处理一件无法拖延的私事。"
            "其余人保留了对方留下的联络线索，未来仍可能在合理时机重逢。"
        )
    return (
        f"{character_name}在确认眼前局势暂时稳定后，与众人作了简短告别。"
        "这段同行经历被完整保留，若条件允许，仍可循着旧线索重新会合。"
    )


# 选项字母对应的圆形字母 emoji，用于行动回合的视觉分区
_CHOICE_LETTER_EMOJI = {
    "A": "🅰️",
    "B": "🅱️",
    "C": "🅲️",
    "D": "🅳️",
    "E": "🅴️",
    "F": "🅵️",
}

# 0.11.3：每轮最多允许的全队行动（collective）选项数量。
# 超过上限的 collective 标记会被规范化为个人选项，防止模型误标
# 导致玩家只能投票、无法直接行动。
MAX_TEAM_CHOICES = 2

# 全队行动候选的编号标识（不占用个人选项的 A—D 字母）。
_TEAM_NUMBER_LABELS = ("①", "②", "③", "④")


def format_choices(character_name: str, choices: Sequence[Mapping[str, Any]], *, rerolls_left: int = 1) -> str:
    # 0.11.2：区分个人选项与「全队行动」（collective）选项。
    # 0.11.3：全队行动不再占用个人选项的 A—D 字母，改以「全队①/②」
    # 编号展示，并通过 jg 全队 指令进入表决。
    defaults={"safe":"安全","controlled":"可控","dangerous":"危险","desperate":"绝境","lethal":"致命"}

    def choice_annotations(item: Mapping[str, Any]) -> list[str]:
        annotations=[str(item.get("risk_label") or defaults.get(str(item.get("risk")),"可控"))]
        if item.get("requires_check"):
            label=str(item.get("check_label") or item.get("check_stat") or "").strip()
            try: dc=int(item.get("difficulty"))
            except (TypeError,ValueError): dc=0
            check_text=(f'需“{label}”检定' if label else "需检定")
            if dc: check_text += f" DC{dc}"
            annotations.append(check_text)
        return annotations

    def append_choice(lines: list[str], item: Mapping[str, Any], letter: str) -> None:
        annotations = choice_annotations(item)
        lines.append(f"{letter} {item.get('text')}（{' · '.join(annotations)}）")
        if str(item.get("risk")) in {"dangerous","desperate","lethal"} and item.get("known_consequences"): lines.append(f"⚠️ 已知后果：{item.get('known_consequences')}")

    lines=[f"🎯 【{character_name}的行动回合】"]
    personal=[item for item in choices if not bool(item.get("collective"))]
    collective=[item for item in choices if bool(item.get("collective"))]
    for item in personal:
        key=str(item.get("key") or ""); letter=_CHOICE_LETTER_EMOJI.get(key.upper(),key)
        append_choice(lines, item, letter)
    if collective:
        lines.append("")
        lines.append("🌐 【全队行动 · 需集体表决】")
        lines.append("（发送 jg 全队 选择；不消耗个人行动机会）")
        for index, item in enumerate(collective):
            number = _TEAM_NUMBER_LABELS[index] if index < len(_TEAM_NUMBER_LABELS) else f"{index+1}"
            append_choice(lines, item, f"🌐{number}")
    lines.extend(["","","💬 发送：jg A","📝 也可：/酒馆 选择 A 语气尽量温和",f"♻️ 本回合剩余重整次数：{max(0,rerolls_left)}"] )
    return "\n".join(lines)


def vote_result(
    *,
    eligible_count: int,
    ballots: Sequence[Mapping[str, Any]],
    option_keys: Sequence[str],
) -> dict[str, Any]:
    counts = {str(key): 0 for key in option_keys}
    for ballot in ballots:
        key = str(ballot.get("option_key") or "")
        if key in counts:
            counts[key] += 1
    cast_count = sum(counts.values())
    quorum = cast_count > eligible_count / 2
    winners = [
        key for key, count in counts.items()
        if cast_count and count > cast_count / 2
    ]
    return {
        "counts": counts,
        "cast_count": cast_count,
        "eligible_count": eligible_count,
        "quorum": quorum,
        "winner": winners[0] if quorum and len(winners) == 1 else "",
        "all_voted": cast_count >= eligible_count,
    }
