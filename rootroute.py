#!/usr/bin/env python3
"""perf-root.sh and janitor-root.sh through Pod's root helper (docs/pod-rootd.md).

In Pod, `claude-acc perf-root ...` and `claude-acc mac root-clean ...` come here before the root
copy (root-run.sh under sudo): the same commands, done by `pod-rootctl` as the user. Its tier B
verbs ask for Touch ID once, then 5 minutes for this process. The benches and perf.py's record of
root tweaks run as the user, as perf-root.sh ran them through `sudo -u`. Exit 75 when Pod doesn't
own claude-acc or the helper doesn't answer: the wrapper then goes on to the root copy.

  rootroute.py perf-root [--dry-run] <perf-root.sh command>
  rootroute.py janitor-root [--dry-run] [--high-power]
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(os.path.expanduser("~"), ".local", "share", "claude-acc")
ROOTCTL = os.path.join(STATE, "pod-rootctl")
VNODES = 786432
DIAGNOSTIC_DAYS = 30
# the wrapper's cue to go on to root-run.sh (EX_TEMPFAIL)
FALLBACK = 75


class Route:
    def __init__(self, dry=False):
        self.dry = dry

    def rootctl(self, *args):
        """The helper's reply as a dict; None after a refusal or a failure (pod-rootctl said why)."""
        if self.dry:
            print("  (dry-run) pod-rootctl " + " ".join(args))
            return {"outcome": {"done": {"changed": False}}, "status": self.status() or {}}
        done = subprocess.run([ROOTCTL, *args, "--json"], stdout=subprocess.PIPE, text=True)
        try:
            reply = json.loads(done.stdout or "{}")
        except ValueError:
            reply = {}
        if done.returncode != 0 or "done" not in reply.get("outcome", {}):
            return None
        return reply

    def status(self):
        done = subprocess.run([ROOTCTL, "status", "--json"], stdout=subprocess.PIPE, text=True)
        try:
            return json.loads(done.stdout).get("status") if done.returncode == 0 else None
        except ValueError:
            return None

    def perf(self, *args, capture=False):
        """perf.py next to this file, as the user."""
        if self.dry and args and args[0] == "record":
            return ""
        done = subprocess.run([sys.executable, os.path.join(HERE, "perf.py"), *args],
                              stdout=subprocess.PIPE if capture else None, text=True)
        if capture:
            return done.stdout if done.returncode == 0 else None
        return done.returncode


