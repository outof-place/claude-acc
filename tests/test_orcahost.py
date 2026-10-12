"""Testy orcahost.py: który host (Orca albo Pod) i jego ścieżki, na atrapach pakietów i katalogów.

Prawdziwe /Applications i ~/Library są dla testów niewidoczne: Pod leży w katalogu tymczasowym,
a pod_apps wskazuje tylko tam. Na końcu straż: dosłowne ścieżki Orki żyją tylko w orcahost.py.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests -p test_orcahost.py
"""

import os
import plistlib
import shutil
import socket
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
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import orcahost  # noqa: E402

POD_SERVICE = "Pod Claude Code Managed Credentials"


def make_bundle(root, name="Pod", bundle_id="codes.pod.app", cli="podx", declared=None, binary=False, executable=None):
    """Pakiet .app z Info.plist, CLI w Resources/bin i głównym plikiem wykonywalnym."""
    app = os.path.join(root, name + ".app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
    info = {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleExecutable": executable or name,
        "CFBundleIdentifier": bundle_id,
        # zagnieżdżony słownik z kluczem o tej samej nazwie nie może podmienić nazwy z korzenia
        "CFBundleDocumentTypes": [{"CFBundleName": "nested", "CFBundleTypeName": "Folder"}],
    }
    if declared:
        info["ClaudeAccHost"] = declared
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML)
    if cli:
        os.makedirs(os.path.join(app, "Contents", "Resources", "bin"), exist_ok=True)
        path = os.path.join(app, "Contents", "Resources", "bin", cli)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
    return app


def lock(user_data, pid):
    """SingletonLock Electronu: link do "<host>-<pid>" w katalogu danych działającej aplikacji."""
    os.makedirs(user_data, exist_ok=True)
    path = os.path.join(user_data, "SingletonLock")
    if os.path.lexists(path):
        os.remove(path)
    os.symlink(f"{socket.gethostname()}-{pid}", path)


