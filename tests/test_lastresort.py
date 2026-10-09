"""Ostatnia linia strażnika: kogo gasi przy krytycznej presji, a kogo nigdy.

Tabela procesów, rozmiary i wiek są atrapami; scenariusze pochodzą z zamarznięcia Maca
2026-10-08 (sieroty po agentach, headless Chrome, gopls, vitest) i z tego, czego strażnik
nie wolno ruszyć (agent, powłoka, przeglądarka użytkownika, dev serwer z jednostki).
"""

import os
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import devguard_core as dg
import lastresort as lr

MB = 1024**2
GB = 1024**3
NOW = 1_800_000_000.0

CLAUDE = "claude --resume"
ZSH = "-zsh"
MCP = "node /Users/u/.npm/_npx/abc/node_modules/.bin/some-mcp-server"
GOPLS = "/opt/homebrew/bin/gopls"
VITEST = "node /w/node_modules/vitest/vitest.mjs run --maxWorkers=2"
VITEST_FORK = "node /w/node_modules/vitest/dist/workers/forks.js"
CHROME = "/Users/u/Library/Caches/ms-playwright/chromium-1200/chrome-mac/Chromium.app/Contents/MacOS/Chromium --headless --remote-debugging-pipe"
RENDERER = "/Users/u/Library/Caches/ms-playwright/chromium-1200/chrome-mac/Chromium.app/Contents/Frameworks/Helper (Renderer) --type=renderer"
USER_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
GO_TEST_BIN = "/var/folders/x/T/go-build123/b001/booking.test -test.paniconexit0 -test.timeout=10m0s"


def pick(table, sizes, ages, skip=()):
    """choose() na atrapach; rozmiar w MB, wiek w minutach."""
    return lr.choose(
        table,
        lambda pid: sizes.get(pid, 10) * MB,
        lambda pid: ages.get(pid, 60) * 60,
        skip,
    )


