from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    candidate_role_id: int
    manager_role_id: int
    channel_id: int
    district: str
    campaign: str
    database: str
    calendar_url: str
    calendar_key: str
    calendar_enabled: bool = False
    registration_enabled: bool = False
    poll_seconds: int = 60

    @classmethod
    def load(cls):
        # Never pick up the shared bot's .env file.
        load_dotenv(Path(__file__).resolve().parents[1] / '.env')
        def required(name):
            value = os.environ.get('EV27_' + name, '').strip()
            if not value:
                raise ValueError(f'EV27_{name} is required')
            return value
        def flag(name):
            value = os.environ.get('EV27_' + name, 'false').lower()
            if value not in {'true', 'false'}:
                raise ValueError(f'EV27_{name} must be true or false')
            return value == 'true'
        enabled = flag('CALENDAR_ENABLED')
        registration = flag('REGISTRATION_ENABLED')
        if registration and not enabled:
            raise ValueError('Registration requires calendar discovery')
        url = os.environ.get('EV27_CALENDAR_URL', '').rstrip('/')
        key = os.environ.get('EV27_CALENDAR_KEY', '')
        if enabled:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('EV27_CALENDAR_URL must be an HTTPS URL without credentials')
            if not key:
                raise ValueError('EV27_CALENDAR_KEY is required')
        ids = [int(required(name)) for name in
               ('GUILD_ID', 'CANDIDATE_ROLE_ID', 'MANAGER_ROLE_ID', 'CHANNEL_ID')]
        if (any(value <= 0 for value in ids) or ids[1] == ids[2]
                or ids[0] in ids[1:3]):
            raise ValueError('IDs must be positive; candidate and manager roles must differ and cannot be @everyone')
        poll = int(os.environ.get('EV27_POLL_SECONDS', '60'))
        if poll < 15:
            raise ValueError('Poll interval must be at least 15 seconds')
        return cls(required('TOKEN'), *ids, required('DISTRICT'),
                   os.environ.get('EV27_CAMPAIGN', 'EV27'),
                   os.environ.get('EV27_DATABASE', 'data/ev27.sqlite3'),
                   url, key, enabled, registration, poll)
