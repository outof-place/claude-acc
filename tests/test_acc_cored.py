"""acc-cored's readers, patterns and guard tick against devguard_core's, with the built binary.

The readers' checks read this Mac's process table and sockets; the guard's fixtures are synthetic
(tests/acc_cored/devguard_replay.py fuzz). Nothing is changed. Build first:
    cd app && swift build -c release --product acc-cored
"""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILT = os.path.join(ROOT, "app/.build/release/acc-cored")
needs_binary = unittest.skipUnless(os.access(BUILT, os.X_OK), "no app/.build/release/acc-cored")


@needs_binary
class ReadersTest(unittest.TestCase):
    def run_check(self, script, *args):
        done = subprocess.run(
            [sys.executable, "-I", os.path.join(HERE, "acc_cored", script), BUILT, *args], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_patterns_match_python_re(self):
        self.run_check("parity_regex.py", "--fuzz", "500")

    def test_process_and_socket_tables_match(self):
        self.run_check("parity_probes.py", "--rounds", "2")


@needs_binary
class GuardReplayTest(unittest.TestCase):
    def test_fuzzed_ticks_match_python(self):
        sys.path.insert(0, os.path.join(HERE, "acc_cored"))
        import devguard_replay as dr

        dg, janitor, lastresort = dr.load(ROOT)
        rng = random.Random(11)
        # the paths Python's guard reads are fixed at import (janitor.HOME); the fixtures serve them
        home = janitor.HOME
        written = 0
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for i in range(200):
                fixture = dr.fuzz_fixture(rng, home, i)
                try:
                    fixture["expect"], _missing = dr.serve(dg, janitor, lastresort, fixture)
                except Exception:  # noqa: BLE001 - a config of the wrong type can make Python's tick raise
                    continue
                f.write(json.dumps(fixture, ensure_ascii=False) + "\n")
                written += 1
        done = subprocess.run([BUILT, "guard-replay", f.name, "--show", "3"], capture_output=True, text=True)
        os.unlink(f.name)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        m = re.search(r"same (\d+), different 0, .* handed to Python \(config types\) (\d+)", done.stdout)
        self.assertIsNotNone(m, done.stdout)
        self.assertEqual(int(m[1]) + int(m[2]), written, done.stdout)
        self.assertGreater(int(m[1]), 150, done.stdout)


if __name__ == "__main__":
    unittest.main()
