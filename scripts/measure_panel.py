"""Pod Menu's open panel, measured with nothing on the user's screen (app/Sources/ClaudeAcc/PanelMeasure.swift).

  python3 -I scripts/measure_panel.py record REC 300          every new version of the live state files, read-only
  python3 -I scripts/measure_panel.py run REC 120 [--store-only | --closed] [--no-writer] [--trace-wakeups] [--bin PATH]

`run` makes a throwaway home in a temp dir: the state files as REC's first versions, a stub acc.py
that answers `status --json` from that copy (no Keychain, no network, no daemons), and a writer that
replays REC's versions at their recorded times, round and round, with their Unix times moved to now
(`--no-writer`: the files stay put). The app runs from a bundle of its own
(com.filip.claude-acc.menubar.measure: no login item, no URL scheme) under CFFIXED_USER_HOME and
prints one line of JSON: CPU ms/s of the app, its main thread and the stub scripts
(`--trace-wakeups`: the main thread's first 400 wakeups go to stderr, time and busy µs).
The temp dir goes to the Trash afterwards; REC holds copies of the user's state files, keep it local.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

STATE = os.path.expanduser("~/.local/share/claude-acc")
# what the panel reads (Store.readLocal, the tick's snapshot, Awake's hotspot state)
FILES = [
    "status.json",
    "janitor-state.json",
    "devguard-state.json",
    "fans-state.json",
    "perf-state.json",
    "sched/state.json",
    "sched/depot.json",
    "updates-state.json",
    "hotspot.json",
    "hotspot-state.json",
    "mail/panel.json",
    "browser/panel.json",
    "desktop/panel.json",
    "awake-state.json",
]
# settings the cards show, copied once
CONFIGS = [
    "devguard.json",
    "fans.json",
    "perf.json",
    "janitor.json",
    "awake.json",
    "config.json",
]
BUNDLE_ID = "com.filip.claude-acc.menubar.measure"
WARMUP = 10

STUB = """import json, os, sys, time
# measure_panel.py's stand-in for the scripts: `accswitch status --json` from the copied tick
# snapshot, made fresh; everything else does nothing
if sys.argv[1:3] == ["accswitch", "status"]:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.json")) as f:
        data = json.load(f)
    data["generated_at"] = time.time()
    print(json.dumps(data))
