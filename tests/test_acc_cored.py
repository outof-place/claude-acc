"""acc-cored's readers and patterns against devguard_core's, with the built binary.

The checks read this Mac's process table and sockets (nothing is changed). Build first:
    cd app && swift build -c release --product acc-cored
"""

import os
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
