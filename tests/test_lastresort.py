"""Ostatnia linia strażnika: kogo gasi przy krytycznej presji, a kogo nigdy.

Tabela procesów, rozmiary i wiek są atrapami; scenariusze pochodzą z zamarznięcia Maca
2026-10-08 (sieroty po agentach, headless Chrome, gopls, vitest) i z tego, czego strażnik
nie wolno ruszyć (agent, powłoka, przeglądarka użytkownika, dev serwer z jednostki).
"""

import os
import subprocess
import sys
import types
import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