class ChooseTest(unittest.TestCase):
    def test_orphan_mcp_server_goes_first_even_when_gopls_is_bigger(self):
        # sierota po martwym agencie nikomu nie służy, gopls jeszcze komuś tak
        table = {
            100: (1, ZSH),
            101: (100, CLAUDE),
            200: (1, MCP),
            300: (101, GOPLS),
        }
        got = pick(table, {200: 400, 300: 1500}, {})
        self.assertEqual(got[0], "orphan")
        self.assertEqual(got[2], 200)

    def test_mcp_server_of_a_live_agent_is_not_a_helper(self):
        table = {100: (1, ZSH), 101: (100, CLAUDE), 102: (101, MCP)}
        self.assertIsNone(pick(table, {102: 2000}, {}))

    def test_background_job_of_a_live_agent_is_not_an_orphan(self):
        # 2026-10-08: agent puścił `pnpm ... &`, jego powłoka wyszła (rodzic 1), a on czekał
        # na wynik przez `claude-acc sched wait`
        table = {101: (1, CLAUDE), 200: (1, MCP)}
        got = lr.choose(
            table,
            lambda pid: GB,
            lambda pid: 3600,
            owned=lambda pid: pid == 200,
        )
        self.assertIsNone(got)

    def test_young_orphan_waits(self):
        # dziesięć minut: agent mógł właśnie przepiąć proces (nohup, disown)
        table = {200: (1, MCP)}
        self.assertIsNone(pick(table, {200: 900}, {200: 4}))
        self.assertEqual(pick(table, {200: 900}, {200: 11})[2], 200)

    def test_small_orphan_is_not_worth_it(self):
        self.assertIsNone(pick({200: (1, MCP)}, {200: 60}, {}))

    def test_orphan_claude_under_node_is_never_touched(self):
        # Claude Code uruchomiony przez node, którego powłoka umarła, to dalej agent
        table = {200: (1, "node /usr/local/bin/claude --resume")}
        self.assertIsNone(pick(table, {200: 900}, {}))

    def test_headless_browser_counts_its_renderers_and_stops_as_one_tree(self):
        table = {
            101: (1, CLAUDE),
            400: (101, "node /w/node_modules/playwright-core/cli.js run-driver"),
            401: (400, CHROME),
            402: (401, RENDERER),
            403: (401, RENDERER),
        }
        got = pick(table, {401: 150, 402: 200, 403: 200}, {})
        self.assertEqual(got[0], "headless")
        self.assertEqual(got[2], 401)
        self.assertEqual(sorted(got[3]), [401, 402, 403])
        self.assertEqual(got[4], 550 * MB)

    def test_user_chrome_is_never_touched(self):
        table = {500: (1, USER_CHROME), 501: (500, USER_CHROME + " Helper (Renderer)")}
        self.assertIsNone(pick(table, {500: 4000, 501: 4000}, {}))

    def test_gopls_over_the_line_goes_before_test_runs(self):
        table = {101: (1, CLAUDE), 300: (101, GOPLS), 600: (101, VITEST)}
        got = pick(table, {300: 1200, 600: 3000}, {})
        self.assertEqual((got[0], got[2]), ("gopls", 300))

    def test_gopls_under_the_line_is_left_alone(self):
        table = {101: (1, CLAUDE), 300: (101, GOPLS)}
        self.assertIsNone(pick(table, {300: 700}, {}))

    def test_vitest_tree_root_is_the_runner_not_its_forks(self):
        table = {
            101: (1, CLAUDE),
            102: (101, "/bin/zsh -c pnpm vitest run"),
            600: (102, VITEST),
            601: (600, VITEST_FORK),
            602: (600, VITEST_FORK),
        }
        got = pick(table, {600: 300, 601: 500, 602: 500}, {})
        self.assertEqual((got[0], got[2]), ("runner", 600))
        self.assertEqual(sorted(got[3]), [600, 601, 602])

    def test_runner_young_or_small_waits(self):
        table = {101: (1, CLAUDE), 700: (101, GO_TEST_BIN)}
        self.assertIsNone(pick(table, {700: 2000}, {700: 1}))
        self.assertIsNone(pick(table, {700: 500}, {}))
        self.assertEqual(pick(table, {700: 2000}, {})[2], 700)

    def test_biggest_candidate_in_a_class_wins(self):
        table = {200: (1, MCP), 201: (1, MCP), 202: (1, MCP)}
        self.assertEqual(pick(table, {200: 300, 201: 900, 202: 500}, {})[2], 201)

    def test_dev_server_pids_from_the_guard_are_skipped(self):
        # dev serwer zostaje strażnikowi: on wie, kto go ogląda
        table = {200: (1, "node /w/node_modules/.bin/next dev"), 201: (1, MCP)}
        got = pick(table, {200: 900, 201: 900}, {}, skip={200, 201})
        self.assertIsNone(got)

    def test_unknown_age_is_not_old_enough(self):
        table = {200: (1, MCP)}
        got = lr.choose(table, lambda pid: GB, lambda pid: None)
        self.assertIsNone(got)


class OwnerTest(unittest.TestCase):
    TABLE = {101: (1, CLAUDE), 102: (1, "codex exec"), 103: (1, ZSH), 200: (1, MCP)}

    def owned(self, env, pgid=None):
        return lr.owner_alive(200, self.TABLE, env, pgid)

    def test_live_agent_from_environment_owns_it(self):
        self.assertTrue(self.owned({"CLAUDE_PID": "101"}))
        self.assertTrue(self.owned({"CLAUDE_PID": "102"}))

    def test_dead_agent_from_environment_leaves_an_orphan(self):
        # pgid żyje (np. przypadkiem ten sam numer), ale agent ze środowiska nie: rozstrzyga agent
        self.assertFalse(self.owned({"CLAUDE_PID": "999"}, pgid=103))

    def test_reused_pid_of_the_agent_is_not_an_agent(self):
        self.assertFalse(self.owned({"CLAUDE_PID": "103"}))

    def test_without_the_variable_the_group_leader_decides(self):
        self.assertTrue(self.owned({}, pgid=101))
        self.assertFalse(self.owned({}, pgid=999))
        # sam sobie liderem: nikt się nie przyznaje
        self.assertFalse(self.owned({}, pgid=200))
        self.assertFalse(self.owned({}, pgid=None))


