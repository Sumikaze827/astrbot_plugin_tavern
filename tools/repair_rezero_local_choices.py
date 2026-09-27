"""Replace only the evidenced wrong-location choice set, with a backup and CAS guards."""
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tavern.database import TavernDatabase
from tavern.database_support import new_id, utc_now, json_dump
from tavern.lifecycle import normalize_choices_compat

DATA = Path('D:/qq-claude-bot/.runtime/astrbot-data/data/plugin_data/astrbot_plugin_tavern')
SID = 'session_af9eda05714c4386b217acbc53f33985'
PID = 'participant_4b129806c9d2454ea5298b8ea8045a36'


def main():
    backup = DATA / 'catalog_backups' / ('before_rezero_local_choices_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
    with closing(sqlite3.connect((DATA / 'catalog_v090.sqlite3').as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
    print('Backup:', backup)
    db = TavernDatabase(DATA)
    with db._connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            session = c.execute('SELECT * FROM sessions WHERE id=?', (SID,)).fetchone()
            assert session['turn_no'] == 38 and session['state'] == 'running', 'Story advanced; do not change current choices'
            current = c.execute("SELECT * FROM choice_sets WHERE session_id=? AND status='active' ORDER BY created_at DESC LIMIT 1", (SID,)).fetchone()
            assert current and current['participant_id'] == PID
            assert '和卫兵一起赶回赃物库' in current['choices_json'], 'Choices already changed'
            assert not c.execute('SELECT 1 FROM rolls WHERE choice_set_id=?', (current['id'],)).fetchone(), 'Choice already rolled'
            location = json.loads(c.execute('SELECT state_json FROM character_runtime_states WHERE participant_id=?', (PID,)).fetchone()[0])['current_location']
            assert location == '露格尼卡王都·贫民窟赃物库内'
            world = json.loads(c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (SID,)).fetchone()[0])
            choices = normalize_choices_compat([
                {'key': 'A', 'text': '向罗姆爷说明先暂停交易，询问屋内可用的退路', 'risk': 'safe', 'requires_check': False},
                {'key': 'B', 'text': '从屋内靠近门边查看门外冲突，不离开赃物库', 'risk': 'controlled', 'requires_check': True, 'check_stat': 'perception', 'difficulty': 9, 'known_consequences': '视线受阻时无法看清门外细节，不自动移动到屋外。'},
                {'key': 'C', 'text': '提醒菲鲁特保护好徽章，请她留意门外动静', 'risk': 'safe', 'requires_check': False},
                {'key': 'D', 'text': '挪开脚边木箱，为屋内人员留出移动空间', 'risk': 'controlled', 'requires_check': True, 'check_stat': 'strength', 'difficulty': 9, 'known_consequences': '木箱过重或堆放不稳时未能清出足够空间。'},
            ], world)
            for choice in choices:
                choice['actor_id'] = PID
                choice['collective'] = False
            now = utc_now()
            cid = new_id('choices')
            c.execute("UPDATE choice_sets SET status='superseded',updated_at=? WHERE id=?", (now, current['id']))
            c.execute("INSERT INTO choice_sets(id,session_id,participant_id,round_no,session_revision,choices_json,status,reroll_count,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,'active',?,?,?,?)",
                      (cid, SID, PID, current['round_no'], session['revision'], json_dump(choices), current['reroll_count'], 'location-repair:'+cid, now, now))
            db._insert_audit(c, SID, 'user_authorized_location_repair', 'rescue.replace_choices', cid,
                             {'previous_choice_set_id': current['id'], 'location': location, 'reason': '上一行动者在街口求援，柏辰仍在屋内，应提供其本地选项'})
            c.execute('COMMIT')
            print('Replaced for 柏辰 at', location, 'new choice set:', cid)
        except BaseException:
            c.execute('ROLLBACK')
            raise
    db.storage.sync_session(SID)
    print('Story storage synchronized; locations, narrative and turn order unchanged.')


if __name__ == '__main__':
    main()
