"""Small, isolated SQLite store. One bot process owns each database."""
import json
from pathlib import Path
import sqlite3
import uuid


class Store:
    def __init__(self, path):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS appointments (
                candidate INTEGER NOT NULL, manager INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','active','revoked')),
                PRIMARY KEY(candidate, manager));
            CREATE TABLE IF NOT EXISTS invitations (
                token TEXT PRIMARY KEY, candidate INTEGER NOT NULL,
                manager INTEGER NOT NULL, message_id INTEGER,
                UNIQUE(candidate,manager));
            CREATE TABLE IF NOT EXISTS identities (
                discord_id INTEGER PRIMARY KEY, calendar_id TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, data TEXT NOT NULL, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS reminders (
                event_id TEXT NOT NULL, kind TEXT NOT NULL,
                PRIMARY KEY(event_id, kind));
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL, candidate INTEGER NOT NULL,
                calendar_id TEXT NOT NULL, actor INTEGER NOT NULL,
                desired INTEGER NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_operation
                ON operations(event_id,candidate) WHERE state='pending';
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor INTEGER, action TEXT NOT NULL, details TEXT NOT NULL);
            PRAGMA user_version=2;
        ''')

    def bind_guild(self, guild_id):
        value = str(guild_id)
        self.execute("INSERT OR IGNORE INTO metadata VALUES('guild_id',?)", (value,))
        stored = self.rows("SELECT value FROM metadata WHERE key='guild_id'")[0]['value']
        if stored != value:
            raise ValueError('This EV27 database belongs to another Discord server')

    def rows(self, query, args=()):
        return [dict(row) for row in self.conn.execute(query, args).fetchall()]

    def execute(self, query, args=()):
        with self.conn:
            return self.conn.execute(query, args)

    def audit(self, actor, action, **details):
        self.execute('INSERT INTO audit(actor,action,details) VALUES(?,?,?)',
                     (actor, action, json.dumps(details, ensure_ascii=False)))

    def appoint(self, candidate, manager):
        self.execute('''INSERT INTO appointments VALUES(?,?,'pending')
            ON CONFLICT(candidate,manager) DO UPDATE SET state='pending'
            WHERE appointments.state='revoked' ''', (candidate, manager))

    def state(self, candidate, manager):
        rows = self.rows('SELECT state FROM appointments WHERE candidate=? AND manager=?', (candidate, manager))
        return rows[0]['state'] if rows else None

    def invite(self, candidate, manager):
        if self.state(candidate, manager) != 'pending':
            raise ValueError('Invitation requires a pending appointment')
        token = uuid.uuid4().hex
        self.execute("""INSERT INTO invitations(token,candidate,manager) VALUES(?,?,?)
            ON CONFLICT(candidate,manager) DO UPDATE SET token=excluded.token,message_id=NULL""",
                     (token, candidate, manager))
        return token

    def invitation(self, token):
        rows = self.rows('SELECT * FROM invitations WHERE token=?', (token,))
        return rows[0] if rows else None

    def invitations(self):
        return self.rows("""SELECT i.* FROM invitations i JOIN appointments a
            ON a.candidate=i.candidate AND a.manager=i.manager
            WHERE a.state IN ('pending','active')""")

    def invitation_message(self, token, message_id):
        self.execute('UPDATE invitations SET message_id=? WHERE token=?', (message_id, token))

    def accept(self, candidate, manager):
        return self.execute("UPDATE appointments SET state='active' WHERE candidate=? AND manager=? AND state='pending'",
                            (candidate, manager)).rowcount == 1

    def revoke(self, candidate, manager):
        self.execute("UPDATE appointments SET state='revoked' WHERE candidate=? AND manager=?", (candidate, manager))
        self.execute('DELETE FROM invitations WHERE candidate=? AND manager=?', (candidate, manager))

    def appointments(self):
        return self.rows("SELECT * FROM appointments WHERE state != 'revoked'")

    def active_count(self, manager):
        return self.rows("SELECT COUNT(*) AS n FROM appointments WHERE manager=? AND state='active'", (manager,))[0]['n']

    def invalidate_member(self, member, candidate_only=False):
        where = 'candidate=?' if candidate_only else 'candidate=? OR manager=?'
        args = (member,) if candidate_only else (member, member)
        self.execute(f"UPDATE appointments SET state='revoked' WHERE {where}", args)
        self.execute(f'DELETE FROM invitations WHERE {where}', args)

    def targets(self, actor):
        return [row['candidate'] for row in self.rows(
            "SELECT candidate FROM appointments WHERE manager=? AND state='active'", (actor,))]

    def map_identity(self, member, calendar_id):
        if self.rows("SELECT id FROM operations WHERE candidate=? AND state='pending'", (member,)):
            raise ValueError('Keskeneräinen ilmoittautuminen estää tunnisteen vaihtamisen.')
        old = self.identity(member)
        if old and old != calendar_id and self.rows('SELECT id FROM operations WHERE candidate=?', (member,)):
            raise ValueError('Käytettyä kalenteritunnistetta ei voi vaihtaa; korjaa ensin kalenterin tiedot.')
        self.execute('INSERT INTO identities VALUES(?,?) ON CONFLICT(discord_id) DO UPDATE SET calendar_id=excluded.calendar_id',
                     (member, calendar_id))

    def identity(self, member):
        rows = self.rows('SELECT calendar_id FROM identities WHERE discord_id=?', (member,))
        return rows[0]['calendar_id'] if rows else None

    def save_event(self, event):
        self.execute('INSERT INTO events(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                     (event['id'], json.dumps(event)))

    def events(self):
        return self.rows('SELECT * FROM events')

    def event(self, event_id):
        rows = self.rows('SELECT * FROM events WHERE id=?', (event_id,))
        return rows[0] if rows else None

    def message(self, event_id, message_id):
        self.execute('UPDATE events SET message_id=? WHERE id=?', (message_id, event_id))

    def claim_reminder(self, event_id, kind):
        return self.execute('INSERT OR IGNORE INTO reminders VALUES(?,?)', (event_id, kind)).rowcount == 1

    def pending(self, event_id=None, candidate=None):
        if event_id is None:
            return self.rows("SELECT * FROM operations WHERE state='pending'")
        rows = self.rows("SELECT * FROM operations WHERE event_id=? AND candidate=? AND state='pending'", (event_id, candidate))
        return rows[0] if rows else None

    def begin(self, event_id, candidate, calendar_id, actor, desired):
        existing = self.pending(event_id, candidate)
        if existing:
            return existing
        op = dict(id=str(uuid.uuid4()), event_id=event_id, candidate=candidate,
                  calendar_id=calendar_id, actor=actor, desired=int(desired), state='pending')
        self.execute('INSERT INTO operations(id,event_id,candidate,calendar_id,actor,desired,state) VALUES(?,?,?,?,?,?,?)',
                     tuple(op.values()))
        return op

    def finish(self, operation, state):
        if state not in {'confirmed', 'failed'}:
            raise ValueError('Invalid terminal operation state')
        self.execute('UPDATE operations SET state=? WHERE id=?', (state, operation))

    def close(self):
        self.conn.close()
