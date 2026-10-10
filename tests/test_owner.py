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

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location("acc_owner", os.path.join(ROOT, "owner.py"))
O = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(O)

GUARD = "#!/bin/sh\necho \"$(basename \"$0\") $*\" >> \"$FAKE_LOG\"\nexit 0\n"
# prawdziwy interpreter (nie shim /usr/bin/python3, który pod nazwą `python` woła instalator narzędzi)
PYTHON = os.path.realpath(sys.executable)


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

    def test_menu_app_is_pods_when_it_runs_the_agents(self):
        legacy = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(self.dir))), "Applications", "Claude Acc.app")
        self.assertEqual(O.menu_app(), legacy)  # brew: kopia setup.sh w ~/Applications
        menu = os.path.join(self.dir, "Pod.app/Contents/Library/LoginItems/Pod Menu.app")
        O.write("pod", "1.31.0", "/Applications/Pod.app", menu)
        self.assertEqual(O.read()["menu"], menu)
        self.assertEqual(O.menu_app(), legacy)  # Pod Menu.app jeszcze nie istnieje
        os.makedirs(menu)
        self.assertEqual(O.menu_app(), menu)

    def test_clear_leaves_a_brew_tombstone(self):
        """Oddanie (handback) nie kasuje pliku: bez niego Pod przejąłby claude-acc znowu przy starcie."""
        O.write("pod", "1.31.0", "/Applications/Pod.app")
        with mock.patch("sys.stdout"):
            O.main(["clear"])
        data = O.record()
        self.assertEqual((data["owner"], data["version"], data["app"]), ("brew", "1.31.0", None))
        self.assertIsNone(O.read())  # dla claude-acc to brak właściciela
        self.assertEqual(O.main(["check"]), 0)  # brew i install.sh instalują
        self.assertEqual(O.main(["check", "--as", "pod"]), 0)
        with mock.patch("sys.stdout") as out:
            O.main(["show", "--json"])
        self.assertEqual(json.loads(out.write.call_args_list[0].args[0])["owner"], "brew")

    def test_uninstall_tombstone_only_over_pod(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(O.main(["uninstalled"]), 0)
        self.assertFalse(os.path.exists(O.OWNER_PATH))  # brew bez owner.json: dalej bez pliku
        O.write("pod", "1.31.0")
        O.main(["uninstalled"])
        self.assertEqual(O.record()["owner"], "none")
        self.assertIsNone(O.read())
        with mock.patch("sys.stdout"):
            O.main(["clear"])
        O.main(["uninstalled"])  # nagrobek zostaje nagrobkiem
        self.assertEqual(O.record()["owner"], "brew")
        with self.assertRaises(ValueError):
            O.tombstone("pod")

    def test_forget_deletes_the_file(self):
        O.write("pod", "1")
        with mock.patch("sys.stdout"):
            O.main(["forget"])
            self.assertFalse(os.path.exists(O.OWNER_PATH))
            self.assertEqual(O.main(["forget"]), 0)


class SetupHarness(unittest.TestCase):
    """setup.sh w osobnym HOME z atrapami pkill, launchctl, open, codesign i ditto na początku PATH."""

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

    def setup(self, *args, **extra):
        env = dict(os.environ, HOME=self.home, PATH=self.bin + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
                   FAKE_LOG=self.log, CLAUDE_CONFIG_DIR=os.path.join(self.home, ".claude"),
                   CLAUDE_ACC_ALLOW_FOREIGN_HOME="1")
        env.update(extra)
        env = {k: v for k, v in env.items() if v is not None}
        done = subprocess.run(["/bin/bash", os.path.join(ROOT, "setup.sh"), *args], env=env,
                              capture_output=True, text=True, timeout=240)
        return done.returncode, done.stdout + done.stderr

    def own(self):
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "pod", "version": "1.26.0", "app": "/Applications/Pod.app"}, f)

    def calls(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as f:
            return f.read()


class SetupOwnerTest(SetupHarness):
    def test_without_owner_nothing_changes(self):
        rc, out = self.setup()
        self.assertEqual(rc, 2)
        self.assertIn("brak aplikacji", out)

    def test_homebrew_setup_refuses_when_pod_owns_it(self):
        self.own()
        for args in ((), ("--app", "/x")):
            rc, out = self.setup(*args)
            self.assertEqual(rc, 3, args)
            self.assertIn("należy teraz do Pod", out)
        self.assertEqual(self.calls(), "")  # ani pkill, ani launchctl
        self.assertTrue(os.path.exists(os.path.join(self.state, "owner.json")))

    def test_anyone_uninstalls_and_pod_gets_a_tombstone(self):
        """`claude-acc uninstall` (setup.sh --uninstall bez --owner) przy claude-acc Poda: zdejmuje i
        zostawia nagrobek "none", żeby Pod nie zainstalował go od nowa, tylko zdjął swoje agenty."""
        self.own()
        rc, out = self.setup("--uninstall")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.state, "owner.json")) as f:
            self.assertEqual(json.load(f)["owner"], "none")
        self.assertIn("pkill -x ClaudeAcc", self.calls())

    def test_a_tombstone_lets_homebrew_install(self):
        with open(os.path.join(self.state, "owner.json"), "w") as f:
            json.dump({"owner": "brew", "version": "1.31.0", "app": None, "at": 0}, f)
        rc, out = self.setup()
        self.assertEqual(rc, 2, out)  # dalej niż odmowa właściciela: brak --app
        self.assertIn("brak aplikacji", out)

    def test_the_owner_gets_past_the_check(self):
        self.own()
        rc, out = self.setup("--owner", "pod", "--owner-app", "/Applications/Pod.app")
        self.assertEqual(rc, 2)  # dalej niż odmowa: brak --app
        self.assertIn("brak aplikacji", out)

    def test_the_owner_uninstalls_and_leaves_a_tombstone(self):
        self.own()
        rc, _ = self.setup("--owner", "pod", "--uninstall")
        self.assertEqual(rc, 0)
        with open(os.path.join(self.state, "owner.json")) as f:
            data = json.load(f)
        self.assertEqual((data["owner"], data["version"]), ("none", "1.26.0"))
        self.assertIn("pkill -x ClaudeAcc", self.calls())


