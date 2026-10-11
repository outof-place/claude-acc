"""scripts/acc-cored-switch.sh: devguard, the accswitch tick and the janitor out, acc-cored in, and back.

A scratch $HOME and a fake launchctl (tests/fakes-launchctl): nothing on this Mac's launchd changes.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "acc-cored-switch.sh")
JOBS = ["com.filip.claude-acc.devguard", "com.filip.claude-acc", "com.filip.claude-acc.janitor"]


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.agents = os.path.join(self.home, "Library", "LaunchAgents")
        self.state = os.path.join(self.home, ".local", "share", "claude-acc")
        os.makedirs(self.agents)
        os.makedirs(self.state)
        for job in JOBS:
            with open(os.path.join(self.agents, job + ".plist"), "w") as f:
                f.write("<plist/>")
        self.loaded = os.path.join(self.tmp, "loaded")
        with open(self.loaded, "w") as f:
            f.write("\n".join(JOBS) + "\n")
        self.binary = os.path.join(self.tmp, "acc-cored")
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.binary, 0o755)

    def run_switch(self, *args):
        env = dict(os.environ, HOME=self.home, ACC_CORED_LAUNCHCTL=os.path.join(HERE, "fakes-launchctl", "launchctl"),
                   FAKE_LOADED=self.loaded, FAKE_LOG=os.path.join(self.tmp, "calls.log"))  # fmt: skip
        return subprocess.run([SCRIPT, *args], env=env, capture_output=True, text=True)

    def loaded_jobs(self):
        with open(self.loaded) as f:
            return [line for line in f.read().split("\n") if line]

    def test_check_only_changes_nothing(self):
        done = self.run_switch("on", "--binary", self.binary)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(check only: add --apply)", done.stdout)
        self.assertEqual(self.loaded_jobs(), JOBS)
        self.assertFalse(os.path.exists(os.path.join(self.state, "acc-cored")))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))

    def test_on_then_off_restores_the_jobs(self):
        done = self.run_switch("on", "--apply", "--binary", self.binary)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.loaded_jobs(), ["com.filip.claude-acc.cored"])
        with open(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")) as f:
            plist = f.read()
        self.assertIn(self.home + "/.local/share/claude-acc/acc-cored", plist)
        self.assertNotIn("__HOME__", plist)
        with open(os.path.join(self.state, "acc-cored-switch.json")) as f:
            self.assertEqual(json.load(f)["replaced"], JOBS)
        # the three plists stay for the undo
        for job in JOBS:
            self.assertTrue(os.path.exists(os.path.join(self.agents, job + ".plist")))

        again = self.run_switch("on", "--apply", "--binary", self.binary)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already switched", again.stderr)

        done = self.run_switch("off", "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "acc-cored-switch.json")))

    def test_the_tick_job_goes_only_while_no_tick_holds_the_switcher_s_lock(self):
        # a tick mid token refresh holds $STATE/lock: launchd's bootout must not land then
        import fcntl
        import time

        lock = open(os.path.join(self.state, "lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = dict(os.environ, HOME=self.home, ACC_CORED_LAUNCHCTL=os.path.join(HERE, "fakes-launchctl", "launchctl"),
                   FAKE_LOADED=self.loaded, FAKE_LOG=os.path.join(self.tmp, "calls.log"))  # fmt: skip
        proc = subprocess.Popen([SCRIPT, "on", "--apply", "--binary", self.binary], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        self.assertIn("com.filip.claude-acc", self.loaded_jobs())
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn("com.filip.claude-acc", self.loaded_jobs())
        self.assertTrue(os.path.exists(os.path.join(self.state, "lock")))  # the ticks' lock file stays

    def test_a_lock_held_too_long_changes_nothing(self):
        # a hung lock holder: lockf times out, and no other job goes, no record, no acc-cored
        import fcntl

        lock = open(os.path.join(self.state, "lock"), "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = dict(os.environ, HOME=self.home, ACC_CORED_LAUNCHCTL=os.path.join(HERE, "fakes-launchctl", "launchctl"),
                   FAKE_LOADED=self.loaded, FAKE_LOG=os.path.join(self.tmp, "calls.log"), ACC_CORED_LOCK_WAIT="1")  # fmt: skip
        done = subprocess.run([SCRIPT, "on", "--apply", "--binary", self.binary], env=env, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("didn't go", done.stderr)
        self.assertEqual(self.loaded_jobs(), JOBS)
        self.assertFalse(os.path.exists(os.path.join(self.state, "acc-cored-switch.json")))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))

    def test_on_brings_back_only_what_it_stopped(self):
        with open(self.loaded, "w") as f:
            f.write("com.filip.claude-acc.devguard\n")  # the tick and the janitor weren't loaded
        self.run_switch("on", "--apply", "--binary", self.binary)
        self.run_switch("off", "--apply")
        self.assertEqual(self.loaded_jobs(), ["com.filip.claude-acc.devguard"])


if __name__ == "__main__":
    unittest.main()
