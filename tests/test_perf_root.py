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

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

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


FAKE_ID = """#!/bin/sh
case "$1" in
  -u) echo "$FAKE_UID" ;;
  -un) echo "$FAKE_USER" ;;
  *) exec /usr/bin/id "$@" ;;
esac
"""
FAKE_STAT = """#!/bin/sh
[ "$*" = "-f %Su /dev/console" ] && { echo "$FAKE_CONSOLE"; exit 0; }
exec /usr/bin/stat "$@"
"""
FAKE_SUDO = """#!/bin/sh
echo "sudo $*"
"""


class SudoUserTest(unittest.TestCase):
    """Czyj stan zmienia perf-root.sh: SUDO_USER, a bez niego (albo z root od sudo wołanego przez roota)
    właściciel konsoli; root to odmowa, bo /var/root nie ma claude-acc. Atrapy id, stat i sudo w PATH."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fakes = os.path.join(self.tmp, "bin")
        os.makedirs(self.fakes)
        for name, body in (("id", FAKE_ID), ("stat", FAKE_STAT), ("sudo", FAKE_SUDO)):
            path = os.path.join(self.fakes, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)

    def env(self, uid, user="tester", console="tester", sudo_user=None):
        env = {"PATH": f"{self.fakes}:/usr/bin:/bin", "HOME": self.tmp, "FAKE_UID": str(uid),
               "FAKE_USER": user, "FAKE_CONSOLE": console}
        if sudo_user is not None:
            env["SUDO_USER"] = sudo_user
        return env

    def user(self, **kw):
        return subprocess.run(["/bin/bash", os.path.join(ROOT, "perf-root.sh"), "user"], env=self.env(**kw),
                              capture_output=True, text=True)

    def test_sudo_user_wins(self):
        done = self.user(uid=0, sudo_user="tester", console="other")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "tester"))

    def test_root_without_a_real_sudo_user_takes_the_console_user(self):
        for sudo_user in (None, "", "root"):
            with self.subTest(sudo_user=sudo_user):
                done = self.user(uid=0, sudo_user=sudo_user, console="tester")
                self.assertEqual((done.returncode, done.stdout.strip()), (0, "tester"), done.stderr)

    def test_refuses_root_as_the_owner(self):
        for sudo_user in (None, "root"):
            with self.subTest(sudo_user=sudo_user):
                done = self.user(uid=0, sudo_user=sudo_user, console="root")
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertIn("czyj jest stan claude-acc", done.stderr)

    def test_plain_user_is_itself(self):
        done = self.user(uid=501, user="tester", console="other")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "tester"))

    def wrapper(self, root_copy=True):
        """Komenda claude-acc z setup.sh; kopia roota (root-run.sh) leży w katalogu testu i tylko się
        przedstawia, a perf-root.sh w `source` (libexec) też, żeby było widać, że go nie woła."""
        src = open(os.path.join(ROOT, "setup.sh"), encoding="utf-8").read()
        start = src.index("<<'EOF'\n", src.index('cat > "$HOME/.local/bin/claude-acc"')) + len("<<'EOF'\n")
        rootdir = os.path.join(self.tmp, "claude-acc-root")
        path = os.path.join(self.tmp, "claude-acc")
        with open(path, "w") as f:
            f.write(src[start:src.index("\nEOF\n", start) + 1].replace("/usr/local/libexec/claude-acc-root", rootdir))
        libexec = os.path.join(self.tmp, "libexec")
        state = os.path.join(self.tmp, ".local", "share", "claude-acc")
        os.makedirs(libexec)
        os.makedirs(state)
        with open(os.path.join(state, "source"), "w") as f:
            f.write(libexec + "\n")
        for folder, name in ((libexec, "perf-root.sh"), (rootdir, "root-run.sh")):
            if folder == rootdir and not root_copy:
                continue
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, name), "w") as f:
                f.write('#!/bin/sh\necho "%s SUDO_USER=${SUDO_USER:-} $*"\n' % name)
            os.chmod(os.path.join(folder, name), 0o755)
        return path, os.path.join(rootdir, "root-run.sh")

    def test_wrapper_under_sudo_keeps_sudo_user(self):
        path, run = self.wrapper()
        under_sudo = subprocess.run(["/bin/sh", path, "perf-root", "spotlight", "apps-only"],
                                    env=self.env(uid=0, sudo_user="tester"), capture_output=True, text=True)
        self.assertEqual(under_sudo.stdout.strip(), "root-run.sh SUDO_USER=tester perf-root spotlight apps-only",
                         under_sudo.stderr)
        plain = subprocess.run(["/bin/sh", path, "perf-root", "spotlight", "apps-only"],
                               env=self.env(uid=501), capture_output=True, text=True)
        self.assertEqual(plain.stdout.strip(), f"sudo {run} perf-root spotlight apps-only", plain.stderr)

    def test_wrapper_never_runs_the_source_copy_under_sudo(self):
        """Bez kopii roota odmowa z poleceniem instalacji, a nie sudo na pliku z `source`."""
        path, _ = self.wrapper(root_copy=False)
        for cmd in (["perf-root", "spotlight", "apps-only"], ["mac", "root-clean", "--dry-run"]):
            with self.subTest(cmd=cmd):
                done = subprocess.run(["/bin/sh", path, *cmd], env=self.env(uid=501), capture_output=True, text=True)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(done.stdout, "")
                self.assertIn("claude-acc root install", done.stderr)

    def test_fsguard_status_reads_the_daemon_without_root(self):
        """`claude-acc fsguard` (status) tylko czyta plistę demona: nic nie woła sudo ani instalatora."""
        path, _ = self.wrapper()
        done = subprocess.run(["/bin/sh", path, "fsguard"], env=self.env(uid=501), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.count("\n"), 1, done.stdout)  # jedna linia, bez przejścia do accswitch
        self.assertTrue(done.stdout.startswith("strażnik fseventsd:"), done.stdout)

    def test_root_outside_the_root_copy_is_refused(self):
        """perf-root.sh i janitor-root.sh z katalogu źródeł pod rootem: odmowa, zanim cokolwiek zmienią."""
        for script, args in (("perf-root.sh", ["spotlight", "apps-only"]), ("janitor-root.sh", [])):
            with self.subTest(script=script):
                env = self.env(uid=0, sudo_user="tester")
                done = subprocess.run(["/bin/bash", os.path.join(ROOT, script), *args], env=env,
                                      capture_output=True, text=True)
                if script == "janitor-root.sh" and os.geteuid() != 0:
                    # janitor-root.sh czyta $EUID powłoki, którego atrapa id nie zmienia
                    self.assertIn("kopię roota", done.stderr)
                    continue
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("tylko z kopii roota", done.stderr)


if __name__ == "__main__":
    unittest.main()