"""


def record(out, seconds):
    os.makedirs(out, exist_ok=True)
    seen, log, start = {}, [], time.time()
    while time.time() - start < seconds:
        for rel in FILES:
            path = os.path.join(STATE, rel)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            if seen.get(rel) == stamp:
                continue
            seen[rel] = stamp
            copy = f"{rel.replace('/', '__')}.{len(log):05d}"
            try:
                shutil.copyfile(path, os.path.join(out, copy))
            except FileNotFoundError:
                continue
            log.append({"t": round(time.time() - start, 3), "file": rel, "copy": copy})
        time.sleep(0.1)
    with open(os.path.join(out, "log.json"), "w") as f:
        json.dump({"start": start, "seconds": seconds, "versions": log}, f, indent=1)
    print(f"{len(log)} versions in {seconds:.0f} s: {out}")


def replace(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.measure-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def moved(value, by):
    """Every Unix time in a state file (seconds or milliseconds) moved `by` seconds: the cards judge
    freshness by them, and a replayed fan reading from an hour ago reads as no fan control."""
    if isinstance(value, dict):
        return {k: moved(v, by) for k, v in value.items()}
    if isinstance(value, list):
        return [moved(v, by) for v in value]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    for low, high, scale in ((1.6e9, 2.1e9, 1), (1.6e12, 2.1e12, 1000)):
        if low <= value <= high:
            return type(value)(value + by * scale)
    return value


def version(rec, copy, by):
    with open(os.path.join(rec, copy), "rb") as f:
        data = f.read()
    try:
        return json.dumps(moved(json.loads(data), by)).encode()
    except ValueError:
        return data


def latest_time(value):
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return max((latest_time(v) for v in value), default=0)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 1.6e9 <= value <= time.time():
        return value
    return 0


def recording_start(rec, log):
    """When REC began, in Unix time: saved by `record`, or the median of each version's newest time
    less its offset (a writer stamps the moment it writes)."""
    if "start" in log:
        return log["start"]
    guesses = []
    for v in log["versions"]:
        with open(os.path.join(rec, v["copy"]), "rb") as f:
            try:
                newest = latest_time(json.loads(f.read()))
            except ValueError:
                continue
        if newest:
            guesses.append(newest - v["t"])
    return sorted(guesses)[len(guesses) // 2] if guesses else time.time()


def replay(rec, log, state, stop, shift):
    """REC's versions at their recorded offsets, looping over the recording's length, their
    times moved to now."""
    versions = [v for v in log["versions"] if v["t"] > 0.5]
    start = time.monotonic()
    while not stop.is_set():
        for v in versions:
            wait = start + v["t"] - time.monotonic()
            if wait > 0 and stop.wait(wait):
                return
            replace(os.path.join(state, v["file"]), version(rec, v["copy"], shift))
        start += log["seconds"]
        shift += log["seconds"]


def bundle(root, binary):
    app = os.path.join(root, "Pod Menu Measure.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    shutil.copy2(binary, os.path.join(app, "Contents", "MacOS", "PodMenuMeasure"))
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(
            {
                "CFBundleExecutable": "PodMenuMeasure",
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleName": "Pod Menu Measure",
                "CFBundlePackageType": "APPL",
                "LSMinimumSystemVersion": "26.0",
                "LSUIElement": True,
                "NSHighResolutionCapable": True,
            },
            f,
        )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", app], check=True, capture_output=True
    )
    return app


def run(rec, seconds, flags, binary):
    with open(os.path.join(rec, "log.json")) as f:
        log = json.load(f)
    root = tempfile.mkdtemp(prefix="pod-menu-measure.")
    home = os.path.join(root, "home")
    state = os.path.join(home, ".local", "share", "claude-acc")
    os.makedirs(state)
    first = {}
    for v in log["versions"]:
        first.setdefault(v["file"], v["copy"])
    shift = time.time() - recording_start(rec, log)
    for rel, copy in first.items():
        replace(os.path.join(state, rel), version(rec, copy, shift))
    for name in CONFIGS:
        if os.path.exists(os.path.join(STATE, name)):
            shutil.copyfile(os.path.join(STATE, name), os.path.join(state, name))
    with open(os.path.join(state, "acc.py"), "w") as f:
        f.write(STUB)
    python = os.path.realpath(os.path.join(STATE, "python"))
    if os.path.exists(python):
        os.symlink(python, os.path.join(state, "python"))
    app = bundle(root, binary)
    stop = threading.Event()
    writer = threading.Thread(target=replay, args=(rec, log, state, stop, shift), daemon=True)
    if "--no-writer" not in flags:
        writer.start()
    env = {
        "HOME": home,
        "CFFIXED_USER_HOME": home,
        "USER": os.environ.get("USER", ""),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    try:
        done = subprocess.run(
            [
                os.path.join(app, "Contents", "MacOS", "PodMenuMeasure"),
                "--measure-panel",
                str(seconds),
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=WARMUP + seconds + 60,
        )
    finally:
        stop.set()
        if writer.is_alive():
            writer.join()
        lsregister = (
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
            "LaunchServices.framework/Support/lsregister"
        )
        subprocess.run([lsregister, "-u", app], capture_output=True)
        subprocess.run(["trash", root], capture_output=True)
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        sys.exit(f"exit {done.returncode}: {done.stderr.strip()[-2000:]}")
    if len(lines) > 1:
        sys.stderr.write("\n".join(lines[:-1]) + "\n")
    print(lines[-1])


def main(args):
    if len(args) >= 3 and args[0] == "record":
        return record(args[1], float(args[2]))
    if len(args) >= 3 and args[0] == "run":
        binary = (
            args[args.index("--bin") + 1]
            if "--bin" in args
            else os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "app",
                ".build",
                "release",
                "ClaudeAcc",
            )
        )
        flags = [a for a in args[3:] if a in ("--store-only", "--closed", "--no-writer", "--trace-wakeups")]
        return run(args[1], float(args[2]), flags, binary)
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
