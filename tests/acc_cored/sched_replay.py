#!/usr/bin/env python3
"""Parity fixtures for acc-cored's scheduler housekeeping: sched.py's reap, refresh_memory, safety,
plan and update_queue_view, run in that order on one state.

    sched_replay.py record <src> --ticks N [--every S] --out F.jsonl
        the live state.json and the live readings (memory, the guard's snapshot, native builds,
        which wrappers live), read once per tick; nothing is written and nobody is signalled
    sched_replay.py fuzz <src> --n N [--seed S] --out F.jsonl
        synthetic queues and running jobs (local and Depot, small, native, paused, stalled, passed
        heads, starving heads, simulators), memory, snapshots and configs

`acc-cored sched-replay F.jsonl` runs the native pass on each fixture and compares the state, the
admissions and the signals with Python's.
"""

import copy
import json
import os
import random
import sys
import tempfile
import time

GB = 1024**3


def load(src):
    sys.path.insert(0, src)
    import sched as S

    return S


def serve(S, fixture):
    """sched.py's pass on a fixture's inputs: (state after, admitted, kill calls)."""
    now = fixture["now"]
    kills = []
    real = (S.probe_memory, S.devguard_snapshot, S.native_scan, S.alive, S.descendants, S.os.killpg, S.time.time,
            S.time.strftime, S.DEVGUARD_CONFIG)  # fmt: skip
    alive = set(fixture["alive"])
    desc = {int(k): set(v) for k, v in fixture.get("descendants", {}).items()}
    tmp = None
    try:
        S.probe_memory = lambda: copy.deepcopy(fixture["memory"])
        S.devguard_snapshot = lambda: copy.deepcopy(fixture["snapshot"])
        scan = fixture.get("native_scan") or {"gb": 0.0, "active": False, "pids": []}
        S.native_scan = lambda *a, **k: {
            "gb": scan["gb"],
            "active": scan["active"],
            "pids": set(scan["pids"]),
        }
        S.alive = lambda pid: bool(pid) and pid in alive
        S.descendants = lambda pid: desc.get(pid, {pid})

        def killpg(pgid, sig):
            kills.append([pgid, int(sig)])
            if fixture.get("killpg_fails"):
                raise OSError("no such group")

        S.os.killpg = killpg
        S.time.time = lambda: now
        S.time.strftime = lambda fmt, *a: (
            fixture["day"] if fmt == "%Y-%m-%d" and not a else real[7](fmt, *a)
        )
        if fixture.get("max_server_gb") is not None:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump({"max_server_gb": fixture["max_server_gb"]}, tmp)
            tmp.close()
            S.DEVGUARD_CONFIG = tmp.name
        else:
            S.DEVGUARD_CONFIG = "/nonexistent/devguard.json"
        cfg = dict(S.DEFAULTS)
        cfg.update(fixture["cfg"])
        state = copy.deepcopy(fixture["state_before"])
        S.reap(state)
        S.refresh_memory(state, cfg)
        S.safety(state, cfg)
        admitted = S.plan(state, cfg, now)
        S.update_queue_view(state, cfg)
    finally:
        (S.probe_memory, S.devguard_snapshot, S.native_scan, S.alive, S.descendants, S.os.killpg, S.time.time,
         S.time.strftime, S.DEVGUARD_CONFIG) = real  # fmt: skip
        if tmp:
            os.unlink(tmp.name)
    return {
        "state_after": state,
        "admitted": [[k, v[0], v[1]] for k, v in admitted.items()],
        "kills": kills,
    }