class ProcEnvTest(unittest.TestCase):
    def test_reads_the_environment_of_another_process(self):
        env = dict(os.environ, LASTRESORT_PROBE="tak=na pewno", CLAUDE_PID="4242")
        # nie /bin/sleep: jądro ukrywa środowisko binarek systemowych (platform binaries)
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; print(1, flush=True); time.sleep(30)"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        try:
            child.stdout.readline()  # po exec, nie w trakcie forka
            got = dg.proc_env(child.pid)
        finally:
            child.kill()
            child.wait()
        self.assertEqual(got.get("LASTRESORT_PROBE"), "tak=na pewno")
        self.assertEqual(got.get("CLAUDE_PID"), "4242")
        self.assertNotIn("executable_path", got)

    def test_arguments_still_parse(self):
        child = subprocess.Popen(["/bin/sleep", "31"], stdin=subprocess.DEVNULL)
        try:
            self.assertEqual(dg.proc_argv(child.pid), ["/bin/sleep", "31"])
        finally:
            child.kill()
            child.wait()

    def test_missing_process_gives_empty_environment(self):
        self.assertEqual(dg.proc_env(999_999), {})


class FakeCore:
    """To, czego reap() używa z devguard_core, bez prawdziwych sygnałów."""

    TICK_NS = 1.0

    def __init__(self, sizes, ages, left=()):
        self.sizes, self.ages, self.left = sizes, ages, list(left)
        self.killed, self.lines = [], []
        self._libc = types.SimpleNamespace(mach_absolute_time=lambda: int(1e12))
        self.janitor = types.SimpleNamespace(
            human=lambda size: f"{size / GB:.1f} GB", notify=mock.Mock()
        )

    def usage(self, pid):
        if pid not in self.sizes:
            return None
        start = int(1e12 - self.ages.get(pid, 3600) * 1e9)
        return {"footprint": self.sizes[pid], "start": start}

    def terminate(self, unit, table, grace=10):
        self.killed.append(sorted(unit.pids))
        return self.left

    def log(self, line):
        self.lines.append(line)

    def proc_env(self, pid):
        return {}


class ReapTest(unittest.TestCase):
    def world(self, table, units=()):
        return types.SimpleNamespace(table=table, units=list(units), now=NOW)

    def test_reaps_one_tree_logs_and_remembers_it(self):
        table = {200: (1, MCP), 201: (200, "node child"), 300: (1, MCP)}
        core = FakeCore({200: 600 * MB, 201: 300 * MB, 300: 200 * MB}, {})
        state = {}
        line = lr.reap(self.world(table), state, core)
        self.assertEqual(core.killed, [[200, 201]])
        self.assertIn("ostatnia linia", line)
        self.assertIn("pid 200", line)
        self.assertEqual(core.lines, [line])
        self.assertEqual(state["lastresort"][0]["pid"], 200)
        self.assertEqual(state["lastresort"][0]["size"], 900 * MB)
        core.janitor.notify.assert_called_once()

    def test_dev_server_units_are_skipped(self):
        table = {200: (1, MCP)}
        core = FakeCore({200: 2 * GB}, {})
        unit = types.SimpleNamespace(pids=[200])
        self.assertIsNone(lr.reap(self.world(table, [unit]), {}, core))
        self.assertEqual(core.killed, [])

    def test_survivors_are_reported(self):
        table = {200: (1, MCP)}
        core = FakeCore({200: 2 * GB}, {}, left=[200])
        line = lr.reap(self.world(table), {}, core)
        self.assertIn("nie chcą zginąć", line)

    def test_history_keeps_the_last_fifty(self):
        state = {"lastresort": [{"pid": i} for i in range(60)]}
        core = FakeCore({200: 2 * GB}, {})
        lr.reap(self.world({200: (1, MCP)}), state, core)
        self.assertEqual(len(state["lastresort"]), 50)
        self.assertEqual(state["lastresort"][-1]["pid"], 200)


