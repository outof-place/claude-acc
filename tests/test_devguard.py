"""Testy devguard.py: decyzje na sztucznym obrazie świata i strażnik na prawdziwych procesach.

Decyzje (`decide`, presja, rozpoznawanie komend agentów) testujemy wprost na funkcjach.
Testy z procesami stawiają podróbkę `next dev` (Python udający serwer z balastem w RAM)
w osobnym $HOME, a konfiguracja ogranicza strażnika do katalogu testu (`scope`), więc
prawdziwe dev serwery na tym Macu są dla niego niewidoczne. Orka jest wyłączona albo
podrobiona (gniazdo i CLI w katalogu testu). Strażnik to devguard_core.py, wejście hooka
i komend to devguard.py.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "devguard.py")
sys.path.insert(0, ROOT)

import devguard as entry
import devguard_core as dg

GB = 1024**3
NOW = 1_800_000_000.0


def cfg(**extra):
    out = dict(dg.DEFAULT_CONFIG)
    out.update(extra)
    return out


def unit(key, fp_gb=1.0, cwd=None, **attrs):
    """Jednostka z tymi polami, których używa `decide`; domyślnie dojrzała, cicha, bez widzów."""
    cwd = cwd or f"/w/{key}"
    u = types.SimpleNamespace(
        key=key,
        label=f":{key} {cwd}",
        servers=[types.SimpleNamespace(cwd=cwd)],
        app_key=cwd,
        footprint=fp_gb * GB,
        biggest=fp_gb * GB,
        protected=False,
        age=3600,
        quiet=3600,
        host="shell",
        recyclable=True,
        attended=False,
        watched=False,
        agent_working=False,
        start=0,
        ports=[],
    )
    for name, value in attrs.items():
        setattr(u, name, value)
    u.last_watched = attrs.get("last_watched", NOW if u.watched else NOW - 7200)
    return u


def world(*units, level=0, reasons=()):
    pressure = types.SimpleNamespace(level=level, ram=48 * GB, reasons=list(reasons))
    return types.SimpleNamespace(now=NOW, pressure=pressure, units=list(units))


def plans(world_, state=None, **extra):
    return [
        (p.unit.key, p.action) for p in dg.decide(cfg(**extra), world_, state or {})
    ]


class DecideTest(unittest.TestCase):
    def test_bloated_quiet_server_is_recycled(self):
        self.assertEqual(plans(world(unit("a", 7, watched=True))), [("a", "recycle")])

    def test_bloated_busy_server_waits(self):
        self.assertEqual(plans(world(unit("a", 7, watched=True, quiet=5))), [])

    def test_bloated_server_you_watch_needs_long_quiet(self):
        watched = dict(watched=True, attended=True)
        self.assertEqual(plans(world(unit("a", 7, quiet=120, **watched))), [])
        self.assertEqual(
            plans(world(unit("a", 7, quiet=400, **watched))), [("a", "recycle")]
        )

    def test_bloated_unrecyclable_without_viewers_stops(self):
        u = unit("a", 7, recyclable=False, host="agent")
        self.assertEqual(plans(world(u)), [("a", "stop")])

    def test_bloated_unrecyclable_with_viewers_only_warns(self):
        u = unit("a", 7, recyclable=False, host="agent", watched=True)
        self.assertEqual(plans(world(u)), [("a", "warn")])

    def test_duplicate_without_viewers_stops_watched_one_stays(self):
        old = unit("old", cwd="/w/app", watched=True)
        dup = unit("dup", cwd="/w/app", start=5)
        self.assertEqual(plans(world(old, dup)), [("dup", "stop")])

    def test_orphan_stops(self):
        u = unit("a", host="orphan", recyclable=False, quiet=0)
        self.assertEqual(plans(world(u)), [("a", "stop")])

    def test_idle_waits_twice_as_long_while_agent_works(self):
        idle = 50 * 60
        self.assertEqual(plans(world(unit("a", quiet=idle))), [("a", "stop")])
        self.assertEqual(plans(world(unit("a", quiet=idle, agent_working=True))), [])

    def test_young_and_protected_are_untouchable(self):
        young = unit("young", 7, age=60)
        safe = unit("safe", 7, protected=True)
        self.assertEqual(plans(world(young, safe), state={}, budget_percent=1), [])

    def test_over_budget_frees_the_biggest_idle_server(self):
        small = unit("small", 2, quiet=20 * 60)
        big = unit("big", 4, quiet=20 * 60)
        watched = unit("watched", 4.5, quiet=20 * 60, watched=True)
        got = plans(world(small, big, watched), budget_percent=15)
        self.assertEqual(got, [("big", "stop")])

    def test_budget_alone_spares_a_cheap_busy_stack(self):
        """2026-10-04: sam budżet ubił `pnpm dev` z korzenia (2,4 GB), bo dwa serwery
        po 8 GB akurat kompilowały. Bez duszenia się Maca tani, żywy stos zostaje."""
        stack = unit("stack", 2.4, quiet=60)
        landing = unit("landing", 8, watched=True, quiet=5)
        spacing = unit("spacing", 8, watched=True, quiet=5)
        self.assertEqual(plans(world(stack, landing, spacing)), [])

    def test_budget_alone_recycles_the_bloated_server_once_it_is_quiet(self):
        stack = unit("stack", 2.4, quiet=60)
        spacing = unit("spacing", 8, watched=True, quiet=40)
        self.assertEqual(plans(world(stack, spacing)), [("spacing", "recycle")])

    def test_warning_spares_watched_servers_that_are_not_bloated(self):
        watched = unit("w", 4, watched=True)
        self.assertEqual(plans(world(watched, level=1, reasons=["swap"])), [])

    def test_critical_recycles_what_you_watch_but_never_stops_it(self):
        mine = unit("mine", 4, watched=True, attended=True)
        self.assertEqual(plans(world(mine, level=2)), [("mine", "recycle")])
        mine.recyclable = False
        self.assertEqual(plans(world(mine, level=2)), [])

    def test_critical_prefers_unwatched_big_server(self):
        agents = unit("agents", 4, watched=True)
        idle = unit("idle", 3)
        self.assertEqual(plans(world(agents, idle, level=2))[0], ("idle", "stop"))

    def test_regrowing_server_is_a_loop_and_stops(self):
        state = {"recycles": [[NOW - 600, "/w/a"], [NOW - 1200, "/w/a"]]}
        self.assertEqual(
            plans(world(unit("a", 7, watched=True)), state), [("a", "stop")]
        )

    def test_regrowing_server_you_watch_only_warns(self):
        state = {"recycles": [[NOW - 600, "/w/a"], [NOW - 1200, "/w/a"]]}
        u = unit("a", 7, watched=True, attended=True)
        self.assertEqual(plans(world(u), state), [("a", "warn")])

    def test_one_plan_per_unit_the_most_urgent(self):
        u = unit("a", 7, watched=False, recyclable=False, host="orphan")
        self.assertEqual(plans(world(u, level=2)), [("a", "stop")])


PIN_KEEP = {"target": ":3747", "level": "keep", "until": None, "reason": "film"}
PIN_HOLD = {"target": ":3747", "level": "hold", "until": None, "reason": "film"}


class PinTest(unittest.TestCase):
    """Przypięcia z `pin`: wyjątek na czas, którego strażnik nie zatrzymuje."""

    def pinned(self, pin, **attrs):
        return unit("a", protected=True, pin=pin, ports=[3747], **attrs)

    def test_pinned_is_never_stopped_for_idle_or_budget(self):
        idle = self.pinned(PIN_KEEP, quiet=99_999)
        self.assertEqual(plans(world(idle), budget_percent=1), [])

    def test_pinned_duplicate_and_orphan_stay(self):
        old = unit("old", cwd="/w/app", watched=True)
        dup = unit("dup", cwd="/w/app", start=5, protected=True, pin=PIN_KEEP)
        orphan = self.pinned(PIN_KEEP, host="orphan", recyclable=False)
        self.assertEqual(plans(world(old, dup)), [])
        self.assertEqual(plans(world(orphan)), [])

    def test_pinned_bloated_is_recycled_never_stopped(self):
        self.assertEqual(
            plans(world(self.pinned(PIN_KEEP, fp_gb=7))), [("a", "recycle")]
        )
        stuck = self.pinned(PIN_KEEP, fp_gb=7, recyclable=False)
        self.assertEqual(plans(world(stuck)), [])

    def test_no_restart_pin_is_left_alone_until_critical(self):
        held = self.pinned(PIN_HOLD, fp_gb=7)
        self.assertEqual(plans(world(held)), [])
        self.assertEqual(
            plans(world(held, level=2, reasons=["swap"])), [("a", "recycle")]
        )

    def test_critical_pressure_frees_unpinned_first(self):
        held = self.pinned(PIN_HOLD, fp_gb=7)
        other = unit("b", 3)
        self.assertEqual(plans(world(held, other, level=2))[0], ("b", "stop"))

    def test_pinned_restart_loop_only_warns(self):
        state = {"recycles": [[NOW - 600, "/w/a"], [NOW - 1200, "/w/a"]]}
        got = plans(world(self.pinned(PIN_KEEP, fp_gb=7)), state)
        self.assertEqual(got, [("a", "warn")])

    def test_pin_matches_port_and_directory_below(self):
        u = unit("a", cwd="/w/repo/apps/web", ports=[3747])
        self.assertTrue(dg.pin_matches({"target": ":3747"}, u))
        self.assertTrue(dg.pin_matches({"target": "/w/repo"}, u))
        self.assertFalse(dg.pin_matches({"target": "/w/rep"}, u))
        self.assertFalse(dg.pin_matches({"target": ":3000"}, u))

    def test_durations(self):
        self.assertEqual(dg.parse_duration("90m"), 90 * 60)
        self.assertEqual(dg.parse_duration("1,5h"), 5400)
        self.assertEqual(dg.parse_duration("2d"), 2 * 86400)
        with self.assertRaises(ValueError):
            dg.parse_duration("soon")


class PinCommandTest(unittest.TestCase):
    """`pin`, `unpin`, `pins` na prawdziwym pliku przypięć w katalogu testu."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patches = [
            mock.patch.object(dg, "PINS_PATH", os.path.join(self.tmp, "pins.json")),
            mock.patch.object(dg, "STATE_DIR", self.tmp),
            mock.patch.object(dg, "LOG_PATH", os.path.join(self.tmp, "devguard.log")),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_pin_defaults_to_twelve_hours_and_keep(self):
        self.assertEqual(dg.cmd_pin({}, [":3747", "--reason", "film hero"]), 0)
        (pin,) = dg.load_pins()
        self.assertEqual(
            (pin["target"], pin["level"], pin["reason"]), (":3747", "keep", "film hero")
        )
        self.assertAlmostEqual(pin["until"] - time.time(), 12 * 3600, delta=5)

    def test_pin_replaces_the_same_target_and_expires(self):
        dg.cmd_pin({}, ["3747", "--for", "1h"])
        dg.cmd_pin({}, [":3747", "--forever", "--no-restart"])
        (pin,) = dg.load_pins()
        self.assertEqual((pin["until"], pin["level"]), (None, "hold"))
        dg.cmd_pin({}, [":3000", "--for", "1m"])
        self.assertEqual(len(dg.load_pins(time.time() + 120)), 1)

    def test_directory_pin_is_absolute(self):
        dg.cmd_pin({}, [self.tmp])
        (pin,) = dg.load_pins()
        self.assertEqual(pin["target"], os.path.realpath(self.tmp))

    def test_unpin_one_and_all(self):
        dg.cmd_pin({}, [":3747"])
        dg.cmd_pin({}, [":3000"])
        self.assertEqual(dg.cmd_unpin({}, [":3747"]), 0)
        self.assertEqual([p["target"] for p in dg.load_pins()], [":3000"])
        self.assertEqual(dg.cmd_unpin({}, [":9999"]), 1)
        self.assertEqual(dg.cmd_unpin({}, ["all"]), 0)
        self.assertEqual(dg.load_pins(), [])

    def test_bad_arguments(self):
        self.assertEqual(dg.cmd_pin({}, []), 2)
        self.assertEqual(dg.cmd_pin({}, [":3747", "--soon"]), 2)


