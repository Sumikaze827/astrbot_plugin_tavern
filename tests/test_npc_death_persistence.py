from test_status_ops import StatusOpsTests, _insert_participant
from tavern.database_support import new_id


class NPCDeathTests(StatusOpsTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pid = new_id('participant')
        with self.database._connect() as c:
            _insert_participant(c, self.session['id'], self.pid, '玩家')
        self.npc = await self.database.save_session_character({
            'session_id': self.session['id'], 'name': '守门人',
            'state': {'location': '门口', 'status': 'active'},
        }, 'admin')

    def apply_npc(self, *ops):
        return self._call_v05(self.pid, {'npc_ops': list(ops)})

    def status(self):
        import json
        with self.database._connect() as c:
            r = c.execute('SELECT sc.lifecycle_status, st.state_json FROM session_characters sc LEFT JOIN session_character_states st ON sc.id=st.character_id WHERE sc.id=?', (self.npc['id'],)).fetchone()
            return r[0], json.loads(r[1])

    def kill(self):
        self.apply_npc({'op': 'kill', 'npc_id': self.npc['id']})
        self.assertEqual(self.status()[0], 'dead')
        self.assertEqual(self.status()[1]['status'], 'dead')

    async def test_kill_without_runtime_payload_records_both_statuses(self):
        self.kill()
        self.assertEqual(self.status()[1]['location'], '门口')

    async def test_ordinary_ops_cannot_resurrect(self):
        self.kill()
        for op in ('update', 'create', 'depart', 'archive', 'update'):
            self.apply_npc({'op': op, 'npc_id': self.npc['id'],
                            'runtime_state': {'status': 'active'},
                            'public_profile': {'identity': '已倒下的守门人'}})
            self.assertEqual(self.status()[0], 'dead', op)
            self.assertEqual(self.status()[1]['status'], 'dead', op)

    async def test_duplicate_name_does_not_create_living_replacement(self):
        self.kill()
        self.apply_npc({'op': 'create', 'name': '守门人', 'runtime_state': {'status': 'active'}})
        self.assertEqual(self.status()[0], 'dead')
        with self.database._connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM session_characters WHERE session_id=? AND name=?', (self.session['id'], '守门人')).fetchone()[0], 1)

    async def test_chapter_sync_cannot_overwrite_dead_actor(self):
        character = await self.database.save_character({
            'world_id': self.session['world_id'], 'slug': 'death-test-guard',
            'name': '守门人原型', 'profile': {},
        }, 'admin')
        with self.database._connect() as c:
            c.execute('UPDATE session_characters SET stable_key=? WHERE id=?', ('world:' + character['id'], self.npc['id']))
        self.kill()
        await self.database.sync_chapter_npc_states(self.session['id'], self.session['world_id'],
            [{'ref': 'death-test-guard', 'state': {'status': 'active', 'location': '宴会厅'}}])
        self.assertEqual(self.status(), ('dead', {'location': '门口', 'status': 'dead'}))

    async def test_admin_profile_edit_preserves_death_explicit_correction_allowed(self):
        self.kill()
        payload = {'session_id': self.session['id'], 'id': self.npc['id'], 'name': '守门人'}
        await self.database.save_session_character(payload, 'admin')
        self.assertEqual(self.status()[0], 'dead')
        self.assertEqual(self.status()[1]['status'], 'dead')
        await self.database.save_session_character(dict(payload, lifecycle_status='active'), 'admin')
        self.assertEqual(self.status()[0], 'active')
        self.assertEqual(self.status()[1]['status'], 'active')
