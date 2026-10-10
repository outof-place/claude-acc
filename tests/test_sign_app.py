"""sign-app.sh: podpis aplikacji paska menu z hardened runtime i uprawnieniem audio-input.

Pakiet w katalogu tymczasowym z kopią /usr/bin/true zamiast ClaudeAcc, podpis ad hoc
(CLAUDE_ACC_SIGN_ID=-), więc certyfikaty z Pęku kluczy nie biorą udziału.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_sign_app
"""

import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGN = os.path.join(ROOT, "sign-app.sh")
CODESIGN = shutil.which("codesign")


@unittest.skipUnless(CODESIGN, "brak codesign")
class SignAppTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sign-app-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = os.path.join(self.dir, "Claude Acc.app")
        os.makedirs(os.path.join(self.app, "Contents", "MacOS"))
        shutil.copy("/usr/bin/true", os.path.join(self.app, "Contents", "MacOS", "ClaudeAcc"))
        shutil.copy(os.path.join(ROOT, "app", "Info.plist"), os.path.join(self.app, "Contents", "Info.plist"))
        # plik uprawnień idzie do TMPDIR: tu widać, czy po podpisie zniknął
        env = dict(os.environ, CLAUDE_ACC_SIGN_ID="-", TMPDIR=self.dir + "/")
        subprocess.run(["/bin/bash", SIGN, self.app], env=env, capture_output=True, check=True)

    def codesign(self, *args):
        """(stdout, stderr): codesign -d pisze opis na stderr, a uprawnienia na stdout."""
        done = subprocess.run([CODESIGN, *args, self.app], capture_output=True)
        return done.stdout, done.stderr

    def test_hardened_runtime(self):
        self.assertIn(b"runtime", self.codesign("-dv")[1])

    def test_microphone_entitlement_only(self):
        out = self.codesign("-d", "--entitlements", "-", "--xml")[0]
        self.assertEqual(plistlib.loads(out), {"com.apple.security.device.audio-input": True})

    def test_designated_requirement_keeps_the_grants(self):
        self.assertIn(b'designated => identifier "com.filip.claude-acc.menubar"', b"".join(self.codesign("-d", "-r-")))
        self.assertIn(b"satisfies its Designated Requirement", self.codesign("--verify", "--strict", "-v")[1])

    def test_no_entitlements_file_left_behind(self):
        self.assertEqual(sorted(os.listdir(self.dir)), ["Claude Acc.app"])


if __name__ == "__main__":
    unittest.main()
