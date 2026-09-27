"""把某一回合已经落库、但没送进群的叙事重新排队投递。

2026-09-23 线上：NapCat 的 sendMsg 超时让 turn 387 的正文两次发送都失败，
群里只剩「回合秩序」。正文本身在 events 里，没丢。这里把它按
notification_outbox 的格式重新入队——下一条群消息到达时 _deliver_pending
会自动重投。

默认 dry-run；--apply 才写。
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sqlite3
import sys
from contextlib import closing

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavern.database import TavernDatabase  # noqa: E402

# Windows 控制台默认 GBK，正文里的 emoji（📖）会直接抛 UnicodeEncodeError。
sys.stdout.reconfigure(errors="replace")

SID = "session_e461df85ee694d298235a963673dd1da"
STORY_HEADER = "📖 【故事推进】"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--data-dir",
        type=pathlib.Path,
        default=ROOT.parents[1] / "plugin_data" / "astrbot_plugin_tavern",
    )
    parser.add_argument("--session", default=SID)
    parser.add_argument(
        "--turn",
        type=int,
        default=0,
        help="要重投哪一回合的叙事；0 = 最新一条旁白",
    )
    args = parser.parse_args()

    catalog = args.data_dir / "catalog_v090.sqlite3"
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        session = c.execute(
            "SELECT * FROM sessions WHERE id=?", (args.session,)
        ).fetchone()
        if not session:
            raise SystemExit("找不到该会话")
        if args.turn:
            event = c.execute(
                "SELECT * FROM events WHERE session_id=? AND role='narrator' "
                "AND turn_no=? ORDER BY seq DESC LIMIT 1",
                (args.session, args.turn),
            ).fetchone()
        else:
            event = c.execute(
                "SELECT * FROM events WHERE session_id=? AND role='narrator' "
                "ORDER BY seq DESC LIMIT 1",
                (args.session,),
            ).fetchone()
        if not event:
            raise SystemExit("找不到对应的旁白事件")
        pending = c.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE session_id=? "
            "AND status='pending'",
            (args.session,),
        ).fetchone()[0]

    origin = str(session["unified_origin"] or "").strip()
    if not origin:
        origin = f"{session['platform_id']}:GroupMessage:{session['group_id']}"
    body = str(event["content"] or "").strip()
    text = f"{STORY_HEADER}\n\n{body}" if body else ""
    if not body:
        raise SystemExit("该事件的正文是空的")
    if STORY_HEADER in body:
        text = body

    print(f"会话：{args.session}")
    print(f"目标来源：{origin}")
    print(f"回合：{event['turn_no']}  事件：{event['id']}  正文 {len(body)} 字")
    print(f"当前待投递条数：{pending}")
    print("--- 将要重投的内容 ---")
    print(text)
    print("----------------------")
    if not args.apply:
        print("dry-run，没有写任何东西。加 --apply 才入队。")
        return

    db = TavernDatabase.__new__(TavernDatabase)
    db.path, db.data_dir = catalog, args.data_dir
    item = db._queue_delivery(
        args.session,
        origin,
        "turn_reply",
        text,
        "平台发送超时导致丢失，人工重投",
        "turn_reply:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:20],
    )
    print(f"已入队：{item['id']}  status={item['status']}")
    print("下一条群消息到达时会自动补发。")


if __name__ == "__main__":
    main()
