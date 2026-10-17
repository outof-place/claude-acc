"""Rejestr decyzji (wydanie A) nie zmienia żadnej decyzji zabicia ani pauzy.

Test liczy tablice decyzji na siatce wejść: stopień hamulca (lastresort.stage) na progach każdego
sygnału i na odtworzeniu zamrożenia 2026-10-08, wybór ofiary i całe reap() na atrapach procesów,
poziom i powody presji strażnika (devguard_core.Pressure na podrobionych sysctl), bramkę hamulca
(devguard_core.brake: stopień, przerwy, akcja strażnika w tym przebiegu, uciekinier), reset okna
swapu w tick() i pauzę schedulera (sched.safety). Wynik porównuje z tablicami policzonymi tym samym
kodem testu na drzewie sprzed wydania A (filip/live 2f190f1): tests/decision-tables.json.

Pomyłka, którą łapie: wiersz rejestru dopisany w reap, tick, brake albo safety, który przy okazji
zmienia wybór ofiary, próg, przerwę, kolejność albo to, co zostaje w stanie.

Nowe tablice wolno zapisać tylko po świadomej zmianie reguły:
  python3 tests/test_decision_replay.py --write
"""

import hashlib
import importlib.util
import itertools
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import devguard_core as dg  # noqa: E402
import lastresort as lr  # noqa: E402