class TickTest(unittest.TestCase):
    """tick() woła ostatnią linię tylko przy krytycznej presji, bez akcji strażnika i po
    przerwie między akcjami."""

    def run_tick(self, level=2, plans=(), dry_run=False, last_action=0, **extra):
        cfg = dict(dg.DEFAULT_CONFIG, **extra)
        pressure = types.SimpleNamespace(
            level=level,
            ram=48 * GB,
            swap_used=0,
            compressed=0,
            summary=lambda: {"level": level},
        )
        world = types.SimpleNamespace(
            now=NOW, units=[], table={}, pressure=pressure, orca=None
        )
        state = {"last_action": last_action, "swap_history": [[NOW, 1, 1]]}
        reap = mock.Mock(return_value="ostatnia linia: test")
        with mock.patch.object(dg, "World", return_value=world), mock.patch.object(
            dg, "check_pending"
        ), mock.patch.object(dg, "decide", return_value=list(plans)), mock.patch.object(
            dg, "execute"
        ), mock.patch.object(lr, "reap", reap):
            dg.tick(cfg, state, orca=None, dry_run=dry_run, now=NOW)
        return reap, state

    def test_critical_pressure_with_nothing_left_for_the_guard_reaps(self):
        reap, state = self.run_tick()
        reap.assert_called_once()
        self.assertEqual(state["last_action"], NOW)
        self.assertEqual(state["swap_history"], [])
        self.assertEqual(state["snapshot"]["last_resort"], "ostatnia linia: test")

    def test_warning_pressure_never_reaps(self):
        reap, _ = self.run_tick(level=1)
        reap.assert_not_called()

    def test_guard_action_in_this_tick_comes_first(self):
        plan = types.SimpleNamespace(action="stop", summary=lambda: {})
        # bez przerwy między akcjami: odpuszcza tylko dlatego, że strażnik już zadziałał
        reap, _ = self.run_tick(plans=[plan], cooldown_seconds=0)
        reap.assert_not_called()

    def test_cooldown_after_an_action(self):
        reap, _ = self.run_tick(last_action=NOW - 10)
        reap.assert_not_called()

    def test_dry_run_and_observe_mode_never_reap(self):
        reap, _ = self.run_tick(dry_run=True)
        reap.assert_not_called()
        reap, _ = self.run_tick(mode="observe")
        reap.assert_not_called()

    def test_can_be_switched_off(self):
        reap, _ = self.run_tick(last_resort=False)
        reap.assert_not_called()


