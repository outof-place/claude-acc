#!/usr/bin/env python3
"""Parity fixtures for acc-cored's native guard tick: what devguard_core's tick reads and decides.

    devguard_replay.py record <src> --ticks N [--every S] --out F.jsonl
        live ticks of the real Mac, from a copy of devguard-state.json: every reading the tick
        makes is kept in the fixture, with the state, plans and verdict Python produced
    devguard_replay.py fuzz <src> --n N [--seed S] --out F.jsonl
        synthetic Macs (process trees with dev servers, shells, agents, launchers, simulators,
        sockets, host tabs and terminals, memory pressure, histories) through the same tick
    devguard_replay.py check <src> F.jsonl
        re-runs Python on a file's inputs: the expected outputs must come back (harness self-test)

<src> is a claude-acc checkout (or an install's source dir) whose devguard_core is the reference.
Nothing acts: execute and lastresort.reap are replaced by recorders, notifications and the log
are captured, the tick never saves the state. `acc-cored guard-replay F.jsonl` runs the native
tick on every fixture and compares.
"""

import copy
import json
import os
import random
import sys
import time

MB, GB = 1024**2, 1024**3


def load(src):
    sys.path.insert(0, src)
    import devguard_core as dg
    import janitor
    import lastresort

    return dg, janitor, lastresort


class Probe:
    """The tick's readings: recorded from the live functions, or served from a fixture."""

    def __init__(self, dg, janitor, fixture=None):
        self.dg, self.janitor = dg, janitor
        self.serve = fixture is not None
        self.f = fixture or {
            "rows": None, "sockets": None, "usage": {}, "cwd": {}, "argv": {}, "sysctl": {}, "swap": None,
            "json": {}, "start_epoch": {}, "sim_names": {}, "orca_calls": {},
        }  # fmt: skip
        self.missing = []
        self.used = {}
        self.real = {
            "processes": dg.processes, "sockets": dg.sockets, "usage": dg.usage, "proc_cwd": dg.proc_cwd,
            "proc_argv": dg.proc_argv, "sysctl_int": dg.sysctl_int, "swap_usage": dg.swap_usage,
            "proc_start_epoch": dg.proc_start_epoch, "sim_name": dg.sim_name, "load_json": janitor.load_json,
        }  # fmt: skip

    def keyed(self, table, key, live):
        k = str(key)
        if self.serve:
            if k not in self.f[table]:
                self.missing.append(f"{table}:{k}")
                return None
            return self.f[table][k]
        value = live()
        self.f[table][k] = value
        return value

    def install(self):
        dg, j = self.dg, self.janitor
        p = self

        def processes():
            if p.serve:
                return [tuple(r) for r in p.f["rows"]]
            rows = p.real["processes"]()
            p.f["rows"] = [list(r) for r in rows]
            return rows

        def sockets():
            if p.serve:
                s = p.f["sockets"] or {"listen": {}, "links": []}
                return {int(k): set(v) for k, v in s["listen"].items()}, [
                    tuple(x) for x in s["links"]
                ]
            listen, links = p.real["sockets"]()
            p.f["sockets"] = {
                "listen": {str(k): sorted(v) for k, v in listen.items()},
                "links": [list(x) for x in links],
            }
            return listen, links

        def swap_usage():
            if p.serve:
                return tuple(p.f["swap"])
            v = p.real["swap_usage"]()
            p.f["swap"] = list(v)
            return v

        def load_json(path, default):
            # only the files the tick reads; the rest (config) goes to the real reader
            if not any(
                path.endswith(s)
                for s in (
                    "devguard-pins.json",
                    "claude-acc-fsguard.json",
                    "sched/state.json",
                    ".json",
                )
            ):
                return p.real["load_json"](path, default)
            if p.serve:
                if path not in p.f["json"]:
                    return default
                v = p.f["json"][path]
                return default if v is None else copy.deepcopy(v)
            v = p.real["load_json"](path, None)
            p.f["json"][path] = v
            return default if v is None else v

        dg.processes = processes
        dg.sockets = sockets
        def usage(pid):
            # read more than once a tick (server, unit root, inventory), a little apart: kept in order
            k = str(pid)
            if p.serve:
                seq = p.f.get("usage_seq", {}).get(k)
                if seq is not None:
                    i = p.used.get(k, 0)
                    p.used[k] = i + 1
                    return seq[min(i, len(seq) - 1)]
                return p.keyed("usage", pid, None)
            value = p.real["usage"](pid)
            p.f.setdefault("usage_seq", {}).setdefault(k, []).append(value)
            return value

        dg.usage = usage
        dg.proc_cwd = lambda pid: p.keyed("cwd", pid, lambda: p.real["proc_cwd"](pid))
        dg.proc_argv = lambda pid: p.keyed(
            "argv", pid, lambda: p.real["proc_argv"](pid)
        )
        dg.sysctl_int = lambda name: p.keyed(
            "sysctl", name, lambda: p.real["sysctl_int"](name)
        )
        dg.swap_usage = swap_usage
        dg.proc_start_epoch = lambda pid: p.keyed(
            "start_epoch", pid, lambda: p.real["proc_start_epoch"](pid)
        )
        dg.sim_name = lambda udid: p.keyed(
            "sim_names", udid, lambda: p.real["sim_name"](udid)
        )
        j.load_json = load_json


