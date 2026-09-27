"""把 re0 王选篇的角色卡从「预设职业 + 50 点基础」改成「自拟设定 → 生成基础值」。

只改世界包里的 character_card：职业字段删掉，自拟设定（原 supplement）挪到姓名/
代号之后并改为必填，基础值由模型按设定生成；**主/副属性 +7/+3 仍然由玩家自己选**，
所以 stats 里除了 mode 与 stat_generation 之外的东西一律不动。

刻意不碰的东西：会话里正在等玩家选择的 choice_sets、group_votes、sessions.revision
（这不是剧情改动，改这些会把正在进行的场景掀掉）。世界 revision 会 +1，因为它就是
世界配置的版本号，能力授权用它做幂等键。

默认 dry-run；--apply 才写。写之前先备份目录，写之后逐张验证现有角色卡数值不变。
"""
from __future__ import annotations

import argparse
import copy
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
from tavern.lifecycle import card_template  # noqa: E402
from tavern.stat_generation import (  # noqa: E402
    AUTHORED_MODE,
    authored_stat_config,
    uses_authored_stats,
    validate_authored_stat_config,
)
from tavern.storage import InstanceStorage  # noqa: E402
from tavern.worldgen.lint import lint_world_package  # noqa: E402

SID = "session_e461df85ee694d298235a963673dd1da"
WID = "world_831bd58f83de477f9c943cef3d4a03a9"
SLUG = "re0-ch-chapter030"

SOURCE_FIELD = "supplement"
SOURCE_LABEL = "自拟设定（写清你的背景与能力，系统据此生成基础属性）"
GUIDE = (
    "按设定的具体程度给分：明确写出的专长给对应属性高分，没写到的属性不要高于中位；"
    "设定与属性含义冲突时以属性含义为准；设定里超出本世界规则的能力照写，但不额外加分。"
)
# 7 项属性总和 50，主属性还要 +7，而模板上限是 20 —— 基础值必须封在 13 以内，
# 否则主属性那一项会算出 21 直接越界。
MIN_PER_STAT = 2
MAX_PER_STAT = 13

FIELD_ORDER = ["name", "code", SOURCE_FIELD, "primary_attribute", "secondary_attribute"]


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def convert_card(card: dict) -> dict:
    """返回改造后的 character_card（不改原对象）。"""
    updated = copy.deepcopy(card)
    fields = [dict(item) for item in updated["fields"]]
    by_key = {str(item.get("key") or ""): item for item in fields}
    assert "profession" in by_key, "这张卡已经不是预设职业形态了"
    assert SOURCE_FIELD in by_key, f"找不到自拟设定字段 {SOURCE_FIELD}"
    assert not by_key[SOURCE_FIELD].get("required"), "自拟设定应当还是可留空的旧形态"

    source = by_key[SOURCE_FIELD]
    source.update(
        {
            "label": SOURCE_LABEL,
            "type": "text",
            "required": True,
            "max_chars": 600,
            "private": False,
        }
    )
    source.pop("clear_on_change", None)
    fields = [item for item in fields if str(item.get("key") or "") != "profession"]
    fields.sort(key=lambda item: FIELD_ORDER.index(str(item.get("key") or "")))
    assert [str(item["key"]) for item in fields] == FIELD_ORDER

    stats = copy.deepcopy(updated["stats"])
    assert stats.get("mode") == "preset", stats.get("mode")
    assert stats.get("base_budget") == 50 and stats.get("budget") == 60
    stats["mode"] = AUTHORED_MODE
    # 这两个键参与 uses_profession_preset_stats 的判定，必须一起改掉，
    # 否则 authored 的卡还会被当成预设职业卡。
    stats["allocation_mode"] = "authored_base_plus_primary7_secondary3"
    stats["input_mode"] = "authored_base_plus_two_fixed_bonus_choices"
    stats["calculation_formula"] = (
        "effective_attribute = generated.base_attribute + 7 if primary + 3 if secondary"
    )
    stats["stat_generation"] = {
        "mode": AUTHORED_MODE,
        "source_field": SOURCE_FIELD,
        "expected_total": 50,
        "min_per_stat": MIN_PER_STAT,
        "max_per_stat": MAX_PER_STAT,
        "guide": GUIDE,
    }
    # profession_presets 留着：只作数据，职业字段已经没了，不会再被选到；
    # 万一有旧卡缺 profession_base_stats，还能按老的职业路径解析出来。
    updated["stats"] = stats
    updated["fields"] = fields
    updated["version"] = int(updated.get("version", 1)) + 1
    return updated


