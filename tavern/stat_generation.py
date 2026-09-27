from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import product
from typing import Any

from .card_wizard import PRESET_REFS_KEY, preset_options


PRESET_STACK_MODE = "preset_stack"
STAT_GENERATION_SNAPSHOT_KEY = "stat_generation_snapshot"
MAX_PRESET_COMBINATIONS = 100_000


def modifier_from_table(table: Any, value: Any) -> int:
    """按修正表查修正；数值超界时钳制到最接近档位，绝不静默归零。

    修正表是世界声明的「属性值 → 修正」映射。当角色的属性值超出表内
    最大/最小键（例如表只写到 16、属性却到了 20）时，旧逻辑查表落空返回 0，
    导致高属性判定与低属性一样——本函数钳制到最近档位：
    - 空表 / 非法值 → 0
    - 表内精确命中 → 该档修正
    - 高于表最大键 → 取最大键修正（不再归零）
    - 低于表最小键 → 取最小键修正
    - 介于两档之间（稀疏表）→ 取不高于该值的最接近档位
    """
    if not isinstance(table, Mapping):
        return 0
    numeric: dict[int, int] = {}
    for key, raw in table.items():
        try:
            numeric[int(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    if not numeric:
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    if parsed in numeric:
        return numeric[parsed]
    below = [key for key in numeric if key <= parsed]
    if below:
        return numeric[max(below)]
    return numeric[min(numeric)]


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def uses_preset_stack_stats(template: Mapping[str, Any]) -> bool:
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    generation = stats.get("stat_generation")
    generation = generation if isinstance(generation, Mapping) else {}
    return str(stats.get("mode") or generation.get("mode") or "").lower() == PRESET_STACK_MODE


def stat_generation_config(template: Mapping[str, Any]) -> dict[str, Any]:
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    raw = stats.get("stat_generation")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "mode": str(raw.get("mode") or stats.get("mode") or "").lower(),
        "base_stats": dict(raw.get("base_stats") or {}),
        "bonus_sources": [str(item) for item in _sequence(raw.get("bonus_sources"))],
        "bonus_source_rules": {
            str(key): dict(value)
            for key, value in (raw.get("bonus_source_rules") or {}).items()
            if isinstance(value, Mapping)
        } if isinstance(raw.get("bonus_source_rules"), Mapping) else {},
        "expected_total": raw.get("expected_total", stats.get("budget")),
        "min_per_stat": raw.get("min_per_stat"),
        "max_per_stat": raw.get("max_per_stat"),
        "allow_manual_edit": bool(raw.get("allow_manual_edit", False)),
    }


def _attribute_index(template: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("key") or ""): dict(item)
        for item in template.get("stats", {}).get("attributes", [])
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }


def _field_index(template: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("key") or ""): dict(item)
        for item in template.get("fields", [])
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }


def _int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} 必须是整数")
    return value


def _option_bonus(
    option: Mapping[str, Any],
    attribute_keys: set[str],
    *,
    path: str,
) -> dict[str, int]:
    source = option.get("source")
    source = source if isinstance(source, Mapping) else option
    raw = source.get("stat_bonus")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"{path}.stat_bonus 必须是非空对象")
    bonus: dict[str, int] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key)
        if key not in attribute_keys:
            raise ValueError(f"{path}.stat_bonus 引用了未知属性：{key}")
        bonus[key] = _int(raw_value, f"{path}.stat_bonus.{key}")
    return bonus


