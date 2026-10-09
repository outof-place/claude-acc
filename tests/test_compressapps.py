"""Testy compressapps.py: kompresja aplikacji roota ze sprawdzeniem podpisu.

Logika przebiegu idzie na atrapach narzędzi (codesign, afsctool, ps), bo prawdziwy przebieg
wymaga roota i aplikacji z /Applications. Jeden test robi prawdziwą kompresję afsctool
i prawdziwe sprawdzenie codesign na pakiecie Dummy.app podpisanym ad hoc w katalogu
tymczasowym.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import contextlib
import io
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import compressapps  # noqa: E402

AFSCTOOL = shutil.which("afsctool", path="/opt/homebrew/bin:/usr/local/bin")
CODESIGN = shutil.which("codesign")


def stats(files=100, compressed=0, logical=1000, used=1000):
    return {"files": files, "compressed": compressed, "logical": logical, "used": used}


class FakeTools:
    """Atrapy narzędzi: zapisują wywołania, wyniki podpisu idą z kolejki."""

    def __init__(self, owners=None, running=(), signatures=None, after=None):
        self.owners = owners or {}
        self.running_set = set(running)
        self.signatures = list(signatures or [])
        self.after = after or stats(compressed=95, used=600)
        self.calls = []
        self.compressed = set()

    def owner(self, bundle):
        return self.owners.get(bundle, 0)

    def stats(self, bundle):
        self.calls.append(("stats", bundle))
        return self.after if bundle in self.compressed else stats()

    def running(self):
        return self.running_set

    def verify(self, bundle):
        self.calls.append(("verify", bundle))
        return self.signatures.pop(0) if self.signatures else (True, "")

    def compress(self, bundle):
        self.calls.append(("compress", bundle))
        self.compressed.add(bundle)
        return True

    def decompress(self, bundle):
        self.calls.append(("decompress", bundle))
        self.compressed.discard(bundle)
        return True

    def modifying(self):
        return [c for c in self.calls if c[0] in ("compress", "decompress")]


class PlanTest(unittest.TestCase):
    def test_running_already_done_and_user_owned_are_skipped(self):
        bundles = ["/Applications/Live.app", "/Applications/Done.app", "/Applications/Mine.app", "/Applications/Word.app"]
        tools = FakeTools(owners={"/Applications/Mine.app": 501}, running={"/Applications/Live.app"})
        tools.stats = lambda b: stats(compressed=95) if b.endswith("Done.app") else stats()
        rows = {os.path.basename(b): reason for b, _, reason in compressapps.plan(bundles, tools)}
        self.assertEqual(rows["Live.app"], "działa")
        self.assertEqual(rows["Done.app"], "już skompresowana")
        self.assertNotIn("Mine.app", rows)  # pakiety użytkownika robi janitor compress
        self.assertIsNone(rows["Word.app"])
        self.assertEqual(tools.modifying(), [])

    def test_apps_filter_by_name_case_insensitive(self):
        bundles = ["/Applications/Microsoft Word.app", "/Applications/Microsoft Excel.app",
                   "/Applications/Adobe Premiere Pro 2026/Adobe Premiere Pro 2026.app"]
        rows = compressapps.plan(bundles, FakeTools(), wanted=["word", "PREMIERE"])
        self.assertEqual(sorted(os.path.basename(b) for b, _, _ in rows),
                         ["Adobe Premiere Pro 2026.app", "Microsoft Word.app"])

    def test_named_user_owned_app_says_why(self):
        tools = FakeTools(owners={"/Applications/Mine.app": 501})
        rows = compressapps.plan(["/Applications/Mine.app"], tools, wanted=["mine"])
        self.assertIn("janitor", rows[0][2])

    def test_outer_bundle_for_helpers(self):
        path = "/Applications/Brave Browser.app/Contents/Frameworks/X.framework/Helpers/H.app/Contents/MacOS/H"
        self.assertEqual(compressapps.outer_bundle(path), "/Applications/Brave Browser.app")
        self.assertIsNone(compressapps.outer_bundle("/usr/bin/true"))


class ProcessTest(unittest.TestCase):
    APP = "/Applications/Word.app"

    def test_good_signature_compresses_and_reports_savings(self):
        tools = FakeTools(signatures=[(True, ""), (True, "")])
        row = compressapps.process(self.APP, tools)
        self.assertEqual(row["status"], "skompresowana")
        self.assertEqual(row["signature"], "dobry")
        self.assertEqual(row["freed"], 400)
        self.assertGreater(row["after"], row["before"])
        self.assertEqual(tools.modifying(), [("compress", self.APP)])

    def test_broken_signature_before_is_left_alone(self):
        tools = FakeTools(signatures=[(False, "a sealed resource is missing or invalid")])
        row = compressapps.process(self.APP, tools)
        self.assertIn("podpis już zepsuty", row["status"])
        self.assertIn("sealed resource", row["signature"])
        self.assertEqual(tools.modifying(), [])

    def test_signature_broken_by_compression_is_rolled_back(self):
        tools = FakeTools(signatures=[(True, ""), (False, "invalid"), (True, "")])
        row = compressapps.process(self.APP, tools)
        self.assertTrue(row.get("rolled_back"))
        self.assertEqual(row["signature"], "dobry po cofnięciu")
        self.assertEqual(tools.modifying(), [("compress", self.APP), ("decompress", self.APP)])
        self.assertEqual(row["freed"], 0)

    def test_rollback_that_does_not_help_is_reported_loudly(self):
        tools = FakeTools(signatures=[(True, ""), (False, "invalid"), (False, "still invalid")])
        row = compressapps.process(self.APP, tools)
        self.assertTrue(row["signature"].startswith("NADAL ZŁY"))

    def test_app_launched_meanwhile_is_skipped(self):
        tools = FakeTools(running={self.APP})
        row = compressapps.process(self.APP, tools)
        self.assertIn("uruchomiła się", row["status"])
        self.assertEqual(tools.modifying(), [])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tools = FakeTools()
        self.orig = compressapps.Tools, compressapps.app_bundles
        compressapps.Tools = lambda afsctool, threads: self.tools
        compressapps.app_bundles = lambda root=compressapps.APPS_ROOT: ["/Applications/Word.app"]
        self.addCleanup(self.restore)

    def restore(self):
        compressapps.Tools, compressapps.app_bundles = self.orig

    def test_dry_run_changes_nothing(self):
        rc = compressapps.main(["run", "--dry-run", "--afsctool", "/bin/sh"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tools.modifying(), [])
        self.assertNotIn("verify", [c[0] for c in self.tools.calls])

    def test_missing_afsctool(self):
        orig = compressapps.find_afsctool
        compressapps.find_afsctool = lambda: None
        try:
            self.assertEqual(compressapps.main(["plan"]), 1)
        finally:
            compressapps.find_afsctool = orig

    @unittest.skipIf(os.geteuid() == 0, "test sprawdza odmowę bez roota")
    def test_run_without_root_refuses(self):
        self.assertEqual(compressapps.main(["run", "--afsctool", "/bin/sh"]), 1)
        self.assertEqual(self.tools.modifying(), [])


    def run_as_root(self, *args):
        out = io.StringIO()
        with mock.patch.object(compressapps.os, "geteuid", return_value=0), contextlib.redirect_stdout(out):
            rc = compressapps.main(["run", "--afsctool", "/bin/sh", *args])
        return rc, out.getvalue()

    def test_no_match_says_so(self):
        rc, out = self.run_as_root("--apps", "Nope")
        self.assertEqual(rc, 0)
        self.assertIn("nie pasuje do --apps Nope", out)
        self.assertEqual(self.tools.modifying(), [])

    def test_real_run_lists_skipped_apps(self):
        self.tools.running_set = {"/Applications/Word.app"}
        rc, out = self.run_as_root()
        self.assertEqual(rc, 0)
        self.assertIn("pominięte: Word.app (działa)", out)
        self.assertEqual(self.tools.modifying(), [])


class ArgsTest(unittest.TestCase):
    def test_parse(self):
        a = compressapps.parse_args(["run", "--apps", "Word, Excel ,", "--threads", "3"])
        self.assertEqual(a.mode, "run")
        self.assertEqual([w.strip() for w in a.wanted], ["Word", "Excel"])
        self.assertEqual(a.threads, 3)
        self.assertFalse(a.dry_run)

    def test_bad_threads_and_mode(self):
        with open(os.devnull, "w") as null:
            err, sys.stderr = sys.stderr, null
            try:
                for argv in (["--threads", "0"], ["explode"]):
                    with self.assertRaises(SystemExit):
                        compressapps.parse_args(argv)
            finally:
                sys.stderr = err


@unittest.skipUnless(AFSCTOOL and CODESIGN, "brak afsctool (brew install afsctool) albo codesign")
class RealBundleTest(unittest.TestCase):
    """Prawdziwy afsctool i codesign na pakiecie podpisanym ad hoc (bez roota: pakiet jest nasz)."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="compressapps-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = os.path.join(self.root, "Dummy.app")
        contents = os.path.join(self.app, "Contents")
        os.makedirs(os.path.join(contents, "MacOS"))
        os.makedirs(os.path.join(contents, "Resources"))
        with open(os.path.join(contents, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleExecutable": "dummy", "CFBundleIdentifier": "eu.claude-acc.test.dummy",
                           "CFBundleName": "Dummy", "CFBundlePackageType": "APPL"}, f)
        shutil.copy("/usr/bin/true", os.path.join(contents, "MacOS", "dummy"))
        self.text = os.path.join(contents, "Resources", "strings.txt")
        with open(self.text, "w") as f:
            f.write("compressible resource line\n" * 20000)
        sign = subprocess.run([CODESIGN, "--force", "--deep", "--sign", "-", self.app], capture_output=True)
        if sign.returncode:
            self.skipTest("codesign ad hoc nie zadziałał")

    def test_compresses_and_keeps_signature(self):
        tools = compressapps.Tools(AFSCTOOL, 2)
        tools.running = lambda: set()
        self.assertTrue(tools.verify(self.app)[0])
        row = compressapps.process(self.app, tools)
        self.assertEqual(row["status"], "skompresowana", row)
        self.assertEqual(row["signature"], "dobry")
        self.assertTrue(os.lstat(self.text).st_flags & stat.UF_COMPRESSED)
        self.assertGreater(row["freed"], 0)
        with open(self.text) as f:
            self.assertTrue(f.read().startswith("compressible resource line"))

    def test_broken_bundle_is_not_compressed(self):
        with open(self.text, "a") as f:
            f.write("tampered\n")  # zmiana zapieczętowanego zasobu psuje podpis
        tools = compressapps.Tools(AFSCTOOL, 2)
        tools.running = lambda: set()
        row = compressapps.process(self.app, tools)
        self.assertIn("podpis już zepsuty", row["status"])
        self.assertFalse(os.lstat(self.text).st_flags & stat.UF_COMPRESSED)


if __name__ == "__main__":
    unittest.main()
