"""Run the same persistence contract on SQLite and a real MySQL server.

EV27_TEST_MYSQL_URL must use a test account allowed to CREATE/DROP DATABASE.
Each test creates a randomly named database and drops only that database.
"""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from ev27.config import Config
from ev27.store import Store, StoreIntegrityError
import test_core
from test_core import event


class StoreContract:
    def test_consent_tokens_and_revocation(self):
        s = self.store
        c, m = 1481670108379222026, 1481670108379222027
        s.appoint(c, m)
        token = s.invite(c, m)
        s.invitation_message(token, 1481670108379222028)
        self.assertEqual(s.invitation(token)['message_id'], 1481670108379222028)
        replacement = s.invite(c, m)
        self.assertIsNone(s.invitation(token))
        self.assertTrue(s.accept(c, m))
        self.assertFalse(s.accept(c, m))
        s.appoint(c, m)
        self.assertEqual(s.state(c, m), 'active')
        self.assertEqual(s.targets(m), [c])
        self.assertEqual(s.active_count(m), 1)
        self.assertEqual(s.invitations()[0]['token'], replacement)
        s.revoke(c, m)
        self.assertEqual(s.active_count(m), 0)
        self.assertIsNone(s.invitation(replacement))
        s.appoint(c, m)
        self.assertEqual(s.state(c, m), 'pending')

    def test_identity_collision_does_not_reassign_other_member(self):
        s = self.store
        s.map_identity(1, 'Candidate-A')
        with self.assertRaises(StoreIntegrityError):
            s.map_identity(2, 'Candidate-A')
        self.assertEqual(s.identity(1), 'Candidate-A')
        self.assertIsNone(s.identity(2))
        s.map_identity(2, 'candidate-a')
        with self.assertRaises(StoreIntegrityError):
            s.map_identity(1, 'candidate-a')
        self.assertEqual(s.identity(1), 'Candidate-A')
        s.map_identity(1, 'new-id')
        self.assertEqual(s.identity(1), 'new-id')

    def test_pending_constraint_and_terminal_history(self):
        s = self.store
        op = s.begin('event', 1, 'candidate', 2, True)
        self.assertEqual(s.begin('event', 1, 'candidate', 2, True)['id'], op['id'])
        with self.assertRaises(StoreIntegrityError):
            s.execute('''INSERT INTO operations(id,event_id,candidate,calendar_id,actor,desired,state)
                VALUES(?,?,?,?,?,?,?)''', (str(uuid.uuid4()), 'event', 1, 'candidate', 2, 0, 'pending'))
        s.finish(op['id'], 'confirmed')
        second = s.begin('event', 1, 'candidate', 2, False)
        self.assertNotEqual(second['id'], op['id'])
        s.finish(second['id'], 'failed')
        self.assertEqual(s.pending(), [])
        self.assertEqual(len(s.rows('SELECT * FROM operations')), 2)

    def test_identity_protection_after_use(self):
        s = self.store
        s.map_identity(1, 'candidate')
        op = s.begin('event', 1, 'candidate', 1, True)
        with self.assertRaises(ValueError):
            s.map_identity(1, 'other')
        s.finish(op['id'], 'confirmed')
        with self.assertRaises(ValueError):
            s.map_identity(1, 'other')

    def test_event_upsert_preserves_message_and_case(self):
        s = self.store
        s.save_event(event(id='Event', title='Ääni 🌿'))
        s.message('Event', -1)
        s.save_event(event(id='Event', title='Updated 🌿'))
        s.save_event(event(id='event'))
        self.assertEqual(s.event('Event')['message_id'], -1)
        s.save_event(event(id='Event '))
        self.assertEqual(len(s.events()), 3)
        self.assertIsNone(s.event('Event ')['message_id'])
        self.assertIn('Updated', s.event('Event')['data'])
        self.assertTrue(s.claim_reminder('Event', '7d'))
        self.assertFalse(s.claim_reminder('Event', '7d'))
        self.assertTrue(s.claim_reminder('event', '7d'))
        s.audit(1481670108379222026, 'test', name='Ääni 🌿')
        self.assertEqual(s.rows('SELECT actor FROM audit')[0]['actor'], 1481670108379222026)

    def test_member_invalidation_and_guild_binding(self):
        s = self.store
        s.bind_guild(1481670108379222026)
        s.bind_guild(1481670108379222026)
        with self.assertRaises(ValueError):
            s.bind_guild(99)
        for c, m in [(1, 3), (2, 3)]:
            s.appoint(c, m)
            s.invite(c, m)
            s.accept(c, m)
        s.invalidate_member(1, candidate_only=True)
        self.assertEqual(s.targets(3), [2])
        s.invalidate_member(3)
        self.assertEqual(s.appointments(), [])
        self.assertEqual(s.invitations(), [])

    def test_restart_preserves_state_and_claims(self):
        self.store.appoint(1, 2)
        self.store.accept(1, 2)
        op = self.store.begin('event', 1, 'candidate', 2, True)
        self.store.claim_reminder('event', '7d')
        self.store.close()
        self.store = Store(self.database)
        self.assertEqual(self.store.targets(2), [1])
        self.assertEqual(self.store.pending('event', 1)['id'], op['id'])
        self.assertFalse(self.store.claim_reminder('event', '7d'))


