"""One-off removal authorized by the owner; refuses a changed/started session.

Default: rehearse against an in-memory copy. --apply: back up the catalog first.
The existing story folder is retained for a separate recoverable trash move.
"""
import argparse
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


CATALOG = Path('D:/qq-claude-bot/.runtime/astrbot-data/data/plugin_data/astrbot_plugin_tavern/catalog_v090.sqlite3')
WORLD = 'world_3db3e5333639451597b6395bfb0a376b'
SESSION = 'session_0fabad8aa42f467a8e38cc8ba7df6ab8'
SLUG = 'rezero-first-day-no-return'


def remove(connection):
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('BEGIN IMMEDIATE')
    try:
        world = connection.execute('SELECT * FROM worlds WHERE id=? AND slug=?', (WORLD, SLUG)).fetchone()
        assert world is not None and world['name'] == '王都第一日', 'Target changed or already removed'
        sessions = connection.execute('SELECT * FROM sessions WHERE world_id=?', (WORLD,)).fetchall()
        assert len(sessions) == 1 and sessions[0]['id'] == SESSION, 'Unexpected sessions'
        assert sessions[0]['state'] == 'preparing' and sessions[0]['turn_no'] == 0, 'The story has started; refusing removal'
        assert sessions[0]['revision'] == 3, 'Session changed since inspection'
        assert connection.execute('SELECT count(*) FROM events WHERE session_id=?', (SESSION,)).fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM participants WHERE session_id<>? AND character_card_id IN (SELECT id FROM character_cards WHERE world_id=?)', (SESSION, WORLD)).fetchone()[0] == 0, 'Cards used by another session'
        print('Removing world:', world['name'])
        print('NPCs:', connection.execute('SELECT count(*) FROM characters WHERE world_id=?', (WORLD,)).fetchone()[0])
        print('Cards:', connection.execute('SELECT count(*) FROM character_cards WHERE world_id=?', (WORLD,)).fetchone()[0])
        other_sessions = [tuple(r) for r in connection.execute('SELECT id,state,revision FROM sessions WHERE id<>? ORDER BY id', (SESSION,))]
        other_worlds = [tuple(r) for r in connection.execute('SELECT id,revision FROM worlds WHERE id<>? ORDER BY id', (WORLD,))]
        connection.execute("DELETE FROM token_quota_policies WHERE scope_type='session' AND scope_id=?", (SESSION,))
        connection.execute('DELETE FROM sessions WHERE id=?', (SESSION,))
        connection.execute('DELETE FROM character_cards WHERE world_id=?', (WORLD,))
        connection.execute('DELETE FROM world_snapshots WHERE world_id=?', (WORLD,))
        connection.execute('DELETE FROM worlds WHERE id=?', (WORLD,))
        assert other_sessions == [tuple(r) for r in connection.execute('SELECT id,state,revision FROM sessions ORDER BY id')]
        assert other_worlds == [tuple(r) for r in connection.execute('SELECT id,revision FROM worlds ORDER BY id')]
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        connection.commit()
        print('Removal verified; other worlds and sessions unchanged.')
    except BaseException:
        connection.rollback()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with closing(sqlite3.connect(CATALOG.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(':memory:')) as rehearsal:
            source.backup(rehearsal)
            remove(rehearsal)
        if args.apply:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = CATALOG.parent / 'catalog_backups' / ('before_remove_rezero_' + stamp + '.sqlite3')
            backup.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
            print('Recoverable backup:', backup)
    if args.apply:
        with closing(sqlite3.connect(CATALOG, timeout=15)) as live:
            remove(live)
    else:
        print('Dry run only; live database unchanged.')
