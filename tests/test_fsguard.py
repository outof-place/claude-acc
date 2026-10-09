"""Strażnik fseventsd uruchamiany tak, jak robi to launchd: osobny proces co "minutę".

Zamiast fseventsd celem jest atrapa: kopia /bin/sleep pod losową nazwą, a zamiast demonów
git fsmonitor procesy z losowym znacznikiem w argumentach. Bez roota strażnik i tak nie
mógłby ruszyć prawdziwego fseventsd, a znacznik chroni Twoje demony gita. Stan i log
lądują w katalogu tymczasowym.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_fsguard
"""

import json
import os
import random
import shutil
import string
import subprocess
import tempfile
import time
import unittest

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(os.path.dirname(HERE), "fsguard.py")


def token():
    return "fsg" + "".join(random.choices(string.ascii_lowercase, k=8))


class World:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="fsguard-")
        self.state = os.path.join(self.dir, "state.json")
        self.log = os.path.join(self.dir, "guard.log")
        self.name = token()  # proc_name: do 16 znaków
        self.binary = os.path.join(self.dir, self.name)
        shutil.copy("/bin/sleep", self.binary)
        # kopia binarki systemowej ma podpis platformy z innej ścieżki i jądro ją ubija;
        # podpis ad-hoc wystarcza, żeby atrapa działała
        subprocess.run(["/usr/bin/codesign", "--force", "-s", "-", self.binary], capture_output=True, check=True)
        self.marker = token()
        self.procs = []

    def target(self):
        p = subprocess.Popen([self.binary, "300"])
        self.procs.append(p)
        time.sleep(0.2)
        return p

    def fsmonitor(self, marker=None):
        """Proces, który w `ps` wygląda jak demon git fsmonitor z danym znacznikiem."""
        p = subprocess.Popen(["/bin/bash", "-c", f'exec -a "{marker or self.marker} fsmonitor run" /bin/sleep 300'])
        self.procs.append(p)
        time.sleep(0.2)
        return p

    def run(self, limit_mb, *extra):
        r = subprocess.run(
            ["/usr/bin/python3", GUARD, "--target", self.name, "--limit-mb", str(limit_mb),
             "--state", self.state, "--log", self.log, "--fsmonitor-match", self.marker,
             "--respawn-wait", "0.5", *extra],
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r

    def lines(self):
        try:
            with open(self.log) as f:
                return f.read()
        except OSError:
            return ""

    def saved(self):
        with open(self.state) as f:
            return json.load(f)

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        shutil.rmtree(self.dir, ignore_errors=True)


def gone(p, timeout=15):
    try:
        p.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_small_daemon_is_left_alone(self):
        p = self.w.target()

        self.w.run(100_000)
        self.w.run(100_000)

        self.assertIsNone(p.poll())
        self.assertNotIn("RESTART", self.w.lines())
        self.assertIn(self.w.name, self.w.lines())  # trend: pierwszy odczyt trafia do logu

    def test_one_reading_over_the_limit_does_not_restart(self):
        # chwilowy skok nie wystarcza: restart dopiero przy drugim kolejnym odczycie
        p = self.w.target()

        self.w.run(0.01)

        self.assertIsNone(p.poll())
        self.assertIn("restart przy następnym odczycie", self.w.lines())

    def test_bloated_daemon_is_restarted_and_fsmonitors_follow(self):
        p = self.w.target()
        ours = self.w.fsmonitor()
        other = self.w.fsmonitor(marker=token())  # inny demon niż dopasowany: zostaje

        self.w.run(0.01)
        self.w.run(0.01)

        self.assertTrue(gone(p), "spuchnięty demon dalej żyje")
        self.assertTrue(gone(ours), "demon fsmonitor przeżył restart fseventsd")
        self.assertIsNone(other.poll())
        self.assertIn("RESTART", self.w.lines())
        self.assertIn("zatrzymane demony git fsmonitor: 1", self.w.lines())
        self.assertEqual(self.w.saved()["restarts"], 1)

    def test_restarts_are_at_least_five_minutes_apart(self):
        first = self.w.target()
        self.w.run(0.01)
        self.w.run(0.01)
        self.assertTrue(gone(first))
        second = self.w.target()  # launchd postawił demona i ten od razu jest za duży

        self.w.run(0.01)
        self.w.run(0.01)

        self.assertIsNone(second.poll())
        self.assertIn("poprzedni restart był przed chwilą", self.w.lines())

    def test_dry_run_only_reports(self):
        p = self.w.target()

        self.w.run(0.01, "--dry-run")
        self.w.run(0.01, "--dry-run")

        self.assertIsNone(p.poll())
        self.assertIn("DRY-RUN", self.w.lines())

    def test_missing_daemon_is_logged_not_fatal(self):
        r = self.w.run(0.01)

        self.assertEqual(r.returncode, 0)
        self.assertIn("brak procesu", self.w.lines())


if __name__ == "__main__":
    unittest.main()
