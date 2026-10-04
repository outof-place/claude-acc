"""Testy perf.py: księgowanie poprawek (apply, keep, undo) i odczyty pomiarów.

Księgowanie sprawdzamy na atrapie systemu (procesy, priorytety), stan i log idą do
katalogu tymczasowego. Jeden test robi to naprawdę: stawia własny proces-wydmuszkę,
skrypt w osobnym $HOME daje go do tła, a ps ma pokazać priorytet 4 i powrót po undo.
Prawdziwe procesy na tym Macu są dla testów niewidoczne: lista `background` w ich
konfiguracji pasuje tylko do wydmuszki.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "perf.py")
sys.path.insert(0, ROOT)

import perf


class FakeSystem:
    """Procesy jako {pid: [start, linia poleceń, w tle?]}; zapisuje każdą zmianę priorytetu."""

    def __init__(self, procs):
        self.procs = {pid: list(v) for pid, v in procs.items()}
        self.calls = []

    def processes(self):
        return [(pid, p[0], p[1]) for pid, p in sorted(self.procs.items())]

    def start(self, pid):
        return self.procs[pid][0] if pid in self.procs else None

    def background(self, pid):
        return pid in self.procs and self.procs[pid][2]

    def set_background(self, pid, on):
        if pid not in self.procs:
            return False
        self.procs[pid][2] = on
        self.calls.append((pid, on))
        return True


WORKER = "node /x/lib/node_modules/cavemem/dist/index.js worker run"


class Isolated(unittest.TestCase):
    """Stan, konfiguracja i log w katalogu tymczasowym."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="perf-test-")
        patches = {
            "STATE_PATH": os.path.join(self.dir, "perf-state.json"),
            "CONFIG_PATH": os.path.join(self.dir, "perf.json"),
            "LOG_PATH": os.path.join(self.dir, "perf.log"),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(perf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = dict(perf.DEFAULT_CONFIG)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cmd(self, func, *args, system=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(self.cfg, list(args), system=system)
        return code, out.getvalue()


class BackgroundBookkeepingTest(Isolated):
    def test_apply_records_only_what_it_changed(self):
        system = FakeSystem(
            {
                10: [100, WORKER, False],
                11: [110, "node /y/cavemem/dist/index.js worker run", True],
                12: [120, "/usr/bin/vim notes.txt", False],
            }
        )
        code, out = self.run_cmd(perf.cmd_apply, "bg-helpers", system=system)
        self.assertEqual(code, 0)
        self.assertEqual(system.calls, [(10, True)])
        record = perf.load_state()["applied"]["bg-helpers"]
        # 11 był w tle już wcześniej: undo nie może go z niego wyciągnąć
        self.assertEqual([e["pid"] for e in record["procs"]], [10])
        self.assertIn("cavemem", out)

        self.run_cmd(perf.cmd_undo, "bg-helpers", system=system)
        self.assertEqual(system.calls[-1], (10, False))
        self.assertTrue(system.procs[11][2])
        self.assertNotIn("bg-helpers", perf.load_state()["applied"])

    def test_undo_skips_reused_pid(self):
        system = FakeSystem({10: [100, WORKER, False]})
        self.run_cmd(perf.cmd_apply, "bg-helpers", system=system)
        # proces padł, a jego pid dostał ktoś inny z innym czasem startu
        system.procs[10] = [999, "/bin/zsh", True]
        system.calls.clear()
        self.run_cmd(perf.cmd_undo, "--all", system=system)
        self.assertEqual(system.calls, [])

    def test_keep_follows_restarted_helper(self):
        system = FakeSystem({10: [100, WORKER, False]})
        self.run_cmd(perf.cmd_apply, "bg-helpers", system=system)
        first_at = perf.load_state()["applied"]["bg-helpers"]["at"]
        del system.procs[10]
        system.procs[20] = [200, WORKER, False]
        self.run_cmd(perf.cmd_keep, system=system)
        record = perf.load_state()["applied"]["bg-helpers"]
        self.assertEqual([e["pid"] for e in record["procs"]], [20])
        self.assertTrue(system.procs[20][2])
        self.assertEqual(record["at"], first_at)

    def test_keep_restores_background_lost_by_tracked_process(self):
        system = FakeSystem({10: [100, WORKER, False]})
        self.run_cmd(perf.cmd_apply, "bg-helpers", system=system)
        system.procs[10][2] = False  # np. aplikacja sama podniosła sobie priorytet
        self.run_cmd(perf.cmd_keep, system=system)
        self.assertTrue(system.procs[10][2])

    def test_keep_ignores_tweaks_that_are_off(self):
        system = FakeSystem({10: [100, WORKER, False]})
        self.run_cmd(perf.cmd_keep, system=system)
        self.assertEqual(system.calls, [])
        self.assertEqual(perf.load_state()["applied"], {})

    def test_dry_run_changes_nothing(self):
        system = FakeSystem({10: [100, WORKER, False]})
        code, out = self.run_cmd(perf.cmd_apply, "--all", "--dry-run", system=system)
        self.assertEqual(code, 0)
        self.assertEqual(system.calls, [])
        self.assertFalse(os.path.exists(perf.STATE_PATH))
        self.assertIn("cavemem (10)", out)

    def test_root_tweak_is_only_described(self):
        system = FakeSystem({})
        code, out = self.run_cmd(perf.cmd_apply, "shaper", system=system)
        self.assertEqual(code, 2)
        self.assertIn("perf-root.sh", out)
        self.assertEqual(perf.load_state()["applied"], {})

    def test_unknown_tweak(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(perf.cmd_apply, "turbo", system=FakeSystem({}))

    def test_status_json_shows_applied_tweak(self):
        system = FakeSystem({10: [100, WORKER, False]})
        self.run_cmd(perf.cmd_apply, "bg-helpers", system=system)
        _, out = self.run_cmd(perf.cmd_status, "--json", system=system)
        data = json.loads(out)
        entry = next(t for t in data["tweaks"] if t["name"] == "bg-helpers")
        self.assertTrue(entry["applied"])
        self.assertIn("cavemem (10)", entry["detail"])


class RootRecordTest(Isolated):
    def test_record_and_forget(self):
        self.assertEqual(perf.cmd_record(self.cfg, ["shaper", "en0", "27Mbps"]), 0)
        self.assertEqual(perf.load_state()["applied"]["shaper"]["detail"], "en0 27Mbps")
        perf.cmd_record(self.cfg, ["shaper", "--forget"])
        self.assertNotIn("shaper", perf.load_state()["applied"])

    def test_record_refuses_non_root_tweak(self):
        self.assertEqual(perf.cmd_record(self.cfg, ["bg-helpers", "x"]), 2)


class BenchStateTest(Isolated):
    def network(self, gateway, up):
        return {"where": {"interface": "en0", "gateway": gateway}, "up_mbps": up}

    def test_shaper_rate_uses_latest_bench_of_this_network(self):
        state = perf.load_state()
        perf.record_bench(state, "network", self.network("10.0.0.1", 31.0), 1)
        perf.record_bench(state, "network", self.network("192.168.0.1", 300), 2)
        perf.record_bench(state, "network", self.network("10.0.0.1", 30.0), 3)
        # pomiar z włączonym ogranicznikiem nie mówi, ile ma łącze
        capped = dict(self.network("10.0.0.1", 24.0), shaper="27.00 Mbps")
        perf.record_bench(state, "network", capped, 4)
        where = {"interface": "en0", "gateway": "10.0.0.1"}
        self.assertEqual(perf.shaper_rate(self.cfg, state, where), 27)
        where["gateway"] = "172.20.10.1"
        self.assertIsNone(perf.shaper_rate(self.cfg, state, where))

    def test_history_is_trimmed(self):
        state = perf.load_state()
        for i in range(perf.HISTORY + 5):
            perf.record_bench(state, "cpu", {"single_mbs": i}, i, {"load1": i})
        history = state["history"]["cpu"]
        self.assertEqual(len(history), perf.HISTORY)
        self.assertEqual(history[-1]["result"]["single_mbs"], perf.HISTORY + 4)
        self.assertEqual(state["bench"]["cpu"]["at"], perf.HISTORY + 4)
        self.assertEqual(state["bench"]["cpu"]["load"], {"load1": perf.HISTORY + 4})


class ParseTest(unittest.TestCase):
    def test_network_quality_report(self):
        report = {
            "dl_throughput": 441_000_000,
            "ul_throughput": 136_000_000,
            "dl_responsiveness": 600,
            "ul_responsiveness": 150,
            "base_rtt": 49.5,
            "il_h2_req_resp": [42, 41, 43],
            "lud_foreign_dl_h2_req_resp": [40] * 9 + [80],
            "lud_foreign_ul_h2_req_resp": [44] * 10,
            "lud_self_ul_h2_req_resp": [250, 300, 350],
            "interface_name": "en12",
            "test_endpoint": "edge",
            "other": {
                "ecn_values": {"ecn_disabled": 5},
                "l4s_enablement": {"disabled": 5},
            },
        }
        with mock.patch.object(perf.janitor, "run", return_value=json.dumps(report)):
            q = perf.network_quality()
        self.assertEqual(q["down_mbps"], 441.0)
        self.assertEqual(q["up_mbps"], 136.0)
        self.assertEqual(q["down_loaded_ms"], 100.0)
        self.assertEqual(q["up_loaded_ms"], 400.0)
        self.assertEqual(q["idle_ms"], 42)
        self.assertEqual(q["down_net_p90_ms"], 44.0)
        self.assertEqual(q["up_net_p90_ms"], 44.0)
        self.assertEqual(q["up_self_ms"], 300)
        self.assertEqual(q["ecn"], ["ecn_disabled"])

    def test_network_quality_failure(self):
        with mock.patch.object(perf.janitor, "run", return_value=None):
            self.assertIsNone(perf.network_quality())

    def test_ping_summary(self):
        out = (
            "--- 1.1.1.1 ping statistics ---\n"
            "40 packets transmitted, 39 packets received, 2.5% packet loss\n"
            "round-trip min/avg/max/stddev = 9.1/10.4/31.0/3.2 ms\n"
        )
        with mock.patch.object(perf.janitor, "run", return_value=out):
            p = perf.ping("1.1.1.1")
        self.assertEqual(
            p, {"loss_pct": 2.5, "avg_ms": 10.4, "max_ms": 31.0, "jitter_ms": 3.2}
        )

    def test_interface_tbr(self):
        shaped = (
            "\tscheduler: FQ_CODEL (driver managed)\n"
            "\tuplink rate: 25.10 Mbps [eff] / 27.00 Mbps [tbr] / 1.00 Gbps [max]\n"
        )
        with mock.patch.object(perf.janitor, "run", return_value=shaped):
            self.assertEqual(perf.interface_tbr("en0"), "27.00 Mbps")
        plain = "\tuplink rate: 561.97 Mbps [eff] / 748.80 Mbps\n"
        with mock.patch.object(perf.janitor, "run", return_value=plain):
            self.assertIsNone(perf.interface_tbr("en0"))

    def test_rusage_of_self(self):
        info = perf.rusage(os.getpid())
        self.assertGreater(info["cpu"], 0)
        self.assertLessEqual(info["pcpu"], info["cpu"] + 1e-6)
        self.assertIsNone(perf.rusage(1))  # launchd należy do roota

    def test_short_command(self):
        self.assertEqual(perf.short_command(WORKER), "cavemem")
        self.assertEqual(perf.short_command("/usr/bin/vim a.txt"), "vim")


class RealProcessTest(unittest.TestCase):
    """Skrypt w osobnym $HOME daje do tła prawdziwy proces-wydmuszkę i go z tła wyciąga."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="perf-home-"))
        self.marker = f"perf-test-dummy-{uuid.uuid4().hex}"
        state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(state_dir)
        with open(os.path.join(state_dir, "perf.json"), "w") as f:
            json.dump({"background": [self.marker]}, f)
        self.state_path = os.path.join(state_dir, "perf-state.json")
        self.dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", self.marker]
        )

    def tearDown(self):
        self.dummy.kill()
        self.dummy.wait()
        shutil.rmtree(self.home, ignore_errors=True)

    def perf(self, *args):
        done = subprocess.run(
            ["/usr/bin/python3", SCRIPT, *args],
            capture_output=True,
            text=True,
            env=dict(os.environ, HOME=self.home),
            timeout=60,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def priority(self):
        out = subprocess.run(
            ["ps", "-o", "pri=", "-p", str(self.dummy.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return int(out.strip())

    def test_apply_and_undo(self):
        time.sleep(0.2)
        normal = self.priority()
        self.assertNotEqual(normal, perf.BACKGROUND_PRI)
        self.perf("apply", "bg-helpers")
        self.assertEqual(self.priority(), perf.BACKGROUND_PRI)
        with open(self.state_path) as f:
            procs = json.load(f)["applied"]["bg-helpers"]["procs"]
        self.assertEqual([p["pid"] for p in procs], [self.dummy.pid])
        status = json.loads(self.perf("status", "--json"))
        entry = next(t for t in status["tweaks"] if t["name"] == "bg-helpers")
        self.assertTrue(entry["applied"])
        self.perf("undo", "bg-helpers")
        self.assertEqual(self.priority(), normal)
        with open(self.state_path) as f:
            self.assertNotIn("bg-helpers", json.load(f)["applied"])


if __name__ == "__main__":
    unittest.main()
