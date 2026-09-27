"""0.12.0-A2 回归测试。

覆盖：
1. 世界市场远程拉取（#4）：HTTPS+主机白名单、清单/包拉取、体积上限、
   SHA256 校验、TTL 缓存。
2. 倒计时倒序（#3）：session_timers 的 desc/asc 排序与活跃过滤。
3. 配置解析：world_market 配置组。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tavern.config import TavernConfig
from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.dashboard import session_timers
from tavern.database import TavernDatabase
from tavern.events import EventBroker
from tavern.world_market import (
    DEFAULT_ALLOWED_HOSTS,
    clear_remote_cache,
    fetch_remote_manifest,
    fetch_remote_package,
    validate_remote_url,
)

WORLD = json.dumps(
    {
        "slug": "remote-test",
        "name": "远程测试世界",
        "description": "来自远程清单",
        "world_schema_version": 5,
        "world_content_version": "1.0.0",
        "minimum_plugin_version": "0.11.0",
        "system_prompt": "你是酒馆叙事裁定器。",
        "opening_scene": "测试开场",
        "rules": {
            "resolution": {"mode": "dice_only", "dice_system": "d20"},
            "strict_choices": True,
            "player_limits": {
                "recommended_min": 2,
                "recommended_max": 4,
                "minimum_start": 2,
                "maximum": 4,
            },
        },
        "initial_state": {"location": "", "time": "", "scene_summary": "", "facts": []},
        "protocol": {"core_version": 5, "features": {}},
        "required_features": [],
    },
    ensure_ascii=False,
).encode("utf-8")

WORLD_SHA256 = hashlib.sha256(WORLD).hexdigest()


class _FakeResponse:
    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self._raw = raw

    async def read(self) -> bytes:
        return self._raw

    async def release(self) -> None:
        pass


class _FakeClient:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def get(self, url: str):
        self.calls.append(url)
        raw = self.mapping.get(url)
        if raw is None:
            return _FakeResponse(404, b"not found")
        return _FakeResponse(200, raw)


class V1120RemoteMarketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_remote_cache()

    async def asyncTearDown(self) -> None:
        clear_remote_cache()

    def test_validate_remote_url_enforces_https_and_allowlist(self) -> None:
        # HTTPS + 白名单内 → 通过
        url = validate_remote_url(
            "https://raw.githubusercontent.com/owner/repo/ref/worlds/a.json",
            DEFAULT_ALLOWED_HOSTS,
        )
        self.assertTrue(url.startswith("https://"))
        # HTTP → 拒绝
        with self.assertRaises(ValueError):
            validate_remote_url(
                "http://raw.githubusercontent.com/a/b.json",
                DEFAULT_ALLOWED_HOSTS,
            )
        # 白名单外主机 → 拒绝
        with self.assertRaises(ValueError):
            validate_remote_url(
                "https://evil.example.com/a.json",
                DEFAULT_ALLOWED_HOSTS,
            )
        # 自定义白名单生效
        validate_remote_url(
            "https://cdn.example.com/w.json", ["cdn.example.com"]
        )

    async def test_remote_manifest_happy_path_and_cache(self) -> None:
        manifest = json.dumps(
            [
                {
                    "slug": "remote-test",
                    "name": "远程测试世界",
                    "description": "来自远程清单",
                    "url": (
                        "https://raw.githubusercontent.com/owner/repo/ref/"
                        "worlds/remote-test.json"
                    ),
                    "sha256": WORLD_SHA256,
                    "size": len(WORLD),
                    "world_schema_version": 5,
                }
            ]
        ).encode("utf-8")
        client = _FakeClient(
            {
                "https://raw.githubusercontent.com/owner/repo/ref/worlds/manifest.json": manifest,
                "https://raw.githubusercontent.com/owner/repo/ref/worlds/remote-test.json": WORLD,
            }
        )
        entries = await fetch_remote_manifest(
            client,
            "https://raw.githubusercontent.com/owner/repo/ref/worlds/manifest.json",
            DEFAULT_ALLOWED_HOSTS,
            ttl_seconds=600,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["package_key"], "remote:remote-test")
        self.assertEqual(entries[0]["source"], "remote")
        # TTL 缓存：第二次调用不再请求网络。
        again = await fetch_remote_manifest(
            client,
            "https://raw.githubusercontent.com/owner/repo/ref/worlds/manifest.json",
            DEFAULT_ALLOWED_HOSTS,
            ttl_seconds=600,
        )
        self.assertEqual(again, entries)
        manifest_calls = [
            call for call in client.calls if call.endswith("manifest.json")
        ]
        self.assertEqual(len(manifest_calls), 1)

    async def test_remote_manifest_rejects_non_list_and_oversize(self) -> None:
        bad = _FakeClient(
            {
                "https://raw.githubusercontent.com/a/b/worlds/manifest.json": b'{"a": 1}',
            }
        )
        with self.assertRaises(LookupError):
            await fetch_remote_manifest(
                bad,
                "https://raw.githubusercontent.com/a/b/worlds/manifest.json",
                DEFAULT_ALLOWED_HOSTS,
                max_bytes=10,
            )

    async def test_remote_package_sha256_mismatch_is_rejected(self) -> None:
        client = _FakeClient(
            {
                "https://raw.githubusercontent.com/owner/repo/ref/worlds/remote-test.json": WORLD,
            }
        )
        entry = {
            "url": (
                "https://raw.githubusercontent.com/owner/repo/ref/"
                "worlds/remote-test.json"
            ),
            "sha256": "0" * 64,
        }
        with self.assertRaises(LookupError):
            await fetch_remote_package(
                client,
                entry,
                DEFAULT_ALLOWED_HOSTS,
                verify_sha256=True,
            )

    async def test_remote_package_happy_path(self) -> None:
        client = _FakeClient(
            {
                "https://raw.githubusercontent.com/owner/repo/ref/worlds/remote-test.json": WORLD,
            }
        )
        entry = {
            "url": (
                "https://raw.githubusercontent.com/owner/repo/ref/"
                "worlds/remote-test.json"
            ),
            "sha256": WORLD_SHA256,
        }
        content = await fetch_remote_package(
            client,
            entry,
            DEFAULT_ALLOWED_HOSTS,
            verify_sha256=True,
        )
        self.assertEqual(content["slug"], "remote-test")


class V1120ConfigTests(unittest.TestCase):
    def test_world_market_config_parsing(self) -> None:
        config = TavernConfig.from_mapping(
            {
                "world_market": {
                    "enabled": True,
                    "remote_manifest_url": (
                        "https://raw.githubusercontent.com/o/r/ref/worlds/manifest.json"
                    ),
                    "allowed_hosts": ["raw.githubusercontent.com"],
                    "cache_ttl_seconds": 120,
                    "max_package_bytes": 99999,
                    "verify_sha256": False,
                }
            }
        )
        self.assertTrue(config.world_market_enabled)
        self.assertIn("raw.githubusercontent.com", config.world_market_remote_manifest_url)
        self.assertEqual(config.world_market_allowed_hosts, ("raw.githubusercontent.com",))
        self.assertEqual(config.world_market_cache_ttl_seconds, 120)
        self.assertEqual(config.world_market_max_package_bytes, 99999)
        self.assertFalse(config.world_market_verify_sha256)
        default = TavernConfig.from_mapping({})
        self.assertFalse(default.world_market_enabled)
        self.assertIn("raw.githubusercontent.com", default.world_market_allowed_hosts)


class V1120TimerOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-v1120", "qq:group-v1120", DEFAULT_WORLD_SLUG,
            "admin-1",
        )
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin-1"
        )
        await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin-1"
        )
        # 直接播种两个活跃计时器：turn 先建、vote 后建（created_at 显式相差 2 秒）。
        from datetime import datetime, timedelta, timezone
        from tavern.database_support import new_id

        base = datetime.now(timezone.utc)
        stamps = {
            "turn": base.isoformat(timespec="seconds"),
            "vote": (base + timedelta(seconds=2)).isoformat(timespec="seconds"),
        }
        self._timer_ids: dict[str, str] = {}
        with self.database._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for timer_type in ("turn", "vote"):
                timer_id = new_id("timer")
                self._timer_ids[timer_type] = timer_id
                stamp = stamps[timer_type]
                connection.execute(
                    """
                    INSERT INTO timer_instances(
                        id, session_id, participant_id, timer_type, status,
                        deadline_at, remaining_seconds, action_json,
                        created_at, updated_at
                    ) VALUES (?, ?, '', ?, 'active', ?, 60, '{}', ?, ?)
                    """,
                    (
                        timer_id,
                        self.session["id"],
                        timer_type,
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
            connection.execute("COMMIT")

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_session_timers_desc_puts_newest_first(self) -> None:
        timers = await session_timers(self.database, self.session["id"], order="desc")
        # 只保留活跃/暂停；开演流程可能自带计时器，因此断言顺序而非数量。
        self.assertTrue(all(t["status"] in {"active", "paused"} for t in timers))
        self.assertGreaterEqual(len(timers), 2)
        # desc：最新创建的（vote）排在先建的（turn）之前。
        by_type = {t["timer_type"]: t["id"] for t in timers}
        ids = [t["id"] for t in timers]
        self.assertEqual(self._timer_ids["vote"] in ids, True)
        self.assertLess(
            ids.index(self._timer_ids["vote"]),
            ids.index(self._timer_ids["turn"]),
        )
        # asc：倒过来。
        asc = await session_timers(self.database, self.session["id"], order="asc")
        asc_ids = [t["id"] for t in asc]
        self.assertGreater(
            asc_ids.index(self._timer_ids["vote"]),
            asc_ids.index(self._timer_ids["turn"]),
        )


if __name__ == "__main__":
    unittest.main()
