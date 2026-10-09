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
import signal
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
        started=NOW - 3600,
        ports=[],
    )
    for name, value in attrs.items():
        setattr(u, name, value)
    u.last_watched = attrs.get("last_watched", NOW if u.watched else NOW - 7200)
    return u


def world(*units, level=0, reasons=(), fsevents_restart=0, simulators=()):
    pressure = types.SimpleNamespace(level=level, ram=48 * GB, reasons=list(reasons))
    return types.SimpleNamespace(
        now=NOW, pressure=pressure, units=list(units), fsevents_restart=fsevents_restart,
        simulators=list(simulators),
    )


def sim(udid, fp_gb=2.0, name=None, lease=None, watched=False, quiet=3600, age=3600):
    """Symulator tak, jak widzi go `decide`: domyślnie z puli, bez dzierżawy, cichy od godziny."""
    name = name or f"Portivo-{udid}"
    pool = name.startswith("Portivo-")
    return types.SimpleNamespace(
        key=f"sim:{udid}",
        udid=udid,
        name=name,
        label=f"symulator {name}",
        app_key=f"sim:{udid}",
        footprint=fp_gb * GB,
        pool=pool,
        lease=lease,
        lease_alive=lease == "alive",
        watched=watched,
        in_use=lease == "alive" or watched or not pool,
        quiet=quiet,
        age=age,
        protected=False,
        ports=[],
        pids=[],
        argv=None,
        launch_cwd="",
    )


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


class NoteTest(unittest.TestCase):
    """Notka na karcie worktree: tylko gdy karta jest pusta albo ma notkę devguard lub linię wtyczki Orki."""

    def calls(self, comment):
        orca = mock.Mock()
        unit = types.SimpleNamespace(worktree={"worktreeId": "r::/w", "comment": comment})
        dg.note({"orca_comment": True}, orca, unit, "devguard: zatrzymałem :3000")
        return orca.call.call_args_list

    def test_writes_over_its_own_note_and_the_plugin_line(self):
        for comment in ("", "devguard: stare", "claude-acc: :3000 4.0 GB"):
            self.assertEqual(len(self.calls(comment)), 1, comment)

    def test_leaves_a_user_comment_alone(self):
        self.assertEqual(self.calls("review the auth flow"), [])


