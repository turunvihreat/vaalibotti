"""Client for the proposed EV27 calendar API; see CALENDAR_API.md."""
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
import aiohttp


class CalendarError(Exception):
    """Request failed or response could not be trusted; no secrets in message."""


def instant(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timezone required')
    return dt.astimezone(timezone.utc)


def validate_event(value, district, campaign):
    if not isinstance(value, dict):
        raise ValueError('Event must be an object')
    for key in ('id', 'title', 'start', 'location', 'url', 'district', 'campaign', 'status'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f'Missing event field: {key}')
    if len(value['id']) > 100 or len(value['title']) > 256 or len(value['location']) > 1000:
        raise ValueError('Event field exceeds length limit')
    if value['district'] != district or value['campaign'] != campaign:
        raise ValueError('Calendar returned event outside configured scope')
    if value['status'] not in {'scheduled', 'cancelled'}:
        raise ValueError('Unknown event status')
    if type(value.get('public')) is not bool or type(value.get('registration_open')) is not bool:
        raise ValueError('Audience and registration flags are required')
    if urlparse(value['url']).scheme != 'https' or not urlparse(value['url']).netloc:
        raise ValueError('Calendar event URL must use HTTPS')
    instant(value['start'])
    if value.get('registration_closes') is not None:
        if not isinstance(value['registration_closes'], str):
            raise ValueError('Registration deadline must be a timestamp or null')
        instant(value['registration_closes'])
    return dict(value)


def can_register(event, now=None):
    now = now or datetime.now(timezone.utc)
    return (event['public'] and event['status'] == 'scheduled'
            and event['registration_open'] and instant(event['start']) > now
            and (not event.get('registration_closes') or instant(event['registration_closes']) > now))


class Calendar:
    def __init__(self, session, url, key, district, campaign):
        self.session, self.url = session, url.rstrip('/')
        self.headers = {'api-key': key}
        self.district, self.campaign = district, campaign

    async def request(self, method, path, **kwargs):
        try:
            async with self.session.request(method, self.url + '/api/v1/ev27' + path,
                                            headers=self.headers, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=15), **kwargs) as response:
                if response.status not in {200, 202}:
                    raise CalendarError(f'Calendar returned HTTP {response.status}')
                data = await response.json()
                if not isinstance(data, dict):
                    raise CalendarError('Calendar returned an invalid response')
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise CalendarError('Calendar request failed; result may be uncertain') from exc

    async def events(self):
        result = await self.request('GET', '/events', params={'district': self.district, 'campaign': self.campaign})
        try:
            items = result['events']
            if not isinstance(items, list):
                raise ValueError('Expected events array')
            events = [validate_event(item, self.district, self.campaign) for item in items]
            if len({item['id'] for item in events}) != len(events):
                raise ValueError('Duplicate event IDs')
            return events
        except (ValueError, KeyError, TypeError) as exc:
            raise CalendarError('Calendar returned an invalid event snapshot') from exc

    async def current_event(self, event_id):
        result = await self.request('GET', '/events/' + quote(event_id, safe=''),
                                    params={'district': self.district, 'campaign': self.campaign})
        try:
            event = validate_event(result['event'], self.district, self.campaign)
            if event['id'] != event_id:
                raise ValueError('Event ID mismatch')
            return event
        except (ValueError, KeyError, TypeError) as exc:
            raise CalendarError('Calendar returned invalid event details') from exc

    async def submit(self, op):
        return await self.request('PUT', '/operations/' + op['id'], json={
            'event_id': op['event_id'], 'candidate_id': op['calendar_id'],
            'registered': bool(op['desired']), 'district': self.district, 'campaign': self.campaign,
        })

    async def operation(self, op):
        return await self.request('GET', '/operations/' + op['id'])

    @staticmethod
    def result(op, result):
        # An unknown key has no stored payload yet; never treat this as success.
        if result.get('id') == op['id'] and result.get('state') == 'not_found':
            return 'not_found'
        # Bind confirmation to the exact request, including the desired state.
        if (result.get('id') != op['id'] or result.get('event_id') != op['event_id']
                or result.get('candidate_id') != op['calendar_id']
                or type(result.get('registered')) is not bool
                or result['registered'] != bool(op['desired'])
                or result.get('state') not in {'pending', 'confirmed', 'failed', 'not_found'}):
            raise CalendarError('Calendar operation response does not match request')
        return result['state']
