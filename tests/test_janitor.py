"""Testy janitor.py na prawdziwym systemie plików i prawdziwym lsof.

Każdy test stawia osobny $HOME z katalogiem projektów, uruchamia skrypt jako
proces i sprawdza, co zostało na dysku. Zadania, które dotykają narzędzi
systemu (docker, brew, npm, go...), są wyłączone w konfiguracji testu.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "janitor.py")
SYSTEM_TASKS = ["tmp", "go", "npm", "pnpm", "docker", "xcode", "brew", "uv", "logs"]
OLD = 60  # dni: starsze niż każdy próg w konfiguracji


class Env:
    def __init__(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="janitor-test-"))
        self.work = os.path.join(self.home, "work")
        self.state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.work)
        os.makedirs(self.state_dir)
        self.config(protect=[])

    def config(self, **extra):
        cfg = {
            "roots": [self.work],
            "skip": SYSTEM_TASKS,
            "next_idle_hours": 24,
            "cache_idle_days": 7,
            "node_modules_idle_days": 30,
        }
        cfg.update(extra)
        with open(os.path.join(self.state_dir, "janitor.json"), "w") as f:
            json.dump(cfg, f)

    def file(self, rel, days_old=0, text="x"):
        path = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text * 4096)
        if days_old:
            stamp = time.time() - days_old * 86400
            os.utime(path, (stamp, stamp))
        return path

    def exists(self, rel):
        return os.path.exists(os.path.join(self.work, rel))

    def sweep(self, *args):
        env = dict(os.environ, HOME=self.home)
        done = subprocess.run(
            ["/usr/bin/python3", SCRIPT, "sweep", "--force", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    def state(self):
        with open(os.path.join(self.state_dir, "janitor-state.json")) as f:
            return json.load(f)

    def cleanup(self):
        shutil.rmtree(self.home, ignore_errors=True)


class JanitorTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.cleanup)

    def app(self, name, next_dir=".next", days_old=OLD):
        self.env.file(f"{name}/package.json", days_old, "{}")
        return self.env.file(f"{name}/{next_dir}/cache/chunk.bin", days_old)

    def spawn(self, cmd, cwd):
        """Proces z katalogiem roboczym w projekcie, sprzątany po teście."""
        proc = subprocess.Popen(
            cmd, cwd=os.path.join(self.env.work, cwd), stdin=subprocess.PIPE
        )

        def stop():
            proc.kill()
            proc.wait()
            proc.stdin.close()

        self.addCleanup(stop)
        time.sleep(0.3)
        return proc

    def test_idle_next_goes_fresh_stays(self):
        self.app("idle")
        self.app("blog", next_dir=".next-blog")
        self.app("fresh", days_old=0)
        self.env.sweep()
        self.assertFalse(self.env.exists("idle/.next"))
        self.assertFalse(self.env.exists("blog/.next-blog"))
        self.assertTrue(self.env.exists("fresh/.next"))
        self.assertTrue(self.env.exists("idle/package.json"))

    def test_next_with_open_file_stays(self):
        chunk = self.app("served")
        with open(chunk):
            self.env.sweep()
        self.assertTrue(self.env.exists("served/.next"))

    def test_next_of_running_dev_server_stays(self):
        """Proces z katalogiem roboczym w aplikacji to dev server, nawet bez otwartych plików."""
        self.app("dev")
        self.spawn(["sleep", "60"], "dev")
        self.env.sweep()
        self.assertTrue(self.env.exists("dev/.next"))

    def test_shell_in_project_does_not_block(self):
        """Terminal otwarty w projekcie to nie praca: jego .next i tak idzie."""
        self.app("shell")
        # powłoka czeka na wejście wbudowanym `read`, bez procesów potomnych, jak przy znaku zachęty
        self.spawn(["/bin/zsh", "-c", "read x"], "shell")
        self.env.sweep()
        self.assertFalse(self.env.exists("shell/.next"))

    def test_protected_and_foreign_next_stay(self):
        self.app("footage/app")
        self.env.file("site/package.json", OLD, "{}")
        self.env.file("site/.vercel/output/functions/f.func/.next/x.js", OLD)
        self.env.file("loose/.next/x.js", OLD)  # bez package.json obok
        self.env.config(protect=[os.path.join(self.env.work, "footage")])
        self.env.sweep()
        self.assertTrue(self.env.exists("footage/app/.next"))
        self.assertTrue(self.env.exists("site/.vercel/output/functions/f.func/.next"))
        self.assertTrue(self.env.exists("loose/.next"))

    def test_build_committed_to_git_stays(self):
        chunk = self.app("committed")
        repo = os.path.join(self.env.work, "committed")
        for cmd in (
            ["init", "-q"],
            ["add", "-A"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "build"],
        ):
            subprocess.run(["git", "-C", repo, *cmd], check=True, capture_output=True)
        stamp = time.time() - OLD * 86400
        os.utime(chunk, (stamp, stamp))
        self.env.sweep()
        self.assertTrue(self.env.exists("committed/.next/cache/chunk.bin"))

    def test_stale_project_loses_node_modules(self):
        self.env.file("old/package.json", OLD, "{}")
        self.env.file("old/src/index.ts", OLD)
        self.env.file("old/node_modules/dep/index.js", OLD)
        self.env.file("old/apps/web/node_modules/dep/index.js", OLD)
        self.env.file("new/package.json", OLD, "{}")
        self.env.file("new/node_modules/dep/index.js", OLD)
        self.env.file("new/src/index.ts", 0)  # ktoś tu pracował
        self.env.sweep()
        self.assertFalse(self.env.exists("old/node_modules"))
        self.assertFalse(self.env.exists("old/apps/web/node_modules"))
        self.assertTrue(self.env.exists("old/src/index.ts"))
        self.assertTrue(self.env.exists("new/node_modules"))

    def test_git_activity_keeps_node_modules(self):
        self.env.file("repo/package.json", OLD, "{}")
        self.env.file("repo/node_modules/dep/index.js", OLD)
        self.env.file("repo/.git/HEAD", 0, "ref: refs/heads/main\n")
        self.env.sweep()
        self.assertTrue(self.env.exists("repo/node_modules"))

    def test_idle_caches_go(self):
        self.env.file("mono/package.json", 0, "{}")
        self.env.file("mono/node_modules/dep/index.js", 0)
        self.env.file("mono/node_modules/.cache/babel/x.json", OLD)
        self.env.file("mono/.turbo/cookies/1.cookie", OLD)
        self.env.sweep()
        self.assertFalse(self.env.exists("mono/node_modules/.cache"))
        self.assertFalse(self.env.exists("mono/.turbo"))
        self.assertTrue(self.env.exists("mono/node_modules/dep/index.js"))

    def test_caps_drop_oldest_over_limit(self):
        """Snapshoty buildów ponad limit idą od najstarszych, najnowszy zostaje zawsze."""
        for name in ("a", "b", "c"):
            self.env.file(f"perf/builds/{name}/big.bin")
            time.sleep(1.1)  # różne daty powstania
        self.env.file("perf/builds/notes.txt")  # też wpis, też najnowszy
        cap = {
            "path": os.path.join(self.env.work, "perf/*"),
            "max_gb": 9 / 1024**2,  # 9 KB: mieszczą się dwa wpisy po 4 KB
            "keep": 1,
            "fresh_minutes": 0,
        }
        self.env.config(caps=[cap])
        self.env.sweep()
        self.assertTrue(self.env.exists("perf/builds/notes.txt"))
        self.assertTrue(self.env.exists("perf/builds/c"))
        self.assertFalse(self.env.exists("perf/builds/b"))
        self.assertFalse(self.env.exists("perf/builds/a"))

    def test_dry_run_touches_nothing(self):
        self.app("idle")
        out = self.env.sweep("--dry-run")
        self.assertIn("do zwolnienia", out)
        self.assertTrue(self.env.exists("idle/.next"))
        self.assertFalse(
            os.path.exists(os.path.join(self.env.state_dir, "janitor-state.json"))
        )

    def test_interrupted_delete_is_finished(self):
        self.env.file("app/package.json", 0, "{}")
        self.env.file("app/.janitor-trash-.next-1700000000/x.bin", 0)
        self.env.sweep()
        self.assertFalse(self.env.exists("app/.janitor-trash-.next-1700000000"))

    def test_state_records_sweep(self):
        self.app("idle")
        self.env.sweep()
        state = self.env.state()
        self.assertGreater(state["last_sweep"]["freed"], 0)
        self.assertEqual(state["freed_total"], state["last_sweep"]["freed"])
        self.assertNotIn("running_since", state)
        self.assertIn("next", state["task_runs"])

    def test_second_sweep_waits_for_interval(self):
        """Bez --force przebieg zaraz po poprzednim nic nie robi (launchd przy logowaniu)."""
        self.app("idle")
        self.env.sweep()
        self.app("idle2")
        env = dict(os.environ, HOME=self.env.home)
        subprocess.run(
            ["/usr/bin/python3", SCRIPT, "sweep"], env=env, check=True, timeout=60
        )
        self.assertTrue(self.env.exists("idle2/.next"))


if __name__ == "__main__":
    unittest.main()
