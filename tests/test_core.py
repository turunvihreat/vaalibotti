import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aiohttp

from ev27.calendar import Calendar, CalendarError, can_register, instant, validate_event
from ev27.config import Config
from ev27.service import Service, UserError
from ev27.store import Store
from fake_calendar import FakeCalendar


def event(**changes):
    value = dict(id='event-1', title='Campaign day', start='2030-03-20T10:00:00+02:00',
                 location='Market square', url='https://calendar.example/events/1', district='district',
                 campaign='EV27', status='scheduled', public=True, registration_open=True,
                 registration_closes=None)
    value.update(changes)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.close()

    def test_consent_and_repeated_nomination(self):
        self.store.appoint(1, 2)
        self.assertEqual(self.store.targets(2), [])
        self.assertTrue(self.store.accept(1, 2))
        self.store.appoint(1, 2)
        self.assertEqual(self.store.state(1, 2), 'active')
        self.assertFalse(self.store.accept(1, 2))
        self.store.revoke(1, 2)
        self.assertEqual(self.store.targets(2), [])
        self.store.appoint(1, 2)
        self.assertEqual(self.store.state(1, 2), 'pending')

    def test_multiple_and_shared_managers(self):
        for candidate, manager in [(1, 3), (2, 3), (1, 4)]:
            self.store.appoint(candidate, manager)
            self.store.accept(candidate, manager)
        self.store.revoke(1, 3)
        self.assertEqual(self.store.active_count(3), 1)
        self.assertEqual(self.store.targets(3), [2])
        self.store.invalidate_member(2, candidate_only=True)
        self.assertEqual(self.store.active_count(3), 0)
        self.assertEqual(self.store.active_count(4), 1)
        self.store.invalidate_member(4)
        self.assertEqual(self.store.active_count(4), 0)

    def test_identity_and_pending_protection(self):
        self.store.map_identity(1, 'candidate-1')
        self.store.begin('event-1', 1, 'candidate-1', 1, True)
        with self.assertRaises(ValueError):
            self.store.map_identity(1, 'candidate-2')
        op = self.store.pending('event-1', 1)
        self.store.finish(op['id'], 'confirmed')
        with self.assertRaises(ValueError):
            self.store.map_identity(1, 'candidate-2')

    def test_persistence_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'data.db')
            store = Store(path)
            store.appoint(1, 2)
            token = store.invite(1, 2)
            store.invitation_message(token, 12345)
            store.accept(1, 2)
            store.save_event(event())
            store.message('event-1', 456)
            self.assertTrue(store.claim_reminder('event-1', '7d'))
            op = store.begin('event-1', 1, 'candidate-1', 2, True)
            store.close()
            store = Store(path)
            self.assertEqual(store.targets(2), [1])
            self.assertEqual(store.invitation(token)['message_id'], 12345)
            self.assertEqual(store.event('event-1')['message_id'], 456)
            self.assertEqual(store.pending('event-1', 1)['id'], op['id'])
            self.assertFalse(store.claim_reminder('event-1', '7d'))
            store.close()


