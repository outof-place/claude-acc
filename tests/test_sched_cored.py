"""The scheduler with acc-cored doing the per-second pass: real wrappers (`acc.py sched run`), the built
acc-cored, a scratch $HOME and SCHED_FAKE_MEMORY. Short jobs, a killed and a restarted acc-cored, a
state of another version, and native_scan against Python's. Build first:
    cd app && swift build -c release --product acc-cored

Uruchomienie: /usr/bin/python3 -m unittest tests.test_sched_cored
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILT = os.path.join(ROOT, "app/.build/release/acc-cored")
needs_binary = unittest.skipUnless(os.access(BUILT, os.X_OK), "no app/.build/release/acc-cored")
# a "build" the classifier schedules: python running a script named build_*.py
JOB = """import sys, time
b = bytearray(int(float(sys.argv[1]) * 2**20))
for i in range(0, len(b), 16384):
    b[i] = 1
time.sleep(float(sys.argv[2]))
del b
time.sleep(float(sys.argv[3]))
"""


@needs_binary
class CoredSchedTest(unittest.TestCase):
    def setUp(self):
        # under /tmp: the control socket's path must stay under 104 bytes (sun_path)
        self.home = tempfile.mkdtemp(prefix="acs-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state = os.path.join(self.home, ".local", "share", "claude-acc")
        os.makedirs(os.path.join(self.state, "sched"))
        for name in os.listdir(ROOT):
            if name.endswith(".py"):
                shutil.copy(os.path.join(ROOT, name), self.state)
        os.symlink(sys.executable, os.path.join(self.state, "python"))
        self.jobdir = os.path.join(self.home, "job")
        os.makedirs(self.jobdir)
        with open(os.path.join(self.jobdir, "build_mem.py"), "w") as f:
            f.write(JOB)
        with open(os.path.join(self.state, "sched", "config.json"), "w") as f:
            json.dump({"headroom_gb": 2.0}, f)
        with open(os.path.join(self.home, "mem.json"), "w") as f:
            json.dump({"level": 80, "ram_gb": 48, "swap_gb": 0, "pressure": "normal"}, f)
        self.env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_ACC_SCHED_JOB", "SCHED_OFF", "SCHED_NO_CORED")}
        self.env.update(HOME=self.home, SCHED_CONFIG=os.path.join(self.state, "sched", "config.json"),
                        SCHED_FAKE_MEMORY=os.path.join(self.home, "mem.json"))  # fmt: skip
        self.cored = []

    def tearDown(self):
        for p in self.cored:
            if p.poll() is None:
                p.kill()
                p.wait()

    def start_cored(self):
        p = subprocess.Popen([BUILT, "run", "--sched"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.cored.append(p)
        for _ in range(100):  # the socket answers
            if os.path.exists(os.path.join(self.state, "acc-cored.sock")):
                break
            time.sleep(0.02)
        time.sleep(0.2)
        return p

    def job(self, mb, hold, after):
        cmd = f"{sys.executable} {self.jobdir}/build_mem.py {mb} {hold} {after}"
        return subprocess.Popen([os.path.join(self.state, "python"), "-B", os.path.join(self.state, "acc.py"), "sched", "run",
                                 "--via", "hook", "--shell", cmd], cwd=self.jobdir, env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip

    def history(self):
        path = os.path.join(self.state, "sched", "history.jsonl")
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def sched_state(self):
        with open(os.path.join(self.state, "sched", "state.json")) as f:
            return json.load(f)

    def ping(self):
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(3)
            c.connect(os.path.join(self.state, "acc-cored.sock"))
            c.sendall(b'{"op": "ping"}\n')
            return json.loads(c.recv(65536))

    def test_a_queued_wrapper_is_woken_when_admitted(self):
        # wake: usr1: the wrapper sleeps in kqueue up to CORED_WAKE_S (5 s); acc-cored's SIGUSR1 after
        # the pass that admits it starts the job at once
        def memory(level, pressure):
            with open(os.path.join(self.home, "mem.json"), "w") as f:
                json.dump({"level": level, "ram_gb": 48, "swap_gb": 0, "pressure": pressure}, f)

        memory(2, "critical")
        self.start_cored()
        job = self.job(100, 0.2, 0)
        waiting = False
        for _ in range(100):
            try:
                waiting = any(j.get("wake") == "usr1" for j in self.sched_state()["queue"])
            except (OSError, ValueError):
                pass
            if waiting:
                break
            time.sleep(0.05)
        self.assertTrue(waiting, "the job never queued with wake: usr1")
        time.sleep(1.5)
        self.assertIsNone(job.poll())
        memory(80, "normal")
        self.assertEqual(job.wait(30), 0)
        self.assertGreaterEqual(self.ping()["wakes"], 1)

    def test_short_jobs_get_their_peak(self):
        self.start_cored()
        jobs = [self.job(300, 0.3, 0) for _ in range(4)]
        self.assertEqual([j.wait(60) for j in jobs], [0, 0, 0, 0])
        peaks = sorted(r["peak_gb"] for r in self.history())
        self.assertEqual(len(peaks), 4)
        self.assertGreater(peaks[0], 0.2, peaks)

    def test_a_killed_acc_cored_leaves_the_wrappers_their_own_loops(self):
        cored = self.start_cored()
        job = self.job(200, 1, 6)
        time.sleep(2)
        running = self.sched_state()["running"]
        self.assertEqual([r.get("monitor") for r in running], ["cored"])
        cored.send_signal(signal.SIGKILL)
        cored.wait()
        self.assertEqual(job.wait(60), 0)
        (row,) = self.history()
        self.assertGreater(row["peak_gb"], 0.15)

    def test_a_stopped_acc_cored_counts_as_gone(self):
        cored = self.start_cored()
        job = self.job(50, 0, 12)
        time.sleep(2)
        cored.send_signal(signal.SIGSTOP)
        try:
            # its mark goes stale: within a wake (5 s) plus CORED_STALE_S the wrapper beats on its own
            deadline = time.time() + 12
            while time.time() < deadline:
                running = self.sched_state()["running"]
                if running and running[0].get("monitor") is None:
                    break
                time.sleep(0.5)
            self.assertIsNone(self.sched_state()["running"][0].get("monitor"))
        finally:
            cored.send_signal(signal.SIGCONT)
        self.assertEqual(job.wait(60), 0)

    def test_a_restarted_acc_cored_keeps_the_peak(self):
        first = self.start_cored()
        job = self.job(500, 1.5, 4)
        time.sleep(2.2)
        first.send_signal(signal.SIGKILL)
        first.wait()
        self.start_cored()
        self.assertEqual(job.wait(60), 0)
        (row,) = self.history()
        self.assertGreater(row["peak_gb"], 0.4, row)

    def test_a_state_of_another_version_is_left_alone(self):
        path = os.path.join(self.state, "sched", "state.json")
        with open(path, "w") as f:
            json.dump({"version": 2, "running": [{"id": "x"}], "queue": []}, f)
        before = os.stat(path).st_mtime_ns
        self.start_cored()
        os.utime(os.path.join(self.state, "sched"))  # a write in the directory: a pass
        with open(os.path.join(self.state, "sched", "nudge"), "w") as f:
            f.write("x")
        time.sleep(1.5)
        self.assertEqual(os.stat(path).st_mtime_ns, before)


    def test_clients_gone_before_their_answer_leave_the_daemon_up(self):
        # a wrapper whose call timed out closes first; XNU then refuses SO_NOSIGPIPE at accept, and
        # an unchecked write killed the daemon (exit 141) after ~60 such clients
        import socket

        cored = self.start_cored()
        path = os.path.join(self.state, "acc-cored.sock")
        for _ in range(400):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                c.connect(path)
                c.sendall(b'{"op": "ping"}\n')
            except OSError:
                pass
            c.close()
        time.sleep(0.3)
        self.assertIsNone(cored.poll(), "acc-cored ended with %s" % cored.returncode)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(3)
        c.connect(path)
        c.sendall(b'{"op": "ping"}\n')
        self.assertIn(b'"pid"', c.recv(65536))
        c.close()


@needs_binary
class NativeScanTest(unittest.TestCase):
    def test_scan_matches_native_scan(self):
        sys.path.insert(0, ROOT)
        import sched as S

        for since in (None, 0):
            for _ in range(3):  # a build starting or ending between the reads: read again
                a = S.native_scan(sims_since=since)
                args = [BUILT, "sched-scan"] + (["--sims-since", str(since)] if since is not None else [])
                native = json.loads(subprocess.run(args, capture_output=True, text=True, check=True).stdout)
                b = S.native_scan(sims_since=since)
                if a["pids"] == b["pids"] and a["active"] == b["active"]:
                    break
            self.assertEqual(native["active"], a["active"])
            self.assertEqual(set(native["pids"]), a["pids"])


if __name__ == "__main__":
    unittest.main()
