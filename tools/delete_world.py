#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 slug 删除世界包 + 所有内部依赖。

用法：
    python tools/delete_world.py --slug zuowang-shanmen-guishi          # dry-run（默认）
    python tools/delete_world.py --slug zuowang-shanmen-guishi --apply  # 真正执行

⚠️ 启动的操作由人执行，本脚本仅提供 dry-run 预览 + apply 提交。
本脚本走 SQLite 直连，不走插件 repo —— 因 plugin 的 worlds repo
没有暴露 world 级 delete 接口（只有 session/delete）。

依赖：仅 stdlib（sqlite3 / json）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(
    "D:/qq-claude-bot/.runtime/astrbot-data/data/plugin_data/"
    "astrbot_plugin_tavern/catalog_v090.sqlite3"
)

# 删除顺序按外键依赖倒序：children 先于 parents。
# 仅删除与目标 world 直接以 world_id 关联的表。
DEPENDENT_TABLES = [
    "world_snapshots",
    "world_rule_revisions",
    "world_feature_versions",
    "world_entity_registry",
    "character_cards",
    "character_card_versions",
    "character_card_drafts",
    "characters",
    "sessions",
    "instance_configs",
    "selected_world_events",
    "character_runtime_states",
    "session_character_states",
    "session_characters",
    "session_rule_states",
    "actor_capability_instances",
    "runtime_effect_instances",
    "timer_instances",
    "configuration_revisions",
    "session_archives",
]


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _find_world(con: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, slug, name, archived, revision FROM worlds WHERE slug = ?",
        (slug,),
    ).fetchone()


def _dep_counts(con: sqlite3.Connection, world_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tbl in DEPENDENT_TABLES:
        try:
            row = con.execute(
                f"SELECT COUNT(*) AS c FROM {tbl} WHERE world_id = ?",
                (world_id,),
            ).fetchone()
            counts[tbl] = int(row["c"])
        except sqlite3.OperationalError:
            counts[tbl] = -1  # 表不存在
    return counts


def cmd_dry_run(slug: str) -> int:
    con = _connect()
    try:
        world = _find_world(con, slug)
        if world is None:
            print(f"DRY: slug='{slug}' 不存在，无需删除。")
            return 0
        deps = _dep_counts(con, world["id"])
        nonzero = {t: n for t, n in deps.items() if n > 0}
        print(f"DRY-RUN —— 准备删除 world_id={world['id']}")
        print(f"  slug           : {world['slug']}")
        print(f"  name           : {world['name']}")
        print(f"  revision       : {world['revision']}")
        print(f"  archived       : {bool(world['archived'])}")
        print()
        print(f"  依赖行计数（按表）:")
        for t, n in deps.items():
            mark = " ←" if n > 0 else ""
            print(f"    {t:<32} {n}{mark}")
        total = sum(n for n in deps.values() if n > 0)
        print(f"  合计依赖行数: {total}")
        print()
        if total == 0:
            print("DRY: 仅删除 worlds 表自身一行。安全。")
        else:
            print("DRY: 将删除 worlds 表自身一行 + 上述依赖行。")
            print("DRY: 若 worlds 行删除触发外键约束 (sessions FK NO ACTION)，")
            print("     请先确认 sessions.world_id = 0（本脚本已统计）。")
        print()
        print("要真正删除，请加 --apply 重跑。")
        return 0
    finally:
        con.close()


def cmd_apply(slug: str) -> int:
    con = _connect()
    try:
        world = _find_world(con, slug)
        if world is None:
            print(f"APPLY: slug='{slug}' 不存在，无事可做。")
            return 0
        world_id = world["id"]
        deps = _dep_counts(con, world_id)
        nonzero = {t: n for t, n in deps.items() if n > 0}
        total = sum(n for n in deps.values() if n > 0)

        print(f"APPLY —— 将删除 world_id={world_id}")
        print(f"  slug           : {world['slug']}")
        print(f"  name           : {world['name']}")
        print(f"  revision       : {world['revision']}")
        print(f"  依赖行数合计   : {total}")
        for t, n in nonzero.items():
            print(f"    - {t}: {n}")
        print()

        try:
            confirm = input("确认删除？(yes/no): ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "yes":
            print("已取消。")
            return 1

        with con:  # 事务
            for tbl in DEPENDENT_TABLES:
                if deps.get(tbl, -1) <= 0:
                    continue
                cur = con.execute(
                    f"DELETE FROM {tbl} WHERE world_id = ?", (world_id,)
                )
                print(f"  DELETE {tbl:<32} {cur.rowcount} rows")
            cur = con.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
            print(f"  DELETE worlds                                {cur.rowcount} rows")

        # 验证
        still = _find_world(con, slug)
        print()
        if still is None:
            print(f"APPLIED: world slug='{slug}' 已从 DB 删除。")
            return 0
        else:
            print(f"ERROR: 删除后仍能查到 world row: {dict(still)}")
            return 2
    finally:
        con.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="按 slug 删除世界包（+依赖）")
    ap.add_argument("--slug", required=True, help="世界包 slug")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    args = ap.parse_args(argv)

    if args.apply:
        return cmd_apply(args.slug)
    return cmd_dry_run(args.slug)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))