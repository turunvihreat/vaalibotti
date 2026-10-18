"""Local HTTP test fixture for the proposed contract; not a production calendar."""
from aiohttp import web


class FakeCalendar:
    def __init__(self, events):
        self.events = {item['id']: item for item in events}
        self.operations = {}
        self.attendance = set()
        self.calls = []
        self.pending = False
        self.fail = False
        self.app = web.Application()
        self.app.router.add_get('/api/v1/ev27/events', self.list_events)
        self.app.router.add_get('/api/v1/ev27/events/{event_id}', self.event)
        self.app.router.add_put('/api/v1/ev27/operations/{operation}', self.submit)
        self.app.router.add_get('/api/v1/ev27/operations/{operation}', self.operation)
        self.runner = None

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

    async def close(self):
        await self.runner.cleanup()

    def check(self, request):
        if request.headers.get('api-key') != 'test-key':
            raise web.HTTPForbidden()
        if self.fail:
            raise web.HTTPServiceUnavailable()

    async def list_events(self, request):
        self.check(request)
        items = [e for e in self.events.values() if e['district'] == request.query.get('district')
                 and e['campaign'] == request.query.get('campaign')]
        return web.json_response({'events': items})

    async def event(self, request):
        self.check(request)
        event = self.events.get(request.match_info['event_id'])
        if not event or event['district'] != request.query.get('district') or event['campaign'] != request.query.get('campaign'):
            raise web.HTTPNotFound()
        return web.json_response({'event': event})

    async def submit(self, request):
        self.check(request)
        op_id = request.match_info['operation']
        payload = await request.json()
        self.calls.append((op_id, payload))
        existing = self.operations.get(op_id)
        if existing:
            if any(existing[k] != payload[k] for k in ('event_id', 'candidate_id', 'registered')):
                raise web.HTTPConflict()
            return web.json_response(existing)
        event = self.events.get(payload['event_id'])
        failed = (not event or (payload['registered'] and
                  (not event['registration_open'] or event['status'] == 'cancelled')))
        result = dict(id=op_id, event_id=payload['event_id'], candidate_id=payload['candidate_id'],
                      registered=payload['registered'], state='failed' if failed else 'pending' if self.pending else 'confirmed')
        self.operations[op_id] = result
        if result['state'] == 'confirmed':
            pair = (payload['event_id'], payload['candidate_id'])
            if payload['registered']:
                self.attendance.add(pair)
            else:
                self.attendance.discard(pair)
        return web.json_response(result, status=202 if self.pending else 200)

    async def operation(self, request):
        self.check(request)
        op_id = request.match_info['operation']
        return web.json_response(self.operations.get(op_id, {'id': op_id, 'state': 'not_found'}))
