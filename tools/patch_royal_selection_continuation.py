"""Guarded, backed-up live continuation edit. Default is read-only validation."""
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
from tavern.worldgen.lint import lint_world_package
from tavern.world_preflight import inspect_world_package

DATA = ROOT.parents[1] / 'plugin_data' / 'astrbot_plugin_tavern'
SID = 'session_e461df85ee694d298235a963673dd1da'
WID = 'world_831bd58f83de477f9c943cef3d4a03a9'
PACKAGE = ROOT / 'worlds' / 're0-ch-chapter030.json'
PATCH = Path(__file__).with_name('royal_selection_continuation.json')
MARKER = '【王选篇后半程校订】'


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def revised(world):
    result = copy.deepcopy(world)
    patch = json.loads(PATCH.read_text(encoding='utf-8'))
    assert MARKER not in result['system_prompt'], 'Already patched; do not apply twice'
    result['system_prompt'] += '\n\n' + patch['system_addendum']
    old = result['rules']['progress']['chapters']
    assert [c['id'] for c in old] == [f'ch_{n:02d}_chapter' for n in range(1, 9)]
    tail = copy.deepcopy(patch['chapters'])
    for number, chapter in enumerate(tail, 5):
        for index, milestone in enumerate(chapter['milestones'], 1):
            if milestone['id'] != 'm_05_01_chapter':
                milestone['id'] = f'm_{number:02d}_{index:02d}_' + milestone['id'][5:]
        chapter['exits_when'] = {'all_milestones': [m['id'] for m in chapter['milestones']]}
        if number < 8:
            chapter['next_chapter_id'] = f'ch_{number+1:02d}_chapter'
    # Identity/qualification was already discussed in this run. Do not demand
    # that players replay an exact three-part interrogation to leave the room.
    tail[0]['milestones'][0]['label'] = '玩家在王选会场的身份与立场已公开，并已得到骑士团或贤人会的明确回应；已发生的自称骑士、随员发言及处理结果直接计入，不要求重新受审、逐条回答忠诚力量觉悟、认错或惨败。'
    result['rules']['progress']['chapters'] = old[:4] + tail
    result['rules']['progress']['total_milestones'] = sum(len(c['milestones']) for c in old[:4] + tail)
    result['rules']['progress']['design_note'] = '本局续篇校订：前四章历史保留；第五至八章按无死亡回归单线因果重写。8章25里程碑，最后结果触发现有整体收尾。'
    return result


