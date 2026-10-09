"""Testy updates.py na atrapach brew, npm, go, pip, uv, claude, npx skills i pkgutil.

Każdy test stawia osobny $HOME z plikiem świata (co jest zainstalowane, co najnowsze), a atrapy
z tests/fakes-updates czytają go i zmieniają tak, jak prawdziwe narzędzia. Prawdziwe brew, npm,
go, uv, pip ani claude nie są wołane: skrypt dostaje PATH z samymi atrapami i /usr/bin:/bin
(stamtąd prawdziwe curl do plików file:// i git do hashy skilli).

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import copy
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "updates.py")
FAKES = os.path.join(HERE, "fakes-updates")
DAY = 86400

WORLD = {
    "brew": {
        "formulae": {
            "railway": {"installed": "5.63.1", "latest": "5.63.3"},
            "facebook/fb/idb-companion": {"installed": "1.1.8", "latest": "1.6.5", "pinned": True},
            "gh": {"installed": "2.80.0", "latest": "2.80.0"},
        },
        "casks": {"ngrok": {"installed": "3.20.0", "latest": "3.22.1"}},
    },
    "npm": {
        "vercel": {"installed": "59.16.0", "latest": "62.4.0", "versions": ["59.16.0", "62.4.0"], "bin": "vercel"},
        # rejestr nie sortuje: 11.9.0 po 11.28.5, a tekstowo "11.9.0" > "11.28.5"
        "pnpm": {"installed": "11.8.0", "latest": "12.9.1", "versions": ["11.8.0", "11.28.5", "11.9.0", "12.9.1"]},
        "npm": {"installed": "11.19.1", "latest": "12.2.0", "versions": ["11.19.1", "12.2.0"]},
        "corepack": {"installed": "0.36.0", "latest": "0.36.0", "versions": ["0.36.0"]},
    },
    "go": {
        "air": {"path": "github.com/air-verse/air", "module": "github.com/air-verse/air",
                "installed": "v1.61.7", "latest": "v1.67.4"},
        "ent": {"path": "entgo.io/ent/cmd/ent", "module": "entgo.io/ent", "installed": "v0.14.6", "latest": "v0.14.6"},
    },
    "pip": {"attrs": {"installed": "25.4.0", "latest": "26.1.0"}},
    "uv_pythons": [],
    "claude": {
        "version": "2.1.291",
        "latest": "2.1.292",
        "plugins": [{"id": "security-guidance@claude-plugins-official", "scope": "user",
                     "version": "2.0.8", "latest": "2.0.10"}],
    },
    "skills": {},
    "fail": [],
}
# python.org: najnowsza 3.14 to 3.14.8, a 3.15 to inna wersja główna, której automat nie proponuje
RELEASES = [{"name": "Python 3.14.7"}, {"name": "Python 3.14.8"}, {"name": "Python 3.15.0"}, {"name": "Python 3.14.9rc1"}]
PYTHON_ORG = {"pip:version": "3.14", "pip:patch": "3.14.0", "pip:prefix": "/Library/Frameworks/Python.framework/Versions/3.14"}


def tree_hash(folder):
    scratch = tempfile.mkdtemp()
    try:
        env = dict(os.environ, GIT_DIR=f"{scratch}/repo", GIT_INDEX_FILE=f"{scratch}/index", GIT_WORK_TREE=folder)
        subprocess.run(["git", "init", "-q", "--bare", f"{scratch}/repo"], check=True)
        subprocess.run(["git", "add", "-A", "."], cwd=folder, env=env, check=True)
        return subprocess.run(["git", "write-tree"], env=env, capture_output=True, text=True, check=True).stdout.strip()
    finally:
        shutil.rmtree(scratch)




class Env:
    def __init__(self, test, edit=None, **changes):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="updates-test-"))
        test.addCleanup(shutil.rmtree, self.home, True)
        self.state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(os.path.join(self.home, "fake"))
        os.makedirs(self.state_dir)
        world = copy.deepcopy(WORLD)
        world.update(changes)
        if edit:
            edit(world)
        self.save_world(world)
        gobin = os.path.join(self.home, "go/bin")
        os.makedirs(gobin)
        for name in ("air", "ent", "notgo"):  # notgo: skrypt w GOBIN, nie z go install
            path = os.path.join(gobin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
        self.write("python-releases.json", json.dumps(RELEASES))
        self.write("ftp/3.14.8/python-3.14.8-macos11.pkg", "pkg")
        os.makedirs(os.path.join(self.home, "ms-playwright/chromium-1243"))
        # globalne paczki npm z komendami: package.json z polem bin i komenda w npm-global/bin
        os.makedirs(os.path.join(self.home, "npm-global/bin"))
        for name, package in world["npm"].items():
            if "bin" in package:
                self.write(f"npm-global/lib/node_modules/{name}/package.json", json.dumps({"bin": {package["bin"]: "cli.js"}}))
                os.symlink(os.path.join(FAKES, "npm-bin"), os.path.join(self.home, "npm-global/bin", package["bin"]))
                self.write(f"npm-global/bin/{package['bin']}.package", name)
        self.config(npm_pins={"pnpm": "11"}, python=os.path.join(FAKES, "python3"))

    def write(self, name, text):
        path = os.path.join(self.home, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def config(self, **cfg):
        with open(os.path.join(self.state_dir, "updates.json"), "w") as f:
            json.dump(cfg, f)

    def run(self, *args):
        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "CLAUDE_ACC_TOOL_PATH": f"{FAKES}:/usr/bin:/bin",
            "CLAUDE_ACC_PYTHON_RELEASES": f"file://{self.home}/python-releases.json",
            "CLAUDE_ACC_PYTHON_FTP": f"file://{self.home}/ftp",
            "PLAYWRIGHT_BROWSERS_PATH": os.path.join(self.home, "ms-playwright"),
        }
        return subprocess.run(
            ["/usr/bin/python3", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=120
        )

    def world(self):
        with open(os.path.join(self.home, "fake/world.json")) as f:
            return json.load(f)

    def save_world(self, world):
        with open(os.path.join(self.home, "fake/world.json"), "w") as f:
            json.dump(world, f)

    def state(self):
        path = os.path.join(self.state_dir, "updates-state.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def set_state(self, state):
        with open(os.path.join(self.state_dir, "updates-state.json"), "w") as f:
            json.dump(state, f)

    def set_link(self, link):
        # wpis, który perf.py keep zostawia w perf-state.json
        with open(os.path.join(self.state_dir, "perf-state.json"), "w") as f:
            json.dump({"ultra": {}, "link": link}, f)

    def calls(self):
        return self.lines("fake/calls.log")

    def notifications(self):
        return self.lines("fake/notify.log")

    def lines(self, name):
        path = os.path.join(self.home, name)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()


def step(state, name):
    return next(s for s in state["steps"] if s["name"] == name)


def names(items):
    return [i["name"] for i in items]


class UpdatesTest(unittest.TestCase):
    def test_brings_every_manager_to_the_newest_version(self):
        env = Env(self, uv_pythons=[{"installed": "3.13.13", "latest": "3.13.14"}])
        done = env.run("run", "--force")
        self.assertEqual(done.returncode, 0, done.stderr)

        world = env.world()
        self.assertEqual(world["brew"]["formulae"]["railway"]["installed"], "5.63.3")
        self.assertEqual(world["brew"]["casks"]["ngrok"]["installed"], "3.22.1")
        # globalne paczki npm idą też o wersję główną wyżej
        self.assertEqual(world["npm"]["vercel"]["installed"], "62.4.0")
        self.assertEqual(world["npm"]["npm"]["installed"], "12.2.0")
        self.assertEqual(world["go"]["air"]["installed"], "v1.67.4")
        self.assertEqual(world["uv_pythons"][0]["installed"], "3.13.14")
        self.assertEqual(world["claude"]["version"], "2.1.292")
        self.assertEqual(world["claude"]["plugins"][0]["version"], "2.0.10")

        state = env.state()
        run = state["last_run"]
        self.assertTrue(run["ok"], state["steps"])
        self.assertEqual(state["last_success"], run["at"])
        # railway, ngrok, vercel, pnpm, npm, air, attrs, Python 3.13 (uv), Claude Code, wtyczka
        self.assertEqual(run["updated"], 10)
        self.assertNotIn("running_since", state)
        self.assertEqual(names(state["steps"]), ["brew", "npm", "go", "pip", "claude"])
        self.assertEqual(names(step(state, "go")["updated"]), ["air"])
        self.assertEqual(step(state, "go")["failed"], [])  # notgo pominięty, nie błąd
        self.assertEqual(names(step(state, "pip")["updated"]), ["attrs", "Python 3.13 (uv)"])
        self.assertEqual(names(step(state, "claude")["updated"]), ["Claude Code", "security-guidance plugin"])
        self.assertEqual(env.notifications(), [])

    def test_pins_hold_a_brew_formula_and_an_npm_major(self):
        env = Env(self)
        env.run("run", "--force")
        world = env.world()
        self.assertEqual(world["brew"]["formulae"]["facebook/fb/idb-companion"]["installed"], "1.1.8")
        self.assertEqual(world["npm"]["pnpm"]["installed"], "11.28.5")

        state = env.state()
        self.assertEqual(step(state, "brew")["held"],
                         [{"name": "idb-companion", "from": "1.1.8", "to": "1.6.5", "why": "pin"}])
        self.assertEqual(step(state, "npm")["held"],
                         [{"name": "pnpm", "from": "11.28.5", "to": "12.9.1", "why": "pin", "pin": "11"}])

    def test_a_failing_package_leaves_the_rest_updated_and_is_named_once(self):
        env = Env(self, fail=["brew ngrok", "npm vercel"])
        env.run("run")  # automatyczny: pierwszy przebieg jest od razu należny
        world = env.world()
        self.assertEqual(world["brew"]["formulae"]["railway"]["installed"], "5.63.3")
        self.assertEqual(world["npm"]["npm"]["installed"], "12.2.0")
        self.assertEqual(world["brew"]["casks"]["ngrok"]["installed"], "3.20.0")

        state = env.state()
        self.assertFalse(state["last_run"]["ok"])
        self.assertNotIn("last_success", state)
        [ngrok] = step(state, "brew")["failed"]
        self.assertEqual(ngrok["name"], "ngrok")
        self.assertIn("Failure while executing", ngrok["error"])
        self.assertEqual((ngrok["admin"], ngrok["retry"]), (True, "brew upgrade --cask ngrok"))
        [vercel] = step(state, "npm")["failed"]
        self.assertEqual(vercel["error"], "npm error 404 Not Found - GET https://registry.npmjs.org/vercel")
        [note] = env.notifications()
        self.assertIn("ngrok (Homebrew)", note)
        self.assertIn("vercel (npm)", note)

        # następnej nocy ten sam problem: próbuje znowu, ale nie powiadamia drugi raz
        state["last_run"]["at"] -= 21 * 3600
        env.set_state(state)
        env.run("run")
        self.assertEqual(sum(c == "brew update --quiet" for c in env.calls()), 2)
        self.assertEqual(len(env.notifications()), 1)

    def test_an_npm_package_whose_command_breaks_is_rolled_back(self):
        # npm 12 nie uruchamia skryptów instalacyjnych: paczka się instaluje, a komenda nie działa
        def breaks(world):
            world["npm"]["vercel"].update(breaks="62.4.0", blocked=["@vercel/fun"])

        env = Env(self, edit=breaks)
        env.run("run", "--force")
        world = env.world()
        self.assertEqual(world["npm"]["vercel"]["installed"], "59.16.0")
        self.assertEqual(world["npm"]["npm"]["installed"], "12.2.0")
        [vercel] = step(env.state(), "npm")["failed"]
        self.assertIn("vercel stopped working after 62.4.0, rolled back", vercel["error"])
        self.assertIn("npm blocked install scripts of @vercel/fun", vercel["error"])
        self.assertEqual(vercel["retry"], "npm install -g --allow-scripts=@vercel/fun vercel@62.4.0")

    def test_runs_every_three_days_and_retries_a_failure_the_next_night(self):
        env = Env(self)
        now = time.time()
        cases = [
            ({"at": now - 2 * DAY, "ok": True}, False),
            ({"at": now - 3 * DAY + 1800, "ok": True}, True),  # launchd budzi o 4:30, przebieg trwał chwilę
            ({"at": now - 5 * 3600, "ok": False}, False),
            ({"at": now - 21 * 3600, "ok": False}, True),
        ]
        for last_run, runs in cases:
            with self.subTest(last_run=last_run):
                env.set_state({"last_run": dict(last_run, updated=0, failed=0, duration=1)})
                before = len(env.calls())
                env.run("run")
                self.assertEqual(len(env.calls()) > before, runs)

        run = env.state()["last_run"]
        upcoming = datetime.fromtimestamp(env.state()["next_run"])
        self.assertEqual((upcoming.hour, upcoming.minute), (4, 30))
        self.assertTrue(3 * DAY - 3600 <= upcoming.timestamp() - run["at"] < 4 * DAY)

    def test_a_scheduled_run_waits_while_the_mac_is_tethered(self):
        env = Env(self)
        tether = {"tethered": True, "port": "iPhone USB", "iface": "en8", "gateway": "172.20.10.1"}
        env.set_link(dict(tether, at=time.time() - 60))
        done = env.run("run")
        self.assertEqual(done.stdout.strip(), "aktualizacja odłożona: Mac na tetheringu")
        self.assertEqual(env.calls(), [])
        log = "\n".join(env.lines(".local/share/claude-acc/updates.log"))
        self.assertIn("=== aktualizacja odłożona: Mac na tetheringu (iPhone USB, en8)", log)
        # bez last_run przebieg zostaje należny: następne uruchomienie z launchd próbuje znowu
        self.assertIsNone(env.state())

        env.set_link(dict(tether, tethered=False, at=time.time()))
        env.run("run")
        self.assertIn("brew update --quiet", env.calls())
        self.assertEqual(env.state()["last_run"]["trigger"], "auto")

    def test_a_stale_or_broken_tether_entry_does_not_hold_the_run(self):
        env = Env(self)
        stale = {"tethered": True, "port": "iPhone USB", "iface": "en8", "at": time.time() - 16 * 60}
        cases = {
            "stale": json.dumps({"link": stale}),
            "no time": json.dumps({"link": dict(stale, at="teraz")}),
            "garbled": '{"link": {"tethered": tr',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                env.write(".local/share/claude-acc/perf-state.json", text)
                env.set_state({})
                before = len(env.calls())
                env.run("run")
                self.assertGreater(len(env.calls()), before)
                self.assertIn("last_run", env.state())

    def test_update_by_hand_runs_while_tethered(self):
        env = Env(self)
        env.set_link({"tethered": True, "port": "iPhone USB", "iface": "en8", "at": time.time()})
        env.run("run", "--force")
        self.assertIn("brew update --quiet", env.calls())
        self.assertEqual(env.state()["last_run"]["trigger"], "manual")

    def test_python_packages_are_upgraded_together_from_wheels(self):
        env = Env(self, pip={
            "attrs": {"installed": "25.4.0", "latest": "26.1.0"},
            # pydantic trzyma ją niżej; llama-cpp-python ma nowszą tylko w źródłach
            "pydantic-core": {"installed": "2.46.5", "latest": "2.49.0", "required": True, "held": True},
            "llama-cpp-python": {"installed": "0.3.16", "latest": "0.3.36", "nowheel": True},
            "openpyxl": {"installed": "3.1.2", "latest": "3.1.5", "user": True},
            # postawiona z katalogu: pod tą nazwą na PyPI jest coś innego
            "mytool": {"installed": "0.1.0", "latest": "9.9.9", "local": True},
        })
        env.run("run", "--force")
        installed = {n: p["installed"] for n, p in env.world()["pip"].items()}
        self.assertEqual(installed, {
            "attrs": "26.1.0", "pydantic-core": "2.46.5", "llama-cpp-python": "0.3.16",
            "openpyxl": "3.1.5", "mytool": "0.1.0",
        })

        state = env.state()
        pip = step(state, "pip")
        self.assertEqual(pip["updated"], [
            {"name": "attrs", "from": "25.4.0", "to": "26.1.0", "python": "3.13"},
            {"name": "openpyxl", "from": "3.1.2", "to": "3.1.5", "python": "3.13"},
        ])
        self.assertEqual(sorted(names(pip["held"])), ["llama-cpp-python", "pydantic-core"])
        self.assertEqual(pip["failed"], [])
        self.assertTrue(state["last_run"]["ok"])

        # jedno polecenie na katalog: resolver widzi wszystkie paczki naraz; user site osobno
        base, user = [c for c in env.calls() if c.startswith("python3 -m pip install")]
        self.assertIn("--upgrade-strategy eager --only-binary :all:", base)
        self.assertTrue(base.endswith(" attrs llama-cpp-python pydantic-core"), base)
        self.assertNotIn("--user", base)
        self.assertTrue(user.endswith(" openpyxl") and "--user" in user, user)

    def test_a_python_upgrade_that_breaks_a_dependency_is_rolled_back(self):
        broken = "cattrs 24.1.0 has requirement attrs<26, but you have attrs 26.1.0."
        env = Env(self, pip={"attrs": {"installed": "25.4.0", "latest": "26.1.0", "breaks": broken}})
        env.run("run")
        self.assertEqual(env.world()["pip"]["attrs"]["installed"], "25.4.0")
        state = env.state()
        [attrs] = step(state, "pip")["failed"]
        self.assertEqual((attrs["from"], attrs["to"]), ("25.4.0", "26.1.0"))
        self.assertEqual(attrs["error"], f"rolled back, pip check: {broken}")
        self.assertFalse(state["last_run"]["ok"])
        [note] = env.notifications()
        self.assertIn("attrs (Python)", note)

    def test_a_dependency_problem_from_before_the_run_is_not_rolled_back(self):
        env = Env(self, pip_broken=["cattrs 24.1.0 requires exceptiongroup, which is not installed."])
        env.run("run", "--force")
        self.assertEqual(env.world()["pip"]["attrs"]["installed"], "26.1.0")
        self.assertTrue(env.state()["last_run"]["ok"])

    def test_a_failed_pip_install_is_named_and_the_rest_goes_on(self):
        env = Env(self, fail=["pip install"])
        env.run("run", "--force")
        state = env.state()
        [attrs] = step(state, "pip")["failed"]
        self.assertEqual(attrs["error"], "ERROR: ResolutionImpossible: fake conflict")
        self.assertEqual(env.world()["npm"]["vercel"]["installed"], "62.4.0")
        self.assertFalse(state["last_run"]["ok"])

    def test_every_python_is_upgraded_once_and_named(self):
        env = Env(self, **{
            "pip:python3-brew": {"numpy": {"installed": "2.4.3", "latest": "2.5.3"}},
            "pip:python3-brew:version": "3.14",
            "pip:python3-brew:prefix": "/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14",
        })
        fake = os.path.join(FAKES, "python3")
        brew = os.path.join(env.home, "fake/python3-brew")
        os.symlink(fake, brew)
        env.config(python=[fake, brew, fake])  # ten sam Python drugi raz nie liczy się
        env.run("run", "--force")
        pip = step(env.state(), "pip")
        self.assertEqual([(p["name"], p["python"]) for p in pip["updated"]], [("attrs", "3.13"), ("numpy", "3.14 Homebrew")])
        self.assertEqual(sum(c.startswith("python3 -m pip install") for c in env.calls()), 1)
        self.assertEqual(env.world()["pip:python3-brew"]["numpy"]["installed"], "2.5.3")

    def test_a_pinned_python_package_stays_and_new_playwright_gets_its_browsers(self):
        env = Env(self, pip={
            "attrs": {"installed": "25.4.0", "latest": "26.1.0"},
            "fb-idb": {"installed": "1.1.7", "latest": "1.2.0"},
            # przypięta zależność: eager podbiłby ją razem z paczką, która jej wymaga
            "grpclib": {"installed": "0.4.7", "latest": "0.4.9", "required": True},
            "playwright": {"installed": "1.58.0", "latest": "1.63.0"},
        })
        env.config(pip_pins={"fb-idb": "==1.1.7", "grpclib": "==0.4.7"}, python=os.path.join(FAKES, "python3"))
        env.run("run", "--force")
        installed = {n: p["installed"] for n, p in env.world()["pip"].items()}
        self.assertEqual(installed, {"attrs": "26.1.0", "fb-idb": "1.1.7", "grpclib": "0.4.7", "playwright": "1.63.0"})
        [install] = [c for c in env.calls() if c.startswith("python3 -m pip install")]
        self.assertTrue(install.endswith(" attrs fb-idb==1.1.7 grpclib==0.4.7 playwright"), install)
        self.assertIn("python3 -m playwright install chromium", env.calls())

        pip = step(env.state(), "pip")
        self.assertEqual(names(pip["updated"]), ["attrs", "playwright"])
        self.assertEqual(sorted((h["name"], h["why"]) for h in pip["held"]), [("fb-idb", "pin"), ("grpclib", "pin")])

    def test_a_python_upgrade_that_breaks_an_import_is_rolled_back(self):
        # pip check tego nie widzi: zależności się zgadzają, a biblioteka natywna nie wstaje
        error = "ImportError: numpy.core.multiarray failed to import"
        env = Env(self, pip={
            "pandas": {"installed": "2.3.3", "latest": "3.0.6", "unimportable": error},
            "playwright": {"installed": "1.58.0", "latest": "1.63.0"},
        })
        env.run("run", "--force")
        installed = {n: p["installed"] for n, p in env.world()["pip"].items()}
        self.assertEqual(installed, {"pandas": "2.3.3", "playwright": "1.58.0"})
        state = env.state()
        failed = step(state, "pip")["failed"]
        self.assertEqual(sorted(names(failed)), ["pandas", "playwright"])
        self.assertEqual(failed[0]["error"], f"rolled back, pandas stopped importing: pandas: {error}")
        self.assertFalse(state["last_run"]["ok"])
        self.assertNotIn("python3 -m playwright install chromium", env.calls())

    def test_a_newer_python_org_patch_is_downloaded_and_offered(self):
        env = Env(self, **PYTHON_ORG)
        env.run("run", "--force")
        [offer] = [h for h in step(env.state(), "pip")["held"] if h["name"] == "Python"]
        self.assertEqual((offer["from"], offer["to"], offer["why"]), ("3.14.0", "3.14.8", "install"))
        self.assertTrue(os.path.isfile(offer["installer"]))
        self.assertTrue(any(c.startswith("pkgutil --check-signature") for c in env.calls()))

        # Python z Homebrew też ma w ścieżce Python.framework, a aktualizuje go brew
        brew = Env(self, **dict(PYTHON_ORG, **{
            "pip:prefix": "/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14"}))
        brew.run("run", "--force")
        self.assertFalse([h for h in step(brew.state(), "pip")["held"] if h["name"] == "Python"])
        self.assertFalse([c for c in brew.calls() if c.startswith("pkgutil")])

    def test_an_unsigned_python_installer_is_thrown_away(self):
        env = Env(self, fail=["signature"], **PYTHON_ORG)
        env.run("run", "--force")
        pip = step(env.state(), "pip")
        self.assertFalse([h for h in pip["held"] if h["name"] == "Python"])
        [python] = pip["failed"]
        self.assertIn("signature check failed", python["error"])
        self.assertEqual(os.listdir(os.path.join(env.state_dir, "downloads")), [])

    def test_a_uv_python_holding_pip_packages_is_not_moved_to_a_new_patch(self):
        # nowa poprawka to nowy katalog: paczki w starym przestałyby być widoczne
        env = Env(self, **{
            "uv_pythons": [{"installed": "3.13.13", "latest": "3.13.14"}, {"installed": "3.11.15", "latest": "3.11.17"}],
            "pip:prefix": "/fake/uv/cpython-3.13.13",
        })
        env.run("run", "--force")
        self.assertIn("uv python upgrade 3.11", env.calls())
        self.assertEqual([v["installed"] for v in env.world()["uv_pythons"]], ["3.13.13", "3.11.17"])
        pip = step(env.state(), "pip")
        self.assertIn("Python 3.11 (uv)", names(pip["updated"]))
        self.assertIn(("Python 3.13 (uv)", "packages"), [(h["name"], h["why"]) for h in pip["held"]])

    def test_plugins_update_in_their_own_project_and_wait_for_confirmation(self):
        project = os.path.join(os.path.realpath(tempfile.gettempdir()), f"updates-proj-{os.getpid()}")
        os.makedirs(project, exist_ok=True)
        self.addCleanup(shutil.rmtree, project, True)
        gone = project + "-gone"

        def plugins(world):
            world["claude"]["plugins"] += [
                {"id": "vercel@claude-plugins-official", "scope": "local", "projectPath": project,
                 "version": "0.49.2", "latest": "0.50.0"},
                {"id": "vercel@claude-plugins-official", "scope": "local", "projectPath": gone,
                 "version": "0.49.2", "latest": "0.50.0"},
                {"id": "stripe@stripe", "scope": "project", "projectPath": project,
                 "version": "0.11.10", "latest": "0.12.0", "confirm": True},
            ]

        env = Env(self, edit=plugins)
        env.run("run", "--force")
        updates = [c for c in env.calls() if c.startswith("claude plugin update")]
        self.assertIn(f"claude plugin update vercel@claude-plugins-official -s local --json cwd={project}", updates)
        self.assertFalse([c for c in updates if gone in c])
        # polecenia z katalogu wtyczki nigdy nie potwierdza automat
        self.assertFalse([c for c in env.calls() if c.startswith("claude") and " -y" in c])

        claude = step(env.state(), "claude")
        self.assertEqual(claude["failed"], [])  # skasowany projekt to nie błąd
        self.assertIn("vercel plugin", names(claude["updated"]))
        [stripe] = claude["held"]
        self.assertEqual((stripe["name"], stripe["why"]), ("stripe plugin", "confirm"))

    def test_plugins_clone_over_https_without_a_github_ssh_key(self):
        # plugin update klonuje źródło github tylko po SSH, a klon marketplace'u ucina po 120 s
        def plugins(world):
            world["claude"]["plugins"].append({"id": "cache-tax@claude-code-mods", "scope": "user",
                                               "version": "2.2.1", "latest": "2.3.0", "ssh_only": True})

        env = Env(self, edit=plugins, slow_github=True)
        env.run("run", "--force")
        claude = step(env.state(), "claude")
        self.assertEqual(claude["failed"], [])
        self.assertIn("cache-tax plugin", names(claude["updated"]))
        self.assertIn("claude-env CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1", env.calls())

    def test_plugins_stay_on_ssh_when_github_takes_the_key(self):
        env = Env(self, ssh_github=True)
        env.run("run", "--force")
        self.assertEqual(step(env.state(), "claude")["failed"], [])
        self.assertNotIn("claude-env CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1", env.calls())

    def test_a_skill_edited_by_hand_is_not_overwritten(self):
        env = Env(self, skills={"kept": "v2 from GitHub", "mine": "v2 from GitHub"})
        lock = {"version": 3, "skills": {}}
        for name in ("kept", "mine"):
            env.write(f".agents/skills/{name}/SKILL.md", "v1")
            lock["skills"][name] = {"source": "someone/skills", "skillFolderHash": tree_hash(
                os.path.join(env.home, ".agents/skills", name))}
        env.write(".agents/.skill-lock.json", json.dumps(lock))
        env.write(".agents/skills/mine/SKILL.md", "v1 with my own rules")

        env.run("run", "--force")
        [update] = [c for c in env.calls() if c.startswith("npx")]
        self.assertTrue(update.endswith("update -g -y kept"), update)
        with open(os.path.join(env.home, ".agents/skills/mine/SKILL.md")) as f:
            self.assertEqual(f.read(), "v1 with my own rules")
        claude = step(env.state(), "claude")
        self.assertIn("kept skill", names(claude["updated"]))
        self.assertEqual([(h["name"], h["why"]) for h in claude["held"]], [("mine skill", "edited")])

    def test_native_claude_comes_before_stale_copies_on_the_path(self):
        # 6.10: /usr/local/bin/claude 2.1.68 z npm zasłonił natywnego, a jego `claude update`
        # przestawił installMethod w ~/.claude.json z native na global
        home = os.path.realpath(tempfile.mkdtemp(prefix="updates-path-"))
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, ".local/bin"))
        done = subprocess.run(
            ["/usr/bin/python3", "-c", "import updates; print(updates.tool_path())"],
            cwd=os.path.dirname(SCRIPT), env={"HOME": home, "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=30)
        path = done.stdout.strip().split(":")
        self.assertEqual(path[0], os.path.join(home, ".local/bin"), done.stderr)

    def test_a_step_from_an_older_version_drops_out_of_the_state(self):
        # do 6.10 krok Pythona nazywał się "python"; stan z tamtej wersji nie może wywrócić przebiegu
        env = Env(self)
        old = {"name": "python", "label": "Python", "ok": True, "updated": [], "failed": [], "held": []}
        env.set_state({"steps": [old]})
        done = env.run("run", "--force")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(names(env.state()["steps"]), ["brew", "npm", "go", "pip", "claude"])

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        env = Env(self)
        done = env.run("run", "--dry-run")
        self.assertIn("railway 5.63.1 → 5.63.3", done.stdout)
        self.assertIn("pnpm 11.8.0 → 11.28.5", done.stdout)
        self.assertIn("air v1.61.7 → v1.67.4", done.stdout)
        self.assertIn("attrs (3.13) 25.4.0 → 26.1.0", done.stdout)
        self.assertEqual(env.world(), {**copy.deepcopy(WORLD)})
        self.assertIsNone(env.state())

    def test_brew_update_failure_skips_brew_but_not_the_others(self):
        env = Env(self, fail=["brew update"])
        env.run("run", "--force")
        self.assertFalse([c for c in env.calls() if c.startswith("brew upgrade")])
        state = env.state()
        self.assertIn("Failed to download", step(state, "brew")["error"])
        self.assertEqual(env.world()["npm"]["vercel"]["installed"], "62.4.0")
        self.assertFalse(state["last_run"]["ok"])

    def test_a_second_run_waits_for_the_first(self):
        env = Env(self)
        with open(os.path.join(env.state_dir, "updates.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            done = env.run("run", "--force")
        self.assertEqual(done.stdout.strip(), "aktualizacja już trwa")
        self.assertEqual(env.calls(), [])

    def test_only_runs_the_named_managers_and_keeps_the_schedule(self):
        env = Env(self)
        env.run("run", "--only", "go")
        self.assertEqual({c.split()[0] for c in env.calls()}, {"go"})
        state = env.state()
        self.assertEqual(names(state["steps"]), ["go"])
        self.assertNotIn("last_run", state)

    def test_polish_letters_and_quotes_reach_osascript_as_arguments(self):
        # json.dumps wstawiał do skryptu \u0105 zamiast "ą", AppleScript go nie parsował i nic się nie pokazywało
        env = Env(self)
        text = 'Żółć, ą, ł, ż i "cudzysłów"'
        script = f"import sys; sys.path.insert(0, {os.path.dirname(HERE)!r}); import updates; updates.notify('Tytuł ą', sys.argv[1])"
        run_env = {"HOME": env.home, "PATH": "/usr/bin:/bin", "CLAUDE_ACC_TOOL_PATH": f"{FAKES}:/usr/bin:/bin"}

        subprocess.run(["/usr/bin/python3", "-c", script, text], env=run_env, check=True)

        [note] = env.notifications()
        self.assertEqual(note.split(" -- ", 1)[1], f"{text} Tytuł ą")


if __name__ == "__main__":
    unittest.main()