class ValidationTests(unittest.TestCase):
    def test_timezones(self):
        self.assertEqual(instant('2027-01-01T10:00:00+02:00').hour, 8)
        self.assertEqual(instant('2027-07-01T10:00:00+03:00').hour, 7)
        with self.assertRaises(ValueError):
            instant('2027-01-01T10:00:00')

    def test_event_scope_and_fail_closed_flags(self):
        for changes in [dict(district='other'), dict(campaign='other'), dict(public=None),
                        dict(registration_open='true'), dict(registration_closes=False),
                        dict(url='http://unsafe.example'),
                        dict(start='2030-01-01T10:00:00'), dict(status='unknown')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_event(event(**changes), 'district', 'EV27')

    def test_registration_deadline_and_cancellation(self):
        now = datetime(2027, 3, 1, tzinfo=timezone.utc)
        self.assertTrue(can_register(event(), now))
        for changes in [dict(public=False), dict(status='cancelled'), dict(registration_open=False),
                        dict(registration_closes='2027-02-28T00:00:00Z'), dict(start='2027-02-28T00:00:00Z')]:
            self.assertFalse(can_register(event(**changes), now))

    def test_config_is_separate_and_defaults_disabled(self):
        env = dict(EV27_TOKEN='new-token', EV27_GUILD_ID='1', EV27_CANDIDATE_ROLE_ID='2',
                   EV27_MANAGER_ROLE_ID='3', EV27_CHANNEL_ID='4', EV27_DISTRICT='district',
                   DISCORD_BOT_TOKEN='old-token')
        with patch.dict(os.environ, env, clear=True), patch('ev27.config.load_dotenv') as loader:
            cfg = Config.load()
            self.assertEqual(cfg.token, 'new-token')
            self.assertFalse(cfg.calendar_enabled)
            self.assertFalse(cfg.registration_enabled)
            self.assertEqual(loader.call_args.args[0], Path(__file__).resolve().parents[1] / '.env')
            os.environ['EV27_REGISTRATION_ENABLED'] = 'true'
            with self.assertRaises(ValueError):
                Config.load()


class CalendarIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = FakeCalendar([event()])
        await self.fixture.start()
        self.session = aiohttp.ClientSession()
        self.calendar = Calendar(self.session, self.fixture.url, 'test-key', 'district', 'EV27')
        self.store = Store(':memory:')
        self.candidates, self.members = {1, 2}, {1, 2, 3, 4, 5}
        async def candidate(member):
            return member in self.candidates
        async def member(member):
            return member in self.members
        self.service = Service(self.store, self.calendar, candidate, member, True)
        self.store.map_identity(1, 'candidate-1')
        self.store.map_identity(2, 'candidate-2')
        for c in (1, 2):
            self.store.appoint(c, 3)
            self.store.accept(c, 3)

    async def asyncTearDown(self):
        self.store.close()
        await self.session.close()
        await self.fixture.close()

    async def test_discovery_filters_unrelated_activity(self):
        self.fixture.events['other'] = event(id='other', district='other')
        self.assertEqual([e['id'] for e in await self.calendar.events()], ['event-1'])

    async def test_candidate_and_manager_registration_cancellation(self):
        self.assertEqual(await self.service.targets(3), [1, 2])
        await self.service.register(3, 1, 'event-1', True)
        await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(self.fixture.attendance, {('event-1', 'candidate-1')})
        await self.service.register(3, 1, 'event-1', False)
        self.assertEqual(self.fixture.attendance, set())

    async def test_concurrent_duplicate_registration(self):
        await asyncio.gather(self.service.register(3, 1, 'event-1', True),
                             self.service.register(1, 1, 'event-1', True))
        self.assertEqual(len(self.fixture.attendance), 1)
        self.assertEqual(self.store.pending(), [])

    async def test_unverified_unrelated_and_pending_managers_denied(self):
        self.store.appoint(1, 4)
        for actor in (4, 5):
            with self.assertRaises(UserError):
                await self.service.register(actor, 1, 'event-1', True)
        self.assertEqual(self.fixture.calls, [])
        with self.assertRaises(UserError):
            await self.service.register(3, 5, 'event-1', True)

    async def test_revocation_candidate_role_removal_and_departure(self):
        self.store.revoke(1, 3)
        with self.assertRaises(UserError):
            await self.service.register(3, 1, 'event-1', True)
        self.candidates.remove(2)
        with self.assertRaises(UserError):
            await self.service.register(3, 2, 'event-1', True)
        self.members.remove(1)
        with self.assertRaises(UserError):
            await self.service.register(1, 1, 'event-1', True)

    async def test_missing_identity_and_disabled_integration(self):
        self.store.execute('DELETE FROM identities WHERE discord_id=1')
        with self.assertRaises(UserError):
            await self.service.register(1, 1, 'event-1', True)
        self.service.registration_enabled = False
        with self.assertRaises(UserError):
            await self.service.register(2, 2, 'event-1', True)
        self.assertEqual(self.fixture.calls, [])

    async def test_live_event_state_blocks_stale_buttons(self):
        for key, value in [('registration_open', False), ('public', False), ('status', 'cancelled')]:
            self.fixture.events['event-1'] = event(**{key: value})
            with self.assertRaises(UserError):
                await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(self.fixture.calls, [])

    async def test_network_failure_keeps_operation_uncertain(self):
        original = self.calendar.submit
        async def received_but_timed_out(op):
            await original(op)
            raise CalendarError('Simulated dropped response')
        self.calendar.submit = received_but_timed_out
        with self.assertRaises(CalendarError):
            await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(len(self.fixture.attendance), 1)
        self.calendar.submit = original
        # Repeating same action reconciles instead of writing twice.
        await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.store.pending(), [])

    async def test_never_received_request_reuses_operation_id(self):
        op = self.store.begin('event-1', 1, 'candidate-1', 1, True)
        await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(self.fixture.calls[0][0], op['id'])

    async def test_uncertain_request_can_be_resolved_after_registration_closes(self):
        self.store.begin('event-1', 1, 'candidate-1', 1, True)
        self.fixture.events['event-1']['registration_open'] = False
        # Replay the original ID so the calendar can durably reject it. This is
        # recovery of an existing request, never a new closed registration.
        with self.assertRaises(UserError):
            await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(len(self.fixture.calls), 1)
        await self.service.register(1, 1, 'event-1', False)
        self.assertEqual(self.fixture.attendance, set())

    async def test_pending_blocks_opposite_until_reconciled(self):
        self.fixture.pending = True
        with self.assertRaises(UserError):
            await self.service.register(1, 1, 'event-1', True)
        with self.assertRaises(UserError):
            await self.service.register(1, 1, 'event-1', False)
        self.assertEqual(len(self.fixture.calls), 1)
        op = self.store.pending()[0]
        self.fixture.operations[op['id']]['state'] = 'confirmed'
        await self.service.reconcile_operations()
        self.assertEqual(self.store.pending(), [])

    async def test_mismatched_confirmation_cannot_succeed(self):
        op = self.store.begin('event-1', 1, 'candidate-1', 1, True)
        response = dict(id=op['id'], event_id='different-event', candidate_id='candidate-1', registered=True, state='confirmed')
        with self.assertRaises(CalendarError):
            Calendar.result(op, response)

    async def test_calendar_outage_does_not_confirm(self):
        self.fixture.fail = True
        with self.assertRaises(CalendarError):
            await self.service.register(1, 1, 'event-1', True)
        self.assertEqual(self.fixture.attendance, set())