# 2026-10-08 od 17:19:15 do ostatniego zapisu strażnika przed zamrożeniem (18:52:13; Mac stanął
# o 18:56:43, czyli 5848 s od początku): sekundy od początku, swap w GB, kompresor w GB, co ~30 s,
# z historii strażnika (devguard-state.json). 48 GB RAM.
FREEZE_0810 = """
    0,4.36,10.19 31,4.36,10.15 62,4.36,9.98 92,4.36,10.15 123,4.35,10.08 154,4.35,14.75 
    185,4.34,15.72 216,4.34,17.75 248,4.34,17.12 279,4.34,19.13 310,4.34,17.05 341,4.34,15.6 
    372,4.34,18.27 407,4.34,19.71 438,4.34,21.67 471,6.81,21.52 504,8.8,20.26 538,8.75,19.86 
    571,8.7,19.85 603,8.65,17.97 635,8.6,19.26 668,9.75,16.76 700,9.68,16.8 734,9.65,17.49 
    766,9.64,15.24 798,9.56,17.64 833,9.55,20.28 867,9.54,18.42 899,9.51,16.64 930,9.5,15.21 
    961,9.5,14.26 992,9.49,16.34 1024,9.48,18.85 1057,9.48,17.51 1090,10.42,19.31 1123,10.22,14.87 
    1156,10.12,16.57 1188,10.07,16.47 1220,10.84,20.75 1254,13.38,18.91 1288,13.35,17.36 
    1321,13.3,16.08 1354,13.27,13.25 1386,13.17,12.83 1418,13.13,14.15 1450,13.07,14.51 
    1481,13.04,17.11 1513,13.01,14.66 1543,13.0,13.9 1575,12.98,16.18 1608,12.97,16.87 
    1641,12.95,16.49 1674,12.89,17.35 1706,12.88,15.44 1737,12.87,13.92 1771,12.85,12.91 
    1802,12.82,12.69 1833,12.81,13.12 1863,12.78,13.22 1894,12.78,14.81 1926,12.76,12.85 
    1957,12.74,12.43 1988,12.7,11.85 2018,12.68,12.05 2049,12.68,11.98 2080,12.67,12.31 
    2110,12.65,13.51 2141,12.64,14.01 2172,12.64,14.41 2203,12.56,14.6 2233,12.56,13.5 
    2265,12.53,18.42 2297,12.51,17.78 2329,12.51,18.36 2361,12.49,17.01 2392,12.49,18.62 
    2423,12.47,19.27 2456,12.45,16.44 2487,12.43,20.41 2519,12.64,20.99 2550,12.79,16.07 
    2581,12.78,15.75 2611,12.76,15.1 2642,12.71,16.15 2673,12.71,18.71 2704,12.7,18.91 
    2735,12.56,16.72 2766,12.55,16.71 2797,12.55,17.53 2828,12.55,16.48 2858,12.49,15.22 
    2890,12.47,15.34 2921,9.23,13.59 2951,9.16,11.35 2982,9.16,13.33 3014,9.15,15.46 
    3045,9.15,14.07 3076,9.14,16.84 3108,9.12,16.91 3141,9.12,16.0 3174,9.09,20.49 3207,9.08,19.27 
    3238,9.05,19.06 3270,10.24,21.53 3301,10.24,17.74 3331,10.24,17.5 3363,10.22,17.12 
    3394,10.21,18.4 3427,10.2,19.83 3460,10.2,17.4 3492,10.2,17.69 3525,10.49,21.12 
    3560,11.79,21.42 3592,12.21,17.19 3624,12.05,13.3 3655,12.01,13.59 3686,11.98,13.53 
    3717,11.98,14.71 3748,11.96,13.76 3780,11.94,14.09 3812,11.89,15.15 3844,11.88,16.98 
    3877,11.87,17.75 3909,11.85,16.19 3943,11.77,16.15 3974,11.77,18.15 4008,11.73,18.14 
    4043,11.69,18.98 4077,11.96,19.97 4111,11.87,16.26 4145,11.71,16.81 4177,11.66,19.94 
    4209,12.03,18.84 4242,12.74,16.19 4275,12.73,17.08 4305,12.78,21.05 4338,12.77,18.12 
    4370,12.74,15.2 4403,12.73,17.41 4437,12.72,14.99 4471,12.66,16.38 4504,12.62,16.15 
    4539,10.08,17.89 4574,10.07,18.42 4607,10.07,18.86 4639,10.03,11.78 4671,9.98,14.87 
    4702,9.88,12.46 4733,9.86,11.8 4766,10.06,17.92 4798,10.53,17.43 4830,10.44,17.61 
    4861,10.96,19.91 4894,11.51,14.06 4925,11.39,13.87 4958,11.13,13.43 4990,11.06,12.2 
    5023,10.97,15.94 5054,10.9,15.0 5085,10.88,16.23 5116,10.83,16.63 5148,10.82,17.89 
    5182,10.94,21.52 5217,11.06,17.05 5250,10.97,17.79 5285,11.12,21.9 5320,13.12,21.48 
    5355,14.04,17.3 5387,13.98,20.81 5417,15.69,19.29 5448,16.38,20.7 5481,18.1,20.59 
    5514,19.38,18.2 5546,15.99,21.12 5578,15.93,20.64
"""
FREEZE_AT = 5848