def record(src, ticks, every, out):
    S = load(src)
    with open(out, "w") as f:
        for i in range(ticks):
            cfg = S.load_config()
            state = S.load_state(cfg)
            pids = {j.get("pid") for j in state["running"] + state["queue"]} | {
                j.get("child_pgid") for j in state["running"]
            }
            pids = {p for p in pids if p}
            scan = S.native_scan()
            try:
                with open(S.DEVGUARD_CONFIG) as g:
                    max_server = json.load(g).get("max_server_gb")
            except (OSError, ValueError, AttributeError):
                max_server = None
            fixture = {
                "now": time.time(), "day": time.strftime("%Y-%m-%d"), "cfg": {k: v for k, v in cfg.items() if k in S.DEFAULTS},
                "state_before": state, "memory": S.probe_memory(), "snapshot": S.devguard_snapshot(),
                "native_scan": {"gb": scan["gb"], "active": scan["active"], "pids": sorted(scan["pids"])},
                "alive": sorted(p for p in pids if S.alive(p)),
                "descendants": {str(j["pid"]): sorted(S.descendants(j["pid"])) for j in state["running"] if j.get("pid")},
                "max_server_gb": max_server,
            }  # fmt: skip
            fixture["expect"] = serve(S, fixture)
            f.write(json.dumps(fixture, ensure_ascii=False) + "\n")
            if i + 1 < ticks:
                time.sleep(every)
    return 0


