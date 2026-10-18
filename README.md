# Standalone EV27 candidate bot

This service manages candidate–campaign manager appointments, Discord access roles,
EV27 calendar event cards, reminders and candidate participation. It does not import,
modify or share credentials/databases with the existing tapahtumabotti. General
event creation remains in that bot. Python 3.11+ is required.

## Discord preparation

Create a **separate Discord application and bot token**. Enable **Server Members
Intent** in the Developer Portal; Message Content Intent is not required. Invite
with `bot` and `applications.commands` scopes. Grant View Channels, Send Messages,
Embed Links, Read Message History and Manage Roles. Do not grant Administrator.
Place its role above `EV27 Kampanjapäällikkö`. That role must be dedicated to this
bot; reconciliation removes manually granted manager roles without appointments.

Manually maintain `EV27 Ehdokas`, staff and working-group roles. Create category
`EDUSKUNTAVAALIT 2027` and these channels:

| Channel | Intended permissions |
| --- | --- |
| `ev27-tiedotus` | Campaign participants read; election organisation/staff write |
| `ev27-kysy` | Campaign participants ask and answer |
| `ev27-keskustelu` | Campaign participants discuss |
| `ev27-katukampanja` | Campaign coordination and bot event cards |
| `ev27-ehdokkaat` | Candidates and active campaign managers only, plus authorised staff |
| `ev27-paneelit` | Panel working group and explicitly authorised personnel |

Check all existing server roles and channel overwrites: another role's allow can
grant access despite a deny on a different role. Server administrators bypass
channel restrictions. Test effective access with representative accounts. Do not
grant manager-role permissions that allow managing the server or other roles.
Give the bot access only to its event/command channels. Moderators handle recognisable
server nicknames (`Etunimi Sukunimi`). Add future channels only as needed.

## Local installation

From the standalone EV27 repository root:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Fill in .env with the new bot's token and Discord IDs.
.venv/bin/python -m ev27.bot
```

The bot reads only this repository’s `.env`, never the shared bot's `.env`. Environment variables
override that file. Copy IDs with Discord Developer Mode enabled. Set the district
identifier to the calendar's district ID. One deployment/database belongs to one
server; do not change server IDs on a populated database. Both calendar flags are
false by default. Access management works without calendar credentials.

For Docker, from the repository root:

```sh
docker compose up --build -d
```

The Docker build excludes secrets, the shared bot and local databases. The named
volume persists `/data` when using SQLite. MySQL stores all state on the database
server and does not require a local database volume. Run **one replica only** per
Discord application/database.
Stop the service before copying its SQLite database for backups (include the WAL
and SHM files if copying a live database). Protect database backups and environment
files: they contain identities, appointments, participation history and credentials.
Set an organisational retention policy and delete campaign data when no longer needed.

## MySQL deployment

MySQL 8.0+ is supported alongside SQLite. Create a dedicated EV27 database and
account on your existing MySQL server; do not point EV27 at tapahtumabotti's
schema. The account needs SELECT, INSERT, UPDATE, DELETE and CREATE privileges
on the EV27 database. Tables and indexes are created automatically at startup.

Set the following secret environment variable on the EV27 container (or in the
repository-root `.env` for local use):

```dotenv
EV27_DATABASE_URL=mysql+pymysql://ev27_user:URL_ENCODED_PASSWORD@mysql-host:3306/ev27?charset=utf8mb4
```

`EV27_DATABASE_URL` takes precedence over `EV27_DATABASE`, which remains the
SQLite path setting. A MySQL URL in `EV27_DATABASE` is also accepted. The shared
bot's `DATABASE_URL` is never read. Percent-encode special characters in the
username/password, including `@`, `:`, `/`, `#` and `%`. For servers requiring TLS,
append `&ssl_ca=/path/to/ca.pem&ssl_verify_cert=true&ssl_verify_identity=true` and
make the CA file available inside the container. The hostname must match the
server certificate. Configure the EV27 container's network access to MySQL.

In an ACI YAML deployment, add this to EV27's `environmentVariables`:

```yaml
- name: EV27_DATABASE_URL
  secureValue: "<EV27 MySQL connection URL supplied at deployment>"
```

Keep actual credentials out of committed deployment files. Rebuild the image to
include the MySQL driver before deploying this setting. Use your MySQL backup
policy for the EV27 database. Existing SQLite data is not automatically migrated;
switching backends starts with a separate database.

## GitHub image builds

`.github/workflows/build-ev27.yml` runs the EV27 tests and builds the standalone
image on pull requests and pushes to `main`. Pull requests build without publishing.
Pushes to `main` publish to `ghcr.io/<owner>/<repository>-ev27`, with `latest` and
`sha-<short-commit>` tags. The workflow can also be run manually; only runs on
`main` publish. It uses the built-in `GITHUB_TOKEN`; no Discord, calendar or Azure
credentials are needed for the build.

For this repository the image is
`ghcr.io/turunvihreat/vaalibotti-ev27:latest`. Use the SHA tag to select
a commit-specific build for Azure Container Instances. Add it as a second container
alongside tapahtumabotti, with separate EV27 environment variables, secrets and
persistent database storage. Configure registry pull access in Azure if the GHCR
package is private. The workflow builds/publishes images only; it does not change
the Azure deployment or the existing tapahtumabotti image workflow.

