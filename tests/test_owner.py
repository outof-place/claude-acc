"""Testy owner.py, odmowy w setup.sh i paczki scripts/payload.sh dla Pod.

setup.sh biegnie w osobnym HOME z podróbkami pkill, launchctl i open na początku PATH, więc nawet
błąd w teście nie dotknie działającej aplikacji ani automatów. Paczka powstaje z podrobionych
produktów Swift (--products) bez podpisu, w katalogu testu.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location("acc_owner", os.path.join(ROOT, "owner.py"))
O = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(O)

GUARD = "#!/bin/sh\necho \"$(basename \"$0\") $*\" >> \"$FAKE_LOG\"\nexit 0\n"


class OwnerFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="owner-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, value in {"STATE_DIR": self.dir, "OWNER_PATH": os.path.join(self.dir, "owner.json")}.items():
            patcher = mock.patch.object(O, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_file_means_homebrew(self):
        self.assertIsNone(O.read())
        self.assertEqual(O.main(["check"]), 0)

    def test_pod_owns_it(self):
        O.write("pod", "1.26.0", "/Applications/Pod.app")
        self.assertEqual(O.read()["owner"], "pod")
        with mock.patch("sys.stderr"):
            self.assertEqual(O.main(["check"]), O.EXIT_OWNED)
        self.assertEqual(O.main(["check", "--as", "pod"]), 0)

    def test_garbage_and_unknown_owner_count_as_no_owner(self):
        for text in ("{", '{"owner": "someone"}', "[]"):
            with open(O.OWNER_PATH, "w") as f:
                f.write(text)
            self.assertIsNone(O.read(), text)
        with self.assertRaises(ValueError):
            O.write("someone", "1")

    def test_clear(self):
        O.write("pod", "1")
        with mock.patch("sys.stdout"):
            O.main(["clear"])
        self.assertFalse(os.path.exists(O.OWNER_PATH))


class SetupOwnerTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="setup-owner-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        self.state = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.state)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        for name in ("pkill", "launchctl", "open", "codesign", "ditto"):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(GUARD)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.log = os.path.join(self.dir, "calls.log")

    def setup(self, *args):
        env = dict(os.environ, HOME=self.home, PATH=self.bin + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
                   FAKE_LOG=self.log, CLAUDE_CONFIG_DIR=os.path.join(self.home, ".claude"))
        done = subprocess.run(["/bin/bash", os.path.join(ROOT, "setup.sh"), *args], env=env,
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr

    def own(self):
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "pod", "version": "1.26.0", "app": "/Applications/Pod.app"}, f)

    def calls(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as f:
            return f.read()

    def test_without_owner_nothing_changes(self):
        rc, out = self.setup()
        self.assertEqual(rc, 2)
        self.assertIn("brak aplikacji", out)

    def test_homebrew_setup_refuses_when_pod_owns_it(self):
        self.own()
        for args in ((), ("--app", "/x"), ("--uninstall",)):
            rc, out = self.setup(*args)
            self.assertEqual(rc, 3, args)
            self.assertIn("należy teraz do Pod", out)
        self.assertEqual(self.calls(), "")  # ani pkill, ani launchctl
        self.assertTrue(os.path.exists(os.path.join(self.state, "owner.json")))

    def test_the_owner_gets_past_the_check(self):
        self.own()
        rc, out = self.setup("--owner", "pod", "--owner-app", "/Applications/Pod.app")
        self.assertEqual(rc, 2)  # dalej niż odmowa: brak --app
        self.assertIn("brak aplikacji", out)

    def test_the_owner_uninstalls_and_drops_the_mark(self):
        self.own()
        rc, _ = self.setup("--owner", "pod", "--uninstall")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "owner.json")))
        self.assertIn("pkill -x ClaudeAcc", self.calls())


class PayloadTest(unittest.TestCase):
    def test_payload_layout_version_and_tarball(self):
        work = os.path.realpath(tempfile.mkdtemp(prefix="payload-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        products = os.path.join(work, "products")
        os.makedirs(products)
        for name in ("ClaudeAcc", "fanctl", "claude-acc-hook", "claude-acc-pause", "claude-acc-desktop"):
            with open(os.path.join(products, name), "w") as f:
                f.write(f"fake {name}\n")
        out = os.path.join(work, "out")
        done = subprocess.run(["/bin/bash", os.path.join(ROOT, "scripts/payload.sh"), "--products", products, "--out", out,
                               "--version", "9.9.9"], env=dict(os.environ, PAYLOAD_NO_SIGN="1"),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = os.path.join(out, "claude-acc")
        names = set(os.listdir(payload))
        for expected in ("setup.sh", "accswitch.py", "owner.py", "awake.py", "orcaplugin.py", "launchd", "hooks",
                         "orca-plugin", "Claude Acc.app", "fanctl", "claude-acc-hook", "claude-acc-pause",
                         "claude-acc-desktop", "VERSION", "payload.json"):
            self.assertIn(expected, names)
        self.assertNotIn("test", os.listdir(os.path.join(payload, "orca-plugin")))
        self.assertFalse([p for p, _, _ in os.walk(payload) if p.endswith("__pycache__")])
        with open(os.path.join(payload, "VERSION")) as f:
            self.assertEqual(f.read().strip(), "9.9.9")
        self.assertTrue(os.path.isfile(os.path.join(payload, "Claude Acc.app/Contents/MacOS/ClaudeAcc")))
        tarball = os.path.join(out, "claude-acc-payload-9.9.9.tar.gz")
        with open(tarball + ".sha256") as f:
            digest, name = f.read().split()
        self.assertEqual(name, os.path.basename(tarball))
        import hashlib
        with open(tarball, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest)
        with tarfile.open(tarball) as tar:
            members = tar.getnames()
        self.assertIn("claude-acc/VERSION", members)
        self.assertFalse([m for m in members if os.path.basename(m).startswith("._")])

    def test_missing_product_fails(self):
        work = tempfile.mkdtemp(prefix="payload-test-")
        self.addCleanup(shutil.rmtree, work, True)
        done = subprocess.run(["/bin/bash", os.path.join(ROOT, "scripts/payload.sh"), "--products", work, "--out", work],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("brak produktu Swift", done.stderr)


if __name__ == "__main__":
    unittest.main()
