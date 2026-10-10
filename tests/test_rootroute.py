"""rootroute.py: perf-root.sh and janitor-root.sh through Pod's root helper (docs/pod-rootd.md).

A fake pod-rootctl in a temporary $HOME writes down every call and answers like the real one with
--json; perf.py records the root tweaks in that $HOME. Nothing reaches the real helper or needs root.

Run: /usr/bin/python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAKE = r'''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["ROOTCTL_CALLS"], "a") as f:
    f.write(json.dumps(args) + "\n")
status = {"power": {"highPowerCapable": True},
          "shapers": [{"interface": "en0", "kbps": 27000, "previousKbps": 650000, "scope": "untilReboot"}],
          "sysctls": [{"key": "iogpu.wired_limit_mb", "current": 40960, "persisted": 40960}]}
report = None
if args[:2] == ["launchd", "park-orphans"]:
    report = {"orphans": {"_0": [{"label": "com.gone", "plist": "/Library/LaunchDaemons/com.gone.plist",
                                  "program": "/Library/gone", "domain": "system"}], "parked": "--dry-run" not in args}}
if args[:2] == ["logs", "prune"]:
    report = {"pruned": {"files": 3, "bytes": 4096, "dryRun": "--dry-run" in args}}
if os.environ.get("ROOTCTL_DOWN"):
    sys.exit(69)
refuse = os.environ.get("ROOTCTL_REFUSE")
if refuse and args[:1] == [refuse]:
    print(json.dumps({"outcome": {"refused": {"needsApproval": {"verb": refuse}}}, "status": status}))
    sys.exit(1)
done = {"changed": True}
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
        self.assertIn("the root copy instead", done.stderr)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "homebrew"}, f)
        self.assertEqual(self.run_route("perf-root", "vnodes", "apply").returncode, 75)
        os.remove(os.path.join(self.state, "owner.json"))
        os.remove(self.rootctl)
        self.assertEqual(self.run_route("janitor-root").returncode, 75)
        self.assertEqual([c for c in self.calls() if c[0] != "status"], [])


if __name__ == "__main__":
    unittest.main()
