# Proposed EV27 calendar API (not an existing API)

This contract must be implemented in the calendar application before live integration
is enabled. The existing event creation API is unrelated. No calendar server source
is present in this repository. The test fixture implements this contract in memory.

All endpoints below are relative to `/api/v1/ev27`. Use HTTPS and a dedicated
`api-key` header credential restricted to the configured district/campaign and
candidate participation. Redirects are rejected. Invalid credentials return 403;
out-of-scope identities or events return 403/404. Never return another campaign's
records. The server validates candidate eligibility; a Discord identity mapping
alone is not evidence of calendar eligibility.

## Discovery

`GET /events?district=<id>&campaign=EV27`

Return HTTP 200 with a complete, non-paginated snapshot:

```json
{
  "events": [{
    "id": "event-123",
    "title": "Campaign day",
    "start": "2027-03-20T10:00:00+02:00",
    "location": "Market square",
    "url": "https://calendar.example/events/123",
    "district": "district-id",
    "campaign": "EV27",
    "status": "scheduled",
    "public": true,
    "registration_open": true,
    "registration_closes": "2027-03-19T18:00:00+02:00"
  }]
}
```

IDs are stable strings of at most 100 characters. Titles are at most 256 characters,
locations at most 1000. Timestamps must include timezone offsets. `public` means
approved for the configured campaign channel's audience, not necessarily the whole
internet. Both boolean fields are mandatory and must be JSON booleans.

Include cancelled and changed events previously advertised by this integration;
`status` is `scheduled` or `cancelled`. `registration_closes` may be null or omitted.
An absent event becomes unavailable in the bot, with registration disabled and no
reminders. Absence alone never implies cancellation. Restricted events use
`public: false`; the bot removes any previously published card.

`GET /events/{percent-encoded-event-id}?district=<id>&campaign=EV27`

Return `{"event": {...}}` with the same fields. This endpoint supplies live state
before each registration or cancellation. Return 404 for unavailable events.

## Participation operations

`PUT /operations/{uuid}`

```json
{
  "event_id": "event-123",
  "candidate_id": "candidate-456",
  "registered": true,
  "district": "district-id",
  "campaign": "EV27"
}
```

`registered: false` cancels participation. Apply the desired state atomically with
a unique registration per event/candidate. Repeated registrations and cancellations
are successful no-ops when the desired state already holds.

The UUID is an idempotency key, persisted by the bot before transmission. Persist
its payload and result atomically with the participation change. An identical
retry returns the same operation; a different payload for the UUID returns 409.
Do not queue an operation without first persisting its status. Validate scope,
candidate eligibility, capacity, deadline and event status on the server. A rejected
business operation must be recorded as `failed` and returned with HTTP 200 so the
bot can distinguish rejection from an uncertain transport failure.

Return HTTP 200 for terminal results or 202 for pending:

```json
{
  "id": "operation-uuid",
  "event_id": "event-123",
  "candidate_id": "candidate-456",
  "registered": true,
  "state": "confirmed"
}
```

States: `pending`, `confirmed`, `failed`. `registered` echoes the **requested** state,
including for failed operations. Confirmation means the participation change has
committed; it is never an optimistic acknowledgement.

`GET /operations/{uuid}` returns the same envelope. It must be read-after-write
consistent. For an unknown operation return HTTP 200 with
`{"id":"operation-uuid","state":"not_found"}`. A previously accepted operation
must never become `not_found`. Operation records must outlive the campaign.
The bot can retry an unknown request with its original UUID after reauthorisation.
It never automatically resubmits pending writes in the background.

The server also owns canonical attendance lookup for organisers (outside this bot's
UI). The bot uses operation lookup to reconcile each request and retains request
history, not a separate authoritative attendance list.

## Rollout

Implement and test these endpoints in the calendar repository. Verify filters,
credential scope, candidate identifiers, deadline/capacity handling, atomic
idempotency and request status recovery in staging. Enable calendar discovery first,
then participant registration using the separate configuration flags. Do not point
the adapter at an assumed participant endpoint on the production calendar.
