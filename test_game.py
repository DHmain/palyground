import threading
import unittest
import urllib.request
import json
from http.server import ThreadingHTTPServer
import server


class GameTest(unittest.TestCase):
    def ai_lobby(self):
        g = server.Game()
        clock = [0.0]
        g.now = lambda: clock[0]
        host = g.act(None, {'action': 'join', 'name': '사회자'})['token']
        g.act(host, {'action': 'host', 'pin': g.pin})
        return g, clock, host

    def test_ai_complete_all_stages(self):
        g, clock, host = self.ai_lobby()
        for team, size in enumerate((2, 3, 4, 2, 4)):
            for _ in range(size):
                g.act(host, {'action': 'add_ai', 'team': team})
        for p in g.players.values():
            p['seen'] = -100  # AI remains online without browser polling.
        state = g.public(host)
        self.assertTrue(all(p['ai'] and p['online'] for team in state['lobby'] for p in team))
        self.assertFalse(any(k in json.dumps(state) for k in g.players))
        for stage in range(4):
            g.act(host, {'action': 'start'})
            self.assertEqual(g.stage, stage)
            g.tick()  # Countdown must not generate inputs.
            self.assertTrue(all(t['feedback'] == '준비' for t in g.teams))
            clock[0] = g.start
            for _ in range(45001):
                g.tick()
                if g.phase == 'results':
                    break
                clock[0] += .02
            self.assertEqual(g.phase, 'results')
            self.assertTrue(all(t['finish'] is not None for t in g.teams), stage)
            self.assertTrue(all(len(t['history']) == stage + 1 for t in g.teams))
        g.act(host, {'action': 'reset'})
        self.assertEqual(sum(bool(p.get('ai')) for p in g.players.values()), 15)
        g.act(host, {'action': 'clear_ai'})
        self.assertEqual(list(g.players), [host])

    def test_ai_management_limits_and_authority(self):
        g, clock, host = self.ai_lobby()
        user = g.act(None, {'action': 'join', 'name': '사람'})['token']
        for action in ('add_ai', 'remove_ai', 'clear_ai'):
            with self.assertRaises(ValueError):
                g.act(user, {'action': action, 'team': 0})
        for team in (-1, 5):
            with self.assertRaises(ValueError):
                g.act(host, {'action': 'add_ai', 'team': team})
        g.act(user, {'action': 'team', 'team': 0})
        for _ in range(3):
            g.act(host, {'action': 'add_ai', 'team': 0})
        with self.assertRaises(ValueError):
            g.act(host, {'action': 'add_ai', 'team': 0})
        g.act(host, {'action': 'remove_ai', 'team': 0})
        self.assertEqual(len(g.members(0)), 3)
        self.assertIn(user, g.members(0))
        for _ in range(2):
            g.act(host, {'action': 'add_ai', 'team': 1})
        g.act(host, {'action': 'start'})
        for action in ('add_ai', 'remove_ai', 'clear_ai'):
            with self.assertRaises(ValueError):
                g.act(host, {'action': action, 'team': 0})
        clock[0] = g.start + 1
        before = [t['pos'][:] for t in g.teams]
        g.paused = True
        g.tick()
        self.assertEqual([t['pos'] for t in g.teams], before)
        g.paused = False
        g.act(host, {'action': 'reset'})
        g.act(host, {'action': 'clear_ai'})
        g.act(host, {'action': 'add_ai', 'team': 4})
        g.act(host, {'action': 'count', 'count': 2})
        self.assertTrue(any(p['ai'] for p in g.public(host)['waiting']))
        g.act(host, {'action': 'clear_ai'})
        self.assertEqual(set(g.players), {host, user})

    def test_ai_mixed_team_and_shuffle(self):
        g, clock, host = self.ai_lobby()
        g.act(host, {'action': 'count', 'count': 2})
        user = g.act(None, {'action': 'join', 'name': '사람'})['token']
        g.act(user, {'action': 'team', 'team': 0})
        for team in (0, 1, 1):
            g.act(host, {'action': 'add_ai', 'team': team})
        for p in g.players.values():
            if p.get('ai'):
                p['seen'] = -100
        g.act(host, {'action': 'shuffle'})
        self.assertEqual([len(g.members(i)) for i in range(2)], [2, 2])
        # Restore a known mixed team with the human owning U/D.
        g.act(host, {'action': 'clear_ai'})
        g.act(user, {'action': 'team', 'team': 0})
        for team in (0, 1, 1):
            g.act(host, {'action': 'add_ai', 'team': team})
        g.act(host, {'action': 'start'})
        t = g.teams[0]
        # Find a corridor step for each role, independently of maze shape.
        for axis, human_owned in ((('U', 'D'), True), (('L', 'R'), False)):
            x, y, key = next((x, y, k) for (x, y), distance in t['distances'].items()
                             for k, (dx, dy) in server.DIRECTIONS.items()
                             if distance > 2 and k in axis and t['distances'].get((x + dx, y + dy)) == distance - 1)
            t['pos'] = [x, y]
            clock[0] = max(clock[0] + 1, g.start)
            g.tick()
            if human_owned:
                self.assertEqual(t['pos'], [x, y])
                g.input(user, key, g.round_id)
            dx, dy = server.DIRECTIONS[key]
            self.assertEqual(t['pos'], [x + dx, y + dy])

    def setup_game(self, sizes=(4, 4, 4, 4, 4), host_playing=False):
        g = server.Game()
        clock = [0.0]
        g.now = lambda: clock[0]
        host = g.act(None, {'action': 'join', 'name': '사회자'})['token']
        g.act(host, {'action': 'host', 'pin': g.pin})
        if host_playing:
            g.act(host, {'action': 'team', 'team': 0})
        for team, n in enumerate(sizes):
            for i in range(n - (1 if host_playing and team == 0 else 0)):
                k = g.act(None, {'action': 'join', 'name': f'{team}-{i}'})['token']
                g.act(k, {'action': 'team', 'team': team})
        g.begin()
        return g, clock, host

    def test_all_stages_with_20_players(self, host_playing=False, sizes=(4, 4, 4, 4, 4)):
        g, clock, host = self.setup_game(sizes=sizes, host_playing=host_playing)
        self.assertEqual(len(g.players), sum(sizes) + (0 if host_playing else 1))
        self.assertEqual(g.players[host]['team'], 0 if host_playing else None)
        # Each team solves its own maze using the assigned direction owners.
        clock[0] = g.start
        for t in g.teams:
            clock[0] = g.start
            while t['pos'] != t['goal']:
                x, y = t['pos']
                key = next(k for k, (dx, dy) in server.DIRECTIONS.items()
                           if t['distances'].get((x + dx, y + dy)) == t['distances'][(x, y)] - 1)
                slot = next(i for i, r in enumerate(g.roles(len(t['members']))) if key in r)
                clock[0] += .15
                g.input(t['members'][slot], key, g.round_id)
            self.assertIsNotNone(t['finish'])
            g.tick()
            clock[0] = g.start
        g.tick()
        self.assertEqual(g.phase, 'results')
        self.assertEqual(g.teams[0]['points'], 100)
        # Relay: eight turns, no leaked future order in public state.
        g.begin()
        for t in g.teams:
            while t['relay_done'] < 8:
                runner = t['order'][t['relay_done']]
                for i, key in enumerate(t['patterns'][t['relay_done']]):
                    clock[0] = t['turn_start'] + 3 + i * server.RELAY_STEP
                    g.input(runner, key, g.round_id)
                clock[0] = t['turn_start'] + 3 + (server.RELAY_NOTES-1) * server.RELAY_STEP + server.RELAY_WINDOW + .04
                g.tick()
        self.assertTrue(all(t['finish'] is not None for t in g.teams))
        self.assertNotIn('order', g.public(host)['teams'][0])
        self.assertEqual(g.phase, 'results')
        # Rope: each member once per beat through three full pattern cycles.
        g.begin()
        for t in g.teams:
            for beat in range(server.ROPE_TARGET):
                clock[0] = g.rope_beat(beat)
                for p in t['members']:
                    g.input(p, 'S', g.round_id)
                    g.input(p, 'S', g.round_id)
                clock[0] += .19
                g.tick()
        self.assertEqual(g.phase, 'results')
        self.assertTrue(all(abs(t['rope_progress']-server.ROPE_TARGET)<1e-6 for t in g.teams))
        # Gourd: center hits until every team finishes.
        g.begin()
        for t in g.teams:
            for cycle in range(24):
                clock[0] = g.start + (cycle + .5) * server.GOURD_PERIOD
                g.input(t['members'][0], 'S', g.round_id)
            g.tick()
        g.tick()
        self.assertEqual(g.phase, 'results')
        self.assertTrue(all(t['health'] == 0 for t in g.teams))
        self.assertTrue(all(len(t['history']) == 4 for t in g.teams))

    def test_host_can_play_all_stages(self):
        self.test_all_stages_with_20_players(host_playing=True)

    def test_start_play_enrolls_host_and_accepts_input(self):
        g, clock, host = self.ai_lobby()
        g.act(host, {'action': 'start_play'})
        self.assertEqual(g.members(0), [host])
        state = g.public(host)
        self.assertEqual(state['my_team'], state['active_team'])
        self.assertEqual(state['my_slot'], 0)
        clock[0] = g.start
        t = g.teams[0]
        x, y = t['pos']
        key = next(k for k, (dx, dy) in server.DIRECTIONS.items()
                   if t['distances'].get((x+dx, y+dy)) == t['distances'][(x, y)]-1)
        g.act(host, {'action': 'input', 'key': key, 'round': g.round_id})
        self.assertNotEqual(t['pos'], [x, y])

    def test_host_can_take_ai_without_resetting_game(self):
        for stage in range(4):
            with self.subTest(stage=stage):
                g, clock, host = self.ai_lobby()
                g.act(host, {'action': 'add_ai', 'team': 0})
                g.act(host, {'action': 'start'})
                g.stage = stage
                t = g.teams[0]
                bot = t['members'][0]
                # Preserve consumed attempts and prior successes at takeover.
                t['used'][bot] = 0
                t['successes'] = [bot]
                old_round, old_start = g.round_id, g.start
                g.act(host, {'action': 'take_ai'})
                self.assertEqual(t['members'], [host])
                self.assertTrue(all(k == host for k in t['order']))
                self.assertEqual(t['successes'], [host])
                self.assertEqual(t['used'][host], 0)
                self.assertNotIn(bot, g.players)
                self.assertEqual((g.round_id, g.start), (old_round, old_start))
                self.assertEqual(g.public(host)['my_slot'], 0)
                with self.assertRaises(ValueError):
                    g.act(host, {'action': 'take_ai'})

    def test_start_play_preserves_roster_when_full(self):
        g, clock, host = self.ai_lobby()
        g.act(host, {'action': 'count', 'count': 2})
        for team in (0, 1):
            for _ in range(4):
                g.act(host, {'action': 'add_ai', 'team': team})
        with self.assertRaises(ValueError):
            g.act(host, {'action': 'start_play'})
        self.assertEqual(g.phase, 'lobby')
        self.assertIsNone(g.players[host]['team'])
        self.assertEqual([len(g.members(i)) for i in (0, 1)], [4, 4])

    def test_small_rosters_complete_all_stages(self):
        # Drive human inputs through every stage, including the participating host.
        for sizes in ((1,), (2,), (3,), (1, 1), (2, 1), (1, 2), (1, 1, 1), (3, 3)):
            with self.subTest(sizes=sizes):
                self.test_all_stages_with_20_players(host_playing=True, sizes=sizes)

    def test_small_roster_shuffle_and_ai(self):
        self.assertEqual(server.Game().roles(1), [['U', 'D', 'L', 'R']])
        for total in (1, 2, 3):
            with self.subTest(total=total):
                g, clock, host = self.ai_lobby()
                with self.assertRaises(ValueError):
                    g.act(host, {'action': 'start'})
                for _ in range(total):
                    g.act(host, {'action': 'add_ai', 'team': 0})
                g.act(host, {'action': 'shuffle'})
                self.assertEqual(sum(len(g.members(i)) for i in range(g.team_count)), total)
                for stage in range(4):
                    g.act(host, {'action': 'start'})
                    for _ in range(30001):
                        g.tick()
                        if g.phase == 'results':
                            break
                        clock[0] += .02
                    self.assertEqual(g.phase, 'results')
                    self.assertTrue(all(t['finish'] is not None for t in g.teams), stage)

    def test_host_team_management_and_authority(self):
        g, clock, host = self.ai_lobby()
        g.act(host, {'action': 'count', 'count': 2})
        g.act(host, {'action': 'team', 'team': 0})
        for team in (0, 1, 1):
            g.act(host, {'action': 'add_ai', 'team': team})
        state = g.public(host)
        self.assertTrue(state['host'])
        self.assertEqual(len(state['lobby'][0]), 2)
        g.act(host, {'action': 'shuffle'})
        self.assertIsNotNone(g.players[host]['team'])
        self.assertEqual([len(g.members(i)) for i in range(2)], [2, 2])
        g.act(host, {'action': 'start'})
        self.assertIsNotNone(g.public(host)['my_slot'])
        g.act(host, {'action': 'pause'})
        self.assertTrue(g.paused)
        g.act(host, {'action': 'pause'})
        self.assertFalse(g.paused)
        g.act(host, {'action': 'reset'})
        g.act(host, {'action': 'spectate'})
        self.assertEqual(g.host, host)
        self.assertIsNone(g.players[host]['team'])
        g.act(host, {'action': 'count', 'count': 3})
        self.assertEqual(g.team_count, 3)
        # Claiming the host seat does not remove an existing team assignment.
        g.host = None
        g.act(host, {'action': 'team', 'team': 0})
        g.act(host, {'action': 'host', 'pin': g.pin})
        self.assertEqual(g.players[host]['team'], 0)

    def test_balancing_and_authority(self):
        g, clock, host = self.setup_game((2, 3, 4))
        self.assertEqual(g.roles(2), [['U','D'],['L','R']])
        self.assertEqual([t['health'] for t in g.teams], [360,540,720])
        user = g.teams[0]['members'][0]
        with self.assertRaises(ValueError):
            g.act(user, {'action':'reset'})
        with self.assertRaises(ValueError):
            g.act(user, {'action':'team','team':1})
        clock[0] = g.start+1
        before = g.teams[0]['pos'][:]
        g.input(user, 'R', g.round_id)  # this user only owns U/D
        self.assertEqual(before, g.teams[0]['pos'])
        g.stage = 2
        clock[0] = g.rope_beat(0)
        g.input(user, 'S', g.round_id)
        clock[0] += .19
        g.tick()
        self.assertEqual(g.teams[0]['rope_progress'], .5)
        for i in range(len(g.teams)):
            self.assertEqual(g.active_index, i)
            clock[0] = g.start + g.limit + 1
            g.tick()
        self.assertEqual(g.phase, 'results')

    def test_sequential_turns_countdown_and_stale_inputs(self):
        g, clock, host = self.setup_game((2, 2))
        first, second = g.teams
        clock[0] = g.start
        old_round = g.round_id
        # A waiting team cannot play, even with the current round identifier.
        for key in server.DIRECTIONS:
            for player in second['members']:
                g.input(player, key, old_round)
        self.assertEqual(second['pos'], [1, 1])
        self.assertEqual(second['moved'], -100)
        self.assertEqual(g.public(host)['active_team'], first['id'])
        # Complete the first team, then give the next team its own full clock.
        first['finish'] = 10
        clock[0] += 10
        g.tick()
        self.assertTrue(first['completed'])
        self.assertEqual(g.phase, 'playing')
        self.assertEqual(g.active_index, 1)
        self.assertEqual(g.start, clock[0] + 4)
        self.assertNotEqual(g.round_id, old_round)
        before = second['pos'][:]
        for player in second['members']:
            for key in server.DIRECTIONS:
                g.input(player, key, g.round_id)
        self.assertEqual(second['pos'], before)
        clock[0] = g.start
        for player in second['members']:
            for key in server.DIRECTIONS:
                g.input(player, key, old_round)
        self.assertEqual(second['moved'], -100)
        second['finish'] = 10
        g.tick()
        self.assertEqual(g.phase, 'results')
        self.assertEqual([t['rank'] for t in g.teams], [1, 1])
        self.assertEqual([t['score'] for t in g.teams], [100, 100])

    def test_rope_jump_events_only_for_current_team(self):
        g, clock, host = self.setup_game((2, 2))
        g.stage = 2
        first, second = g.teams
        clock[0] = g.rope_beat(0)
        player = first['members'][0]
        g.input(player, 'S', g.round_id)
        jump_time = first['jumps'][0]
        clock[0] += .01
        g.input(player, 'S', g.round_id)
        g.input(second['members'][0], 'S', g.round_id)
        state = g.public(host)
        self.assertEqual(first['jumps'][0], jump_time)
        self.assertEqual(state['teams'][0]['successes'], [0])
        self.assertEqual(second['jumps'], [-100, -100])

    def test_distinct_balanced_courses_and_rope_patterns(self):
        g, clock, host = self.setup_game()
        layouts = [json.dumps(t['maze']) for t in g.teams]
        self.assertEqual(len(set(layouts)), 5)
        lengths = [t['distances'][(1, 1)] for t in g.teams]
        self.assertEqual(len(set(lengths)), 1)
        self.assertGreaterEqual(lengths[0], 64)
        self.assertTrue(all(len(t['maze']) == 15 for t in g.teams))
        patterns = [tuple(t['rope_pattern']) for t in g.teams]
        self.assertEqual(len(set(patterns)), 5)
        self.assertTrue(all(sorted(p) == sorted(patterns[0]) for p in patterns))
        for t in g.teams:
            t['started'] = g.start
            self.assertAlmostEqual(g.rope_beat(23, t) - g.start, 12.96)
            for i in range(1, 16):
                self.assertAlmostEqual(g.rope_beat(i, t) - g.rope_beat(i-1, t), t['rope_pattern'][i % 8])

    def test_relay_space_key_and_tighter_window(self):
        g, clock, host = self.setup_game((2, 2))
        g.stage = 1
        t = g.teams[0]
        self.assertTrue(all(len(p) == 7 and p.count('S') == 2 for p in t['patterns']))
        t['patterns'][0] = ['S', 'U', 'D', 'L', 'R', 'S', 'U']
        runner = t['order'][0]
        clock[0] = t['turn_start'] + 3
        g.input(runner, 'S', g.round_id)
        self.assertEqual(t['relay_hits'], [0])
        clock[0] += server.RELAY_STEP + server.RELAY_WINDOW + .01
        g.input(runner, 'U', g.round_id)
        self.assertTrue(t['relay_bad'])
        self.assertEqual(t['relay_hits'], [0])

    def test_accelerating_target_damage_and_attempt_limit(self):
        def speed(p):
            return (server.target_position(p+.001)-server.target_position(p))/.001
        self.assertGreater(speed(.49), speed(.2)*3)
        self.assertAlmostEqual(server.target_position(.5), .5)
        self.assertAlmostEqual(server.target_position(1.5), .5)
        g, clock, host = self.setup_game((2, 2))
        g.stage = 3
        t = g.teams[0]
        player = t['members'][0]
        clock[0] = g.start + .5*server.GOURD_PERIOD
        g.input(player, 'S', g.round_id)
        self.assertEqual(t['health'], 2*server.GOURD_HP-30)
        g.input(player, 'S', g.round_id)
        self.assertEqual(t['health'], 2*server.GOURD_HP-30)
        # A press outside the outer target consumes the next cycle too.
        clock[0] = g.start + 1.2*server.GOURD_PERIOD
        g.input(player, 'S', g.round_id)
        clock[0] = g.start + 1.5*server.GOURD_PERIOD
        g.input(player, 'S', g.round_id)
        self.assertEqual(t['health'], 2*server.GOURD_HP-30)

    def test_http_tokens_and_twenty_sessions(self):
        server.GAME = server.Game()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{httpd.server_port}'
        try:
            self.assertIn(b'FOURWARD', urllib.request.urlopen(url).read())
            tokens=[]
            for i in range(20):
                req=urllib.request.Request(url+'/api/action', data=json.dumps({'action':'join','name':str(i)}).encode(),headers={'Content-Type':'application/json'})
                tokens.append(json.load(urllib.request.urlopen(req))['token'])
            self.assertEqual(len(set(tokens)),20)
            req=urllib.request.Request(url+'/api/state',headers={'X-Player':tokens[0]})
            data=json.load(urllib.request.urlopen(req))
            self.assertEqual(len(data['waiting']),20)
            self.assertNotIn('pin',data)
            self.assertNotIn(tokens[1],json.dumps(data))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    unittest.main()
