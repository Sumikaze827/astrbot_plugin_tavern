"""替换某个玩家角色卡的「自拟设定」文字，不动任何数值。

GM 明确要求：只换设定文本，基础值与主/副属性加点完全保持原样。所以这里只写
profile 里的 supplement，复制原 stats，最后用发布期同一套校验复核：
新 profile 解析出来的最终属性必须与改动前逐项相等，否则拒绝落库。

默认 dry-run；--apply 才写。写之前备份 catalog，写完核对 catalog 与实例快照两处。
刻意不动 participants.ready —— 那一列表示玩家本轮已确认行动，重置会把正在进行的
回合卡住；改设定不影响已经提交的行动。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tavern.card_lifecycle import validate_card_revision  # noqa: E402
from tavern.database import DATABASE_SCHEMA_VERSION, TavernDatabase  # noqa: E402
from tavern.database_support import new_id  # noqa: E402
from tavern.storage import InstanceStorage  # noqa: E402

DEFAULT_SETTING = ROOT / "tools" / "settings" / "duanxian-qinlong.txt"


def dump(value) -> str:
    """Canonical JSON for comparisons (key order must not matter)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT.parents[1] / "plugin_data" / "astrbot_plugin_tavern",
    )
    parser.add_argument("--session", default="session_e461df85ee694d298235a963673dd1da")
    parser.add_argument("--character", default="短线擒龙大王")
    parser.add_argument("--setting-file", type=Path, default=DEFAULT_SETTING)
    parser.add_argument("--actor", default="user_authorized_setting_edit")
    args = parser.parse_args()

    catalog = args.data_dir / "catalog_v090.sqlite3"
    setting = args.setting_file.read_text(encoding="utf-8").strip()
    assert setting, "设定文件是空的"
    assert not any(ord(ch) < 32 and ch not in "\n\t" for ch in setting), (
        "设定里含控制字符"
    )

    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        participant = c.execute(
            "SELECT * FROM participants WHERE session_id=? AND character_name=?",
            (args.session, args.character),
        ).fetchone()
        assert participant, f"找不到 {args.character}"
        version = c.execute(
            "SELECT * FROM character_card_versions WHERE id=?",
            (participant["character_version_id"],),
        ).fetchone()
        assert version and version["status"] == "approved", version
        card = c.execute(
            "SELECT * FROM character_cards WHERE id=?",
            (participant["character_card_id"],),
        ).fetchone()
        frozen = json.loads(
            c.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                (args.session,),
            ).fetchone()[0]
        )
        existing = c.execute(
            "SELECT COUNT(*) FROM card_revision_requests WHERE participant_id=?",
            (participant["id"],),
        ).fetchone()[0]
        session_before = tuple(
            c.execute(
                "SELECT turn_no, revision FROM sessions WHERE id=?", (args.session,)
            ).fetchone()
        )
        ready_before = int(participant["ready"])

    profile = json.loads(version["profile_json"] or "{}")
    stats = json.loads(version["stats_json"] or "{}")
    old_setting = str(profile.get("supplement") or "")

    template = (frozen.get("rules") or {}).get("character_card") or {}
    field = next(
        item for item in template.get("fields") or [] if item.get("key") == "supplement"
    )
    max_chars = int(field.get("max_chars") or 0)
    assert len(setting) <= max_chars, f"设定 {len(setting)} 字超过 {max_chars} 上限"

    updated = dict(profile)
    updated["supplement"] = setting
    resolved = validate_card_revision(frozen, updated, stats)
    if dump(resolved["stats"]["raw"]) != dump(stats.get("raw")):
        raise AssertionError(
            "最终属性会变，超出本次改动范围："
            f"{dump(stats.get('raw'))} -> {dump(resolved['stats']['raw'])}"
        )

    print(f"角色：{args.character}（{participant['character_name']}）")
    print(f"卡：{card['id']} 当前版本 v{version['version_no']}（{version['id']}）")
    print(f"设定字数：{len(old_setting)} → {len(setting)}（上限 {max_chars}）")
    print(f"主属性：{profile.get('primary_attribute')}　副属性：{profile.get('secondary_attribute')}")
    print(f"基础值（不动）：{pretty(profile.get('profession_base_stats'))}")
    print(f"最终值（不动）：{pretty(stats.get('raw'))}")
    print("--- 新设定 ---")
    print(setting)
    print("-------------")
    if not args.apply:
        print("dry-run，没有写任何东西。加 --apply 才落库。")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = args.data_dir / "catalog_backups" / ("card_setting_" + stamp)
    backup.mkdir(parents=True)
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup / "catalog.sqlite3")) as target:
            source.backup(target)
    print("BACKUP", backup, flush=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = TavernDatabase.__new__(TavernDatabase)
    db.path, db.data_dir = catalog, args.data_dir
    new_version_id = new_id("pcardv")
    version_no = int(card["current_version"]) + 1
    request_id = new_id("cardedit")
    with db._connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            assert (
                c.execute(
                    "SELECT character_version_id FROM participants WHERE id=?",
                    (participant["id"],),
                ).fetchone()[0]
                == participant["character_version_id"]
            ), "准备期间这张卡被改过，先重新读一遍"
            assert (
                c.execute(
                    "SELECT current_version FROM character_cards WHERE id=?",
                    (card["id"],),
                ).fetchone()[0]
                == card["current_version"]
            )
            assert (
                c.execute(
                    "SELECT profile_json FROM character_card_versions WHERE id=?",
                    (version["id"],),
                ).fetchone()[0]
                == version["profile_json"]
            )
            c.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?)
                """,
                (
                    new_version_id,
                    card["id"],
                    version_no,
                    int(version["template_version"] or 1),
                    dump(updated),
                    dump(stats),
                    "GM 直接修改自拟设定，数值未变",
                    args.actor,
                    now,
                ),
            )
            c.execute(
                "UPDATE character_cards SET current_version=?, updated_at=? WHERE id=?",
                (version_no, now, card["id"]),
            )
            # ready 保持不动：改设定不该把已经确认的本轮行动作废。
            c.execute(
                """
                UPDATE participants SET character_version_id=?,
                    character_name=?, character_code=?, updated_at=?
                WHERE id=?
                """,
                (
                    new_version_id,
                    str(updated.get("name") or "")[:12],
                    str(updated.get("code") or "")[:12],
                    now,
                    participant["id"],
                ),
            )
            c.execute(
                """
                UPDATE players SET character_name=?, profile_json=?, updated_at=?
                WHERE id=(SELECT player_id FROM participants WHERE id=?)
                """,
                (
                    str(updated.get("name") or "")[:12],
                    dump(updated),
                    now,
                    participant["id"],
                ),
            )
            c.execute(
                """
                INSERT INTO card_revision_requests(
                    id, session_id, participant_id, character_card_id,
                    base_version_id, candidate_version_id, status, request_note,
                    review_note, requested_by, reviewed_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    args.session,
                    participant["id"],
                    card["id"],
                    version["id"],
                    new_version_id,
                    "GM 直接修改自拟设定",
                    "设定文本替换，基础值与主副加点未变",
                    args.actor,
                    args.actor,
                    now,
                    now,
                ),
            )
            db._insert_audit(
                c,
                args.session,
                args.actor,
                "card.setting.update",
                card["id"],
                {
                    "backup": str(backup),
                    "character": args.character,
                    "from_version": version["id"],
                    "to_version": new_version_id,
                    "old_chars": len(old_setting),
                    "new_chars": len(setting),
                    "stats_unchanged": stats.get("raw"),
                    "ready_untouched": True,
                    "setting_file": str(args.setting_file),
                },
            )
            c.commit()
        except BaseException:
            c.execute("ROLLBACK")
            raise

    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        live = c.execute(
            "SELECT profile_json, stats_json, status FROM character_card_versions WHERE id=?",
            (new_version_id,),
        ).fetchone()
        assert live["status"] == "approved"
        stored_profile = json.loads(live["profile_json"])
        assert stored_profile["supplement"] == setting
        assert dump(json.loads(live["stats_json"])) == dump(stats)
        assert (
            c.execute(
                "SELECT character_version_id FROM participants WHERE id=?",
                (participant["id"],),
            ).fetchone()[0]
            == new_version_id
        )
        assert (
            c.execute(
                "SELECT current_version FROM character_cards WHERE id=?", (card["id"],)
            ).fetchone()[0]
            == version_no
        )
        assert (
            c.execute(
                "SELECT ready FROM participants WHERE id=?", (participant["id"],)
            ).fetchone()[0]
            == ready_before
        ), "ready 被改动了"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM card_revision_requests WHERE participant_id=?",
                (participant["id"],),
            ).fetchone()[0]
            == existing + 1
        )
        turn = tuple(
            c.execute(
                "SELECT turn_no, revision FROM sessions WHERE id=?", (args.session,)
            ).fetchone()
        )
        assert turn == session_before, (
            f"落库期间这一局前进了：{session_before} -> {turn}"
        )

    # catalog 是主库，运行中的剧本读的是实例库快照；把这次改动投影过去。
    storage = InstanceStorage(
        data_dir=args.data_dir,
        catalog_path=catalog,
        connect_catalog=db._connect,
        schema_version=DATABASE_SCHEMA_VERSION,
    )
    storage.sync_session(args.session)

    instance = (
        args.data_dir
        / "groups"
        / "onebot_g_d6fe6b4cb1b5a9df"
        / "stories"
        / "re0-ch-chapter030_20260917010029_i-e461df85"
        / "instance.sqlite3"
    )
    with closing(sqlite3.connect(instance.as_uri() + "?mode=ro", uri=True)) as c:
        row = c.execute(
            "SELECT profile_json FROM character_card_versions WHERE id=?",
            (new_version_id,),
        ).fetchone()
        if row is None:
            raise AssertionError("实例库没同步到新版本")
        assert json.loads(row[0])["supplement"] == setting
        assert (
            c.execute(
                "SELECT character_version_id FROM participants WHERE id=?",
                (participant["id"],),
            ).fetchone()[0]
            == new_version_id
        )
        assert (
            c.execute(
                "SELECT ready FROM participants WHERE id=?", (participant["id"],)
            ).fetchone()[0]
            == ready_before
        )
    print(f"已写入 v{version_no}（{new_version_id}）并核对一致")
    print(f"回合 {turn[0]}、会话版本 {turn[1]}、ready 均未改动")


if __name__ == "__main__":
    main()