def fuzz_fixture(rng):
    now = 1_791_000_000.0 + rng.random() * 1e6
    ids = iter(range(1000, 2000))
    pid = iter(range(5000, 9000, 7))

    def job(running):
        jid = f"j{next(ids)}"
        lang = rng.choice([None, None, None, "native", "generic"])
        j = {
            "id": jid, "pid": next(pid), "label": rng.choice(["go test ./...", "pnpm tc", "next build", "xcodebuild", "pytest"]),
            "mem_predicted_gb": round(rng.choice([0.1, 0.5, 1.2, 2.0, 4.5, 6.0, 11.7, 14.8]) * rng.random() * 2, 2),
            "predicted_wall_s": rng.choice([None, 5, 30, 60, 120, 300, 1800]),
            "predicted_from": rng.choice(["history:class", "history", "prior", "family", None]),
            "small": rng.random() < 0.4, "lang": lang, "exclusive": lang == "native" and rng.random() < 0.6,
            "enqueued_at": now - rng.random() * 900, "class": "go:test", "route": {"choice": rng.choice(["local", "local", "depot"])},
        }  # fmt: skip
        if lang == "native":
            j["native_tool"] = rng.choice(
                ["xcodebuild", "portivo-mobile", "simulator", "expo-run"]
            )
            if rng.random() < 0.3:
                j["sim_lease"] = "lease"
        if rng.random() < 0.1:
            j["outside"] = True
        if running:
            j.update(where=rng.choice(["local", "local", "local", "depot"]), started_at=now - rng.random() * 2400,
                     mem_now_gb=rng.choice([0.0, round(rng.random() * 8, 2)]), mem_peak_gb=round(rng.random() * 9, 2),
                     paused=rng.random() < 0.15, stalled_s=rng.choice([None, None, None, 650]),
                     child_pgid=rng.choice([None, next(pid)]))  # fmt: skip
            if rng.random() < 0.2:
                j["native_done"] = True
            if rng.random() < 0.2:
                j["paused_at"] = now - rng.random() * 120
        return j

    running = [job(True) for _ in range(rng.randint(0, 5))]
    queue = [job(False) for _ in range(rng.randint(0, 7))]
    for r in running:
        if queue and rng.random() < 0.3:
            r["passed"] = rng.choice(queue)["id"]
    alive = [j["pid"] for j in running + queue if rng.random() < 0.9] + [
        r["child_pgid"] for r in running if r.get("child_pgid") and rng.random() < 0.7
    ]
    ram = rng.choice([16.0, 24.0, 48.0, 64.0])
    state = {
        "version": 1, "updated_at": now - 1, "idle_since": None, "host": {"ram_gb": ram},
        "config": {}, "memory": {}, "running": running, "queue": queue, "overtakes": [], "recent": [],
        "today": {"date": "2026-10-10", "jobs_local": 0, "max_reserved_gb": rng.choice([0, 3.2]), "peak_concurrency": rng.randint(0, 6), "pauses": rng.randint(0, 3)},
        "_internal": {
            "swap": [[now - rng.random() * 200, round(rng.random() * 9, 2)] for _ in range(rng.randint(0, 5))],
            "avail": [[rng.choice(["2026-10-09", "2026-10-10"]), round(rng.random() * ram, 2), now - rng.random() * 86400 * 8] for _ in range(rng.randint(0, 9))],
            "native": rng.choice([{}, {"at": now - rng.random() * 10, "gb": round(rng.random() * 9, 2), "active": rng.random() < 0.5,
                                      "in_job": rng.choice([None] + [r["id"] for r in running])}]),
            "native_peaks": [round(rng.random() * 14, 2) for _ in range(rng.randint(0, 5))],
            "old_lock": {"wait_s": 0.0, "free_at": 0.0, "pending": []},
        },
    }  # fmt: skip
    snap_at = now - rng.choice([1, 20, 45, 100, 200])
    sims = [{"name": rng.choice(["Portivo-1", "iPhone"]), "udid": f"U{i}", "in_use": rng.random() < 0.6, "pool": rng.random() < 0.7,
             "protected": rng.random() < 0.2, "footprint": rng.randint(0, 3 * GB)} for i in range(rng.randint(0, 3))]  # fmt: skip
    snapshot = rng.choice([{}, {
        "at": snap_at, "pressure": {"level": rng.choice([0, 1, 2]), "stage": rng.choice([0, 1, 2, 3])},
        "budget": rng.random() * 20 * GB, "total": rng.random() * 20 * GB,
        "units": [{"protected": rng.random() < 0.5, "regrow": rng.randint(0, 6 * GB)} for _ in range(rng.randint(0, 3))],
        "simulators": sims, "simulator_cap": rng.choice([0, 1, 2]),
        "inventory": {"families": {f: {"footprint": rng.randint(0, 9 * GB), "count": rng.randint(1, 9)} for f in rng.sample(
            ["dev", "metro", "watchers", "simulators", "headless", "lsp", "docker", "git", "rest"], rng.randint(0, 6))},
            "long_lived": rng.choice([None, rng.randint(0, 20 * GB)])},
    }])  # fmt: skip
    memory = {"level": float(rng.choice([5, 18, 24, 31, 50, 77])), "ram_gb": ram, "swap_gb": round(rng.random() * 12, 2),
              "pressure": rng.choice(["normal", "normal", "warn", "critical"])}  # fmt: skip
    if rng.random() < 0.2:
        memory["native"] = {
            "gb": round(rng.random() * 9, 2),
            "active": rng.random() < 0.5,
        }
    cfg = {}
    for key, choices in (("headroom_gb", [2.0, 4.0]), ("small_gb", [4.5, 1.0]), ("starve_s", [120, 30]), ("aging_s", [300, 60]),
                         ("head_delay_s", [60, 600]), ("pause_swap_gb", [0.5, 0.1]), ("idle_floor_pct", [65, 40])):  # fmt: skip
        if rng.random() < 0.4:
            cfg[key] = rng.choice(choices)
    scan_pids = [r["pid"] for r in running if rng.random() < 0.3]
    return {
        "now": now, "day": rng.choice(["2026-10-09", "2026-10-10"]), "cfg": cfg, "state_before": state, "memory": memory,
        "snapshot": snapshot, "native_scan": {"gb": round(rng.random() * 9, 3), "active": rng.random() < 0.3, "pids": scan_pids},
        "alive": alive, "descendants": {str(r["pid"]): [r["pid"]] + scan_pids[:1] for r in running if rng.random() < 0.5},
        "max_server_gb": rng.choice([None, 4, 5.5]), "killpg_fails": rng.random() < 0.1,
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
            opt("--out", "sched-live.jsonl"),
        )
    if cmd == "fuzz":
        S = load(src)
        rng = random.Random(opt("--seed", 1))
        n, bad = opt("--n", 500), 0
        with open(opt("--out", "sched-fuzz.jsonl"), "w") as f:
            for _ in range(n):
                fixture = fuzz_fixture(rng)
                try:
                    fixture["expect"] = serve(S, fixture)
                except Exception:  # noqa: BLE001 - a state Python itself can't pass through is dropped
                    bad += 1
                    continue
                f.write(json.dumps(fixture, ensure_ascii=False) + "\n")
        print(f"fixtures: {n - bad}, dropped (Python raised): {bad}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