def replay(rows, cfg=None):
    """[(sekunda, stopień)] dla każdego wiersza, ze wzrostem swapu w oknie 2 min jak w Pressure."""
    out = []
    for i, (t, swap, comp) in enumerate(rows):
        window = [r for r in rows[: i + 1] if t - r[0] <= 120]
        level, _ = lr.stage({"ram": 48 * GB, "compressed": comp * GB, "swap_used": swap * GB,
                             "swap_growth": (swap - window[0][1]) * GB, "kernel": 1}, cfg)
        out.append((t, level))
    return out


class StageTest(unittest.TestCase):
    """Stopień hamulca z sygnałów pamięci. Błąd, który łapie: hamulec, który rusza dopiero przy
    zamrożeniu (za późno) albo przy zwykłym stojącym swapie (zabija przy normalnej pracy)."""

    ROWS = [tuple(float(x) for x in item.split(",")) for item in FREEZE_0810.split()]

    def first(self, level):
        return next((t for t, lvl in replay(self.ROWS) if lvl >= level), None)

    def test_replay_of_the_freeze_brakes_long_before_it(self):
        # 17:22-17:24 jetsam zabił ~100 demonów, 17:25 raport Jetsam: Mac już się dusił
        self.assertLessEqual(self.first(1), 7 * 60)       # ciasno najpóźniej o 17:26
        self.assertLessEqual(self.first(2), 12 * 60)      # hamulec najpóźniej o 17:31
        emergency = self.first(3)
        self.assertIsNotNone(emergency)
        self.assertLessEqual(emergency, FREEZE_AT - 5 * 60)  # awaria co najmniej 5 min przed

    def test_calm_hour_before_the_trouble_stays_calm(self):
        # pierwsze 2,5 min: swap 4,4 GB stoi, kompresor 10 GB: zwykła praca
        self.assertEqual({lvl for t, lvl in replay(self.ROWS) if t < 150}, {0})

    def test_standing_swap_alone_is_never_a_brake(self):
        sig = {"ram": 48 * GB, "swap_used": 20 * GB, "swap_growth": 0, "compressed": 8 * GB}
        self.assertEqual(lr.stage(sig)[0], 0)

    def test_compressor_and_segments_against_their_limits(self):
        base = {"ram": 48 * GB, "segments_limit": 1_000_000}
        self.assertEqual(lr.stage(dict(base, compressed=20 * GB))[0], 1)
        self.assertEqual(lr.stage(dict(base, compressed=25 * GB))[0], 2)
        self.assertEqual(lr.stage(dict(base, compressed=34 * GB))[0], 3)  # zamrożenie 08.10
        self.assertEqual(lr.stage(dict(base, segments=650_000))[0], 2)
        self.assertEqual(lr.stage(dict(base, segments=850_000))[0], 3)

    def test_kernel_and_guard_critical_is_a_brake(self):
        self.assertEqual(lr.stage({"ram": 48 * GB, "kernel": 4})[0], 2)
        self.assertEqual(lr.stage({"ram": 48 * GB, "guard_level": 2})[0], 2)
        self.assertEqual(lr.stage({"ram": 48 * GB, "kernel": 4, "swap_growth": 3 * GB})[0], 3)

    def test_thresholds_come_from_config(self):
        cfg = {"brake": {"compressor_brake_percent": 30}}
        self.assertEqual(lr.stage({"ram": 48 * GB, "compressed": 16 * GB}, cfg)[0], 2)


