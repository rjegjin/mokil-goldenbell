"""Mokil Golden Bell: up to 70 real-time students in one classroom game room.

Run: TEACHER_PASSWORD='a-long-private-password' python server.py
Install: pip install -r requirements.txt
No database needed; room state is volatile and stays in this single Python process.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

LOG = logging.getLogger('mokil')
MAX_STUDENTS = 70
MAX_MESSAGE_BYTES = 4096
SNAPSHOT_INTERVAL = .25
W, H, SPEED = 1280, 820, 230
ZONES = {
    'ox': {'O': (160, 320, 440, 370), 'X': (680, 320, 440, 370)},
    'mc': {'1': (160, 305, 440, 180), '2': (680, 305, 440, 180),
           '3': (160, 525, 440, 180), '4': (680, 525, 440, 180)},
}
EMOTES = {'wave', 'bow', 'clap', 'cheer', 'dance', 'jump', 'heart', 'laugh'}
ROOM_RE = re.compile(r'^[0-9]{3,6}$')


def ms():
    return int(time.time() * 1000)


def game_default():
    return {
        'phase': 'lobby', 'round': 0, 'q': '', 'dur': 15, 'endAt': 0,
        'answer': None, 'exp': '', 'out': {}, 'lastOut': {}, 'late': {},
        'allRevive': False, 'survivors': None, 'outCount': 0,
        'winners': None, 'winnerIds': {}, 'autoEndAt': 0, 'notice': None,
        'closed': 0, 'picks': {}, 'pickN': {}, 'lockOut': 0,
        'qtype': 'ox', 'opts': [],
    }


def in_zone(zone, x, y):
    zx, zy, zw, zh = zone
    return zx < x < zx + zw and zy < y < zy + zh


def checked_position(data):
    if not isinstance(data, dict):
        return None
    x, y = data.get('x'), data.get('y')
    if (not isinstance(x, (int, float)) or isinstance(x, bool)
            or not isinstance(y, (int, float)) or isinstance(y, bool)):
        return None
    if not (0 <= x <= W and 0 <= y <= H):
        return None
    return round(x), round(y)


class Room:
    def __init__(self, code):
        self.code = code
        self.clients = set()
        self.students = {}  # active student ID => ws
        self.sessions = {}  # opaque resume token => {id, ws, timer}; 35s disconnect grace
        self.teacher = None
        self.players = {}  # student ID => profile and latest position
        self.teacher_avatar = None
        self.dirty = {}  # player ID => [id,x,y]
        self.game = game_default()
        self.timer_handle = None
        self.lock_handle = None
        self.autoend_handle = None
        self.closed_at = None
        self.last_active = time.monotonic()

    async def broadcast(self, message):
        payload = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        targets = [ws for ws in self.clients if not ws.closed]
        if not targets:
            return
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_str(payload), timeout=.35)
            except (asyncio.TimeoutError, ConnectionError, RuntimeError):
                await ws.close()
        await asyncio.gather(*(send(ws) for ws in targets), return_exceptions=True)

    async def game_update(self):
        self.last_active = time.monotonic()
        await self.broadcast({'t': 'game', 'g': self.game})

    def cancel_timers(self):
        for item in (self.timer_handle, self.lock_handle, self.autoend_handle):
            if item:
                item.cancel()
        self.timer_handle = self.lock_handle = self.autoend_handle = None

    async def start_question(self, args):
        g = self.game
        if g['phase'] in {'question', 'judging', 'locked'} or g['closed']:
            return
        q = str(args.get('q', '')).strip()[:500]
        t = args.get('qtype', 'ox')
        dur = args.get('dur', 15)
        opts = args.get('opts', [])
        if not q or t not in ('ox', 'mc') or dur not in (10, 15, 20, 30):
            return
        if t == 'mc':
            if (not isinstance(opts, list) or len(opts) != 4
                    or any(not isinstance(o, str) or not o.strip() for o in opts)):
                return
            opts = [o.strip()[:180] for o in opts]
        else:
            opts = []
        self.cancel_timers()
        self.game = {**g, 'phase': 'question', 'round': g['round'] + 1, 'q': q,
                     'dur': dur, 'endAt': ms() + dur * 1000 + 800,
                     'answer': None, 'exp': '', 'lastOut': {}, 'allRevive': False,
                     'survivors': None, 'winners': None, 'winnerIds': {},
                     'autoEndAt': 0, 'picks': {}, 'pickN': {}, 'lockOut': 0,
                     'qtype': t, 'opts': opts}
        await self.game_update()
        self.timer_handle = asyncio.get_running_loop().call_later(
            (dur * 1000 + 800) / 1000, lambda: asyncio.create_task(self.stop_question()))

    async def stop_question(self):
        if self.game['phase'] != 'question':
            return
        if self.timer_handle:
            self.timer_handle.cancel()
            self.timer_handle = None
        self.game = {**self.game, 'phase': 'judging', 'endAt': ms()}
        # Positions are frozen at this instant: the late client update cannot alter a pick.
        self.frozen = {pid: (p['x'], p['y']) for pid, p in self.players.items()}
        await self.game_update()
        self.lock_handle = asyncio.get_running_loop().call_later(
            .9, lambda: asyncio.create_task(self.lock_zones()))

    async def lock_zones(self):
        if self.game['phase'] != 'judging':
            return
        g = self.game
        alive = [pid for pid in self.players if pid not in g['out']]
        zones = ZONES[g['qtype']]
        picks, pick_n = {}, {z: 0 for z in zones}
        for pid in alive:
            x, y = self.frozen.get(pid, (-1, -1))
            for zone, rect in zones.items():
                if in_zone(rect, x, y):
                    picks[pid] = zone
                    pick_n[zone] += 1
                    break
        no_pick = [pid for pid in alive if pid not in picks]
        keep_all = len(no_pick) == len(alive)
        out = dict(g['out'])
        last_out = {}
        if not keep_all:
            for pid in no_pick:
                out[pid] = 1
                last_out[pid] = 1
        self.game = {**g, 'phase': 'locked', 'picks': picks, 'pickN': pick_n,
                     'out': out, 'lastOut': last_out,
                     'lockOut': 0 if keep_all else len(no_pick)}
        await self.game_update()

    async def reveal(self, args):
        g = self.game
        if g['phase'] != 'locked':
            return
        ans = str(args.get('answer', ''))
        if ans not in ZONES[g['qtype']]:
            return
        alive = [pid for pid in self.players if pid not in g['out']]
        wrong = [pid for pid in alive if g['picks'].get(pid) != ans]
        revive = bool(args.get('revive')) and bool(alive) and len(wrong) == len(alive)
        out, last_out = dict(g['out']), dict(g['lastOut'])
        if not revive:
            for pid in wrong:
                out[pid] = 1
                last_out[pid] = 1
        survivors = [pid for pid in alive if pid not in out]
        winners, winner_ids, autoend = None, {}, 0
        if len(survivors) <= 1 and (len(alive) + g['lockOut']) > 1:
            winners = [self.players[pid]['n'] for pid in survivors]
            winner_ids = {pid: 1 for pid in survivors}
            autoend = ms() + 5000
        self.game = {**g, 'phase': 'reveal', 'answer': ans,
                     'exp': str(args.get('exp', ''))[:500], 'out': out,
                     'lastOut': last_out, 'allRevive': revive,
                     'survivors': len(survivors),
                     'outCount': (0 if revive else len(wrong)) + g['lockOut'],
                     'winners': winners, 'winnerIds': winner_ids,
                     'autoEndAt': autoend}
        await self.game_update()
        if autoend:
            self.autoend_handle = asyncio.get_running_loop().call_later(
                5, lambda: asyncio.create_task(self.finish_game(auto=True)))

    async def finish_game(self, auto=False):
        g = self.game
        if auto and g['phase'] != 'reveal':
            return
        if not auto and g['phase'] in {'question', 'judging', 'locked'}:
            return
        alive = [pid for pid in self.players if pid not in g['out']]
        self.game = {**g, 'phase': 'end', 'winners': [self.players[p]['n'] for p in alive],
                     'winnerIds': {pid: 1 for pid in alive}, 'autoEndAt': 0}
        await self.game_update()

    async def teacher_command(self, name, args):
        if self.game['closed']:
            return
        if name == 'question':
            await self.start_question(args)
        elif name == 'stop':
            await self.stop_question()
        elif name == 'reveal':
            await self.reveal(args)
        elif name == 'new':
            self.cancel_timers()
            self.game = game_default()
            await self.game_update()
        elif name == 'end':
            await self.finish_game()
        elif name == 'notice':
            value = str(args.get('text', '')).strip()[:200]
            if value:
                self.game['notice'] = {'text': value, 't': ms()}
                await self.game_update()
        elif name == 'close':
            self.cancel_timers()
            self.game['closed'] = ms()
            self.closed_at = time.monotonic()
            await self.game_update()


async def expire_session(room, token, client_id):
    session = room.sessions.get(token)
    if not session or session['id'] != client_id or session['ws'] is not None:
        return
    room.sessions.pop(token, None)
    room.players.pop(client_id, None)
    room.dirty.pop(client_id, None)
    room.game['out'].pop(client_id, None)
    room.game['late'].pop(client_id, None)
    await room.broadcast({'t': 'player', 'id': client_id, 'd': None})
    room.last_active = time.monotonic()


async def connect_ws(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_MESSAGE_BYTES,
                               receive_timeout=120)
    await ws.prepare(request)
    room = None
    role = None
    client_id = None
    session_token = None
    intentional_leave = False
    try:
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
            except (ValueError, TypeError):
                await ws.send_json({'t': 'error', 'message': '잘못된 메시지입니다.'})
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get('t')
            if room is None:
                if kind != 'hello' or data.get('role') not in ('teacher', 'student'):
                    await ws.send_json({'t': 'error', 'message': '입장 정보가 올바르지 않습니다.'})
                    break
                code = data.get('room', '')
                role = data['role']
                if not isinstance(code, str) or not ROOM_RE.fullmatch(code):
                    await ws.send_json({'t': 'error', 'message': '방 코드를 확인해 주세요.'})
                    break
                room = app['rooms'].get(code)
                if role == 'teacher':
                    supplied = str(data.get('password', ''))
                    if not secrets.compare_digest(supplied, app['teacher_password']):
                        await ws.send_json({'t': 'error', 'message': '교사 비밀번호가 올바르지 않습니다.'})
                        break
                    if room and room.teacher and not room.teacher.closed:
                        await ws.send_json({'t': 'error', 'message': '이미 교사가 접속 중입니다.'})
                        break
                    if room is None:
                        room = Room(code)
                        app['rooms'][code] = room
                    room.teacher = ws
                    client_id = 'teacher'
                else:
                    if room is None or room.game['closed']:
                        await ws.send_json({'t': 'error', 'message': '교사가 방을 만든 뒤 입장해 주세요.'})
                        break
                    session_token = data.get('session', '')
                    if (not isinstance(session_token, str)
                            or not re.fullmatch(r'[0-9a-f]{32}', session_token)):
                        session_token = secrets.token_hex(16)
                    session = room.sessions.get(session_token)
                    if session is None:
                        if len(room.sessions) >= MAX_STUDENTS:
                            await ws.send_json({'t': 'error', 'message': '학생 정원이 70명입니다.'})
                            break
                        client_id = secrets.token_hex(8)
                        session = {'id': client_id, 'ws': None, 'timer': None}
                        room.sessions[session_token] = session
                    else:
                        client_id = session['id']
                        if session['timer']:
                            session['timer'].cancel()
                            session['timer'] = None
                        old_socket = session['ws']
                        if old_socket and old_socket is not ws and not old_socket.closed:
                            await old_socket.close()
                    session['ws'] = ws
                    room.students[client_id] = ws
                room.clients.add(ws)
                room.last_active = time.monotonic()
                players = [{'id': pid, **p} for pid, p in room.players.items()]
                if room.teacher_avatar:
                    players.append({'id': 'teacher', **room.teacher_avatar})
                await ws.send_json({'t': 'welcome', 'id': client_id, 'players': players,
                                    'g': room.game, 'serverTime': ms(),
                                    'session': session_token if role == 'student' else None})
                continue
            if kind == 'put':
                payload = data.get('d')
                pos = checked_position(payload)
                if pos is None:
                    continue
                if role == 'student' and client_id not in room.players:
                    name = str(payload.get('n', '')).strip()[:8]
                    if not name:
                        continue
                    color = payload.get('c', 0)
                    if not isinstance(color, int) or isinstance(color, bool):
                        color = 0
                    state = {'n': name, 'c': max(0, min(9, color)),
                             'x': pos[0], 'y': pos[1], 'e': '', 'et': 0}
                    room.players[client_id] = state
                    if room.game['round'] > 0 and room.game['phase'] != 'lobby':
                        room.game['out'][client_id] = 1
                        room.game['late'][client_id] = 1
                        await room.game_update()
                    await room.broadcast({'t': 'player', 'id': client_id, 'd': state})
                elif role == 'teacher':
                    state = {'n': '선생님', 'x': pos[0], 'y': pos[1],
                             'e': '', 'et': 0, 't': 1}
                    room.teacher_avatar = state
                    await room.broadcast({'t': 'player', 'id': 'teacher', 'd': state})
            elif kind == 'patch':
                state = room.teacher_avatar if role == 'teacher' else room.players.get(client_id)
                if not state:
                    continue
                patch = data.get('d', {})
                if not isinstance(patch, dict):
                    continue
                if 'x' in patch or 'y' in patch:
                    # The server closes the question before accepting a late position.
                    if room.game['phase'] == 'question' and ms() >= room.game['endAt']:
                        await room.stop_question()
                    if room.game['phase'] not in ('judging', 'locked'):
                        pos = checked_position(patch)
                        if pos is not None:
                            state['x'], state['y'] = pos
                            room.dirty[client_id] = [client_id, pos[0], pos[1]]
                if 'e' in patch and patch['e'] in EMOTES:
                    state['e'] = patch['e']
                    state['et'] = ms()
                    await room.broadcast({'t': 'player', 'id': client_id, 'd': state})
                room.last_active = time.monotonic()
            elif kind == 'command' and role == 'teacher':
                cmd = data.get('name')
                args = data.get('args', {})
                if isinstance(args, dict) and isinstance(cmd, str):
                    await room.teacher_command(cmd, args)
            elif kind == 'del':
                intentional_leave = True
                await ws.close()
    except Exception:
        LOG.exception('Unexpected WebSocket handler failure')
    finally:
        if room is not None:
            room.clients.discard(ws)
            if role == 'teacher' and room.teacher is ws:
                room.teacher = None
                room.teacher_avatar = None
                await room.broadcast({'t': 'player', 'id': 'teacher', 'd': None})
            elif role == 'student' and room.students.get(client_id) is ws:
                room.students.pop(client_id, None)
                session = room.sessions.get(session_token)
                if session and session['id'] == client_id:
                    session['ws'] = None
                    if intentional_leave:
                        await expire_session(room, session_token, client_id)
                    else:
                        session['timer'] = asyncio.get_running_loop().call_later(
                            35, lambda: asyncio.create_task(expire_session(room, session_token, client_id)))
            room.last_active = time.monotonic()
    return ws


async def broadcaster(app):
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL)
        for code, room in list(app['rooms'].items()):
            if room.dirty:
                changed = list(room.dirty.values())
                room.dirty.clear()
                await room.broadcast({'t': 'positions', 'rows': changed, 'at': ms()})
            if ((not room.clients and time.monotonic() - room.last_active > 600)
                    or (room.closed_at and time.monotonic() - room.closed_at > 6)):
                room.cancel_timers()
                for ws in list(room.clients):
                    await ws.close()
                app['rooms'].pop(code, None)


async def publisher_ctx(app):
    task = asyncio.create_task(broadcaster(app))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def create_app(password):
    app = web.Application(client_max_size=MAX_MESSAGE_BYTES)
    app['rooms'] = {}
    app['teacher_password'] = password
    app.cleanup_ctx.append(publisher_ctx)
    app.router.add_get('/ws', connect_ws)
    app.router.add_get('/healthz', lambda request: web.json_response({'status': 'ok'}))
    async def index(request):
        return web.FileResponse(Path(__file__).with_name('index.html'))
    app.router.add_get('/', index)
    app.router.add_get('/index.html', index)
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8080'))
    password = os.environ.get('TEACHER_PASSWORD', '')
    if len(password) < 8:
        raise SystemExit('TEACHER_PASSWORD 환경변수를 8자 이상으로 지정하세요.')
    LOG.info('Starting Mokil Golden Bell on %s:%s', host, port)
    web.run_app(create_app(password), host=host, port=port)


if __name__ == '__main__':
    main()