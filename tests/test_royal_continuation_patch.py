"""Exercise the one-off migration on an isolated catalog copy, never live data."""
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
spec = importlib.util.spec_from_file_location('royal_patch', ROOT / 'tools' / 'patch_royal_selection_continuation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RoyalContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_package = module.PACKAGE
        cls.fixture_catalog = module.DATA / 'catalog_v090.sqlite3'
        if module.MARKER in json.loads(module.PACKAGE.read_text(encoding='utf-8'))['system_prompt']:
            backups = sorted((module.DATA / 'catalog_backups').glob('royal_continuation_*'), reverse=True)
            for backup in backups:
                if (backup / module.PACKAGE.name).exists() and (backup / 'catalog.sqlite3').exists():
                    cls.fixture_package = backup / module.PACKAGE.name
                    cls.fixture_catalog = backup / 'catalog.sqlite3'
                    break
            else:
                raise unittest.SkipTest('Original migration fixture backup unavailable')

    def test_structure_and_preservation(self):
        old = json.loads(self.fixture_package.read_text(encoding='utf-8'))
        new = module.revised(old)
        self.assertEqual(module.validate(old, new)['new_lint_errors'], [])
        self.assertEqual(new['rules']['progress']['chapters'][:4], old['rules']['progress']['chapters'][:4])
        chapters = new['rules']['progress']['chapters']
        ids = {m['id'] for c in chapters for m in c['milestones']}
        self.assertEqual(len(ids), 25)
        for chapter in chapters:
            self.assertEqual(set(chapter['exits_when']['all_milestones']), {m['id'] for m in chapter['milestones']})
        self.assertNotIn('next_chapter_id', chapters[-1])
        self.assertFalse(chapters[-1]['milestones'][-1].get('ending_milestone', False))
        with self.assertRaises(AssertionError):
            module.revised(new)

    def test_isolated_database_migration(self):
        with tempfile.TemporaryDirectory(prefix='royal-continuation-test-') as folder:
            root = Path(folder)
            catalog = root / 'catalog_v090.sqlite3'
            with closing(sqlite3.connect(self.fixture_catalog.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(catalog)) as destination:
                    source.backup(destination)
            package = root / module.PACKAGE.name
            shutil.copy2(self.fixture_package, package)
            def preserved():
                with closing(sqlite3.connect(catalog)) as c:
                    names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    keep = [t for t in names if t in ('events', 'story_ledger', 'players', 'participants') or 'npc' in t]
                    return {t: list(c.execute('SELECT * FROM "' + t + '" ORDER BY rowid')) for t in keep}
            before = preserved()
            with patch.object(module, 'DATA', root), patch.object(module, 'PACKAGE', package):
                with patch.object(sys, 'argv', ['patch', '--apply', '--expected-revision', '0']):
                    with self.assertRaises(AssertionError):
                        module.main()
                with patch.object(sys, 'argv', ['patch', '--apply']):
                    module.main()
            self.assertEqual(before, preserved())
            with closing(sqlite3.connect(catalog)) as c:
                progress = json.loads(c.execute('SELECT progress_json FROM session_rule_states WHERE session_id=?', (module.SID,)).fetchone()[0])
                self.assertEqual(progress['completed_milestones'], 13)
                self.assertEqual(progress['current_chapter_id'], 'ch_05_chapter')
                self.assertEqual(c.execute('SELECT turn_no,revision FROM sessions WHERE id=?', (module.SID,)).fetchone(), (226, 248))
                world = json.loads(c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (module.SID,)).fetchone()[0])
                self.assertEqual(world['rules']['progress']['chapters'], json.loads(package.read_text(encoding='utf-8'))['rules']['progress']['chapters'])
                self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main()