def validate_stat_generation_config(
    template: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate preset_stack declarations and every legal preset combination."""

    if not uses_preset_stack_stats(template):
        return {"mode": str(template.get("stats", {}).get("mode") or "manual"), "combination_count": 0}
    config = stat_generation_config(template)
    if config["mode"] != PRESET_STACK_MODE:
        raise ValueError("stat_generation.mode 必须是 preset_stack")
    if config["allow_manual_edit"]:
        raise ValueError("preset_stack 必须设置 allow_manual_edit=false")

    attributes = _attribute_index(template)
    attribute_keys = set(attributes)
    base_raw = config["base_stats"]
    if set(base_raw) != attribute_keys:
        missing = sorted(attribute_keys - set(base_raw))
        extra = sorted(set(base_raw) - attribute_keys)
        detail = []
        if missing:
            detail.append("缺少 " + "、".join(missing))
        if extra:
            detail.append("未知 " + "、".join(extra))
        raise ValueError("stat_generation.base_stats 必须覆盖全部属性（" + "；".join(detail) + "）")
    base = {
        key: _int(base_raw[key], f"stat_generation.base_stats.{key}")
        for key in attributes
    }

    sources = config["bonus_sources"]
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("stat_generation.bonus_sources 必须是非空且不重复的字段列表")
    fields = _field_index(template)
    option_groups: list[list[dict[str, Any]]] = []
    source_rules = config["bonus_source_rules"]
    for source_id in sources:
        field = fields.get(source_id)
        if not field:
            raise ValueError(f"bonus_sources 引用了不存在的建卡字段：{source_id}")
        if str(field.get("type") or "") not in {"select", "preset_select"}:
            raise ValueError(f"加成来源 {source_id} 必须是单选预设字段")
        options = preset_options(template, field, {})
        if not options:
            raise ValueError(f"加成来源 {source_id} 没有有效选项")
        expected_source_total = source_rules.get(source_id, {}).get("expected_bonus_total")
        normalized: list[dict[str, Any]] = []
        for option in options:
            option_id = str(option.get("id") or "?")
            bonus = _option_bonus(
                option,
                attribute_keys,
                path=f"{source_id}.{option_id}",
            )
            if expected_source_total is not None:
                expected_value = _int(
                    expected_source_total,
                    f"bonus_source_rules.{source_id}.expected_bonus_total",
                )
                if sum(bonus.values()) != expected_value:
                    raise ValueError(
                        f"加成来源 {source_id} 的选项 {option_id} 合计必须为 {expected_value}"
                    )
            normalized.append({**option, "stat_bonus": bonus})
        option_groups.append(normalized)

    combination_count = 1
    for options in option_groups:
        combination_count *= len(options)
    if combination_count > MAX_PRESET_COMBINATIONS:
        raise ValueError(
            f"preset_stack 合法组合数 {combination_count} 超过发布体检上限 {MAX_PRESET_COMBINATIONS}"
        )
    expected_total = _int(config["expected_total"], "stat_generation.expected_total")
    configured_min = config.get("min_per_stat")
    configured_max = config.get("max_per_stat")
    for combination in product(*option_groups):
        generated = dict(base)
        option_ids: list[str] = []
        for option in combination:
            option_ids.append(str(option.get("id") or "?"))
            for key, bonus in option["stat_bonus"].items():
                generated[key] += bonus
        if sum(generated.values()) != expected_total:
            raise ValueError(
                "preset_stack 组合总和不符："
                + "+".join(option_ids)
                + f" 得到 {sum(generated.values())}，应为 {expected_total}"
            )
        for key, value in generated.items():
            minimum = int(configured_min) if configured_min is not None else int(attributes[key]["minimum"])
            maximum = int(configured_max) if configured_max is not None else int(attributes[key]["maximum"])
            if not minimum <= value <= maximum:
                raise ValueError(
                    "preset_stack 组合越界："
                    + "+".join(option_ids)
                    + f" 的 {attributes[key].get('label', key)}={value}，允许 {minimum}—{maximum}"
                )
    return {
        "mode": PRESET_STACK_MODE,
        "combination_count": combination_count,
        "expected_total": expected_total,
        "sources": list(sources),
    }


def _selected_option(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    source_id: str,
) -> dict[str, Any] | None:
    field = _field_index(template).get(source_id)
    if not field:
        return None
    refs = fields.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    ref = refs.get(source_id)
    ref = ref if isinstance(ref, Mapping) else {}
    options = preset_options(template, field, fields)
    current = str(fields.get(source_id) or "").casefold()
    ref_id = str(ref.get("id") or "").casefold()
    ref_values = {
        ref_id,
        str(ref.get("value") or "").casefold(),
        str(ref.get("label") or "").casefold(),
    }
    ref_values.discard("")
    if current and current in ref_values and ref_id:
        for option in options:
            if str(option.get("id") or "").casefold() == ref_id:
                return option
    if current:
        current_matches: list[dict[str, Any]] = []
        for option in options:
            identities = {
                str(option.get("id") or "").casefold(),
                str(option.get("value") or "").casefold(),
                str(option.get("label") or "").casefold(),
            }
            if current in identities:
                current_matches.append(option)
        if len(current_matches) == 1:
            return current_matches[0]
        if len(current_matches) > 1:
            raise ValueError(
                f"属性来源 {source_id} 的当前值对应多个预设，请使用稳定 ID"
            )
    candidates = {
        str(ref.get("id") or "").casefold(),
        str(ref.get("value") or "").casefold(),
        str(ref.get("label") or "").casefold(),
    }
    candidates.discard("")
    for option in options:
        identities = {
            str(option.get("id") or "").casefold(),
            str(option.get("value") or "").casefold(),
            str(option.get("label") or "").casefold(),
        }
        if candidates & identities:
            return option
    return None


def calculate_preset_stack_stats(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any] | None:
    """Calculate from the immutable base on every call; never add to stored stats."""

    if not uses_preset_stack_stats(template):
        raise ValueError("当前角色卡不是 preset_stack 属性模式")
    config = stat_generation_config(template)
    attributes = _attribute_index(template)
    keys = set(attributes)
    base = {key: int(config["base_stats"][key]) for key in attributes}
    generated = dict(base)
    sources: list[dict[str, Any]] = []
    for source_id in config["bonus_sources"]:
        option = _selected_option(template, fields, source_id)
        if option is None:
            if require_complete:
                label = _field_index(template).get(source_id, {}).get("label") or source_id
                raise ValueError(f"尚未选择：{label}")
            return None
        bonus = _option_bonus(option, keys, path=f"{source_id}.{option.get('id') or '?'}")
        for key, value in bonus.items():
            generated[key] += value
        sources.append(
            {
                "source_id": source_id,
                "source_label": str(_field_index(template).get(source_id, {}).get("label") or source_id),
                "option_id": str(option.get("id") or ""),
                "option_label": str(option.get("label") or option.get("value") or ""),
                "stat_bonus": bonus,
            }
        )
    expected_total = int(config["expected_total"])
    if sum(generated.values()) != expected_total:
        raise ValueError(f"自动生成属性总和为 {sum(generated.values())}，应为 {expected_total}")
    for key, value in generated.items():
        minimum = int(config["min_per_stat"]) if config.get("min_per_stat") is not None else int(attributes[key]["minimum"])
        maximum = int(config["max_per_stat"]) if config.get("max_per_stat") is not None else int(attributes[key]["maximum"])
        if not minimum <= value <= maximum:
            raise ValueError(f"{attributes[key].get('label', key)}自动生成值 {value} 超出 {minimum}—{maximum}")
    labels = {key: str(item.get("label") or key) for key, item in attributes.items()}
    table = template.get("stats", {}).get("modifier_table") or {}
    modifiers = {key: modifier_from_table(table, value) for key, value in generated.items()}
    snapshot = {
        "mode": PRESET_STACK_MODE,
        "base_stats": dict(base),
        "sources": sources,
    }
    return {
        "mode": PRESET_STACK_MODE,
        "base": base,
        "raw": generated,
        "labels": labels,
        "modifiers": modifiers,
        "sources": sources,
        "base_total": sum(base.values()),
        "bonus_total": sum(sum(item["stat_bonus"].values()) for item in sources),
        "effective_total": sum(generated.values()),
        "budget": expected_total,
        "modifier_table": dict(table),
        STAT_GENERATION_SNAPSHOT_KEY: snapshot,
    }


def clear_generated_stats(template: Mapping[str, Any], fields: dict[str, Any]) -> None:
    for key in _attribute_index(template):
        fields.pop(f"stat_{key}", None)
    fields.pop("resolved_stat_total", None)
    fields.pop(STAT_GENERATION_SNAPSHOT_KEY, None)


# ---------------------------------------------------------------------------
# authored 模式：属性由叙事模型根据玩家自拟设定分配
# ---------------------------------------------------------------------------
#
# 2026-09-20 需求（修订版）：角色卡不再由世界包预设**职业**，改由玩家在最后一段
# 自拟设定里写背景，插件据此生成**基础属性分配**。
#
# **主属性 +N / 副属性 +M 仍然由玩家自己选**，与原来的 profession 模式完全一致：
# authored 只替代"职业基础值"那一块。所以生成结果写进 `profession_base_stats`，
# 之后由既有的 resolve_profession_stats 负责叠加主副加点与算修正。
#
# 与 preset_stack 的共同点是数字都进 stat_<key>；区别只在基础值从哪来（预设表
# vs 一次模型调用）。下游检定与能力衡量不需要改。
#
# 模型可能返回不合规的数字（少项、越界、总和不等于基础预算），所以解析严格校验，
# 失败时用确定性兜底分配补齐——**建卡不允许因为模型输出问题而卡住**。
AUTHORED_MODE = "authored"
AUTHORED_REASON_MAX = 200
AUTHORED_BASE_FIELD = "profession_base_stats"

# 自拟设定字段可用的类型。这些是玩家自由输入的文本字段。
FREEFORM_FIELD_TYPES = frozenset({"text", "long_text", "textarea", "paragraph"})


def uses_authored_stats(template: Mapping[str, Any]) -> bool:
    return str(stat_generation_config(template)["mode"]).lower() == AUTHORED_MODE


def uses_generated_stats(template: Mapping[str, Any]) -> bool:
    """属性是否由系统生成（预设求和或模型分配），而非玩家逐步手填。"""
    return uses_preset_stack_stats(template) or uses_authored_stats(template)


def authored_stat_config(template: Mapping[str, Any]) -> dict[str, Any]:
    """读取 authored 模式配置。键缺失时的语义见 validate_authored_stat_config。"""
    config = stat_generation_config(template)
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    raw = stats.get("stat_generation")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "mode": AUTHORED_MODE,
        "source_field": str(
            raw.get("source_field") or raw.get("background_field") or ""
        ).strip(),
        "expected_total": config.get("expected_total"),
        "min_per_stat": config.get("min_per_stat"),
        "max_per_stat": config.get("max_per_stat"),
        "base_stats": dict(config.get("base_stats") or {}),
        "guide": str(raw.get("guide") or "").strip()[:400],
    }


def _authored_bounds(
    template: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """返回 (attributes, minimum, maximum)。逐项下限优先用配置，否则用模板声明。"""
    attributes = _attribute_index(template)
    lows: dict[str, int] = {}
    highs: dict[str, int] = {}
    for key, item in attributes.items():
        low = config.get("min_per_stat")
        high = config.get("max_per_stat")
        lows[key] = int(low) if low is not None else int(item["minimum"])
        highs[key] = int(high) if high is not None else int(item["maximum"])
    return attributes, lows, highs


def validate_authored_stat_config(template: Mapping[str, Any]) -> dict[str, Any]:
    """发布期体检：authored 模式必须自洽，且兜底分配一定存在。"""
    config = authored_stat_config(template)
    attributes, lows, highs = _authored_bounds(template, config)
    if not attributes:
        raise ValueError("authored 模式需要 stats.attributes 声明属性表")
    source_field = config["source_field"]
    if not source_field:
        raise ValueError("authored 模式必须声明 stat_generation.source_field")
    field = _field_index(template).get(source_field)
    if not field:
        raise ValueError(f"source_field 引用了不存在的建卡字段：{source_field}")
    field_type = str(field.get("type") or "text").lower()
    if field_type not in FREEFORM_FIELD_TYPES:
        raise ValueError(
            f"source_field {source_field} 必须是自由输入文本字段，当前为 {field_type}"
        )
    if not field.get("required"):
        raise ValueError(f"source_field {source_field} 必须设为必填")
    total = config.get("expected_total")
    if total is None:
        raise ValueError("authored 模式必须声明 stat_generation.expected_total")
    try:
        total = int(total)
    except (TypeError, ValueError) as exc:
        raise ValueError("authored 模式 expected_total 必须是整数") from exc
    floor = sum(lows.values())
    ceiling = sum(highs.values())
    if not floor <= total <= ceiling:
        raise ValueError(
            f"authored 模式 expected_total={total} 不可达："
            f"逐项下限合计 {floor}，上限合计 {ceiling}"
        )
    return {
        "mode": AUTHORED_MODE,
        "source_field": source_field,
        "expected_total": total,
        "floor": floor,
        "ceiling": ceiling,
        "attribute_count": len(attributes),
    }


def authored_fallback_allocation(template: Mapping[str, Any]) -> dict[str, int]:
    """确定性兜底分配：先给每项下限，再把余量按顺序摊平。

    模型输出不合规时用它，保证建卡永远能完成。分配结果可复现，便于排查。
    """
    config = authored_stat_config(template)
    attributes, lows, highs = _authored_bounds(template, config)
    total = int(config.get("expected_total") or sum(lows.values()))
    allocation = {key: lows[key] for key in attributes}
    remaining = total - sum(allocation.values())
    if remaining < 0:
        return allocation
    keys = list(attributes)
    index = 0
    while remaining > 0 and keys:
        key = keys[index % len(keys)]
        if allocation[key] < highs[key]:
            allocation[key] += 1
            remaining -= 1
        elif all(allocation[k] >= highs[k] for k in keys):
            break
        index += 1
    return allocation


def authored_stat_prompt(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> str:
    """构造属性分配提示词。只给模型属性表、预算与玩家自拟设定，不给别的。"""
    config = authored_stat_config(template)
    attributes, lows, highs = _authored_bounds(template, config)
    source_text = str(fields.get(config["source_field"]) or "").strip()
    lines = [f"根据下面这段玩家自拟的角色设定，为这个角色分配 {len(attributes)} 项属性。"]
    lines.append("")
    lines.append("【角色设定】")
    lines.append(source_text)
    lines.append("")
    lines.append("【属性表】")
    for key, item in attributes.items():
        hint = str(item.get("description") or item.get("hint") or "").strip()
        suffix = f"（{hint}）" if hint else ""
        lines.append(
            f"- {key}｜{item.get('label') or key}："
            f"{lows[key]}—{highs[key]}{suffix}"
        )
    total = int(config["expected_total"])
    lines.append("")
    lines.append(f"【要求】总和必须正好等于 {total}；每项必须落在自己的区间内。")
    if config.get("guide"):
        lines.append(f"【倾向】{config['guide']}")
    lines.append(
        "设定里明确写了的能力要给高分，没提到的不给高于中位的分；"
        "设定与某项属性矛盾时按设定来。不要为了平均而抹平差异。"
    )
    lines.append("")
    lines.append(
        '只输出一个 JSON 对象，不要任何其他文字：{"attributes": {'
        + ", ".join(f'"{key}": <整数>' for key in attributes)
        + '}, "reason": "一句话说明为什么这样分配"}'
    )
    return "\n".join(lines)


def parse_authored_allocation(
    template: Mapping[str, Any],
    payload: Any,
) -> dict[str, int]:
    """严格校验模型返回的分配；任何不合规都抛 ValueError（调用方走兜底）。"""
    config = authored_stat_config(template)
    attributes, lows, highs = _authored_bounds(template, config)
    raw = payload
    if isinstance(raw, Mapping) and "attributes" in raw:
        raw = raw.get("attributes")
    if not isinstance(raw, Mapping):
        raise ValueError("属性分配必须是对象")
    allocation: dict[str, int] = {}
    for key in attributes:
        if key not in raw:
            raise ValueError(f"属性分配缺少 {key}")
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"属性 {key} 必须是整数")
        if not lows[key] <= value <= highs[key]:
            raise ValueError(
                f"属性 {key} 为 {value}，必须在 {lows[key]}—{highs[key]} 之间"
            )
        allocation[key] = value
    total = sum(allocation.values())
    expected = int(config["expected_total"])
    if total != expected:
        raise ValueError(f"属性总和为 {total}，应为 {expected}")
    return allocation


def apply_authored_allocation(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    allocation: Mapping[str, int],
    *,
    reason: str = "",
    provider_id: str = "",
) -> dict[str, Any]:
    """把**基础分配**写入卡片字段，并留下可审计的快照。

    写进 `profession_base_stats`（与 profession 模式同一位置），后续由
    `resolve_profession_stats` 叠加玩家选的主/副属性加点。这里同时把基础值先
    落到 `stat_<key>`，这样即使玩家还没选主副属性，预览也能看到当前数值。

    快照记录**依据的原文与理由**：基础值决定的最终属性会被用来衡量玩家能力，
    出问题时必须能看出当时是按哪段设定、由哪个模型算出来的。
    """
    config = authored_stat_config(template)
    attributes = _attribute_index(template)
    base = {key: int(value) for key, value in allocation.items()}
    clear_generated_stats(template, fields)
    fields[AUTHORED_BASE_FIELD] = dict(base)
    for key, value in base.items():
        fields[f"stat_{key}"] = value
    labels = {key: str(item.get("label") or key) for key, item in attributes.items()}
    table = template.get("stats", {}).get("modifier_table") or {}
    modifiers = {
        key: modifier_from_table(table, value) for key, value in base.items()
    }
    total = sum(base.values())
    fields["resolved_stat_total"] = total
    fields[STAT_GENERATION_SNAPSHOT_KEY] = {
        "mode": AUTHORED_MODE,
        "source_field": config["source_field"],
        "source_text": str(fields.get(config["source_field"]) or "").strip()[:1000],
        "reason": str(reason or "")[:AUTHORED_REASON_MAX],
        "provider_id": str(provider_id or ""),
        "base_stats": dict(base),
        "total": total,
    }
    return {
        "mode": AUTHORED_MODE,
        "base": dict(base),
        "raw": dict(base),
        "labels": labels,
        "modifiers": modifiers,
        "total": total,
        "budget": int(config["expected_total"]),
        "reason": str(reason or "")[:AUTHORED_REASON_MAX],
        "provider_id": str(provider_id or ""),
    }


def format_authored_stat_result(resolved: Mapping[str, Any]) -> str:
    """渲染**基础分配**结果。

    这里报的是基础值（re0 为 50），主/副属性加点由玩家在后续步骤自己选，
    所以不能把它说成最终属性值，也不该显示最终总和。
    """
    labels = resolved.get("labels") or {}
    raw = resolved.get("raw") or {}
    title = (
        "【角色五维基础值已按设定生成】"
        if len(raw) == 5
        else "【角色属性基础值已按设定生成】"
    )
    lines = [
        title,
        "｜".join(
            f"{labels.get(key, key)} {value}" for key, value in raw.items()
        ),
        "",
        f"基础合计：{resolved.get('total', 0)}",
    ]
    reason = str(resolved.get("reason") or "").strip()
    if reason:
        lines.append(f"分配依据：{reason}")
    lines.append("接下来由你自己选主属性与副属性加点，之后还能修改。")
    return "\n".join(lines)



def sync_preset_stack_fields(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    *,
    require_complete: bool = False,
) -> dict[str, Any] | None:
    clear_generated_stats(template, fields)
    resolved = calculate_preset_stack_stats(
        template,
        fields,
        require_complete=require_complete,
    )
    if resolved is None:
        return None
    for key, value in resolved["raw"].items():
        fields[f"stat_{key}"] = value
    refs = fields.get(PRESET_REFS_KEY)
    refs = dict(refs) if isinstance(refs, Mapping) else {}
    for source in resolved["sources"]:
        source_id = str(source["source_id"])
        option = _selected_option(template, fields, source_id)
        option_source = option.get("source") if isinstance(option, Mapping) else {}
        refs[source_id] = {
            "id": str(source["option_id"]),
            "value": str(option.get("value") or "") if isinstance(option, Mapping) else "",
            "label": str(source["option_label"]),
            "snapshot": dict(option_source) if isinstance(option_source, Mapping) else {},
        }
    fields[PRESET_REFS_KEY] = refs
    fields["resolved_stat_total"] = int(resolved["effective_total"])
    fields[STAT_GENERATION_SNAPSHOT_KEY] = dict(
        resolved[STAT_GENERATION_SNAPSHOT_KEY]
    )
    return resolved


def assess_preset_stack_migration(
    template: Mapping[str, Any],
    profile: Mapping[str, Any],
    stored_stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a legacy card without mutating it or silently changing values."""

    resolved = calculate_preset_stack_stats(template, profile, require_complete=True)
    assert resolved is not None
    raw = stored_stats.get("raw")
    raw = raw if isinstance(raw, Mapping) else {}
    try:
        matches = all(
            key in raw and int(raw[key]) == value
            for key, value in resolved["raw"].items()
        )
    except (TypeError, ValueError):
        matches = False
    return {
        "status": "snapshot_backfill_safe" if matches else "admin_confirmation_required",
        "matches": matches,
        "stored": dict(raw),
        "calculated": dict(resolved["raw"]),
        STAT_GENERATION_SNAPSHOT_KEY: dict(resolved[STAT_GENERATION_SNAPSHOT_KEY]),
    }


def format_preset_stack_result(resolved: Mapping[str, Any]) -> str:
    labels = resolved.get("labels") or {}
    raw = resolved.get("raw") or {}
    title = (
        "【角色五维已自动生成】"
        if len(raw) == 5
        else "【角色属性已自动生成】"
    )
    lines = [
        title,
        "｜".join(
            f"{labels.get(key, key)} {value}"
            for key, value in raw.items()
        ),
        "",
        "属性来源：",
    ]
    for source in resolved.get("sources") or []:
        bonus = source.get("stat_bonus") or {}
        lines.append(
            f"{source.get('option_label') or source.get('option_id')}："
            + "、".join(
                f"{labels.get(key, key)}{int(value):+d}"
                for key, value in bonus.items()
            )
        )
    lines.extend(
        [
            "",
            f"总和：{resolved.get('effective_total', 0)}",
            "属性已根据角色预设锁定，将继续填写后续资料。",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "AUTHORED_MODE",
    "MAX_PRESET_COMBINATIONS",
    "PRESET_STACK_MODE",
    "STAT_GENERATION_SNAPSHOT_KEY",
    "apply_authored_allocation",
    "assess_preset_stack_migration",
    "authored_fallback_allocation",
    "authored_stat_config",
    "authored_stat_prompt",
    "calculate_preset_stack_stats",
    "clear_generated_stats",
    "format_authored_stat_result",
    "format_preset_stack_result",
    "parse_authored_allocation",
    "stat_generation_config",
    "sync_preset_stack_fields",
    "uses_authored_stats",
    "uses_generated_stats",
    "uses_preset_stack_stats",
    "validate_authored_stat_config",
    "validate_stat_generation_config",
]