class SQLiteContractTests(StoreContract, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = str(Path(self.directory.name) / 'ev27.db')
        self.store = Store(self.database)

    def tearDown(self):
        self.store.close()


@unittest.skipUnless(os.environ.get('EV27_TEST_MYSQL_URL'), 'Set EV27_TEST_MYSQL_URL for real MySQL tests')
class MySQLContractTests(StoreContract, unittest.TestCase):
    def setUp(self):
        url = make_url(os.environ['EV27_TEST_MYSQL_URL']).set(drivername='mysql+pymysql')
        name = 'ev27_test_' + uuid.uuid4().hex
        self.admin = create_engine(url.set(database=''), hide_parameters=True)
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE `{name}`')
        def drop_database():
            with self.admin.begin() as connection:
                connection.exec_driver_sql(f'DROP DATABASE `{name}`')
        self.addCleanup(drop_database)
        self.database = url.set(database=name).render_as_string(hide_password=False)
        self.store = Store(self.database)

    def tearDown(self):
        self.store.close()


class DatabaseConfigTests(unittest.TestCase):
    def test_dedicated_url_overrides_path_without_using_shared_credentials(self):
        env = dict(EV27_TOKEN='test', EV27_GUILD_ID='1', EV27_CANDIDATE_ROLE_ID='2',
                   EV27_MANAGER_ROLE_ID='3', EV27_CHANNEL_ID='4', EV27_DISTRICT='district',
                   EV27_DATABASE='local.db', EV27_DATABASE_URL='mysql+pymysql://user:pass@host/ev27',
                   DATABASE_URL='mysql+pymysql://shared:secret@host/shared')
        with patch.dict(os.environ, env, clear=True), patch('ev27.config.load_dotenv'):
            self.assertEqual(Config.load().database, env['EV27_DATABASE_URL'])
            os.environ['EV27_DATABASE_URL'] = ''
            self.assertEqual(Config.load().database, 'local.db')

    def test_unsupported_database_url_rejected(self):
        with self.assertRaises(ValueError):
            Store('postgresql://user:password@localhost/test')
        with self.assertRaises(ValueError):
            Store('mysql+pymysql://user:password@localhost')


@unittest.skipUnless(os.environ.get('EV27_TEST_MYSQL_URL'), 'Set EV27_TEST_MYSQL_URL for real MySQL tests')
class MySQLCalendarTests(test_core.CalendarIntegrationTests):
    async def asyncSetUp(self):
        MySQLContractTests.setUp(self)
        mysql_store = self.store
        self.addCleanup(mysql_store.close)
        await super().asyncSetUp()
        self.store.close()
        self.store = mysql_store
        self.service.store = self.store
        self.store.map_identity(1, 'candidate-1')
        self.store.map_identity(2, 'candidate-2')
        for candidate in (1, 2):
            self.store.appoint(candidate, 3)
            self.store.accept(candidate, 3)
