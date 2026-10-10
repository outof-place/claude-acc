"""Nic nie pisze bajtkodu w paczce claude-acc, którą wozi Pod.

Pod wozi claude-acc w Contents/Resources/claude-acc podpisanej Pod.app; __pycache__ w środku łamie
pieczęć aplikacji i macOS mówi, że Pod.app jest uszkodzona (2026-10-10). Test buduje paczkę tak jak
wydanie (scripts/payload.sh, z atrapami binarek), kładzie ją w udawanej Pod.app (do zapisu, żeby każdy
zapis było widać: tylko do odczytu Python po cichu by go pominął), uruchamia z niej setup.sh,
perf-root.sh, acc.py i skrypt wprost, w osobnym HOME z atrapami launchctl, pkill, open, codesign
i ditto, i sprawdza, że w aplikacji nie przybył ani nie zmienił się żaden plik.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_no_bytecode
"""

import glob
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = "#!/bin/sh\necho \"$(basename \"$0\") $*\" >> \"$FAKE_LOG\"\nexit 0\n"
# prawdziwy interpreter (nie shim /usr/bin/python3, który pod nazwą `python` woła instalator narzędzi)
PYTHON = os.path.realpath(sys.executable)
PRODUCTS = ("ClaudeAcc", "fanctl", "claude-acc-hook", "claude-acc-pause", "claude-acc-desktop", "pod-acc-run",
            "pod-rootd", "pod-rootctl")


def plain_python():
    """Interpreter, który pisze bajtkod obok źródeł (bez domyślnego pycache_prefix, który Python
    Apple'a ma w ~/Library/Caches): jak uv-owy CPython w $STATE/python, który na tym Macu pisał w Pod.app."""
    candidates = [PYTHON, shutil.which("python3") or ""]
    candidates += sorted(glob.glob(os.path.expanduser("~/.local/share/uv/python/cpython-3.*/bin/python3")), reverse=True)
    for path in candidates:
        if not path or not os.access(path, os.X_OK):
            continue
        # prefiks Pythona Apple'a wisi na HOME: bez HOME w środowisku wychodzi None
        done = subprocess.run([path, "-c", "import sys; print(sys.pycache_prefix)"], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()})
        if done.returncode == 0 and done.stdout.strip() == "None":
            return os.path.realpath(path)
    return None


def snapshot(top):
    """{ścieżka względna: (rodzaj, rozmiar, mtime_ns)} wszystkiego pod `top`."""
    found = {}
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            found[os.path.relpath(path, top)] = (info.st_mode >> 12, info.st_size, info.st_mtime_ns)
    return found


class NoBytecodeInThePayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = os.path.realpath(tempfile.mkdtemp(prefix="no-bytecode-payload-"))
        products = os.path.join(cls.work, "products")
        os.makedirs(products)
        for name in PRODUCTS:
            with open(os.path.join(products, name), "w") as f:
                f.write(f"fake {name}\n")
        done = subprocess.run(["/bin/bash", os.path.join(ROOT, "scripts/payload.sh"), "--products", products,
                               "--out", os.path.join(cls.work, "out"), "--version", "9.9.9"],
                              env=dict(os.environ, PAYLOAD_NO_SIGN="1"), capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        cls.payload = os.path.join(cls.work, "out", "claude-acc")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, True)

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="no-bytecode-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pod = os.path.join(self.dir, "Pod.app")
        self.src = os.path.join(self.pod, "Contents/Resources/claude-acc")
        shutil.copytree(self.payload, self.src, symlinks=True)
        self.menu = os.path.join(self.src, "Pod Menu.app")
        with open(os.path.join(self.pod, "Contents/Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "codes.pod.app", "CFBundleName": "Pod"}, f)
        self.home = os.path.join(self.dir, "home")
        self.state = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.state)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        for name in ("pkill", "launchctl", "open", "codesign", "ditto"):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(GUARD)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.env = dict(os.environ, HOME=self.home, PATH=self.bin + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
                        FAKE_LOG=os.path.join(self.dir, "calls.log"), CLAUDE_CONFIG_DIR=os.path.join(self.home, ".claude"),
                        CLAUDE_ACC_ALLOW_FOREIGN_HOME="1", CLAUDE_ACC_NO_HOOKS="1", SCHED_OFF="1")
        for key in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"):
            self.env.pop(key, None)  # bez nich: test ma zobaczyć każdy zapis, którego nie zatrzymał claude-acc
        self.before = snapshot(self.pod)

    def run_here(self, argv):
        done = subprocess.run(argv, env=self.env, cwd=self.dir, capture_output=True, text=True, timeout=240)
        return done.returncode, done.stdout + done.stderr

    def assert_bundle_untouched(self):
        after = snapshot(self.pod)
        added = sorted(set(after) - set(self.before))
        changed = sorted(p for p in self.before if p in after and after[p] != self.before[p])
        self.assertEqual((added, changed), ([], []))

    def test_every_python_folder_of_the_payload_has_the_sentinel(self):
        folders = {os.path.dirname(p) for p in glob.glob(os.path.join(self.payload, "**", "*.py"), recursive=True)}
        self.assertIn(self.payload, folders)
        for folder in folders:
            self.assertTrue(os.path.isfile(os.path.join(folder, "__pycache__")), folder)

    def test_setup_from_the_bundle(self):
        rc, out = self.run_here(["/bin/bash", os.path.join(self.src, "setup.sh"), "--app", self.menu, "--owner", "pod",
                                 "--owner-app", self.pod, "--pod-agents", "--python", plain_python() or PYTHON])
        self.assertEqual(rc, 0, out)
        self.assert_bundle_untouched()
        # w $STATE bajtkod wolno: wartownik z paczki tam nie przechodzi
        self.assertFalse([p for p in glob.glob(os.path.join(self.state, "**", "__pycache__"), recursive=True)
                          if os.path.isfile(p)])

    def test_perf_root_from_the_bundle(self):
        # `claude-acc perf-root iogpu status` biegnie z `source`, czyli z Pod.app: perf.py stamtąd importuje
        # orcahost, janitor i owner
        rc, out = self.run_here(["/bin/bash", os.path.join(self.src, "perf-root.sh"), "iogpu", "status"])
        self.assertEqual(rc, 0, out)
        self.assertIn("iogpu.wired_limit_mb", out)
        self.assert_bundle_untouched()

    def test_python_without_a_cache_prefix_from_the_bundle(self):
        python = plain_python()
        if not python:
            self.skipTest("brak interpretera bez domyślnego pycache_prefix")
        # sprawdzian interpretera: bez wartownika ta sama paczka dostaje __pycache__
        bare = os.path.join(self.dir, "bare")
        shutil.copytree(self.payload, bare, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run([python, os.path.join(bare, "perf.py"), "status", "--json"], env=self.env,
                       capture_output=True, timeout=240)
        self.assertTrue(os.path.isdir(os.path.join(bare, "__pycache__")), "ten interpreter nie pisze bajtkodu")
        for argv in ([python, os.path.join(self.src, "acc.py"), "perf", "status", "--json"],
                     [python, os.path.join(self.src, "perf.py"), "status", "--json"],
                     [python, "-I", os.path.join(self.src, "orcahost.py")]):
            with self.subTest(argv=argv[1:]):
                rc, out = self.run_here(argv)
                self.assertEqual(rc, 0, out)
                self.assert_bundle_untouched()


if __name__ == "__main__":
    unittest.main()
