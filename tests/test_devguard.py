"""Testy devguard.py: decyzje na sztucznym obrazie świata i strażnik na prawdziwych procesach.

Decyzje (`decide`, presja, rozpoznawanie komend agentów) testujemy wprost na funkcjach.
Testy z procesami stawiają podróbkę `next dev` (Python udający serwer z balastem w RAM)
w osobnym $HOME, a konfiguracja ogranicza strażnika do katalogu testu (`scope`), więc
prawdziwe dev serwery na tym Macu są dla niego niewidoczne. Orka jest wyłączona.

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

import devguard as dg

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
            "available_critical_percent": 0,
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
            # z rtk na PATH scheduler sam wstawia rtk w środku (hook rtk ma Go w wyjątkach)
            inner = ("rtk " + command) if shutil.which("rtk") else command
            self.assertEqual(argv[-2:], ["--shell", inner])
        self.assertIsNone(self.admit("go version", self.go_module("svc")))
        self.assertIsNone(self.admit("rtk git status", self.go_module("svc")))

    def test_admit_wraps_node_commands_in_the_scheduler(self):
        # testy JS w każdym projekcie z package.json; pnpm idzie szybką ścieżką, vitest (ma w sobie
        # "vite") pełną; dev serwer i instalacja zostają poza kolejką
        root = os.path.join(self.home, "shop")
        os.makedirs(os.path.join(root, ".git"), exist_ok=True)
        with open(os.path.join(root, "package.json"), "w") as f:
            f.write('{"name": "shop"}')
        for command in ("pnpm test 2>&1 | tail -5", "npx vitest run"):
            out = self.admit(command, root)
            self.assertIsNotNone(out, command)
            argv = shlex.split(out["updatedInput"]["command"])
            self.assertEqual(argv[argv.index("--via") + 1], "hook", command)
            self.assertIn("vitest" if "vitest" in command else "pnpm test", argv[-1])
        self.assertIsNone(self.admit("pnpm install", root))


if __name__ == "__main__":
    unittest.main()
