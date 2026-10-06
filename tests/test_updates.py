"""Testy updates.py na atrapach brew, npm, go i pip.

Każdy test stawia osobny $HOME z plikiem świata (co jest zainstalowane, co najnowsze), a atrapy
z tests/fakes-updates czytają go i zmieniają tak, jak prawdziwe narzędzia. Prawdziwe brew, npm
ani go nie są wołane: skrypt dostaje PATH z samymi atrapami i /usr/bin:/bin.

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
        "vercel": {"installed": "59.16.0", "latest": "62.4.0", "versions": ["59.16.0", "62.4.0"]},
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
    "fail": [],
}


class Env:
    def __init__(self, test, **changes):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="updates-test-"))
        test.addCleanup(shutil.rmtree, self.home, True)
        self.state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(os.path.join(self.home, "fake"))
        os.makedirs(self.state_dir)
        world = copy.deepcopy(WORLD)
        world.update(changes)
        self.save_world(world)
        gobin = os.path.join(self.home, "go/bin")
        os.makedirs(gobin)
        for name in ("air", "ent", "notgo"):  # notgo: skrypt w GOBIN, nie z go install
            path = os.path.join(gobin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
        self.config(npm_pins={"pnpm": "11"}, python=os.path.join(FAKES, "python3"))

    def config(self, **cfg):
        with open(os.path.join(self.state_dir, "updates.json"), "w") as f:
            json.dump(cfg, f)

    def run(self, *args):
        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "CLAUDE_ACC_TOOL_PATH": f"{FAKES}:/usr/bin:/bin",
        }
        return subprocess.run(
            ["/usr/bin/python3", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=60
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
        env = Env(self)
        done = env.run("run", "--force")
        self.assertEqual(done.returncode, 0, done.stderr)

        world = env.world()
        self.assertEqual(world["brew"]["formulae"]["railway"]["installed"], "5.63.3")
        self.assertEqual(world["brew"]["casks"]["ngrok"]["installed"], "3.22.1")
        # globalne paczki npm idą też o wersję główną wyżej
        self.assertEqual(world["npm"]["vercel"]["installed"], "62.4.0")
        self.assertEqual(world["npm"]["npm"]["installed"], "12.2.0")
        self.assertEqual(world["go"]["air"]["installed"], "v1.67.4")

        state = env.state()
        run = state["last_run"]
        self.assertTrue(run["ok"])
        self.assertEqual(state["last_success"], run["at"])
        self.assertEqual(run["updated"], 7)  # railway, ngrok, vercel, pnpm, npm, air, attrs
        self.assertNotIn("running_since", state)
        self.assertEqual(names(state["steps"]), ["brew", "npm", "go", "pip"])
        self.assertEqual(names(step(state, "go")["updated"]), ["air"])
        self.assertEqual(step(state, "go")["failed"], [])  # notgo pominięty, nie błąd
        self.assertEqual(env.notifications(), [])

    def test_pins_hold_a_brew_formula_and_an_npm_major(self):
        env = Env(self)
        env.run("run", "--force")
        world = env.world()
        self.assertEqual(world["brew"]["formulae"]["facebook/fb/idb-companion"]["installed"], "1.1.8")
        self.assertEqual(world["npm"]["pnpm"]["installed"], "11.28.5")

        state = env.state()
        self.assertEqual(step(state, "brew")["held"], [{"name": "idb-companion", "from": "1.1.8", "to": "1.6.5"}])
        self.assertEqual(step(state, "npm")["held"], [{"name": "pnpm", "from": "11.28.5", "to": "12.9.1", "pin": "11"}])

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
        })
        fake = os.path.join(FAKES, "python3")
        brew = os.path.join(env.home, "fake/python3-brew")
        os.symlink(fake, brew)
        env.config(python=[fake, brew, fake])  # ten sam Python drugi raz nie liczy się
        env.run("run", "--force")
        pip = step(env.state(), "pip")
        self.assertEqual([(p["name"], p["python"]) for p in pip["updated"]], [("attrs", "3.13"), ("numpy", "3.14")])
        self.assertEqual(sum(c.startswith("python3 -m pip install") for c in env.calls()), 1)
        self.assertEqual(env.world()["pip:python3-brew"]["numpy"]["installed"], "2.5.3")

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


if __name__ == "__main__":
    unittest.main()