def validate(original, updated):
    assert original['rules']['progress']['chapters'][:4] == updated['rules']['progress']['chapters'][:4]
    assert original['rules']['character_card'] == updated['rules']['character_card']
    assert original['opening_scene'] == updated['opening_scene']
    chapters = updated['rules']['progress']['chapters']
    assert sum(len(c['milestones']) for c in chapters) == 25
    assert 'next_chapter_id' not in chapters[-1]
    # Frozen DB worlds omit import-envelope fields. Compare new findings with
    # baseline rather than silently changing unrelated metadata/history.
    before = lint_world_package(original)
    after = lint_world_package(updated)
    existing = {(x['code'], x['path']) for x in before['issues']}
    introduced = [x for x in after['issues'] if x['level'] == 'error' and (x['code'], x['path']) not in existing]
    assert not introduced, introduced
    preflight = inspect_world_package(updated)
    assert preflight.get('ok', not preflight.get('errors')), preflight
    return {'new_lint_errors': introduced, 'baseline_lint_errors': before['errors'], 'lint_errors': after['errors']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-turn', type=int, default=226)
    parser.add_argument('--expected-revision', type=int, default=247)
    args = parser.parse_args()
    path = DATA / 'catalog_v090.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        session = dict(reader.execute('SELECT * FROM sessions WHERE id=?', (SID,)).fetchone())
        config = dict(reader.execute('SELECT * FROM instance_configs WHERE session_id=?', (SID,)).fetchone())
        rule = dict(reader.execute('SELECT * FROM session_rule_states WHERE session_id=?', (SID,)).fetchone())
        world_row = reader.execute('SELECT * FROM worlds WHERE id=?', (WID,)).fetchone()
        catalog = TavernDatabase._world(world_row)
        frozen = json.loads(config['world_snapshot_json'])
        progress = json.loads(rule['progress_json'])
        ledger = [tuple(r) for r in reader.execute('SELECT * FROM story_ledger WHERE session_id=? ORDER BY id', (SID,))]
        completed = {r['stable_key'] for r in reader.execute("SELECT stable_key FROM story_ledger WHERE session_id=? AND kind='milestone' AND status='completed'", (SID,))}
    assert session['world_id'] == WID and session['state'] == 'running' and session['selected'] == 1
    assert (session['turn_no'], session['revision']) == (args.expected_turn, args.expected_revision), (session['turn_no'], session['revision'])
    assert progress['current_chapter_id'] == 'ch_05_chapter'
    assert len(completed) == 13 and all(k.startswith(('m_01_', 'm_02_', 'm_03_', 'm_04_')) for k in completed), completed
    source = json.loads(PACKAGE.read_text(encoding='utf-8'))
    output, new_catalog, new_frozen = revised(source), revised(catalog), revised(frozen)
    reports = [validate(a, b) for a, b in [(source, output), (catalog, new_catalog), (frozen, new_frozen)]]
    chapter = new_frozen['rules']['progress']['chapters'][4]
    progress.update(chapter=chapter['title'], current_objective=chapter['current_objective'], total_milestones=25)
    assert progress['completed_milestones'] == 13
    print(encode({'mode': 'apply' if args.apply else 'dry-run', 'turn': session['turn_no'], 'revision': session['revision'], 'validation': reports, 'remaining_milestones': 12}))
    if not args.apply:
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = DATA / 'catalog_backups' / ('royal_continuation_' + stamp)
    backup.mkdir(parents=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as reader:
        with closing(sqlite3.connect(backup / 'catalog.sqlite3')) as destination:
            reader.backup(destination)
    shutil.copy2(PACKAGE, backup / PACKAGE.name)
    print('BACKUP', backup, flush=True)
    # Avoid initialization/migrations/bootstrap against the running database.
    db = TavernDatabase.__new__(TavernDatabase)
    db.path, db.data_dir = path, DATA
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            live = connection.execute('SELECT * FROM sessions WHERE id=?', (SID,)).fetchone()
            assert live['revision'] == session['revision'] and live['turn_no'] == session['turn_no'], 'Live turn changed; abort'
            assert connection.execute('SELECT revision FROM worlds WHERE id=?', (WID,)).fetchone()[0] == catalog['revision']
            assert connection.execute('SELECT revision FROM session_rule_states WHERE session_id=?', (SID,)).fetchone()[0] == rule['revision']
            assert connection.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (SID,)).fetchone()[0] == config['world_snapshot_json']
            assert not connection.execute("SELECT 1 FROM group_votes WHERE session_id=? AND status IN ('active','pending')", (SID,)).fetchone()
            assert not connection.execute("SELECT 1 FROM action_operations WHERE session_id=? AND status IN ('running','pending','processing')", (SID,)).fetchone()
            revision = catalog['revision'] + 1
            new_catalog['revision'] = new_frozen['revision'] = revision
            connection.execute('UPDATE worlds SET rules_json=?,system_prompt=?,revision=?,updated_at=? WHERE id=?', (encode(new_catalog['rules']), new_catalog['system_prompt'], revision, now, WID))
            row = connection.execute('SELECT * FROM worlds WHERE id=?', (WID,)).fetchone()
            db._persist_world_revision(connection, row, new_catalog, now)
            connection.execute('UPDATE instance_configs SET world_snapshot_json=?,world_revision=?,updated_at=? WHERE session_id=?', (encode(new_frozen), revision, now, SID))
            connection.execute('UPDATE session_rule_states SET progress_json=?,revision=revision+1,updated_at=? WHERE session_id=?', (encode(progress), now, SID))
            state = json.loads(live['world_state_json'])
            if 'progress' in state:
                state['progress'] = progress
            connection.execute('UPDATE sessions SET world_state_json=?,revision=revision+1,updated_at=? WHERE id=?', (encode(state), now, SID))
            # Old model options can still invite the obsolete interrogation.
            connection.execute("UPDATE choice_sets SET status='superseded',updated_at=? WHERE session_id=? AND status='active'", (now, SID))
            connection.execute("UPDATE story_storage SET sync_status='pending',updated_at=? WHERE session_id=?", (now, SID))
            detail = {'backup': str(backup), 'turn': live['turn_no'], 'completed_preserved': sorted(completed), 'chapters_replaced': [c['id'] for c in new_frozen['rules']['progress']['chapters'][4:]], 'world_revision': revision, 'no_event_or_player_or_npc_edits': True}
            connection.execute('INSERT INTO audit_logs(session_id,actor_id,action,target,detail_json,created_at) VALUES (?,?,?,?,?,?)', (SID, 'user_authorized_continuation_edit', 'world.continuation.patch', WID, encode(detail), now))
            assert [tuple(r) for r in connection.execute('SELECT * FROM story_ledger WHERE session_id=? ORDER BY id', (SID,))] == ledger
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    # The file is a mechanical projection of the reviewed patch; the DB is
    # authoritative. A backup and audit record exist before either is replaced.
    InstanceStorage._atomic_json(PACKAGE, output)
    storage = InstanceStorage(data_dir=DATA, catalog_path=path, connect_catalog=db._connect, schema_version=DATABASE_SCHEMA_VERSION)
    storage.sync_session(SID)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as verify:
        assert verify.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert json.loads(verify.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (SID,)).fetchone()[0]) == new_frozen
        assert verify.execute('SELECT turn_no FROM sessions WHERE id=?', (SID,)).fetchone()[0] >= session['turn_no']
        print('VERIFIED', verify.execute('SELECT sync_status,relative_path FROM story_storage WHERE session_id=?', (SID,)).fetchone())
    print('Applied. Events, completed milestones, players, NPC runtime states and turn order preserved.')


if __name__ == '__main__':
    main()