def retype_source_field(card: dict) -> dict:
    """把自拟设定从单行 text 改成多行 textarea。

    单行 text 在填卡时会拒绝任何空白字符（含换行），玩家根本没法把一段分行列的
    设定写进去；textarea 是模板与归一化层都认的多行类型。
    """
    updated = copy.deepcopy(card)
    found = False
    for item in updated["fields"]:
        if str(item.get("key") or "") != SOURCE_FIELD:
            continue
        assert str(item.get("type") or "text") == "text", item.get("type")
        item["type"] = "textarea"
        found = True
    assert found, f"找不到自拟设定字段 {SOURCE_FIELD}"
    updated["version"] = int(updated.get("version", 1)) + 1
    return updated


def convert_world(world: dict, *, retype: bool = False) -> dict:
    updated = copy.deepcopy(world)
    rules = updated.get("rules")
    assert isinstance(rules, dict) and "character_card" in rules
    rules["character_card"] = (
        retype_source_field(rules["character_card"])
        if retype
        else convert_card(rules["character_card"])
    )
    return updated


def check_new_contract(frozen: dict, cards: list[dict]) -> list[str]:
    """新模板必须自洽，而且现有角色卡的数值一个都不能变。"""
    notes: list[str] = []
    template = card_template(frozen)
    assert uses_authored_stats(template)
    config = validate_authored_stat_config(template)
    notes.append(
        "authored 体检通过：source_field={} expected_total={} 可达区间={}-{}".format(
            config["source_field"],
            config["expected_total"],
            config["floor"],
            config["ceiling"],
        )
    )
    assert authored_stat_config(template)["source_field"] == SOURCE_FIELD
    keys = [str(item["key"]) for item in template["fields"]]
    assert keys == FIELD_ORDER, keys
    assert "profession" not in keys
    source_field = next(
        item for item in template["fields"] if str(item["key"]) == SOURCE_FIELD
    )
    assert source_field.get("type") == "textarea", source_field.get("type")
    assert not [
        key
        for key in template["fields"]
        if key.get("required") and key.get("key") == "profession"
    ]
    for card in cards:
        profile = card["profile"]
        stats = card["stats"]
        assert profile.get("profession_base_stats"), card["id"]
        resolved = validate_card_revision(frozen, profile, stats)
        if dict(resolved["stats"].get("raw") or {}) != dict(stats.get("raw") or {}):
            raise AssertionError(
                f"{card['name']} 的最终属性会被改："
                f"{stats.get('raw')} -> {resolved['stats'].get('raw')}"
            )
        notes.append(
            f"{card['name']}：最终属性不变 {dump(resolved['stats']['raw'])}"
        )
    return notes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只读复查三处角色卡是否已经一致（改完之后用这个）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT.parents[1] / "plugin_data" / "astrbot_plugin_tavern",
    )
    parser.add_argument("--package-dir", type=Path, default=ROOT / "worlds")
    parser.add_argument("--expected-turn", type=int, default=309)
    parser.add_argument("--expected-session-revision", type=int, default=339)
    parser.add_argument("--expected-world-revision", type=int, default=3)
    parser.add_argument("--expected-active-choices", type=int, default=1)
    parser.add_argument(
        "--retype-multiline",
        action="store_true",
        help="已切到 authored 之后，把自拟设定从单行 text 改成多行 textarea",
    )
    args = parser.parse_args()

    if args.verify_only:
        verify_only(args)
        return

    catalog = args.data_dir / "catalog_v090.sqlite3"
    package = args.package_dir / (SLUG + ".json")

    package_before = package.read_bytes()
    world = convert_world(json.loads(package_before), retype=args.retype_multiline)
    report = lint_world_package(world)
    assert report["ok"], report

    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        session = dict(c.execute("SELECT * FROM sessions WHERE id=?", (SID,)).fetchone())
        assert session["world_id"] == WID and session["state"] == "running"
        assert (session["turn_no"], session["revision"]) == (
            args.expected_turn,
            args.expected_session_revision,
        ), (session["turn_no"], session["revision"])
        snapshot_before = c.execute(
            "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
            (SID,),
        ).fetchone()[0]
        world_row = c.execute("SELECT * FROM worlds WHERE id=?", (WID,)).fetchone()
        assert world_row["revision"] == args.expected_world_revision, world_row["revision"]
        assert world_row["slug"] == SLUG
        catalog_world = TavernDatabase._world(world_row)
        cards = []
        for row in c.execute(
            """
            SELECT cc.id, cc.display_name, v.profile_json, v.stats_json
            FROM character_cards cc
            JOIN character_card_versions v
              ON v.character_card_id = cc.id AND v.version_no = cc.current_version
            WHERE cc.world_id = ?
            """,
            (WID,),
        ):
            cards.append(
                {
                    "id": row["id"],
                    "name": row["display_name"],
                    "profile": json.loads(row["profile_json"] or "{}"),
                    "stats": json.loads(row["stats_json"] or "{}"),
                }
            )
        open_choices = [
            dict(r)
            for r in c.execute(
                "SELECT id,status FROM choice_sets WHERE session_id=? AND status='active'",
                (SID,),
            )
        ]

    frozen_before = json.loads(snapshot_before)
    assert frozen_before["revision"] == args.expected_world_revision
    assert dump(frozen_before["rules"]["character_card"]) == dump(
        catalog_world["rules"]["character_card"]
    ), "世界包文件与快照里的角色卡本来就不一致，先查清楚再改"

    frozen = convert_world(frozen_before, retype=args.retype_multiline)
    notes = check_new_contract(frozen, cards)
    for note in notes:
        print("OK  " + note)
    print(f"现有角色卡 {len(cards)} 张全部数值不变。")
    print(f"保持原样（不改）：活跃选项 {len(open_choices)} 组、sessions.revision、投票。")
    if args.retype_multiline:
        print("自拟设定改为多行 textarea：玩家可以换行、可以带空格。")
    else:
        print("新流程：姓名 → 代号 → 自拟设定 → 生成基础值 → 选主属性(+7) → 选副属性(+3)")
    if not args.apply:
        print("dry-run，没有写任何东西。加 --apply 才落库。")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = args.data_dir / "catalog_backups" / ("authored_card_stats_" + stamp)
    backup.mkdir(parents=True)
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup / "catalog.sqlite3")) as target:
            source.backup(target)
    shutil.copy2(package, backup / package.name)
    print("BACKUP", backup, flush=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = TavernDatabase.__new__(TavernDatabase)
    db.path, db.data_dir = catalog, args.data_dir
    revision = args.expected_world_revision + 1
    world["revision"] = frozen["revision"] = revision
    with db._connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            assert (
                tuple(
                    c.execute(
                        "SELECT turn_no,revision FROM sessions WHERE id=?", (SID,)
                    ).fetchone()
                    or ()
                )
                == (args.expected_turn, args.expected_session_revision)
            ), "会话回合/版本在准备期间变了，先重新读一遍再改"
            assert (
                c.execute(
                    "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                    (SID,),
                ).fetchone()[0]
                == snapshot_before
            )
            assert (
                c.execute("SELECT revision FROM worlds WHERE id=?", (WID,)).fetchone()[0]
                == args.expected_world_revision
            )
            assert package.read_bytes() == package_before, "准备期间世界包文件被改过"
            c.execute(
                "UPDATE worlds SET rules_json=?,revision=?,updated_at=? WHERE id=?",
                (dump(world["rules"]), revision, now, WID),
            )
            c.execute(
                "UPDATE instance_configs SET world_snapshot_json=?,world_revision=?,updated_at=? WHERE session_id=?",
                (dump(frozen), revision, now, SID),
            )
            db._persist_world_revision(
                c, c.execute("SELECT * FROM worlds WHERE id=?", (WID,)).fetchone(), world, now
            )
            c.execute(
                "INSERT INTO audit_logs(session_id,actor_id,action,target,detail_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    SID,
                    "user_authorized_authored_card",
                    "card.stats_mode.install",
                    WID,
                    dump(
                        {
                            "backup": str(backup),
                            "change": (
                                "source_field.type: text -> textarea"
                                if args.retype_multiline
                                else f"stats.mode: preset -> {AUTHORED_MODE}"
                            ),
                            "source_field": SOURCE_FIELD,
                            "field_order": FIELD_ORDER,
                            "cards_unchanged": [card["name"] for card in cards],
                            "choice_sets_untouched": [r["id"] for r in open_choices],
                        }
                    ),
                    now,
                ),
            )
            c.commit()
        except BaseException:
            c.rollback()
            raise

    InstanceStorage._atomic_json(package, world)
    storage = InstanceStorage(
        data_dir=args.data_dir,
        catalog_path=catalog,
        connect_catalog=db._connect,
        schema_version=DATABASE_SCHEMA_VERSION,
    )
    storage.sync_session(SID)

    # 落库后重新读一遍：快照、实例、世界包文件三处必须一致。
    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            c.execute("SELECT turn_no FROM sessions WHERE id=?", (SID,)).fetchone()[0]
            == args.expected_turn
        ), "回合数不该变"
        assert c.execute(
            "SELECT COUNT(*) FROM choice_sets WHERE session_id=? AND status='active'",
            (SID,),
        ).fetchone()[0] == len(open_choices)
        stored = json.loads(
            c.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                (SID,),
            ).fetchone()[0]
        )
    # 四处比对必须跟键的顺序无关：不同写入路径会重排 key，值一样才算一致。
    def canonical(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    assert canonical(stored["rules"]["character_card"]) == canonical(
        world["rules"]["character_card"]
    )
    assert canonical(
        json.loads(package.read_text(encoding="utf-8"))["rules"]["character_card"]
    ) == canonical(world["rules"]["character_card"])
    with closing(
        sqlite3.connect(
            (args.data_dir / "groups" / "onebot_g_d6fe6b4cb1b5a9df" / "stories" /
             "re0-ch-chapter030_20260917010029_i-e461df85" / "instance.sqlite3").as_uri()
            + "?mode=ro",
            uri=True,
        )
    ) as c:
        instance_card = json.loads(
            c.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                (SID,),
            ).fetchone()[0]
        )["rules"]["character_card"]
    assert canonical(instance_card) == canonical(world["rules"]["character_card"])
    print("已写入并核对一致：世界包文件 / catalog / 实例快照")
    print("需要重载插件后才生效（进程没重启，剧情与选项原样保留）。")


