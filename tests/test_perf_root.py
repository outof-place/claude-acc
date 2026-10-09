"""Testy perf-root.sh: lista Prywatności Spotlight dla trybu apps-only.

Wykonujemy ten sam heredoc Pythona, który perf-root.sh odpala jako root, ale na atrapie:
plik VolumeConfiguration.plist, katalog domowy i katalog aplikacji leżą w katalogu
tymczasowym. Prawdziwy Spotlight i prawdziwe /Applications są dla testu niewidoczne.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def exclusions_code():
    src = open(os.path.join(ROOT, "perf-root.sh"), encoding="utf-8").read()
    start = src.index("<<'PY'\n", src.index("spotlight_exclusions() {")) + len("<<'PY'\n")
    return src[start:src.index("\nPY\n", start)]


class SpotlightAppsOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def mk(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def run_exclusions(self, cfg, mode, home, apps):
        out = subprocess.run(["/usr/bin/python3", "-", cfg, mode, home, apps],
                             input=exclusions_code(), capture_output=True, text=True,
                             check=True).stdout
        with open(cfg, "rb") as f:
            return json.loads(out), plistlib.load(f)["Exclusions"]

    def test_apps_only_keeps_bundles_and_drops_folders_without_one(self):
        home = self.mk("home")
        self.mk("home", "Documents")
        self.mk("home", "Applications")
        apps = self.mk("Applications")
        self.mk("Applications", "Top.app", "Contents")
        self.mk("Applications", "Vendor", "Tool.app", "Contents")
        self.mk("Applications", "Vendor", "Presets", "deep")
        self.mk("Applications", "Stack", "files", "manager.app", "Contents")
        self.mk("Applications", "Stack", "files", "lib")
        self.mk("Applications", "Plugins", "a", "b")
        os.symlink(os.path.join(apps, "Stack", "files", "manager.app"),
                   os.path.join(apps, "Stack", "manager.app"))
        cfg = os.path.join(self.tmp, "VolumeConfiguration.plist")
        with open(cfg, "wb") as f:
            plistlib.dump({"Exclusions": ["/old"]}, f, fmt=plistlib.FMT_BINARY)

        before, got = self.run_exclusions(cfg, "apps-only", home, apps)

        self.assertEqual(before, ["/old"])
        self.assertIn(os.path.join(home, "Documents"), got)
        self.assertNotIn(os.path.join(home, "Applications"), got)
        self.assertEqual(sorted(p for p in got if p.startswith(apps)), [
            os.path.join(apps, "Plugins"),
            os.path.join(apps, "Stack", "files", "lib"),
            os.path.join(apps, "Vendor", "Presets"),
        ])
        for p in got:
            self.assertFalse(p.endswith(".app"), p)

    def test_undo_restores_the_saved_list_verbatim(self):
        home = self.mk("home")
        apps = self.mk("Applications")
        cfg = os.path.join(self.tmp, "VolumeConfiguration.plist")
        with open(cfg, "wb") as f:
            plistlib.dump({"Exclusions": ["/x"]}, f, fmt=plistlib.FMT_BINARY)
        saved = os.path.join(self.tmp, "saved.json")
        with open(saved, "w") as f:
            json.dump(["/a", "/b"], f)

        _, got = self.run_exclusions(cfg, saved, home, apps)

        self.assertEqual(got, ["/a", "/b"])


if __name__ == "__main__":
    unittest.main()
