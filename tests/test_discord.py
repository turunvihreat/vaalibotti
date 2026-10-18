from datetime import datetime, timedelta, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from ev27.bot import CandidatePicker, EV27Bot, EventView, InvitationView
from ev27.config import Config
from ev27.service import UserError
from test_core import event


class Role:
    def __init__(self, role_id, rank=1, privileged=False):
        self.id, self.rank = role_id, rank
        self.managed = False
        self.permissions = discord.Permissions(administrator=privileged)

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.rank >= other.rank


class Member:
    def __init__(self, member_id, roles=(), admin=False):
        self.id, self.roles = member_id, list(roles)
        self.display_name = f'Member {member_id}'
        self.bot = False
        self.send = AsyncMock(return_value=SimpleNamespace(id=12345))
        self.guild_permissions = discord.Permissions(manage_guild=admin)
        self.add_roles = AsyncMock(side_effect=lambda role, **kw: self.roles.append(role))
        self.remove_roles = AsyncMock(side_effect=lambda role, **kw: self.roles.remove(role))


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = Config('test-token', 10, 20, 30, 40, 'district', 'EV27', ':memory:', '', '', True, True)
        self.bot = EV27Bot(cfg)
        self.candidate_role, self.manager_role = Role(20), Role(30)
        self.members = {1: Member(1, [self.candidate_role]), 2: Member(2, [self.candidate_role]),
                        3: Member(3), 4: Member(4)}
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=999))
        self.partial = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        self.channel.get_partial_message.return_value = self.partial
        self.guild = SimpleNamespace(id=10, name='District server', me=SimpleNamespace(top_role=Role(100, 10),
                                     guild_permissions=discord.Permissions(manage_roles=True)),
                                     get_role=lambda rid: {20: self.candidate_role, 30: self.manager_role}.get(rid),
                                     get_channel=lambda cid: self.channel if cid == 40 else None)
        async def fetch(member_id):
            return self.members.get(member_id)
        self.bot.member = fetch
        self.guild_patch = patch.object(EV27Bot, 'guild', property(lambda bot: self.guild))
        self.guild_patch.start()
        self.bot.service.calendar = SimpleNamespace(events=AsyncMock(return_value=[event()]))

    async def asyncTearDown(self):
        self.guild_patch.stop()
        await self.bot.close()

    def activate(self, candidate, manager):
        self.bot.store.appoint(candidate, manager)
        self.bot.store.accept(candidate, manager)

    def command(self, name):
        return self.bot.tree.get_command('ek27', guild=discord.Object(id=10)).get_command(name).callback

    def interaction(self, actor=1, guild_id=10):
        return SimpleNamespace(user=self.members[actor], guild_id=guild_id,
                               response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                                        is_done=lambda: True),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_role_retained_for_other_candidate_and_removed_after_last_revocation(self):
        self.activate(1, 3)
        self.activate(2, 3)
        await self.bot.sync_manager(3)
        self.assertIn(self.manager_role, self.members[3].roles)
        self.bot.store.revoke(1, 3)
        await self.bot.sync_manager(3)
        self.assertIn(self.manager_role, self.members[3].roles)
        self.bot.store.revoke(2, 3)
        await self.bot.sync_manager(3)
        self.assertNotIn(self.manager_role, self.members[3].roles)

    async def test_privileged_role_configuration_rejected(self):
        self.manager_role.permissions.administrator = True
        self.activate(1, 3)
        with self.assertRaises(UserError):
            await self.bot.sync_manager(3)
        self.members[3].add_roles.assert_not_awaited()

    def dm_interaction(self, actor=3, message_id=12345):
        interaction = self.interaction(actor, guild_id=None)
        interaction.message = SimpleNamespace(id=message_id, edit=AsyncMock())
        return interaction

    async def nominate(self):
        await self.command('kampanjapäällikkö')(self.interaction(), self.members[3])
        return self.bot.store.invitations()[0]['token']

    async def test_nomination_and_acceptance_commands(self):
        await self.command('kampanjapäällikkö')(self.interaction(), self.members[3])
        self.assertEqual(self.bot.store.state(1, 3), 'pending')
        self.assertNotIn(self.manager_role, self.members[3].roles)
        self.members[3].send.assert_awaited_once()
        token = self.bot.store.invitations()[0]['token']
        dm = self.dm_interaction()
        await self.bot.respond_to_invitation(dm, token, True)
        dm.message.edit.assert_awaited_once_with(view=None)
        self.assertEqual(self.bot.store.state(1, 3), 'active')
        self.assertIn(self.manager_role, self.members[3].roles)

    async def test_command_uses_native_member_picker_and_new_prefix(self):
        group = self.bot.tree.get_command('ek27', guild=discord.Object(id=10))
        command = group.get_command('kampanjapäällikkö')
        self.assertEqual(command.parameters[0].display_name, 'henkilö')
        self.assertEqual(command.parameters[0].type, discord.AppCommandOptionType.user)
        self.assertIsNone(group.get_command('hyvaksy'))
        self.assertIsNone(self.bot.tree.get_command('ev27', guild=discord.Object(id=10)))

    async def test_decline_does_not_grant_role(self):
        token = await self.nominate()
        await self.bot.respond_to_invitation(self.dm_interaction(), token, False)
        self.assertEqual(self.bot.store.state(1, 3), 'revoked')
        self.assertIsNone(self.bot.store.invitation(token))
        self.members[3].add_roles.assert_not_awaited()

    async def test_invitation_can_only_be_used_by_recipient_in_original_dm(self):
        token = await self.nominate()
        for interaction in [self.dm_interaction(4), self.dm_interaction(message_id=54321)]:
            await self.bot.respond_to_invitation(interaction, token, True)
            self.assertEqual(self.bot.store.state(1, 3), 'pending')
        guild_interaction = self.dm_interaction()
        guild_interaction.guild_id = 10
        await self.bot.respond_to_invitation(guild_interaction, token, True)
        self.assertEqual(self.bot.store.state(1, 3), 'pending')
        self.members[3].add_roles.assert_not_awaited()

    async def test_revoked_and_superseded_invitations_cannot_be_accepted(self):
        old = await self.nominate()
        new = await self.nominate()
        self.assertNotEqual(old, new)
        await self.bot.respond_to_invitation(self.dm_interaction(), old, True)
        self.assertEqual(self.bot.store.state(1, 3), 'pending')
        self.bot.store.revoke(1, 3)
        await self.bot.respond_to_invitation(self.dm_interaction(), new, True)
        self.assertEqual(self.bot.store.state(1, 3), 'revoked')
        self.members[3].add_roles.assert_not_awaited()

    async def test_live_candidate_verification_on_dm_acceptance(self):
        token = await self.nominate()
        self.members[1].roles = []
        await self.bot.respond_to_invitation(self.dm_interaction(), token, True)
        self.assertEqual(self.bot.store.state(1, 3), 'revoked')
        self.members[3].add_roles.assert_not_awaited()

    async def test_blocked_dms_are_actionable_and_do_not_activate_appointment(self):
        response = SimpleNamespace(status=403, reason='Forbidden')
        self.members[3].send.side_effect = discord.Forbidden(response, 'Cannot send messages to this user')
        with self.assertRaisesRegex(UserError, 'sallimaan yksityisviestit'):
            await self.command('kampanjapäällikkö')(self.interaction(), self.members[3])
        self.assertEqual(self.bot.store.state(1, 3), 'revoked')
        self.assertEqual(self.bot.store.invitations(), [])
        self.members[3].add_roles.assert_not_awaited()
        self.members[3].send.side_effect = None
        await self.nominate()
        self.assertEqual(self.bot.store.state(1, 3), 'pending')

    async def test_invitation_buttons_restored_with_stable_ids(self):
        token = await self.nominate()
        first = self.members[3].send.call_args.kwargs['view']
        self.assertTrue(first.is_persistent())
        with patch.object(self.bot, 'add_view') as add_view:
            self.bot.restore_views()
            restored = add_view.call_args.args[0]
            self.assertIsInstance(restored, InvitationView)
            self.assertEqual(add_view.call_args.kwargs['message_id'], 12345)
            self.assertEqual([b.custom_id for b in first.children], [b.custom_id for b in restored.children])
        await restored.children[0].callback(self.dm_interaction())
        self.assertEqual(self.bot.store.state(1, 3), 'active')
        self.assertIsNotNone(self.bot.store.invitation(token))

    async def test_repeated_nomination_of_active_manager_does_not_send_another_dm(self):
        self.activate(1, 3)
        await self.command('kampanjapäällikkö')(self.interaction(), self.members[3])
        self.members[3].send.assert_not_awaited()
        self.assertEqual(self.bot.store.state(1, 3), 'active')

    async def test_unverified_nomination_and_cross_server_commands_denied(self):
        with self.assertRaises(UserError):
            await self.command('kampanjapäällikkö')(self.interaction(3), self.members[4])
        with self.assertRaises(UserError):
            await self.command('kampanjapäällikkö')(self.interaction(guild_id=11), self.members[3])
        self.assertEqual(self.bot.store.appointments(), [])

    async def test_unrelated_actor_cannot_revoke(self):
        self.activate(1, 3)
        with self.assertRaises(UserError):
            await self.command('poista')(self.interaction(4), self.members[1], self.members[3])
        self.assertEqual(self.bot.store.state(1, 3), 'active')

    async def test_candidate_role_removal_and_restore_do_not_reactivate(self):
        self.activate(1, 3)
        await self.bot.sync_manager(3)
        before = SimpleNamespace(guild=self.guild, id=1, roles=[self.candidate_role])
        after = SimpleNamespace(guild=self.guild, id=1, roles=[])
        self.members[1].roles = []
        await self.bot.on_member_update(before, after)
        self.assertEqual(self.bot.store.state(1, 3), 'revoked')
        self.assertNotIn(self.manager_role, self.members[3].roles)
        self.members[1].roles = [self.candidate_role]
        await self.bot.on_member_update(after, before)
        self.assertEqual(self.bot.store.state(1, 3), 'revoked')

    async def test_departure_invalidates_all_appointments(self):
        self.activate(1, 3)
        self.activate(2, 3)
        await self.bot.sync_manager(3)
        leaving = self.members.pop(3)
        leaving.guild = self.guild
        await self.bot.on_member_remove(leaving)
        self.assertEqual(self.bot.store.targets(3), [])
        self.members[3] = Member(3)
        self.assertEqual(self.bot.store.targets(3), [])

    async def test_reconciliation_uses_complete_member_snapshot(self):
        self.activate(1, 3)
        self.activate(2, 4)
        del self.members[2]
        self.members[4].roles.append(self.manager_role)
        async def fetch_members(limit):
            for member in self.members.values():
                yield member
        self.guild.fetch_members = fetch_members
        await self.bot.reconcile_members()
        self.assertEqual(self.bot.store.state(2, 4), 'revoked')
        self.assertIn(self.manager_role, self.members[3].roles)
        self.assertNotIn(self.manager_role, self.members[4].roles)

    async def test_partial_member_fetch_failure_cannot_invalidate(self):
        self.activate(1, 3)
        async def fetch_members(limit):
            yield self.members[4]
            raise RuntimeError('network failure')
        self.guild.fetch_members = fetch_members
        with self.assertRaises(RuntimeError):
            await self.bot.reconcile_members()
        self.assertEqual(self.bot.store.state(1, 3), 'active')

    async def test_persistent_buttons_have_stable_ids_and_event_scope(self):
        first = EventView(self.bot, 'event-1')
        second = EventView(self.bot, 'event-1')
        different = EventView(self.bot, 'event-2')
        self.assertTrue(first.is_persistent())
        self.assertEqual([b.custom_id for b in first.children], [b.custom_id for b in second.children])
        self.assertNotEqual(first.children[0].custom_id, different.children[0].custom_id)

    async def test_candidate_picker_paginates_more_than_25_candidates(self):
        options = [(number, str(number)) for number in range(30)]
        first = CandidatePicker(self.bot, 3, 'event-1', True, options)
        second = CandidatePicker(self.bot, 3, 'event-1', True, options, 1)
        self.assertEqual(len(first.children[0].options), 25)
        self.assertEqual(len(second.children[0].options), 5)
        self.assertFalse(await first.interaction_check(self.interaction(4)))

    async def test_event_card_published_once_then_updated_and_cancelled(self):
        await self.bot.sync_events()
        await self.bot.sync_events()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.bot.store.event('event-1')['message_id'], 999)
        self.bot.service.calendar.events.return_value = [event(status='cancelled')]
        await self.bot.sync_events()
        view = self.partial.edit.call_args.kwargs['view']
        self.assertTrue(all(button.disabled for button in view.children))

    async def test_restricted_event_removes_card(self):
        await self.bot.sync_events()
        self.bot.service.calendar.events.return_value = [event(public=False)]
        await self.bot.sync_events()
        self.partial.delete.assert_awaited_once()
        self.assertIsNone(self.bot.store.event('event-1')['message_id'])
        self.channel.send.assert_awaited_once()

    async def test_missing_event_disables_cached_card(self):
        await self.bot.sync_events()
        self.bot.service.calendar.events.return_value = []
        await self.bot.sync_events()
        self.assertTrue(json.loads(self.bot.store.event('event-1')['data'])['_unavailable'])
        self.assertTrue(all(button.disabled for button in self.partial.edit.call_args.kwargs['view'].children))

    async def test_reminders_deduplicated_and_missed_times_skipped(self):
        due_soon = (datetime.now(timezone.utc) + timedelta(days=7, seconds=-2)).isoformat()
        self.bot.service.calendar.events.return_value = [event(start=due_soon)]
        await self.bot.sync_events()
        await self.bot.sync_events()
        self.assertEqual(self.channel.send.await_count, 2)  # card and one reminder
        self.bot.service.calendar.events.return_value = [event(start=(datetime.now(timezone.utc) + timedelta(days=1, hours=-1)).isoformat())]
        await self.bot.sync_events()
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_uncertain_discord_publication_not_duplicated(self):
        self.channel.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.bot.sync_events()
        self.assertEqual(self.bot.store.event('event-1')['message_id'], -1)
        self.channel.send.side_effect = None
        await self.bot.sync_events()
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_database_cannot_be_reused_for_another_guild(self):
        with self.assertRaises(ValueError):
            self.bot.store.bind_guild(999)