SCHED_SHELL = "/bin/zsh -c pnpm sm capture"


class BrakeChooseTest(unittest.TestCase):
    """Kogo hamulec wybiera na stopniach 2 i 3 poza klasami ostatniej linii."""

    def test_scheduler_job_is_its_shells_children_with_a_resume_command(self):
        table = {101: (1, CLAUDE), 102: (101, "/bin/zsh -c x"), 103: (102, "python acc.py sched run"),
                 104: (103, SCHED_SHELL), 105: (104, "node plugins/cli/main.ts capture"),
                 106: (105, CHROME)}
        job = {"id": "j-1", "child_pgid": 104, "cmd": "pnpm sm capture", "label": "pnpm sm capture",
               "agent": {"worktree": "/w/site"}}
        got = lr.choose(table, lambda pid: 700 * MB, lambda pid: 600, jobs=[job])
        self.assertEqual(got[0], "job")
        self.assertEqual(sorted(got[3]), [105, 106])  # bez powłoki joba i wrappera schedulera
        self.assertIs(got[5], job)
        self.assertEqual(lr.resume_of(105, job, None), "cd /w/site && pnpm sm capture")

    def test_any_agent_command_tree_only_in_an_emergency(self):
        table = {101: (1, CLAUDE), 102: (101, "/bin/zsh -c node capture.js"),
                 103: (102, "node capture.js"), 104: (101, MCP)}
        pick = lambda level: lr.choose(table, lambda pid: 3 * GB, lambda pid: 5, level=level)  # noqa: E731
        self.assertIsNone(pick(2))
        got = pick(3)
        self.assertEqual((got[0], got[2]), ("agent", 103))  # serwer MCP agenta (104) nie

    def test_emergency_ignores_the_minimum_age(self):
        table = {101: (1, CLAUDE), 700: (101, GO_TEST_BIN)}
        self.assertIsNone(pick(table, {700: 2000}, {700: 1}))
        got = lr.choose(table, lambda pid: 2 * GB, lambda pid: 30, level=3)
        self.assertEqual(got[2], 700)

    def test_runaway_process_dies_on_any_stage(self):
        # dawny memory-guard: node z testem urósł do 229 GB w 80 s (2026-09-30)
        table = {101: (1, CLAUDE), 102: (101, "/bin/zsh -c node t.js"), 103: (102, "node t.js")}
        got = lr.choose(table, lambda pid: 25 * GB if pid == 103 else MB, lambda pid: 5, level=0, ram=48 * GB)
        self.assertEqual((got[0], got[2]), ("runaway", 103))
        self.assertIsNone(lr.choose(table, lambda pid: 10 * GB, lambda pid: 5, level=0, ram=48 * GB))
        # sam agent nie: Claude Code zajmujący pół RAM dostaje najwyżej powiadomienie gdzie indziej
        self.assertIsNone(lr.choose({101: (1, CLAUDE)}, lambda pid: 30 * GB, lambda pid: 5, level=3, ram=48 * GB))

    def test_bloated_git_and_language_servers(self):
        table = {101: (1, CLAUDE), 200: (101, "git grep -nE x HEAD"),
                 300: (101, "/opt/homebrew/bin/node /x/typescript/lib/tsserver.js --useInferredProjectPerProjectRoot")}
        got = lr.choose(table, lambda pid: 10 * GB if pid == 200 else 2 * GB, lambda pid: 600)
        self.assertEqual((got[0], got[2]), ("git", 200))
        got = lr.choose({300: table[300], 101: table[101]}, lambda pid: 2 * GB, lambda pid: 600)
        self.assertEqual(got[0], "lsp")

    def test_dry_run_signals_nobody(self):
        core = FakeCore({200: 2 * GB}, {})
        line = lr.reap(types.SimpleNamespace(table={200: (1, MCP)}, units=[], now=NOW), {}, core, dry_run=True)
        self.assertIn("próba", line)
        self.assertEqual(core.killed, [])
        # próba (`guard brake`) tylko pokazuje: nic do logu strażnika, żadnego powiadomienia
        self.assertEqual(core.lines, [])
        core.janitor.notify.assert_not_called()


