"""v0.12.0 发布前检查脚本。

用法：
    python tools/run_checks.py              # 编译 + JS 语法 + 全量单测，对照基线
    python tools/run_checks.py --refresh-baseline   # 更新 tests/baseline.txt

行为：
- 全仓 Python 编译检查（失败即退出非零）；
- 前端 app.js 语法检查（可用 node 时执行）；
- 运行全部 unittest；与 tests/baseline.txt 中的「已知失败子集」比对，
  任何新出现的失败/报错都会导致退出码非零（防止回归悄悄扩散）；
- 已知失败子集仅作记录，不在本脚本中静默吞掉。
"""
import io
import os
import py_compile
import re
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "baseline.txt"

# 保证以 `python tools/run_checks.py` 运行时测试模块也能 import tavern.*
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def compile_all() -> list[str]:
    errors: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or "work" in path.parts:
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"  py_compile {path.relative_to(ROOT)}: {exc}")
    return errors


def node_check() -> list[str]:
    errors: list[str] = []
    node = shutil.which("node")
    if not node:
        print("  - node 不可用，跳过前端语法检查")
        return errors
    import subprocess

    app_js = ROOT / "pages" / "console" / "app.js"
    result = subprocess.run(
        [node, "--check", str(app_js)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        errors.append(f"  node --check app.js 失败:\n{result.stderr}")
    return errors


def run_unittest() -> tuple[str, set[str]]:
    buffer = io.StringIO()
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(
        stream=buffer, verbosity=1, buffer=True
    )
    result = runner.run(suite)
    output = buffer.getvalue()
    failed_ids: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^(FAIL|ERROR): (\S+)", line)
        if match:
            failed_ids.add(match.group(2))
    summary = (
        f"Ran {result.testsRun} tests: "
        f"ok={result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)}, "
        f"failures={len(result.failures)}, errors={len(result.errors)}, "
        f"skipped={len(result.skipped)}"
    )
    return summary, failed_ids


def load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def write_baseline(ids: set[str]) -> None:
    header = (
        "# AI 酒馆已知失败/报错基线（作者记录的已知失败子集，随版本演进更新）\n"
        "# 每条为 unittest 输出中的 FAIL:/ERROR: 测试标识。\n"
    )
    BASELINE.write_text(
        header + "".join(f"{item}\n" for item in sorted(ids)),
        encoding="utf-8",
    )


def main() -> int:
    refresh = "--refresh-baseline" in sys.argv
    print("== 1/3 Python 编译检查 ==")
    compile_errors = compile_all()
    for error in compile_errors:
        print(error)
    if compile_errors:
        print(f"编译失败 {len(compile_errors)} 项")
        return 1
    print("  OK")

    print("== 2/3 前端语法检查 ==")
    js_errors = node_check()
    for error in js_errors:
        print(error)
    if js_errors:
        return 1
    print("  OK")

    print("== 3/3 全量单测 ==")
    summary, failed_ids = run_unittest()
    print(f"  {summary}")
    baseline = load_baseline()
    if refresh:
        write_baseline(failed_ids)
        print(f"  已刷新基线：{len(failed_ids)} 条")
        return 0
    new_failures = sorted(failed_ids - baseline)
    fixed = sorted(baseline - failed_ids)
    if fixed:
        print(f"  已修复（不再出现在失败集）：{len(fixed)} 条")
    if new_failures:
        print(f"  !! 新增失败/报错 {len(new_failures)} 条（不在基线中）：")
        for item in new_failures:
            print(f"     - {item}")
        print("  运行 python tools/run_checks.py --refresh-baseline 可在确认后更新基线。")
        return 1
    print(f"  无新增失败（基线 {len(baseline)} 条，当前失败 {len(failed_ids)} 条）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