## Commands

Command responses are private. Commands are registered only in the configured server.
Nomination sends a direct message to the selected member.

| Command | Purpose |
| --- | --- |
| `/ek27 kampanjapäällikkö henkilö` | Candidate selects a server member; bot sends a private invitation |
| `/ek27 nimitykset` | List own active/pending appointments |
| `/ek27 poista ehdokas paallikko` | Candidate revokes, manager relinquishes, or administrator corrects |
| `/ek27 tunniste ehdokas kalenteritunniste` | Administrator maps a verified candidate to a calendar ID |
| `/ek27 osallistu tapahtuma ehdokas` | Register a candidate by stable event ID |
| `/ek27 peru tapahtuma ehdokas` | Cancel participation |
| `/ek27 tila` | Administrator checks configuration and pending operations |
| `/ek27 ohje` | Finnish user guidance |

Candidates may have multiple managers; managers may represent multiple candidates.
Nomination does not grant access until accepted. Type
`/ek27 kampanjapäällikkö`, select the `henkilö` field, then search for the member by
name or @username in Discord's native member picker. Select the actual account;
matching names alone do not determine the recipient. The candidate's private
confirmation includes the selected member's mention and unique Discord ID.

The bot sends the nominee a DM identifying the candidate and server, with
**Hyväksy nimitys** and **Hylkää nimitys** buttons. Only that nominee may use the
invitation. Acceptance rechecks current membership and candidate status before
granting the role. Buttons survive restarts; revoking or replacing a nomination
invalidates its earlier invitation. No acceptance slash command is needed.

If Discord blocks the DM, the candidate receives instructions to ask the nominee
to enable DMs from server members and run the nomination command again. A failed
DM does not activate the appointment. Existing pending nominations created before
this update need the candidate to run the nomination command to send an invitation.

Managers register the **candidate**,
not themselves. The event button offers a paginated candidate selector if needed.

The database is the source of appointment authority, with live Discord membership
and candidate-role checks on event actions. Removing an appointment removes event
authority immediately; role changes are retried by reconciliation if Discord fails.
Member departures and candidate-role removals invalidate appointments. Rejoining
or restoring the role does not revive invalidated appointments. Changes while the
bot is offline are reconciled against current membership at startup; an entire
leave-and-rejoin or role-remove-and-restore cycle while offline cannot be inferred
from Discord's current snapshot. Administrators should revoke such appointments.

## Calendar integration and recovery

See [CALENDAR_API.md](CALENDAR_API.md). These are proposed endpoints, **not verified
features of the production calendar**. Calendar server changes are outside this
repository. After verifying staging, enable `EV27_CALENDAR_ENABLED=true` with a
separate HTTPS URL/API credential, then `EV27_REGISTRATION_ENABLED=true`.

Operation IDs survive crashes. Network failures do not produce success messages.
The background worker reads operation status without replaying writes. If a request
was never received, repeat the same action to resend the same ID. Resolve an earlier
uncertain action before submitting its opposite. Administrator mapping changes are
blocked once an identity has been used, preventing accidental orphaned attendance.

Reminders occur in the poll window seven days and 24 hours before an event, with
no catch-up after downtime. A reminder is claimed before sending: uncertain Discord
sends can lose a reminder, but cannot generate repeated reminders. Rescheduling an
event does not reset already delivered reminder kinds. Discord renders event times
in each reader's local timezone, including Finnish daylight-saving transitions.

Event cards are edited as the calendar changes. Restricted events' cards are removed.
A failed fetch does not erase the previous snapshot; live checks still protect
registration. Each sync failure is logged without API keys or raw calendar bodies.
Inspect logs and `/ek27 tila`; unexpected pending operations need calendar-side checks.

Card publication is also claimed before transmission. If Discord times out after
possibly accepting a message, the event has `message_id=-1` and is not automatically
reposted. Stop the bot, inspect the channel for a card with footer `EV27 · <event ID>`,
and use your database client to set that event's `message_id` to the existing
message ID. If no card was delivered, set it to NULL to permit a new publication. Restart afterwards.
This deliberate recovery step prevents duplicate election announcements.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The suite uses temporary databases, Discord doubles and a local in-memory calendar
HTTP fixture. CI also runs the persistence and calendar integration tests against
MySQL 8.0. To run these locally, provide a disposable MySQL server and a test account
with CREATE/DROP DATABASE privileges:

```sh
EV27_TEST_MYSQL_URL='mysql+pymysql://test_user:password@127.0.0.1:3306/ev27_test' \
  .venv/bin/python -m unittest discover -s tests -v
```

Each MySQL test creates and drops its own randomly named `ev27_test_*` database.
Without this variable, MySQL tests are skipped and the SQLite suite still runs.
No real Discord or calendar credentials are needed. Before production, verify role hierarchy and effective channel permissions with candidate, manager,
ordinary campaign member and panel-group accounts; test against the real staging
calendar before enabling live participant registration.
