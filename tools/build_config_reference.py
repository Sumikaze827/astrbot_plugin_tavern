"""从 _conf_schema.json 生成 docs/CONFIG_REFERENCE.md（v0.12.0 文档工具）。

用法：python tools/build_config_reference.py
在仓库根目录运行；输出覆盖 docs/CONFIG_REFERENCE.md。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


_TYPE_LABELS = {
    "string": "字符串",
    "list": "列表",
    "bool": "布尔",
    "int": "整数",
    "float": "浮点数",
    "text": "长文本",
    "object": "分组",
}

_GROUP_TITLES = {
    "security": "安全与群范围",
    "model": "叙事模型",
    "runtime": "运行规则",
    "advanced": "高级设置",
    "token_quota": "Token 配额默认值",
}


def _item_rows(key: str, item: dict, depth: int) -> list[str]:
    rows: list[str] = []
    indent = "  " * depth
    label = _TYPE_LABELS.get(str(item.get("type") or "string"), "字符串")
    default = item.get("default", "")
    default_text = f"`{default}`" if default not in ("", None) else "—"
    hint = str(item.get("hint") or "").strip()
    rows.append(f"{indent}- **{key}**（{label}）：{item.get('description', '')}")
    if hint:
        rows.append(f"{indent}  - 说明：{hint}")
    rows.append(f"{indent}  - 默认值：{default_text}")
    options = item.get("options")
    labels = item.get("labels")
    if options and labels:
        pairs = "、".join(
            f"`{option}`（{labels[index] if index < len(labels) else option}）"
            for index, option in enumerate(options)
        )
        rows.append(f"{indent}  - 可选值：{pairs}")
    elif options:
        rows.append(
            f"{indent}  - 可选值：{'、'.join(f'`{o}`' for o in options)}"
        )
    return rows


def build(root: Path) -> str:
    schema = json.loads(
        (root / "_conf_schema.json").read_text(encoding="utf-8")
    )
    lines: list[str] = []
    lines.append("# AI 酒馆配置参考（自动生成）")
    lines.append("")
    lines.append(
        "> 本文档由 `tools/build_config_reference.py` 从 `_conf_schema.json` 自动生成。"
        "修改配置项后请重新运行该工具以同步。"
    )
    lines.append("")
    for group_key, group in schema.items():
        title = _GROUP_TITLES.get(group_key, group_key)
        lines.append(f"## {group_key} · {title}")
        lines.append("")
        lines.append(f"{group.get('description', '')}")
        lines.append("")
        for key, item in group.get("items", {}).items():
            if item.get("type") == "object":
                lines.append(f"### {key}")
                lines.append("")
                lines.append(f"{item.get('description', '')}")
                if item.get("hint"):
                    lines.append("")
                    lines.append(f"> {item['hint']}")
                lines.append("")
                for sub_key, sub_item in item.get("items", {}).items():
                    lines.extend(_item_rows(sub_key, sub_item, 0))
            else:
                lines.extend(_item_rows(key, item, 0))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    output = build(ROOT)
    target = ROOT / "docs" / "CONFIG_REFERENCE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(output, encoding="utf-8")
    print(f"written: {target.relative_to(ROOT)} ({len(output.splitlines())} lines)")
    sys.exit(0)
