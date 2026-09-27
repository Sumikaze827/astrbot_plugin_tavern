"""Restore the evidenced chapter boundary for the owner's current ReZero game."""
import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tavern.database import TavernDatabase

DATA = Path('D:/qq-claude-bot/.runtime/astrbot-data/data/plugin_data/astrbot_plugin_tavern')
SID = 'session_af9eda05714c4386b217acbc53f33985'
EVIDENCE = 'event_8b4c65e9b8f143c19759f853d951223a'
QUOTE = '罗姆爷已从她身后沉声开口：“让他进来。”门被拉开大半'


async def main():
    backup = DATA / 'catalog_backups' / ('before_rezero_chapter_realign_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
    with closing(sqlite3.connect((DATA / 'catalog_v090.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup)) as dest:
            source.backup(dest)
    print('Backup:', backup)
    db = TavernDatabase(DATA)
    session = await db.get_session(SID)
    assert session['state'] == 'running'
    rule = await db.get_session_rule_state(SID)
    progress = dict(rule['progress'])
    if progress.get('current_chapter_id') != 'ch_01_trail':
        print('Chapter already changed, no write:', progress.get('current_chapter_id'))
        return
    with db._connect() as c:
        world = json.loads(c.execute('SELECT world_snapshot_json FROM instance_configs WHERE session_id=?', (SID,)).fetchone()[0])
        event = c.execute('SELECT content,turn_no FROM events WHERE session_id=? AND id=? AND role=?', (SID, EVIDENCE, 'narrator')).fetchone()
        assert event and QUOTE in event['content']
        ledger = [tuple(r) for r in c.execute('SELECT stable_key,status FROM story_ledger WHERE session_id=? AND kind=? ORDER BY stable_key', (SID, 'milestone'))]
    assert ('m_01_01_route', 'completed') in ledger
    assert not await db.active_vote(SID)
    assert not await db.pending_vote_resolution(SID)
    chapter = next(c for c in world['rules']['progress']['chapters'] if c['id'] == 'ch_02_bargain')
    progress.update({
        'current_chapter_id': chapter['id'], 'chapter': chapter['title'],
        'current_objective': chapter['current_objective'],
        'chapter_entered_at_turn': event['turn_no'],
        'milestone_evidence_since_turn': min(int(progress.get('milestone_evidence_since_turn') or 0), event['turn_no']),
        'narrative_length_band': chapter.get('narrative_length_band', 'standard'),
        'last_chapter_closure': {'chapter_id': 'ch_01_trail', 'event_id': EVIDENCE,
            'quote': QUOTE, 'reason': '寻路目标已完成，人物已获准进入赃物库开展下一幕交涉；本章不要求交易完成。',
            'terminal': False},
    })
    await db.save_session_rule_state(SID, {'progress': progress, 'revision': rule['revision']}, actor_id='user_authorized_chapter_realign')
    with db._connect() as c:
        after = [tuple(r) for r in c.execute('SELECT stable_key,status FROM story_ledger WHERE session_id=? AND kind=? ORDER BY stable_key', (SID, 'milestone'))]
        assert after == ledger, 'Milestones must remain unchanged'
        print('Storage:', dict(c.execute('SELECT relative_path,sync_status FROM story_storage WHERE session_id=?', (SID,)).fetchone()))
    print('Updated:', (await db.get_session_rule_state(SID))['progress']['chapter'])


if __name__ == '__main__':
    asyncio.run(main())
