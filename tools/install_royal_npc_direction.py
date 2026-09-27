"""Install reviewed NPC canon/source bindings, never advance the live story.

Default is a dry-run. --apply is a guarded transaction plus recoverable file
projections. --data-dir/--package-dir support isolated migration tests.
"""
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
from tavern.database import TavernDatabase, DATABASE_SCHEMA_VERSION
from tavern.storage import InstanceStorage
from tavern.npc_direction import source_context
from tavern.worldgen.lint import lint_world_package

SID = 'session_e461df85ee694d298235a963673dd1da'
WID = 'world_831bd58f83de477f9c943cef3d4a03a9'
SLUG = 're0-ch-chapter030'


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def configure(world, policy):
    updated = copy.deepcopy(world)
    assert not updated['rules'].get('npc_direction'), 'Already installed; do not reapply blindly'
    updated['rules']['npc_direction'] = policy
    assert updated['rules']['progress'] == world['rules']['progress']
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=ROOT.parents[1] / 'plugin_data' / 'astrbot_plugin_tavern')
    parser.add_argument('--package-dir', type=Path, default=ROOT / 'worlds')
    parser.add_argument('--expected-turn', type=int, default=226)
    parser.add_argument('--expected-revision', type=int, default=248)
    args = parser.parse_args()
    path = args.data_dir / 'catalog_v090.sqlite3'
    policy = json.loads(Path(__file__).with_name('royal_npc_policy.json').read_text(encoding='utf-8'))
    sources = json.loads(Path(__file__).with_name('royal_npc_sources.json').read_text(encoding='utf-8'))
    package = args.package_dir / (SLUG + '.json')
    npc_file = args.package_dir / (SLUG + '-npcs.json')
    registry_file = args.data_dir / 'npc_source_registry.json'
    file_before = {p: p.read_bytes() if p.exists() else None for p in (package, npc_file, registry_file)}
    output = configure(json.loads(file_before[package]), policy)
    assert lint_world_package(output)['ok'], lint_world_package(output)
    npcs_output = json.loads(file_before[npc_file])
    npc_list = npcs_output if isinstance(npcs_output, list) else npcs_output.get('items', npcs_output.get('characters', npcs_output.get('npcs', [])))
    assert isinstance(npc_list, list) and npc_list, 'Unrecognized NPC package format'
    for npc in npc_list:
        correction = policy['core_profiles'].get(npc.get('name'))
        if correction:
            npc.setdefault('profile', {}).update(correction)
            if correction.get('private_direction'):
                npc['prompt'] = correction['private_direction']
    registry = json.loads(file_before[registry_file]) if file_before[registry_file] else {}
    registry[SLUG] = sources
    # Validate every authored span before touching the live DB or files.
    for chapter in sources['chapters'].values():
        for span in chapter['spans']:
            f = (Path(sources['root']) / span['file']).resolve(strict=True)
            assert f.is_relative_to(Path(sources['root']).resolve())
            lines = f.read_text(encoding='utf-8').splitlines()
            assert 0 < span['start'] <= span['end'] <= len(lines)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        s = dict(c.execute('SELECT * FROM sessions WHERE id=?', (SID,)).fetchone())
        assert s['world_id'] == WID and s['state'] == 'running'
        assert (s['turn_no'], s['revision']) == (args.expected_turn, args.expected_revision), (s['turn_no'], s['revision'])
        frozen_text = c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (SID,)).fetchone()[0]
        frozen = configure(json.loads(frozen_text), policy)
        row = c.execute('SELECT * FROM worlds WHERE id=?', (WID,)).fetchone()
        old_revision = row['revision']
        world = configure(TavernDatabase._world(row), policy)
        profiles = [dict(r) for r in c.execute('SELECT * FROM characters WHERE world_id=?', (WID,))]
        live_npcs = [dict(r) for r in c.execute('SELECT * FROM session_characters WHERE session_id=?', (SID,))]
    print('Validated: canon corrections, scoped source references, exact hall mappings; no progress changes.')
    if not args.apply:
        return
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    backup = args.data_dir / 'catalog_backups' / ('npc_direction_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup / 'catalog.sqlite3')) as target:
            source.backup(target)
    for p, raw in file_before.items():
        if raw is not None:
            shutil.copy2(p, backup / p.name)
    print('BACKUP', backup, flush=True)
    db = TavernDatabase.__new__(TavernDatabase)
    db.path, db.data_dir = path, args.data_dir
    with db._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            assert tuple(c.execute('SELECT turn_no,revision FROM sessions WHERE id=?', (SID,)).fetchone()) == (args.expected_turn, args.expected_revision)
            assert c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (SID,)).fetchone()[0] == frozen_text
            assert c.execute('SELECT revision FROM worlds WHERE id=?', (WID,)).fetchone()[0] == old_revision
            assert not c.execute("SELECT 1 FROM group_votes WHERE session_id=? AND status='open'", (SID,)).fetchone()
            for p, raw in file_before.items():
                assert (p.read_bytes() if p.exists() else None) == raw, 'File changed during preparation'
            revision = old_revision + 1
            world['revision'] = frozen['revision'] = revision
            c.execute('UPDATE worlds SET rules_json=?,revision=?,updated_at=? WHERE id=?', (dump(world['rules']), revision, now, WID))
            c.execute('UPDATE instance_configs SET world_snapshot_json=?,world_revision=?,updated_at=? WHERE session_id=?', (dump(frozen), revision, now, SID))
            for row in profiles:
                correction = policy['core_profiles'].get(row['name'])
                if not correction:
                    continue
                assert c.execute('SELECT revision FROM characters WHERE id=?', (row['id'],)).fetchone()[0] == row['revision']
                profile = json.loads(row['profile_json']); profile.update(correction)
                c.execute('UPDATE characters SET profile_json=?,prompt=?,revision=revision+1,updated_at=? WHERE id=?',
                    (dump(profile), correction.get('private_direction', row['prompt']), now, row['id']))
            for row in live_npcs:
                correction = policy['core_profiles'].get(row['name'])
                if not correction:
                    continue
                assert c.execute('SELECT revision FROM session_characters WHERE id=?', (row['id'],)).fetchone()[0] == row['revision']
                profile = json.loads(row['public_profile_json']); profile.update(correction)
                c.execute('UPDATE session_characters SET public_profile_json=?,revision=revision+1,updated_at=? WHERE id=?', (dump(profile), now, row['id']))
            db._persist_world_revision(c, c.execute('SELECT * FROM worlds WHERE id=?', (WID,)).fetchone(), world, now)
            c.execute('UPDATE sessions SET revision=revision+1,updated_at=? WHERE id=?', (now, SID))
            c.execute("UPDATE choice_sets SET status='superseded',updated_at=? WHERE session_id=? AND status='active'", (now, SID))
            c.execute("UPDATE story_storage SET sync_status='pending',updated_at=? WHERE session_id=?", (now, SID))
            c.execute('INSERT INTO audit_logs(session_id,actor_id,action,target,detail_json,created_at) VALUES (?,?,?,?,?,?)',
                (SID, 'user_authorized_npc_direction', 'npc.direction.install', WID,
                 dump({'backup': str(backup), 'turn': s['turn_no'], 'profiles': list(policy['core_profiles']), 'progress_unchanged': True}), now))
            c.commit()
        except BaseException:
            c.rollback()
            raise
    InstanceStorage._atomic_json(package, output)
    InstanceStorage._atomic_json(npc_file, npcs_output)
    InstanceStorage._atomic_json(registry_file, registry)
    storage = InstanceStorage(data_dir=args.data_dir, catalog_path=path, connect_catalog=db._connect, schema_version=DATABASE_SCHEMA_VERSION)
    storage.sync_session(SID)
    for chapter in sources['chapters']:
        assert source_context(args.data_dir, SLUG, chapter, '')['status'] == 'found'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as c:
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert c.execute('SELECT turn_no FROM sessions WHERE id=?', (SID,)).fetchone()[0] == args.expected_turn
    print('Verified. Code reload required; no process restarted; story progress and runtime locations untouched.')


if __name__ == '__main__':
    main()