class RegrowTest(unittest.TestCase):
    """Ile stos może jeszcze urosnąć: każdy proces do swojego szczytu. Scheduler trzyma na to
    miejsce, więc niedoszacowanie oznacza wpuszczenie roboty, która potem wypycha serwery do swapu."""

    def test_each_process_can_grow_back_to_its_own_peak(self):
        stats = {
            1: {"footprint": 2 * GB, "peak": 5 * GB},  # turbopack po odśnieżeniu cache
            2: {"footprint": 1 * GB, "peak": 1 * GB},
            3: {"footprint": GB // 2, "peak": 3 * GB},  # worker, który spuchł przy buildzie trasy
        }
        fake = lambda pid: dict(stats[pid], cpu=0.0, written=0, start=NOW) if pid in stats else None
        table = {1: (99, "pnpm dev"), 99: (1, "-zsh")}
        with mock.patch.object(dg, "usage", fake), mock.patch.object(dg, "proc_cwd", lambda pid: "/w/app"), \
                mock.patch.object(dg, "proc_argv", lambda pid: ["pnpm", "dev"]):
            server = dg.Server(1, "next", "next dev", [1, 2, 3])
            # 3 GB pierwszego i 2,5 GB trzeciego; sam szczyt największego minus suma dałby 1,5 GB
            self.assertEqual(server.regrow, 5.5 * GB)
            other = dg.Server(2, "next", "next dev", [2])
            stack = dg.Unit(1, [server, other], table, {1: [2, 3]})
        self.assertEqual(stack.summary()["regrow"], 5.5 * GB)


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


class FseventsRestartTest(unittest.TestCase):
    """Po restarcie fseventsd obserwatory plików sprzed niego są głuche: serwer bez restartu
    nie widzi edycji agenta. Pomyłki, które ten test łapie: restart serwera postawionego już
    po restarcie demona (pętla bez końca) i ruszanie serwera chronionego."""

    def test_servers_started_before_the_restart_are_recycled(self):
        old = unit("old", started=NOW - 600, quiet=0)
        fresh = unit("fresh", started=NOW - 30, quiet=0)

        got = plans(world(old, fresh, fsevents_restart=NOW - 60))

        self.assertEqual(got, [("old", "recycle")])

    def test_protected_and_unmanaged_servers_stay(self):
        mine = unit("mine", started=NOW - 600, quiet=0, protected=True)
        agents = unit("agents", started=NOW - 600, quiet=0, recyclable=False)

        self.assertEqual(plans(world(mine, agents, fsevents_restart=NOW - 60)), [])

    def test_pinned_is_recycled_unless_no_restart(self):
        # przypięcie chroni przed zatrzymaniem, nie przed restartem; --no-restart przed obydwoma
        held = unit("held", started=NOW - 600, quiet=0, protected=True, pin=PIN_HOLD, ports=[3747])
        kept = unit("kept", started=NOW - 600, quiet=0, protected=True, pin=PIN_KEEP, ports=[3748])

        self.assertEqual(plans(world(held, kept, fsevents_restart=NOW - 60)), [("kept", "recycle")])

    def test_no_restart_no_recycle(self):
        self.assertEqual(plans(world(unit("old", started=NOW - 600, quiet=0))), [])


UDID = "BF23E1F4-BE16-45DA-A285-F01B892F4116"
SIM_APP = f"/Users/x/Library/Developer/CoreSimulator/Devices/{UDID}/data/Containers/Bundle/Application/7A/Shop.app/Shop"


class SimulatorTest(unittest.TestCase):
    """Symulatory: najwyżej `max_booted_simulators` włączonych naraz, a nieużywane wracają do
    Shutdown. Pomyłki, które ten test łapie: wyłączenie symulatora sesji, która żyje (dzierżawa
    portivo-mobile); symulatora, na który ktoś patrzy (serve-sim, maestro z jego UDID) albo
    człowieka spoza puli Portivo-*; świeżo włączonego; i symulatory po 2-4 GB, które zostają
    włączone na zawsze po skończonych sesjach (2026-10-08: kilka naraz obok buildu iOS)."""

    def test_over_cap_shuts_down_the_idlest_unused_pool_simulator(self):
        sims = [sim("A", lease="alive", quiet=10), sim("B", quiet=20 * 60), sim("C", lease="dead", quiet=8 * 60)]
        self.assertEqual(plans(world(simulators=sims))[0], ("sim:B", "shutdown"))

    def test_sessions_watchers_people_and_young_ones_keep_their_simulators(self):
        sims = [
            sim("A", lease="alive"),
            sim("B", watched=True),
            sim("C", name="iPhone 17 Pro"),
            sim("D", age=30, quiet=30),
        ]
        got = plans(world(simulators=sims))
        self.assertEqual([action for _key, action in got], ["warn"])  # ponad limit, nie ma czego wyłączyć

    def test_unused_simulator_goes_after_the_idle_time_even_under_the_cap(self):
        self.assertEqual(plans(world(simulators=[sim("A", quiet=40 * 60)])), [("sim:A", "shutdown")])
        self.assertEqual(
            plans(world(simulators=[sim("A", lease="dead", quiet=40 * 60)])), [("sim:A", "shutdown")]
        )
        self.assertEqual(plans(world(simulators=[sim("A", quiet=10 * 60)])), [])
        self.assertEqual(plans(world(simulators=[sim("A", lease="alive", quiet=5 * 3600)])), [])
        self.assertEqual(plans(world(simulators=[sim("A", name="iPhone 17", quiet=5 * 3600)])), [])

    def test_cap_and_idle_time_come_from_the_config(self):
        sims = [sim("A", lease="alive"), sim("B", quiet=6 * 60)]
        self.assertEqual(plans(world(simulators=sims)), [])  # 2 przy limicie 2
        self.assertEqual(plans(world(simulators=sims), max_booted_simulators=1), [("sim:B", "shutdown")])
        self.assertEqual(
            plans(world(simulators=[sim("A", quiet=6 * 60)]), simulator_idle_minutes=5),
            [("sim:A", "shutdown")],
        )

    def test_lease_is_alive_only_while_its_session_process_is(self):
        lstart = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True
        ).stdout.strip()
        self.assertTrue(dg.lease_alive({"owner": {"pid": os.getpid(), "start": lstart}}))
        self.assertTrue(dg.lease_alive({"owner": {"pid": os.getpid(), "start": ""}}))
        self.assertFalse(dg.lease_alive({"owner": {"pid": os.getpid(), "start": "Mon Jan  1 00:00:00 2001"}}))
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.assertFalse(dg.lease_alive({"owner": {"pid": gone.pid, "start": ""}}))
        self.assertFalse(dg.lease_alive(None))

    def test_booted_simulators_come_from_the_process_table(self):
        import plistlib

        root = os.path.realpath(tempfile.mkdtemp(prefix="devguard-sims-"))
        self.addCleanup(shutil.rmtree, root, True)
        devices, leases = os.path.join(root, "Devices"), os.path.join(root, "leases")
        os.makedirs(os.path.join(devices, UDID))
        os.makedirs(leases)
        with open(os.path.join(devices, UDID, "device.plist"), "wb") as f:
            plistlib.dump({"name": "Portivo-Auto-1", "UDID": UDID}, f)
        with open(os.path.join(leases, UDID + ".json"), "w") as f:
            json.dump({"owner": {"pid": os.getpid(), "start": ""}, "app": "storefront-mobile"}, f)
        runtime = "/Library/Developer/CoreSimulator/Volumes/iOS_23A343/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 26.0.simruntime/Contents/Resources/RuntimeRoot"
        rows = [
            (100, 1, f"{runtime}/sbin/launchd_sim /Users/x/Library/Developer/CoreSimulator/Devices/{UDID}/data/var/run/launchd_bootstrap.plist"),
            (101, 100, f"{runtime}/System/Library/CoreServices/SpringBoard.app/SpringBoard"),
            (102, 100, SIM_APP),
            (200, 1, f"/opt/homebrew/bin/serve-sim --device {UDID}"),
            (300, 1, "/bin/zsh -l"),
        ]
        with mock.patch.object(dg, "SIM_DEVICES", devices):
            found = dg.discover_simulators(cfg(simulator_leases=leases), rows)
        self.assertEqual(len(found), 1)
        s = found[0]
        self.assertEqual((s.udid, s.name, s.pool), (UDID, "Portivo-Auto-1", True))
        self.assertEqual(sorted(s.pids), [100, 101, 102])
        self.assertEqual(s.watchers, [200])
        self.assertTrue(s.lease_alive and s.watched and s.in_use)

    def discover(self, rows, names, leases=None, **extra):
        import plistlib

        root = os.path.realpath(tempfile.mkdtemp(prefix="devguard-sims-"))
        self.addCleanup(shutil.rmtree, root, True)
        devices, lease_dir = os.path.join(root, "Devices"), os.path.join(root, "leases")
        os.makedirs(lease_dir)
        for udid, name in names.items():
            os.makedirs(os.path.join(devices, udid))
            with open(os.path.join(devices, udid, "device.plist"), "wb") as f:
                plistlib.dump({"name": name, "UDID": udid}, f)
        for udid, lease in (leases or {}).items():
            with open(os.path.join(lease_dir, udid + ".json"), "w") as f:
                json.dump(lease, f)
        with mock.patch.object(dg, "SIM_DEVICES", devices):
            return {s.udid: s for s in dg.discover_simulators(cfg(simulator_leases=lease_dir, **extra), rows)}

    def test_who_uses_a_simulator(self):
        """Nikt nie trzyma i nikt nie patrzy: tylko wtedy symulator z puli jest nieużywany. Twój
        (spoza puli) i chroniony w configu zawsze są w użyciu; serve-sim bez UDID i `simctl ...
        booted` patrzą na każdy włączony."""
        other = "0F6A3C2B-1111-4222-8333-944455556666"
        names = {UDID: "Portivo-Auto-1", other: "iPhone 17 Pro"}
        rows = [
            (100, 1, f"launchd_sim /Users/x/Library/Developer/CoreSimulator/Devices/{UDID}/data/var/run/launchd_bootstrap.plist"),
            (110, 1, f"launchd_sim /Users/x/Library/Developer/CoreSimulator/Devices/{other}/data/var/run/launchd_bootstrap.plist"),
            (300, 1, "/bin/zsh -l"),
        ]
        found = self.discover(rows, names)
        self.assertFalse(found[UDID].in_use)
        self.assertTrue(found[other].in_use)
        self.assertTrue(self.discover(rows, names, simulator_protect=["Portivo-Auto-1"])[UDID].in_use)
        for viewer in ("/opt/homebrew/bin/serve-sim", "xcrun simctl io booted recordVideo /tmp/a.mov"):
            found = self.discover(rows + [(400, 1, viewer)], names)
            self.assertEqual(found[UDID].watchers, [400], viewer)
            self.assertTrue(found[UDID].in_use, viewer)

    def test_performance_simulators_are_never_shut_down(self):
        """Portivo-Perf-* to symulatory sesji, które mierzą wydajność aplikacji iOS: bez dzierżawy
        i długo bez ruchu między pomiarami. Domyślny config chroni każdy taki (wzorzec, nie jedna
        nazwa), także po godzinach ciszy, ponad limitem i przy braku pamięci; zwykły symulator puli
        obok dalej idzie."""
        perf, ipad, auto = UDID, "0F6A3C2B-1111-4222-8333-944455556666", "6B1D2E3F-AAAA-4BBB-8CCC-DDDDEEEEFFFF"
        names = {perf: "Portivo-Perf-iPhone", ipad: "Portivo-Perf-iPad-Pro", auto: "Portivo-Auto-1"}
        rows = [
            (100 + i, 1, f"launchd_sim /Users/x/Library/Developer/CoreSimulator/Devices/{u}/data/var/run/launchd_bootstrap.plist")
            for i, u in enumerate(names)
        ]
        found = self.discover(rows, names)
        self.assertEqual({u: s.protected for u, s in found.items()}, {perf: True, ipad: True, auto: False})
        for s in found.values():
            s.age = s.quiet = 10 * 3600
        sims = list(found.values())
        # ponad limitem i przy presji: idzie tylko symulator spoza wzorca
        self.assertEqual(plans(world(simulators=sims, level=2, reasons=["swap"])), [(f"sim:{auto}", "shutdown")])
        # same chronione ponad limitem: ostrzeżenie, żadnego wyłączenia
        got = plans(world(simulators=sims[:2], level=2, reasons=["swap"]), max_booted_simulators=1,
                    simulator_idle_minutes=1, simulator_quiet_minutes=0)
        self.assertEqual([action for _key, action in got], ["warn"])
        # własna lista z innym wzorcem zastępuje domyślną
        mine = self.discover(rows, names, simulator_protect=["Portivo-Auto-*"])
        self.assertEqual({u: s.protected for u, s in mine.items()}, {perf: False, ipad: False, auto: True})

    def test_quiet_counts_from_the_last_use(self):
        """Cisza rośnie tylko, gdy CPU całego symulatora jest pod `simulator_busy_cores`, a nikt
        go nie trzyma i nikt nie patrzy (pomiar 2026-10-08: bezczynny 0,01 rdzenia)."""
        s = sim("A")
        state = {}

        def tick(at, cpu, **flags):
            s.start, s.cpu = 7, cpu
            s.lease_alive = flags.get("lease_alive", False)
            s.watched = flags.get("watched", False)
            dg.track_simulators(cfg(), state, [s], NOW + at)
            return s.quiet

        self.assertEqual(tick(0, 10.0), 0)
        self.assertEqual(tick(60, 10.6), 60)  # 0,01 rdzenia: bezczynny
        self.assertEqual(tick(120, 40.6), 0)  # 0,5 rdzenia: ktoś go używa
        self.assertEqual(tick(180, 41.2), 60)
        self.assertEqual(tick(240, 41.8, lease_alive=True), 0)  # sesja go trzyma
        self.assertEqual(tick(300, 42.4, watched=True), 0)  # maestro patrzy
        self.assertEqual(tick(900, 42.5), 600)

    def test_metro_counts_a_simulator_as_a_viewer_only_while_it_is_in_use(self):
        """Metro, któremu sesja umarła, zostaje przy życiu przez aplikację w symulatorze, którego
        nikt już nie używa. Ten widz się nie liczy; symulator sesji, która żyje, tak."""
        self.assertEqual(dg.client_kind(SIM_APP), "simulator")
        commands = {42: SIM_APP, 43: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
        metro = types.SimpleNamespace(clients=[(42, "simulator", "Shop"), (43, "browser", "Google Chrome")])
        dg.drop_unused_simulator_clients(metro, commands, {UDID: sim(UDID)})
        self.assertEqual(metro.clients, [(43, "browser", "Google Chrome")])
        self.assertEqual(metro.idle_sim_clients, [(42, "simulator", "Shop")])
        metro = types.SimpleNamespace(clients=[(42, "simulator", "Shop")])
        dg.drop_unused_simulator_clients(metro, commands, {UDID: sim(UDID, lease="alive")})
        self.assertEqual(metro.clients, [(42, "simulator", "Shop")])

    SHUTDOWN = ["xcrun", "simctl", "shutdown", "B"]

    def run_shutdown(self, front, lease=None, apps=()):
        target = sim("B", quiet=20 * 60)
        plan = dg.Plan(target, "shutdown", 85, "ponad limit", "simulator_cap")
        state = {}
        calls = []
        root = tempfile.mkdtemp(prefix="devguard-leases-")
        self.addCleanup(shutil.rmtree, root, True)
        leases = os.path.join(root, "leases")
        os.makedirs(leases)
        if lease:
            with open(os.path.join(leases, "B.json"), "w") as f:
                json.dump(lease, f)

        # `launchctl list` w symulatorze: aplikacje użytkownika i systemowe, jak w prawdziwym
        listing = "PID\tStatus\tLabel\n" + "".join(
            f"{100 + i}\t0\tUIKitApplication:{app}[3f2a][rb-legacy]\n"
            for i, app in enumerate(("com.apple.Spotlight",) + tuple(apps))
        )

        def fake_run(argv, **_kw):
            calls.append(argv)
            out = listing if argv[:3] == ["xcrun", "simctl", "spawn"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        with mock.patch.object(dg, "frontmost_bundle", return_value=front), \
                mock.patch.object(dg.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(dg, "log"), mock.patch.object(dg.janitor, "notify"):
            dg.execute(cfg(notify=False, simulator_leases=leases), plan, world(simulators=[target]), state)
        return calls, state

    def test_shutdown_goes_through_simctl_and_never_while_you_look_at_simulator(self):
        calls, state = self.run_shutdown("com.stablyai.orca")
        self.assertEqual(calls[-1], self.SHUTDOWN)
        self.assertEqual(state["events"][-1]["action"], "shutdown")
        for front in ("com.apple.iphonesimulator", None):  # None: lsappinfo nie odpowiedział
            calls, state = self.run_shutdown(front)
            self.assertNotIn(self.SHUTDOWN, calls, front)
            self.assertNotIn("events", state)

    def test_an_app_running_without_a_live_session_keeps_the_simulator(self):
        """Symulator z puli bez dzierżawy, w którym działa aplikacja, ktoś używa poza
        portivo-mobile (2026-10-08: Portivo-Perf-iPhone pod skryptem perf przez idb). Po martwej
        sesji zostają jej aplikacja i sterownik maestro: te nie trzymają symulatora."""
        calls, state = self.run_shutdown("com.stablyai.orca", apps=("eu.portivo.app",))
        self.assertNotIn(self.SHUTDOWN, calls)
        self.assertNotIn("events", state)
        dead = {"owner": {"pid": 0, "start": ""}, "bundle": "eu.portivo.app"}
        calls, _ = self.run_shutdown("com.stablyai.orca", lease=dead,
                                     apps=("eu.portivo.app", dg.MAESTRO_DRIVER))
        self.assertEqual(calls[-1], self.SHUTDOWN)
        calls, _ = self.run_shutdown("com.stablyai.orca", lease=dead, apps=("com.example.other",))
        self.assertNotIn(self.SHUTDOWN, calls)

    def test_a_held_shutdown_does_not_block_the_plans_below_it(self):
        """Tick robi pierwszy plan, który coś zrobił: wyłączenie wstrzymane, bo patrzysz na
        Simulator, nie może co 5 s zasłaniać zatrzymania serwera przy presji."""
        held = dg.Plan(sim("B"), "shutdown", 85, "ponad limit", "simulator_cap")
        server = unit("srv", 3)
        stop = dg.Plan(server, "stop", 80, "brak pamięci", "pressure", level=1)
        fake = world(server, simulators=[held.unit])
        fake.pressure.summary = lambda: {"level": 1}
        fake.pressure.swap_used = fake.pressure.compressed = 0
        fake.orca = None
        server.summary = held.unit.summary = lambda: {}
        done = []
        with mock.patch.object(dg, "World", return_value=fake), mock.patch.object(dg, "check_pending"), \
                mock.patch.object(dg, "decide", return_value=[held, stop]), \
                mock.patch.object(dg, "execute", side_effect=lambda c, p, w, s: done.append(p.action) or p.action != "shutdown"):
            _w, _p, acted = dg.tick(cfg(last_resort=False), {}, orca=None, now=NOW)
        self.assertEqual(done, ["shutdown", "stop"])
        self.assertIs(acted, stop)

    def test_shutdown_rereads_the_lease_a_session_may_have_just_taken(self):
        calls, state = self.run_shutdown("com.stablyai.orca", lease={"owner": {"pid": os.getpid(), "start": ""}})
        self.assertNotIn(self.SHUTDOWN, calls)
        self.assertNotIn("events", state)
        calls, _state = self.run_shutdown("com.stablyai.orca", lease={"owner": {"pid": 0, "start": ""}})
        self.assertEqual(calls[-1], self.SHUTDOWN)

    def test_metro_with_an_app_in_a_simulator_waits_while_you_look_at_simulator(self):
        metro = unit("metro", idle_sim_clients=[(42, "simulator", "Shop")], argv=None, launch_cwd="/w")
        plan = dg.Plan(metro, "stop", 50, "sierota", "orphan")
        stopped = []
        for front, expected in (("com.apple.iphonesimulator", []), ("com.stablyai.orca", ["metro"])):
            with mock.patch.object(dg, "frontmost_bundle", return_value=front), \
                    mock.patch.object(dg, "stop", side_effect=lambda u, _w: (stopped.append(u.key), (True, "zatrzymany"))[1]), \
                    mock.patch.object(dg, "after_stop"), mock.patch.object(dg, "log"):
                dg.execute(cfg(notify=False), plan, world(metro), {})
            self.assertEqual(stopped, expected)


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
            self.assertEqual(self.starts(command), [("/w/mono", None, False, False)], command)

    def test_cd_and_dir_flags_move_the_target(self):
        self.assertEqual(
            self.starts("cd apps/web && pnpm exec next dev"),
            [("/w/mono/apps/web", None, False, False)],
        )
        self.assertEqual(
            self.starts("pnpm -C apps/web exec next dev"),
            [("/w/mono/apps/web", None, False, False)],
        )

    def test_scripts_and_filters(self):
        self.assertEqual(self.starts("pnpm dev"), [("/w/mono", None, True, False)])
        self.assertEqual(self.starts("npm run dev:landing"), [("/w/mono", None, True, False)])
        self.assertEqual(
            self.starts("pnpm --filter landing-page dev"),
            [("/w/mono", "landing-page", False, False)],
        )

    def test_dev_server_started_through_orca_terminal(self):
        command = 'orca terminal create --worktree active --command "cd apps/web && pnpm exec next dev"'
        self.assertEqual(self.starts(command), [("/w/mono/apps/web", None, False, False)])

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
            self.starts("npx vite --port 5173"), [("/w/mono", None, False, False)]
        )
        self.assertEqual(self.starts("vite dev"), [("/w/mono", None, False, False)])

    def test_every_way_to_start_metro(self):
        """2026-10-08 18:52: przy krytycznej presji agent postawił Metro przez
        `./node_modules/.bin/expo start`, którego hook nie uznał za dev serwer (znał tylko
        `npx expo start` i `pnpm exec expo start`), i o 18:57 Mac zamarzł. Każda droga do CLI expo
        i do Metro to start dev serwera."""
        app = "/w/mono/apps/mobile"
        for command in (
            "./node_modules/.bin/expo start --dev-client --port 8199",
            "node_modules/.bin/expo start --port 8183",
            "TTP_DEV_ENTITLEMENT=1 ./node_modules/.bin/expo start --dev-client --port 8183 > /tmp/m.log 2>&1",
            "/w/mono/apps/mobile/node_modules/.bin/expo start",
            "node /w/mono/node_modules/.pnpm/expo@58.0.3_0823/node_modules/expo/bin/cli start --port 8183",
            "npx expo start",
            "npx expo@latest start --port 8085",
            "pnpm exec expo start --port 8085",
            "bunx expo start",
            "pnpm expo start",
            "npx react-native start",
        ):
            self.assertEqual(self.starts(command, app), [(app, None, False, False)], command)
        self.assertEqual(
            self.starts(
                "cd /w/wt/apps/storefront-mobile && ./node_modules/.bin/expo start --dev-client --port 8183"
            ),
            [("/w/wt/apps/storefront-mobile", None, False, False)],
        )
        self.assertEqual(
            self.starts("pnpm --filter mobile exec expo start"), [("/w/mono", "mobile", False, False)]
        )

    def test_restart_commands_the_guard_logs(self):
        """Komendę wznowienia z logu strażnika (node i ścieżka skryptu CLI) agent kopiuje 1:1."""
        self.assertEqual(
            self.starts("node /w/x/node_modules/next/dist/bin/next dev -p 3292"),
            [("/w/mono", None, False, False)],
        )
        self.assertEqual(
            self.starts("cd /w/avatar && node /opt/homebrew/bin/pnpm --filter whale dev"),
            [("/w/avatar", "whale", False, False)],
        )

    def test_expo_run_starts_metro_unless_no_bundler(self):
        """`expo run:ios` po buildzie stawia Metro (albo bierze to, które już serwuje aplikację),
        `--no-bundler` tylko buduje; pomoc, prebuild, export i config niczego nie stawiają."""
        self.assertEqual(self.starts("npx expo run:ios"), [("/w/mono", None, False, True)])
        self.assertEqual(
            self.starts("./node_modules/.bin/expo run:android --device emulator-5554"),
            [("/w/mono", None, False, True)],
        )
        for command in (
            "npx expo run:ios --no-bundler",
            "npx expo start --help",
            "./node_modules/.bin/expo run:ios --help",
            "npx expo prebuild --platform ios",
            "npx expo export --platform ios",
            "./node_modules/.bin/expo config --json",
        ):
            self.assertEqual(self.starts(command), [], command)


class DuplicateRefusalTest(unittest.TestCase):
    def test_refusal_inside_a_stack_names_the_server_of_this_app(self):
        """2026-10-08, hook na żywo: `./node_modules/.bin/expo start` w apps/mobile, gdy stos
        `pnpm dev` serwuje Metro tej aplikacji na :8083, dostał w odmowie http://localhost:3000
        i katalog innej aplikacji (pierwszy port i pierwszy serwer stosu). Agent szedłby pod
        zły adres."""
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        landing, mobile = (os.path.join(root, name) for name in ("landing", "mobile"))
        for path in (landing, mobile):
            os.makedirs(os.path.join(path, "node_modules/.bin"))
        servers = [
            types.SimpleNamespace(cwd=landing, ports=[3000]),
            types.SimpleNamespace(cwd=mobile, ports=[8083]),
        ]
        stack = unit(
            "stack", 6.3, servers=servers, ports=[3000, 8083], root=4242, terminal=None,
            launch_cwd=root,
        )
        event = {"tool_input": {"command": "./node_modules/.bin/expo start --port 8199"}, "cwd": mobile}
        with mock.patch.object(dg, "World", return_value=world(stack)), \
                mock.patch.object(dg.janitor, "load_json", return_value={}):
            why = dg.devserver_refusal(cfg(), event)
        self.assertIn(f"już działa http://localhost:8083 ({dg.short(mobile)}", why)
        self.assertNotIn("localhost:3000", why)


class TerminateTest(unittest.TestCase):
    """Proces po SIGKILL pod presją kończy się sekundami (jądro zwalnia jego strony ze swapu i
    kompresora). 2026-10-08 log strażnika dwa razy mówił „nie chcą zginąć” o Metro, które za
    chwilę zniknęło: terminate() sprawdzał je tuż po sygnale."""

    def terminate(self, dies_after_kill_s):
        clock, sent, killed_at = [0.0], [], {}

        def kill(pid, sig):
            sent.append(sig)
            if sig == signal.SIGKILL:
                killed_at[pid] = clock[0]

        def alive(pid, _start):
            if pid not in killed_at:
                return True  # SIGTERM go nie rusza
            return dies_after_kill_s is None or clock[0] - killed_at[pid] < dies_after_kill_s

        def sleep(seconds):
            clock[0] += seconds

        unit = types.SimpleNamespace(pids=[4242])
        with (
            mock.patch.object(dg, "usage", return_value={"start": 1}),
            mock.patch.object(dg, "alive", side_effect=alive),
            mock.patch.object(dg.os, "kill", side_effect=kill),
            mock.patch.object(dg.time, "time", side_effect=lambda: clock[0]),
            mock.patch.object(dg.time, "sleep", side_effect=sleep),
        ):
            left = dg.terminate(unit, {4242: (1, "node expo/bin/cli start")}, grace=1)
        return left, sent

    def test_killed_process_that_exits_slowly_is_dead(self):
        left, sent = self.terminate(dies_after_kill_s=3)
        self.assertEqual(left, [])
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])

    def test_process_that_outlives_the_wait_is_still_reported(self):
        left, _ = self.terminate(dies_after_kill_s=None)
        self.assertEqual(left, [4242])


