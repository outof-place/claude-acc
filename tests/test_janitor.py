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
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

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


class NoRealBannerTest(unittest.TestCase):
    """Przebieg z mało miejsca woła atrapę osascript, nigdy prawdziwego: inaczej każdy przebieg
    testów (nowy $HOME, więc nowa deduplikacja) pokazywałby Filipowi prawdziwy baner."""

    def test_low_disk_sweep_reaches_the_fake_osascript(self):
        env = Env()
        self.addCleanup(env.cleanup)
        env.config(low_disk_gb=10**6)  # każdy dysk ma mniej
        log = os.path.join(env.home, "osascript.log")
        with mock.patch.dict(os.environ, {"CLAUDE_ACC_TEST_OSASCRIPT_LOG": log}):
            self.assertEqual(os.path.dirname(shutil.which("osascript")), os.path.join(HERE, "fakes-osascript"))
            env.sweep()

        with open(log) as f:
            self.assertIn("Mało miejsca na dysku", f.read())


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

    def test_low_disk_alert_carries_its_limit_for_the_panel(self):
        """Panel pokazuje alert z odczytem sprzed nawet 3 godzin; z progiem w alercie porównuje
        go z bieżącym odczytem i chowa, gdy miejsca znów jest dość."""
        self.env.config(protect=[], low_disk_gb=1_000_000)  # każdy dysk jest poniżej
        # świeże powiadomienie: przebieg testowy nie wyśle prawdziwego do Centrum powiadomień
        with open(os.path.join(self.env.state_dir, "janitor-state.json"), "w") as f:
            json.dump({"low_disk_alert_at": time.time()}, f)
        self.env.sweep()
        low = [a for a in self.env.state()["alerts"] if a["kind"] == "low_disk"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["limit"], 1_000_000 * 1024**3)
        self.assertGreater(low[0]["free"], 0)

    def test_idle_next_goes_fresh_stays(self):
        self.app("idle")
        self.app("blog", next_dir=".next-blog")
        self.app("fresh", days_old=0)
        self.env.sweep()
        self.assertFalse(self.env.exists("idle/.next"))
        self.assertFalse(self.env.exists("blog/.next-blog"))
        self.assertTrue(self.env.exists("fresh/.next"))
        self.assertTrue(self.env.exists("idle/package.json"))

    def turbopack_app(self, name, cache_days, dev_cache_days, build_days=2):
        """Build Next 16 z trwałym cache Turbopacka w .next/cache i .next/dev/cache."""
        self.env.file(f"{name}/package.json", OLD, "{}")
        for rel in ("BUILD_ID", "server/app/page.js", "static/chunks/a.js", "dev/server/page.js", "dev/lock"):
            self.env.file(f"{name}/.next/{rel}", build_days)
        self.env.file(f"{name}/.next/cache/turbopack/v1/00001.sst", cache_days)
        self.env.file(f"{name}/.next/cache/fetch-cache/f.json", cache_days)
        self.env.file(f"{name}/.next/dev/cache/turbopack/v1/00001.sst", dev_cache_days)

    def test_idle_next_keeps_recent_turbopack_cache(self):
        """2026-10-09: Turbopack trzyma cache buildu w .next/cache, a nowe worktree się z niego
        rozgrzewają. Build po 24 godzinach idzie, cache zostaje do cache_idle_days."""
        self.turbopack_app("web", cache_days=2, dev_cache_days=2)
        self.env.sweep()
        for rel in ("BUILD_ID", "server", "static", "dev/server", "dev/lock"):
            self.assertFalse(self.env.exists(f"web/.next/{rel}"), rel)
        self.assertTrue(self.env.exists("web/.next/cache/turbopack/v1/00001.sst"))
        self.assertTrue(self.env.exists("web/.next/cache/fetch-cache/f.json"))
        self.assertTrue(self.env.exists("web/.next/dev/cache/turbopack/v1/00001.sst"))
        self.assertIn("next", self.env.state()["task_runs"])
        self.assertGreater(self.env.state()["last_sweep"]["freed"], 0)
        self.env.sweep()  # zostały same cache: kolejny przebieg ich nie rusza
        self.assertTrue(self.env.exists("web/.next/cache/turbopack/v1/00001.sst"))
        self.assertTrue(self.env.exists("web/.next/dev/cache/turbopack/v1/00001.sst"))
        self.assertEqual(sorted(os.listdir(os.path.join(self.env.work, "web/.next/dev"))), ["cache"])

    def test_turbopack_cache_goes_once_idle_for_cache_days(self):
        self.turbopack_app("stale", cache_days=10, dev_cache_days=10)
        self.turbopack_app("mixed", cache_days=10, dev_cache_days=2)
        self.env.sweep()
        self.assertFalse(self.env.exists("stale/.next"))
        self.assertFalse(self.env.exists("mixed/.next/cache"))
        self.assertFalse(self.env.exists("mixed/.next/server"))
        self.assertFalse(self.env.exists("mixed/.next/dev/server"))
        self.assertTrue(self.env.exists("mixed/.next/dev/cache/turbopack/v1/00001.sst"))

    def test_fresh_next_keeps_its_build(self):
        self.turbopack_app("live", cache_days=10, dev_cache_days=10, build_days=0)
        self.env.sweep()
        self.assertTrue(self.env.exists("live/.next/server/app/page.js"))
        self.assertTrue(self.env.exists("live/.next/cache/turbopack/v1/00001.sst"))

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