def dead_pid():
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    return proc.pid


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="orcahost-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.apps = os.path.join(self.home, "Applications")
        os.makedirs(self.apps)
        self.pods = []
        patcher = mock.patch.object(orcahost, "pod_apps", side_effect=lambda home=None: list(self.pods))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(orcahost, "owned_by_pod", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def install_pod(self, **kw):
        app = make_bundle(self.apps, **kw)
        self.pods.append(app)
        return app

    def resolve(self, **env):
        return orcahost.resolve(env=env, home=self.home)


class OrcaDefaultsTest(Fixture):
    def test_without_pod_everything_is_orca_as_before(self):
        host = self.resolve()
        self.assertEqual(host, orcahost.orca(self.home))
        self.assertEqual(host.kind, "orca")
        self.assertEqual(host.user_data, os.path.join(self.home, "Library", "Application Support", "orca"))
        self.assertEqual(host.app, os.path.join("/Applications", "Orca.app"))
        self.assertEqual(host.keychain_service, "Orca Claude Code Managed Credentials")
        self.assertEqual(host.cli, "orca")
        self.assertEqual(host.hooks, ".orca/agent-hooks")
        self.assertEqual((host.data_file, host.runtime_file), ("orca-data.json", "orca-runtime.json"))
        self.assertEqual(orcahost.main_marker(host), "Orca.app/Contents/MacOS/Orca")
        self.assertEqual(orcahost.known(env={}, home=self.home), [host])

    def test_orca_bundle_describes_exactly_the_defaults(self):
        app = make_bundle(self.apps, name="Orca", bundle_id=orcahost.ORCA_BUNDLE_ID, cli="orca")
        self.assertEqual(orcahost.from_bundle(app, self.home), orcahost.orca(self.home)._replace(app=app))

    @unittest.skipUnless(os.path.isdir(orcahost.orca().app), "brak Orki w /Applications")
    def test_installed_orca_matches_the_defaults(self):
        self.assertEqual(orcahost.from_bundle(orcahost.orca().app), orcahost.orca())

    def test_orca_cli_comes_from_path_only(self):
        with mock.patch("shutil.which", return_value="/usr/local/bin/orca") as which:
            self.assertEqual(orcahost.cli_path(orcahost.orca(self.home)), "/usr/local/bin/orca")
        which.assert_called_once_with("orca")


class PodBundleTest(Fixture):
    def test_identity_from_info_plist(self):
        host = orcahost.from_bundle(self.install_pod(), self.home)
        self.assertEqual(host.kind, "pod")
        self.assertEqual((host.name, host.executable, host.bundle_id), ("Pod", "Pod", "codes.pod.app"))
        self.assertEqual(host.user_data, os.path.join(self.home, "Library", "Application Support", "pod"))
        self.assertEqual(host.cli, "podx")
        # czego pakiet nie mówi, zostaje po Orce: fork trzyma jej nazwy, dopóki ich nie zmieni
        self.assertEqual(host.keychain_service, orcahost.orca().keychain_service)
        self.assertEqual(host.hooks, ".orca/agent-hooks")
        self.assertEqual(orcahost.main_marker(host), "Pod.app/Contents/MacOS/Pod")

    def test_declared_values_win(self):
        declared = {"userData": "~/Library/Application Support/Pod Dev", "cli": "podx-canary", "keychainService": POD_SERVICE,
                    "hooksDir": "~/.pod/agent-hooks", "dataFile": "pod-data.json", "runtimeFile": "pod-runtime.json",
                    "envPrefix": "POD_"}
        for binary in (False, True):
            with self.subTest(binary=binary):
                app = make_bundle(tempfile.mkdtemp(dir=self.home), declared=declared, binary=binary)
                host = orcahost.from_bundle(app, self.home)
                self.assertEqual(host.user_data, os.path.join(self.home, "Library", "Application Support", "Pod Dev"))
                self.assertEqual((host.cli, host.keychain_service, host.hooks), ("podx-canary", POD_SERVICE, ".pod/agent-hooks"))
                self.assertEqual((host.data_file, host.runtime_file), ("pod-data.json", "pod-runtime.json"))
                self.assertEqual(host.env_prefix, "POD_")
        # hooksDir jako pełna ścieżka w HOME to ten sam katalog względem HOME
        app = make_bundle(tempfile.mkdtemp(dir=self.home), declared={"hooksDir": os.path.join(self.home, ".pod/agent-hooks/")})
        self.assertEqual(orcahost.from_bundle(app, self.home).hooks, ".pod/agent-hooks")
        self.assertEqual(orcahost.from_bundle(make_bundle(tempfile.mkdtemp(dir=self.home)), self.home).env_prefix, "ORCA_")

    def test_unpacked_package_json_names_the_user_data_folder(self):
        app = self.install_pod()
        os.makedirs(os.path.join(app, "Contents", "Resources", "app"))
        with open(os.path.join(app, "Contents", "Resources", "app", "package.json"), "w") as f:
            f.write('{"name": "pod-ide", "productName": "Pod IDE"}')
        self.assertEqual(orcahost.from_bundle(app, self.home).user_data,
                         os.path.join(self.home, "Library", "Application Support", "Pod IDE"))

    def test_broken_or_missing_plist_falls_back_to_the_bundle_name(self):
        app = os.path.join(self.apps, "Pod.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            f.write(b"bplist00 garbage")
        host = orcahost.from_bundle(app, self.home)
        self.assertEqual((host.kind, host.name, host.executable, host.cli), ("pod", "Pod", "Pod", "orca"))

    def test_pod_cli_comes_from_its_bundle_first(self):
        app = self.install_pod()
        host = orcahost.from_bundle(app, self.home)
        with mock.patch("shutil.which", return_value="/usr/local/bin/podx"):
            self.assertEqual(orcahost.cli_path(host), os.path.join(app, "Contents", "Resources", "bin", "podx"))
        # pakiet bez CLI: to, co jest w PATH
        os.remove(os.path.join(app, "Contents", "Resources", "bin", "podx"))
        with mock.patch("shutil.which", return_value="/usr/local/bin/podx"):
            self.assertEqual(orcahost.cli_path(host), "/usr/local/bin/podx")


class ResolveTest(Fixture):
    def setUp(self):
        super().setUp()
        self.orca = orcahost.orca(self.home)
        os.makedirs(self.orca.user_data)

    def test_installed_pod_never_started_leaves_orca(self):
        self.install_pod()
        self.assertEqual(self.resolve().kind, "orca")

    def test_pod_with_its_data_wins_when_nothing_runs(self):
        pod = orcahost.from_bundle(self.install_pod(), self.home)
        os.makedirs(pod.user_data)
        self.assertEqual(self.resolve(), pod)

    def test_running_orca_beats_an_idle_pod(self):
        pod = orcahost.from_bundle(self.install_pod(), self.home)
        os.makedirs(pod.user_data)
        lock(self.orca.user_data, os.getpid())
        self.assertEqual(self.resolve().kind, "orca")

    def test_running_pod_beats_running_orca(self):
        pod = orcahost.from_bundle(self.install_pod(), self.home)
        lock(self.orca.user_data, os.getpid())
        lock(pod.user_data, os.getpid())
        self.assertEqual(self.resolve(), pod)

    def test_stale_lock_is_not_running(self):
        pod = orcahost.from_bundle(self.install_pod(), self.home)
        lock(pod.user_data, dead_pid())
        lock(self.orca.user_data, os.getpid())
        self.assertFalse(orcahost.running(pod))
        self.assertEqual(self.resolve().kind, "orca")

    def test_pod_alone_on_the_mac(self):
        os.rmdir(self.orca.user_data)
        self.install_pod()
        self.assertEqual(self.resolve().kind, "pod")

    def test_pins_and_explicit_paths(self):
        pod_app = self.install_pod()
        pod = orcahost.from_bundle(pod_app, self.home)
        lock(pod.user_data, os.getpid())
        self.assertEqual(self.resolve(CLAUDE_ACC_HOST="orca"), self.orca)
        os.remove(os.path.join(pod.user_data, "SingletonLock"))
        self.assertEqual(self.resolve(CLAUDE_ACC_HOST="pod"), pod)
        other = make_bundle(tempfile.mkdtemp(dir=self.home), name="Pod Canary", bundle_id="codes.pod.canary")
        for key in orcahost.APP_ENV:
            with self.subTest(key=key):
                self.assertEqual(self.resolve(**{key: other}).bundle_id, "codes.pod.canary")
        data = os.path.join(self.home, "elsewhere")
        self.assertEqual(self.resolve(CLAUDE_ACC_HOST_DATA=data).user_data, data)

    def test_session_instance_moves_only_the_runtime_socket(self):
        dev = os.path.join(self.home, "orca-dev")
        env = {orcahost.USER_DATA_ENV: dev}
        host = orcahost.resolve(env=env, home=self.home)
        self.assertEqual(host.user_data, self.orca.user_data)
        self.assertEqual(orcahost.runtime_path(host, env), os.path.join(dev, "orca-runtime.json"))
        self.assertEqual(orcahost.runtime_path(host, {}), os.path.join(self.orca.user_data, "orca-runtime.json"))
        # Pod ustawia POD_USER_DATA_PATH; przy obu wygrywa przedrostek hosta
        pod = orcahost.from_bundle(self.install_pod(declared={"envPrefix": "POD_"}), self.home)
        both = {"POD_USER_DATA_PATH": dev + "-pod", "ORCA_USER_DATA_PATH": dev}
        self.assertEqual(orcahost.runtime_path(pod, both), os.path.join(dev + "-pod", "orca-runtime.json"))
        self.assertEqual(orcahost.runtime_path(host, both), os.path.join(dev, "orca-runtime.json"))
        self.assertEqual(orcahost.runtime_path(host, {"POD_USER_DATA_PATH": dev}), os.path.join(dev, "orca-runtime.json"))

    def test_env_reads_either_prefix(self):
        self.assertEqual(orcahost.env("PANE_KEY", {"POD_PANE_KEY": "p"}), "p")
        self.assertEqual(orcahost.env("PANE_KEY", {"ORCA_PANE_KEY": "o", "POD_PANE_KEY": "p"}), "o")
        self.assertIsNone(orcahost.env("PANE_KEY", {"ORCA_PANE_KEY": ""}))
        self.assertIn("POD_PAIRING_CODE", orcahost.REMOTE_ENV)
        self.assertIn("ORCA_PAIRING_CODE", orcahost.REMOTE_ENV)

    def test_known_lists_every_host_once(self):
        pod = orcahost.from_bundle(self.install_pod(declared={"keychainService": POD_SERVICE,
                                                              "hooksDir": "~/.pod/agent-hooks"}), self.home)
        hosts = orcahost.known(env={}, home=self.home)
        self.assertEqual(hosts, [self.orca, pod])
        self.assertEqual(orcahost.keychain_services(env={}, home=self.home), [self.orca.keychain_service, POD_SERVICE])
        self.assertEqual(orcahost.hook_hosts(hosts), {".orca/agent-hooks": "Orca", ".pod/agent-hooks": "Pod"})


class OwnerTest(unittest.TestCase):
    """owner.json (owner.py) z aplikacją Pod spoza /Applications: pierwsze miejsce, gdzie szukamy Pod."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="orcahost-owner-")
        self.addCleanup(shutil.rmtree, self.home, True)
        import owner

        self.owner = owner

    def test_owner_app_is_the_first_pod_candidate(self):
        app = make_bundle(os.path.join(self.home, "Downloads"))
        with mock.patch.object(self.owner, "read", return_value={"owner": "pod", "version": "1.29.0", "app": app + "/"}):
            self.assertEqual(orcahost.pod_apps(self.home)[0], app)
            host = orcahost.resolve(env={}, home=self.home)  # Orca bez katalogu danych: Pod
        self.assertEqual((host.kind, host.app), ("pod", app))

    def test_owner_names_the_shared_hook_folder(self):
        both = {".orca/agent-hooks": "Pod", ".pod/agent-hooks": "Pod"}
        with mock.patch.object(self.owner, "read", return_value={"owner": "pod", "version": "1", "app": None}):
            self.assertEqual(orcahost.hook_hosts([orcahost.orca(self.home)]), both)
            # Pod, który dzieli ~/.orca z Orką (bez własnego hooksDir), też podpisuje go sobą
            shared = orcahost.from_bundle(make_bundle(os.path.join(self.home, "Apps")), self.home)
            self.assertEqual(orcahost.hook_hosts([orcahost.orca(self.home), shared]), both)
            # Pod z własnym ~/.pod (od 2026-10-10): ~/.orca wraca do Orki
            moved = orcahost.from_bundle(make_bundle(os.path.join(self.home, "Apps2"), declared={"hooksDir": "~/.pod/agent-hooks"}), self.home)
            self.assertEqual(orcahost.hook_hosts([orcahost.orca(self.home), moved]),
                             {".orca/agent-hooks": "Orca", ".pod/agent-hooks": "Pod"})
        with mock.patch.object(self.owner, "read", return_value=None):
            self.assertEqual(orcahost.hook_hosts([orcahost.orca(self.home)]), {".orca/agent-hooks": "Orca", ".pod/agent-hooks": "Pod"})

    def test_without_owner_json_the_usual_places(self):
        for owned in (None, {"owner": "pod", "version": "1", "app": None}):
            with mock.patch.object(self.owner, "read", return_value=owned):
                self.assertEqual(orcahost.pod_apps(self.home),
                                 ["/Applications/Pod.app", os.path.join(self.home, "Applications", "Pod.app")])


class AppPatternTest(unittest.TestCase):
    def test_matches_host_bundles_only(self):
        import re

        pattern = re.compile(orcahost.app_pattern({"CLAUDE_ACC_HOST_APP": "/Users/x/Applications/Pod Canary.app"}))
        orca = orcahost.orca().app
        for command in (orcahost.main_path(orcahost.orca()),
                        orca + "/Contents/Frameworks/Orca Helper (Renderer).app/Contents/MacOS/x",
                        "/Applications/Pod.app/Contents/MacOS/Pod --type=gpu",
                        "/Users/x/Applications/Pod Canary.app/Contents/MacOS/Pod Canary"):
            self.assertTrue(pattern.search(command), command)
        for command in ("/Applications/iPod.app/Contents/MacOS/iPod", "/Applications/Killer-Orca.app/x", "node orca.js"):
            self.assertFalse(pattern.search(command), command)


class CliTest(unittest.TestCase):
    def test_prints_the_host_or_one_field(self):
        env = dict(os.environ, CLAUDE_ACC_HOST="orca")
        out = subprocess.run([sys.executable, os.path.join(ROOT, "orcahost.py"), "keychain_service"], env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), orcahost.orca().keychain_service)
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "orcahost.py"), "nope"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)


# dosłowne ścieżki Orki: tylko orcahost.py je zna, reszta pyta resolver (składane, żeby ten plik sam ich
# nie zawierał)
LITERALS = ("Application Support/" + "orca", "/Applications/" + "Orca.app")
CODE = (".py", ".sh", ".swift", ".template", ".plist", ".c", ".rb")


class LiteralGuardTest(unittest.TestCase):
    def test_orca_paths_live_only_in_orcahost(self):
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.split()
        found = []
        for rel in files:
            if rel == "orcahost.py" or not rel.endswith(CODE):
                continue
            with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if any(literal.lower() in line.lower() for literal in LITERALS):
                        found.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(found, [], "ścieżki Orki poza orcahost.py:\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