def kbps(rate):
    """"27Mbps", "27.5 Mbps", "650Kbps", "1Gbps" in kb/s; None for anything else."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMG])bps\s*", rate or "")
    if not m:
        return None
    return int(round(float(m.group(1)) * {"K": 1, "M": 1000, "G": 1000000}[m.group(2)]))


def mbps(value):
    """kb/s as perf-root.sh wrote a previous limit: "27.00Mbps"; "" for none."""
    return "%.2fMbps" % (value / 1000) if value else ""


def sysctl(name):
    done = subprocess.run(["/usr/sbin/sysctl", "-n", name], stdout=subprocess.PIPE, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def source():
    """Where claude-acc was installed from: perf-root.sh lives there, not in $STATE."""
    try:
        with open(os.path.join(STATE, "source")) as f:
            return f.read().strip() or HERE
    except OSError:
        return HERE


def default_interface():
    out = subprocess.run(["/sbin/route", "-n", "get", "default"], stdout=subprocess.PIPE, text=True).stdout
    return next((line.split(":", 1)[1].strip() for line in out.splitlines() if line.strip().startswith("interface:")), "")


def option(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            value = args[i + 1]
            del args[i:i + 2]
            return value
    return default


def flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def applied(route, name):
    """The detail perf.py keeps for a root tweak ("en0 27Mbps prev=..."), or None."""
    out = route.perf("status", "--json", capture=True)
    try:
        tweaks = json.loads(out or "{}").get("tweaks", [])
    except ValueError:
        return None
    return next((t.get("detail") for t in tweaks if t.get("name") == name and t.get("applied")), None)


# --- perf-root ------------------------------------------------------------------------------------

def shaper_apply(route, iface, rate):
    if not iface or not rate:
        found = route.perf("shaper-rate", capture=True)
        if found and len(found.split()) == 2:
            iface = iface or found.split()[0]
            rate = rate or found.split()[1]
        elif rate:
            iface = iface or default_interface()
        else:
            print("give --rate, or measure the network first: perf.py bench network", file=sys.stderr)
            return 1
    limit = kbps(rate)
    if limit is None:
        print("the rate is like 27Mbps, not: %s" % rate, file=sys.stderr)
        return 2
    print("upload limit: %s %s" % (iface, rate))
    reply = route.rootctl("shaper", "set", iface, str(limit))
    if reply is None:
        return 1
    shaper = next((s for s in (reply.get("status") or {}).get("shapers", []) if s.get("interface") == iface), {})
    route.perf("record", "shaper", iface, rate, "prev=" + mbps(shaper.get("previousKbps")))
    return 0


def shaper_undo(route):
    detail = applied(route, "shaper")
    iface = detail.split()[0] if detail else default_interface()
    print("upload limit off on %s" % iface)
    if route.rootctl("shaper", "clear", iface) is None:
        return 1
    route.perf("record", "shaper", "--forget")
    return 0


def summary(before, after, rows, key, headers):
    print("%-36s %10s %10s" % ("", headers[0], headers[1]))
    for label, field in rows:
        print("%-36s %10s %10s" % (label, before.get(key, {}).get(field), after.get(key, {}).get(field)))


def bench(route, kind):
    if route.dry:
        return {}
    try:
        return json.loads(route.perf("bench", kind, "--json", capture=True) or "{}")
    except ValueError:
        return {}


def trial(route, iface, rate, keep):
    print("1/3 a measurement without the limit")
    before = bench(route, "network")
    print("2/3 the limit")
    rc = shaper_apply(route, iface, rate)
    if rc:
        return rc
    try:
        print("3/3 a measurement with the limit")
        after = bench(route, "network")
    finally:
        if not keep:
            shaper_undo(route)
    if keep:
        print("the limit stays; to take it off: claude-acc perf-root shaper undo")
    summary(before, after, [("download Mb/s", "down_mbps"), ("upload Mb/s", "up_mbps"), ("idle ms", "idle_ms"),
                            ("network while downloading p90 ms", "down_net_p90_ms"),
                            ("network while uploading p90 ms", "up_net_p90_ms"),
                            ("responsiveness downloading ms", "down_loaded_ms"),
                            ("responsiveness uploading ms", "up_loaded_ms")], "network", ("without", "limited"))
    return 0


def vnodes_apply(route, value, persist):
    before = sysctl("kern.maxvnodes")
    print("kern.maxvnodes: %s -> %d" % (before, value))
    args = ["sysctl", "set", "maxvnodes", str(value)] + (["--persist"] if persist else [])
    if route.rootctl(*args) is None:
        return 1
    route.perf("record", "vnodes", str(value), "prev=" + before)
    return 0


def vnodes_undo(route):
    print("kern.maxvnodes back to the value from before")
    print("  (the kernel doesn't give vnodes back: the memory returns only with a restart)")
    if route.rootctl("sysctl", "reset", "maxvnodes") is None:
        return 1
    route.perf("record", "vnodes", "--forget")
    return 0


def vnodes_trial(route, value, persist, keep):
    print("1/3 lstat of the module tree with the cache as it is")
    before = bench(route, "fs")
    previous = sysctl("kern.maxvnodes")
    print("2/3 a bigger vnode cache")
    rc = vnodes_apply(route, value, persist)
    if rc:
        return rc
    try:
        print("3/3 lstat of the module tree with the bigger cache")
        after = bench(route, "fs")
    finally:
        if not keep:
            vnodes_undo(route)
    if keep and not route.dry:
        warm = (before.get("fs", {}).get("warm_s"), after.get("fs", {}).get("warm_s"))
        if None not in warm:
            route.perf("record", "vnodes", str(value), "prev=" + previous, "--result", str(warm[0]), str(warm[1]))
    summary(before, after, [("second lstat pass s", "warm_s"), ("recycled vnodes", "warm_recycled"),
                            ("kern.maxvnodes", "maxvnodes")], "fs", ("before", "after"))
    return 0


def iogpu_set(route, value):
    mb = value if value is not None else (route.perf("iogpu-default", capture=True) or "").strip()
    if not mb.isdigit():
        print("the limit is a number of MB, not: %s" % mb, file=sys.stderr)
        return 2
    if int(mb) == 0:
        print("with this much RAM the macOS default is best; nothing changes")
        return 0
    before = sysctl("iogpu.wired_limit_mb")
    print("iogpu.wired_limit_mb: %s -> %s (also after a restart)" % (before, mb))
    if route.rootctl("sysctl", "set", "gpu-wired-limit-mb", mb, "--persist") is None:
        return 1
    route.perf("record", "iogpu", mb, "prev=" + before)
    return 0


def iogpu_undo(route):
    print("iogpu.wired_limit_mb back to the macOS default")
    if route.rootctl("sysctl", "reset", "gpu-wired-limit-mb") is None:
        return 1
    route.perf("record", "iogpu", "--forget")
    return 0


def iogpu_status(route):
    status = route.status() or {}
    gpu = next((s for s in status.get("sysctls", []) if s.get("key") == "iogpu.wired_limit_mb"), {})
    now = gpu.get("current", sysctl("iogpu.wired_limit_mb"))
    print("iogpu.wired_limit_mb: %s" % ("0 (the macOS default, about 2/3 of RAM)" if str(now) == "0" else "%s MB" % now))
    persisted = gpu.get("persisted")
    print("after a restart: %s" % ("%s MB (Pod's root helper)" % persisted if persisted else "the default"))
    want = (route.perf("iogpu-default", capture=True) or "0").strip()
    if want.isdigit() and int(want) > 0:
        print("for this Mac: %s MB (claude-acc perf-root iogpu set)" % want)
    else:
        print("for this Mac: keep the default (with this much RAM there is nothing to raise)")
    return 0


def spotlight(route, mode):
    if mode == "apps-only":
        if route.rootctl("spotlight", "apps-only") is None:
            return 1
        route.perf("record", "spotlight", "apps-only")
        print("Spotlight indexes only applications; to undo: claude-acc perf-root spotlight undo")
    else:
        if route.rootctl("spotlight", "restore") is None:
            return 1
        route.perf("record", "spotlight", "--forget")
        print("the Spotlight Privacy list from before is back")
    return 0


def perf_root(args):
    route = Route(dry=flag(args, "--dry-run"))
    keep = flag(args, "--keep")
    persist = flag(args, "--persist")
    rate = option(args, "--rate")
    iface = option(args, "--if")
    value = option(args, "--value")
    if value is not None and not value.isdigit():
        print("--value is a number", file=sys.stderr)
        return 2
    vnodes = int(value) if value is not None else VNODES
    command = (args + ["", ""])[:2]
    if command[0] in ("devtools", "user") or command == ["shaper", "status"] or command == ["shaper", ""]:
        # nothing here needs root: perf-root.sh as it is, without sudo
        os.execv("/bin/bash", ["/bin/bash", os.path.join(source(), "perf-root.sh")] + args)
    handlers = {
        ("shaper", "apply"): lambda: shaper_apply(route, iface, rate),
        ("shaper", "undo"): lambda: shaper_undo(route),
        ("trial", ""): lambda: trial(route, iface, rate, keep),
        ("vnodes", "apply"): lambda: vnodes_apply(route, vnodes, persist),
        ("vnodes", "undo"): lambda: vnodes_undo(route),
        ("vnodes", "trial"): lambda: vnodes_trial(route, vnodes, persist, keep),
        ("iogpu", "set"): lambda: iogpu_set(route, args[2] if len(args) > 2 else None),
        ("iogpu", "undo"): lambda: iogpu_undo(route),
        ("iogpu", "status"): lambda: iogpu_status(route),
        ("iogpu", ""): lambda: iogpu_status(route),
        ("spotlight", "apps-only"): lambda: spotlight(route, "apps-only"),
        ("spotlight", "undo"): lambda: spotlight(route, "undo"),
    }
    handler = handlers.get(tuple(command))
    if handler is None:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return handler()


# --- janitor-root ---------------------------------------------------------------------------------

def janitor_root(args):
    route = Route(dry=flag(args, "--dry-run"))
    high_power = flag(args, "--high-power")
    if args:
        print("unknown option: %s" % args[0], file=sys.stderr)
        return 2
    dry = ["--dry-run"] if route.dry else []
    rc = 0
    # launchd plists whose program went with its app: parked next to the folder, not deleted
    reply = route.rootctl("launchd", "park-orphans", *dry)
    if reply is None:
        rc = 1
    else:
        for orphan in (reply["outcome"]["done"].get("report") or {}).get("orphans", {}).get("_0", []):
            print("plist without its program: %s -> %s" % (orphan.get("plist"), orphan.get("program")))
    # crash and hang reports older than a month
    reply = route.rootctl("logs", "prune", "--days", str(DIAGNOSTIC_DAYS), *dry)
    if reply is None:
        rc = 1
    else:
        pruned = (reply["outcome"]["done"].get("report") or {}).get("pruned", {})
        if pruned.get("files"):
            print("old reports in /Library/Logs/DiagnosticReports: %d" % pruned["files"])
    # high power mode only on the charger: on battery it stays automatic
    if high_power:
        status = route.status() or {}
        if status.get("power", {}).get("highPowerCapable"):
            print("power mode on the charger: high")
            if route.rootctl("power", "ac", "high") is None:
                rc = 1
        else:
            print("this Mac has no high power mode")
    print("done" if rc == 0 else "done, with errors above")
    return rc


def pod_owns():
    try:
        with open(os.path.join(STATE, "owner.json")) as f:
            return json.load(f).get("owner") == "pod"
    except (OSError, ValueError, AttributeError):
        return False


def answers():
    """The helper is registered, approved and reachable (pod-rootctl exits 69 when it isn't)."""
    try:
        return subprocess.run([ROOTCTL, "status", "--json"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv):
    if not os.access(ROOTCTL, os.X_OK) or not pod_owns():
        return FALLBACK
    if argv[:1] in (["perf-root"], ["janitor-root"]) and not answers():
        print("Pod's root helper doesn't answer (turn it on in Pod and allow it in Login Items); "
              "the root copy instead", file=sys.stderr)
        return FALLBACK
    if argv[:1] == ["perf-root"]:
        return perf_root(argv[1:])
    if argv[:1] == ["janitor-root"]:
        return janitor_root(argv[1:])
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