class PressureTest(unittest.TestCase):
    def measure(self, swap_gb, history=None, swapouts=100, kernel=1, available=50):
        values = {
            "hw.memsize": 48 * GB,
            "kern.memorystatus_vm_pressure_level": kernel,
            "kern.memorystatus_level": available,
            "vm.compressor_bytes_used": 0,
            "vm.compressor.compactor.swapouts_queued_pressure": swapouts,
        }
        state = {"swap_history": history or []}
        with (
            mock.patch.object(dg, "sysctl_int", values.get),
            mock.patch.object(dg, "swap_usage", lambda: (16 * GB, swap_gb * GB)),
        ):
            return dg.Pressure(cfg(), state, NOW)

    def test_big_swap_that_stays_is_history_not_pressure(self):
        """2026-10-04: swap stał na 10,3 GB po zwolnieniu pamięci, a strażnik zatrzymał
        świeżo zrestartowany serwer. Stojący swap to tylko notatka."""
        old = [[NOW - 90, 11 * GB, 100]]
        pressure = self.measure(11, old)
        self.assertEqual(pressure.level, 0)
        self.assertTrue(pressure.notes)

    def test_big_swap_that_grows_is_critical(self):
        old = [[NOW - 90, 10 * GB, 100]]
        self.assertEqual(self.measure(11, old).level, 2)

    def test_swapout_burst_counts_as_swapping(self):
        old = [[NOW - 90, 11 * GB, 100]]
        self.assertEqual(self.measure(11, old, swapouts=110).level, 0)
        self.assertEqual(self.measure(11, old, swapouts=1100).level, 2)

    def test_little_available_memory_is_a_warning(self):
        self.assertEqual(self.measure(1, available=18).level, 1)

    def test_normal_kernel_level_does_not_hide_low_memory(self):
        self.assertEqual(self.measure(1, available=8).level, 2)

    def test_small_swap_is_fine(self):
        self.assertEqual(self.measure(2).level, 0)


