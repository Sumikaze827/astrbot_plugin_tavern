"""集中式版本同步工具（审计 #17）。

以 tavern/constants.py 的 PLUGIN_VERSION 为唯一真相，自动回写其余版本引用：

- metadata.yaml                 version: <VER>
- templates/template-manifest.json  compatible_plugin_version
- main.py / presentation.py      HELP_TEXT 中的 v<VER> 字样
- world_contract.py              最低版本提示文案
- pages/console/index.html       ?v=<VER> 查询串
- pages/console/app.js           备份导出默认文件名 backup_tavern_v<VER>.zip

用法：
    python tools/sync_version.py            # 仅校验一致性
    python tools/sync_version.py --write     # 写回不一致项
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSTANTS = ROOT / "tavern" / "constants.py"


def current_version() -> str:
    match = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', CONSTANTS.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("无法从 constants.py 解析 PLUGIN_VERSION")
    return match.group(1)


def targets(version: str) -> list[tuple[Path, str, str]]:
    v = version
    return [
        (ROOT / "metadata.yaml", "version: 0.0.0", f"version: {v}"),
        (ROOT / "templates" / "template-manifest.json", '"compatible_plugin_version": "0.0.0"', f'"compatible_plugin_version": "{v}"'),
        (ROOT / "main.py", "【AI 酒馆 v0.0.0｜", f"【AI 酒馆 v{v}｜"),
        (ROOT / "tavern" / "presentation.py", "【AI 酒馆 v{PLUGIN_VERSION}｜", "【AI 酒馆 v{PLUGIN_VERSION}｜"),
        (ROOT / "tavern" / "world_contract.py", 'f"{PLUGIN_VERSION} 仅接受世界包协议', 'f"{PLUGIN_VERSION} 仅接受世界包协议'),
        (ROOT / "pages" / "console" / "index.html", "?v=0.0.0", f"?v={v}"),
        (ROOT / "pages" / "console" / "app.js", "backup_tavern_v0.0.0.zip", f"backup_tavern_v{v}.zip"),
    ]


def main() -> int:
    write = "--write" in sys.argv
    version = current_version()
    print(f"PLUGIN_VERSION = {version}")
    dirty = 0
    for path, pattern, replacement in targets(version):
        if not path.exists():
            print(f"  - 缺失文件: {path.relative_to(ROOT)}")
            dirty += 1
            continue
        text = path.read_text(encoding="utf-8")
        # 通配 0.0.0 占位符，便于先写占位再同步
        regex = re.compile(re.escape(pattern).replace(r"0\.0\.0", r"[0-9A-Za-z._-]+"))
        found = regex.search(text)
        if found and found.group(0) == replacement:
            print(f"  OK  {path.relative_to(ROOT)}")
        elif found:
            if write:
                text = regex.sub(lambda m: replacement, text)
                path.write_text(text, encoding="utf-8")
                print(f"  FIX {path.relative_to(ROOT)}: {found.group(0)} -> {replacement}")
            else:
                print(f"  !! {path.relative_to(ROOT)}: {found.group(0)} != {replacement}")
            dirty += 1
        else:
            print(f"  ?? {path.relative_to(ROOT)}: 未找到目标模式（{pattern}）")
            dirty += 1
    if dirty and not write:
        print("\n存在不一致；使用 --write 回写。")
        return 1
    print("\n全部一致。" if not dirty else "\n已回写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