BUILT_HOOK = os.path.join(ROOT, "app/.build/release/claude-acc-hook")
# komendy, przy których Python coś robi: stawiają dev serwer...
DEV_STARTS = (
    "pnpm dev",
    "npm run dev:landing",
    "pnpm --filter landing-page dev",
    "(cd apps/web && pnpm dev)",
    'orca terminal create --worktree active --command "cd apps/web && pnpm exec next dev"',
    "bash -c 'pnpm dev'",
    "npx vite --port 5173",
    "vite dev",
    "nohup pnpm exec next dev > /tmp/log 2>&1 &",
    "PORT=3001 npx next dev",
    "turbo run dev",
    "npx expo start",
    "npx webpack serve",
    "npx astro dev",
    "./node_modules/.bin/expo start --dev-client --port 8199",
    "node /w/node_modules/.pnpm/expo@58/node_modules/expo/bin/cli start --port 8183",
    "npx react-native start",
    "npx expo run:ios",
)
# ...albo idą do schedulera (w katalogu z go.mod i package.json)
SCHEDULED = (
    "go test -count=1 ./... 2>&1 | tail -5",
    "/usr/local/go/bin/go build ./...",
    "make test",
    "golangci-lint run",
    "pnpm test 2>&1 | tail -5",
    "npx vitest run",
    "./node_modules/.bin/vitest run",
    "pnpm exec tsc --noEmit",
    "npx vue-tsc --noEmit",
    "yarn lint",
    "bunx eslint .",
    "npx playwright test",
    "npx next build",
    "npx turbo run build",
    "npm test",
    "bun test",
    "npx jest",
    "vite build",
    # każde narzędzie z NODE_TOOLS wołane wprost
    "vitest run",
    "jest",
    "playwright test",
    "next build",
    "tsc --noEmit",
    "vue-tsc --noEmit",
    "eslint .",
    "turbo run build",
    "govulncheck ./...",
    # natywne buildy i start symulatora
    "xcodebuild -scheme X build",
    "cd ios && xcodebuild -workspace A.xcworkspace -scheme A",
    "xcrun xcodebuild -scheme X test",
    "xcrun simctl boot 1234-ABCD",
    "pod install",
    "./gradlew assembleDebug",
    "eas build --platform ios --local",
    "npx expo run:ios --no-bundler",
    "npx expo prebuild",
    "portivo-mobile up mobile",
    "open -a Simulator",
)
# słowa w ścieżkach i innych słowach: Python nic tu nie robi, więc nie ma po co startować
QUIET = (
    "ls -la 2>/dev/null",
    "export FOO=1; echo $FOO",
    "rg -n Foo main_test.go internal/x.go",
    "cat tsconfig.json playwright.config.ts vite.config.ts jest.config.js .eslintrc",
    "echo observe the server",
    "cat ~/dev/notes.md",
    "sed -n 1,20p cmd/server/main.go",
    "git log --oneline | head",
    "xcrun simctl list devices booted",
    "cat ios/Podfile.lock | head",
    "rg -n easing src/",
    "ls ~/Library/Developer/CoreSimulator/Devices",
    "echo pods ready",
    # słowa programów w ścieżkach i nazwach plików: dalej bez Pythona
    "cat scripts/e2e.sh src/node_modules.txt",
    "rg -n cargo_test docs/python-notes.md",
    "ls node_modules .venv",
    "git status --short",
    "gh pr list",
    # interpretery bez skryptu z pliku, docker bez buildu, program systemowy po ścieżce
    "python3 -c 'print(1)'",
    "python3 - <<'EOF'\nprint(1)\nEOF",
    "node --version",
    "node -e 'console.log(1)'",
    "docker ps -a",
    "bash -c 'echo hi'",
    "uv pip list",
    "/usr/bin/git status",
)
# reszta ciężkiej pracy (sched.GENERIC_TOOLS, interpretery ze skryptem, skrypty projektu): w
# projekcie z tymi plikami idą do schedulera
GENERIC = (
    "cargo test",
    "cargo build --release 2>&1 | tail -20",
    "swift test",
    "xcodebuild -scheme App test",
    "docker build .",
    "pytest -x tests",
    "python3 -m pytest",
    "uv run pytest",
    "python3 scripts/capture.py --url http://localhost:3000",
    "node plugins/cli/main.ts capture",
    "bash scripts/e2e.sh",
    "./scripts/e2e.sh",
    "e2e.sh",
    "bin/verify all",
    "pnpm sm capture",
    "npx lighthouse http://localhost:3000",
    "just test",
    "bash -c './scripts/e2e.sh'",
    "docker compose -f c.yml build",
    "python3 -W ignore -m unittest discover -s tests",
    "uv run scripts/capture.py",
    "tsx plugins/cli/main.ts capture",
    "cd scripts && ./e2e.sh",
    'SPECS="leads notifications" ./scripts/e2e.sh',
    "nohup ./scripts/e2e.sh >/dev/null 2>&1 &",
)