class ForeignHomeTest(SetupHarness):
    """setup.sh z HOME innym niż katalog domowy konta odmawia (kod 4), zanim cokolwiek zawoła.

    Bez argumentów setup.sh i tak kończy się przed launchctl i pkill (kod 2, brak --app), więc
    przypadek z prawdziwymi programami na PATH nie ma czego zepsuć, nawet gdyby straż nie zadziałała."""

    SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

    def fake_account(self, home):
        # `id -P` jak z bazy kont, z katalogiem domowym `home`; reszta `id` prawdziwa
        with open(os.path.join(self.bin, "id"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = -P ] && { echo "x:*:501:20::0:0:X:%s:/bin/zsh"; exit 0; }\nexec /usr/bin/id "$@"\n' % home)
        os.chmod(os.path.join(self.bin, "id"), 0o755)

    def test_foreign_home_is_refused_before_anything_runs(self):
        for args in (("--app", "/x"), ("--uninstall",), ("--owner", "pod", "--uninstall")):
            rc, out = self.setup(*args, CLAUDE_ACC_ALLOW_FOREIGN_HOME=None)
            self.assertEqual(rc, 4, args)
            self.assertIn("nie katalog domowy konta", out)
        self.assertEqual(self.calls(), "")  # ani pkill, ani launchctl, ani open

    def test_the_allow_variable_needs_fake_launchctl_and_pkill(self):
        # prawdziwe launchctl i pkill z PATH: zmienna nie wystarcza
        rc, out = self.setup(PATH=self.SYSTEM_PATH)
        self.assertEqual(rc, 4, out)
        # tylko launchctl podrobiony, pkill prawdziwy
        only = os.path.join(self.dir, "only-launchctl")
        os.makedirs(only)
        shutil.copy(os.path.join(self.bin, "launchctl"), only)
        rc, out = self.setup(PATH=only + os.pathsep + self.SYSTEM_PATH)
        self.assertEqual(rc, 4, out)
        # obie atrapy: dalej niż straż (brak --app)
        rc, out = self.setup()
        self.assertEqual(rc, 2, out)
        self.assertIn("brak aplikacji", out)

    def test_the_account_home_passes_without_the_variable(self):
        self.fake_account(self.home)
        rc, out = self.setup(CLAUDE_ACC_ALLOW_FOREIGN_HOME=None)
        self.assertEqual(rc, 2, out)
        self.assertIn("brak aplikacji", out)
        # ten sam katalog inną drogą (dowiązanie, ukośnik na końcu) to dalej konto
        link = os.path.join(self.dir, "home-link")
        os.symlink(self.home, link)
        for home in (link, self.home + "/"):
            rc, out = self.setup(HOME=home, CLAUDE_ACC_ALLOW_FOREIGN_HOME=None)
            self.assertEqual(rc, 2, (home, out))
        # konto ma inny katalog domowy niż HOME: odmowa
        self.fake_account(os.path.join(self.dir, "elsewhere"))
        rc, out = self.setup(CLAUDE_ACC_ALLOW_FOREIGN_HOME=None)
        self.assertEqual(rc, 4, out)

    def test_missing_home_is_refused(self):
        self.fake_account(self.home)
        rc, out = self.setup(HOME=os.path.join(self.dir, "missing"), CLAUDE_ACC_ALLOW_FOREIGN_HOME=None)
        self.assertEqual(rc, 4, out)
        self.assertEqual(self.calls(), "")


class PodAgentsSetupTest(SetupHarness):
    """setup.sh --pod-agents: automaty i aplikację paska menu prowadzi Pod (SMAppService), więc
    instalacja zdejmuje stare com.filip.claude-acc.* i kopię Claude Acc.app, a niczego nie stawia."""

    JOBS = ("com.filip.claude-acc", "com.filip.claude-acc.janitor", "com.filip.claude-acc.devguard",
            "com.filip.claude-acc.perf", "com.filip.claude-acc.updates", "com.filip.claude-acc.jobs")

    def setUp(self):
        super().setUp()
        self.agents = os.path.join(self.home, "Library/LaunchAgents")
        os.makedirs(self.agents)
        for job in self.JOBS:
            with open(os.path.join(self.agents, job + ".plist"), "w") as f:
                f.write("<plist/>\n")
        self.legacy_app = os.path.join(self.home, "Applications/Claude Acc.app")
        os.makedirs(os.path.join(self.legacy_app, "Contents/MacOS"))
        self.pod = os.path.join(self.dir, "Pod.app")
        self.menu = os.path.join(self.pod, "Contents/Library/LoginItems/Pod Menu.app")
        os.makedirs(os.path.join(self.menu, "Contents/MacOS"))
        import plistlib
        with open(os.path.join(self.menu, "Contents/Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "com.filip.claude-acc.menubar", "CFBundleShortVersionString": "9.9.9"}, f)

    def install(self, *extra):
        return self.setup("--app", self.menu, "--owner", "pod", "--owner-app", self.pod, "--pod-agents",
                          "--python", PYTHON, *extra, CLAUDE_ACC_NO_HOOKS="1")

    def test_pod_agents_replace_the_legacy_jobs_and_app(self):
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        calls = self.calls().splitlines()
        uid = os.getuid()
        for job in self.JOBS:
            self.assertIn(f"launchctl bootout gui/{uid}/{job}", calls)
        self.assertFalse([c for c in calls if c.startswith("launchctl bootstrap")], calls)
        self.assertEqual(os.listdir(self.agents), [])
        # stara kopia aplikacji: zamknięta po ścieżce (Pod Menu ma ten sam plik ClaudeAcc) i usunięta
        self.assertIn(f"pkill -f {self.legacy_app}/Contents/MacOS/ClaudeAcc", calls)
        self.assertFalse([c for c in calls if c.startswith(("pkill -x", "ditto", "open", "codesign"))], calls)
        self.assertFalse(os.path.exists(self.legacy_app))
        # interpreter aplikacji i ślad w owner.json, gdzie jest aplikacja paska menu
        self.assertEqual(os.readlink(os.path.join(self.state, "python")), PYTHON)
        with open(os.path.join(self.state, "owner.json")) as f:
            owned = json.load(f)
        self.assertEqual((owned["owner"], owned["app"], owned["menu"]), ("pod", self.pod, self.menu))
        env = dict(os.environ, HOME=self.home)
        menu = subprocess.run(["/usr/bin/python3", os.path.join(self.state, "owner.py"), "menu"], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(menu, self.menu)
        # komenda i skrypty jak w każdej instalacji
        self.assertTrue(os.access(os.path.join(self.home, ".local/bin/claude-acc"), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "acc.py")))

    def test_pod_rootd_replaces_the_root_installers(self):
        # Pod's root helper in the owner app: `claude-acc rootd` is its CLI, `fans install` points to it
        plist = os.path.join(self.pod, "Contents/Library/LaunchDaemons/codes.pod.app.rootd.plist")
        rootctl = os.path.join(self.pod, "Contents/Resources/claude-acc/pod-rootctl")
        os.makedirs(os.path.dirname(plist))
        os.makedirs(os.path.dirname(rootctl))
        with open(plist, "w") as f:
            f.write("<plist/>\n")
        with open(rootctl, "w") as f:
            f.write('#!/bin/sh\necho "fake pod-rootctl $*"\n')
        os.chmod(rootctl, 0o755)
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.readlink(os.path.join(self.state, "pod-rootctl")), rootctl)
        self.assertIn("pomocnik roota Poda", out)
        self.assertNotIn("claude-acc fans install", out)
        command = os.path.join(self.home, ".local/bin/claude-acc")
        env = dict(os.environ, HOME=self.home)
        done = subprocess.run([command, "rootd", "status"], env=env, capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "fake pod-rootctl status"))
        done = subprocess.run([command, "fans", "install"], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("claude-acc rootd fans", done.stderr)
        # perf-root and mac root-clean go to rootroute.py first, not to sudo
        done = subprocess.run([command, "mac", "root-clean", "--bogus"], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("nieznana opcja: --bogus", done.stderr)
        # without the helper in the app: the old installers again
        os.remove(plist)
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(os.path.join(self.state, "pod-rootctl")))
        self.assertIn("claude-acc fans install", out)
        done = subprocess.run([command, "rootd", "status"], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 69)

    def test_pod_agents_need_the_pod_owner(self):
        rc, out = self.setup("--app", self.menu, "--pod-agents")
        self.assertEqual(rc, 2, out)
        self.assertIn("--pod-agents tylko z --owner pod", out)
        self.assertEqual(self.calls(), "")
        self.assertEqual(len(os.listdir(self.agents)), len(self.JOBS))


class PayloadTest(unittest.TestCase):
    def test_payload_layout_version_and_tarball(self):
        work = os.path.realpath(tempfile.mkdtemp(prefix="payload-test-"))
        self.addCleanup(shutil.rmtree, work, True)
        products = os.path.join(work, "products")
        os.makedirs(products)
        for name in ("ClaudeAcc", "fanctl", "claude-acc-hook", "claude-acc-pause", "claude-acc-desktop", "pod-acc-run",
                     "pod-rootd", "pod-rootctl"):
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
                         "orca-plugin", "Pod Menu.app", "fanctl", "claude-acc-hook", "claude-acc-pause",
                         "claude-acc-desktop", "pod-acc-run", "pod-rootd", "pod-rootctl", "LaunchAgents",
                         "LaunchDaemons", "VERSION", "payload.json"):
            self.assertIn(expected, names)
        self.assertNotIn("Claude Acc.app", names)  # układ 2: aplikację paska menu wozi Pod jako Pod Menu
        self.assertNotIn("test", os.listdir(os.path.join(payload, "orca-plugin")))
        self.assertFalse([p for p, _, _ in os.walk(payload) if p.endswith("__pycache__")])
        with open(os.path.join(payload, "VERSION")) as f:
            self.assertEqual(f.read().strip(), "9.9.9")
        menu = os.path.join(payload, "Pod Menu.app")
        self.assertTrue(os.path.isfile(os.path.join(menu, "Contents/MacOS/ClaudeAcc")))
        import plistlib
        with open(os.path.join(menu, "Contents/Info.plist"), "rb") as f:
            info = plistlib.load(f)
        # bundle id i plik wykonywalny zostają: na nich wiszą zgody TCC i `pgrep -x ClaudeAcc` Poda
        self.assertEqual((info["CFBundleIdentifier"], info["CFBundleExecutable"]), ("com.filip.claude-acc.menubar", "ClaudeAcc"))
        self.assertEqual((info["CFBundleName"], info["CFBundleDisplayName"]), ("Pod Menu", "Pod Menu"))
        with open(os.path.join(payload, "payload.json")) as f:
            self.assertEqual(json.load(f)["layout"], 2)
        agents = sorted(os.listdir(os.path.join(payload, "LaunchAgents")))
        self.assertEqual(agents, sorted(f"codes.pod.app.acc.{j}.plist" for j in ("tick", "janitor", "devguard", "perf", "updates", "jobs")))
        # Pod's root helper: its plist goes to Contents/Library/LaunchDaemons, BundleProgram next to pod-acc-run
        self.assertEqual(os.listdir(os.path.join(payload, "LaunchDaemons")), ["codes.pod.app.rootd.plist"])
        with open(os.path.join(payload, "LaunchDaemons/codes.pod.app.rootd.plist"), "rb") as f:
            daemon = plistlib.load(f)
        self.assertEqual((daemon["Label"], daemon["BundleProgram"]), ("codes.pod.app.rootd", "Contents/Resources/claude-acc/pod-rootd"))
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
