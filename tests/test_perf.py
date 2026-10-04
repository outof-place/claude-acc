"""Testy perf.py: księgowanie poprawek (apply, keep, undo, Ultra) i odczyty pomiarów.

Księgowanie sprawdzamy na atrapie systemu (procesy, priorytety, Docker), a stan, log
i wszystkie cudze pliki, które poprawki zmieniają (~/.claude/settings.json,
devguard.json, ustawienia Dockera), są kopiami w katalogu tymczasowym. Jeden test robi to naprawdę: stawia własny proces-wydmuszkę,
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

    def __init__(self, procs, docker=False):
        self.procs = {pid: list(v) for pid, v in procs.items()}
        self.calls = []
        self.docker = docker

    def docker_running(self):
        return self.docker

    def git(self, repo, *args):
        return None

    def docker_memory(self):
        return 8318709760 if self.docker else None

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
        patches["CLAUDE_PROJECTS"] = os.path.join(self.dir, "projects")
        for name, value in patches.items():
            patcher = mock.patch.object(perf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # żaden test nie może dotknąć prawdziwych plików Claude, strażnika ani Dockera
        self.claude = os.path.join(self.dir, "claude-settings.json")
        self.files = {}
        fake = {
            perf.DEVGUARD_CONFIG: os.path.join(self.dir, "devguard.json"),
            perf.DOCKER_SETTINGS: os.path.join(self.dir, "docker-settings.json"),
            perf.CLAUDE_SETTINGS: self.claude,
        }
        for item in perf.TWEAKS:
            # każda poprawka, która pisze do settings.json Claude, dostaje kopię
            if hasattr(item, "settings_path"):
                patcher = mock.patch.object(item, "path", self.claude)
            elif isinstance(item, perf.JsonSetting):
                self.files[item.name] = fake[item.path]
                patcher = mock.patch.object(item, "path", self.files[item.name])
            else:
                continue
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hooks_dir = os.path.join(self.dir, "hooks")
        patcher = mock.patch.object(perf, "HOOKS_DIR", self.hooks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # siatka bezpieczeństwa: prawdziwe ustawienia Claude nie mogą się zmienić w teście
        self.real_settings = self.stamp(os.path.expanduser("~/.claude/settings.json"))
        patcher = mock.patch.object(
            perf, "ORCA_DATA", os.path.join(self.dir, "orca-data.json")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # prawdziwa Orca w /Applications dokładałaby krok "devtools" do każdego testu
        self.orca_app = os.path.join(self.dir, "Orca.app")
        patcher = mock.patch.object(perf, "ORCA_APP", self.orca_app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.orca_started = None
        patcher = mock.patch.object(
            perf, "orca_started", side_effect=lambda: self.orca_started
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = dict(perf.DEFAULT_CONFIG)

    def write(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def read(self, path):
        with open(path) as f:
            return json.load(f)

    def text(self, path):
        with open(path) as f:
            return f.read()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.assertEqual(
            self.stamp(os.path.expanduser("~/.claude/settings.json")),
            self.real_settings,
            "test zmienił prawdziwy ~/.claude/settings.json",
        )

    @staticmethod
    def stamp(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

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
        self.assertIn("claude-acc perf-root", out)
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

    def test_record_result_shows_next_to_ultra(self):
        self.assertEqual(
            perf.cmd_record(
                self.cfg,
                ["vnodes", "786432", "prev=263168", "--result", "5.68", "3.42"],
            ),
            0,
        )
        state = perf.load_state()
        self.assertEqual(state["applied"]["vnodes"]["detail"], "786432 prev=263168")
        ultra = state["ultra"]
        self.assertEqual(ultra["root_applied"], ["vnodes"])
        self.assertEqual(ultra["results"]["vnodes"]["after"], 3.42)
        self.assertNotIn("vnodes", ultra["pending_root"])
        # re-recording without numbers keeps the measured ones; forgetting drops them
        perf.cmd_record(self.cfg, ["vnodes", "786432", "prev=263168"])
        self.assertEqual(
            perf.load_state()["applied"]["vnodes"]["result"]["before"], 5.68
        )
        perf.cmd_record(self.cfg, ["vnodes", "--forget"])
        ultra = perf.load_state()["ultra"]
        self.assertEqual(ultra["root_applied"], [])
        self.assertNotIn("vnodes", ultra["results"])

    def test_record_refuses_non_root_tweak(self):
        self.assertEqual(perf.cmd_record(self.cfg, ["bg-helpers", "x"]), 2)

    def test_devtools_is_manual_until_recorded(self):
        self.assertNotIn("devtools", perf.pending_manual(perf.load_state()))
        os.makedirs(self.orca_app)
        self.assertIn("devtools", perf.pending_manual(perf.load_state()))
        perf.cmd_record(
            self.cfg,
            ["devtools", "com.stablyai.orca", "prev=none", "--result", "196.2", "4.1"],
        )
        state = perf.load_state()
        self.assertNotIn("devtools", perf.pending_manual(state))
        self.assertNotIn("devtools", state["ultra"]["pending_root"])
        self.assertEqual(state["ultra"]["root_applied"], ["devtools"])
        self.assertEqual(
            state["ultra"]["results"]["devtools"],
            {
                "before": 196.2,
                "after": 4.1,
                "unit": "ms pierwszego uruchomienia nowej binarki",
            },
        )
        perf.cmd_record(self.cfg, ["devtools", "--forget"])
        self.assertIn("devtools", perf.pending_manual(perf.load_state()))

    def test_rerecord_keeps_the_moment_of_change(self):
        perf.cmd_record(self.cfg, ["devtools", "com.stablyai.orca", "prev=none"])
        at = perf.load_state()["applied"]["devtools"]["at"]
        with mock.patch.object(perf.time, "time", return_value=at + 100):
            perf.cmd_record(
                self.cfg,
                ["devtools", "com.stablyai.orca", "prev=none", "--result", "196.2", "4.1"],
            )
            self.assertEqual(perf.load_state()["applied"]["devtools"]["at"], at)
            perf.cmd_record(self.cfg, ["devtools", "com.other", "prev=none"])
        self.assertEqual(perf.load_state()["applied"]["devtools"]["at"], at + 100)

    def test_gatekeeper_bench_records_after_from_restarted_orca(self):
        perf.cmd_record(self.cfg, ["devtools", "com.stablyai.orca", "prev=none"])
        at = perf.load_state()["applied"]["devtools"]["at"]
        probes = [
            # z Terminala: nie mówi nic o Orce
            ({"responsible": "Terminal", "first_ms": 4.3, "second_ms": 3.3}, at + 60),
            # Orca sprzed zmiany: to jeszcze "przed"
            ({"responsible": "Orca", "first_ms": 196.5, "second_ms": 3.6}, at - 60),
            # Orca po restarcie
            ({"responsible": "Orca", "first_ms": 4.1, "second_ms": 3.4}, at + 60),
            # kolejny pomiar nie nadpisuje pierwszego
            ({"responsible": "Orca", "first_ms": 9.9, "second_ms": 3.4}, at + 60),
        ]
        for probe, started in probes:
            self.orca_started = started
            with mock.patch.object(
                perf, "bench_gatekeeper", return_value=dict(probe, penalty_ms=1.0)
            ):
                self.run_cmd(perf.cmd_bench, "gatekeeper")
            if probe["first_ms"] == 196.5:
                self.assertNotIn("result", perf.load_state()["applied"]["devtools"])
        state = perf.load_state()
        self.assertEqual(
            state["applied"]["devtools"]["result"], {"before": 196.2, "after": 4.1}
        )
        self.assertEqual(state["ultra"]["results"]["devtools"]["after"], 4.1)
        self.assertEqual(state["bench"]["gatekeeper"]["result"]["first_ms"], 9.9)

    def test_devtools_wants_orca_started_after_it(self):
        os.makedirs(self.orca_app)
        perf.cmd_record(self.cfg, ["devtools", "com.stablyai.orca", "prev=none"])
        state = perf.load_state()
        at = state["applied"]["devtools"]["at"]
        self.orca_started = at - 600
        self.assertEqual(perf.pending_manual(state), ["devtools-restart"])
        self.orca_started = at + 5
        self.assertEqual(perf.pending_manual(state), [])
        self.orca_started = None  # Orca nie działa
        self.assertEqual(perf.pending_manual(state), [])


class OrcaStartedTest(unittest.TestCase):
    def test_parses_etime(self):
        main = "/Applications/Orca.app/Contents/MacOS/Orca"
        procs = {7: "/usr/bin/other", 9: main + " --flag"}
        for etime, seconds in (("05:10", 310), ("01:00:00", 3600), ("2-00:00:01", 172801)):
            with mock.patch.object(perf, "own_processes", return_value=procs), mock.patch.object(
                perf.janitor, "run", return_value=f"   {etime}\n"
            ) as run:
                started = perf.orca_started()
            self.assertEqual(run.call_args[0][0][-1], "9")
            self.assertAlmostEqual(time.time() - started, seconds, delta=2)
        with mock.patch.object(perf, "own_processes", return_value={7: "/usr/bin/other"}):
            self.assertIsNone(perf.orca_started())


class GatekeeperTest(unittest.TestCase):
    def test_penalty_is_first_minus_second_exec(self):
        probe = {"first_ms": 196.2, "second_ms": 3.7}
        with mock.patch.object(perf, "first_exec", return_value=probe), mock.patch.object(
            perf, "responsible_app", return_value="Orca"
        ):
            result = perf.bench_gatekeeper()
        self.assertEqual(result["penalty_ms"], 192.5)
        lines = perf.describe_gatekeeper(result)
        self.assertIn("Orca", lines[0])
        self.assertIn("ocena Gatekeepera 192 ms", lines[1])

    def test_without_go(self):
        with mock.patch.object(perf, "first_exec", return_value=None):
            result = perf.bench_gatekeeper()
        self.assertNotIn("penalty_ms", result)
        self.assertIn("brak pomiaru", perf.describe_gatekeeper(result)[0])

    def test_responsible_app_of_this_process(self):
        # testy chodzą z Terminala, Orki albo launchd; zawsze jest jakaś odpowiedzialna aplikacja
        self.assertTrue(perf.responsible_app())


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


ORCA = 'if [ -z "${HOME-}" ]; then printf "{}"; fi  # ORCA_AGENT_HOOK_PORT'
CAVE = "/x/node /x/lib/node_modules/cavemem/dist/index.js hook run"


FORMAT = "/Users/x/.claude/hooks/auto-format.sh"
TYPECHECK = "/Users/x/.claude/hooks/ts-typecheck.sh"


def claude_settings():
    """Wycinek prawdziwego ~/.claude/settings.json: hooki cavemem obok hooka Orki."""
    return {
        "cleanupPeriodDays": 90,
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {"type": "command", "command": FORMAT},
                        {"type": "command", "command": TYPECHECK, "timeout": 30},
                        {"type": "command", "command": "fmt.sh | tee log"},
                    ],
                },
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{CAVE} post-tool-use --ide claude-code",
                            "timeout": 10,
                        },
                        {"type": "command", "command": ORCA, "timeout": 10},
                    ],
                },
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{CAVE} stop --ide claude-code",
                            "timeout": 10,
                        }
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": f"{CAVE} user-prompt-submit"}
                    ]
                }
            ],
        },
    }


class AsyncHooksTest(Isolated):
    def test_apply_and_exact_undo(self):
        original = claude_settings()
        self.write(self.claude, original)
        item = perf.tweak("claude-hooks-async")
        record, changed = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(len(changed), 2)
        data = self.read(self.claude)
        post = data["hooks"]["PostToolUse"][1]["hooks"]
        self.assertIs(post[0]["async"], True)
        self.assertNotIn("async", post[1])  # hook Orki zostaje synchroniczny
        self.assertIs(data["hooks"]["Stop"][0]["hooks"][0]["async"], True)
        self.assertNotIn("async", data["hooks"]["UserPromptSubmit"][0]["hooks"][0])
        # drugi raz nic nie zmienia
        record2, changed2 = item.apply(self.cfg, FakeSystem({}), record)
        self.assertEqual(changed2, [])
        self.assertEqual(record2, record)
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.read(self.claude), original)

    def test_undo_keeps_later_user_change_and_restores_false(self):
        original = claude_settings()
        original["hooks"]["Stop"][0]["hooks"][0]["async"] = False
        self.write(self.claude, original)
        item = perf.tweak("claude-hooks-async")
        record, _ = item.apply(self.cfg, FakeSystem({}))
        data = self.read(self.claude)
        # użytkownik sam zmienia hook PostToolUse po nas
        data["hooks"]["PostToolUse"][1]["hooks"][0]["async"] = "custom"
        self.write(self.claude, data)
        item.undo(record, FakeSystem({}))
        after = self.read(self.claude)
        self.assertEqual(
            after["hooks"]["PostToolUse"][1]["hooks"][0]["async"], "custom"
        )
        self.assertIs(after["hooks"]["Stop"][0]["hooks"][0]["async"], False)

    def test_no_settings_file(self):
        record, changed = perf.tweak("claude-hooks-async").apply(
            self.cfg, FakeSystem({})
        )
        self.assertEqual((record, changed), ({"hooks": []}, []))
        self.assertFalse(os.path.exists(self.claude))


class ClaudeEnvTest(Isolated):
    def test_env_added_and_removed(self):
        original = {"cleanupPeriodDays": 90}
        self.write(self.claude, original)
        item = perf.tweak("node-compile-cache")
        record, changed = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(
            self.read(self.claude)["env"],
            {"NODE_COMPILE_CACHE": perf.COMPILE_CACHE_DIR},
        )
        self.assertEqual(changed, [f"NODE_COMPILE_CACHE={perf.COMPILE_CACHE_DIR}"])
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.read(self.claude), original)

    def test_other_env_untouched_and_previous_value_restored(self):
        original = claude_settings()
        original["env"]["NODE_COMPILE_CACHE"] = "/old/cache"
        self.write(self.claude, original)
        item = perf.tweak("node-compile-cache")
        record, _ = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(record["prev"], "/old/cache")
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.read(self.claude), original)

    def test_same_value_already_there_means_nothing_to_undo(self):
        original = {"env": {"NODE_COMPILE_CACHE": perf.COMPILE_CACHE_DIR}}
        self.write(self.claude, original)
        item = perf.tweak("node-compile-cache")
        stamp = os.stat(self.claude).st_mtime_ns
        record, changed = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(changed, [])
        self.assertEqual(os.stat(self.claude).st_mtime_ns, stamp)  # bez zbędnego zapisu
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.read(self.claude), original)


class JsonSettingTest(Isolated):
    def test_budget_in_missing_file_is_removed_with_the_file(self):
        item = perf.tweak("devguard-budget")
        record, _ = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(
            self.read(self.files["devguard-budget"]), {"budget_percent": 25}
        )
        item.undo(record, FakeSystem({}))
        self.assertFalse(os.path.exists(self.files["devguard-budget"]))

    def test_file_bytes_restored_without_trailing_newline(self):
        path = self.files["devguard-budget"]
        with open(path, "w") as f:
            f.write('{\n  "protect": [\n    "~/x"\n  ]\n}')
        before = self.text(path)
        item = perf.tweak("devguard-budget")
        record, _ = item.apply(self.cfg, FakeSystem({}))
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.text(path), before)

    def test_budget_keeps_other_keys_and_restores_old_value(self):
        original = {"protect": ["~/x"], "budget_percent": 30}
        self.write(self.files["devguard-budget"], original)
        item = perf.tweak("devguard-budget")
        record, _ = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(self.read(self.files["devguard-budget"])["budget_percent"], 25)
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.read(self.files["devguard-budget"]), original)

    def test_docker_waits_until_docker_is_closed(self):
        path = self.files["docker-vm"]
        original = {"AutoStart": True, "UseContainerdSnapshotter": True}
        self.write(path, original)
        system = FakeSystem({}, docker=True)
        self.run_cmd(perf.cmd_apply, "docker-vm", system=system)
        self.assertEqual(self.read(path), original)  # Docker działa: nic nie piszemy
        self.assertFalse(perf.load_state()["applied"]["docker-vm"]["written"])
        system.docker = False
        self.run_cmd(perf.cmd_keep, system=system)
        self.assertEqual(self.read(path)["MemoryMiB"], 6144)
        record = perf.load_state()["applied"]["docker-vm"]
        self.assertTrue(record["written"])
        self.assertFalse(record["active"])
        # cofnięcie przy działającym Dockerze czeka na keep
        system.docker = True
        self.run_cmd(perf.cmd_undo, "docker-vm", system=system)
        self.assertEqual(self.read(path)["MemoryMiB"], 6144)
        self.assertIn("docker-vm", perf.load_state()["deferred"])
        system.docker = False
        self.run_cmd(perf.cmd_keep, system=system)
        self.assertEqual(self.read(path), original)
        self.assertEqual(perf.load_state()["deferred"], {})


class GitSpeedTest(Isolated):
    def test_real_repo_config_restored(self):
        repo = os.path.join(self.dir, "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(
            ["git", "-C", repo, "config", "core.untrackedCache", "false"], check=True
        )

        class GitSystem(FakeSystem):
            def git(self, repo, *args):
                return perf.System().git(repo, *args)

        system = GitSystem({})
        cfg = dict(self.cfg, git_repos=[repo])
        record, changed = perf.tweak("git-speed").apply(cfg, system)
        self.assertEqual(changed, [repo])

        def get(key):
            return subprocess.run(
                ["git", "-C", repo, "config", "--local", "--get", key],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()

        self.assertEqual(get("core.untrackedCache"), "true")
        self.assertEqual(get("core.fsmonitor"), "true")
        perf.tweak("git-speed").undo(record, system)
        self.assertEqual(get("core.untrackedCache"), "false")
        self.assertEqual(get("core.fsmonitor"), "")


def transcript_lines():
    """Dwa wywołania narzędzia i jedna tura Stop, jak w transkrypcie Claude Code."""

    def rec(event, tool, ms, stamp, command):
        attachment = {
            "type": "hook_success",
            "hookEvent": event,
            "durationMs": str(ms),
            "command": command,
        }
        if tool:
            attachment["toolUseID"] = tool
        return {"uuid": f"u{stamp}{ms}", "timestamp": stamp, "attachment": attachment}

    return [
        rec("PostToolUse", "t1", 30, "2026-10-04T10:00:00.100Z", ORCA),
        rec(
            "PostToolUse",
            "t1",
            110,
            "2026-10-04T10:00:00.200Z",
            f"{CAVE} post-tool-use",
        ),
        rec("PostToolUse", "t2", 25, "2026-10-04T10:00:05.100Z", ORCA),
        rec(
            "PostToolUse", "t2", 90, "2026-10-04T10:00:05.200Z", f"{CAVE} post-tool-use"
        ),
        rec("Stop", None, 40, "2026-10-04T10:00:09.100Z", ORCA),
        rec("Stop", None, 70, "2026-10-04T10:00:09.300Z", f"{CAVE} stop"),
    ]


class HookLatencyTest(Isolated):
    def test_per_call_max_and_async_skip(self):
        folder = os.path.join(perf.CLAUDE_PROJECTS, "proj")
        os.makedirs(folder)
        with open(os.path.join(folder, "s.jsonl"), "w") as f:
            f.writelines(json.dumps(line) + "\n" for line in transcript_lines())
            f.write("{zepsuta linia hook_success\n")
        since = perf.iso_epoch("2026-10-04T09:00:00")
        stats = perf.hook_latency(since, since + 7200)
        self.assertEqual(stats["PostToolUse"]["n"], 2)
        self.assertEqual(
            stats["PostToolUse"]["p50"], 100
        )  # max z równoległych: 110 i 90
        self.assertEqual(stats["Stop"], {"n": 1, "p50": 70, "p90": 70})
        skipped = perf.hook_latency(
            since, since + 7200, skip=["cavemem/dist/index.js hook run"]
        )
        self.assertEqual(skipped["PostToolUse"]["p50"], 28)
        self.assertEqual(skipped["Stop"]["p50"], 40)


class FsBenchTest(Isolated):
    def test_walk_counts_entries(self):
        root = os.path.join(self.dir, "tree")
        for i in range(3):
            os.makedirs(os.path.join(root, f"d{i}"))
            for j in range(4):
                open(os.path.join(root, f"d{i}", f"f{j}"), "w").close()
        result = perf.bench_fs(dict(self.cfg, fs_bench_path=root))
        self.assertEqual(result["entries"], 15)
        self.assertIn("warm_s", result)
        self.assertIn(
            "error", perf.bench_fs(dict(self.cfg, fs_bench_path="/nonexistent"))
        )


class ClaudeEnvSetTest(Isolated):
    def test_limits_added_and_removed_exactly(self):
        self.write(self.claude, claude_settings())
        before = self.text(self.claude)
        item = perf.tweak("claude-limits")
        record, changed = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(changed, ["BASH_MAX_TIMEOUT_MS=3600000", "MAX_MCP_OUTPUT_TOKENS=50000"])
        self.assertEqual(
            self.read(self.claude)["env"]["BASH_MAX_TIMEOUT_MS"], "3600000"
        )
        again, changed = item.apply(self.cfg, FakeSystem({}), record)
        self.assertEqual((again, changed), (record, []))
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.text(self.claude), before)

    def test_existing_value_kept_and_user_change_survives(self):
        data = claude_settings()
        data["env"]["BASH_MAX_TIMEOUT_MS"] = "1200000"
        self.write(self.claude, data)
        item = perf.tweak("claude-limits")
        record, _ = item.apply(self.cfg, FakeSystem({}))
        self.assertEqual(record["vars"]["BASH_MAX_TIMEOUT_MS"]["prev"], "1200000")
        item.undo(record, FakeSystem({}))
        self.assertEqual(
            self.read(self.claude)["env"]["BASH_MAX_TIMEOUT_MS"], "1200000"
        )
        record, _ = item.apply(self.cfg, FakeSystem({}))
        changed = self.read(self.claude)
        changed["env"]["BASH_MAX_TIMEOUT_MS"] = "900000"  # użytkownik zmienia po nas
        self.write(self.claude, changed)
        item.undo(record, FakeSystem({}))
        self.assertEqual(self.read(self.claude)["env"]["BASH_MAX_TIMEOUT_MS"], "900000")


class HookWrapTest(Isolated):
    def test_wraps_only_listed_plain_commands_and_undoes_exactly(self):
        self.write(self.claude, claude_settings())
        before = self.text(self.claude)
        cfg = dict(
            self.cfg,
            npx_fast_hooks=[
                "/.claude/hooks/auto-format.sh",
                "/.claude/hooks/ts-typecheck.sh",
                "fmt.sh",
            ],
        )
        item = perf.tweak("fast-npx-hooks")
        record, changed = item.apply(cfg, FakeSystem({}))
        self.assertEqual(
            len(changed), 2
        )  # "fmt.sh | tee log" to składnia powłoki: zostaje
        wrap = os.path.join(self.hooks_dir, "npx-fast-wrap.sh")
        hooks = self.read(self.claude)["hooks"]["PostToolUse"][0]["hooks"]
        self.assertEqual(hooks[0]["command"], f"{wrap} {FORMAT}")
        self.assertEqual(hooks[1]["command"], f"{wrap} {TYPECHECK}")
        self.assertEqual(hooks[1]["timeout"], 30)
        self.assertEqual(hooks[2]["command"], "fmt.sh | tee log")
        self.assertTrue(
            os.access(os.path.join(self.hooks_dir, "npx-fast/npx"), os.X_OK)
        )
        again, changed = item.apply(cfg, FakeSystem({}), record)
        self.assertEqual(changed, [])
        self.assertEqual(again["hooks"], record["hooks"])
        item.undo(again, FakeSystem({}))
        self.assertEqual(self.text(self.claude), before)
        self.assertFalse(os.path.exists(self.hooks_dir))

    def test_installed_hooks_are_not_deleted_on_undo(self):
        """Gdy perf.py działa z katalogu instalacji, hooks/ to pliki instalacji, nie kopie."""
        self.write(self.claude, claude_settings())
        os.makedirs(os.path.join(self.hooks_dir, "npx-fast"))
        for rel in perf.HookWrap.FILES:
            shutil.copyfile(
                os.path.join(perf.REPO_HOOKS, rel), os.path.join(self.hooks_dir, rel)
            )
        item = perf.tweak("fast-npx-hooks")
        with mock.patch.object(perf, "REPO_HOOKS", self.hooks_dir):
            record, _ = item.apply(self.cfg, FakeSystem({}))
            self.assertEqual(record["created"], [])
            item.undo(record, FakeSystem({}))
        self.assertTrue(
            os.path.exists(os.path.join(self.hooks_dir, "npx-fast-wrap.sh"))
        )


class PauseHooksTest(Isolated):
    """hook.py (pauza limitów) i Ultra piszą do tego samego settings.json: żadne nie
    może zabrać drugiemu jego wpisów ani cofnąć ich przy swoim cofnięciu."""

    EVENTS = ("PostToolUse", "PreToolUse", "UserPromptSubmit", "Stop", "StopFailure")

    def setUp(self):
        super().setUp()
        import hook

        self.hook = hook
        # jak w prawdziwej instalacji: wrapper szybkiego npx leży w ~/.local/share/claude-acc/hooks
        self.hooks_dir = os.path.join(self.dir, ".local/share/claude-acc/hooks")
        patcher = mock.patch.object(perf, "HOOKS_DIR", self.hooks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write(self.claude, claude_settings())
        with open(self.claude, "a") as f:
            f.write("\n")
        self.original = self.text(self.claude)
        self.tweaks = [perf.tweak(n) for n in ("claude-hooks-async", "fast-npx-hooks", "node-compile-cache")]

    def pause_hooks(self, install):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.hook.run_install([self.claude], install), 0)

    def ours(self):
        data = self.read(self.claude)
        return {
            e: [g for g in data["hooks"][e] if any(self.hook.ours(h) for h in g["hooks"])]
            for e in self.EVENTS
        }

    def ultra_on(self, cfg=None):
        return [t.apply(cfg or self.cfg, FakeSystem({}))[0] for t in self.tweaks]

    def ultra_off(self, records):
        for t, record in reversed(list(zip(self.tweaks, records))):
            t.undo(record, FakeSystem({}))

    def test_ultra_never_makes_pause_hooks_async_or_wrapped(self):
        # hook w tle (async) nie dostarcza additionalContext ani odmowy: pauza by
        # ucichła, a sesje pracowałyby do ściany
        self.pause_hooks(True)
        broad = dict(
            self.cfg,
            async_hooks=self.cfg["async_hooks"] + [{"event": e, "match": "claude-acc"} for e in self.EVENTS],
            npx_fast_hooks=self.cfg["npx_fast_hooks"] + ["claude-acc/hook.py"],
        )

        self.ultra_on(broad)

        expected = {e: [g] for e, g in self.hook.entries().items()}
        self.assertEqual(self.ours(), expected)
        post = self.read(self.claude)["hooks"]["PostToolUse"][1]["hooks"]
        self.assertIs(post[0]["async"], True)  # cavemem: Ultra zadziałało obok

    def test_pause_hooks_and_ultra_come_off_in_any_order(self):
        self.pause_hooks(True)
        records = self.ultra_on()
        self.pause_hooks(False)
        data = self.read(self.claude)
        self.assertIs(data["hooks"]["Stop"][0]["hooks"][0]["async"], True)
        self.assertIn("NODE_COMPILE_CACHE", data["env"])
        self.assertTrue(data["hooks"]["PostToolUse"][0]["hooks"][0]["command"].startswith(self.hooks_dir))
        self.ultra_off(records)
        self.assertEqual(self.text(self.claude), self.original)

        records = self.ultra_on()
        self.pause_hooks(True)
        self.ultra_off(records)
        self.assertEqual({e: len(g) for e, g in self.ours().items()}, dict.fromkeys(self.EVENTS, 1))
        self.pause_hooks(False)
        self.assertEqual(self.text(self.claude), self.original)


class NpxShimTest(unittest.TestCase):
    """Atrapa npx na prawdziwych plikach: narzędzie z node_modules/.bin wyżej w drzewie,
    brak narzędzia, inne wywołania przekazane prawdziwemu npx."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="perf-npx-"))
        self.shim = os.path.join(ROOT, "hooks/npx-fast/npx")
        self.wrap = os.path.join(ROOT, "hooks/npx-fast-wrap.sh")
        bin_dir = os.path.join(self.dir, "proj/node_modules/.bin")
        os.makedirs(bin_dir)
        self.nested = os.path.join(self.dir, "proj/apps/web/src")
        os.makedirs(self.nested)
        self.script(os.path.join(bin_dir, "fakefmt"), 'echo "fakefmt $*"')
        # "prawdziwy" npx, do którego atrapa oddaje resztę wywołań
        self.real = os.path.join(self.dir, "real")
        os.makedirs(self.real)
        self.script(os.path.join(self.real, "npx"), 'echo "real npx $*"')
        self.env = dict(os.environ, PATH=f"{self.real}:/usr/bin:/bin")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def script(self, path, body):
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\n{body}\n")
        os.chmod(path, 0o755)

    def run_in(self, cwd, *cmd):
        return subprocess.run(
            list(cmd),
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_tool_found_up_the_tree(self):
        r = self.run_in(
            self.nested,
            self.shim,
            "--no-install",
            "--quiet",
            "fakefmt",
            "--write",
            "a b.ts",
        )
        self.assertEqual(
            (r.returncode, r.stdout.strip()), (0, "fakefmt --write a b.ts")
        )

    def test_missing_tool_fails_like_npx(self):
        r = self.run_in(self.dir, self.shim, "--no-install", "prettier", "--version")
        self.assertEqual(r.returncode, 127)

    def test_other_calls_go_to_real_npx(self):
        r = self.run_in(self.nested, self.shim, "--yes", "cowsay", "hi")
        self.assertEqual(r.stdout.strip(), "real npx --yes cowsay hi")
        r = self.run_in(self.nested, self.shim, "fakefmt")
        self.assertEqual(r.stdout.strip(), "real npx fakefmt")

    def test_wrapper_puts_shim_first_for_the_hook(self):
        hook = os.path.join(self.dir, "hook.sh")
        self.script(hook, "npx --no-install --quiet fakefmt ok")
        r = self.run_in(self.nested, self.wrap, hook)
        self.assertEqual(r.stdout.strip(), "fakefmt ok")


def tool_transcript():
    """Wywołania narzędzi jak w transkrypcie: go test, edycje .ts i .go, Bash ścięty do 10 min."""
    rows = []

    def call(i, name, args, start, end, result="ok"):
        rows.append(
            {
                "timestamp": start,
                "message": {
                    "content": [
                        {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}
                    ]
                },
            }
        )
        rows.append(
            {
                "timestamp": end,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i}",
                            "content": result,
                        }
                    ]
                },
            }
        )

    call(
        1,
        "Bash",
        {"command": "rtk proxy go test ./internal/x -run TestA"},
        "2026-10-04T10:00:00.000Z",
        "2026-10-04T10:00:10.500Z",
    )
    call(
        2,
        "Edit",
        {"file_path": "/w/app/page.ts"},
        "2026-10-04T10:01:00.000Z",
        "2026-10-04T10:01:05.400Z",
    )
    call(
        3,
        "Edit",
        {"file_path": "/w/app/main.go"},
        "2026-10-04T10:02:00.000Z",
        "2026-10-04T10:02:00.100Z",
    )
    call(
        4,
        "Bash",
        {"command": "go test ./...", "timeout": 1800000},
        "2026-10-04T10:03:00.000Z",
        "2026-10-04T10:13:01.000Z",
        "Command did not complete within its 600s timeout and was moved to the background",
    )
    call(
        5,
        "Bash",
        {"command": "sleep 700", "timeout": 600000},
        "2026-10-04T10:20:00.000Z",
        "2026-10-04T10:30:00.000Z",
        "Command did not complete within its 600s timeout",
    )
    return rows


