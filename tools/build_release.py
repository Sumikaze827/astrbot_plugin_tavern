"""Build deterministic v0.12.0 install and complete-source ZIP archives."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "astrbot_plugin_tavern"
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
INSTALL_EXCLUDED_TOP = {"tests", "tools"}
INSTALL_EXCLUDED_FILES = {"requirements-dev.txt"}


def candidates(*, source: bool) -> list[Path]:
    result: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if EXCLUDED_PARTS.intersection(relative.parts) or path.suffix == ".pyc":
            continue
        if not source and (
            relative.parts[0] in INSTALL_EXCLUDED_TOP
            or relative.as_posix() in INSTALL_EXCLUDED_FILES
        ):
            continue
        result.append(path)
    return sorted(result)


def write_archive(target: Path, *, source: bool) -> int:
    files = candidates(source=source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        target,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{PREFIX}/{relative}")
            info.date_time = (2026, 8, 6, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    install = args.output_dir / "astrbot_plugin_tavern-v0.12.0.zip"
    source = args.output_dir / "astrbot_plugin_tavern-v0.12.0-source.zip"
    install_count = write_archive(install, source=False)
    source_count = write_archive(source, source=True)
    print(f"install={install} files={install_count}")
    print(f"source={source} files={source_count}")


if __name__ == "__main__":
    main()