class HookGateTest(unittest.TestCase):
    """Bramka natywnego frontu: Python startuje na całe słowa, a żadna komenda, przy której coś
    robi (dev serwer, scheduler), nie odpada po drodze."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="devguard-gate-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.state)
        self.project = os.path.join(self.home, "shop")
        os.makedirs(os.path.join(self.project, ".git"))
        with open(os.path.join(self.project, "go.mod"), "w") as f:
            f.write("module shop\n")
        with open(os.path.join(self.project, "package.json"), "w") as f:
            f.write('{"name": "shop"}')
        self.gate = __import__("re").compile(entry.HOOK_GATE)

    def admit(self, command):
        """Wyjście prawdziwego `devguard.py admit` na tym HOME, jak w sesji."""
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.project}
        done = subprocess.run(
            ["/usr/bin/python3", SCRIPT, "admit"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=dict(os.environ, HOME=self.home, DEVGUARD_ORCA=""),
            timeout=60,
        )
        return done.stdout.strip()

    def test_every_dev_server_start_passes(self):
        for command in DEV_STARTS:
            self.assertTrue(entry.dev_starts(command, "/w/mono"), command)
            self.assertTrue(self.gate.search(command), command)

    def test_every_scheduled_command_passes(self):
        for command in SCHEDULED:
            self.assertTrue(self.admit(command), command)
            self.assertTrue(self.gate.search(command), command)

    def test_words_inside_paths_and_other_words_stay_quiet(self):
        for command in QUIET:
            self.assertEqual(self.admit(command), "", command)
            self.assertIsNone(self.gate.search(command), command)

    def generic_project(self):
        for name, text in (("scripts/e2e.sh", "#!/bin/sh\n"), ("e2e.sh", "#!/bin/sh\n"),
                           ("scripts/capture.py", ""), ("bin/verify", "#!/bin/sh\n"),
                           ("plugins/cli/main.ts", ""), ("Cargo.toml", "[package]\n"),
                           ("package.json", '{"scripts": {"sm": "node plugins/cli/main.ts"}}')):
            path = os.path.join(self.project, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def test_every_generic_heavy_command_passes(self):
        self.generic_project()
        for command in GENERIC:
            self.assertTrue(self.gate.search(command), command)
            self.assertIn("updatedInput", self.admit(command), command)

    def test_agent_git_grep_skips_binary_files(self):
        out = json.loads(self.admit('for n in a b; do git grep -nE "<$n" HEAD -- apps; done'))
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn('git grep -I -nE "<$n" HEAD', command)
        self.assertEqual(self.admit("git grep -a needle"), "")  # agent chce binarek: zostaje

    def test_every_program_the_scheduler_knows_is_in_the_gate(self):
        import sched as scheduler

        self.assertLessEqual(set(scheduler.NODE_TOOLS), set(entry.GATE_PROGRAMS))
        self.assertLessEqual(set(scheduler.NATIVE_PROGRAMS), set(entry.GATE_PROGRAMS))
        # swift ma w bramce własny kształt (swift build|test|run), reszta stoi jako całe słowo
        self.assertLessEqual(set(scheduler.GENERIC_TOOLS) - {"swift"}, set(entry.GATE_PROGRAMS))

    @unittest.skipUnless(os.access(BUILT_HOOK, os.X_OK), "brak app/.build/release/claude-acc-hook")
    def test_native_front_reads_the_gate_like_python(self):
        """Ten sam wzorzec czyta ICU w Swifcie: Python udaje skrypt, który mówi, że wystartował."""
        words = os.path.join(self.state, "hook-words.json")
        with open(words, "w") as f:
            f.write(subprocess.run(["/usr/bin/python3", SCRIPT, "words"], capture_output=True,
                                   text=True, check=True).stdout)
        python = os.path.join(self.state, "python")
        with open(python, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\necho python\n")
        os.chmod(python, 0o755)

        def front(command):
            event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.project}
            return subprocess.run([BUILT_HOOK], input=json.dumps(event), capture_output=True,
                                  text=True, env={"HOME": self.home}, timeout=30).stdout.strip()

        for command in DEV_STARTS + SCHEDULED + GENERIC:
            self.assertEqual(front(command), "python", command)
        for command in QUIET:
            self.assertEqual(front(command), "", command)
        # plik słów sprzed bramki: dawne podciągi, więc /dev/null znowu budzi Pythona
        with open(words, "w") as f:
            json.dump({"dev": list(entry.DEV_WORDS), "sched": list(entry.SCHED_WORDS)}, f)
        self.assertEqual(front("ls -la 2>/dev/null"), "python")


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
            # ostatnia linia i symulatory patrzą na całą tabelę procesów, nie na `scope`: w teście
            # nie mogą zatrzymać niczego prawdziwego na tym Macu
            "last_resort": False,
            "simulator_pool_prefix": "devguard-test-pool-",
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
        env = dict(os.environ, HOME=self.home, DEVGUARD_ORCA="",
                   CLAUDE_ACC_FSGUARD_STATE=os.path.join(self.home, "fsguard.json"))
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

    def room(self, app):
        done = subprocess.run(
            ["/usr/bin/python3", SCRIPT, "room", app],
            capture_output=True,
            text=True,
            env=dict(os.environ, HOME=self.home, DEVGUARD_ORCA=""),
            timeout=60,
        )
        return done.returncode, done.stdout.strip()

    def metro_app(self, name="mobile"):
        path = os.path.join(self.home, name)
        os.makedirs(os.path.join(path, "node_modules/.bin"), exist_ok=True)
        return path

    METRO = "./node_modules/.bin/expo start --dev-client --port 8199"

    def test_admit_refuses_metro_under_critical_pressure_with_no_server_left(self):
        """2026-10-08 18:52: strażnik zatrzymał ostatni dev serwer, więc hook liczył presję tylko
        „gdy coś działa” i wpuścił nowe Metro przy krytycznej presji. Teraz odmowa z dokładną
        komendą czekania, a ta sama komenda puszcza, gdy pamięć odpuści."""
        app = self.metro_app()
        self.config(available_critical_percent=101)  # każdy odczyt to presja krytyczna
        verdict = self.admit(self.METRO, app)
        self.assertEqual(verdict["permissionDecision"], "deny")
        reason = verdict["permissionDecisionReason"]
        self.assertIn("krytycznym", reason)
        self.assertIn(f"`claude-acc sched wait -- 'claude-acc guard room {app}'`", reason)
        self.assertEqual(self.room(app)[0], 1)
        self.config()
        self.assertIsNone(self.admit(self.METRO, app))
        self.assertEqual(self.room(app), (0, "jest miejsce"))

    def test_server_stopped_for_memory_stays_stopped(self):
        """Po akcji strażnik mierzy przyrost swapu od nowa, więc tuż po zatrzymaniu presja wygląda
        na mniejszą. Serwer zatrzymany z braku pamięci nie wraca, póki Mac nie odetchnął, przez
        `restart_hold_minutes`; inna aplikacja i spokojny Mac przechodzą."""
        app = self.metro_app()
        stopped = {
            "at": time.time() - 120,
            "action": "stop",
            "code": "pressure",
            "cwd": app,
            "apps": [app],
            "reason": "brak pamięci: swap 11,2 GB i rośnie",
            "ok": False,
        }
        with open(os.path.join(self.state_dir, "devguard-state.json"), "w") as f:
            json.dump({"events": [stopped]}, f)
        self.config(available_warn_percent=101)  # ostrzeżenie: jeszcze nie odetchnął
        verdict = self.admit(self.METRO, app)
        self.assertEqual(verdict["permissionDecision"], "deny")
        self.assertIn("zatrzymał serwer", verdict["permissionDecisionReason"])
        self.assertIn("swap 11,2 GB i rośnie", verdict["permissionDecisionReason"])
        self.assertIn("claude-acc guard room", verdict["permissionDecisionReason"])
        self.assertEqual(self.room(app)[0], 1)
        self.assertIsNone(self.admit(self.METRO, self.metro_app("other")))
        self.config(available_warn_percent=101, restart_hold_minutes=1)  # zatrzymany 2 min temu
        self.assertIsNone(self.admit(self.METRO, app))
        self.config()  # presja zero, swap pod progiem
        self.assertIsNone(self.admit(self.METRO, app))

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
            words,
            {
                "dev": list(entry.DEV_WORDS),
                "sched": list(entry.SCHED_WORDS),
                "gate": [entry.HOOK_GATE],
            },
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

    def admit_in_clean_python(self, command, cwd="/w/mono", env=None):
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
            env=env,
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
            path = os.environ.get("PATH", "")
            no_rtk = os.pathsep.join(
                d for d in path.split(os.pathsep) if not os.path.exists(os.path.join(d, "rtk"))
            )
            out, loaded = self.admit_in_clean_python(
                "go test ./...", cwd=root, env=dict(os.environ, PATH=no_rtk)
            )
            self.assertIn("updatedInput", json.loads(out)["hookSpecificOutput"])
            self.assertEqual(loaded, ["json", "re"])
            if shutil.which("rtk"):
                # scheduler pyta rtk o przepisanie, więc dochodzi tylko subprocess
                _, loaded = self.admit_in_clean_python("go test ./...", cwd=root)
                self.assertEqual(loaded, ["json", "re", "subprocess"])

    def test_native_build_loads_only_the_scheduler(self):
        """Natywny build idzie szybką ścieżką do schedulera, bez strażnika i bez ctypes."""
        with tempfile.TemporaryDirectory() as root:
            path = os.environ.get("PATH", "")
            no_rtk = os.pathsep.join(
                d for d in path.split(os.pathsep) if not os.path.exists(os.path.join(d, "rtk"))
            )
            out, loaded = self.admit_in_clean_python(
                "xcodebuild -scheme App build", cwd=root, env=dict(os.environ, PATH=no_rtk)
            )
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


GB = 1024**3
NOW_T = 1_800_000_000.0


class TerminateTest(unittest.TestCase):
    """Zatrzymanie drzewa: SIGCONT, SIGTERM, łaska, SIGKILL i czekanie na koniec."""

    def test_stopped_process_gets_sigterm_not_sigkill(self):
        # job wstrzymany przez scheduler (SIGSTOP przy rosnącym swapie) ma dostać szansę na
        # porządne wyjście; bez SIGCONT stoi do końca łaski i ginie od SIGKILL
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, sys, time\n"
             "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
             "print('ready', flush=True)\n"
             "time.sleep(60)\n"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        child.stdout.readline()
        os.kill(child.pid, signal.SIGSTOP)
        unit = types.SimpleNamespace(pids=[child.pid])
        began = time.time()
        left = dg.terminate(unit, {child.pid: (os.getpid(), "python3 job")}, grace=3)
        self.assertEqual(left, [])
        self.assertLess(time.time() - began, 3)
        self.assertEqual(child.wait(timeout=5), 0)  # wyszedł sam, nie od SIGKILL (-9)


class BrakeTickTest(unittest.TestCase):
    """tick() i hamulec: kiedy gasi drzewo mimo akcji strażnika, kiedy czeka, kiedy nigdy."""

    def run_tick(self, stage, plans=(), brake_at=0, last_action=0, inventory=None, **extra):
        import lastresort as lr

        cfg = dict(dg.DEFAULT_CONFIG, **extra)
        pressure = types.SimpleNamespace(level=min(stage, 2), stage=stage, stage_reasons=[], ram=48 * GB,
                                         swap_used=0, compressed=0, summary=lambda: {"stage": stage})
        world = types.SimpleNamespace(now=NOW_T, units=[], table={}, pressure=pressure, orca=None)
        state = {"brake_at": brake_at, "last_action": last_action, "inventory": inventory or {},
                 "swap_history": [[NOW_T, 1, 1]]}
        reap = mock.Mock(return_value="ostatnia linia: test")
        with mock.patch.object(dg, "World", return_value=world), mock.patch.object(dg, "check_pending"), \
                mock.patch.object(dg, "decide", return_value=list(plans)), mock.patch.object(dg, "execute"), \
                mock.patch.object(lr, "reap", reap):
            dg.tick(cfg, state, orca=None, now=NOW_T)
        return reap, state

    def test_emergency_acts_even_after_a_guard_action(self):
        plan = types.SimpleNamespace(action="stop", summary=lambda: {})
        reap, state = self.run_tick(3, plans=[plan], cooldown_seconds=0)
        reap.assert_called_once()
        self.assertEqual(reap.call_args.kwargs["level"], 3)
        self.assertEqual(state["brake_at"], NOW_T)

    def test_emergency_has_its_own_short_cooldown(self):
        self.run_tick(3, brake_at=NOW_T - 6, last_action=NOW_T - 6)[0].assert_called_once()
        self.run_tick(3, brake_at=NOW_T - 3)[0].assert_not_called()

    def test_tight_never_kills(self):
        self.run_tick(1)[0].assert_not_called()

    def test_runaway_dies_on_a_calm_mac(self):
        inv = {"biggest": [4242, 25 * GB, "node t.js"]}
        reap, _ = self.run_tick(0, inventory=inv)
        reap.assert_called_once()
        self.run_tick(0, inventory={"biggest": [4242, 10 * GB, "node t.js"]})[0].assert_not_called()


class PressureStageTest(unittest.TestCase):
    def test_real_mac_reports_stage_and_compressor_segments(self):
        p = dg.Pressure(dict(dg.DEFAULT_CONFIG), {}, time.time())
        summary = p.summary()
        self.assertIn(summary["stage"], (0, 1, 2, 3))
        self.assertGreater(summary["segments_limit"] or 0, 0)  # macOS 26: vm.compressor.segment.limit
        self.assertGreater(summary["segments"] or 0, 0)


class InventoryTest(unittest.TestCase):
    def test_long_lived_families_and_biggest(self):
        table = {
            10: (1, "claude --resume"),
            11: (1, "/Users/u/Library/Caches/ms-playwright/chromium-1/chrome --headless"),
            12: (1, "/Library/Developer/CoreSimulator/Volumes/x/launchd_sim"),
            13: (1, "/opt/homebrew/bin/gopls"),
            14: (1, "node /w/node_modules/.bin/../expo/bin/cli start --port 8081"),
            15: (1, "node /w/node_modules/typescript/bin/tsc --watch"),
            16: (1, "/Applications/Spotify.app/Contents/MacOS/Spotify"),
            17: (1, "node /w/node_modules/.bin/next dev"),
        }
        sizes = {10: 1, 11: 2, 12: 3, 13: 1, 14: 2, 15: 1, 16: 5, 17: 4}
        unit = types.SimpleNamespace(pids=[17])
        with mock.patch.object(dg, "usage", lambda pid: {"footprint": sizes[pid] * GB, "start": 1}), \
                mock.patch.object(dg.janitor, "load_json", lambda path, default: {}):
            inv = dg.inventory(table, [unit], NOW_T)
        fams = inv["families"]
        self.assertEqual({n: f["footprint"] // GB for n, f in fams.items()},
                         {"agents": 1, "headless": 2, "simulators": 3, "lsp": 1, "metro": 2, "watchers": 1,
                          "rest": 5, "dev": 4})
        self.assertEqual(inv["long_lived"], 13 * GB)  # bez agentów i reszty
        self.assertEqual(inv["biggest"][0], 16)
        self.assertIn("symulatory 3,0 GB", dg.inventory_line(inv).replace(".", ","))


if __name__ == "__main__":
    unittest.main()
