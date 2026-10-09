"""Testy awake.py: `claude-acc awake` steruje Stay Awake aplikacji przez claude-acc://awake/...

Prawdziwy `open` zastępuje podróbka na początku PATH: zapisuje URL i odpisuje plik stanu tak, jak
zrobiłaby to aplikacja (pid procesu testu, więc stan wygląda na żywy). Nic nie dotyka prawdziwego
~/.local/share/claude-acc ani aplikacji.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "awake.py")

_spec = importlib.util.spec_from_file_location("acc_awake", SCRIPT)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

# podróbka `open -g claude-acc://awake/<verb>?for=N`: aplikacja zapisuje nowy stan
FAKE_OPEN = r"""#!/usr/bin/python3
import json, os, sys, time, urllib.parse
url = urllib.parse.urlparse(sys.argv[-1])
with open(os.environ["FAKE_OPEN_LOG"], "a") as f:
    f.write(sys.argv[-1] + "\n")
if os.environ.get("FAKE_APP_DEAD"):
    sys.exit(0)
path = os.path.expanduser("~/.local/share/claude-acc/awake-state.json")
try:
    state = json.load(open(path))
except (OSError, ValueError):
    state = {"on": False, "manual": False}
verb = url.path.strip("/")
on = {"on": True, "off": False}.get(verb, not state.get("on"))
seconds = dict(urllib.parse.parse_qsl(url.query)).get("for")
state = {"on": on, "manual": on, "forever": on and not seconds, "hotspot": False, "auto_on_hotspot": True,
         "lid_closed": True, "pid": int(os.environ["FAKE_APP_PID"]), "updated_at": time.time()}
if on and seconds:
    state["until"] = time.time() + float(seconds)
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(state, open(path, "w"))
"""


class DurationTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(A.parse_duration("3600"), 3600)
        self.assertEqual(A.parse_duration("90m"), 5400)
        self.assertEqual(A.parse_duration("1h30m"), 5400)
        self.assertEqual(A.parse_duration("2h"), 7200)
        for bad in ("", "0", "abc", "2x", "1h 30m", "-5"):
            self.assertIsNone(A.parse_duration(bad), bad)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="awake-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "open"), "w") as f:
            f.write(FAKE_OPEN)
        os.chmod(os.path.join(self.bin, "open"), 0o755)
        self.log = os.path.join(self.dir, "open.log")
        self.env = dict(os.environ, HOME=self.home, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_OPEN_LOG=self.log, FAKE_APP_PID=str(os.getpid()))
        self.state_path = os.path.join(self.home, ".local/share/claude-acc/awake-state.json")

    def run_awake(self, *args, **env):
        done = subprocess.run([sys.executable, SCRIPT, *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=30)
        return done.returncode, done.stdout, done.stderr

    def urls(self):
        return open(self.log).read().split() if os.path.exists(self.log) else []

    def test_status_without_state_file(self):
        rc, out, _ = self.run_awake("status", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"on": False, "running": False, "known": False})

    def test_on_for_a_while_then_off(self):
        rc, out, _ = self.run_awake("on", "--for", "90m")
        self.assertEqual(rc, 0, out)
        self.assertIn("włączone jeszcze 1 h 29 min", out)
        rc, out, _ = self.run_awake("status", "--json")
        state = json.loads(out)
        self.assertTrue(state["on"] and state["running"] and state["manual"])
        rc, out, _ = self.run_awake("off")
        self.assertEqual(rc, 0)
        self.assertIn("wyłączone", out)
        self.assertEqual(self.urls(), ["claude-acc://awake/on?for=5400", "claude-acc://awake/off"])

    def test_toggle_flips_the_state(self):
        self.assertEqual(self.run_awake("toggle")[0], 0)
        self.assertTrue(json.load(open(self.state_path))["on"])
        self.assertEqual(self.run_awake("toggle")[0], 0)
        self.assertFalse(json.load(open(self.state_path))["on"])

    def test_dead_app_means_off_and_no_confirmation(self):
        os.makedirs(os.path.dirname(self.state_path))
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        with open(self.state_path, "w") as f:
            json.dump({"on": True, "manual": True, "pid": dead.pid, "updated_at": time.time()}, f)
        rc, out, _ = self.run_awake("status", "--json")
        state = json.loads(out)
        self.assertEqual((state["on"], state["running"]), (False, False))
        A_confirm = A.CONFIRM_S
        start = time.time()
        rc, _, err = self.run_awake("on", FAKE_APP_DEAD="1")
        self.assertEqual(rc, 1)
        self.assertIn("nie potwierdziła", err)
        self.assertGreaterEqual(time.time() - start, A_confirm - 0.5)

    def test_bad_duration(self):
        rc, _, err = self.run_awake("on", "--for", "soon")
        self.assertEqual(rc, 2)
        self.assertIn("--for", err)
        self.assertEqual(self.urls(), [])


if __name__ == "__main__":
    unittest.main()
