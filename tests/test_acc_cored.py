"""acc-cored's readers, patterns, guard tick and scheduler pass against Python's, with the built binary.

The readers' checks read this Mac's process table and sockets; the guard's and the scheduler's
fixtures are synthetic (tests/acc_cored/devguard_replay.py and sched_replay.py fuzz). Nothing is
changed. Build first:
    cd app && swift build -c release --product acc-cored
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILT = os.path.join(ROOT, "app/.build/release/acc-cored")
needs_binary = unittest.skipUnless(os.access(BUILT, os.X_OK), "no app/.build/release/acc-cored")


def python_312():
    """An interpreter of 3.12 or newer, as the live sched.py runs on: from 3.12 `sum()` of floats is
    compensated, and acc-cored mirrors that (on 3.9, 0.17 + 4.49 + 2.395 rounds to 7.05, not 7.06)."""
    if sys.version_info >= (3, 12):
        return sys.executable
    for name in ("python3.14", "python3.13", "python3.12", "python3"):
        path = shutil.which(name)
        if path and subprocess.run([path, "-I", "-c", "import sys; sys.exit(sys.version_info < (3, 12))"]).returncode == 0:
            return path
    return None


@needs_binary
class ReadersTest(unittest.TestCase):
    def run_check(self, script, *args):
        done = subprocess.run(
            [sys.executable, "-I", "-B", os.path.join(HERE, "acc_cored", script), BUILT, *args], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_patterns_match_python_re(self):
        self.run_check("parity_regex.py", "--fuzz", "500")

    def test_process_and_socket_tables_match(self):
        self.run_check("parity_probes.py", "--rounds", "2")


@needs_binary
class GuardReplayTest(unittest.TestCase):
    def test_fuzzed_ticks_match_python(self):
        # in a process of its own: serving a fixture patches devguard_core's readers, and the
        # other suites in this interpreter must see the real ones
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "guard.jsonl")
            made = subprocess.run(
                [sys.executable, "-I", os.path.join(HERE, "acc_cored", "devguard_replay.py"), "fuzz", ROOT, "--n", "200",
                 "--seed", "11", "--out", out],
                capture_output=True, text=True,
            )  # fmt: skip
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            with open(out) as f:
                written = sum(1 for _ in f)
            done = subprocess.run([BUILT, "guard-replay", out, "--show", "3"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        m = re.search(r"same (\d+), different 0, .* handed to Python \(config types\) (\d+)", done.stdout)
        self.assertIsNotNone(m, done.stdout)
        self.assertEqual(int(m[1]) + int(m[2]), written, done.stdout)
        self.assertGreater(int(m[1]), 150, done.stdout)


@needs_binary
class SchedReplayTest(unittest.TestCase):
    def test_fuzzed_passes_match_python(self):
        python = python_312()
        if python is None:
            self.skipTest("no Python 3.12 or newer, whose sum() of floats acc-cored mirrors")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sched.jsonl")
            made = subprocess.run(
                [python, "-I", "-B", os.path.join(HERE, "acc_cored", "sched_replay.py"), "fuzz", ROOT, "--n", "300",
                 "--seed", "7", "--out", out],
                capture_output=True, text=True,
            )  # fmt: skip
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            with open(out) as f:
                count = sum(1 for _ in f)
            self.assertGreater(count, 250, made.stdout)
            done = subprocess.run([BUILT, "sched-replay", out], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"same {count}, different 0", done.stdout)


if __name__ == "__main__":
    unittest.main()
