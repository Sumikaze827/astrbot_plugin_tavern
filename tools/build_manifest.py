"""Generate the v0.12.0 source file inventory with SHA-256 hashes."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "FILE_MANIFEST.md"
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}


def main() -> None:
    paths = sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not EXCLUDED_PARTS.intersection(path.parts)
        and path.suffix != ".pyc"
        and path != OUTPUT
    )
    lines = [
        "# v0.12.0 完整文件清单",
        "",
        "以下清单由 `tools/build_manifest.py` 生成。路径相对于插件根目录。",
        "",
        f"文件总数（含本清单）：{len(paths) + 1}",
        "",
        "| 路径 | 字节 | SHA-256 |",
        "|---|---:|---|",
    ]
    for path in paths:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        relative = path.relative_to(ROOT).as_posix().replace("|", "\\|")
        lines.append(f"| `{relative}` | {len(payload)} | `{digest}` |")
    lines.append(
        "| `docs/FILE_MANIFEST.md` | 生成后变化 | `self-generated` |"
    )
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {OUTPUT} ({len(paths) + 1} files)")


if __name__ == "__main__":
    main()
