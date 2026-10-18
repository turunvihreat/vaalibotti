"""MySQL 8 schema, kept separate from the existing SQLite schema.

A nullable generated column provides SQLite's partial unique index semantics:
only pending operations reserve an event/candidate pair. Terminal history can
contain multiple rows. Binary collation preserves case-sensitive calendar IDs.
"""

_TABLES = (
    '''CREATE TABLE IF NOT EXISTS metadata (
        `key` VARCHAR(100) PRIMARY KEY, value VARCHAR(255) NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS appointments (
        candidate BIGINT NOT NULL, manager BIGINT NOT NULL,
        state ENUM('pending','active','revoked') NOT NULL,
        PRIMARY KEY(candidate,manager), INDEX appointments_manager(manager,state))''',
    '''CREATE TABLE IF NOT EXISTS invitations (
        token VARCHAR(32) PRIMARY KEY, candidate BIGINT NOT NULL,
        manager BIGINT NOT NULL, message_id BIGINT,
        UNIQUE KEY invitation_pair(candidate,manager))''',
    '''CREATE TABLE IF NOT EXISTS identities (
        discord_id BIGINT PRIMARY KEY, calendar_id VARCHAR(100) NOT NULL UNIQUE)''',
    '''CREATE TABLE IF NOT EXISTS events (
        id VARCHAR(100) PRIMARY KEY, data LONGTEXT NOT NULL, message_id BIGINT)''',
    '''CREATE TABLE IF NOT EXISTS reminders (
        event_id VARCHAR(100) NOT NULL, kind VARCHAR(10) NOT NULL,
        PRIMARY KEY(event_id,kind))''',
    '''CREATE TABLE IF NOT EXISTS operations (
        id VARCHAR(36) PRIMARY KEY, event_id VARCHAR(100) NOT NULL,
        candidate BIGINT NOT NULL, calendar_id VARCHAR(100) NOT NULL,
        actor BIGINT NOT NULL, desired TINYINT NOT NULL,
        state ENUM('pending','confirmed','failed') NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        pending_slot TINYINT GENERATED ALWAYS AS
            (CASE WHEN state='pending' THEN 1 ELSE NULL END) STORED,
        UNIQUE KEY one_pending_operation(event_id,candidate,pending_slot),
        INDEX operations_candidate(candidate,state), INDEX operations_state(state))''',
    '''CREATE TABLE IF NOT EXISTS audit (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor BIGINT, action VARCHAR(100) NOT NULL, details LONGTEXT NOT NULL)''',
)

SCHEMA = tuple(statement + ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin'
               for statement in _TABLES)
