"""Snapshot-based migration test. All writes go to an isolated temporary dir."""
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_npc_direction', ROOT / 'tools' / 'install_royal_npc_direction.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class MigrationTests(unittest.TestCase):
    def test_isolated_migration_preserves_game_and_rejects_stale_revision(self):
        data = ROOT.parents[1] / 'plugin_data' / 'astrbot_plugin_tavern'
        package_dir = ROOT / 'worlds'
        catalog = data / 'catalog_v090.sqlite3'
        if not catalog.exists():
            self.skipTest('Optional local live-snapshot fixture unavailable')
        if json.loads((package_dir / (installer.SLUG + '.json')).read_text(encoding='utf-8'))['rules'].get('npc_direction'):
            backups = sorted((data / 'catalog_backups').glob('npc_direction_*'), reverse=True)
            if not backups:
                self.skipTest('Pre-install fixture unavailable')
            package_dir = backups[0]
            catalog = package_dir / 'catalog.sqlite3'
        with tempfile.TemporaryDirectory(prefix='npc-install-test-') as temp:
            root = Path(temp)
            with closing(sqlite3.connect(catalog.as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(root / 'catalog_v090.sqlite3')) as dst:
                    src.backup(dst)
            for suffix in ('.json', '-npcs.json'):
                shutil.copy2(package_dir / (installer.SLUG + suffix), root / (installer.SLUG + suffix))
            def preserved():
                with closing(sqlite3.connect(root / 'catalog_v090.sqlite3')) as c:
                    return {t: list(c.execute('SELECT * FROM ' + t + ' ORDER BY rowid')) for t in
                        ('events', 'story_ledger', 'session_rule_states', 'session_character_states', 'character_runtime_states', 'participants')}
            before = preserved()
            argv = ['install', '--apply', '--data-dir', str(root), '--package-dir', str(root)]
            with patch.object(sys, 'argv', argv + ['--expected-revision', '0']):
                with self.assertRaises(AssertionError):
                    installer.main()
            with patch.object(sys, 'argv', argv):
                installer.main()
            self.assertEqual(before, preserved())
            with closing(sqlite3.connect(root / 'catalog_v090.sqlite3')) as c:
                self.assertEqual(c.execute('SELECT turn_no,revision FROM sessions WHERE id=?', (installer.SID,)).fetchone(), (226, 249))
                frozen = json.loads(c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (installer.SID,)).fetchone()[0])
                self.assertTrue(frozen['rules']['npc_direction']['enabled'])
                self.assertEqual(json.loads(c.execute("SELECT public_profile_json FROM session_characters WHERE session_id=? AND name='菲鲁特'", (installer.SID,)).fetchone()[0])['personality'], frozen['rules']['npc_direction']['core_profiles']['菲鲁特']['personality'])
            with patch.object(sys, 'argv', argv + ['--expected-revision', '249']):
                with self.assertRaises(AssertionError):
                    installer.main()


if __name__ == '__main__':
    unittest.main()