def orca_state(o):
    return {"ok": o.ok, "at": o.at, "sessions_at": o.sessions_at, "tabs": o.tabs, "worktrees": o.worktrees,
            "terminals": o.terminals, "sessions": [[k, v] for k, v in sorted(o.sessions.items())]}  # fmt: skip


def set_orca(o, s):
    o.ok, o.at, o.sessions_at = s["ok"], s["at"], s["sessions_at"]
    o.tabs, o.worktrees, o.terminals = s["tabs"], s["worktrees"], s["terminals"]
    o.sessions = {k: v for k, v in s["sessions"]}


def run_tick(dg, janitor, lastresort, probe, cfg, state, orca, now, absolute, marker, cli=None):
    """One tick with every action replaced by a recorder; returns what Python decided."""
    calls, logs, notes, reaps = [], [], [], []

    def execute(cfg_, plan, world, state_):
        if (
            plan.action == "warn"
            and now - state_.get("warned", {}).get(plan.unit.app_key, 0) < dg.HOUR
        ):
            return False
        calls.append({"unit": plan.unit.key, "action": plan.action, "code": plan.code})
        return False

    def reap(world, state_, module, level, cfg):
        reaps.append(level)

    real = (dg.execute, lastresort.reap, dg.log, janitor.notify, dg.started_at)
    dg.execute = execute
    lastresort.reap = reap
    dg.log = lambda line: logs.append(line)
    janitor.notify = lambda title, text: notes.append([title, text])

    def started_at(abstime, first_seen, now_):
        if not abstime:
            return first_seen
        awake = (absolute - abstime) * dg.TICK_NS / 1e9
        return min(first_seen, now_ - awake)

    dg.started_at = started_at
    orca.marker = marker
    orca.bin = cli or ("/usr/bin/false" if marker else None)

    def call(*args, timeout=10):
        key = " ".join(args)
        if probe.serve:
            if key not in probe.f["orca_calls"]:
                probe.missing.append("orca:" + key)
                return None
            return copy.deepcopy(probe.f["orca_calls"][key])
        v = real_call(orca, *args, timeout=timeout)
        probe.f["orca_calls"][key] = v
        return v

    real_call = type(orca).call
    orca.call = call
    orca.attach = lambda host: None
    try:
        _world, plans, _acted = dg.tick(
            cfg, state, orca, dry_run=False, now=now, qos=False
        )
    finally:
        dg.execute, lastresort.reap, dg.log, janitor.notify, dg.started_at = real
    act = calls[0] if calls else None
    return {
        "plans": [p.summary() for p in plans],
        "act": act,
        "brake": reaps[0] if reaps else None,
        "log": logs,
        "notify": notes,
    }