class AdmitParserTest(unittest.TestCase):
    def starts(self, command, cwd="/w/mono"):
        return dg.dev_starts(command, cwd)

    def test_plain_and_wrapped_next_dev(self):
        for command in (
            "next dev",
            "pnpm exec next dev --turbopack -p 3012",
            "rtk proxy pnpm exec next dev --turbopack -p 3012",
            "PORT=3001 npx next dev",
            "nohup pnpm exec next dev > /tmp/log 2>&1 &",
        ):
            self.assertEqual(self.starts(command), [("/w/mono", None, False)], command)

    def test_cd_and_dir_flags_move_the_target(self):
        self.assertEqual(
            self.starts("cd apps/web && pnpm exec next dev"),
            [("/w/mono/apps/web", None, False)],
        )
        self.assertEqual(
            self.starts("pnpm -C apps/web exec next dev"),
            [("/w/mono/apps/web", None, False)],
        )

    def test_scripts_and_filters(self):
        self.assertEqual(self.starts("pnpm dev"), [("/w/mono", None, True)])
        self.assertEqual(self.starts("npm run dev:landing"), [("/w/mono", None, True)])
        self.assertEqual(
            self.starts("pnpm --filter landing-page dev"),
            [("/w/mono", "landing-page", False)],
        )

    def test_dev_server_started_through_orca_terminal(self):
        command = 'orca terminal create --worktree active --command "cd apps/web && pnpm exec next dev"'
        self.assertEqual(self.starts(command), [("/w/mono/apps/web", None, False)])

    def test_not_a_dev_server(self):
        for command in (
            "grep -r 'next dev' docs",
            "pnpm build",
            "vite build",
            "npm run build && npm test",
            "echo pnpm dev",
            "cat package.json",
        ):
            self.assertEqual(self.starts(command), [], command)

    def test_vite_forms(self):
        self.assertEqual(
            self.starts("npx vite --port 5173"), [("/w/mono", None, False)]
        )
        self.assertEqual(self.starts("vite dev"), [("/w/mono", None, False)])


