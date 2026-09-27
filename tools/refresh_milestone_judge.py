"""补账工具：把当前 session 的 last_milestone_judge_at_turn 推到当前 turn_no。

玩家反馈（2026-08-25）：
未定天门 session_f09cf38f5cb44b51a0b9d0facb0e3cc3 进 ch_05 后，
`last_milestone_judge_at_turn` 卡在 194，每个 commit 都满足
`_cur_turn - 194 >= 4` 但 `_judge_chapter_milestones` 内部抛异常被
外层 except 吞掉，冷却字段永远不刷新——死循环。
补丁修了未来（把冷却字段更新提到 try 块外），但当前 session 的旧值
还在那儿——重启插件也修不了历史数据。
本工具走应用层 `_save_session_rule_state` 路径（不是裸 SQL），
把 `last_milestone_judge_at_turn` 推到当前 turn_no，让下次 commit
按节奏重新触发 judge。

用法：
    cd <plugin_dir>
    python tools/refresh_milestone_judge.py              # dry-run
    python tools/refresh_milestone_judge.py --apply      # 写回
    python tools/refresh_milestone_judge.py --session <sid> --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavern.database import TavernDatabase
from tavern.lifecycle import normalize_progress


def _load(database: TavernDatabase, session_id: str) -> dict:
    rule_state = asyncio.run(
        database.get_session_rule_state(session_id)
    )
    progress = normalize_progress(rule_state.get("progress") or {})
    sess = asyncio.run(database.get_session(session_id))
    return {
        "session_id": session_id,
        "turn_no": int(sess.get("turn_no") or 0),
        "chapter_id": progress.get("current_chapter_id") or "",
        "last_milestone_judge_at_turn": int(
            progress.get("last_milestone_judge_at_turn") or 0
        ),
        "announced_milestones": list(
            progress.get("announced_milestones") or []
        ),
        "revision": int(rule_state.get("revision") or 0),
        "rule_state": rule_state,
    }


def _plan(state: dict) -> dict:
    cur_turn = state["turn_no"]
    old = state["last_milestone_judge_at_turn"]
    if old >= cur_turn:
        return {
            "noop": True,
            "reason": "last_judge >= turn_no，无需刷新",
            "old": old,
        }
    new = cur_turn
    return {
        "noop": False,
        "old": old,
        "new": new,
        "delta": new - old,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session",
        default="session_f09cf38f5cb44b51a0b9d0facb0e3cc3",
        help="目标 session_id（默认未书天当前 session）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写回（默认 dry-run，仅打印 planned diff）",
    )
    parser.add_argument(
        "--data-dir",
        default=(
            "D:/qq-claude-bot/.runtime/astrbot-data/"
            "data/plugin_data/astrbot_plugin_tavern"
        ),
        help="plugin_data 目录（含 catalog_v090.sqlite3）",
    )
    args = parser.parse_args()

    db = TavernDatabase(Path(args.data_dir))
    state = _load(db, args.session)
    print(
        f"session={state['session_id']}\n"
        f"  turn_no={state['turn_no']}\n"
        f"  chapter={state['chapter_id']}\n"
        f"  last_milestone_judge_at_turn={state['last_milestone_judge_at_turn']}\n"
        f"  announced_milestones={len(state['announced_milestones'])}\n"
        f"  revision={state['revision']}"
    )
    plan = _plan(state)
    if plan.get("noop"):
        print(f"NOOP: {plan.get('reason')} (last_judge={plan['old']})")
        return 0
    print(
        f"\nPLANNED:\n"
        f"  last_milestone_judge_at_turn: {plan['old']} → {plan['new']} "
        f"(+{plan['delta']})\n"
        f"  chapter={state['chapter_id']}, turn_no={state['turn_no']}\n"
        f"  下次 commit 会立刻不满足 `>=4` 触发间隔（避免重复触发死循环）；\n"
        f"  **不会**自动判定/落账 ch_05 的 m_05_*——下次正常 commit 走\n"
        f"  `_judge_chapter_milestones` 时按 ledger 现有 clue 重新判定。"
    )
    if not args.apply:
        print("\nDRY-RUN, no writes. Re-run with --apply to commit.")
        return 0

    # 应用层写：构造 _save_session_rule_state 入参
    rs = state["rule_state"]
    progress = dict(rs.get("progress") or {})
    progress["last_milestone_judge_at_turn"] = plan["new"]
    new_rs = asyncio.run(
        db.save_session_rule_state(
            args.session,
            {
                "progress": progress,
                "content_boundaries": dict(rs.get("content_boundaries") or {}),
                "npc_policy": dict(rs.get("npc_policy") or {}),
                "context_budget": dict(rs.get("context_budget") or {}),
                "dice_rules": dict(rs.get("dice_rules") or {}),
                "recovery": dict(rs.get("recovery") or {}),
                "revision": rs.get("revision"),
            },
            actor_id="manual_milestone_judge_refresh",
        )
    )
    print(
        f"\nAPPLIED: revision {state['revision']} → "
        f"{new_rs.get('revision')}, "
        f"last_milestone_judge_at_turn → {plan['new']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