class DerivedDataTest(unittest.TestCase):
    """DerivedData projektów idzie po derived_data_idle_days, wspólne cache Xcode zostają:
    CAS kompilacji (CompilationCache.noindex) odtwarza się zimnym buildem ~2,7 GB."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import janitor

        self.janitor = janitor
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="derived-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.derived = os.path.join(self.root, "Library/Developer/Xcode/DerivedData")
        patched = {"HOME": self.root, "which": lambda name: None, "log": lambda line, path=None: None}
        for name, value in patched.items():
            self.addCleanup(setattr, janitor, name, getattr(janitor, name))
            setattr(janitor, name, value)
        self.sw = janitor.Sweep(dict(janitor.DEFAULT_CONFIG), dry_run=False)

    def entry(self, rel, days_old):
        path = os.path.join(self.derived, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x" * 4096)
        stamp = time.time() - days_old * 86400
        os.utime(path, (stamp, stamp))

    def test_shared_caches_stay_idle_projects_go(self):
        self.entry("Old-abc/Build/Intermediates.noindex/o.o", OLD)
        self.entry("New-def/Build/Intermediates.noindex/o.o", 1)
        self.entry("ModuleCache.noindex/Foundation.pcm", OLD)
        self.entry("CompilationCache.noindex/plugin/v1/data", OLD)
        self.janitor.task_xcode(self.sw, None)
        left = sorted(os.listdir(self.derived))
        self.assertEqual(left, ["CompilationCache.noindex", "ModuleCache.noindex", "New-def"])
        self.assertEqual([path for _, path, _ in self.sw.items], [os.path.join(self.derived, "Old-abc")])


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

    def compress_state(self):
        with open(os.path.join(self.env.state_dir, "janitor-compress.json")) as f:
            return json.load(f)

    def write_compress_state(self, state):
        with open(os.path.join(self.env.state_dir, "janitor-compress.json"), "w") as f:
            json.dump(state, f)

    def dry_run_count(self):
        out = self.env.sweep("--dry-run")
        line = next(l for l in out.splitlines() if l.startswith("kompresja APFS"))
        return int(line.split(": ")[2].split()[0])

    def write_random(self, rel):
        path = os.path.join(self.logs, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(64 * 1024))
        return path

    def test_incompressible_file_is_not_retried_until_it_changes(self):
        noise = self.write_random("media/noise.bin")
        self.env.sweep()
        self.assertFalse(compressed(noise))
        root = os.path.realpath(self.logs)
        state = self.compress_state()
        self.assertIn(noise, state["incompressible"])
        # granica od zera (jak po resecie stanu): nieściśliwy plik i tak nie wraca
        state[root] = 0
        self.write_compress_state(state)
        self.assertEqual(self.dry_run_count(), 0)
        with open(noise, "ab") as f:
            f.write(os.urandom(1024))
        self.assertEqual(self.dry_run_count(), 1)

    def test_state_from_1_20_0_loads(self):
        root = os.path.realpath(self.logs)
        log = self.write("session.jsonl")
        self.write_compress_state({root: time.time() - 3600})
        self.env.sweep()
        self.assertTrue(compressed(log))
        state = self.compress_state()
        self.assertIsInstance(state[root], float)
        self.assertEqual(state["pending_bundles"], {})

    def test_dry_run_compresses_nothing(self):
        log = self.write("session.jsonl")
        out = self.env.sweep("--dry-run")
        self.assertIn("kompresja APFS", out)
        self.assertFalse(compressed(log))
        self.assertFalse(os.path.exists(os.path.join(self.env.state_dir, "janitor-compress.json")))

    def start_live_app(self):
        """Live.app z działającym procesem i Idle.app obok; [proces, plik Live.app, plik Idle.app]"""
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
        return proc, resource, idle

    @unittest.skipUnless(shutil.which("cc"), "brak kompilatora C")
    def test_running_app_stays_whole(self):
        _, resource, idle = self.start_live_app()
        self.env.sweep()
        self.assertFalse(compressed(resource))
        self.assertTrue(compressed(idle))

    @unittest.skipUnless(shutil.which("cc"), "brak kompilatora C")
    def test_running_app_is_compressed_after_it_quits(self):
        proc, resource, _ = self.start_live_app()
        self.env.sweep()
        self.assertFalse(compressed(resource))
        state = self.compress_state()
        bundle = os.path.join(os.path.realpath(self.logs), "Apps/Live.app")
        self.assertEqual(state["pending_bundles"], {os.path.realpath(self.logs): [bundle]})
        proc.kill()
        proc.wait()
        time.sleep(1.1)  # granica katalogu jest już za plikami aplikacji
        self.env.sweep()
        self.assertTrue(compressed(resource))
        self.assertEqual(self.compress_state()["pending_bundles"], {})


class OptimizeTest(unittest.TestCase):
    """`mac optimize` na atrapie `defaults`: co zapisuje, co pamięta i co przywraca."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import janitor

        self.janitor = janitor
        self.dir = tempfile.mkdtemp(prefix="optimize-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.prefs = {
            ("com.apple.dock", "autohide"): "1",
            ("com.apple.dock", "expose-animation-duration"): "0.15",
            ("NSGlobalDomain", "KeyRepeat"): "2",
        }
        self.calls = []

        def run(cmd, timeout=600):
            if cmd[:2] == ["defaults", "read"]:
                return self.prefs.get((cmd[2], cmd[3]))
            if cmd[:2] == ["defaults", "write"]:
                self.prefs[(cmd[2], cmd[3])] = cmd[5]
                return ""
            if cmd[:2] == ["defaults", "delete"]:
                self.prefs.pop((cmd[2], cmd[3]), None)
                return ""
            return None

        self.killed = []
        patches = [
            mock.patch.object(janitor, "run", side_effect=run),
            mock.patch.object(janitor, "BACKUP_PATH", os.path.join(self.dir, "optimize.json")),
            mock.patch.object(janitor, "broken_launch_items", return_value=[]),
            mock.patch.object(janitor, "log"),
            mock.patch.object(
                janitor.subprocess, "run", side_effect=lambda cmd, **kw: self.killed.append(cmd)
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def optimize(self, *args):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            return self.janitor.cmd_optimize({}, list(args))

    def test_writes_snappy_values_and_undo_restores(self):
        before = dict(self.prefs)
        self.assertEqual(self.optimize(), 0)
        self.assertEqual(self.prefs[("NSGlobalDomain", "KeyRepeat")], "1")
        self.assertEqual(self.prefs[("NSGlobalDomain", "QLPanelAnimationDuration")], "0")
        self.assertEqual(self.prefs[("com.apple.dock", "expose-animation-duration")], "0.1")
        self.assertEqual(self.prefs[("com.apple.dock", "autohide-time-modifier")], "0.1")
        self.assertIn(["killall", "Dock"], self.killed)
        self.assertEqual(self.optimize(), 0)  # drugi raz nic nie zmienia
        self.assertEqual(self.optimize("--undo"), 0)
        self.assertEqual(self.prefs, before)

    def test_autohide_speed_only_with_autohide(self):
        self.prefs[("com.apple.dock", "autohide")] = "0"
        self.optimize()
        self.assertNotIn(("com.apple.dock", "autohide-time-modifier"), self.prefs)

    def test_dock_not_restarted_without_dock_changes(self):
        for domain, key, kind, value, _ in self.janitor.TWEAKS:
            if domain == "com.apple.dock":
                self.prefs[(domain, key)] = value
        self.optimize()
        self.assertNotIn(["killall", "Dock"], self.killed)


if __name__ == "__main__":
    unittest.main()
