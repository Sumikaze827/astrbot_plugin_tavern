from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .lifecycle import (
    card_stat_allocation,
    card_template,
    resolve_profession_stats,
)
from .stat_generation import (
    modifier_from_table,
    sync_preset_stack_fields,
    uses_authored_stats,
    uses_preset_stack_stats,
)


def validate_card_revision(
    world: Mapping[str, Any],
    profile: Mapping[str, Any],
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an edited card against the frozen instance contract."""

    template = card_template(world)
    fields = dict(profile)
    missing = [
        str(item.get("label") or item.get("key"))
        for item in template["fields"]
        if item.get("required")
        and item.get("type") != "derived"
        and not str(fields.get(str(item.get("key") or ""), "")).strip()
    ]
    if missing:
        raise ValueError("尚未填写：" + "、".join(missing))
    mode = str(template["stats"].get("mode") or "manual")
    if uses_preset_stack_stats(template):
        resolved = sync_preset_stack_fields(
            template,
            fields,
            require_complete=True,
        )
        assert resolved is not None
    elif uses_authored_stats(template):
        # 基础值来自模型（在 profession_base_stats），主/副属性加点仍由玩家选，
        # 所以改卡直接复用 profession 模式那条加点路径，不重算基础值
        # （重算要再调一次模型，而这里是同步校验路径）。
        resolved = resolve_profession_stats(template, fields, require_complete=True)
        # 最终值要落回 profile，否则这次修订只存了 stats，下一次改卡/展示
        # 读不到 stat_<key> 就会当成没加点。
        for key, value in resolved["raw"].items():
            fields[f"stat_{key}"] = value
    elif mode == "preset":
        resolved = resolve_profession_stats(template, fields, require_complete=True)
    elif mode == "manual":
        raw = dict((stats or {}).get("raw") or {})
        for attribute in template["stats"]["attributes"]:
            key = str(attribute["key"])
            field_key = f"stat_{key}"
            if field_key in fields:
                raw[key] = int(fields[field_key])
            if key not in raw:
                raise ValueError(f"缺少属性：{attribute['label']}")
            value = int(raw[key])
            if not int(attribute["minimum"]) <= value <= int(attribute["maximum"]):
                raise ValueError(f"{attribute['label']}超出允许范围")
            fields[field_key] = value
        allocation = card_stat_allocation(template, fields)
        if not allocation.get("total_ok", False):
            raise ValueError("角色属性总值不符合当前世界模板")
        table = template["stats"].get("modifier_table", {})
        resolved = {
            "mode": "manual",
            "raw": raw,
            "labels": {
                str(item["key"]): str(item["label"])
                for item in template["stats"]["attributes"]
            },
            "modifiers": {
                key: modifier_from_table(table, value) for key, value in raw.items()
            },
            "budget": int(template["stats"].get("budget", 0)),
        }
    else:
        resolved = {"mode": "none", "raw": {}, "labels": {}, "modifiers": {}}
    return {
        "profile": fields,
        "stats": resolved,
        "template_version": int(template.get("version", 1)),
        "requires_review": bool(template.get("edit_requires_review", True)),
    }
