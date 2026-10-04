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
        for item in perf.TWEAKS:
            if isinstance(item, (perf.AsyncHooks, perf.ClaudeEnv)):
                patcher = mock.patch.object(item, "path", self.claude)
            elif isinstance(item, perf.JsonSetting):
                self.files[item.name] = os.path.join(self.dir, f"{item.name}.json")
                patcher = mock.patch.object(item, "path", self.files[item.name])
            else:
                continue
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


ORCA = 'if [ -z "${HOME-}" ]; then printf "{}"; fi  # ORCA_AGENT_HOOK_PORT'
CAVE = "/x/node /x/lib/node_modules/cavemem/dist/index.js hook run"


def claude_settings():
    """Wycinek prawdziwego ~/.claude/settings.json: hooki cavemem obok hooka Orki."""
    return {
        "cleanupPeriodDays": 90,
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [{"type": "command", "command": "fmt.sh"}],
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
        later = perf.hook_latency(since, since + 7200, born_after=time.time() + 60)
        self.assertEqual(later, {})  # transkrypt powstał przed tą chwilą: stare hooki
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


class UltraTest(Isolated):
    def setUp(self):
        super().setUp()
        self.write(self.claude, claude_settings())
        self.write(self.files["devguard-budget"], {"protect": ["~/x"]})
        self.write(self.files["docker-vm"], {"AutoStart": True})
        self.originals = {
            p: self.read(p)
            for p in (
                self.claude,
                self.files["devguard-budget"],
                self.files["docker-vm"],
            )
        }
        for name, value in {
            "typescript_load": lambda cache_dir=None, runs=5: 40 if cache_dir else 87,
            "hook_latency": lambda since, until=None, events=(), skip=(), born_after=None: {
                "PostToolUse": {"n": 500, "p50": 54, "p90": 99}
            },
        }.items():
            patcher = mock.patch.object(perf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            perf.BackgroundHelpers, "measure", side_effect=[25.0, 0.2, 25.0, 0.2]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(perf, "SETTLE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.system = FakeSystem({10: [100, WORKER, False]})

    def ultra(self, *args):
        code, out = self.run_cmd(perf.cmd_ultra, *args, system=self.system)
        self.assertEqual(code, 0)
        return out

    def test_on_status_off_roundtrip(self):
        self.ultra("on")
        data = json.loads(self.ultra("status", "--json"))
        self.assertEqual(
            set(data),
            {"on", "since", "applied", "pending_root", "pending_manual", "results"},
        )
        self.assertTrue(data["on"])
        self.assertEqual(data["applied"], perf.ULTRA)
        self.assertIn("vnodes", data["pending_root"])
        self.assertNotIn(
            "shaper", data["pending_root"]
        )  # sieć nie puchnie (brak pomiaru)
        results = data["results"]
        self.assertEqual(
            results["bg-helpers"], {"before": 25.0, "after": 0.2, "unit": "% rdzenia P"}
        )
        self.assertEqual(results["node-compile-cache"]["before"], 87)
        self.assertEqual(results["node-compile-cache"]["after"], 40)
        self.assertEqual(results["devguard-budget"]["before"], 35)
        self.assertEqual(results["devguard-budget"]["after"], 25)
        self.assertEqual(results["docker-vm"]["after"], 6.0)
        self.assertEqual(results["claude-hooks-async"]["before"], 54)
        # hooki: po włączeniu jest już 500 zdarzeń (atrapa), więc status uzupełnia "after"
        self.assertEqual(results["claude-hooks-async"]["after"], 54)
        self.assertEqual(self.system.procs[10][2], True)
        self.assertEqual(self.read(self.files["devguard-budget"])["budget_percent"], 25)
        self.assertEqual(self.read(self.files["docker-vm"])["MemoryMiB"], 6144)
        self.assertIn("docker-restart", data["pending_manual"])

        since = data["since"]
        self.ultra("on")  # drugi raz: nic nowego, ten sam początek
        again = json.loads(self.ultra("status", "--json"))
        self.assertEqual(again["since"], since)
        self.assertEqual(again["results"]["bg-helpers"]["before"], 25.0)

        self.ultra("off")
        off = json.loads(self.ultra("status", "--json"))
        self.assertFalse(off["on"])
        self.assertEqual(off["applied"], [])
        self.assertEqual(off["pending_root"], [])
        for path, original in self.originals.items():
            self.assertEqual(self.read(path), original)
        self.assertEqual(self.system.procs[10][2], False)
        self.assertEqual(perf.load_state()["applied"], {})

    def test_manual_tweak_survives_ultra_off(self):
        self.run_cmd(perf.cmd_apply, "devguard-budget", system=self.system)
        self.ultra("on")
        self.assertNotIn(
            "devguard-budget", json.loads(self.ultra("status", "--json"))["applied"]
        )
        self.ultra("off")
        self.assertIn("devguard-budget", perf.load_state()["applied"])
        self.assertEqual(self.read(self.files["devguard-budget"])["budget_percent"], 25)

    def test_docker_running_waits_and_off_defers(self):
        self.system.docker = True
        self.ultra("on")
        data = json.loads(self.ultra("status", "--json"))
        self.assertIn("docker-quit", data["pending_manual"])
        self.assertEqual(self.read(self.files["docker-vm"]), {"AutoStart": True})
        self.ultra("off")
        self.assertEqual(self.read(self.files["docker-vm"]), {"AutoStart": True})
        self.assertEqual(perf.load_state().get("deferred", {}), {})

    def test_shaper_pending_only_when_network_bloats(self):
        state = perf.load_state()
        perf.record_bench(state, "network", {"idle_ms": 30, "up_net_p90_ms": 900}, 1)
        perf.save_state(state)
        self.ultra("on")
        self.assertIn(
            "shaper", json.loads(self.ultra("status", "--json"))["pending_root"]
        )


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
