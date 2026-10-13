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

    def run_switch(self, *args, **extra):
        env = dict(os.environ, HOME=self.home, ACC_CORED_LAUNCHCTL=os.path.join(HERE, "fakes-launchctl", "launchctl"),
                   FAKE_LOADED=self.loaded, FAKE_LOG=os.path.join(self.tmp, "calls.log"), **extra)  # fmt: skip
        return subprocess.run([SCRIPT, *args], env=env, capture_output=True, text=True)

    def record(self):
        return os.path.join(self.state, "acc-cored-switch.json")

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

    def test_off_keeps_the_record_when_a_job_doesn_t_come_back(self):
        self.assertEqual(self.run_switch("on", "--apply", "--binary", self.binary).returncode, 0)
        done = self.run_switch("off", "--apply", FAKE_FAIL="bootstrap:com.filip.claude-acc")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("run off --apply again", done.stderr)
        self.assertTrue(os.path.exists(self.record()))
        self.assertTrue(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))
        self.assertNotIn("com.filip.claude-acc", self.loaded_jobs())
        again = self.run_switch("off", "--apply")  # the record still names the tick: it comes back
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))
        self.assertFalse(os.path.exists(self.record()))

    def test_on_reports_a_failed_bootstrap_or_copy(self):
        done = self.run_switch("on", "--apply", "--binary", self.binary, FAKE_FAIL="bootstrap:com.filip.claude-acc.cored")
        self.assertEqual(done.returncode, 1)
        self.assertIn("off --apply brings the jobs back", done.stderr)
        self.assertTrue(os.path.exists(self.record()))
        self.assertEqual(self.run_switch("off", "--apply").returncode, 0)
        self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))
        # a copy that fails (a binary it can't read, $STATE writable): nothing goes, nothing is written
        os.chmod(self.binary, 0o111)
        self.addCleanup(os.chmod, self.binary, 0o755)
        done = self.run_switch("on", "--apply", "--binary", self.binary)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual(self.loaded_jobs(), JOBS)
        self.assertFalse(os.path.exists(self.record()))
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "acc-cored.new")))

    def test_on_refuses_where_pod_owns_claude_acc_or_nothing_is_loaded(self):
        # Pod's own agents run the jobs there: acc-cored beside them would act twice
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "pod", "version": "1.31.8", "app": "/Applications/Pod.app", "at": 1}, f)
        for loaded in (["codes.pod.app.acc.tick"], JOBS):
            with open(self.loaded, "w") as f:
                f.write("\n".join(loaded) + "\n")
            for args in (["on", "--apply"], ["on"]):
                done = self.run_switch(*args, "--binary", self.binary)
                self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
                self.assertEqual(self.loaded_jobs(), loaded)
                self.assertFalse(os.path.exists(self.record()))
                self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))
                self.assertFalse(os.path.exists(os.path.join(self.state, "acc-cored")))
        # also without a binary: Pod's ownership is the answer, not the missing build
        done = self.run_switch("on", "--binary", os.path.join(self.tmp, "none"))
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        # a tombstone (handed back to Homebrew) is a standalone install again
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "brew", "version": "1.31.8", "app": None, "at": 1}, f)
        self.assertEqual(self.run_switch("on", "--binary", self.binary).returncode, 0)
        # none of the three loaded, and no owner: nothing to replace
        with open(self.loaded, "w") as f:
            f.write("codes.pod.app.acc.tick\n")
        done = self.run_switch("on", "--apply", "--binary", self.binary)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("nothing for acc-cored to replace", done.stderr)
        self.assertEqual(self.loaded_jobs(), ["codes.pod.app.acc.tick"])
        self.assertFalse(os.path.exists(self.record()))
        self.assertFalse(os.path.exists(os.path.join(self.state, "acc-cored")))

    def test_off_keeps_the_record_when_acc_cored_stays_or_a_plist_is_gone(self):
        self.assertEqual(self.run_switch("on", "--apply", "--binary", self.binary).returncode, 0)
        done = self.run_switch("off", "--apply", FAKE_FAIL="bootout:com.filip.claude-acc.cored")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("com.filip.claude-acc.cored", self.loaded_jobs())
        self.assertTrue(os.path.exists(self.record()))
        self.assertTrue(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))
        # a bootout that fails after acc-cored went: launchd says it went, so off finishes
        done = self.run_switch("off", "--apply", FAKE_FAIL="bootout:com.filip.claude-acc.cored:after")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))
        self.assertFalse(os.path.exists(self.record()))
        # the tick's plist gone meanwhile: off says so and keeps the record
        self.assertEqual(self.run_switch("on", "--apply", "--binary", self.binary).returncode, 0)
        os.unlink(os.path.join(self.agents, "com.filip.claude-acc.plist"))
        done = self.run_switch("off", "--apply")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("missing " + os.path.join(self.agents, "com.filip.claude-acc.plist"), done.stderr)
        self.assertTrue(os.path.exists(self.record()))
        self.assertEqual(sorted(self.loaded_jobs()), sorted(["com.filip.claude-acc.devguard", "com.filip.claude-acc.janitor"]))

    def test_a_bootout_that_fails_after_the_job_went_counts_by_launchd(self):
        # rc != 0, but launchd no longer has the job: the switch goes on, and the record names it
        done = self.run_switch("on", "--apply", "--binary", self.binary, FAKE_FAIL="bootout:com.filip.claude-acc:after")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with open(self.record()) as f:
            self.assertIn("com.filip.claude-acc", json.load(f)["replaced"])
        self.assertEqual(self.loaded_jobs(), ["com.filip.claude-acc.cored"])

    def test_a_job_that_stays_brings_back_what_went(self):
        # devguard (or the janitor) still loaded beside acc-cored: the tick that went comes back
        for job in ("com.filip.claude-acc.devguard", "com.filip.claude-acc.janitor"):
            with self.subTest(job=job):
                done = self.run_switch("on", "--apply", "--binary", self.binary, FAKE_FAIL=f"bootout:{job}")
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn("didn't go: the jobs are back as they were", done.stderr)
                self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))
                self.assertFalse(os.path.exists(self.record()))
                self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))

    def test_a_job_that_stays_and_one_that_won_t_come_back_keep_the_record(self):
        done = self.run_switch("on", "--apply", "--binary", self.binary,
                               FAKE_FAIL="bootout:com.filip.claude-acc.devguard,bootstrap:com.filip.claude-acc")  # fmt: skip
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("off --apply brings them back", done.stderr)
        self.assertTrue(os.path.exists(self.record()))
        self.assertNotIn("com.filip.claude-acc.cored", self.loaded_jobs())
        self.assertEqual(self.run_switch("off", "--apply").returncode, 0)
        self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))

    def test_a_bootstrap_that_fails_after_the_job_started_counts_by_launchd(self):
        done = self.run_switch("on", "--apply", "--binary", self.binary, FAKE_FAIL="bootstrap:com.filip.claude-acc.cored:after")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.loaded_jobs(), ["com.filip.claude-acc.cored"])
        done = self.run_switch("off", "--apply", FAKE_FAIL="bootstrap:com.filip.claude-acc.devguard:after")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(sorted(self.loaded_jobs()), sorted(JOBS))
        self.assertFalse(os.path.exists(self.record()))

    def setup_functions(self):
        with open(os.path.join(ROOT, "setup.sh")) as f:
            text = f.read()
        out = []
        for name in ("bootout_job", "drop_cored"):
            start = text.index(name + "() {")
            out.append(text[start:text.index("\n}\n", start) + 3])
        return text, "\n".join(out)

    def test_setup_takes_acc_cored_down_on_uninstall_and_for_pod(self):
        text, functions = self.setup_functions()
        at = text.index("    --uninstall)\n")  # the branch that acts (an earlier pass only notes the flag)
        uninstall = text[at:text.index(";;", at)]
        self.assertIn("drop_cored", uninstall)
        self.assertIn('bootout_job "$job"', uninstall)
        pod = text.index('if [ -n "$POD_AGENTS" ]; then\n  drop_cored\nfi\nfor job in $JOBS; do')
        self.assertGreater(pod, 0)
        # the functions themselves, against the fake launchctl
        for name in ("com.filip.claude-acc.cored.plist",):
            with open(os.path.join(self.agents, name), "w") as f:
                f.write("<plist/>")
        with open(self.record(), "w") as f:
            f.write("{}")
        with open(self.loaded, "a") as f:
            f.write("com.filip.claude-acc.cored\n")
        fake = os.path.join(self.tmp, "bin")
        os.makedirs(fake)
        os.symlink(os.path.join(HERE, "fakes-launchctl", "launchctl"), os.path.join(fake, "launchctl"))
        env = dict(os.environ, HOME=self.home, STATE=self.state, AGENTS=self.agents, PATH=fake + ":/usr/bin:/bin",
                   FAKE_LOADED=self.loaded, FAKE_LOG=os.path.join(self.tmp, "calls.log"))  # fmt: skip
        uid = os.getuid()
        script = functions + f"""
set -euo pipefail
drop_cored
bootout_job com.filip.claude-acc gui/{uid}/com.filip.claude-acc
bootout_job com.filip.claude-acc.perf gui/{uid}/com.filip.claude-acc.perf
"""
        done = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.agents, "com.filip.claude-acc.cored.plist")))
        self.assertFalse(os.path.exists(self.record()))
        self.assertNotIn("com.filip.claude-acc.cored", self.loaded_jobs())
        self.assertNotIn("com.filip.claude-acc", self.loaded_jobs())
        self.assertTrue(os.path.exists(os.path.join(self.state, "lock")))  # the tick went under the lock
        with open(os.path.join(self.tmp, "calls.log")) as f:
            calls = f.read().splitlines()
        # perf isn't loaded: a look, no bootout, and no wait for the switcher's lock
        self.assertIn(f"print gui/{uid}/com.filip.claude-acc.perf", calls)
        self.assertNotIn(f"bootout gui/{uid}/com.filip.claude-acc.perf", calls)

    def test_the_fake_boots_out_a_plist_too(self):
        # setup.sh boots jobs out by plist (gui/UID x.plist): the fake drops those as launchd does
        fake = os.path.join(HERE, "fakes-launchctl", "launchctl")
        env = dict(os.environ, FAKE_LOADED=self.loaded, FAKE_LOG=os.path.join(self.tmp, "calls.log"))
        uid = os.getuid()
        plist = os.path.join(self.agents, "com.filip.claude-acc.janitor.plist")
        self.assertEqual(subprocess.run([fake, "bootout", f"gui/{uid}", plist], env=env).returncode, 0)
        self.assertNotIn("com.filip.claude-acc.janitor", self.loaded_jobs())
        self.assertEqual(subprocess.run([fake, "print", f"gui/{uid}/com.filip.claude-acc.janitor"], env=env).returncode, 1)

    def test_on_brings_back_only_what_it_stopped(self):
        with open(self.loaded, "w") as f:
            f.write("com.filip.claude-acc.devguard\n")  # the tick and the janitor weren't loaded
        self.run_switch("on", "--apply", "--binary", self.binary)
        self.run_switch("off", "--apply")
        self.assertEqual(self.loaded_jobs(), ["com.filip.claude-acc.devguard"])


if __name__ == "__main__":
    unittest.main()
