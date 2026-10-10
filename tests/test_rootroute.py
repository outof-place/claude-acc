"""rootroute.py: perf-root.sh and janitor-root.sh through Pod's root helper (docs/pod-rootd.md).

A fake pod-rootctl in a temporary $HOME writes down every call and answers like the real one with
--json; perf.py records the root tweaks in that $HOME. Nothing reaches the real helper or needs root.

Run: /usr/bin/python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# This Mac's Ultra state before Pod: perf-root.sh's boot daemons for vnodes and the GPU limit, and
# Spotlight on apps-only with the list from before in spotlight-exclusions.json. The helper has no
# record of any of it until `legacy migrate`.
OLD_DAEMONS = {"legacy": [
    {"daemon": "com.filip.claude-acc.vnodes", "installed": True, "migrated": False},
    {"daemon": "com.filip.claude-acc.iogpu", "installed": True, "migrated": False},
], "spotlight": {"appsOnly": False}}
MIGRATED = {"legacy": [
    {"daemon": "com.filip.claude-acc.vnodes", "installed": False, "migrated": True},
    {"daemon": "com.filip.claude-acc.iogpu", "installed": False, "migrated": True},
], "spotlight": {"appsOnly": True, "savedEntries": 12}}

FAKE = r'''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["ROOTCTL_CALLS"], "a") as f:
    f.write(json.dumps(args) + "\n")
status = {"power": {"highPowerCapable": True},
          "shapers": [{"interface": "en0", "kbps": 27000, "previousKbps": 650000, "scope": "untilReboot"}],
          "sysctls": [{"key": "iogpu.wired_limit_mb", "current": 40960, "persisted": 40960}]}
status.update(json.loads(os.environ.get("ROOTCTL_STATUS") or "{}"))
report = None
if args[:2] == ["launchd", "park-orphans"]:
    report = {"orphans": {"_0": [{"label": "com.gone", "plist": "/Library/LaunchDaemons/com.gone.plist",
                                  "program": "/Library/gone", "domain": "system"}], "parked": "--dry-run" not in args}}
if args[:2] == ["logs", "prune"]:
    report = {"pruned": {"files": 3, "bytes": 4096, "dryRun": "--dry-run" in args}}
if os.environ.get("ROOTCTL_DOWN"):
    sys.exit(69)
refuse = os.environ.get("ROOTCTL_REFUSE")
if refuse and " ".join(args).startswith(refuse):
    print(json.dumps({"outcome": {"refused": {"needsApproval": {"verb": refuse}}}, "status": status}))
    sys.exit(1)
done = {"changed": True}
if os.environ.get("ROOTCTL_NOTE") and args[:1] != ["status"]:
    done["note"] = os.environ["ROOTCTL_NOTE"]
if report:
    done["report"] = report
print(json.dumps({"outcome": {"done": done}, "status": status}))
'''


class RootRouteTest(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="rootroute-test-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state = os.path.join(self.home, ".local", "share", "claude-acc")
        os.makedirs(self.state)
        self.rootctl = os.path.join(self.state, "pod-rootctl")
        with open(self.rootctl, "w") as f:
            f.write(FAKE)
        os.chmod(self.rootctl, 0o755)
        with open(os.path.join(self.state, "source"), "w") as f:
            f.write(ROOT + "\n")
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "pod"}, f)
        self.calls_path = os.path.join(self.home, "calls.jsonl")

    def run_route(self, *args, **env):
        environ = dict(os.environ, HOME=self.home, ROOTCTL_CALLS=self.calls_path, SCHED_OFF="1", **env)
        return subprocess.run(["/usr/bin/python3", os.path.join(ROOT, "rootroute.py"), *args], env=environ,
                              capture_output=True, text=True, timeout=120)

    def calls(self):
        try:
            with open(self.calls_path) as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    def applied(self):
        try:
            with open(os.path.join(self.state, "perf-state.json")) as f:
                return json.load(f).get("applied", {})
        except OSError:
            return {}

    def test_shaper_apply_goes_to_the_helper_and_is_recorded(self):
        done = self.run_route("perf-root", "shaper", "apply", "--rate", "27Mbps", "--if", "en0")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(["shaper", "set", "en0", "27000", "--json"], self.calls())
        self.assertEqual(self.applied()["shaper"]["detail"], "en0 27Mbps prev=650.00Mbps")
        done = self.run_route("perf-root", "shaper", "undo")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(["shaper", "clear", "en0", "--json"], self.calls())
        self.assertNotIn("shaper", self.applied())

    def test_vnodes_and_iogpu_are_sysctl_verbs(self):
        self.assertEqual(self.run_route("perf-root", "vnodes", "apply", "--value", "786432", "--persist").returncode, 0)
        self.assertEqual(self.run_route("perf-root", "iogpu", "set", "40960").returncode, 0)
        self.assertEqual(self.run_route("perf-root", "iogpu", "undo").returncode, 0)
        calls = self.calls()
        self.assertIn(["sysctl", "set", "maxvnodes", "786432", "--persist", "--json"], calls)
        self.assertIn(["sysctl", "set", "gpu-wired-limit-mb", "40960", "--persist", "--json"], calls)
        self.assertIn(["sysctl", "reset", "gpu-wired-limit-mb", "--json"], calls)
        applied = self.applied()
        self.assertTrue(applied["vnodes"]["detail"].startswith("786432 prev="))
        self.assertNotIn("iogpu", applied)

    def test_spotlight_apps_only_and_undo(self):
        self.assertEqual(self.run_route("perf-root", "spotlight", "apps-only").returncode, 0)
        self.assertEqual(self.applied()["spotlight"]["detail"], "apps-only")
        self.assertEqual(self.run_route("perf-root", "spotlight", "undo").returncode, 0)
        verbs = [c[:2] for c in self.calls() if c[0] != "status"]  # each run asks first whether the helper answers
        self.assertEqual(verbs, [["spotlight", "apps-only"], ["spotlight", "restore"]])
        self.assertNotIn("spotlight", self.applied())

    def test_a_refusal_records_nothing(self):
        done = self.run_route("perf-root", "spotlight", "apps-only", ROOTCTL_REFUSE="spotlight")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("spotlight", self.applied())

    def test_dry_run_calls_no_verb(self):
        done = self.run_route("perf-root", "--dry-run", "vnodes", "apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(dry-run) pod-rootctl sysctl set maxvnodes 786432", done.stdout)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])
        self.assertNotIn("vnodes", self.applied())

    def test_janitor_root_parks_prunes_and_sets_high_power(self):
        done = self.run_route("janitor-root", "--high-power")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertIn(["launchd", "park-orphans", "--json"], calls)
        self.assertIn(["logs", "prune", "--days", "30", "--json"], calls)
        self.assertIn(["power", "ac", "high", "--json"], calls)
        self.assertIn("/Library/LaunchDaemons/com.gone.plist -> /Library/gone", done.stdout)
        self.assertIn("DiagnosticReports: 3", done.stdout)

    def test_unknown_commands(self):
        self.assertEqual(self.run_route("perf-root", "nonsense").returncode, 2)
        self.assertEqual(self.run_route("janitor-root", "--bogus").returncode, 2)

    def test_falls_back_to_the_root_copy(self):
        # the helper doesn't answer, Pod doesn't own claude-acc, or there is no helper: 75 and no verb
        done = self.run_route("perf-root", "vnodes", "apply", ROOTCTL_DOWN="1")
        self.assertEqual(done.returncode, 75)
        self.assertIn("zamiast tego kopia roota", done.stderr)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "homebrew"}, f)
        self.assertEqual(self.run_route("perf-root", "vnodes", "apply").returncode, 75)
        os.remove(os.path.join(self.state, "owner.json"))
        os.remove(self.rootctl)
        self.assertEqual(self.run_route("janitor-root").returncode, 75)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])

    def record(self, *args):
        environ = dict(os.environ, HOME=self.home, SCHED_OFF="1")
        done = subprocess.run(["/usr/bin/python3", os.path.join(ROOT, "perf.py"), "record", *args], env=environ,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_tweaks_of_the_old_daemons_stay_with_the_root_copy_until_migrate(self):
        # acc-phase0's repro: before, an undo through the helper changed nothing and perf.py forgot both
        self.record("spotlight", "apps-only")
        self.record("vnodes", "786432", "prev=263168")
        with open(os.path.join(self.state, "spotlight-exclusions.json"), "w") as f:
            f.write("[]\n")
        old = json.dumps(OLD_DAEMONS)
        for command in (["spotlight", "undo"], ["spotlight", "apps-only"], ["vnodes", "undo"],
                        ["vnodes", "apply", "--persist"], ["iogpu", "undo"], ["iogpu", "set", "40960"]):
            done = self.run_route("perf-root", *command, ROOTCTL_STATUS=old)
            self.assertEqual(done.returncode, 75, (command, done.stdout, done.stderr))
            self.assertIn("przez kopię roota", done.stderr)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])
        self.assertEqual(sorted(self.applied()), ["spotlight", "vnodes"])
        # reading the GPU limit needs no root and stays here
        self.assertEqual(self.run_route("perf-root", "iogpu", "status", ROOTCTL_STATUS=old).returncode, 0)

        # after `legacy migrate` the helper holds the list and the values: the undo goes through it
        migrated = json.dumps(MIGRATED)
        self.assertEqual(self.run_route("perf-root", "spotlight", "undo", ROOTCTL_STATUS=migrated).returncode, 0)
        self.assertEqual(self.run_route("perf-root", "vnodes", "undo", ROOTCTL_STATUS=migrated).returncode, 0)
        self.assertEqual(self.run_route("perf-root", "iogpu", "undo", ROOTCTL_STATUS=migrated).returncode, 0)
        verbs = [c[:3] for c in self.calls() if c[0] != "status"]
        self.assertEqual(verbs, [["spotlight", "restore", "--json"], ["sysctl", "reset", "maxvnodes"],
                                 ["sysctl", "reset", "gpu-wired-limit-mb"]])
        self.assertEqual(self.applied(), {})

    def test_no_root_commands_go_to_perf_root_sh_with_every_option(self):
        # `devtools add --dry-run` must stay a dry run: perf-root.sh gets the arguments as given
        src = os.path.join(self.home, "src")
        os.makedirs(src)
        with open(os.path.join(src, "perf-root.sh"), "w") as f:
            f.write('echo "perf-root.sh $*"\n')
        with open(os.path.join(self.state, "source"), "w") as f:
            f.write(src + "\n")
        done = self.run_route("perf-root", "devtools", "add", "--dry-run")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "perf-root.sh devtools add --dry-run"))
        done = self.run_route("perf-root", "--dry-run", "shaper", "status", "--if", "en0")
        self.assertEqual(done.stdout.strip(), "perf-root.sh --dry-run shaper status --if en0")

    def test_unknown_options_are_refused(self):
        done = self.run_route("perf-root", "--dry-run", "vnodes", "apply", "--persit")
        self.assertEqual(done.returncode, 2)
        self.assertIn("nieznana opcja: --persit", done.stderr)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])

    def test_the_helpers_note_is_printed(self):
        note = "no list from before apps-only was saved; nothing to put back"
        done = self.run_route("perf-root", "spotlight", "undo", ROOTCTL_NOTE=note)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("pomocnik: " + note, done.stdout)


def load_rootroute(home):
    """rootroute.py as a module, with $HOME in a temporary directory."""
    previous = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        spec = importlib.util.spec_from_file_location("rootroute_under_test", os.path.join(ROOT, "rootroute.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = previous


class TrialUndoTest(unittest.TestCase):
    """A trial whose undo fails (Touch ID cancelled, a refusal) leaves the limit on: it must not exit 0."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="rootroute-trial-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        rootroute = load_rootroute(self.home)

        class Route(rootroute.Route):
            def __init__(self, refuse):
                super().__init__()
                self.refuse = refuse
                self.calls = []

            def rootctl(self, *args):
                self.calls.append(list(args))
                if " ".join(args).startswith(self.refuse):
                    return None
                return {"outcome": {"done": {"changed": True}}, "status": {}}

            def perf(self, *args, capture=False):
                if args[:1] == ("status",):
                    return json.dumps({"tweaks": [{"name": "shaper", "applied": True, "detail": "en0 27Mbps"}]})
                if args[:1] == ("bench",):
                    return "{}"
                return "" if capture else 0

        self.rootroute = rootroute
        self.Route = Route

    def quiet(self, run, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run(*args, **kwargs)

    def test_a_refused_undo_fails_the_trial(self):
        route = self.Route("shaper clear")
        self.assertEqual(self.quiet(self.rootroute.trial, route, "en0", "27Mbps", keep=False), 1)
        self.assertIn(["shaper", "clear", "en0"], route.calls)
        route = self.Route("sysctl reset")
        self.assertEqual(self.quiet(self.rootroute.vnodes_trial, route, 786432, persist=False, keep=False), 1)

    def test_a_trial_that_undoes_cleanly_passes(self):
        route = self.Route("nothing")
        self.assertEqual(self.quiet(self.rootroute.trial, route, "en0", "27Mbps", keep=False), 0)
        self.assertEqual(self.quiet(self.rootroute.vnodes_trial, route, 786432, persist=False, keep=False), 0)


if __name__ == "__main__":
    unittest.main()