def verify_only(args) -> None:
    """只读复查：世界包文件、catalog、实例快照三处的角色卡是否已经一致。"""
    catalog = args.data_dir / "catalog_v090.sqlite3"
    package = args.package_dir / (SLUG + ".json")

    def canonical(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        snapshot = json.loads(
            c.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                (SID,),
            ).fetchone()[0]
        )
        world_rules = json.loads(
            c.execute("SELECT rules_json FROM worlds WHERE id=?", (WID,)).fetchone()[0]
        )
        session = dict(c.execute("SELECT * FROM sessions WHERE id=?", (SID,)).fetchone())
        world_revision = c.execute(
            "SELECT revision FROM worlds WHERE id=?", (WID,)
        ).fetchone()[0]
        instance_revision = c.execute(
            "SELECT world_revision FROM instance_configs WHERE session_id=?", (SID,)
        ).fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM choice_sets WHERE session_id=? AND status='active'",
            (SID,),
        ).fetchone()[0]
        assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    instance_path = (
        args.data_dir / "groups" / "onebot_g_d6fe6b4cb1b5a9df" / "stories"
        / "re0-ch-chapter030_20260917010029_i-e461df85" / "instance.sqlite3"
    )
    with closing(
        sqlite3.connect(instance_path.as_uri() + "?mode=ro", uri=True)
    ) as c:
        instance_card = json.loads(
            c.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                (SID,),
            ).fetchone()[0]
        )["rules"]["character_card"]
        instance_session = tuple(
            c.execute("SELECT turn_no,revision FROM sessions WHERE id=?", (SID,)).fetchone()
        )
    frozen = {"rules": world_rules}
    card = frozen["rules"]["character_card"]
    template = card_template(frozen)
    config = validate_authored_stat_config(template)
    package_card = json.loads(package.read_text(encoding="utf-8"))["rules"][
        "character_card"
    ]
    assert canonical(snapshot["rules"]["character_card"]) == canonical(card), "快照不一致"
    assert canonical(package_card) == canonical(card), "世界包文件不一致"
    assert canonical(instance_card) == canonical(card), "实例快照不一致"
    assert uses_authored_stats(template) and config["source_field"] == SOURCE_FIELD
    assert [str(item["key"]) for item in template["fields"]] == FIELD_ORDER
    assert (
        next(
            item for item in template["fields"] if str(item["key"]) == SOURCE_FIELD
        ).get("type")
        == "textarea"
    ), "自拟设定还是单行 text，玩家写不进带换行的设定"
    assert (session["turn_no"], session["revision"]) == (
        args.expected_turn,
        args.expected_session_revision,
    ), "回合/版本变了"
    assert instance_session == (args.expected_turn, args.expected_session_revision)
    assert world_revision == instance_revision == args.expected_world_revision + 1
    assert active == args.expected_active_choices
    print("复查通过：三处角色卡一致，mode=authored，字段顺序 " + " → ".join(FIELD_ORDER))
    print(
        f"世界版本 {world_revision}；回合 {session['turn_no']}、会话版本 "
        f"{session['revision']}、活跃选项 {active} 组——都与改动前一致"
    )


if __name__ == "__main__":
    main()
