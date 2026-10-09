"""Testy janitor.py na prawdziwym systemie plików i prawdziwym lsof.

Każdy test stawia osobny $HOME z katalogiem projektów, uruchamia skrypt jako
proces i sprawdza, co zostało na dysku. Zadania, które dotykają narzędzi
systemu (docker, brew, npm, go...), są wyłączone w konfiguracji testu.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "janitor.py")
ACC = os.path.join(os.path.dirname(HERE), "acc.py")
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

    def sweep(self, *args, launcher=False):
        """launcher=True: tak jak launchd po setup.sh, `<python> acc.py janitor sweep`."""
        env = dict(os.environ, HOME=self.home)
        start = [sys.executable, ACC, "janitor"] if launcher else ["/usr/bin/python3", SCRIPT]
        done = subprocess.run(
            [*start, "sweep", "--force", *args],
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


class WriteJsonTest(unittest.TestCase):
    def test_same_bytes_as_json_dump(self):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import janitor

        folder = tempfile.mkdtemp(prefix="write-json-test-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        data = {"ścieżka": "~/zażółć", "liczby": [1, 2.5, None], "zagnieżdżone": {"b": True, "a": []}}
        for kwargs in ({}, {"indent": 1}, {"indent": 1, "ensure_ascii": False}):
            path = os.path.join(folder, "state.json")
            janitor.write_json(path, data, **kwargs)
            with open(path) as f:
                written = f.read()
            self.assertEqual(written, json.dumps(data, **kwargs), kwargs)
            self.assertEqual(os.listdir(folder), ["state.json"])  # plik tymczasowy nie zostaje


class GoCacheTrimTest(unittest.TestCase):
    def test_oldest_entries_go_first_and_root_files_stay(self):
        import sys
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import janitor
        root = tempfile.mkdtemp(prefix="gocache-test-")
        self.addCleanup(shutil.rmtree, root, True)
        now = time.time()
        paths = []
        for i, sub in enumerate(("00", "0a", "ff")):
            os.makedirs(os.path.join(root, sub))
            for j in range(3):
                path = os.path.join(root, sub, f"{i}{j}-a")
                with open(path, "wb") as f:
                    f.write(b"x" * 8192)
                age = (i * 3 + j) * 3600  # 0 = newest
                os.utime(path, (now - age, now - age))
                paths.append((age, path))
        with open(os.path.join(root, "trim.txt"), "w") as f:
            f.write("1")
        block = os.lstat(paths[0][1]).st_blocks * 512
        freed = janitor.trim_oldest(root, keep_bytes=4 * block)
        self.assertEqual(freed, 5 * block)
        left = sorted(age for age, path in paths if os.path.exists(path))
        self.assertEqual(left, [0, 3600, 7200, 10800])  # the four most recently used
        self.assertTrue(os.path.exists(os.path.join(root, "trim.txt")))
        # dry run counts without deleting
        self.assertEqual(janitor.trim_oldest(root, keep_bytes=0, dry_run=True), 4 * block)
        self.assertEqual(len([a for a, p in paths if os.path.exists(p)]), 4)


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

    @unittest.skipUnless(os.path.exists(ACC), "brak acc.py")
    def test_sweep_through_launcher_does_the_same(self):
        self.app("idle")
        self.env.sweep(launcher=True)
        self.assertFalse(self.env.exists("idle/.next"))
        self.assertIn("next", self.env.state()["task_runs"])

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


class UnavailableSimulatorsTest(unittest.TestCase):
    """2026-10-09: runtime iOS zniknął na chwilę po zmianie Xcode, a `simctl delete unavailable`
    skasował symulatory agentów. Fałszywy simctl zapisuje wywołania i podaje listę niedostępnych."""

    POOL = "AAAAAAAA-0000-0000-0000-000000000001"
    LEASED = "BBBBBBBB-0000-0000-0000-000000000002"
    MINE = "CCCCCCCC-0000-0000-0000-000000000003"

    def setUp(self):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import janitor

        self.janitor = janitor
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="sims-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.leases = os.path.join(self.root, "leases")
        os.makedirs(self.leases)
        self.calls = []
        names = {self.POOL: "Portivo-E2E-iPhone", self.LEASED: "Scratch", self.MINE: "My iPhone"}
        listing = json.dumps(
            {"devices": {"iOS-26-0": [{"udid": u, "name": n} for u, n in names.items()]}}
        )

        def fake_run(cmd, timeout=600):
            self.calls.append(cmd[1:])
            return listing if cmd[2:4] == ["list", "devices"] else ""

        os.makedirs(os.path.join(self.root, "Library/Developer/CoreSimulator"))
        patched = {
            "HOME": self.root,
            "which": lambda name: "xcrun",
            "run": fake_run,
            "log": lambda line, path=None: None,
            "SIMULATORS_STATE_PATH": os.path.join(self.root, "janitor-simulators.json"),
        }
        for name, value in patched.items():
            self.addCleanup(setattr, janitor, name, getattr(janitor, name))
            setattr(janitor, name, value)
        cfg = dict(janitor.DEFAULT_CONFIG, simulator_leases=self.leases)
        self.sw = janitor.Sweep(cfg, dry_run=False)

    def deleted(self):
        return [c[2] for c in self.calls if c[:2] == ["simctl", "delete"]]

    def sweep(self):
        self.janitor.task_xcode(self.sw, None)

    def test_never_runs_the_blanket_delete(self):
        self.sweep()
        self.sweep()
        self.assertNotIn(["simctl", "delete", "unavailable"], self.calls)

    def test_first_sighting_deletes_nothing(self):
        self.sweep()
        self.assertEqual(self.deleted(), [])

    def test_second_sighting_deletes_only_foreign_unleased(self):
        open(os.path.join(self.leases, self.LEASED + ".json"), "w").write("{}")
        self.sweep()
        self.sweep()
        self.assertEqual(self.deleted(), [self.MINE])

    def test_device_that_came_back_starts_over(self):
        self.sweep()
        self.janitor.write_json(self.janitor.SIMULATORS_STATE_PATH, [])  # w międzyczasie wrócił
        self.sweep()
        self.assertEqual(self.deleted(), [])


def compressed(path):
    return bool(os.stat(path).st_flags & stat.UF_COMPRESSED)


AFSCTOOL = shutil.which("afsctool", path="/opt/homebrew/bin:/usr/local/bin")


class CompressCandidatesTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import janitor

        self.janitor = janitor
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="compress-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, text="log line\n" * 2000):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def names(self, **kwargs):
        args = dict(since=0, until=time.time() + 5, max_bytes=1024**3)
        args.update(kwargs)
        found = self.janitor.compress_candidates(self.root, **args)
        return sorted(os.path.relpath(path, self.root) for path, _, _ in found)

    def test_age_window_and_size_limit(self):
        self.write("a.jsonl")
        self.write("empty.log", "")
        self.write("big.bin", "x" * 50000)
        now = time.time()
        self.assertEqual(self.names(), ["a.jsonl", "big.bin"])
        # zmienione przed chwilą to zapis w toku; zmienione przed `since` już przeszło kompresję
        self.assertEqual(self.names(until=now - 3600), [])
        self.assertEqual(self.names(since=now + 1), [])
        self.assertEqual(self.names(max_bytes=40000), ["a.jsonl"])

    def test_hard_links_count_once_and_running_bundles_are_skipped(self):
        first = self.write("store/dep.js")
        os.link(first, os.path.join(self.root, "node_modules-dep.js"))
        self.write("Live.app/Contents/Resources/strings.txt")
        self.write("Idle.app/Contents/Resources/strings.txt")
        live = os.path.join(self.root, "Live.app")
        found = self.names(skip_bundles={live})
        self.assertEqual(len([n for n in found if n.endswith("dep.js")]), 1)
        self.assertIn("Idle.app/Contents/Resources/strings.txt", found)
        self.assertNotIn("Live.app/Contents/Resources/strings.txt", found)

    def test_bundle_of(self):
        path = "/Applications/Brave Browser.app/Contents/Frameworks/X.framework/Helpers/H.app/Contents/MacOS/H"
        self.assertEqual(self.janitor.bundle_of(path), "/Applications/Brave Browser.app")
        self.assertIsNone(self.janitor.bundle_of("/opt/homebrew/bin/afsctool"))

    def test_missing_afsctool_warns_and_changes_nothing(self):
        path = self.write("a.jsonl")
        cfg = dict(self.janitor.DEFAULT_CONFIG, compress={"paths": [self.root], "min_age_minutes": 0})
        sw = self.janitor.Sweep(cfg, dry_run=False)
        original = self.janitor.which, self.janitor.log
        self.janitor.which = lambda name: None
        self.janitor.log = lambda line, path=None: None  # nie do prawdziwego janitor.log
        try:
            self.janitor.task_compress(sw, None)
        finally:
            self.janitor.which, self.janitor.log = original
        self.assertTrue(any("afsctool" in w for w in sw.warnings))
        self.assertFalse(compressed(path))


@unittest.skipUnless(AFSCTOOL, "brak afsctool (brew install afsctool)")
class CompressSweepTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.cleanup)
        self.logs = os.path.join(self.env.work, "logs")
        self.env.config(compress={"paths": [self.logs], "min_age_minutes": 0, "threads": 2})

    def write(self, rel, mode=None):
        path = self.env.file(f"logs/{rel}", text='{"type":"assistant","text":"hello"}\n')
        if mode is not None:
            os.chmod(path, mode)
        return path

    def test_sweep_compresses_and_records_savings(self):
        log = self.write("session.jsonl")
        frozen = self.write("objects/pack.idx", mode=0o444)  # jak obiekty gita: tylko do odczytu
        self.env.sweep()
        self.assertTrue(compressed(log))
        self.assertTrue(compressed(frozen))
        with open(log) as f:
            self.assertTrue(f.read().startswith('{"type"'))
        state = self.env.state()
        self.assertIn("compress", state["task_runs"])
        self.assertGreater(state["last_sweep"]["freed"], 0)

    def test_appended_file_is_compressed_again(self):
        log = self.write("session.jsonl")
        self.env.sweep()
        with open(log, "a") as f:
            f.write("more\n" * 100)
        self.assertFalse(compressed(log))  # APFS zapisuje dopisany plik bez kompresji
        time.sleep(1.1)
        self.env.sweep()
        self.assertTrue(compressed(log))

    def test_open_file_waits(self):
        log = self.write("session.jsonl")
        with open(log, "a"):
            self.env.sweep()
        self.assertFalse(compressed(log))
        self.env.sweep()
        self.assertTrue(compressed(log))

    def test_protected_path_stays(self):
        log = self.write("footage/notes.txt")
        self.env.config(
            compress={"paths": [self.logs], "min_age_minutes": 0},
            protect=[os.path.join(self.logs, "footage")],
        )
        self.env.sweep()
        self.assertFalse(compressed(log))

    def test_dry_run_compresses_nothing(self):
        log = self.write("session.jsonl")
        out = self.env.sweep("--dry-run")
        self.assertIn("kompresja APFS", out)
        self.assertFalse(compressed(log))
        self.assertFalse(os.path.exists(os.path.join(self.env.state_dir, "janitor-compress.json")))

    @unittest.skipUnless(shutil.which("cc"), "brak kompilatora C")
    def test_running_app_stays_whole(self):
        apps = os.path.join(self.env.work, "logs/Apps")
        live = os.path.join(apps, "Live.app/Contents")
        os.makedirs(os.path.join(live, "MacOS"))
        source = os.path.join(self.env.home, "live.c")
        with open(source, "w") as f:
            f.write("#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n")
        binary = os.path.join(live, "MacOS/Live")
        subprocess.run(["cc", "-o", binary, source], check=True)
        resource = self.write("Apps/Live.app/Contents/Resources/strings.txt")
        idle = self.write("Apps/Idle.app/Contents/Resources/strings.txt")
        proc = subprocess.Popen([binary])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.3)
        self.env.sweep()
        self.assertFalse(compressed(resource))
        self.assertTrue(compressed(idle))


if __name__ == "__main__":
    unittest.main()