FAKE_SERVER = """\
import socket, sys, time
port = int(sys.argv[sys.argv.index("-p") + 1])
ballast = bytearray(int(sys.argv[sys.argv.index("--mb") + 1]) * 1024 * 1024)
for i in range(0, len(ballast), 4096):
    ballast[i] = 1
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", port))
sock.listen(8)
while True:
    time.sleep(1)
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GuardTest(unittest.TestCase):
    """Strażnik na prawdziwych procesach, ograniczony do katalogu testu."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="devguard-test-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.state_dir)
        self.config()

    def config(self, **extra):
        data = {
            "mode": "enforce",
            "scope": [self.home],
            "runtimes": ["node", "Python", "python3"],
            "max_server_gb": 0.02,  # 20 MB: podróbka z balastem 48 MB jest spuchnięta
            "grace_minutes": 0,
            "quiet_seconds": 0,
            "cooldown_seconds": 0,
            "notify": False,
            "close_tabs": False,
            "orca_comment": False,
            "caps_minutes": 0,
            # prawdziwy swap tego Maca nie może wpływać na wynik testu
            "swap_warn_percent": 1000,
            "swap_critical_percent": 1000,
            "kernel_pressure": False,
            "available_critical_percent": 0,
            "available_warn_percent": 0,
            "budget_percent": 1000,
        }
        data.update(extra)
        with open(os.path.join(self.state_dir, "devguard.json"), "w") as f:
            json.dump(data, f)

    def app(self, name):
        path = os.path.join(self.home, name)
        bin_dir = os.path.join(path, "node_modules/next/dist/bin")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "next"), "w") as f:
            f.write(FAKE_SERVER)
        return path

    def serve(self, app, mb=48):
        """Podróbka `next dev`: argv jak u prawdziwego, nasłuch na porcie i balast w RAM."""
        port = free_port()
        proc = subprocess.Popen(
            [
                "/usr/bin/python3",
                "node_modules/next/dist/bin/next",
                "dev",
                "-p",
                str(port),
                "--mb",
                str(mb),
            ],
            cwd=app,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        return proc, port

    def stack(self, app):
        """Podróbka `pnpm dev` z korzenia: powłoka z dev serwerem i backendem, który nie jest
        serwerem Node (jak Go na :8003), ale też należy do tej komendy."""
        web, api = free_port(), free_port()
        backend = os.path.join(self.home, "backend.py")
        with open(backend, "w") as f:
            f.write(
                FAKE_SERVER.replace(
                    'ballast = bytearray(int(sys.argv[sys.argv.index("--mb") + 1]) * 1024 * 1024)',
                    "ballast = b''",
                )
            )
        script = (
            f"/usr/bin/python3 {backend} -p {api} & "
            f"/usr/bin/python3 node_modules/next/dist/bin/next dev -p {web} --mb 8; wait"
        )
        proc = subprocess.Popen(
            ["/bin/sh", "-c", script], cwd=app, start_new_session=True
        )
        self.addCleanup(lambda: (os.killpg(proc.pid, 9), proc.wait()))
        for port in (web, api):
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.2)
        return web, api

    def test_backend_of_the_command_counts_as_its_port_and_its_viewers(self):
        web, api = self.stack(self.app("mono"))
        snap = json.loads(self.run_guard("status", "--json"))
        [u] = snap["units"]
        self.assertEqual(sorted(u["ports"]), sorted([web, api]))
        self.assertEqual(u["clients"], [])
        client = socket.create_connection(("127.0.0.1", api))
        self.addCleanup(client.close)
        snap = json.loads(self.run_guard("status", "--json"))
        self.assertEqual([c["pid"] for c in snap["units"][0]["clients"]], [os.getpid()])

    def run_guard(self, *args, stdin=None):
        env = dict(os.environ, HOME=self.home, DEVGUARD_ORCA="")
        done = subprocess.run(
            ["/usr/bin/python3", SCRIPT, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_status_sees_server_port_and_size(self):
        _proc, port = self.serve(self.app("web"))
        out = self.run_guard("status", "--json")
        snap = json.loads(out)
        [u] = snap["units"]
        self.assertEqual(u["ports"], [port])
        self.assertGreater(u["footprint"], 40 * 1024**2)
        self.assertEqual(u["kinds"], ["next"])

    def test_bloated_server_without_viewers_is_stopped(self):
        proc, _port = self.serve(self.app("web"))
        out = self.run_guard("once")
        self.assertIn("wykonane: stop", out)
        self.assertIsNotNone(proc.wait(timeout=15))
        with open(os.path.join(self.state_dir, "devguard-state.json")) as f:
            event = json.load(f)["events"][-1]
        self.assertTrue(event["ok"])

    def test_dry_run_only_reports(self):
        proc, _port = self.serve(self.app("web"))
        out = self.run_guard("once", "--dry-run")
        self.assertIn("zatrzymam", out)
        self.assertIsNone(proc.poll())

    def test_observe_mode_only_reports(self):
        self.config(mode="observe")
        proc, _port = self.serve(self.app("web"))
        self.run_guard("once")
        time.sleep(0.5)
        self.assertIsNone(proc.poll())

    def test_small_server_stays(self):
        self.config(max_server_gb=1)
        proc, _port = self.serve(self.app("web"), mb=8)
        self.run_guard("once")
        time.sleep(0.5)
        self.assertIsNone(proc.poll())

    def admit(self, command, cwd):
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        out = self.run_guard("admit", stdin=json.dumps(event))
        return json.loads(out)["hookSpecificOutput"] if out.strip() else None

    def test_admit_sends_agent_to_running_server(self):
        app = self.app("web")
        _proc, port = self.serve(app)
        verdict = self.admit("rtk proxy pnpm exec next dev -p 4000", app)
        self.assertEqual(verdict["permissionDecision"], "deny")
        self.assertIn(f"http://localhost:{port}", verdict["permissionDecisionReason"])

    def test_admit_lets_through_other_apps_and_other_commands(self):
        self.serve(self.app("web"))
        other = self.app("blog")
        self.assertIsNone(self.admit("pnpm exec next dev", other))
        self.assertIsNone(self.admit("pnpm build", self.home + "/web"))
        self.assertIsNone(
            self.admit("DEVGUARD_ALLOW=1 pnpm exec next dev", self.home + "/web")
        )

    def test_admit_blocks_over_budget(self):
        self.config(budget_percent=0.0001)
        self.serve(self.app("web"))
        verdict = self.admit("pnpm exec next dev", self.app("blog"))
        self.assertEqual(verdict["permissionDecision"], "deny")
        self.assertIn("budżetu", verdict["permissionDecisionReason"])

    def go_module(self, name):
        root = os.path.join(self.home, name)
        os.makedirs(os.path.join(root, ".git"), exist_ok=True)
        with open(os.path.join(root, "go.mod"), "w") as f:
            f.write("module x\n")
        return root

    def test_admit_wraps_go_commands_in_the_scheduler(self):
        # bez słów dev serwera: szybka ścieżka; ze słowem "dev" w ścieżce: pełna ścieżka
        for name in ("svc", "devtools"):
            root = self.go_module(name)
            command = "go test -count=1 ./... 2>&1 | tail -5"
            event = {
                "tool_name": "Bash",
                "cwd": root,
                "session_id": "s-1",
                "tool_input": {
                    "command": command,
                    "timeout": 600000,
                    "description": "testy",
                },
            }
            out = json.loads(self.run_guard("admit", stdin=json.dumps(event)))[
                "hookSpecificOutput"
            ]
            self.assertNotIn("permissionDecision", out)
            updated = out["updatedInput"]
            self.assertEqual(
                (updated["timeout"], updated["description"]), (600000, "testy")
            )
            argv = shlex.split(updated["command"])
            self.assertIn("run", argv)
            self.assertEqual(argv[argv.index("--via") + 1], "hook")
            self.assertEqual(argv[-2:], ["--shell", command])
        self.assertIsNone(self.admit("go version", self.go_module("svc")))
        self.assertIsNone(self.admit("rtk git status", self.go_module("svc")))

    @unittest.skipUnless(os.path.exists(os.path.join(ROOT, "acc.py")), "brak acc.py")
    def test_running_guard_is_found_by_its_lock_under_the_launcher(self):
        """launchd startuje `acc.py devguard run`: `once` i tak widzi działającego strażnika,
        bo szuka jego zamka, a nie linii komend."""
        import fcntl

        lock = open(os.path.join(self.state_dir, "devguard.lock"), "w")  # noqa: SIM115
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = subprocess.run(
            [sys.executable, os.path.join(ROOT, "acc.py"), "devguard", "once"],
            capture_output=True,
            text=True,
            env=dict(os.environ, HOME=self.home, DEVGUARD_ORCA=""),
            timeout=120,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("strażnik działa w tle", done.stdout)

    def test_words_are_the_fast_path_lists(self):
        words = json.loads(self.run_guard("words"))
        self.assertEqual(
            words, {"dev": list(entry.DEV_WORDS), "go": list(entry.GO_WORDS)}
        )


class ProcessTableTest(unittest.TestCase):
    """Tabela procesów z jądra zamiast `ps`: ta sama komenda, rodzic i kolejność."""

    def test_own_process_reads_like_ps(self):
        # tab i nowa linia ps pisze ósemkowo, resztę sterujących jako ^X; UTF-8 i \\ bez zmian
        argv = ["a\tb", "zażółć", "x\\y", "q\x01r", "nl\nx", "del\x7fz", "end  "]
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", "arg0", *argv])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        time.sleep(0.2)
        ps = subprocess.run(
            ["ps", "-o", "ppid=,command=", "-p", str(proc.pid)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.rstrip("\n")
        ppid, command = ps.split(None, 1)
        mine = {pid: (parent, cmd) for pid, parent, cmd in dg.processes()}
        self.assertEqual(mine[proc.pid], (int(ppid), command))
        self.assertIn("a\\011b", command)

    def test_other_users_process_shows_its_name(self):
        self.assertIn((1, 0, "(launchd)"), dg.processes())

    def test_zombie_reads_like_ps(self):
        proc = subprocess.Popen(["/bin/sleep", "0"])
        self.addCleanup(proc.wait)
        # skończył, a nikt go jeszcze nie odebrał; na zajętym Macu wyjście trwa dłużej niż
        # stała pauza, więc czekamy, aż jądro go pokaże, najwyżej 10 s
        row = (proc.pid, os.getpid(), "<defunct>")
        deadline = time.time() + 10
        while row not in dg.processes() and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn(row, dg.processes())

    def test_order_is_the_order_of_ps(self):
        out = subprocess.run(
            ["ps", "-axo", "pid="], capture_output=True, text=True, check=True
        ).stdout
        mine = [pid for pid, _ppid, _command in dg.processes()]
        common = set(mine) & {int(p) for p in out.split()}
        self.assertGreater(len(common), 10)
        self.assertEqual(
            [p for p in map(int, out.split()) if p in common],
            [p for p in mine if p in common],
        )


def lsof_sockets(pid):
    """Gniazda TCP jednego procesu tak, jak strażnik czytał je z lsof przed libproc."""
    out = subprocess.run(
        ["lsof", "-nP", "-w", "-a", "-p", str(pid), "-iTCP", "-FpnT"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    listen, links = {}, []
    name = ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "n":
            name = value
        elif value == "ST=LISTEN":
            listen.setdefault(int(name.rpartition(":")[2]), set()).add(pid)
        elif value == "ST=ESTABLISHED" and "->" in name:
            local, remote = name.split("->", 1)
            remote_host, _, port = remote.rpartition(":")
            if remote_host in dg.LOOPBACK or remote_host == local.rpartition(":")[0]:
                links.append((pid, int(port)))
    return listen, links


class SocketsTest(unittest.TestCase):
    """Gniazda z proc_pidfdinfo zamiast lsof: te same porty i ci sami klienci."""

    def test_listeners_and_loopback_clients_match_lsof(self):
        """Serwery tutaj, klient w osobnym procesie: klient to (jego pid, port serwera)."""
        me = os.getpid()
        servers, ports = [], []
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            server = socket.socket(family)
            server.bind((host, 0))
            server.listen(4)
            self.addCleanup(server.close)
            servers.append(server)
            ports.append((host, server.getsockname()[1]))
        code = (
            "import socket, time\n"
            f"c = [socket.create_connection(a) for a in {ports!r}]\n"
            "print('ok', flush=True)\n"
            "time.sleep(30)\n"
        )
        client = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(client.stdout.close)
        self.addCleanup(lambda: (client.kill(), client.wait()))
        self.assertEqual(client.stdout.readline().strip(), "ok")
        for server in servers:
            accepted, _ = server.accept()
            self.addCleanup(accepted.close)
        listen, links = dg.sockets()
        for pid in (me, client.pid):
            mine = (
                {p: {pid} for p, pids in listen.items() if pid in pids},
                sorted(link for link in links if link[0] == pid),
            )
            want = lsof_sockets(pid)
            self.assertEqual(mine, (want[0], sorted(want[1])), pid)
        for _host, port in ports:
            self.assertIn(me, listen[port])
            self.assertIn((client.pid, port), links)


class FakeOrcaRuntime:
    """Gniazdo jak u działającej Orki: odpowiada na każde żądanie, zapisuje je i sprawdza token."""

    def __init__(self, test, reply):
        import threading

        self.dir = tempfile.mkdtemp(prefix="orca-", dir="/tmp")
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "s")
        self.metadata = os.path.join(self.dir, "orca-runtime.json")
        with open(self.metadata, "w") as f:
            json.dump(
                {
                    "runtimeId": "rt-1",
                    "authToken": "tok",
                    "transports": [{"kind": "unix", "endpoint": self.path}],
                },
                f,
            )
        self.reply = reply
        self.requests = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(8)
        test.addCleanup(self.server.close)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("rb").readline()
                request = json.loads(line)
                self.requests.append(request)
                conn.sendall(
                    b'{"_keepalive":true}\n'
                    + json.dumps(self.reply(request)).encode()
                    + b"\n"
                )


class OrcaSocketTest(unittest.TestCase):
    """Odczyty Orki przez jej gniazdo, z CLI jako zapasem; zmiany zawsze przez CLI."""

    def orca(self, reply):
        runtime = FakeOrcaRuntime(self, reply)
        cli = os.path.join(runtime.dir, "orca")
        with open(cli, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'echo "$*" >> {runtime.dir}/cli.log\n'
                'echo \'{"ok": true, "result": {"from": "cli"}}\'\n'
            )
        os.chmod(cli, 0o755)
        orca = dg.Orca()
        orca.bin, orca.direct, orca.metadata = cli, True, runtime.metadata
        return orca, runtime

    def cli_log(self, runtime):
        try:
            with open(os.path.join(runtime.dir, "cli.log")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_reads_go_through_the_socket_with_the_cli_request(self):
        orca, runtime = self.orca(
            lambda r: {
                "id": r["id"],
                "ok": True,
                "result": {"m": r["method"]},
                "_meta": {"runtimeId": "rt-1"},
            }
        )
        self.assertEqual(orca.call("worktree", "ps"), {"m": "worktree.ps"})
        self.assertEqual(
            orca.call("tab", "list", "--worktree", "all"), {"m": "browser.tabList"}
        )
        self.assertEqual(orca.call("terminal", "list"), {"m": "terminal.list"})
        self.assertEqual(
            orca.call("diagnostics", "memory"), {"m": "diagnostics.memory"}
        )
        self.assertEqual(self.cli_log(runtime), [])
        params = [r.get("params", "brak") for r in runtime.requests]
        self.assertEqual(params, [{}, {}, {"includeVisualLayouts": False}, "brak"])
        self.assertEqual({r["authToken"] for r in runtime.requests}, {"tok"})

    def test_failed_answer_is_none_and_foreign_answer_falls_back_to_cli(self):
        orca, runtime = self.orca(
            lambda r: {"id": r["id"], "ok": False, "error": {"code": "x"}}
        )
        self.assertIsNone(orca.call("worktree", "ps"))
        orca, runtime = self.orca(
            lambda r: {"id": "someone-else", "ok": True, "result": {}}
        )
        self.assertEqual(orca.call("worktree", "ps"), {"from": "cli"})
        self.assertEqual(self.cli_log(runtime), ["worktree ps --json"])

    def test_changes_go_through_the_cli(self):
        orca, runtime = self.orca(lambda r: {"id": r["id"], "ok": True, "result": {}})
        orca.call("tab", "close", "--page", "p-1")
        self.assertEqual(runtime.requests, [])
        self.assertEqual(self.cli_log(runtime), ["tab close --page p-1 --json"])

    def test_fake_cli_and_remote_orca_never_use_the_socket(self):
        with mock.patch.dict(os.environ, {"DEVGUARD_ORCA": "/x/orca"}):
            self.assertFalse(dg.Orca().direct)
        env = {k: v for k, v in os.environ.items() if k != "DEVGUARD_ORCA"}
        with mock.patch.dict(
            os.environ, dict(env, ORCA_PAIRING_CODE="orca://pair?x"), clear=True
        ):
            self.assertFalse(dg.Orca().direct)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(dg.Orca().direct)


class AdmitFastPathTest(unittest.TestCase):
    """Wejście hooka (devguard.py): komenda bez dev serwera nie płaci za strażnika."""

    def event(self, command, cwd="/w/mono"):
        return json.dumps(
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
        )

    def admit_in_clean_python(self, command, cwd="/w/mono"):
        """(wyjście hooka, ciężkie moduły, które załadował) w czystym interpreterze (-I -S)."""
        code = (
            "import io, sys\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "import devguard\n"
            f"sys.stdin = io.StringIO({self.event(command, cwd)!r})\n"
            "devguard.main(['admit'])\n"
            "heavy = ('json', 're', 'devguard_core', 'janitor', 'ctypes', 'subprocess')\n"
            "print(','.join(m for m in heavy if m in sys.modules))\n"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        *out, loaded = done.stdout.splitlines()
        return "\n".join(out), [m for m in loaded.split(",") if m]

    def test_plain_command_loads_nothing(self):
        self.assertEqual(self.admit_in_clean_python("rtk git status --short"), ("", []))

    def test_dev_word_without_dev_server_skips_the_guard(self):
        out, loaded = self.admit_in_clean_python(
            "rtk ls -la /tmp 2>/dev/null && git log origin/dev"
        )
        self.assertEqual((out, loaded), ("", ["json", "re"]))

    def test_go_command_loads_only_the_scheduler(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".git"))
            with open(os.path.join(root, "go.mod"), "w") as f:
                f.write("module x\n")
            out, loaded = self.admit_in_clean_python("go test ./...", cwd=root)
        self.assertIn("updatedInput", json.loads(out)["hookSpecificOutput"])
        self.assertEqual(loaded, ["json", "re"])

    def test_escaped_json_still_reaches_the_dev_server_check(self):
        raw = self.event("pnpm dev").replace("dev", "\\u0064ev")
        self.assertNotIn("dev", raw)
        guard = types.SimpleNamespace(main=mock.Mock(return_value=0))
        with mock.patch.object(entry, "core", return_value=guard):
            self.assertEqual(entry.admit(raw), 0)
        guard.main.assert_called_once_with(["admit"])

    def test_dev_server_start_goes_to_the_guard_and_allow_does_not(self):
        guard = types.SimpleNamespace(main=mock.Mock(return_value=0))
        with (
            mock.patch.object(entry, "core", return_value=guard),
            mock.patch.object(entry, "sched_rewrite", return_value=None),
        ):
            entry.admit(self.event("cd apps/web && pnpm exec next dev"))
            entry.admit(self.event("DEVGUARD_ALLOW=1 pnpm dev"))
            entry.admit(self.event("pnpm build 2>/dev/null"))
        self.assertEqual(guard.main.call_count, 1)


if __name__ == "__main__":
    unittest.main()