class AgentTurnaroundTest(Isolated):
    def test_families_formatted_edits_and_capped(self):
        folder = os.path.join(perf.CLAUDE_PROJECTS, "proj", "sesja", "subagents")
        os.makedirs(folder)
        with open(os.path.join(folder, "agent.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in tool_transcript())
        since = perf.iso_epoch("2026-10-04T09:00:00")
        data = perf.agent_turnaround(since, since + 7200)
        fam = data["families"]
        self.assertEqual(fam["Bash: go test"]["n"], 2)
        self.assertEqual(fam["Edit formatowane"], {"n": 1, "p50": 5400, "p90": 5400})
        self.assertEqual(fam["Edit inne"]["p50"], 100)
        self.assertEqual(data["formatted_edits"]["n"], 1)
        # sleep 700 z timeoutem 600000 nie prosił o więcej niż sufit, więc się nie liczy
        self.assertEqual(data["capped"], 1)
        now = perf.iso_epoch("2026-10-04T12:00:00")
        with mock.patch.object(perf.time, "time", return_value=now):
            bench = perf.bench_agents(hours=12)
        self.assertEqual(bench["capped_per_day"], 2.0)
        self.assertEqual(bench["tools"]["Bash: go test"]["n"], 2)
        self.assertEqual(bench["formatted_edits"]["p50"], 5400)


class UltraTest(Isolated):
    def setUp(self):
        super().setUp()
        self.write(self.claude, claude_settings())
        self.devguard = self.files["devguard-budget"]
        self.write(self.devguard, {"protect": ["~/x"]})
        self.originals = {p: self.text(p) for p in (self.claude, self.devguard)}
        self.spotlight = [468066]
        for name, value in {
            "typescript_load": lambda cache_dir=None, runs=5: 40 if cache_dir else 87,
            "hook_latency": lambda since, until=None, events=(), skip=(): {
                "PostToolUse": {"n": 500, "p50": 54, "p90": 99}
            },
            "spotlight_indexed": lambda cfg: self.spotlight[0],
            "SETTLE_SECONDS": 0,
        }.items():
            patcher = mock.patch.object(perf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            perf.BackgroundHelpers, "measure", side_effect=[25.0, 0.2, 25.0, 0.2]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.system = FakeSystem({10: [100, WORKER, False]})

    def ultra(self, *args):
        code, out = self.run_cmd(perf.cmd_ultra, *args, system=self.system)
        self.assertEqual(code, 0)
        return out

    def status(self):
        return json.loads(self.ultra("status", "--json"))

    def test_on_status_off_roundtrip(self):
        self.ultra("on")
        data = self.status()
        self.assertEqual(
            set(data),
            {
                "on",
                "since",
                "applied",
                "root_applied",
                "pending_root",
                "pending_manual",
                "results",
            },
        )
        self.assertTrue(data["on"])
        self.assertEqual(data["applied"], perf.ULTRA)
        self.assertNotIn("docker-vm", data["applied"])  # Docker tylko jako zalecenie
        self.assertIn("vnodes", data["pending_root"])
        self.assertNotIn(
            "shaper", data["pending_root"]
        )  # sieć nie puchnie (brak pomiaru)
        self.assertEqual(data["pending_manual"], ["spotlight-privacy"])
        results = data["results"]
        self.assertEqual(
            results["bg-helpers"], {"before": 25.0, "after": 0.2, "unit": "% rdzenia P"}
        )
        self.assertEqual(
            (
                results["node-compile-cache"]["before"],
                results["node-compile-cache"]["after"],
            ),
            (87, 40),
        )
        self.assertEqual(
            (results["devguard-budget"]["before"], results["devguard-budget"]["after"]),
            (35, 25),
        )
        self.assertEqual(
            (
                results["devguard-max-server"]["before"],
                results["devguard-max-server"]["after"],
            ),
            (5, 4),
        )
        self.assertEqual(results["claude-hooks-async"]["before"], 54)
        # atrapa ma już 500 zdarzeń z nowych sesji, więc status uzupełnia "after"
        self.assertEqual(results["claude-hooks-async"]["after"], 54)
        self.assertEqual(results["spotlight-privacy"]["before"], 468066)
        self.assertIsNone(results["spotlight-privacy"]["after"])
        self.assertTrue(self.system.procs[10][2])
        self.assertEqual(
            self.read(self.devguard),
            {"protect": ["~/x"], "budget_percent": 25, "max_server_gb": 4},
        )

        # drugi raz: pliki bez zmian co do bajtu, ten sam początek, "przed" nie mierzone od nowa
        files = {p: self.text(p) for p in self.originals}
        self.ultra("on")
        again = self.status()
        self.assertEqual(again["since"], data["since"])
        self.assertEqual(again["results"]["bg-helpers"]["before"], 25.0)
        self.assertEqual({p: self.text(p) for p in self.originals}, files)

        # użytkownik wyklucza katalogi w Spotlight: wynik dostaje "after", zadanie znika
        self.spotlight[0] = 0
        with mock.patch.object(perf, "SPOTLIGHT_CHECK_SECONDS", 0):
            after = self.status()
        self.assertEqual(after["results"]["spotlight-privacy"]["after"], 0)
        self.assertEqual(after["pending_manual"], [])

        self.ultra("off")
        off = self.status()
        self.assertFalse(off["on"])
        self.assertEqual((off["applied"], off["pending_root"]), ([], []))
        for path, original in self.originals.items():
            self.assertEqual(self.text(path), original)
        self.assertFalse(self.system.procs[10][2])
        self.assertEqual(perf.load_state()["applied"], {})

    def test_keep_puts_new_worker_pid_in_background(self):
        self.ultra("on")
        del self.system.procs[10]
        self.system.procs[11] = [110, WORKER, False]
        self.run_cmd(perf.cmd_keep, system=self.system)
        self.assertTrue(self.system.procs[11][2])
        self.ultra("off")
        self.assertFalse(self.system.procs[11][2])

    def test_manual_tweak_survives_ultra_off(self):
        self.run_cmd(perf.cmd_apply, "devguard-budget", system=self.system)
        self.ultra("on")
        self.assertNotIn("devguard-budget", self.status()["applied"])
        self.ultra("off")
        self.assertIn("devguard-budget", perf.load_state()["applied"])
        self.assertEqual(
            self.read(self.devguard), {"protect": ["~/x"], "budget_percent": 25}
        )

    def test_component_dropped_from_ultra_is_undone(self):
        """Pierwsza wersja Ultry miała docker-vm; nowe `ultra on` je cofa."""
        docker = self.files["docker-vm"]
        self.write(docker, {"AutoStart": True})
        before = self.text(docker)
        self.run_cmd(perf.cmd_apply, "docker-vm", system=self.system)
        state = perf.load_state()
        state["applied"]["docker-vm"]["ultra"] = True
        perf.ultra_state(state).update(on=True, since=1.0, applied=["docker-vm"])
        perf.save_state(state)
        self.ultra("on")
        self.assertEqual(self.text(docker), before)
        data = self.status()
        self.assertNotIn("docker-vm", data["applied"])
        self.assertNotIn("docker-vm", perf.load_state()["applied"])

    def test_shaper_pending_only_when_network_bloats(self):
        state = perf.load_state()
        perf.record_bench(state, "network", {"idle_ms": 30, "up_net_p90_ms": 900}, 1)
        perf.save_state(state)
        self.ultra("on")
        self.assertIn("shaper", self.status()["pending_root"])

    def test_git_repos_from_orca(self):
        self.write(
            perf.ORCA_DATA, {"repos": [{"path": "/w/portivo"}, {"name": "bez ścieżki"}]}
        )
        self.assertEqual(perf.git_repos({"git_repos": "orca"}), ["/w/portivo"])
        self.assertEqual(
            perf.git_repos({"git_repos": ["~/x"]}), [os.path.expanduser("~/x")]
        )
        self.assertEqual(perf.git_repos({}), [])


class UltraFakeHomeTest(unittest.TestCase):
    """Skrypt w osobnym $HOME: prawdziwe ścieżki pod HOME, prawdziwy proces-wydmuszka."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="perf-ultra-home-"))
        self.marker = f"perf-ultra-dummy-{uuid.uuid4().hex}"
        self.state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.state_dir)
        os.makedirs(os.path.join(self.home, ".claude"))
        self.claude = os.path.join(self.home, ".claude/settings.json")
        with open(self.claude, "w") as f:
            json.dump(claude_settings(), f, indent=2)
            f.write("\n")
        self.devguard = os.path.join(self.state_dir, "devguard.json")
        with open(self.devguard, "w") as f:
            f.write('{\n  "protect": [\n    "~/x"\n  ]\n}')
        with open(os.path.join(self.state_dir, "perf.json"), "w") as f:
            json.dump({"background": [self.marker], "spotlight_noise": []}, f)
        self.originals = {}
        self.originals = self.files()
        self.dummy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)", self.marker]
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
            timeout=120,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def files(self):
        out = {}
        for path in (self.claude, self.devguard):
            with open(path) as f:
                out[path] = f.read()
        return out

    def priority(self):
        out = subprocess.run(
            ["ps", "-o", "pri=", "-p", str(self.dummy.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return int(out.strip())

    def test_on_twice_then_off_restores_bytes(self):
        time.sleep(0.2)
        normal = self.priority()
        self.perf("ultra", "on")
        settings = json.loads(self.files()[self.claude])
        self.assertEqual(
            settings["env"]["NODE_COMPILE_CACHE"],
            os.path.join(self.home, "Library/Caches/node-compile-cache"),
        )
        self.assertIs(settings["hooks"]["Stop"][0]["hooks"][0]["async"], True)
        self.assertEqual(json.loads(self.files()[self.devguard])["max_server_gb"], 4)
        self.assertEqual(self.priority(), perf.BACKGROUND_PRI)
        first = self.files()
        self.perf("ultra", "on")
        self.assertEqual(self.files(), first)
        data = json.loads(self.perf("ultra", "status", "--json"))
        self.assertEqual(data["applied"], perf.ULTRA)
        self.perf("ultra", "off")
        self.assertEqual(self.files(), self.originals)
        self.assertEqual(self.priority(), normal)
        data = json.loads(self.perf("ultra", "status", "--json"))
        self.assertFalse(data["on"])


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