_spec = importlib.util.spec_from_file_location("acc_sched_replay", os.path.join(ROOT, "sched.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

GOLDEN = os.path.join(HERE, "decision-tables.json")
MB = 1024**2
GB = 1024**3
NOW = 1_800_000_000.0
# tablice dłuższe niż tyle wierszy trzymamy jako skrót i liczbę wierszy, krótsze w całości
FULL_ROWS = 200

CLAUDE = "claude --resume"
ZSH = "-zsh"
MCP = "node /Users/u/.npm/_npx/abc/node_modules/.bin/some-mcp-server"
GOPLS = "/opt/homebrew/bin/gopls"
VITEST = "node /w/node_modules/vitest/vitest.mjs run --maxWorkers=2"
VITEST_FORK = "node /w/node_modules/vitest/dist/workers/forks.js"
CHROME = "/u/Library/Caches/ms-playwright/chromium-1200/Chromium.app/Contents/MacOS/Chromium --headless"
RENDERER = "/u/Library/Caches/ms-playwright/chromium-1200/Chromium.app/Contents/Frameworks/Helper (Renderer)"
USER_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
GO_TEST_BIN = "/var/folders/x/T/go-build123/b001/booking.test -test.paniconexit0"
TSSERVER = "/opt/homebrew/bin/node /x/typescript/lib/tsserver.js --useInferredProjectPerProjectRoot"
SCHED_SHELL = "/bin/zsh -c pnpm sm capture"

# tabele procesów ze scenariuszy tests/test_lastresort.py i ich skrzyżowania: {pid: (ppid, komenda)}
TABLES = {
    "orphans_gopls_vitest": {
        100: (1, ZSH), 101: (100, CLAUDE), 200: (1, MCP), 201: (200, "node child"), 300: (101, GOPLS),
        400: (101, "/bin/zsh -c x"), 401: (400, VITEST), 402: (401, VITEST_FORK), 403: (401, VITEST_FORK),
    },
    "headless_and_user_chrome": {
        101: (1, CLAUDE), 500: (101, CHROME), 501: (500, RENDERER), 502: (500, RENDERER), 600: (1, USER_CHROME),
    },
    "git_lsp_runner": {
        101: (1, CLAUDE), 102: (101, "/bin/zsh -c go test"), 200: (101, "git grep -nE x HEAD"),
        300: (101, TSSERVER), 700: (102, GO_TEST_BIN),
    },
    "agent_trees": {
        101: (1, CLAUDE), 102: (101, "/bin/zsh -c node capture.js"), 103: (102, "node capture.js"),
        104: (101, MCP),
    },
    "orphan_and_agent_tree": {
        101: (1, CLAUDE), 102: (101, "/bin/zsh -c node capture.js"), 103: (102, "node capture.js"),
        105: (1, "node /orphan/worker.js"),
    },
    "language_servers": {
        101: (1, CLAUDE), 300: (101, GOPLS), 301: (101, TSSERVER),
    },
    "scheduler_job": {
        101: (1, CLAUDE), 102: (101, "/bin/zsh -c x"), 103: (102, "python acc.py sched run"),
        104: (103, SCHED_SHELL), 105: (104, "node plugins/cli/main.ts capture"), 106: (105, CHROME),
    },
    "stale_job_entry": {
        101: (1, CLAUDE), 104: (1, "/usr/local/bin/some-daemon"), 105: (104, "node worker.js"),
    },
}
JOBS = {
    "scheduler_job": [{"id": "j-1", "pid": 103, "child_pgid": 104, "cmd": "pnpm sm capture",
                       "label": "pnpm sm capture", "agent": {"worktree": "/w/site"}}],
    "stale_job_entry": [{"id": "j-1", "pid": 103, "child_pgid": 104, "cmd": "pnpm sm capture"}],
}
# rozmiary (MB) i wiek (s): kilka profili na każdą tabelę, żeby progi rozmiaru i wieku pracowały
PROFILES = {
    "small_young": (lambda pid: 50, lambda pid: 30),
    "mid_old": (lambda pid: 700, lambda pid: 900),
    "big_old": (lambda pid: 3000, lambda pid: 3600),
    "mixed": (lambda pid: (pid % 7 + 1) * 400, lambda pid: (pid % 5) * 200),
    "one_runaway": (lambda pid: 26000 if pid in (103, 200, 401, 500, 700) else 300, lambda pid: 5),
    "unknown_age": (lambda pid: 2500, lambda pid: None if pid % 2 else 1200),
}


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def summarize(rows):
    out = {"rows": len(rows), "sha256": digest(rows)}
    if len(rows) <= FULL_ROWS:
        out["table"] = rows
    return out


def stage_grid():
    """lastresort.stage na progach: tuż pod i na każdym progu każdego sygnału."""
    ram = 48 * GB
    rows = []
    for comp, segs, swap, growth, kernel, guard in itertools.product(
        (39, 40, 49, 50, 59, 60), (None, 59, 60, 79, 80), (11, 12, 19, 20, 29, 30),
        (0.49, 0.5, 0.99, 1.0, 1.99, 2.0), (1, 2, 4), (0, 2),
    ):
        sig = {"ram": ram, "compressed": comp / 100 * ram, "swap_used": swap / 100 * ram,
               "swap_growth": growth * GB, "kernel": kernel, "guard_level": guard}
        if segs is not None:
            sig.update(segments=segs * 10_000, segments_limit=1_000_000)
        level, reasons = lr.stage(sig)
        rows.append([comp, segs, swap, growth, kernel, guard, level, reasons])
    return rows


FREEZE = os.path.join(HERE, "test_lastresort.py")


def freeze_replay():
    """Stopień w każdym wierszu zamrożenia 08.10 (tests/test_lastresort.py: FREEZE_0810)."""
    spec = importlib.util.spec_from_file_location("replay_lastresort", FREEZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [tuple(float(x) for x in item.split(",")) for item in module.FREEZE_0810.split()]
    return [list(r) for r in module.replay(rows)]


def choose_table():
    """lastresort.choose na każdej tabeli, profilu rozmiaru i wieku, stopniu, z RAM i bez."""
    rows = []
    for (tname, table), (pname, (size, age)), level, ram in itertools.product(
        sorted(TABLES.items()), sorted(PROFILES.items()), (0, 1, 2, 3), (None, 48 * GB),
    ):
        got = lr.choose(table, lambda pid, f=size: f(pid) * MB, age, skip=(), level=level,
                        jobs=JOBS.get(tname, ()), ram=ram)
        if got is not None:
            code, label, pid, tree, sz, job = got
            got = [code, label, pid, sorted(tree), sz, (job or {}).get("id")]
        rows.append([tname, pname, level, ram, got])
    return rows


class FakeCore:
    """To, czego reap() używa z devguard_core, bez prawdziwych sygnałów (jak w test_lastresort)."""

    TICK_NS = 1.0

    def __init__(self, sizes, ages, left=()):
        self.sizes, self.ages, self.left = sizes, ages, list(left)
        self.killed, self.lines, self.notes = [], [], []
        self._libc = types.SimpleNamespace(mach_absolute_time=lambda: int(1e12))
        self.janitor = types.SimpleNamespace(human=lambda s: f"{s / GB:.1f} GB",
                                             notify=lambda title, text: self.notes.append([title, text]))

    def usage(self, pid):
        if pid not in self.sizes:
            return None
        age = self.ages.get(pid, 3600)
        return {"footprint": self.sizes[pid], "start": int(1e12 - (age if age is not None else 0) * 1e9)}

    def terminate(self, unit, table, grace=10):
        self.killed.append([sorted(unit.pids), grace])
        return self.left

    def log(self, line):
        self.lines.append(line)

    def proc_env(self, pid):
        return {}


def reap_table():
    """Całe reap() na atrapach: kogo gasi, z jaką łaską, linia logu, wpis w stanie, powiadomienie."""
    rows = []
    for (tname, table), (pname, (size, age)), level, left in itertools.product(
        sorted(TABLES.items()), sorted(PROFILES.items()), (0, 2, 3), ((), (999,)),
    ):
        core = FakeCore({pid: size(pid) * MB for pid in table}, {pid: age(pid) for pid in table}, left)
        world = types.SimpleNamespace(table=table, units=[], now=NOW, pressure=types.SimpleNamespace(ram=48 * GB))
        state = {"lastresort": [{"pid": i} for i in range(55)]}
        with mock.patch.object(lr, "sched_jobs", lambda core, t=tname: JOBS.get(t, [])):
            line = lr.reap(world, state, core, level=level)
        rows.append([tname, pname, level, list(left), line, core.killed, core.lines, core.notes,
                     state["lastresort"][-1], len(state["lastresort"])])
    return rows


SYSCTL_KEYS = ("hw.memsize", "kern.memorystatus_vm_pressure_level", "kern.memorystatus_level",
               "vm.compressor_bytes_used", "vm.compressor.segment.total", "vm.compressor.segment.limit",
               "vm.compressor.compactor.swapouts_queued_pressure")


def pressure_table():
    """devguard_core.Pressure na podrobionych sysctl: poziom strażnika, powody, notatki, stopień."""
    ram = 48 * GB
    rows = []
    for kernel, available, swap_pct, growth_mb, swapouts, comp in itertools.product(
        (1, 2, 4), (5, 10, 11, 20, 21, 60), (0, 11, 12, 19, 20, 31), (0, 255, 256, 1200), (0, 999, 1000),
        (10, 50),
    ):
        swap = swap_pct / 100 * ram
        values = {"hw.memsize": ram, "kern.memorystatus_vm_pressure_level": kernel,
                  "kern.memorystatus_level": available, "vm.compressor_bytes_used": comp / 100 * ram,
                  "vm.compressor.segment.total": 300_000, "vm.compressor.segment.limit": 1_000_000,
                  "vm.compressor.compactor.swapouts_queued_pressure": 5000 + swapouts}
        state = {"swap_history": [[NOW - 200, 0, 0], [NOW - 100, swap - growth_mb * MB, 5000]]}
        with mock.patch.object(dg, "sysctl_int", values.get), \
                mock.patch.object(dg, "swap_usage", lambda s=swap: (64 * GB, s)):
            p = dg.Pressure(dict(dg.DEFAULT_CONFIG), state, NOW)
        rows.append([kernel, available, swap_pct, growth_mb, swapouts, comp, p.level, p.reasons, p.notes,
                     p.stage, p.stage_reasons, p.swapping, p.swap_growth, p.swapouts, state["swap_history"]])
    return rows


def brake_table():
    """devguard_core.brake: kiedy woła reap (i z jakim stopniem) i co zostawia w stanie."""
    rows = []
    for stage, biggest_gb, brake_ago, action_ago, acted, mode, enabled, reaped in itertools.product(
        (None, 0, 1, 2, 3), (0, 23, 24.5), (None, 3, 6, 19, 21), (None, 10, 44, 46), (False, True),
        ("enforce", "off"), (True, False), ("ostatnia linia: x", None),
    ):
        cfg = dict(dg.DEFAULT_CONFIG, last_resort=enabled)
        pressure = types.SimpleNamespace(level=2 if stage is None else min(stage, 2), ram=48 * GB)
        if stage is not None:
            pressure.stage = stage
        world = types.SimpleNamespace(pressure=pressure)
        state = {"swap_history": [[NOW, 1, 1]], "inventory": {"biggest": [1, biggest_gb * GB, "node t.js"]}}
        if brake_ago is not None:
            state["brake_at"] = NOW - brake_ago
        if action_ago is not None:
            state["last_action"] = NOW - action_ago
        calls = []

        def fake_reap(world, state, core, level=2, cfg=None, dry_run=False, **extra):
            calls.append(level)
            return reaped

        with mock.patch.object(lr, "reap", fake_reap):
            out = dg.brake(cfg, world, state, NOW, mode == "enforce", object() if acted else None)
        rows.append([stage, biggest_gb, brake_ago, action_ago, acted, mode, enabled, reaped, out, calls,
                     state.get("brake_at"), state.get("last_action"), state["swap_history"]])
    return rows


def tick_table():
    """tick(): akcja strażnika i hamulec w jednym przebiegu: co wykonane, co zostaje w stanie."""
    rows = []
    for stage, plan_actions, executed, action_ago, dry_run in itertools.product(
        (0, 1, 2, 3), ((), ("warn",), ("stop",), ("recycle",), ("warn", "stop")), (True, False),
        (None, 10, 50), (False, True),
    ):
        cfg = dict(dg.DEFAULT_CONFIG)
        pressure = types.SimpleNamespace(level=min(stage, 2), stage=stage, stage_reasons=[], reasons=[], ram=48 * GB,
                                         swap_used=0, compressed=0, summary=lambda s=stage: {"stage": s})
        unit = types.SimpleNamespace(label=":3000 /w/app", footprint=2 * GB, ports=[3000], key="u")
        plans = [types.SimpleNamespace(action=a, unit=unit, reason="r", code="c", data={},
                                       summary=lambda a=a: {"action": a}) for a in plan_actions]
        world = types.SimpleNamespace(now=NOW, units=[], table={}, pressure=pressure, orca=None)
        state = {"swap_history": [[NOW, 1, 1]]}
        if action_ago is not None:
            state["last_action"] = NOW - action_ago
        done, reaps = [], []

        def fake_execute(cfg, plan, world, state, ok=executed):
            done.append(plan.action)
            return ok

        def fake_reap(world, state, core, level=2, cfg=None, dry_run=False, **extra):
            reaps.append(level)
            return "ostatnia linia: x"

        with mock.patch.object(dg, "World", return_value=world), mock.patch.object(dg, "check_pending"), \
                mock.patch.object(dg, "decide", return_value=plans), mock.patch.object(dg, "execute", fake_execute), \
                mock.patch.object(lr, "reap", fake_reap):
            _w, _p, acted = dg.tick(cfg, state, orca=None, dry_run=dry_run, now=NOW)
        rows.append([stage, list(plan_actions), executed, action_ago, dry_run, done, reaps,
                     acted.action if acted else None, state.get("last_action"), state.get("brake_at"),
                     state["swap_history"], state["snapshot"]["last_resort"], state["snapshot"]["acted"]])
    return rows


def safety_table():
    """sched.safety: kogo wstrzymuje (SIGSTOP), kogo wznawia (SIGCONT) i co zostaje w stanie."""
    rows = []
    jobs = {
        "two_heavy": [("a", False, None, 100.0), ("b", False, None, 200.0)],
        "heavy_and_small": [("a", False, None, 100.0), ("s", True, None, 300.0)],
        "one_heavy": [("a", False, None, 100.0)],
        "native_pair": [("a", False, "native", 100.0), ("b", False, None, 50.0)],
        "paused_old": [("a", False, None, 100.0), ("b", False, None, 200.0, NOW - 40)],
        "paused_young": [("a", False, None, 100.0), ("b", False, None, 200.0, NOW - 10)],
        "two_paused": [("a", False, None, 100.0, NOW - 50), ("b", False, None, 200.0, NOW - 35), ("c", False, None, 1.0)],
    }
    for (jname, spec), growth, level, pressure in itertools.product(
        sorted(jobs.items()), (0.0, 0.05, 0.5, 0.6), (20, 24, 25, 29, 30, 50), ("normal", "critical"),
    ):
        running = []
        for i, item in enumerate(spec):
            jid, small, lang, started = item[:4]
            entry = {"id": jid, "where": "local", "child_pgid": 1000 + i, "small": small, "lang": lang,
                     "started_at": NOW - 1000 + started}
            if len(item) > 4:
                entry.update(paused=True, paused_at=item[4], pause_reason="x")
            running.append(entry)
        state = {"running": running, "today": {"pauses": 0},
                 "memory": {"swap_growth_2m_gb": growth, "level_pct": level, "pressure": pressure}}
        sent = []
        with mock.patch.object(S.os, "killpg", lambda pgid, sig: sent.append([pgid, int(sig)])), \
                mock.patch.object(S.time, "time", lambda: NOW):
            S.safety(state, dict(S.DEFAULTS))
        rows.append([jname, growth, level, pressure, sent,
                     [[j["id"], j.get("paused"), j.get("paused_at"), j.get("pause_reason")] for j in running],
                     state["today"]["pauses"]])
    return rows


TABLE_FUNCS = {
    "stage_grid": stage_grid,
    "freeze_replay": freeze_replay,
    "choose": choose_table,
    "reap": reap_table,
    "pressure": pressure_table,
    "brake": brake_table,
    "tick": tick_table,
    "safety": safety_table,
}


def compute():
    """Wszystkie tablice; rejestr, który reap, tick i safety piszą po wydaniu A, idzie do katalogu
    tymczasowego, nigdy do prawdziwego $STATE."""
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dg, "STATE_DIR", tmp), \
            mock.patch.object(S, "STATE_DIR", tmp):
        return {name: summarize(json.loads(json.dumps(func(), default=str))) for name, func in TABLE_FUNCS.items()}


class DecisionReplayTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        with open(GOLDEN) as f:
            cls.golden = json.load(f)
        cls.now = compute()

    def check(self, name):
        got, want = self.now[name], self.golden[name]
        if "table" in want and got["sha256"] != want["sha256"]:
            self.assertEqual(got.get("table"), want["table"], name)
        self.assertEqual((got["rows"], got["sha256"]), (want["rows"], want["sha256"]), name)

    def test_brake_stage_on_every_threshold(self):
        self.check("stage_grid")

    def test_freeze_of_0810_replays_the_same_stages(self):
        self.check("freeze_replay")

    def test_victim_choice_is_unchanged(self):
        self.check("choose")

    def test_reap_kills_logs_and_remembers_the_same(self):
        self.check("reap")

    def test_guard_pressure_level_and_reasons_are_unchanged(self):
        self.check("pressure")

    def test_brake_gate_is_unchanged(self):
        self.check("brake")

    def test_tick_actions_and_swap_window_reset_are_unchanged(self):
        self.check("tick")

    def test_scheduler_pause_and_resume_are_unchanged(self):
        self.check("safety")


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        tables = compute()
        with open(GOLDEN, "w") as f:
            json.dump(tables, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print({name: (t["rows"], t["sha256"][:12]) for name, t in tables.items()})
    else:
        unittest.main()
