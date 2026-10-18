"""Run with python -m ev27.bot. Standalone EV27 Discord application."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .calendar import Calendar, CalendarError, can_register, instant
from .config import Config
from .service import Service, UserError
from .store import Store, StoreIntegrityError

log = logging.getLogger('ev27')


async def reply(interaction, message, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(message, ephemeral=True, **kwargs)


class EventView(discord.ui.View):
    def __init__(self, bot, event_id, enabled=True):
        super().__init__(timeout=None)
        digest = hashlib.sha256(event_id.encode()).hexdigest()[:32]
        for desired, label, style in (
            (True, 'Ilmoittaudu mukaan', discord.ButtonStyle.success),
            (False, 'Peru ilmoittautuminen', discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, style=style,
                                       custom_id=f'ev27:{digest}:{int(desired)}', disabled=not enabled)
            async def clicked(interaction, registered=desired):
                await bot.choose_candidate(interaction, event_id, registered)
            button.callback = clicked
            self.add_item(button)


class InvitationView(discord.ui.View):
    """Private, recipient-bound invitation. UUIDs prevent obsolete invites reviving."""
    def __init__(self, bot, token):
        super().__init__(timeout=None)
        for accepted, label, style in (
            (True, 'Hyväksy nimitys', discord.ButtonStyle.success),
            (False, 'Hylkää nimitys', discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, style=style,
                                       custom_id=f'ek27:invitation:{token}:{int(accepted)}')
            async def clicked(interaction, accept=accepted):
                await bot.respond_to_invitation(interaction, token, accept)
            button.callback = clicked
            self.add_item(button)


class CandidatePicker(discord.ui.View):
    def __init__(self, bot, actor, event_id, desired, candidates, page=0):
        super().__init__(timeout=180)
        self.actor = actor
        # Ephemeral selections are convenience only: authority is checked again on submission.
        options = [discord.SelectOption(label=label[:100], value=str(candidate))
                   for candidate, label in candidates[page * 25:(page + 1) * 25]]
        select = discord.ui.Select(placeholder='Valitse ehdokas', options=options)
        async def selected(interaction):
            await bot.participate(interaction, int(select.values[0]), event_id, desired)
        select.callback = selected
        self.add_item(select)
        for label, next_page in [('Edellinen', page - 1), ('Seuraava', page + 1)]:
            if 0 <= next_page < (len(candidates) + 24) // 25:
                button = discord.ui.Button(label=label)
                async def change(interaction, target=next_page):
                    await interaction.response.edit_message(view=CandidatePicker(
                        bot, actor, event_id, desired, candidates, target))
                button.callback = change
                self.add_item(button)

    async def interaction_check(self, interaction):
        return interaction.user.id == self.actor

    async def on_error(self, interaction, error, item):
        log.error('Candidate selection failed (%s)', type(error).__name__)
        await reply(interaction, 'Toiminto epäonnistui. Yritä uudelleen.')


class EV27Bot(discord.Client):
    def __init__(self, cfg):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        # No message content intent: the bot does not read campaign discussions.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.store = Store(cfg.database)
        self.store.bind_guild(cfg.guild_id)
        self.tree = app_commands.CommandTree(self)
        self.session = None
        self.service = Service(self.store, None, self.is_candidate, self.is_member, cfg.registration_enabled)
        self.install_commands()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        if self.cfg.calendar_enabled:
            self.service.calendar = Calendar(self.session, self.cfg.calendar_url, self.cfg.calendar_key,
                                             self.cfg.district, self.cfg.campaign)
        self.restore_views()
        await self.tree.sync(guild=discord.Object(id=self.cfg.guild_id))
        self.worker.change_interval(seconds=self.cfg.poll_seconds)
        self.worker.start()

    def restore_views(self):
        for row in self.store.events():
            if row['message_id'] and row['message_id'] > 0:
                self.add_view(EventView(self, row['id']), message_id=row['message_id'])
        for row in self.store.invitations():
            self.add_view(InvitationView(self, row['token']), message_id=row['message_id'])

    async def close(self):
        self.worker.cancel()
        task = self.worker.get_task()
        if task:
            with suppress(asyncio.CancelledError):
                await task
        if self.session:
            await self.session.close()
        self.store.close()
        await super().close()

    @property
    def guild(self):
        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            raise UserError('Vaalipalvelin ei ole käytettävissä.')
        return guild

    async def member(self, member_id):
        try:
            return await self.guild.fetch_member(member_id)
        except discord.NotFound:
            return None
        # Other HTTP failures must not be interpreted as departures.

    async def is_member(self, member_id):
        return await self.member(member_id) is not None

    async def is_candidate(self, member_id):
        member = await self.member(member_id)
        return member is not None and any(role.id == self.cfg.candidate_role_id for role in member.roles)

    def check_context(self, interaction):
        if interaction.guild_id != self.cfg.guild_id:
            raise UserError('Tämä toiminto kuuluu vaalipalvelimelle.')

    async def require_admin(self, interaction):
        self.check_context(interaction)
        member = await self.member(interaction.user.id)
        if member is None or not member.guild_permissions.manage_guild:
            raise UserError('Toiminto vaatii palvelimen hallintaoikeuden.')

    async def sync_manager(self, member_id):
        member = await self.member(member_id)
        if member is None:
            return
        role = self.guild.get_role(self.cfg.manager_role_id)
        me = self.guild.me
        if (role is None or role.is_default() or role.managed or me is None
                or not me.guild_permissions.manage_roles or role >= me.top_role
                or role.permissions.administrator or role.permissions.manage_roles
                or role.permissions.manage_guild):
            raise UserError('Kampanjapäällikköroolin oikeudet tai roolijärjestys pitää korjata.')
        active = self.store.active_count(member_id) > 0
        if active and role not in member.roles:
            await member.add_roles(role, reason='EV27: hyväksytty kampanjapäällikkönimitys')
        elif not active and role in member.roles:
            await member.remove_roles(role, reason='EV27: ei voimassa olevia nimityksiä')

    async def reconcile_members(self):
        # Fetch a complete snapshot before mutating, so a partial network failure
        # never invalidates appointments for members whose page was not retrieved.
        members = {member.id: member async for member in self.guild.fetch_members(limit=None)}
        candidates = {member.id for member in members.values()
                      if any(role.id == self.cfg.candidate_role_id for role in member.roles)}
        affected = set()
        for appointment in self.store.appointments():
            c, m = appointment['candidate'], appointment['manager']
            affected.add(m)
            if c not in candidates or m not in members:
                self.store.revoke(c, m)
                self.store.audit(None, 'appointment_invalidated', candidate=c, manager=m)
        affected.update(member.id for member in members.values()
                        if any(role.id == self.cfg.manager_role_id for role in member.roles))
        for member_id in affected:
            try:
                await self.sync_manager(member_id)
            except (discord.HTTPException, UserError) as error:
                log.error('Manager role reconciliation failed for %s (%s)', member_id, type(error).__name__)

    async def on_member_remove(self, member):
        if member.guild.id == self.cfg.guild_id:
            async with self.service.lock:
                affected = {row['manager'] for row in self.store.appointments() if row['candidate'] == member.id}
                self.store.invalidate_member(member.id)
                self.store.audit(None, 'member_left', member=member.id)
                for manager in affected - {member.id}:
                    await self.sync_manager(manager)

    async def on_member_update(self, before, after):
        if before.guild.id != self.cfg.guild_id:
            return
        lost = (any(role.id == self.cfg.candidate_role_id for role in before.roles)
                and not any(role.id == self.cfg.candidate_role_id for role in after.roles))
        if lost:
            async with self.service.lock:
                affected = {row['manager'] for row in self.store.appointments() if row['candidate'] == after.id}
                self.store.invalidate_member(after.id, candidate_only=True)
                self.store.audit(None, 'candidate_role_removed', candidate=after.id)
                for manager in affected:
                    await self.sync_manager(manager)

    async def respond_to_invitation(self, interaction, token, accepted):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.service.lock:
                invitation = self.store.invitation(token)
                if (invitation is None or interaction.guild_id is not None
                        or interaction.user.id != invitation['manager']
                        or not interaction.message
                        or (invitation['message_id'] is not None
                            and invitation['message_id'] != interaction.message.id)):
                    raise UserError('Kutsu ei ole voimassa tai sitä ei ole osoitettu sinulle.')
                candidate, manager = invitation['candidate'], invitation['manager']
                state = self.store.state(candidate, manager)
                if state not in {'pending', 'active'}:
                    raise UserError('Nimitys ei ole enää voimassa.')
                if not await self.is_candidate(candidate) or not await self.is_member(manager):
                    self.store.revoke(candidate, manager)
                    await self.sync_manager(manager)
                    raise UserError('Nimitys ei ole enää voimassa. Palvelimen jäsenyys tai ehdokasrooli puuttuu.')
                if accepted:
                    if state == 'pending':
                        self.store.accept(candidate, manager)
                        self.store.audit(manager, 'manager_accepted', candidate=candidate)
                    await self.sync_manager(manager)
                    message = 'Nimitys hyväksytty ja kampanjapäällikkörooli annettu.'
                else:
                    if state == 'active':
                        raise UserError('Nimitys on jo hyväksytty. Voit luopua tehtävästä komennolla `/ek27 poista`.')
                    self.store.revoke(candidate, manager)
                    self.store.audit(manager, 'manager_declined', candidate=candidate)
                    message = 'Nimitys hylätty. Kampanjapäällikköroolia ei annettu.'
            # Failure to update the DM never rolls back a recorded decision.
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                log.warning('Could not remove invitation controls')
            await reply(interaction, message)
        except UserError as error:
            await reply(interaction, str(error))
        except Exception as error:
            log.error('Invitation response failed (%s)', type(error).__name__)
            await reply(interaction, 'Toiminto epäonnistui. Yritä uudelleen; ylläpito tarkistaa roolien oikeudet.')

    async def choose_candidate(self, interaction, event_id, desired):
        try:
            self.check_context(interaction)
            await interaction.response.defer(ephemeral=True, thinking=True)
            row = self.store.event(event_id)
            if (not row or not interaction.message or row['message_id'] != interaction.message.id
                    or interaction.channel_id != self.cfg.channel_id):
                raise UserError('Tapahtumaviesti ei ole enää voimassa.')
            if not self.cfg.registration_enabled:
                raise UserError('Kalenteri-ilmoittautuminen ei ole vielä käytössä.')
            targets = await self.service.targets(interaction.user.id)
            if not targets:
                raise UserError('Vain ehdokas tai hänen hyväksymänsä kampanjapäällikkö voi ilmoittautua.')
            if len(targets) == 1:
                await self.participate(interaction, targets[0], event_id, desired)
            else:
                candidates = []
                for candidate in targets:
                    member = await self.member(candidate)
                    if member:
                        candidates.append((candidate, member.display_name))
                await reply(interaction, 'Kenen puolesta toimit?', view=CandidatePicker(
                    self, interaction.user.id, event_id, desired, candidates))
        except (UserError, CalendarError) as error:
            await reply(interaction, str(error) if isinstance(error, UserError)
                        else 'Kalenteriyhteys epäonnistui. Vahvistusta ei ole saatu; yritä myöhemmin.')
        except Exception as error:
            log.error('Event button failed (%s)', type(error).__name__)
            await reply(interaction, 'Toiminto epäonnistui. Yritä uudelleen.')

    async def participate(self, interaction, candidate, event_id, desired):
        try:
            self.check_context(interaction)
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            message = await self.service.register(interaction.user.id, candidate, event_id, desired)
            await reply(interaction, message)
        except (UserError, CalendarError) as error:
            await reply(interaction, str(error) if isinstance(error, UserError)
                        else 'Kalenteriyhteys epäonnistui. Vahvistusta ei ole saatu; yritä myöhemmin.')

    def install_commands(self):
        group = app_commands.Group(name='ek27', description='Ehdokkaat ja kampanjapäälliköt', guild_only=True)

        @group.command(name='kampanjapäällikkö', description='Valitse kampanjapäällikkö ja lähetä hänelle yksityinen kutsu')
        @app_commands.rename(henkilo='henkilö')
        @app_commands.describe(henkilo='Hae nimellä tai @käyttäjänimellä ja valitse palvelimen jäsen')
        async def nominate(interaction: discord.Interaction, henkilo: discord.Member):
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.service.lock:
                self.check_context(interaction)
                if not await self.is_candidate(interaction.user.id):
                    raise UserError('Vain vahvistettu ehdokas voi nimetä kampanjapäällikön.')
                if henkilo.bot or henkilo.id == interaction.user.id or not await self.is_member(henkilo.id):
                    raise UserError('Valitse toinen palvelimen jäsen, joka ei ole botti.')
                if self.store.state(interaction.user.id, henkilo.id) == 'active':
                    await reply(interaction, 'Tämä käyttäjä on jo kampanjapäällikkösi.')
                    return
                self.store.appoint(interaction.user.id, henkilo.id)
                token = self.store.invite(interaction.user.id, henkilo.id)
                self.store.audit(interaction.user.id, 'manager_nominated', manager=henkilo.id)
                # Show both recognisable names and unique account IDs in the DM.
                candidate = interaction.user
                embed = discord.Embed(title='Kutsu kampanjapäälliköksi', description=(
                    f'{discord.utils.escape_markdown(candidate.display_name)} '
                    f'(<@{candidate.id}>, ID {candidate.id}) on nimennyt sinut kampanjapäällikökseen.\n'
                    'Hyväksymällä saat kampanjapäällikköroolin ja oikeuden toimia ehdokkaan puolesta.'))
                embed.add_field(name='Palvelin', value=self.guild.name[:1024])
                embed.add_field(name='Sinulle', value=f'<@{henkilo.id}> (ID {henkilo.id})')
                try:
                    dm = await henkilo.send(embed=embed, view=InvitationView(self, token),
                                           allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as error:
                    # No silent fallback to role assignment or public acceptance.
                    self.store.revoke(candidate.id, henkilo.id)
                    self.store.audit(candidate.id, 'invitation_delivery_failed', manager=henkilo.id)
                    raise UserError('Yksityisviestiä ei voitu toimittaa. Pyydä valittua henkilöä sallimaan '
                                    'yksityisviestit tämän palvelimen jäseniltä ja suorita komento uudelleen. '
                                    'Nimitystä ei aktivoitu.') from error
                self.store.invitation_message(token, dm.id)
            await reply(interaction, f'Kutsu lähetetty käyttäjälle <@{henkilo.id}> (ID {henkilo.id}). '
                        'Hän hyväksyy tai hylkää nimityksen botin yksityisviestin painikkeilla.')

        @group.command(name='poista', description='Peru nimitys, luovu tehtävästä tai korjaa nimitys ylläpitäjänä')
        async def revoke(interaction: discord.Interaction, ehdokas: discord.Member, paallikko: discord.Member):
            await interaction.response.defer(ephemeral=True, thinking=True)
            self.check_context(interaction)
            if interaction.user.id not in {ehdokas.id, paallikko.id}:
                await self.require_admin(interaction)
            async with self.service.lock:
                self.store.revoke(ehdokas.id, paallikko.id)
                self.store.audit(interaction.user.id, 'manager_revoked', candidate=ehdokas.id, manager=paallikko.id)
                await self.sync_manager(paallikko.id)
            await reply(interaction, 'Nimitys poistettu. Rooli säilyy vain muiden voimassa olevien nimitysten perusteella.')

        @group.command(name='nimitykset', description='Näytä omat nimitykset ja hyväksyntää odottavat pyynnöt')
        async def appointments(interaction: discord.Interaction):
            self.check_context(interaction)
            rows = [row for row in self.store.appointments()
                    if interaction.user.id in {row['candidate'], row['manager']}]
            lines = [f"<@{row['candidate']}> → <@{row['manager']}>: "
                     + ('voimassa' if row['state'] == 'active' else 'odottaa hyväksyntää') for row in rows]
            # Keep each response below Discord's message length limit.
            if not lines:
                await reply(interaction, 'Ei nimityksiä.')
            else:
                for start in range(0, len(lines), 15):
                    await reply(interaction, '\n'.join(lines[start:start + 15]))

        @group.command(name='tunniste', description='Ylläpito: liitä ehdokas kalenterin ehdokastunnisteeseen')
        @app_commands.default_permissions(manage_guild=True)
        async def identity(interaction: discord.Interaction, ehdokas: discord.Member, kalenteritunniste: str):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.require_admin(interaction)
            if not kalenteritunniste.strip() or len(kalenteritunniste) > 100:
                raise UserError('Tunnisteen pituuden pitää olla 1–100 merkkiä.')
            async with self.service.lock:
                if not await self.is_candidate(ehdokas.id):
                    raise UserError('Käyttäjällä ei ole ehdokasroolia.')
                try:
                    self.store.map_identity(ehdokas.id, kalenteritunniste.strip())
                except (ValueError, StoreIntegrityError) as error:
                    raise UserError(str(error) if isinstance(error, ValueError)
                                    else 'Kalenteritunniste on jo liitetty toiseen käyttäjään.') from error
                self.store.audit(interaction.user.id, 'identity_mapped', candidate=ehdokas.id)
            await reply(interaction, 'Kalenteritunniste tallennettu.')

        @group.command(name='osallistu', description='Ilmoita ehdokas tapahtumaan tapahtumatunnisteella')
        async def register(interaction: discord.Interaction, tapahtuma: str, ehdokas: discord.Member):
            await self.participate(interaction, ehdokas.id, tapahtuma, True)

        @group.command(name='peru', description='Peru ehdokkaan tapahtumailmoittautuminen')
        async def cancel(interaction: discord.Interaction, tapahtuma: str, ehdokas: discord.Member):
            await self.participate(interaction, ehdokas.id, tapahtuma, False)

        @group.command(name='tila', description='Ylläpito: näytä toiminnan tila')
        @app_commands.default_permissions(manage_guild=True)
        async def status(interaction: discord.Interaction):
            await self.require_admin(interaction)
            await reply(interaction, f'EV27: {self.cfg.campaign} / {self.cfg.district}\n'
                        f'Kalenterihaku: {self.cfg.calendar_enabled}\nIlmoittautuminen: {self.cfg.registration_enabled}\n'
                        f'Keskeneräiset kalenteripyynnöt: {len(self.store.pending())}')

        @group.command(name='ohje', description='Näytä EV27-botin ohje')
        async def help_command(interaction: discord.Interaction):
            self.check_context(interaction)
            await reply(interaction, '**EV27**\nEhdokas: `/ek27 kampanjapäällikkö henkilö:@käyttäjä`\nHyväksy nimitys botin yksityisviestin painikkeella.\n'
                        'Nimitykset: `/ek27 nimitykset`\nPeru nimitys tai luovu tehtävästä: `/ek27 poista`\n'
                        'Ilmoittaudu tapahtumaviestin painikkeilla tai `/ek27 osallistu`.\n'
                        'Peru osallistuminen painikkeella tai `/ek27 peru`.\n'
                        'Käytä palvelimella tunnistettavaa nimeä: Etunimi Sukunimi.\n'
                        'Ehdokasroolit ja kalenteritunnisteet: ota yhteyttä ylläpitoon.')

        self.tree.add_command(group, guild=discord.Object(id=self.cfg.guild_id))

        @self.tree.error
        async def command_error(interaction, error):
            original = getattr(error, 'original', error)
            if isinstance(original, UserError):
                await reply(interaction, str(original))
            elif isinstance(original, CalendarError):
                await reply(interaction, 'Kalenteriyhteys epäonnistui. Vahvistusta ei ole saatu; yritä myöhemmin.')
            else:
                log.error('Command failed (%s)', type(original).__name__)
                await reply(interaction, 'Toiminto epäonnistui. Ylläpito tarkistaa oikeudet ja yhteydet; yritä myöhemmin.')

    def embed(self, event):
        timestamp = int(instant(event['start']).timestamp())
        embed = discord.Embed(title=event['title'], url=event['url'],
                              description=f"<t:{timestamp}:F>\n{discord.utils.escape_markdown(event['location'])}")
        embed.add_field(name='Tila', value=('Peruttu' if event['status'] == 'cancelled' else
                        'Ilmoittautuminen avoinna' if can_register(event) and self.cfg.registration_enabled
                        else 'Ilmoittautuminen suljettu / ei käytössä'))
        embed.set_footer(text='EV27 · ' + event['id'])
        return embed

    async def sync_events(self):
        events = await self.service.calendar.events()
        channel = self.guild.get_channel(self.cfg.channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise UserError('Tapahtumakanava puuttuu.')
        current_ids = {event['id'] for event in events}
        # An omitted event is unavailable, never implicitly cancelled or published again.
        for row in self.store.events():
            if row['id'] not in current_ids:
                old = json.loads(row['data'])
                old['registration_open'] = False
                old['_unavailable'] = True
                events.append(old)
        now = datetime.now(timezone.utc)
        for event in events:
            row = self.store.event(event['id'])
            if not event['public']:
                if row and row['message_id'] and row['message_id'] > 0:
                    try:
                        await channel.get_partial_message(row['message_id']).delete()
                    except discord.NotFound:
                        pass
                    self.store.message(event['id'], None)
                self.store.save_event(event)
                continue
            self.store.save_event(event)
            row = self.store.event(event['id'])
            message_id = row['message_id']
            view = EventView(self, event['id'], enabled=self.cfg.registration_enabled and
                             event['status'] != 'cancelled' and not event.get('_unavailable'))
            # Registration and cancellation are checked independently by the service.
            view.children[0].disabled = not (self.cfg.registration_enabled and can_register(event)
                                             and not event.get('_unavailable'))
            if message_id and message_id > 0:
                try:
                    await channel.get_partial_message(message_id).edit(embed=self.embed(event), view=view)
                except discord.NotFound:
                    self.store.message(event['id'], None)
                    message_id = None
            if message_id is None and event['status'] == 'scheduled' and instant(event['start']) > now and not event.get('_unavailable'):
                # At-most-once publication: an uncertain Discord send is held for admin review,
                # rather than risking duplicate campaign announcements after a restart.
                self.store.message(event['id'], -1)
                message = await channel.send(embed=self.embed(event), view=view)
                self.store.message(event['id'], message.id)
            if row['message_id'] == -1:
                log.warning('Event %s has an uncertain Discord publication; reconcile using documentation', event['id'])
            if event['status'] != 'scheduled' or event.get('_unavailable'):
                continue
            for days, kind in [(7, '7d'), (1, '24h')]:
                due = instant(event['start']) - timedelta(days=days)
                # Only this poll window is eligible; no replay after downtime.
                if due <= now < due + timedelta(seconds=self.cfg.poll_seconds):
                    if self.store.claim_reminder(event['id'], kind):
                        await channel.send(f'Muistutus ({kind}):', embed=self.embed(event))

    @tasks.loop(seconds=60)
    async def worker(self):
        async with self.service.lock:
            try:
                await self.reconcile_members()
            except Exception as error:
                log.error('Member reconciliation failed (%s)', type(error).__name__)
            if self.service.calendar:
                try:
                    await self.service.reconcile_operations()
                except Exception as error:
                    log.error('Registration reconciliation failed (%s)', type(error).__name__)
                try:
                    await self.sync_events()
                except Exception as error:
                    log.error('Calendar sync failed (%s)', type(error).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.wait_until_ready()


def main():
    logging.basicConfig(level=logging.INFO)
    cfg = Config.load()
    EV27Bot(cfg).run(cfg.token, log_handler=None)


if __name__ == '__main__':
    main()
