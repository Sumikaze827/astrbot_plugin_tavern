"""Regression: instance prune must not fail on card_revision_requests FK.

角色卡修改申请（card_revision_requests）会长期保留 base_version_id /
candidate_version_id 指向历史版本；而 _prune_instance 此前只按
participants.character_version_id 保留版本，导致
``DELETE FROM character_card_versions WHERE id NOT IN (...)` 触发
``FOREIGN KEY constraint failed``，进而 bootstrap 失败、插件无法加载。

本测试构造同样的数据，直接调用真实的 InstanceStorage._prune_instance，
验证修复后：
1. 不再抛 sqlite3.IntegrityError；
2. 被 card_revision_requests 引用的版本/角色卡被保留；
3. 真正无引用的孤儿版本仍会被清理。
"""

import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from tavern.storage import InstanceStorage

ROOT = Path(__file__).resolve().parents[1]
DATABASE_PY = ROOT / "tavern" / "database.py"

NEEDED_TABLES = (
    "tavern_meta",
    "worlds",
    "sessions",
    "character_cards",
    "character_card_versions",
    "participants",
    "character_runtime_states",
    "card_revision_requests",
    "group_registry",
    "story_storage",
    "players",
)


def _extract_ddl(source: str, table: str) -> str:
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = source.find(marker)
    if start == -1:
        raise AssertionError(f"DDL for {table} not found")
    index = source.find("(", start) + 1
    depth = 1
    while index < len(source) and depth:
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    end = source.find(";", index)
    block = source[start : end + 1]
    # 去掉每行的公共缩进（全部表定义使用相同的代码缩进层级）
    lines = [line.strip() for line in block.splitlines()]
    return "\n".join(lines)


def _schema_ddl() -> str:
    source = DATABASE_PY.read_text(encoding="utf-8")
    return "\n\n".join(_extract_ddl(source, table) for table in NEEDED_TABLES)


class StorageCardRevisionFkTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._dir = Path(self._temp.name)
        self._db = self._dir / "instance.sqlite3"
        connection = sqlite3.connect(self._db)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_schema_ddl())
            self._seed(connection)
            connection.commit()
        finally:
            connection.close()
        self.storage = InstanceStorage(
            data_dir=self._dir,
            catalog_path=self._dir / "catalog.sqlite3",
            connect_catalog=lambda: sqlite3.connect(":memory:"),
            schema_version=10,
        )


    @staticmethod
    def _insert_row(connection, table: str, **values) -> None:
        """Insert with type-safe defaults for any NOT NULL no-default columns,
        so the fixture keeps working if the schema gains new required columns."""
        columns = {
            row[1]: (row[2], bool(row[3]), row[4])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        data = dict(values)
        for name, (declared_type, notnull, default) in columns.items():
            if name in data:
                continue
            if default is not None:
                continue
            if not notnull:
                continue
            upper = (declared_type or "").upper()
            data[name] = 0 if "INT" in upper else ""
        names = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        connection.execute(
            f'INSERT INTO "{table}"({names}) VALUES ({placeholders})',
            tuple(data.values()),
        )

    def _seed(self, connection: sqlite3.Connection) -> None:
        now = "2026-08-05T00:00:00+00:00"
        self._insert_row(connection, "tavern_meta", key="schema_version", value="10")
        self._insert_row(
            connection, "worlds",
            id="w1", slug="demo-world", display_no=1, name="示例世界",
            description="", system_prompt="", rules_json="{}",
            opening_scene="", initial_state_json="{}", revision=1,
            archived=0, created_at=now, updated_at=now,
        )
        self._insert_row(
            connection, "sessions",
            id="s1", platform_id="qq_official", group_id="100001",
            instance_slug="demo-1", instance_name="示例副本", world_id="w1",
            state="running", turn_no=3, revision=5, selected=1,
            created_at=now, updated_at=now,
        )
        self._insert_row(
            connection, "group_registry",
            id="g1", platform_id="qq_official", group_id="100001",
            created_at=now, updated_at=now,
        )
        self._insert_row(
            connection, "story_storage",
            session_id="s1", group_registry_id="g1",
            relative_path="groups/qq_official_g1/demo-1",
            created_stamp="20260805000000", created_at=now, updated_at=now,
        )
        self._insert_row(
            connection, "character_cards",
            id="card-1", owner_user_id="u1", world_id="w1",
            display_name="阿尔德里克", current_version=2,
            created_at=now, updated_at=now,
        )
        # v1 = 已通过的旧版本（仍被修改申请引用，但不再是参与者当前版本）
        # v2 = 参与者当前版本（同时也是修改申请候选版本）
        # v3 = 完全无引用的孤儿版本（应当被清理）
        for version_id, version_no in (("ver-1", 1), ("ver-2", 2), ("ver-3", 3)):
            self._insert_row(
                connection, "character_card_versions",
                id=version_id, character_card_id="card-1",
                version_no=version_no, profile_json="{}", stats_json="{}",
                status="approved", created_at=now,
            )
        self._insert_row(
            connection, "participants",
            id="p1", session_id="s1", group_user_id="u1",
            display_name="玩家A", character_card_id="card-1",
            character_version_id="ver-2", character_name="阿尔德里克",
            character_code="ALD-01", card_status="approved", ready=1,
            participation_status="active", seat_reserved_at=now,
            created_at=now, updated_at=now,
        )
        self._insert_row(
            connection, "character_runtime_states",
            id="rst-1", session_id="s1", participant_id="p1",
            character_card_id="card-1", created_at=now, updated_at=now,
        )
        # 已审核通过的修改申请：base=ver-1（旧版），candidate=ver-2（新版）
        self._insert_row(
            connection, "card_revision_requests",
            id="req-1", session_id="s1", participant_id="p1",
            character_card_id="card-1", base_version_id="ver-1",
            candidate_version_id="ver-2", status="approved",
            request_note="", review_note="", requested_by="admin",
            reviewed_by="admin", created_at=now, updated_at=now,
        )

    def _indexed(self) -> dict:
        return {
            "session": {
                "id": "s1",
                "world_id": "w1",
                "world_slug": "demo-world",
                "world_name": "示例世界",
                "world_snapshot_json": None,
            },
            "group": {"id": "g1"},
        }

    def _remaining(self, table: str) -> set[str]:
        with closing(sqlite3.connect(self._db)) as connection:
            rows = connection.execute(f'SELECT id FROM "{table}"').fetchall()
        return {str(row[0]) for row in rows}

    def test_prune_keeps_versions_referenced_by_revision_requests(self) -> None:
        InstanceStorage._prune_instance(self.storage, self._db, self._indexed())
        versions = self._remaining("character_card_versions")
        cards = self._remaining("character_cards")
        # 被修改申请引用的 ver-1 / ver-2 必须保留，孤儿 ver-3 被清理
        self.assertEqual(versions, {"ver-1", "ver-2"})
        self.assertEqual(cards, {"card-1"})
        # 外键校验通过
        with closing(sqlite3.connect(self._db)) as connection:
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        self.assertEqual(violations, [])

    def tearDown(self) -> None:
        self._temp.cleanup()


if __name__ == "__main__":
    unittest.main()
