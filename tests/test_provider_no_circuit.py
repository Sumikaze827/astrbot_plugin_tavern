import tempfile
import unittest
from pathlib import Path

from tavern.database import TavernDatabase


class NoCircuitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = TavernDatabase(Path(self.temp.name))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_repeated_failures_remain_callable(self):
        for _ in range(10):
            row = await self.db.record_provider_result('primary', success=False, reason='timeout')
        self.assertEqual(row['consecutive_failures'], 10)
        self.assertEqual(row['circuit_until'], '')
        self.assertEqual(row['status'], 'healthy')
        self.assertEqual(await self.db.filter_healthy_providers(['primary', 'backup']), ['primary', 'backup'])

    async def test_configuration_failure_is_diagnostic_only(self):
        row = await self.db.record_provider_result('primary', success=False, reason='NotFoundError')
        self.assertTrue(row['last_failure_reason'])
        self.assertEqual(row['circuit_until'], '')
        self.assertEqual(await self.db.filter_healthy_providers(['primary']), ['primary'])
        row = await self.db.record_provider_result('primary', success=True)
        self.assertEqual(row['consecutive_failures'], 0)

    async def test_legacy_open_rows_do_not_block_or_reorder(self):
        await self.db.record_provider_result('primary', success=False, reason='old failure')
        with self.db._connect() as c:
            c.execute("UPDATE provider_health SET status='open', circuit_until='2099-01-01T00:00:00+00:00'")
        self.assertEqual(await self.db.filter_healthy_providers(['primary', 'backup', 'primary', '']), ['primary', 'backup'])
        row = (await self.db.list_provider_health())[0]
        self.assertEqual(row['circuit_until'], '')
        self.assertEqual(row['last_failure_reason'], 'old failure')
