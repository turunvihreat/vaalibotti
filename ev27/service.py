"""Candidate authority and registration, independent of Discord UI."""
import asyncio
import logging

from .calendar import Calendar, CalendarError, can_register


class UserError(Exception):
    pass


class Service:
    def __init__(self, store, calendar, candidate_check, member_check, registration_enabled=False):
        self.store, self.calendar = store, calendar
        self.candidate_check, self.member_check = candidate_check, member_check
        self.registration_enabled = registration_enabled
        # Serialises role changes, registration and periodic reconciliation.
        self.lock = asyncio.Lock()

    async def authorised(self, actor, candidate):
        if not await self.candidate_check(candidate) or not await self.member_check(actor):
            return False
        return actor == candidate or self.store.state(candidate, actor) == 'active'

    async def targets(self, actor):
        ids = set(self.store.targets(actor))
        ids.add(actor)
        return [candidate for candidate in sorted(ids) if await self.authorised(actor, candidate)]

    async def register(self, actor, candidate, event_id, desired):
        async with self.lock:
            if not self.registration_enabled or self.calendar is None:
                raise UserError('Kalenteri-ilmoittautuminen ei ole vielä käytössä.')
            if not await self.authorised(actor, candidate):
                raise UserError('Sinulla ei ole oikeutta toimia tämän ehdokkaan puolesta.')
            identity = self.store.identity(candidate)
            if not identity:
                raise UserError('Ehdokkaan kalenteritunniste puuttuu. Ota yhteyttä ylläpitoon.')
            # Fetch live details: a cached card is not proof registration is still open.
            event = await self.calendar.current_event(event_id)
            if not event['public']:
                raise UserError('Tapahtuma ei ole tämän kanavan käytettävissä.')
            op = self.store.pending(event_id, candidate)
            if op:
                state = Calendar.result(op, await self.calendar.operation(op))
                if state in {'confirmed', 'failed'}:
                    self.store.finish(op['id'], state)
                    if state == 'confirmed' and bool(op['desired']) == desired:
                        return 'Ilmoittautuminen vahvistettu.' if desired else 'Ilmoittautuminen peruttu.'
                    op = None
                elif state == 'pending' or bool(op['desired']) != desired:
                    raise UserError('Aiemman pyynnön tulosta selvitetään. Yritä myöhemmin uudelleen.')
                # not_found + same desired: safely resend the same operation ID.
            if op is None:
                if desired and not can_register(event):
                    raise UserError('Tapahtuman ilmoittautuminen on suljettu.')
                op = self.store.begin(event_id, candidate, identity, actor, desired)
                self.store.audit(actor, 'registration_requested', operation=op['id'], candidate=candidate, desired=desired)
            state = Calendar.result(op, await self.calendar.submit(op))
            if state in {'confirmed', 'failed'}:
                self.store.finish(op['id'], state)
                self.store.audit(actor, 'registration_' + state, operation=op['id'])
            if state == 'confirmed':
                return 'Ilmoittautuminen vahvistettu.' if desired else 'Ilmoittautuminen peruttu.'
            if state == 'failed':
                raise UserError('Kalenteri hylkäsi pyynnön. Ota yhteyttä järjestäjään.')
            raise UserError('Pyyntö odottaa kalenterin vahvistusta. Tarkista tilanne myöhemmin.')

    async def reconcile_operations(self):
        for op in self.store.pending():
            try:
                state = Calendar.result(op, await self.calendar.operation(op))
            except CalendarError:
                logging.getLogger('ev27').warning('Operation %s remains uncertain', op['id'])
                continue
            if state in {'confirmed', 'failed'}:
                self.store.finish(op['id'], state)
                self.store.audit(None, 'registration_reconciled', operation=op['id'], state=state)