def record(src, ticks, every, out):
    dg, janitor, lastresort = load(src)
    import orcahost

    cfg = dg.load_config()
    state = janitor.load_json(dg.STATE_PATH, {})
    orca = dg.Orca()
    host = orcahost.host()
    marker = (
        orcahost.main_marker(host) if orcahost.cli_path(host, janitor.which) else None
    )
    real_orca = type(orca)
    with open(out, "w") as f:
        for i in range(ticks):
            probe = Probe(dg, janitor)
            probe.install()
            now, absolute = time.time(), dg._libc.mach_absolute_time()
            before = copy.deepcopy(state)
            orca_before = orca_state(orca)
            # live Orca reads go through the real socket, then the CLI
            cli = orcahost.cli_path(host, janitor.which)
            got = run_tick(dg, janitor, lastresort, probe, cfg, state, orca, now, absolute, marker, cli=cli)
            orca.call = real_orca.call.__get__(orca)
            fixture = dict(probe.f, now=now, absolute=absolute, marker=marker, cfg=cfg, state_before=before,
                           orca_before=orca_before, home=os.path.expanduser("~"), fsguard=dg.FSGUARD_STATE,
                           expect=dict(got, state_after=state, orca_after=orca_state(orca)))  # fmt: skip
            f.write(json.dumps(fixture, ensure_ascii=False) + "\n")
            if i + 1 < ticks:
                time.sleep(every)
    return 0


def serve(dg, janitor, lastresort, fixture):
    """Python's answer to a fixture's inputs."""
    probe = Probe(dg, janitor, fixture)
    probe.install()
    import orcahost  # noqa: F401

    dg.FSGUARD_STATE = fixture["fsguard"]
    orca = dg.Orca()
    set_orca(orca, fixture["orca_before"])
    state = copy.deepcopy(fixture["state_before"])
    cfg = dict(dg.DEFAULT_CONFIG)
    cfg.update(fixture["cfg"])
    got = run_tick(
        dg,
        janitor,
        lastresort,
        probe,
        cfg,
        state,
        orca,
        fixture["now"],
        fixture["absolute"],
        fixture["marker"],
    )
    return dict(got, state_after=state, orca_after=orca_state(orca)), probe.missing


# ---------- fuzz ----------

SERVER_COMMANDS = [
    "node {d}/node_modules/.bin/../next/dist/bin/next dev --turbopack",
    "node {d}/node_modules/next/dist/bin/next dev -p {port}",
    "node {d}/node_modules/.bin/vite",
    "node {d}/node_modules/vite/bin/vite.js dev --port {port}",
    "node {d}/node_modules/.bin/vite --host",
    "node {d}/node_modules/expo/bin/cli start --port {port}",
    "node {d}/node_modules/webpack-cli/bin/cli.js serve",
    "node {d}/node_modules/.bin/astro dev",
    "node {d}/node_modules/storybook/bin/index.cjs dev -p {port}",
    "bun {d}/node_modules/.bin/nuxi dev",
    "deno run -A {d}/node_modules/.bin/vite",
    "node {d}/node_modules/next/dist/bin/next build",
    "node {d}/server.js",
]
LAUNCHERS = ["pnpm dev", "npm run dev", "npx next dev", "yarn dev", "bun run dev", "rtk pnpm dev", "turbo run dev",
             "node /opt/homebrew/lib/node_modules/pnpm/bin/pnpm.cjs dev", "/bin/zsh -c pnpm dev", "sh -c next dev",
             "nohup pnpm dev", "corepack pnpm dev"]  # fmt: skip
SHELLS = ["-zsh", "/bin/zsh -il", "-bash", "zsh", "/opt/homebrew/bin/fish", "sh"]
AGENTS = [
    "claude",
    "/Users/u/.local/bin/claude --resume",
    "codex exec",
    "node /opt/homebrew/bin/codex",
]
OTHER = ["/Applications/Pod.app/Contents/MacOS/Pod", "/Applications/Orca.app/Contents/MacOS/Orca",
         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Safari.app/Contents/MacOS/Safari",
         "/Users/u/Library/Caches/ms-playwright/chromium/chrome --headless", "gopls -mode=stdio", "git status",
         "node /x/tsserver.js", "/Applications/Docker.app/Contents/MacOS/com.docker.backend", "vitest --watch",
         "(launchd)", "<defunct>", "node /x/nodemon app.js", "rust-analyzer", "/usr/libexec/xpcproxy",
         "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser --type=renderer", "Simulator.app/Contents/MacOS/Simulator",
         "node /x/metro start", "zażółć gęślą jaźń  dev", "tab\\011in\\012cmd ^A"]  # fmt: skip
