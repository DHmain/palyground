"""LAN recreation MVP. Python 3.9+, no third-party dependencies."""
import argparse
import json
import math
import random
import secrets
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()
DIRECTIONS = {'U': (0, -1), 'D': (0, 1), 'L': (-1, 0), 'R': (1, 0)}
RELAY_NOTES = 7
RELAY_STEP = .48
RELAY_WINDOW = .16
ROPE_TARGET = 24
ROPE_WINDOW = .12
GOURD_PERIOD = 1.6
GOURD_HP = 180


def target_position(phase):
    p = phase % 1
    if p < .45:
        return p * .35 / .45
    if p <= .55:
        return .35 + (p - .45) * 3
    return .65 + (p - .55) * .35 / .45


NAMES = ['협동 미로', '랜덤 리듬 계주', '변속 줄넘기', '박 터뜨리기']


class Game:
    def __init__(self):
        self.pin = secrets.token_hex(3).upper()
        self.players = {}
        self.host = None
        self.team_count = 5
        self.phase = 'lobby'
        self.stage = -1
        self.teams = []
        self.paused = False
        self.pause_start = 0
        self.pause_total = 0
        self.epoch = time.monotonic()
        self.start = 0
        self.limit = 180
        self.round_id = 0
        self.ai_serial = 0
        self.active_index = 0
        self.maze = self.make_maze()
        self.distances = self.distance_map()

    def now(self):
        return (self.pause_start if self.paused else time.monotonic()) - self.epoch - self.pause_total

    def make_maze(self, seed=42):
        grid = [[1] * 15 for _ in range(15)]
        rng = random.Random(seed)
        grid[1][1] = 0
        stack = [(1, 1)]
        while stack:
            x, y = stack[-1]
            opts = [(x + dx * 2, y + dy * 2, dx, dy) for dx, dy in DIRECTIONS.values()
                    if 1 <= x + dx * 2 < 14 and 1 <= y + dy * 2 < 14 and grid[y + dy * 2][x + dx * 2]]
            if not opts:
                stack.pop()
                continue
            nx, ny, dx, dy = rng.choice(opts)
            grid[y + dy][x + dx] = grid[ny][nx] = 0
            stack.append((nx, ny))
        return grid

    def distance_map(self, maze=None):
        maze = self.maze if maze is None else maze
        goal = len(maze) - 2
        d, q = {(goal, goal): 0}, deque([(goal, goal)])
        while q:
            x, y = q.popleft()
            for dx, dy in DIRECTIONS.values():
                p = (x + dx, y + dy)
                if 0 <= p[0] < len(maze) and 0 <= p[1] < len(maze) and not maze[p[1]][p[0]] and p not in d:
                    d[p] = d[(x, y)] + 1
                    q.append(p)
        return d

    def members(self, team):
        return [token for token, p in self.players.items() if p['team'] == team and token != self.host]

    def assigned(self, token):
        if token not in self.players:
            return None
        t = self.players[token]['team']
        return next((x for x in self.teams if x['id'] == t), None)

    def roles(self, n):
        return [['U', 'D'], ['L', 'R']] if n == 2 else ([['U', 'D'], ['L'], ['R']] if n == 3 else [['U'], ['D'], ['L'], ['R']])

    def rope_beat(self, index, team=None):
        t = self.teams[self.active_index] if team is None else team
        pattern = t['rope_pattern']
        cycles, offset = divmod(index + 1, len(pattern))
        start = self.start if t['started'] is None else t['started']
        return start + cycles * sum(pattern) + sum(pattern[:offset])

    def prepare_courses(self):
        # Equal shortest paths, distinct layouts; no route from another team can be reused.
        buckets = {}
        seed = secrets.randbits(48)
        for attempt in range(5000):
            maze = self.make_maze(seed + attempt)
            distances = self.distance_map(maze)
            length = distances[(1, 1)]
            if 64 <= length <= 100:
                bucket = buckets.setdefault(length, [])
                if all(maze != item[0] for item in bucket):
                    bucket.append((maze, distances))
                if len(bucket) == len(self.teams):
                    for t, (grid, distance) in zip(self.teams, bucket):
                        t.update(maze=grid, distances=distance, goal=[13, 13])
                    return
        raise ValueError('미로 생성에 실패했습니다. 다시 시작하세요.')

    def begin(self):
        if self.phase == 'lobby':
            active = [i for i in range(self.team_count) if self.members(i)]
            if len(active) < 2 or any(not 2 <= len(self.members(i)) <= 4 for i in active):
                raise ValueError('참여 조가 2개 이상이며 각 조에 2~4명이 있어야 합니다.')
            self.teams = [{'id': i, 'members': self.members(i), 'score': 0, 'history': []} for i in active]
            self.prepare_courses()
            self.stage = 0
        elif self.phase == 'results' and self.stage < 3:
            self.stage += 1
        else:
            raise ValueError('대기실 또는 스테이지 결과 화면에서 시작하세요.')
        self.phase = 'playing'
        for t in self.teams:
            t.update(pos=[1, 1], moved=-100, finish=None, progress=0, feedback='준비', health=GOURD_HP * len(t['members']),
                     used={}, successes=[], rope_index=0, rope_progress=0, relay_done=0, relay_hits=[], relay_bad=False,
                     completed=False, jumps=[-100.0] * len(t['members']), started=None)
            order = []
            while len(order) < 8:
                batch = t['members'][:]
                random.shuffle(batch)
                if order and batch[0] == order[-1]:
                    batch[0], batch[1] = batch[1], batch[0]
                order.extend(batch)
            t['order'] = order[:8]
            t['turn_start'] = self.start
            t['patterns'] = []
            for _ in range(8):
                pattern = random.choices(list(DIRECTIONS), k=RELAY_NOTES)
                for i in random.sample(range(RELAY_NOTES), 2):
                    pattern[i] = 'S'
                t['patterns'].append(pattern)
        patterns = set()
        for t in self.teams:
            while True:
                pattern = [.36, .36, .48, .48, .60, .60, .72, .72]
                random.shuffle(pattern)
                if tuple(pattern) not in patterns:
                    patterns.add(tuple(pattern))
                    t['rope_pattern'] = pattern
                    break
        self.active_index = 0
        self.activate_team()

    def activate_team(self):
        self.start = self.now() + 4
        t = self.teams[self.active_index]
        t['started'] = self.start
        t['turn_start'] = self.start
        t['feedback'] = '준비'
        self.round_id += 1

    def settle(self, t, now):
        if t['finish'] is not None or now < self.start:
            return
        if self.stage == 1:
            end = t['turn_start'] + 3 + (RELAY_NOTES - 1) * RELAY_STEP + RELAY_WINDOW + .03
            if now >= end:
                if len(t['relay_hits']) == RELAY_NOTES and not t['relay_bad']:
                    t['relay_done'] += 1
                    t['progress'] = t['relay_done'] / 8
                    t['feedback'] = '구간 통과!'
                else:
                    t['feedback'] = '구간 재도전'
                t['relay_hits'], t['relay_bad'] = [], False
                t['turn_start'] = now + .5
                if t['relay_done'] == 8:
                    t['finish'] = end - self.start
        if self.stage == 2:
            while now >= self.rope_beat(t['rope_index'], t) + ROPE_WINDOW and t['finish'] is None:
                hit = len(t['successes'])
                t['rope_progress'] += hit / len(t['members'])
                t['progress'] = min(1, t['rope_progress'] / ROPE_TARGET)
                t['feedback'] = f'{hit}/{len(t["members"])}명 성공 · +{hit / len(t["members"]):.2f}'
                t['successes'] = []
                t['rope_index'] += 1
                if t['rope_progress'] >= ROPE_TARGET - 1e-8:
                    t['finish'] = self.rope_beat(t['rope_index'] - 1, t) - self.start

    def tick(self):
        if self.phase != 'playing' or self.paused:
            return
        now = self.now()
        t = self.teams[self.active_index]
        self.settle(t, min(now, self.start + self.limit))
        if self.start <= now < self.start + self.limit:
            self.play_ai(now)
        if now >= self.start + self.limit or t['finish'] is not None:
            t['completed'] = True
            if t['finish'] is None:
                t['feedback'] = '시간 종료'
            if self.active_index + 1 < len(self.teams):
                self.active_index += 1
                self.activate_team()
            else:
                self.end()

    def play_ai(self, now):
        """Temporary practice players use the same input validation as humans."""
        for t in self.teams[self.active_index:self.active_index + 1]:
            if t['finish'] is not None:
                continue
            bots = [k for k in t['members'] if self.players[k].get('ai')]
            if not bots:
                continue
            if self.stage == 0:
                if now - t['moved'] < .3:
                    continue
                x, y = t['pos']
                key = next((k for k, (dx, dy) in DIRECTIONS.items()
                            if t['distances'].get((x + dx, y + dy)) == t['distances'][(x, y)] - 1), None)
                for k in bots:
                    if key in self.roles(len(t['members']))[t['members'].index(k)]:
                        self.input(k, key, self.round_id)
                        break
            elif self.stage == 1:
                runner = t['order'][t['relay_done']]
                if runner in bots:
                    for i, key in enumerate(t['patterns'][t['relay_done']]):
                        if i not in t['relay_hits'] and 0 <= now - (t['turn_start'] + 3 + i * RELAY_STEP) <= RELAY_WINDOW:
                            self.input(runner, key, self.round_id)
            elif self.stage == 2:
                if 0 <= now - self.rope_beat(t['rope_index'], t) <= ROPE_WINDOW:
                    for k in bots:
                        self.input(k, 'S', self.round_id)
            elif self.stage == 3:
                for k in bots:
                    phase = (now - self.start) / GOURD_PERIOD + t['members'].index(k) * .23
                    if .5 <= target_position(phase) <= .55:
                        self.input(k, 'S', self.round_id)

    def end(self):
        def key(t):
            return (0, round(t['finish'], 1)) if t['finish'] is not None else (1, -round(t['progress'], 6))
        ordered = sorted(self.teams, key=key)
        last, rank = None, 0
        for i, t in enumerate(ordered):
            if key(t) != last:
                rank = i + 1
            pts = [100, 80, 60, 40, 20][rank - 1]
            t['rank'], t['points'] = rank, pts
            t['score'] += pts
            t['history'].append({'stage': self.stage, 'rank': rank, 'points': pts, 'finish': t['finish']})
            last = key(t)
        self.phase = 'results'

    def input(self, token, key, round_id):
        if self.phase != 'playing' or self.paused or round_id != self.round_id:
            return
        t, now = self.assigned(token), self.now()
        if not t or t is not self.teams[self.active_index] or t['finish'] is not None or now < self.start or now >= self.start + self.limit:
            return
        # All timing is server receipt time; client clocks cannot choose a score.
        if self.stage == 0:
            role = self.roles(len(t['members']))[t['members'].index(token)]
            if key not in role or now - t['moved'] < .12:
                return
            t['moved'] = now
            dx, dy = DIRECTIONS[key]
            x, y = t['pos'][0] + dx, t['pos'][1] + dy
            if not t['maze'][y][x]:
                t['pos'] = [x, y]
                t['progress'] = max(0, 1 - t['distances'][(x, y)] / t['distances'][(1, 1)])
                if [x, y] == t['goal']:
                    t['finish'] = now - self.start
            t['feedback'] = self.players[token]['name'] + ' ' + key
        elif self.stage == 1:
            if token != t['order'][t['relay_done']] or key not in (*DIRECTIONS, 'S'):
                return
            elapsed = now - t['turn_start'] - 3
            if elapsed < -RELAY_WINDOW:
                return
            beat = int(math.floor(elapsed / RELAY_STEP + .5))
            if 0 <= beat < RELAY_NOTES and abs(elapsed - beat * RELAY_STEP) <= RELAY_WINDOW and beat not in t['relay_hits'] and key == t['patterns'][t['relay_done']][beat]:
                t['relay_hits'].append(beat)
                t['feedback'] = 'HIT ' + str(len(t['relay_hits'])) + f'/{RELAY_NOTES}'
            else:
                t['relay_bad'] = True
                t['feedback'] = 'MISS · 패턴 끝까지 이어집니다'
        elif self.stage == 2 and key == 'S':
            idx = t['rope_index']
            if t['used'].get(token) == idx:
                return
            # One attempt per rope cycle, including an early/late miss.
            t['used'][token] = idx
            t['jumps'][t['members'].index(token)] = now
            if abs(now - self.rope_beat(idx, t)) <= ROPE_WINDOW:
                t['successes'].append(token)
                t['feedback'] = self.players[token]['name'] + ' 점프 성공'
            else:
                t['feedback'] = self.players[token]['name'] + ' MISS'
        elif self.stage == 3 and key == 'S':
            i = t['members'].index(token)
            phase = (now - self.start) / GOURD_PERIOD + i * .23
            cycle = math.floor(phase)
            if t['used'].get(token) == cycle:
                return
            t['used'][token] = cycle
            error = abs(target_position(phase) - .5)
            damage = 30 if error <= .018 else 20 if error <= .05 else 10 if error <= .09 else 0
            t['health'] = max(0, t['health'] - damage)
            t['progress'] = 1 - t['health'] / (GOURD_HP * len(t['members']))
            t['feedback'] = self.players[token]['name'] + (' MISS' if not damage else f' 명중! −{damage}')
            if t['health'] == 0:
                t['finish'] = now - self.start

    def act(self, token, data):
        action = data.get('action')
        if action == 'join':
            name = str(data.get('name', '')).strip()[:24]
            if not name:
                raise ValueError('이름을 입력하세요.')
            if len(self.players) >= 80:
                raise ValueError('입장 가능한 인원을 초과했습니다.')
            token = secrets.token_urlsafe(24)
            self.players[token] = {'name': name, 'team': None, 'seen': time.monotonic()}
            return {'token': token}
        if token not in self.players:
            raise ValueError('다시 입장해 주세요.')
        p = self.players[token]
        if action == 'host':
            if self.phase != 'lobby' or self.host not in (None, token):
                raise ValueError('사회자는 대기실에서 한 명만 설정할 수 있습니다.')
            if not secrets.compare_digest(str(data.get('pin', '')).upper(), self.pin):
                raise ValueError('사회자 PIN이 일치하지 않습니다.')
            self.host, p['team'] = token, None
        elif action in ('team', 'spectate'):
            if self.phase != 'lobby':
                raise ValueError('경기 중에는 조를 변경할 수 없습니다.')
            if action == 'spectate':
                p['team'] = None
                if self.host == token:
                    self.host = None
            else:
                i = int(data['team'])
                if token == self.host or not 0 <= i < self.team_count or (p['team'] != i and len(self.members(i)) >= 4):
                    raise ValueError('해당 조에 참여할 수 없습니다. 사회자는 먼저 대기 인원으로 이동하세요.')
                p['team'] = i
        elif action in ('shuffle', 'count', 'start', 'pause', 'reset', 'add_ai', 'remove_ai', 'clear_ai'):
            if token != self.host:
                raise ValueError('사회자만 사용할 수 있습니다.')
            if action in ('shuffle', 'count', 'add_ai', 'remove_ai', 'clear_ai') and self.phase != 'lobby':
                raise ValueError('대기실에서만 변경할 수 있습니다.')
            if action == 'add_ai':
                i = int(data['team'])
                if not 0 <= i < self.team_count or len(self.members(i)) >= 4:
                    raise ValueError('빈자리가 있는 조를 선택하세요.')
                if len(self.players) >= 80:
                    raise ValueError('입장 가능한 인원을 초과했습니다.')
                self.ai_serial += 1
                self.players[secrets.token_urlsafe(24)] = {
                    'name': f'AI {self.ai_serial}', 'team': i, 'seen': time.monotonic(), 'ai': True}
            elif action in ('remove_ai', 'clear_ai'):
                team = int(data['team']) if action == 'remove_ai' else None
                if action == 'remove_ai' and not 0 <= team < self.team_count:
                    raise ValueError('올바른 조를 선택하세요.')
                candidates = [k for k, q in self.players.items()
                              if q.get('ai') and (action == 'clear_ai' or q['team'] == team)]
                for k in (candidates[-1:] if action == 'remove_ai' else candidates):
                    del self.players[k]
            elif action == 'count':
                n = int(data['count'])
                if not 2 <= n <= 5:
                    raise ValueError('2~5개 조를 선택하세요.')
                self.team_count = n
                for q in self.players.values():
                    if q['team'] is not None and q['team'] >= n:
                        q['team'] = None
            elif action == 'shuffle':
                pool = [k for k, q in self.players.items() if k != self.host and (q.get('ai') or time.monotonic() - q['seen'] < 15)]
                if not 2 * self.team_count <= len(pool) <= 4 * self.team_count:
                    raise ValueError('접속 인원에 맞춰 조 수를 선택하세요. 각 조는 2~4명입니다.')
                random.shuffle(pool)
                for q in self.players.values():
                    q['team'] = None
                for j, k in enumerate(pool):
                    self.players[k]['team'] = j % self.team_count
            elif action == 'start':
                if self.paused:
                    raise ValueError('일시정지를 먼저 해제하세요.')
                self.begin()
            elif action == 'pause':
                if self.phase != 'playing':
                    raise ValueError('경기 중에만 일시정지할 수 있습니다.')
                if not self.paused:
                    self.pause_start, self.paused = time.monotonic(), True
                else:
                    self.pause_total += time.monotonic() - self.pause_start
                    self.paused = False
            elif action == 'reset':
                if self.paused:
                    self.pause_total += time.monotonic() - self.pause_start
                    self.paused = False
                self.phase, self.stage, self.teams = 'lobby', -1, []
                self.round_id += 1
        elif action == 'input':
            self.input(token, data.get('key'), data.get('round'))
        else:
            raise ValueError('알 수 없는 요청입니다.')
        return {'ok': True}

    def public(self, token):
        self.tick()
        if token in self.players:
            self.players[token]['seen'] = time.monotonic()
        def person(k):
            p = self.players[k]
            return {'name': p['name'], 'online': bool(p.get('ai')) or time.monotonic() - p['seen'] < 5,
                    'ai': bool(p.get('ai')), 'me': k == token}
        out = []
        for t in self.teams:
            d = {k: t[k] for k in ['id', 'score', 'pos', 'progress', 'finish', 'feedback', 'health', 'relay_done', 'rope_progress', 'history', 'completed', 'jumps', 'started']}
            d['status'] = 'done' if t['completed'] else 'active' if self.phase == 'playing' and t is self.teams[self.active_index] else 'waiting'
            d['members'] = [person(k) for k in t['members']]
            if self.stage == 0:
                d.update(maze=t['maze'], goal=t['goal'], path_length=t['distances'][(1, 1)])
            if self.phase == 'results':
                d.update(rank=t['rank'], points=t['points'])
            if self.stage == 1 and t['relay_done'] < 8:
                d.update(runner=self.players[t['order'][t['relay_done']]]['name'], mine=t['order'][t['relay_done']] == token,
                         turn_start=t['turn_start'], pattern=t['patterns'][t['relay_done']], hits=t['relay_hits'], bad=t['relay_bad'])
            if self.stage == 2:
                idx = t['rope_index']
                pattern = t['rope_pattern']
                d.update(beat=self.rope_beat(idx, t), beat_index=idx,
                         successes=[t['members'].index(k) for k in t['successes']],
                         rope_pattern=pattern, period=pattern[idx % len(pattern)],
                         bpm=round(60 / pattern[idx % len(pattern)]),
                         next_bpm=round(60 / pattern[(idx + 1) % len(pattern)]), change_in=1)
            out.append(d)
        my = self.assigned(token)
        return {'now': self.now(), 'phase': self.phase, 'stage': self.stage, 'start': self.start, 'limit': self.limit,
                'round': self.round_id, 'paused': self.paused, 'team_count': self.team_count, 'teams': out,
                'active_team': self.teams[self.active_index]['id'] if self.phase == 'playing' else None,
                'host': token is not None and token == self.host, 'host_name': self.players[self.host]['name'] if self.host else None,
                'known': token in self.players, 'my_team': self.players.get(token, {}).get('team'),
                'my_slot': my['members'].index(token) if my and token in my['members'] else None,
                'lobby': [[person(k) for k in self.members(i)] for i in range(self.team_count)],
                'rules': {'relay_notes': RELAY_NOTES, 'relay_step': RELAY_STEP, 'relay_window': RELAY_WINDOW,
                          'rope_target': ROPE_TARGET, 'rope_window': ROPE_WINDOW,
                          'gourd_period': GOURD_PERIOD, 'gourd_hp': GOURD_HP},
                'waiting': [person(k) for k, p in self.players.items() if p['team'] is None and k != self.host]}