# perl pod inną nazwą: python z Xcode wraca do Python.app pod /Applications (święte), a /usr/bin też
AGENT_PL = '$| = 1; my $rc = system("/bin/zsh", "-c", "$ARGV[0] $ARGV[1] $ARGV[2]; true"); print "shell $rc\\n"; sleep 60;\n'
CHILD_PL = '$SIG{TERM} = "IGNORE"; $| = 1; my $x = "a" x ($ARGV[0] * 1024 * 1024); print "ready\\n"; sleep 120;\n'


def synthetic_agent(test, mb):
    """Udawany agent (argv0 `claude`) -> /bin/zsh -c -> proces z balastem `mb` MB, który ignoruje
    SIGTERM; zwraca (agent, pid powłoki, pid procesu)."""
    import tempfile

    d = tempfile.mkdtemp(prefix="brake-tree-")
    test.addCleanup(subprocess.run, ["rm", "-rf", d])
    node = os.path.join(d, "node")
    os.symlink("/usr/bin/perl", node)
    for name, text in (("agent.pl", AGENT_PL), ("child.pl", CHILD_PL)):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
    agent = subprocess.Popen(
        ["/bin/bash", "-c", f"exec -a claude {node} {d}/agent.pl {node} {d}/child.pl {mb}"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    test.addCleanup(lambda: (subprocess.run(["pkill", "-9", "-f", d]), agent.wait()))
    test.assertEqual(agent.stdout.readline().strip(), "ready")
    table = {pid: (ppid, command) for pid, ppid, command in dg.processes()}
    shell = next(p for p, (pp, _c) in table.items() if pp == agent.pid)
    child = next(p for p, (pp, _c) in table.items() if pp == shell)
    return agent, shell, child, table


class BrakeOnRealProcessesTest(unittest.TestCase):
    """Hamulec na prawdziwym drzewie: udawany agent, jego powłoka i proces z balastem, który
    ignoruje SIGTERM. Błąd, który łapie: „nie chcą zginąć” przy procesie, który ginie po
    SIGKILL, i zabity agent albo powłoka."""

    def test_sigterm_then_sigkill_frees_the_tree_and_spares_agent_and_shell(self):
        agent, shell, child, table = synthetic_agent(self, 200)
        tree = {agent.pid, shell, child}
        world = types.SimpleNamespace(table={p: table[p] for p in tree}, units=[], now=NOW,
                                      pressure=types.SimpleNamespace(ram=300 * MB))
        start_abs = dg.usage(child)["start"]
        state = {}
        logged, notify = [], mock.Mock()
        began = time.time()
        # log, powiadomienie i joby schedulera z tego testu, nie z prawdziwego ~/.local/share
        with mock.patch.object(dg, "log", logged.append), mock.patch.object(dg, "notify_quiet", notify), \
                mock.patch.object(lr, "sched_jobs", lambda core: []):
            line = lr.reap(world, state, dg, level=3, cfg={"brake": {"emergency_grace_seconds": 1}})
        self.assertIn("zatrzymany", line)
        self.assertEqual(logged, [line])
        notify.assert_called_once()
        self.assertGreaterEqual(time.time() - began, 1.0)  # SIGTERM zignorowany, dopiero SIGKILL
        self.assertFalse(dg.alive(child, start_abs))
        self.assertIsNone(agent.poll())  # agent żyje
        self.assertEqual(agent.stdout.readline().strip(), "shell 0")  # powłoka skończyła sama, kodem 0
        self.assertEqual(state["lastresort"][0]["code"], "runaway")
        self.assertIn("wznowienie: cd ", line)


if __name__ == "__main__":
    unittest.main()