UDIDS = ["0A1B2C3D-0000-4000-8000-00000000000%d" % i for i in range(4)]


def fuzz_fixture(rng, home, i):
    now = 1_791_000_000.0 + rng.random() * 1e6
    absolute = int(5e13 + rng.random() * 1e13)
    rows, usage, cwd, argv = [], {}, {}, {}
    next_pid = [100]

    def add(ppid, command, own=True, tree_cwd=None, fp=None, args=None):
        pid = next_pid[0] = next_pid[0] + rng.randint(1, 40)
        rows.append([pid, ppid, command])
        if own and rng.random() > 0.03:
            big = rng.random() < 0.15
            foot = (
                fp if fp is not None else int((rng.random() * (9 if big else 0.8)) * GB)
            )
            peak = foot + int(rng.random() * GB)
            usage[str(pid)] = {"footprint": foot, "peak": peak, "cpu": round(rng.random() * 500, 6),
                               "written": rng.randint(0, 10**9), "start": absolute - rng.randint(1, 10**12)}  # fmt: skip
        else:
            usage[str(pid)] = None
        cwd[str(pid)] = tree_cwd if rng.random() > 0.05 else None
        argv[str(pid)] = (
            args
            if args is not None
            else (command.split() if rng.random() > 0.1 else None)
        )
        return pid

    host = add(1, OTHER[rng.randrange(2)], tree_cwd="/")
    apps = [f"{home}/Documents/proj{n}" for n in range(rng.randint(1, 4))]
    ports = {}
    links = []
    for _ in range(rng.randint(0, 6)):
        app = rng.choice(apps)
        parent = rng.choice(
            [
                1,
                host,
                add(host, rng.choice(SHELLS), tree_cwd=app),
                add(1, rng.choice(AGENTS), tree_cwd=app),
            ]
        )
        if rng.random() < 0.6:
            parent = add(parent, rng.choice(LAUNCHERS), tree_cwd=app)
        if rng.random() < 0.3:
            parent = add(parent, rng.choice(LAUNCHERS), tree_cwd=app)
        port = rng.choice([3000, 3001, 5173, 8081, 6006, 4321, 3000])
        server = add(
            parent, rng.choice(SERVER_COMMANDS).format(d=app, port=port), tree_cwd=app
        )
        for _ in range(rng.randint(0, 3)):
            child = add(server, rng.choice(["node " + app + "/node_modules/next/dist/server/lib/start-server.js",
                                            "next-server (v16)", rng.choice(SERVER_COMMANDS).format(d=app, port=port)]), tree_cwd=app)  # fmt: skip
            if rng.random() < 0.5:
                ports.setdefault(str(port + rng.randint(0, 2)), []).append(child)
        if rng.random() < 0.8:
            ports.setdefault(str(port), []).append(server)
        for _ in range(rng.randint(0, 3)):
            client = add(rng.choice([1, host]), rng.choice(OTHER), tree_cwd="/")
            links.append([client, port])
    for _ in range(rng.randint(5, 30)):
        add(
            rng.choice([1, host] + [r[0] for r in rows]),
            rng.choice(OTHER + SHELLS + AGENTS + LAUNCHERS),
            own=rng.random() > 0.2,
        )
    sims = {}
    for u in rng.sample(UDIDS, rng.randint(0, 3)):
        root = add(
            1,
            f"/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Resources/bin/launchd_sim {home}/Library/Developer/CoreSimulator/Devices/{u}/data/var/run/launchd_bootstrap.plist",
        )
        for _ in range(rng.randint(0, 4)):
            add(
                root,
                f"{home}/Library/Developer/CoreSimulator/Devices/{u}/data/Containers/Bundle/Application/X/App.app/App",
            )
        sims[u] = rng.choice(["Portivo-1", "Portivo-Perf-2", "iPhone 17", u])
        if rng.random() < 0.3:
            add(
                host,
                f"serve-sim --udid {u}"
                if rng.random() < 0.5
                else "xcrun simctl io booted recordVideo x.mov",
            )
    rng.shuffle(rows)
    rows.sort(key=lambda r: (rng.choice([-1, -1, 3]), r[0]))
    ram = rng.choice([16, 24, 36, 48, 64]) * GB
    swap_used = int(rng.random() * 0.4 * ram)
    sysctl = {
        "hw.memsize": ram, "kern.memorystatus_vm_pressure_level": rng.choice([1, 1, 1, 2, 4, 0]),
        "kern.memorystatus_level": rng.choice([5, 9, 15, 20, 35, 80, None]),
        "vm.compressor_bytes_used": int(rng.random() * 0.7 * ram),
        "vm.compressor.segment.total": rng.randint(0, 900_000), "vm.compressor.segment.limit": rng.choice([1_677_721, 0, None]),
        "vm.compressor.compactor.swapouts_queued_pressure": rng.choice([None, rng.randint(0, 5000)]),
    }  # fmt: skip
    units_hist = {}
    for r in rows:
        if rng.random() < 0.3:
            units_hist[f"{r[0]}:{(usage.get(str(r[0])) or {}).get('start', 0)}"] = {
                "first": now - rng.random() * 7200, "cpu": rng.random() * 400, "at": now - rng.random() * 10,
                "busy": now - rng.random() * 5400, "watched": now - rng.random() * 5400}  # fmt: skip
    state = {
        "swap_history": [[now - rng.random() * 200, swap_used - rng.randint(-GB, GB), rng.choice([None, rng.randint(0, 3000)])]
                         for _ in range(rng.randint(0, 6))],
        "units": units_hist,
        "recycles": [[now - rng.random() * 5000, rng.choice(apps)] for _ in range(rng.randint(0, 4))],
        "warned": {rng.choice(apps): now - rng.random() * 7200},
        "last_action": now - rng.random() * 200, "brake_at": now - rng.random() * 60,
        "pending": [{"app": rng.choice(apps), "at": now - rng.random() * 120, "label": ":3000 ~/proj0", "command": "pnpm dev"}
                    for _ in range(rng.randint(0, 2))],
        "inventory": {"at": now - rng.random() * 60, "biggest": [1, rng.randint(0, ram), "x"]},
        "history": [[int(now - 40), 0, 0, 0]] if rng.random() < 0.5 else [],
        "snapshot": {"pressure": {"stage": rng.choice([0, 0, 1, 2, 3])}},
        "sims": {f"{u}:0": {"first": now - 3000, "cpu": 1.0, "at": now - 5, "busy": now - rng.random() * 4000} for u in sims},
    }  # fmt: skip
    cfg = {}
    for key, choices in (("budget_percent", [25, 35, 10]), ("max_server_gb", [4, 5, 2.5]), ("grace_minutes", [3, 0]),
                         ("idle_minutes", [45, 1]), ("orphan_minutes", [10, 0]), ("duplicate_minutes", [5, 0]),
                         ("quiet_seconds", [30, 0]), ("max_booted_simulators", [2, 1, 0]), ("kernel_pressure", [True, False]),
                         ("protect", [[], [apps[0]], [":3000"], ["3001", ":x"]]), ("scope", [[], [apps[-1]]]),
                         ("simulator_idle_minutes", [30, 0]), ("simulator_quiet_minutes", [5, 0]), ("mode", ["enforce", "observe"])):  # fmt: skip
        if rng.random() < 0.4:
            cfg[key] = rng.choice(choices)
    tabs = [{"url": f"http://localhost:{rng.choice([3000, 5173, 8081])}/x", "worktreeId": rng.choice(["w1", "w2"]),
             "active": rng.random() < 0.5} for _ in range(rng.randint(0, 3))]  # fmt: skip
    worktrees = [{"worktreeId": w, "path": rng.choice(apps), "isActive": rng.random() < 0.5,
                  "status": rng.choice(["working", "idle"]), "agents": [{"state": rng.choice(["working", "idle"])}]} for w in ("w1", "w2")]  # fmt: skip
    shells = [r[0] for r in rows if r[2] in SHELLS]
    terminals = [{"ptyId": f"p{n}", "title": f"t{n}", "agentIdentity": rng.choice([None, "claude"]),
                  "lastOutputAt": (now - rng.random() * 600) * 1000} for n in range(len(shells))]  # fmt: skip
    memory = {
        "worktrees": [
            {
                "sessions": [
                    {"pid": s, "sessionId": f"p{n}"} for n, s in enumerate(shells)
                ]
            }
        ]
    }
    marker = "Pod.app/Contents/MacOS/Pod" if rng.random() < 0.8 else None
    ok = rng.random() < 0.85
    orca_calls = {
        "tab list --worktree all": {"tabs": tabs} if ok else None, "worktree ps": {"worktrees": worktrees},
        "terminal list": {"terminals": terminals}, "diagnostics memory": memory,
    }  # fmt: skip
    leases = {}
    for u in sims:
        if rng.random() < 0.5:
            leases[f"{home}/.cache/portivo-mobile/leases/{u}.json"] = {
                "owner": {
                    "pid": rng.choice([rows[0][0], 999999]),
                    "start": rng.choice(["", "Sat Oct 10 12:00:00 2026"]),
                    "session": "s",
                },
                "app": "a",
            }
    json_files = {f"{home}/.local/share/claude-acc/devguard-pins.json": {"pins": [{"target": rng.choice([":3000", apps[0]]), "until": rng.choice([None, now + 100, now - 100]), "level": rng.choice(["hold", None])}]} if rng.random() < 0.3 else None,
                  "/var/db/claude-acc-fsguard.json": {"last_restart": now - rng.random() * 9000} if rng.random() < 0.3 else None,
                  f"{home}/.local/share/claude-acc/sched/state.json": {"running": [{"child_pgid": rng.choice([r[0] for r in rows])}]} if rng.random() < 0.3 else None}  # fmt: skip
    json_files.update(leases)
    return {
        "now": now, "absolute": absolute, "marker": marker, "cfg": cfg, "state_before": state, "home": home,
        "fsguard": "/var/db/claude-acc-fsguard.json",
        "orca_before": {"ok": False, "at": rng.choice([0, now - 5, now - 20]), "sessions_at": rng.choice([0, now - 30, now - 90]),
                        "tabs": [], "worktrees": [], "terminals": [], "sessions": []},
        "rows": rows, "sockets": {"listen": {k: sorted(v) for k, v in ports.items()}, "links": links}, "usage": usage, "cwd": cwd,
        "argv": argv, "sysctl": sysctl, "swap": [ram // 2, swap_used], "json": json_files,
        "start_epoch": {str(r[0]): int(now - rng.random() * 9000) for r in rows}, "sim_names": sims, "orca_calls": orca_calls,
    }  # fmt: skip


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, src, rest = argv[0], argv[1], argv[2:]

    def opt(name, default):
        return type(default)(rest[rest.index(name) + 1]) if name in rest else default

    if cmd == "record":
        return record(
            src,
            opt("--ticks", 10),
            opt("--every", 5.0),
            opt("--out", "devguard-live.jsonl"),
        )
    dg, janitor, lastresort = load(src)
    if cmd == "fuzz":
        rng = random.Random(opt("--seed", 1))
        home = os.path.expanduser("~")
        bad = 0
        with open(opt("--out", "devguard-fuzz.jsonl"), "w") as f:
            for i in range(opt("--n", 500)):
                fixture = fuzz_fixture(rng, home, i)
                try:
                    expect, missing = serve(dg, janitor, lastresort, fixture)
                except Exception:  # noqa: BLE001 - a fixture Python itself can't tick is dropped
                    bad += 1
                    continue
                fixture["expect"] = expect
                f.write(json.dumps(fixture, ensure_ascii=False) + "\n")
        print(f"fixtures: {opt('--n', 500) - bad}, dropped (Python raised): {bad}")
        return 0
    if cmd == "check":
        same = diff = 0
        for line in open(rest[0]):
            fixture = json.loads(line)
            expect, missing = serve(dg, janitor, lastresort, fixture)
            if json.loads(json.dumps(expect)) == fixture["expect"]:
                same += 1
            else:
                diff += 1
        print(f"same {same}, different {diff}")
        return 1 if diff else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