GAME = Game()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == '/api/state':
                with LOCK:
                    self.reply(200, GAME.public(self.headers.get('X-Player')))
            elif self.path in ('/', '/index.html'):
                body = (ROOT / 'index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.reply(404, {'error': '찾을 수 없습니다.'})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path != '/api/action':
            self.reply(404, {'error': '찾을 수 없습니다.'})
            return
        try:
            # Reject browser requests initiated from a different web origin.
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://' + self.headers.get('Host', ''), 'https://' + self.headers.get('Host', '')):
                self.reply(403, {'error': '다른 출처의 요청은 허용하지 않습니다.'})
                return
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 8192:
                raise ValueError('요청 크기가 잘못되었습니다.')
            data = json.loads(self.rfile.read(length))
            with LOCK:
                GAME.tick()
                self.reply(200, GAME.act(self.headers.get('X-Player'), data))
        except (ValueError, KeyError, TypeError, IndexError) as e:
            self.reply(400, {'error': str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    addresses = {'127.0.0.1'}
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.0.2.1', 80))  # route selection only; no packet is sent
            addresses.add(s.getsockname()[0])
    except OSError:
        pass
    print('\n=== FOURWARD / 4인 협동 운동회 ===', flush=True)
    for ip in sorted(addresses):
        print(f'접속 주소: http://{ip}:{args.port}', flush=True)
    print(f'사회자 PIN: {GAME.pin}\n같은 공유기의 참가자는 서버 PC의 LAN IP로 접속하세요. 종료: Ctrl+C\n', flush=True)
    def ticker():
        while True:
            time.sleep(.02)
            with LOCK:
                GAME.tick()
    threading.Thread(target=ticker, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == '__main__':
    main()
