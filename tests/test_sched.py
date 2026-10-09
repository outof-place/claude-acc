"""Testy sched.py: decyzje na funkcjach i prawdziwe procesy `sched.py run` w osobnym $HOME.

Procesy biegną na podrobionym repo (go.mod, .git, scripts/depot-*.sh) z podróbką `go` na
początku PATH (zapisuje start i koniec do dziennika, śpi, oddaje zadany kod) i z pamięcią z
pliku (SCHED_FAKE_MEMORY), więc wynik nie zależy od tego Maca. Nic nie dotyka prawdziwego
~/.local/share/claude-acc, zamka plock ani ustawień Claude.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "sched.py")

_spec = importlib.util.spec_from_file_location("acc_sched", SCRIPT)
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

FAKE_GO = r"""#!/bin/bash
# podróbka go: list/env odpowiadają z env, reszta zapisuje start i koniec, śpi i oddaje kod
now() { /usr/bin/python3 -c 'import time; print(time.time())'; }
case "$1" in
  env) echo "${FAKE_GOENV_GOFLAGS:--p=4}"; exit 0 ;;
  list)
    last="${@: -1}"
    case " $* " in
      *" -test "*) printf '%s\n' "fmt|/std/fmt|true" "testing|/std/testing|true" ${FAKE_GO_TESTDEPS:-} ;;
      *" -deps "*) printf '%s\n' fmt testing ;;
      *) d="$PWD/${last#./}"; echo "${d%/}" ;;
    esac
    exit 0 ;;
esac
echo "start $$ $(now) GOFLAGS=$GOFLAGS ARGS=$*" >> "$FAKE_GO_LOG"
sleep "${FAKE_GO_SLEEP:-0.3}"
echo "end $$ $(now)" >> "$FAKE_GO_LOG"
echo "fake go done: $*"
exit "${FAKE_GO_RC:-0}"
"""

FAKE_NATIVE = r"""#!/bin/bash
# podróbka portivo-mobile: start i koniec do dziennika, wyjście na oba strumienie
now() { /usr/bin/python3 -c 'import time; print(time.time())'; }
echo "start $$ $(now) ARGS=$(basename "$0") $*" >> "$FAKE_GO_LOG"
echo "building the dev client" >&2
sleep "${FAKE_GO_SLEEP:-0.3}"
echo "end $$ $(now)" >> "$FAKE_GO_LOG"
echo "{\"device\": \"Portivo-1\", \"args\": \"$*\"}"
exit "${FAKE_GO_RC:-0}"
"""

FAKE_DEPOT_CI = r"""#!/bin/bash
echo "[depot-ci] run abcd1234efgh: go-heavy.yml $*" >&2
echo "depot-ci output $*"
sleep 0.2
exit "${FAKE_DEPOT_RC:-0}"
"""

FAKE_DEPOT_EXEC = r"""#!/bin/bash
echo "[depot-exec] uploading" >&2
if [ "${FAKE_EXEC_RC:-0}" = 125 ]; then
  echo "[depot-exec] run 2zwh1lxx2r: setup failed" >&2
  exit 125
fi
echo "depot-exec output $*"
echo "[depot-exec] run 2zwh1lxx2r: exit ${FAKE_EXEC_RC:-0} after 3s on 8 cores (~1 units)" >&2
exit "${FAKE_EXEC_RC:-0}"
"""


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def make_repo(root, depot=True, depot_exec=True):
    os.makedirs(os.path.join(root, ".git"))
    write(
        os.path.join(root, "apps/charter-service/go.mod"),
        "module charter-service\n\ngo 1.27\n",
    )
    write(os.path.join(root, "apps/charter-service/go.sum"), "")
    for pkg in ("internal/handlers", "internal/moneyfmt", "internal/money", "cmd"):
        os.makedirs(os.path.join(root, "apps/charter-service", pkg), exist_ok=True)
    write(
        os.path.join(root, "apps/auth-service/go.mod"),
        "module auth-service\n\ngo 1.27\n",
    )
    if depot:
        write(os.path.join(root, "scripts/depot-ci.sh"), FAKE_DEPOT_CI, 0o755)
    if depot_exec:
        write(os.path.join(root, "scripts/depot-exec.sh"), FAKE_DEPOT_EXEC, 0o755)


class Paths(unittest.TestCase):
    """Moduł z wszystkimi ścieżkami w katalogu testu i pamięcią z pliku."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="sched-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        state_dir = os.path.join(self.dir, "state")
        sched_dir = os.path.join(state_dir, "sched")
        for name, value in {
            "STATE_DIR": state_dir,
            "SCHED_DIR": sched_dir,
            "STATE_PATH": os.path.join(sched_dir, "state.json"),
            "HISTORY_PATH": os.path.join(sched_dir, "history.jsonl"),
            "LOCK_PATH": os.path.join(sched_dir, "lock"),
            "CONFIG_PATH": os.path.join(sched_dir, "config.json"),
            "CACHE_PATH": os.path.join(sched_dir, "cache.json"),
            "DEPOT_PATH": os.path.join(sched_dir, "depot.json"),
            "DEPOT_LOCK": os.path.join(sched_dir, "depot.lock"),
            "DEVGUARD_STATE": os.path.join(state_dir, "devguard-state.json"),
            "DEVGUARD_CONFIG": os.path.join(state_dir, "devguard.json"),
            "SELF": os.path.join(state_dir, "sched.py"),
        }.items():
            patcher = mock.patch.object(S, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        os.makedirs(sched_dir)
        self.repo = os.path.join(self.dir, "repo")
        make_repo(self.repo)
        self.charter = os.path.join(self.repo, "apps/charter-service")
        self.memfile = os.path.join(self.dir, "mem.json")
        self.set_memory(60)
        patcher = mock.patch.dict(os.environ, {"SCHED_FAKE_MEMORY": self.memfile})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = dict(S.DEFAULTS)

    def set_memory(self, level, swap=1.0, pressure="normal", ram=48, native=None):
        # native: natywne buildy na Macu; bez tego test zależałby od xcodebuild, który akurat biegnie
        native = native or {"gb": 0.0, "active": False}
        with open(self.memfile, "w") as f:
            json.dump(
                {"level": level, "ram_gb": ram, "swap_gb": swap, "pressure": pressure, "native": native},
                f,
            )

    def job(self, command):
        j = S.classify(command, self.repo)
        self.assertIsNotNone(j, command)
        return j

    def state(self):
        st = S.load_state(self.cfg)
        S.refresh_memory(st, self.cfg)
        return st


class ClassifyTest(Paths):
    def test_shapes_agents_use(self):
        cases = {
            "cd apps/charter-service && rtk proxy go vet ./... 2>&1 | tail -20": "charter-service:vet:tree",
            "cd apps/charter-service && go test -count=1 -run '^$' ./...": "charter-service:test:tree:compile",
            "cd apps/charter-service && go test -run TestX ./internal/handlers/": "charter-service:test:pkg:internal/handlers:filtered",
            "cd apps/charter-service && go test ./internal/handlers/": "charter-service:test:pkg:internal/handlers",
            "cd apps/charter-service && go test -race ./...": "charter-service:test:tree:race",
            "rtk proxy go -C apps/auth-service test ./...": "auth-service:test:tree",
            "make -C apps/charter-service test-tenant-leakage": "charter-service:make:test-tenant-leakage",
            "cd apps/charter-service && golangci-lint run --concurrency=4 ./...": "charter-service:lint:tree",
            "cd apps/charter-service && go test ./internal/moneyfmt/ -count=1 -v > /tmp/x 2>&1": "charter-service:test:pkg:internal/moneyfmt",
            "cd apps/charter-service && go test charter-service/internal/money": "charter-service:test:pkg:internal/money",
        }
        for command, cls in cases.items():
            self.assertEqual(self.job(command)["class"], cls, command)

    def test_flags_and_shape(self):
        j = self.job(
            "cd apps/charter-service && GOFLAGS=-p=2 go test -count=1 ./internal/money/"
        )
        self.assertEqual(j["p_explicit"], 2)
        self.assertTrue(j["count1"])
        self.assertTrue(j["simple"])
        self.assertEqual(
            self.job("cd apps/charter-service && go test -p 6 ./...")["p_explicit"], 6
        )
        self.assertEqual(
            self.job(
                "cd apps/charter-service && golangci-lint run --concurrency=8 ./..."
            )["p_explicit"],
            8,
        )
        multi = self.job("cd apps/charter-service && go build ./... && go vet ./...")
        self.assertEqual(multi["kind"], "vet")  # cięższy z dwóch
        self.assertTrue(multi["multi"])
        self.assertFalse(multi["simple"])
        self.assertFalse(
            self.job("cd apps/charter-service && go test ./... | python3 parse.py")[
                "simple"
            ]
        )

    def test_not_for_scheduler(self):
        for command in (
            "git status && rtk grep -rn foo .",
            "echo 'go test ./...'",
            "python3 /Users/x/.claude/portivo-locks/plock.py go 600 -- go test ./...",
            "scripts/depot-exec.sh --cores 8 -- go test ./...",
            "scripts/depot-ci.sh go-heavy.yml full",
            "/usr/bin/python3 ~/.local/share/claude-acc/sched.py run --shell 'go test ./...'",
            "SCHED_OFF=1 go test ./...",
            "go version",
            "cd /tmp && go test ./...",  # bez go.mod
        ):
            self.assertIsNone(S.classify(command, self.repo), command)

    def test_argv_forms(self):
        j = S.classify(None, self.charter, argv=["go", "vet", "./..."])
        self.assertEqual(j["class"], "charter-service:vet:tree")
        j = S.classify(
            None,
            self.repo,
            argv=["bash", "-c", "cd apps/charter-service && go build ./..."],
        )
        self.assertEqual(j["class"], "charter-service:build:tree")
        opaque = S.opaque_job(["./scripts/por25-gorun.sh", "x"], self.repo)
        self.assertEqual(opaque["class"], "repo:script:por25-gorun.sh")
        self.assertEqual(S.prior(opaque, 4), (8.0, 300))


class SubtreeTest(Paths):
    """`./x/...` obejmuje część modułu. Pomyłka, którą ten test łapie: wzorzec na jeden mały
    pakiet przewidziany jak cały moduł (24 GB, 25 min) i wysłany na Depot do joba `full`, który
    za ~$1,7 testuje cały moduł zamiast komendy agenta (portivo, 2026-10-05: 4 takie biegi)."""

    PACKAGES = (
        "cmd",
        "internal/handlers",
        "internal/handlers/e2e",
        "internal/money",
        "internal/moneyfmt",
        "internal/worker/emailsend",
        "internal/worker/emailsend/tmpl",
    )
    EMAILSEND = "internal/worker/emailsend"

    def setUp(self):
        super().setUp()
        for pkg in self.PACKAGES:
            write(os.path.join(self.charter, pkg, "x.go"), "package x\n")
        write(os.path.join(self.charter, "internal/money/testdata/fixture.go"), "package fixture\n")

    def test_part_of_the_module_is_a_subtree(self):
        subtree = ("subtree", self.EMAILSEND)
        cases = {
            "cd apps/charter-service && go test -count=1 ./internal/worker/emailsend/...": subtree,
            "cd apps/charter-service/internal/worker && go test ./emailsend/...": subtree,
            "cd apps/charter-service/internal/worker/emailsend && go test ./...": subtree,
            "cd apps/charter-service && go test charter-service/internal/worker/emailsend/...": subtree,
            "cd apps/charter-service && go test ./internal/money/... ./internal/moneyfmt/...": (
                "subtree",
                "internal/money internal/moneyfmt",
            ),
            "cd apps/charter-service && go test ./...": ("tree", "./..."),
            "cd apps/charter-service && go test charter-service/...": ("tree", "./..."),
            "cd apps/charter-service && go test ./internal/money/... ./...": ("tree", "./..."),
        }
        for command, expected in cases.items():
            j = self.job(command)
            self.assertEqual((j["scope"], j["scope_detail"]), expected, command)
        self.assertEqual(
            self.job("cd apps/charter-service && go test ./internal/worker/emailsend/...")["class"],
            "charter-service:test:subtree:internal/worker/emailsend",
        )

    def test_prior_grows_with_the_share_of_packages(self):
        # 2 z 7 pakietów (testdata się nie liczy): między jednym pakietem (3 GB, 40 s) a całym
        # modułem (24 GB, 1500 s), w proporcji
        small = self.job("cd apps/charter-service && go test ./internal/worker/emailsend/...")
        self.assertEqual(S.prior(small, 4), (9.0, 457))
        # internal/handlers to sam w sobie 24 GB i 25 min: poddrzewo z nim nie może być lżejsze
        for command in (
            "cd apps/charter-service && go test ./internal/handlers/...",
            "cd apps/charter-service && go test ./internal/...",
        ):
            self.assertEqual(S.prior(self.job(command), 4), (24.0, 1500), command)
        # wzorca z ... w środku nie rozwiązujemy: ostrożnie, jak cały moduł
        odd = self.job("cd apps/charter-service && go test ./internal/.../tmpl")
        self.assertEqual(S.prior(odd, 4), (24.0, 1500))

    def test_depot_runs_the_agents_command_not_the_full_suite(self):
        command = "cd apps/charter-service && go test -count=1 ./internal/worker/emailsend/..."
        small = self.job(command)
        gb, wall = S.prior(small, 4)
        target = S.depot_target(small, gb, wall, self.cfg, {})
        self.assertEqual(target["target"], "depot-exec")
        self.assertEqual(
            target["argv"][-5:],
            ["--", "go", "test", "-count=1", "./internal/worker/emailsend/..."],
        )
        st = self.state()
        self.assertEqual(S.decide_route(st, small, gb, wall, self.cfg, {})[0]["choice"], "local")
        tree = self.job("cd apps/charter-service && go test ./...")
        self.assertEqual(S.depot_target(tree, 24.0, 1500, self.cfg, {})["job"], "full")


class WaitTest(Paths):
    """Subagent z 5-minutowym cache czeka na długą pracę krótkimi wywołaniami. Pomyłki, które ten
    test łapie: czekanie dłuższe niż żyje cache (następne wywołanie zapisuje cały kontekst od
    nowa), sukces przed spełnieniem warunku i wyjście, po którym agent nie wie, że ma wołać dalej."""

    def wait(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = S.cmd_wait(list(args))
        return rc, out.getvalue()

    def test_returns_as_soon_as_the_condition_holds(self):
        flag = os.path.join(self.dir, "done")
        timer = threading.Timer(0.3, lambda: open(flag, "w").close())
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.time()
        rc, out = self.wait("--max", "5", "--every", "0.1", "--", f"test -e {shlex.quote(flag)}")
        self.assertEqual(rc, 0, out)
        self.assertLess(time.time() - start, 3)

    def test_stops_at_the_cap_and_says_to_call_again(self):
        start = time.time()
        rc, out = self.wait("--max", "0.5", "--every", "0.1", "--", "false")
        self.assertEqual(rc, S.EXIT_TIMEOUT)
        self.assertLess(time.time() - start, 2)
        self.assertIn("ponownie", out)

    def test_default_cap_fits_a_five_minute_cache(self):
        rc, out = self.wait("--max", "x", "--", "true")
        self.assertEqual(rc, 64)  # zły limit to błąd, nie czekanie bez końca
        self.assertLess(S.WAIT_MAX_S, 300)


class PredictTest(Paths):
    def test_priors_from_measurements(self):
        tree = self.job("cd apps/charter-service && go test -run '^$' ./...")
        self.assertEqual(S.prior(tree, 8), (11.6, 72))
        self.assertEqual(S.prior(tree, 4), (8.4, 150))
        vet = self.job("cd apps/charter-service && go vet ./...")
        self.assertEqual(S.prior(vet, 4), (13.7, 41))
        self.assertGreater(
            S.prior(vet, 16)[0], 25.7
        )  # ponad zmierzone p8 rośnie liniowo
        race = self.job("cd apps/charter-service && go test -race ./...")
        self.assertGreater(S.prior(race, 4)[0], 30)
        auth = self.job("go -C apps/auth-service vet ./...")
        self.assertEqual(S.prior(auth, 4), (1.5, 40))

    def test_history_wins_and_needs_three_runs(self):
        vet = self.job("cd apps/charter-service && go vet ./...")
        rows = [
            {
                "where": "local",
                "class": vet["class"],
                "p": 4,
                "peak_gb": 5.0,
                "wall_s": 30.0,
            }
        ]
        gb, s, src = S.predict(vet, 4, rows)
        self.assertEqual(src, "history:1")
        self.assertEqual(
            gb, round(13.7 * 0.8, 2)
        )  # jeden bieg: nie mniej niż 80% priora
        rows = [dict(rows[0], peak_gb=10.0) for _ in range(5)]
        gb, s, src = S.predict(vet, 4, rows)
        self.assertEqual((gb, s), (11.5, 30.0))  # p90 10 GB × 1,15
        self.assertEqual(
            S.predict(vet, 8, rows)[2], "prior"
        )  # inne -p: osobna historia

    def test_p_insensitive_classes_use_all_runs(self):
        small = self.job("cd apps/charter-service && go test ./internal/moneyfmt/")
        rows = [
            {
                "where": "local",
                "class": small["class"],
                "p": p,
                "peak_gb": 0.2,
                "wall_s": 1.0,
            }
            for p in (None, 2, 4)
        ]
        gb, s, src = S.predict(small, 4, rows)
        self.assertEqual(src, "history:3")
        self.assertEqual((gb, s), (0.23, 1.0))


class ChoosePTest(Paths):
    def test_fastest_that_fits(self):
        tree = self.job("cd apps/charter-service && go test -run '^$' ./...")
        self.assertEqual(S.choose_p(tree, 20.0, [])[:2], (8, "scheduler"))
        self.assertEqual(S.choose_p(tree, 9.0, [])[0], 4)
        self.assertEqual(
            S.choose_p(tree, 5.0, [])[0], 2
        )  # nic się nie mieści: najmniejszy szczyt
        vet = self.job("cd apps/charter-service && go vet ./...")
        self.assertEqual(S.choose_p(vet, 30.0, [])[0], 4)  # p8 wolniejsze przy vet
        self.assertEqual(S.choose_p(vet, 10.0, [])[0], 2)
        build = self.job("cd apps/charter-service && go build ./...")
        self.assertEqual(
            S.choose_p(build, 30.0, [])[0], 4
        )  # -p bez wpływu: domyślne maszyny

    def test_agent_and_insensitive(self):
        mine = self.job("cd apps/charter-service && go vet -p 2 ./...")
        self.assertEqual(S.choose_p(mine, 30.0, [])[:2], (2, "agent"))
        small = self.job("cd apps/charter-service && go test ./internal/moneyfmt/")
        self.assertEqual(S.choose_p(small, 30.0, [])[:2], (None, "default"))


class RouteTest(Paths):
    def running(self, st, gb, wall=60, started_ago=0):
        st["running"].append(
            {
                "id": f"r{len(st['running'])}",
                "where": "local",
                "label": "go vet ./...",
                "mem_predicted_gb": gb,
                "mem_now_gb": 0.0,
                "predicted_wall_s": wall,
                "started_at": time.time() - started_ago,
            }
        )
        S.refresh_memory(st, self.cfg)

    def test_fits_cannot_fit_and_cost(self):
        st = self.state()  # level 60: 28,8 GB, wolne 20,8
        vet = self.job("cd apps/charter-service && go vet ./...")
        route, target = S.decide_route(st, vet, 13.7, 41, self.cfg, {})
        self.assertEqual(
            (route["choice"], route["why"], route["text"]),
            ("local", "fits", "local, fits"),
        )
        race = self.job("cd apps/charter-service && go test -race ./...")
        route, target = S.decide_route(st, race, 33.6, 2250, self.cfg, {})
        self.assertEqual(
            (route["choice"], route["why"], target["job"]),
            ("depot", "cannot_fit", "full"),
        )
        self.assertIn("needs 34 GB", route["text"])
        # handlers bez filtra czeka za vet: 25 min lokalnie wobec ~8 min na Depot
        self.running(st, 13.7, wall=41)
        handlers = self.job("cd apps/charter-service && go test ./internal/handlers/")
        route, target = S.decide_route(st, handlers, 24.0, 1500, self.cfg, {})
        self.assertEqual(
            (route["choice"], route["why"], target["job"]),
            ("depot", "cost", "handlers"),
        )
        self.assertGreater(
            route["saves_s"], self.cfg["lambda_s_per_unit"] * route["units"]
        )

    def test_fits_but_depot_is_much_faster(self):
        self.set_memory(75)  # 36 GB: cały handlers (24 GB) się mieści
        st = self.state()
        handlers = self.job("cd apps/charter-service && go test ./internal/handlers/")
        route, target = S.decide_route(st, handlers, 24.0, 1500, self.cfg, {})
        self.assertEqual(
            (route["choice"], route["why"], target["job"]),
            ("depot", "cost", "handlers"),
        )
        self.assertEqual(route["local_eta_s"], 1500)
        # cały moduł: 64 rdzenie przez ~9 min kosztują więcej, niż oszczędzają
        tree = self.job("cd apps/charter-service && go test ./...")
        route, target = S.decide_route(st, tree, 24.0, 1500, self.cfg, {})
        self.assertEqual((route["choice"], route["why"]), ("local", "fits"))

    def test_waiting_small_job_stays_local(self):
        st = self.state()
        self.running(st, 18.0, wall=30)
        small = self.job(
            "cd apps/charter-service && go test -run TestX ./internal/handlers/"
        )
        route, target = S.decide_route(st, small, 5.3, 38, self.cfg, {})
        self.assertEqual((route["choice"], route["why"]), ("local", "waits"))
        self.assertIsNone(target)
        self.assertIn("Depot would cost $", route["text"])

    def test_small_job_stuck_behind_heavy_goes_to_depot(self):
        st = self.state()
        self.running(st, 18.0, wall=300)  # wolne 2,8 GB przez 5 minut
        small = self.job("cd apps/charter-service && go test ./internal/moneyfmt/")
        route, target = S.decide_route(st, small, 3.0, 40, self.cfg, {})
        self.assertEqual(
            (route["choice"], route["why"], target["cores"]), ("depot", "cost", 2)
        )
        self.assertLess(route["units"], 2)

    def test_lambda_moves_the_line(self):
        st = self.state()
        self.running(st, 18.0, wall=600)
        small = self.job(
            "cd apps/charter-service && go test -run TestX ./internal/handlers/"
        )
        cheap = dict(self.cfg, lambda_s_per_unit=1.0)
        self.assertEqual(
            S.decide_route(st, small, 5.3, 38, cheap, {})[0]["choice"], "depot"
        )
        dear = dict(
            self.cfg, lambda_s_per_unit=1000.0
        )  # 540 s oszczędności < 1000 × 1,6 jednostki
        self.assertEqual(
            S.decide_route(st, small, 5.3, 38, dear, {})[0]["choice"], "local"
        )

    def test_local_files_keep_the_job_off_depot(self):
        # Depot dostaje drzewo repo pod inną ścieżką, bez zmiennych z komendy, i nie odsyła plików:
        # nakładka ze scratchpadu dawała tam exit 1 bez testów, czyli fałszywy czerwony wynik
        st = self.state()
        self.running(st, 18.0, wall=300)  # moneyfmt bez tego poszedłby na Depot (test wyżej)
        here = "cd apps/charter-service && "
        for command in (
            "go test -overlay=/tmp/o.json ./internal/moneyfmt/",
            "go test -overlay /tmp/o.json ./internal/moneyfmt/",
            "go test -coverprofile=c.out ./internal/moneyfmt/",
            "go test -modfile=../../../alt.mod ./internal/moneyfmt/",
            "GOFLAGS=-overlay=$TMPDIR/o.json go test ./internal/moneyfmt/",
            "FIXTURES=~/fx go test ./internal/moneyfmt/",
            "go test ./internal/moneyfmt/ -args -golden=/tmp/g",
        ):
            job = self.job(here + command)
            self.assertIsNone(S.depot_target(job, 3.0, 40, self.cfg, {}), command)
            route, target = S.decide_route(st, job, 3.0, 40, self.cfg, {})
            self.assertEqual((route["choice"], target), ("local", None), command)
            self.assertIn("local only", route["text"], command)
        # wzorzec -run ze slashem i samo -count w GOFLAGS to nie pliki: dalej Depot
        clean = self.job(here + "GOFLAGS=-count=1 go test -run 'TestA/b' ./internal/moneyfmt/")
        self.assertEqual(S.decide_route(st, clean, 3.0, 40, self.cfg, {})[0]["choice"], "depot")
        # nie zmieści się nigdy: zamiast Depot zostaje lokalnie i mówi dlaczego
        race = self.job(here + "go test -race -coverprofile=/tmp/c.out ./...")
        route, target = S.decide_route(st, race, 33.6, 2250, self.cfg, {})
        self.assertEqual((route["choice"], target), ("local", None))
        self.assertIn("-coverprofile", route["text"])

    def test_no_depot_route(self):
        shutil.rmtree(os.path.join(self.repo, "scripts"))
        st = self.state()
        self.running(st, 18.0)
        vet = self.job("cd apps/charter-service && go vet ./...")
        route, target = S.decide_route(st, vet, 13.7, 41, self.cfg, {})
        self.assertEqual(
            (route["choice"], route["why"], target), ("local", "no_depot", None)
        )

    def test_depot_exec_size_and_eta_from_depot_cost(self):
        vet = self.job("cd apps/charter-service && go vet ./...")
        target = S.depot_target(vet, 13.7, 41, self.cfg, {})
        self.assertEqual(
            (target["target"], target["cores"]), ("depot-exec", 8)
        )  # 32 GB ≥ 13,7/0,85
        self.assertEqual(
            target["argv"][:6],
            [
                "scripts/depot-exec.sh",
                "--cores",
                "8",
                "--dir",
                "apps/charter-service",
                "--",
            ],
        )
        cache = {"depot_eta": {"jobs": {"depot-exec-8/exec": {"p50_s": 90}}}}
        self.assertEqual(S.depot_target(vet, 13.7, 41, self.cfg, cache)["eta_s"], 90)
        self.assertEqual(S.depot_target(vet, 13.7, 41, self.cfg, cache)["units"], 6.0)
        filtered = self.job(
            "cd apps/charter-service && go test -run TestX ./internal/handlers/"
        )
        self.assertEqual(
            S.depot_target(filtered, 5.3, 38, self.cfg, {})["target"], "depot-exec"
        )  # nie pełny job handlers
        argv = S.depot_target(dict(filtered, uses_pg=True), 5.3, 38, self.cfg, {})[
            "argv"
        ]
        self.assertEqual(
            argv[:5], ["scripts/depot-exec.sh", "--cores", "2", "--with", "pg"]
        )
        self.assertNotIn("--with", S.depot_target(vet, 13.7, 41, self.cfg, {})["argv"])


class PlanTest(Paths):
    def entry(self, jid, gb, small=False, ago=0):
        return {
            "id": jid,
            "label": jid,
            "mem_predicted_gb": gb,
            "small": small,
            "enqueued_at": time.time() - ago,
            "route": {"choice": "local"},
        }

    def test_fifo_overtake_and_reservation(self):
        st = self.state()
        st["running"].append(
            {
                "id": "big",
                "where": "local",
                "label": "vet",
                "mem_predicted_gb": 14.0,
                "mem_now_gb": 0.0,
                "predicted_wall_s": 40,
                "started_at": time.time(),
            }
        )
        S.refresh_memory(st, self.cfg)  # wolne 20,8 - 14 = 6,8
        st["queue"] = [
            self.entry("heavy", 13.7, ago=10),
            self.entry("tiny", 0.3, small=True, ago=5),
            self.entry("mid", 6.0, ago=1),
        ]
        admitted = S.plan(st, self.cfg, time.time())
        self.assertEqual(
            admitted, {"tiny": ("overtake", "heavy")}
        )  # mid nie jest mały, nie wyprzedza
        st["queue"][0]["enqueued_at"] -= 300  # głowa czeka za długo: rezerwacja
        st["queue"][1]["mem_predicted_gb"] = 4.0
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})

    def test_exclusive_when_nothing_runs_and_pressure(self):
        self.set_memory(30)  # 14,4 GB dostępne
        st = self.state()
        st["queue"] = [self.entry("huge", 30.0)]
        self.assertEqual(
            S.plan(st, self.cfg, time.time()), {}
        )  # świeży: może się zwolni
        st["queue"] = [self.entry("huge", 30.0, ago=31)]
        self.assertEqual(
            S.plan(st, self.cfg, time.time()), {"huge": ("fits", None)}
        )  # sam na Macu
        st["queue"] = [self.entry("vet", 13.7)]
        self.set_memory(40)  # 19,2 GB: bez rezerwy na dev serwer mieści się od razu
        st = self.state()
        st["queue"] = [self.entry("vet", 13.7)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vet": ("fits", None)})
        self.set_memory(60, pressure="critical")
        st = self.state()
        st["queue"] = [self.entry("tiny", 0.3, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})

    def test_quick_small_jobs_use_memory_free_now(self):
        # długi job trzyma rezerwę na wzrost, którego jeszcze nie ma; krótki mały job (testy JS,
        # jeden pakiet Go) startuje w pamięci dostępnej teraz, zamiast czekać minutami na cudzą prognozę
        st = self.state()  # level 60: 28,8 GB dostępne
        st["running"].append(
            {"id": "big", "where": "local", "label": "go test ./...", "mem_predicted_gb": 20.0,
             "mem_now_gb": 2.0, "predicted_wall_s": 1500, "started_at": time.time()}
        )
        S.refresh_memory(st, self.cfg)  # wolne 20,8 - 18 rezerwy = 2,8
        st["queue"] = [self.entry("vitest", 3.0, small=True), self.entry("vet", 13.7, ago=1)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vitest": ("overtake", "vet")})
        # głowa czeka ponad starve_s: mały wyprzedza, jeśli zostawia jej miejsce w pamięci teraz,
        # a po 2 × starve_s już nikt (strumień testów JS nie zagłodzi dużego joba Go)
        st["queue"] = [self.entry("vet", 13.7, ago=150), self.entry("vitest", 3.0, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vitest": ("overtake", "vet")})
        st["queue"] = [self.entry("vet", 23.0, ago=150), self.entry("vitest", 3.0, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})  # 24,8 - 23 < 3
        st["queue"] = [self.entry("vet", 13.7, ago=300), self.entry("vitest", 3.0, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        # świeżo wpuszczony mały job jeszcze nie zajął pamięci: liczy się jego prognoza
        st["running"].append(
            {"id": "q1", "where": "local", "label": "vitest", "mem_predicted_gb": 22.0,
             "mem_now_gb": 0.0, "small": True, "predicted_wall_s": 60, "started_at": time.time()}
        )
        S.refresh_memory(st, self.cfg)
        st["queue"] = [self.entry("vitest2", 3.0, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})  # 24,8 - 22 = 2,8 < 3
        self.set_memory(60, pressure="critical")
        st = self.state()
        st["queue"] = [self.entry("tiny", 0.5, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})

    def test_warn_pressure_admits_what_fits(self):
        # macOS trzyma „warn” godzinami przy połowie wolnej pamięci: przy nim startuje to, co
        # mieści się w wolnej pamięci, także kilka naraz; jeden naraz robił z kolejki stary zamek
        self.set_memory(60, pressure="warn")  # 28,8 GB dostępne, wolne 20,8
        st = self.state()
        st["queue"] = [
            self.entry("vet", 13.7, ago=3),
            self.entry("build", 6.6, ago=2),
            self.entry("tiny", 0.3, small=True),
        ]
        self.assertEqual(
            S.plan(st, self.cfg, time.time()),
            {"vet": ("fits", None), "build": ("fits", None), "tiny": ("fits", None)},
        )
        st["running"].append(
            {"id": "vet", "where": "local", "mem_predicted_gb": 13.7, "mem_now_gb": 2.0}
        )
        st["queue"] = [self.entry("build", 6.6, ago=2)]
        S.refresh_memory(st, self.cfg)  # wolne 20,8 - 11,7 rezerwy vet = 9,1
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"build": ("fits", None)})
        self.set_memory(40, pressure="warn")  # 19,2 GB: vet nie mieści się z rezerwą na dev serwer
        st = self.state()
        st["queue"] = [self.entry("vet", 13.7, ago=3)]
        self.assertEqual(
            S.plan(st, self.cfg, time.time()), {"vet": ("fits", None)}
        )  # sam na Macu: bez rezerwy na dev serwer
        self.set_memory(50, pressure="warn")
        st = self.state()
        st["queue"] = [self.entry("huge", 24.0, ago=600)]
        self.assertEqual(
            S.plan(st, self.cfg, time.time()), {}
        )  # przy presji nic ponad dostępną pamięć, nawet po długim czekaniu


class BackfillTest(Paths):
    """Za głową, która się nie mieści, startuje to, co nie opóźni jej startu (backfill w stylu
    EASY). Pomyłka, którą ten test łapie: 2026-10-09 16:40 głowa `next build` (11,7 GB przy 5,9
    wolnych) czekała po 2 × starve_s na twardej rezerwacji pamięci, której i tak nie mogła użyć, a za
    nią 25 krótkich jobów (ruff, go vet jednego pakietu, skrypt Pythona) czekało do 25 minut."""

    def setUp(self):
        super().setUp()
        self.cfg["aging_s"] = 10**9  # aging ma własne testy (JamTest); tu tylko backfill

    def queued(self, jid, gb, wall, ago=0, src="history:20", now=None, **extra):
        now = time.time() if now is None else now
        return dict({"id": jid, "label": jid, "mem_predicted_gb": gb, "predicted_wall_s": wall,
                     "predicted_from": src, "small": gb <= 4.5 and wall <= 120,
                     "enqueued_at": now - ago, "route": {"choice": "local"}}, **extra)

    def running(self, jid, gb, wall, elapsed, src="history:20", now=None, **extra):
        # zajmuje już całą prognozę: bez rezerwy na wzrost wolna pamięć to dostępna - 8 GB
        now = time.time() if now is None else now
        return dict({"id": jid, "label": jid, "where": "local", "mem_predicted_gb": gb, "mem_now_gb": gb,
                     "predicted_wall_s": wall, "predicted_from": src, "started_at": now - elapsed}, **extra)

    def blocked(self, level, running, queue):
        self.set_memory(level)
        st = self.state()
        st["running"] = running
        S.refresh_memory(st, self.cfg)
        st["queue"] = queue
        return st

    def test_jobs_that_end_before_the_head_starts_or_fit_beside_it_start(self):
        # 14,4 GB dostępne, wolne 6,4; skrypt (10 GB) skończy się za 20 min, wtedy wolne 16,4:
        # build się zmieści i zostanie obok niego 4,7 GB
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=600)], [
            self.queued("build", 11.7, 154, ago=1500),
            self.queued("ruff", 0.39, 0.3, ago=600),
            self.queued("sql", 0.13, 27.1, ago=500),
            self.queued("unittest", 0.15, 243, ago=400),  # nie mały (243 s), skończy się przed buildem
            self.queued("vet", 4.0, 60, ago=300, src="prior"),  # bez historii: czas i pamięć zgadnięte
            self.queued("merge", 0.65, 1823, ago=200),  # dłuższy niż czekanie buildu, ale zmieści się obok
            self.queued("e2e", 5.76, 69, ago=100),  # nie mieści się teraz
            self.queued("long", 5.0, 3000, ago=50),  # mieści się teraz, ale nie obok buildu
        ])
        self.assertEqual(
            S.plan(st, self.cfg, time.time()),
            {jid: ("overtake", "build") for jid in ("ruff", "sql", "unittest", "merge")},
        )
        # miejsce obok głowy (4,7 GB) dzielą wszyscy: drugi długi job już się w nim nie zmieści
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=600)], [
            self.queued("build", 11.7, 154, ago=1500),
            self.queued("long1", 3.0, 3000, ago=20), self.queued("long2", 3.0, 3000, ago=10)])
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"long1": ("overtake", "build")})

    def test_job_that_would_delay_the_head_waits(self):
        # skrypt skończy się za 100 s; obok buildu (16,2 GB) zostanie wtedy 0,2 GB
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=1700)], [
            self.queued("build", 16.2, 154, ago=1500),
            self.queued("j60", 1.0, 60, ago=20),  # 2 × 60 + 10 s > 100 s
            self.queued("j40", 1.0, 40, ago=10),  # 2 × 40 + 10 s ≤ 100 s
        ])
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"j40": ("overtake", "build")})
        S.update_queue_view(st, self.cfg)
        self.assertIn("build goes first", st["queue"][1]["reason"]["text"])  # j60
        # głowa zaraz startuje: nawet ruff (0,3 s) nie wchodzi przed nią, a obok niej brak miejsca
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=1798)], [
            self.queued("build", 16.2, 154, ago=1500), self.queued("ruff", 0.39, 0.3)])
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        # ...a gdy obok niej zostanie miejsce, ruff startuje
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=1798)], [
            self.queued("build", 11.7, 154, ago=1500), self.queued("ruff", 0.39, 0.3)])
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"ruff": ("overtake", "build")})

    def test_unknown_forecasts_are_conservative(self):
        behind = [self.queued("ruff", 0.39, 0.3), self.queued("unittest", 0.15, 243),
                  self.queued("merge", 0.65, 1823), self.queued("tiny", 0.1, 1, src="prior"),
                  self.queued("e2e", 5.3, 20)]  # krótki, ale ciężki: gdy się przeciągnie, trzyma 5 GB
        # start głowy nie do przewidzenia, bo prognoza skryptu jest zgadnięta: przechodzą tylko krótkie
        # joby ze zmierzoną prognozą, bo opóźnią ją najwyżej o swój czas
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=600, src="prior")],
                          [self.queued("build", 11.7, 154, ago=1500)] + behind)
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"ruff": ("overtake", "build")})
        # skrypt ze zmierzoną prognozą przeciągnął (2000 s z 1800): według time_left pobiegnie jeszcze
        # ~1000 s, więc lekki unittest (2 × 243 + 10 s) skończy się przed głową (EASY); ciężkie e2e i
        # merge (dłuższy niż to czekanie) nie wchodzą. 2026-10-09 kolejka pisała wtedy „about 5s”
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=2000)],
                          [self.queued("build", 11.7, 154, ago=1500)] + behind)
        self.assertEqual(S.plan(st, self.cfg, time.time()),
                         {"ruff": ("overtake", "build"), "unittest": ("overtake", "build")})
        # głowa zmieściłaby się bez jobów, które ją już wyprzedziły: nikt więcej, aż się skończą
        # (p1 biegnie dłużej, niż miał, więc i z nim start głowy nie do przewidzenia)
        passer = self.running("p1", 5.5, 20, elapsed=25, passed="build")
        st = self.blocked(40, [self.running("script", 10.0, 1800, elapsed=600, src="prior"), passer],
                          [self.queued("build", 11.7, 154, ago=1500), self.queued("ruff", 0.39, 0.3)])
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        passer["passed"] = None  # ten sam job, ale nie wyprzedził buildu: build i tak by się nie zmieścił
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"ruff": ("overtake", "build")})

    def test_light_jobs_start_in_memory_free_now_when_reserves_push_free_below_zero(self):
        """2026-10-09 18:20 (1.25.1 przy starve_s 120): xcodebuild spoza schedulera (rezerwa na
        wzrost do szczytu buildu) i świeży skrypt bez zajętej jeszcze pamięci zepchnęły wolną pamięć
        po rezerwach poniżej zera przy ~14 GB dostępnych teraz. Po 2 × starve_s szybka ścieżka była
        zamknięta, a backfill chciał miejsca po rezerwach, więc ruff i pytest (0,1-0,5 GB, sekundy)
        stały za next build (11,6 GB), choć skończyłyby się, zanim ten mógł ruszyć."""
        queue = [self.queued("build", 11.6, 154, ago=460), self.queued("ruff", 0.46, 1.3),
                 self.queued("pytest", 0.08, 2.3), self.queued("e2e", 5.76, 47)]  # e2e: ciężki
        for why, level, running in (
            # start głowy nie do przewidzenia: skrypt przekroczył prognozę (krótki job, bramka)
            # (w rozgrzewce: po niej rezerwa schodzi do zmierzonego szczytu, reserve_target)
            ("skrypt po prognozie", 37, [self.running("up", 5.9, 4, elapsed=30, mem_now_gb=0.0)]),
            # przewidywalny: skrypt skończy się za 20 min (job kończy się przed głową)
            ("skrypt z prognozą", 30, [self.running("script", 10.0, 1800, elapsed=600)]),
        ):
            self.set_memory(level, native={"gb": 2.5, "active": True})
            st = self.state()
            st["running"] = running
            S.refresh_memory(st, self.cfg)
            self.assertLess(st["memory"]["free_for_admission_gb"], 0, why)
            st["queue"] = [dict(j) for j in queue]
            self.assertEqual(S.plan(st, self.cfg, time.time()),
                             {"ruff": ("overtake", "build"), "pytest": ("overtake", "build")}, why)

    def test_overdue_job_whose_memory_the_head_does_not_need_keeps_its_start_predictable(self):
        """2026-10-09 18:26 (zrzut state.json): głowa e2e (5,76 GB przy 4,7 wolnych) czekała na koniec
        next build (11,6 GB, za ~90 s), a obok biegł pytest (0,08 GB, prognoza 2,2 s) już 4 s. Przez
        niego start głowy był „nie do przewidzenia”, choć zmieściłaby się i bez jego pamięci, więc
        go test jednego pakietu (4,65 GB, 23 s), który skończyłby się przed nią, stał w kolejce: dla
        ścieżki „short” jest za ciężki."""

        def case(overdue_gb, head_gb):
            return self.blocked(26.5, [self.running("build", 11.6, 154, elapsed=62),
                                       self.running("pytest", overdue_gb, 2.2, elapsed=4)],
                                [self.queued("e2e", head_gb, 47.1, ago=705),
                                 self.queued("gotest", 4.65, 23.4, ago=691)])

        st = case(0.08, 5.76)
        free = st["memory"]["free_for_admission_gb"]
        self.assertTrue(4.65 <= free < 5.76, free)
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"gotest": ("overtake", "e2e")})
        # e2e nie czeka na koniec pytest, ale ten może jeszcze biec obok niej: miejsce obok głowy
        # to tylko to, co zwolni next build
        shade = S.shadow(st, 5.76, free, time.time())
        self.assertEqual((shade["sure"], shade["after"]), (True, ["build"]))
        self.assertAlmostEqual(shade["wait_s"], 92, delta=1)
        self.assertAlmostEqual(shade["spare_gb"], free + 11.6 - 5.76)
        # głowa potrzebuje też pamięci joba po prognozie: jej start dalej nie do przewidzenia
        # (po końcu obu zostaje 1,3 GB ponad jej potrzebę, a job po prognozie trzyma 2 GB)
        self.assertEqual(S.plan(case(2.0, 17.0), self.cfg, time.time()), {})
        self.assertEqual(S.plan(case(2.0, 16.0), self.cfg, time.time()), {"gotest": ("overtake", "e2e")})

    def test_native_jobs_and_a_held_native_head_keep_their_rules(self):
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=600)], [
            self.queued("build", 11.7, 154, ago=1500),
            self.queued("boot", 2.5, 20, lang="native", native_tool="simulator")])
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        st = self.blocked(30, [self.running("script", 10.0, 1800, elapsed=600)], [
            self.queued("ios", 10.0, 600, ago=1500, lang="native"), self.queued("ruff", 0.39, 0.3)])
        st["memory"]["guard_level"] = 2  # strażnik: krytyczna presja, natywna głowa wstrzymana
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})

    def simulate(self, blocker_wall, blocker_real, job_gb, cap_gb=24.0, ticks=600):
        """Głowa (11,7 GB, czeka od 25 min) za skryptem (10 GB), a co 3 s przychodzi krótki job
        (prognoza 10 s, naprawdę 15 s). Pamięć dostępna to cap_gb minus to, co biegnie.
        (sekunda startu głowy, ile jobów ją wyprzedziło, ile wyprzedzających biegło przy jej starcie)."""
        now0 = time.time()
        running = [self.running("script", 10.0, blocker_wall, elapsed=0, now=now0)]
        ends = {"script": now0 + blocker_real}
        queue = [self.queued("build", 11.7, 154, ago=1500, now=now0)]
        passed = 0
        for t in range(ticks):
            now = now0 + t
            running = [r for r in running if ends[r["id"]] > now]
            if t % 3 == 0:
                queue.append(self.queued(f"s{t}", job_gb, 10, now=now))
                ends[f"s{t}"] = now + 15
            st = self.blocked((cap_gb - sum(r["mem_now_gb"] for r in running)) / 48 * 100, running, queue)
            for jid, (why, by) in S.plan(st, self.cfg, now).items():
                job = next(j for j in queue if j["id"] == jid)
                queue.remove(job)
                if jid == "build":
                    return t, passed, sum(1 for r in running if r.get("passed") == "build")
                running.append(dict(job, where="local", started_at=now, mem_now_gb=job["mem_predicted_gb"],
                                    passed=by))
                passed += why == "overtake"
        self.fail("głowa nie wystartowała")

    def test_stream_of_short_jobs_still_lets_the_head_start(self):
        for why, wall, real, gb in (
            ("prognoza skryptu trafna", 300, 300, 1.5),
            ("prognoza skryptu trafna, małe joby", 300, 300, 0.4),
            ("skrypt biegnie 3 × dłużej, niż miał", 100, 300, 1.5),
        ):
            start, passed, beside = self.simulate(wall, real, gb)
            # głowa startuje, gdy skończy się skrypt, najwyżej po jednym krótkim jobie (15 s)
            self.assertLessEqual(start, 300 + 15, why)
            self.assertGreater(passed, 50, why)  # a przez 5 minut krótkie joby nie stały w kolejce


class OldLockTest(Paths):
    """Bilans „stary zamek” liczy czekanie przy dawnym `plock go`: jeden job Go naraz, w kolejności
    przyjścia. Pomyłka, którą ten test łapie: 2026-10-09 `sched status` pokazał 38278h, bo przez
    wirtualny zamek szły wszystkie lokalne joby (JS, skrypty, natywne, ~13 naraz), w kolejności
    końca, więc zamek uciekł o 40 h w przyszłość, a każdy job doliczał całą tę kolejkę."""

    def run_entry(self, jid, enq, lang=None):
        return {"id": jid, "label": jid, "where": "local", "lang": lang, "class": jid, "module": "m",
                "repo": "r", "enqueued_at": enq, "started_at": enq, "waited_s": 0.0,
                "mem_predicted_gb": 1.0, "predicted_wall_s": 10,
                "route": {"choice": "local", "why": "fits", "text": "local, fits"}}

    def test_only_go_jobs_in_arrival_order(self):
        t = time.time() - 1000
        st = self.state()
        st["running"] = [self.run_entry("long", t), self.run_entry("a", t + 1),
                         self.run_entry("b", t + 2), self.run_entry("js", t + 3, lang="node")]
        S.save_state(st)
        # krótkie skończyły się pierwsze, ale przy zamku czekałyby na długi, który przyszedł wcześniej
        for jid, wall in (("a", 1), ("b", 1), ("js", 500), ("long", 100)):
            S.finish(jid, self.cfg, 0, wall, 0.5, 1.0)
        today = S.load_state(self.cfg)["today"]
        self.assertAlmostEqual(today["old_lock_wait_s"], 99 + 99, delta=0.5)
        self.assertAlmostEqual(today["wait_saved_s"], 99 + 99, delta=0.5)


class Count1Test(Paths):
    def test_drop_regex(self):
        self.assertEqual(S.drop_count1("go test -count=1 ./x"), "go test ./x")
        self.assertEqual(S.drop_count1("go test ./x -count 1 -v"), "go test ./x -v")
        self.assertIsNone(S.drop_count1("go test -count=10 ./x"))
        self.assertIsNone(S.drop_count1("go test -count=1 ./a && go test -count=1 ./b"))

    def write_go(self, rel, text):
        path = os.path.join(self.charter, rel)
        write(path, text)
        return path

    def fake_list(self, helpers=()):
        """go list dla moneyfmt: pakiet, a w testach dodatkowo pomocniki (ścieżka w module)."""
        moneyfmt = os.path.join(self.charter, "internal/moneyfmt")

        def fake(args, cwd, timeout=60):
            if "-test" in args:
                lines = [
                    "fmt|/std/fmt|true",
                    f"charter-service/internal/moneyfmt|{moneyfmt}|false",
                ]
                lines += [
                    f"charter-service/{h}|{os.path.join(self.charter, h)}|false"
                    for h in helpers
                ]
                return "\n".join(lines)
            if "-deps" in args:
                return "fmt\ncharter-service/internal/moneyfmt"
            return moneyfmt

        return fake

    def test_exec_in_package_tests_or_helpers_keeps_count1(self):
        small = self.job(
            "cd apps/charter-service && go test -count=1 ./internal/moneyfmt/"
        )
        self.write_go("internal/moneyfmt/fmt.go", "package moneyfmt\n")
        test = self.write_go(
            "internal/moneyfmt/fmt_test.go", 'package moneyfmt\n\nimport "testing"\n'
        )
        with mock.patch.object(S, "go_list", side_effect=self.fake_list()):
            cache = {}
            self.assertTrue(S.count1_safe(small, cache))
            write(
                test,
                'package moneyfmt\n\nimport "os/exec"\n\nfunc x() { exec.Command("git", "ls-files") }\n',
            )
            self.assertFalse(
                S.count1_safe(small, cache)
            )  # zmiana pliku: nowe skanowanie
            write(
                test,
                'package moneyfmt\n\nimport sh "os/exec"\n\nfunc x() { sh.CommandContext(nil, "node") }\n',
            )
            self.assertFalse(S.count1_safe(small, cache))  # alias importu
            write(test, "package moneyfmt\n")
        helper = ["internal/testutil"]
        self.write_go(
            "internal/testutil/run.go",
            'package testutil\n\nimport "os/exec"\n\nvar _ = exec.Command("atlas")\n',
        )
        with mock.patch.object(S, "go_list", side_effect=self.fake_list(helper)):
            self.assertFalse(
                S.count1_safe(small, {})
            )  # pomocnik testu uruchamia program
        trusted = ["internal/testhelpers"]
        self.write_go(
            "internal/testhelpers/testpg_docker.go",
            'package testhelpers\n\nimport "os/exec"\n\nvar _ = exec.Command("docker")\n',
        )
        with mock.patch.object(S, "go_list", side_effect=self.fake_list(trusted)):
            cache = {}
            self.assertTrue(
                S.count1_safe(small, cache)
            )  # szablon testpg: wejścia śledzone w procesie
            self.assertTrue(S.uses_pg(small, cache))  # depot-exec dostanie --with pg
        with mock.patch.object(S, "go_list", side_effect=AssertionError("bez go list")):
            self.assertTrue(S.count1_safe(small, cache))  # z cache po podpisie plików

    def test_not_for_gates_flags_or_trees(self):
        race = self.job(
            "cd apps/charter-service && go test -race -count=1 ./internal/moneyfmt/"
        )
        self.assertFalse(S.count1_safe(race, {}))
        tree = self.job("cd apps/charter-service && go test -count=1 ./...")
        self.assertFalse(S.count1_safe(tree, {}))
        gate = self.job("make -C apps/charter-service test-tenant-leakage")
        self.assertFalse(S.count1_safe(gate, {}))


class HookTest(Paths):
    def test_rewrite_keeps_whole_tool_input(self):
        original = "cd apps/charter-service && rtk proxy go test -count=1 ./internal/money/ 2>&1 | tail -5"
        event = {
            "tool_name": "Bash",
            "session_id": "747c2327-abcd",
            "cwd": self.repo,
            "tool_input": {
                "command": original,
                "timeout": 123456,
                "description": "money tests",
                "run_in_background": True,
            },
        }
        out = S.hook_rewrite(event)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", out)
        updated = out["updatedInput"]
        self.assertEqual(
            (updated["timeout"], updated["description"], updated["run_in_background"]),
            (123456, "money tests", True),
        )
        argv = shlex.split(updated["command"])
        self.assertEqual(argv[:5], ["/usr/bin/python3", S.SELF, "run", "--via", "hook"])
        self.assertEqual(argv[argv.index("--session") + 1], "747c2327-abcd")
        self.assertEqual(argv[-2:], ["--shell", original])
        again = dict(event, tool_input=updated)
        self.assertIsNone(S.hook_rewrite(again))  # już owinięte

    def test_hook_runs_through_the_managed_python_and_launcher(self):
        python = os.path.join(S.STATE_DIR, "python")
        launcher = os.path.join(S.STATE_DIR, "acc.py")
        os.symlink(sys.executable, python)
        open(launcher, "w").close()
        original = "cd apps/charter-service && go test ./internal/money/"
        event = {
            "tool_name": "Bash",
            "cwd": self.repo,
            "tool_input": {"command": original},
        }
        updated = S.hook_rewrite(event)["hookSpecificOutput"]["updatedInput"]
        argv = shlex.split(updated["command"])
        self.assertEqual(argv[:6], [python, launcher, "sched", "run", "--via", "hook"])
        self.assertEqual(argv[-2:], ["--shell", S.with_rtk(original)])
        self.assertIsNone(
            S.hook_rewrite(dict(event, tool_input=updated))
        )  # już owinięte
        os.remove(
            launcher
        )  # starsza instalacja bez acc.py: sam plik i systemowy python
        argv = shlex.split(
            S.hook_rewrite(event)["hookSpecificOutput"]["updatedInput"]["command"]
        )
        self.assertEqual(argv[:2], ["/usr/bin/python3", S.SELF])

    def test_launcher_in_a_quoted_path_is_not_wrapped_twice(self):
        state = os.path.join(self.dir, "home with space/.local/share/claude-acc")
        os.makedirs(state)
        for name, value in (
            ("STATE_DIR", state),
            ("SELF", os.path.join(state, "sched.py")),
        ):
            patcher = mock.patch.object(S, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        os.symlink(sys.executable, os.path.join(state, "python"))
        open(os.path.join(state, "acc.py"), "w").close()
        event = {
            "tool_name": "Bash",
            "cwd": self.repo,
            "tool_input": {"command": "cd apps/charter-service && go test ./..."},
        }
        updated = S.hook_rewrite(event)["hookSpecificOutput"]["updatedInput"]
        self.assertIn("acc.py' sched run", updated["command"])
        # owinięta komenda sama mówi, że jest owinięta (SKIP_MARKERS), nie tylko przez to,
        # że klasyfikacja nie widzi Go w cudzysłowie
        self.assertTrue(any(m in updated["command"] for m in S.SKIP_MARKERS))
        self.assertIsNone(S.hook_rewrite(dict(event, tool_input=updated)))

    def test_depot_eta_runs_on_the_managed_python(self):
        """`depot-cost.py eta` idzie tym samym pythonem co reszta, bez niego systemowym."""
        write(
            os.path.join(self.repo, "scripts/depot-cost.py"),
            'import json\nprint(json.dumps({"go-heavy/full": {"p50_s": 99}}))\n',
        )
        used = os.path.join(self.dir, "used")
        write(
            os.path.join(S.STATE_DIR, "python"),
            f'#!/bin/sh\necho "$@" >> {used}\nexec /usr/bin/python3 "$@"\n',
            0o755,
        )
        cache = {}
        self.assertTrue(S.refresh_depot_eta(cache, self.repo))
        self.assertEqual(cache["depot_eta"]["jobs"]["go-heavy/full"]["p50_s"], 99)
        with open(used) as f:
            self.assertIn("depot-cost.py eta --json", f.read())
        os.remove(os.path.join(S.STATE_DIR, "python"))
        os.remove(used)
        self.assertTrue(S.refresh_depot_eta({}, self.repo))
        self.assertFalse(os.path.exists(used))

    def test_recursive_grep_is_left_to_rg_rewrite(self):
        command = "cd apps/charter-service && go test ./internal/moneyfmt/ && grep -rn TODO internal"
        event = {
            "tool_name": "Bash",
            "cwd": self.repo,
            "tool_input": {"command": command},
        }
        self.assertIsNone(S.hook_rewrite(event))
        event["tool_input"]["command"] = (
            "cd apps/charter-service && go test ./internal/moneyfmt/ | grep -c ok"
        )
        self.assertIsNotNone(S.hook_rewrite(event))

    def test_leaves_other_commands(self):
        for command in (
            "git status",
            "scripts/depot-exec.sh --cores 8 -- go test ./...",
            "ls -la",
        ):
            event = {
                "tool_name": "Bash",
                "cwd": self.repo,
                "tool_input": {"command": command},
            }
            self.assertIsNone(S.hook_rewrite(event), command)
        self.assertIsNone(
            S.hook_rewrite({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
        )

    def test_hook_loads_nothing_it_does_not_use(self):
        """Hook idzie przy każdej komendzie Go: ctypes, subprocess i reszta czekają na `run`;
        subprocess (z tym, co sam ładuje) dochodzi tylko po to, żeby zapytać rtk, gdy jest."""
        event = {
            "tool_name": "Bash",
            "cwd": self.repo,
            "tool_input": {"command": "cd apps/charter-service && go test ./..."},
        }
        report = (
            "heavy = ('ctypes', 'subprocess', 'hashlib', 'random', 'threading', 'signal', 'fcntl')\n"
            "print(bool(out), [h for h in heavy if h in sys.modules])\n"
        )
        code = (
            "import json, sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            "m = type(sys)('acc_sched')\n"
            f"SourceFileLoader('acc_sched', {SCRIPT!r}).exec_module(m)\n"
            f"out = m.hook_rewrite(json.loads({json.dumps(event)!r}))\n" + report
        )
        path = os.environ.get("PATH", "")
        no_rtk = os.pathsep.join(
            d for d in path.split(os.pathsep) if not os.path.exists(os.path.join(d, "rtk"))
        )
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=dict(os.environ, PATH=no_rtk),
        )
        self.assertEqual(done.stdout.strip(), "True []")
        if shutil.which("rtk"):
            # z rtk dochodzi sam subprocess z tym, co on ładuje w tej wersji Pythona
            alone = subprocess.run(
                [sys.executable, "-I", "-S", "-c", "import subprocess, sys\nout = True\n" + report],
                capture_output=True,
                text=True,
                check=True,
            )
            done = subprocess.run(
                [sys.executable, "-I", "-S", "-c", code],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(done.stdout.strip(), alone.stdout.strip())
            self.assertIn("subprocess", done.stdout)


class RtkTest(Paths):
    """Hook rtk ma komendy schedulera w exclude_commands, więc `rtk` dokłada scheduler, pytając
    `rtk rewrite` z pustym HOME (bez naszych wyjątków): jedno źródło reguł rtk. Pomyłki, które ten
    test łapie: nasze wyjątki wpadające do zapytania (wtedy rtk nic nie przepisuje), wynik z kodem
    1 albo błędem wzięty za przepisanie, i hook bez rtk w środku."""

    def fake_rtk(self, rc, out):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

        return calls, run

    def test_asks_rtk_without_our_exclusions(self):
        calls, run = self.fake_rtk(3, "rtk go test ./...\n")
        with mock.patch.object(S, "which", return_value="/opt/homebrew/bin/rtk"), \
                mock.patch("subprocess.run", side_effect=run):
            self.assertEqual(S.with_rtk("go test ./..."), "rtk go test ./...")
        argv, kw = calls[0]
        self.assertEqual(argv, ["/opt/homebrew/bin/rtk", "rewrite", "go test ./..."])
        self.assertNotEqual(kw["env"]["HOME"], os.environ.get("HOME"))
        self.assertTrue(kw["env"]["HOME"].startswith(S.STATE_DIR))

    def test_keeps_the_command_when_rtk_has_nothing(self):
        for rc, out in ((1, ""), (0, ""), (2, "garbage")):
            calls, run = self.fake_rtk(rc, out)
            with mock.patch.object(S, "which", return_value="/x/rtk"), \
                    mock.patch("subprocess.run", side_effect=run):
                self.assertEqual(S.with_rtk("go run ./cmd/x"), "go run ./cmd/x", rc)
        with mock.patch.object(S, "which", return_value=None):
            self.assertEqual(S.with_rtk("go test ./..."), "go test ./...")
        with mock.patch.object(S, "which", return_value="/x/rtk"), mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("rtk", 3)
        ):
            self.assertEqual(S.with_rtk("go test ./..."), "go test ./...")

    @unittest.skipUnless(shutil.which("rtk"), "rtk nie jest zainstalowane")
    def test_real_rtk_rewrites_go_and_node(self):
        self.assertEqual(S.with_rtk("go test ./..."), "rtk go test ./...")
        self.assertEqual(
            S.with_rtk("cd apps/web && npx vitest run src/a.test.ts"),
            "cd apps/web && rtk vitest src/a.test.ts",
        )
        self.assertEqual(S.with_rtk("go run ./cmd/x"), "go run ./cmd/x")

    def test_hook_runs_rtk_inside_the_wrapper(self):
        original = "cd apps/charter-service && go test ./internal/money/ 2>&1 | tail -5"
        event = {"tool_name": "Bash", "cwd": self.repo, "tool_input": {"command": original}}
        inner = "cd apps/charter-service && rtk go test ./internal/money/ 2>&1 | tail -5"
        with mock.patch.object(S, "with_rtk", return_value=inner):
            updated = S.hook_rewrite(event)["hookSpecificOutput"]["updatedInput"]
        argv = shlex.split(updated["command"])
        self.assertEqual(argv[-2:], ["--shell", inner])
        # wnętrze z rtk klasyfikuje się tak samo jak komenda agenta
        self.assertEqual(
            S.classify(argv[-1], self.repo)["class"], S.classify(original, self.repo)["class"]
        )


def make_node_repo(root, depot_exec=True):
    os.makedirs(os.path.join(root, ".git"))
    write(os.path.join(root, "package.json"), '{"name": "shop", "private": true}')
    write(os.path.join(root, "pnpm-workspace.yaml"), "packages:\n  - apps/*\n")
    write(os.path.join(root, "apps/web/package.json"), '{"name": "web"}')
    write(os.path.join(root, "apps/web/src/a.test.ts"), "")
    if depot_exec:
        write(os.path.join(root, "scripts/depot-exec.sh"), FAKE_DEPOT_EXEC, 0o755)


class RtkConfigTest(Paths):
    """Instalacja wpisuje wyjątki rtk sama: bez tego na Macu, gdzie linię `exclude_commands`
    zostawiono z poprzedniej wersji, rtk i scheduler oba przepisują xcodebuild, `expo run` i
    gradlew (dwa hooki z updatedInput, losowy wynik). Pomyłki, które ten test łapie: stara
    linia zostaje, reszta configu rtk znika albo się zmienia, sekcji [hooks] brak, ponowny
    zapis zmienia plik."""

    def config(self, text):
        path = os.path.join(self.dir, "rtk", "config.toml")
        if text is not None:
            write(path, text)
        return path

    def excludes(self, text):
        line = next(l for l in text.splitlines() if l.startswith("exclude_commands = ["))
        return re.findall(r"'([^']*)'", line)

    def test_replaces_the_old_line_and_keeps_the_rest(self):
        before = ("[tracking]\nenabled = false\n\n[hooks]\nexclude_commands = ['go', 'make']\n"
                  "transparent_prefixes = []\n\n[limits]\ngrep_max_results = 200\n")
        path = self.config(before)
        self.assertTrue(S.write_rtk_excludes(path))
        with open(path) as f:
            after = f.read()
        self.assertEqual(self.excludes(after), list(S.RTK_EXCLUDES))
        self.assertEqual(after.count("exclude_commands"), 1)
        for kept in ("[tracking]\nenabled = false", "transparent_prefixes = []", "[limits]\ngrep_max_results = 200"):
            self.assertIn(kept, after)
        with open(path + ".bak-claude-acc") as f:
            self.assertEqual(f.read(), before)  # kopia sprzed pierwszej zmiany
        self.assertFalse(S.write_rtk_excludes(path))  # drugi zapis niczego nie zmienia

    def test_keeps_your_entries_comments_and_other_keys(self):
        """Ręcznie dopisany wzorzec zostaje, komentarz za tablicą też, a następny klucz nie
        znika (wcześniej: linia kończąca się `]` z komentarzem zjadała `transparent_prefixes`);
        nagłówek z komentarzem to ta sama sekcja, nie druga [hooks]."""
        before = ("[hooks] # rtk\nexclude_commands = [\n  'go',\n  'my-tool',  # mine\n]  # list\n"
                  "transparent_prefixes = ['x[0]']\n\n[limits]\ngrep_max_results = 200\n")
        path = self.config(before)
        self.assertTrue(S.write_rtk_excludes(path))
        with open(path) as f:
            after = f.read()
        self.assertEqual(after.count("[hooks]"), 1)
        self.assertEqual(self.excludes(after), list(S.RTK_EXCLUDES) + ["my-tool"])
        self.assertIn("]  # list\ntransparent_prefixes = ['x[0]']\n\n[limits]\ngrep_max_results = 200\n", after)
        self.assertFalse(S.write_rtk_excludes(path))

    def test_refuses_a_value_it_cannot_read_and_writes_relative_paths(self):
        path = self.config("[hooks]\nexclude_commands = ['go'\n")  # tablica bez końca
        with self.assertRaises(ValueError):
            S.write_rtk_excludes(path)
        with open(path) as f:
            self.assertEqual(f.read(), "[hooks]\nexclude_commands = ['go'\n")
        here = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, here)
        self.assertTrue(S.write_rtk_excludes("config.toml"))  # bez katalogu w ścieżce
        with open(os.path.join(self.dir, "config.toml")) as f:
            self.assertEqual(self.excludes(f.read()), list(S.RTK_EXCLUDES))

    def test_adds_the_hooks_section_when_missing(self):
        path = self.config("[limits]\ngrep_max_results = 200\n")
        self.assertTrue(S.write_rtk_excludes(path))
        with open(path) as f:
            after = f.read()
        self.assertIn("[limits]\ngrep_max_results = 200\n", after)
        self.assertIn("\n[hooks]\nexclude_commands = [", after)
        self.assertEqual(self.excludes(after), list(S.RTK_EXCLUDES))


class NodeTest(Paths):
    """Testy, buildy i typecheck JS idą przez tę samą kolejkę co Go, w każdym projekcie z
    package.json. Pomyłki, które ten test łapie: dev serwer, tryb watch albo instalacja w kolejce
    (agent czekałby na coś, co się nie kończy), job JS na Depot, wyjątek rtk, który nie pokrywa
    owiniętej komendy (dwa hooki z updatedInput, losowy wynik), i wyjątek, który zabiera rtk
    komendzie spoza kolejki."""

    WRAPPED = {
        "cd apps/web && npx vitest run": "shop:test:apps/web:vitest",
        "cd apps/web && pnpm vitest run src/a.test.ts": "shop:test:apps/web:vitest:filtered",
        "cd apps/web && pnpm exec playwright test --grep login 2>&1 | tail -20": "shop:e2e:apps/web:playwright:filtered",
        "cd apps/web && npm run build": "shop:build:apps/web:build",
        "cd apps/web && next build": "shop:build:apps/web:next",
        "pnpm -C apps/web test": "shop:test:apps/web:test",
        "pnpm --filter web test:unit": "shop:test:filter=web:test:unit",
        "pnpm typecheck": "shop:typecheck:.:typecheck",
        "pnpm -r lint": "shop:lint:.:lint:all",
        "turbo run build test": "shop:build:.:turbo:all",
        "cd apps/web && tsc --noEmit -p tsconfig.json": "shop:typecheck:apps/web:tsc",
        "cd apps/web && ./node_modules/.bin/jest": "shop:test:apps/web:jest",
        "cd apps/web && bun test": "shop:test:apps/web:bun",
        "cd apps/web && CI=1 npm test": "shop:test:apps/web:test",
        "cd apps/web && yarn e2e": "shop:e2e:apps/web:e2e",
    }
    LEFT_ALONE = (
        "pnpm dev",
        "pnpm install",
        "pnpm add -D vitest",
        "cd apps/web && npx vitest --watch",
        "cd apps/web && pnpm test:watch",
        "cd apps/web && npx playwright show-report",
        "npx playwright install chromium",
        "cd apps/web && next dev",
        "tsc --version",
        "cd apps/web && npx tsc -w",
        "echo 'pnpm test'",
        "SCHED_OFF=1 pnpm test",
        "npx prettier --check .",
        "git status",
    )

    def setUp(self):
        super().setUp()
        self.shop = os.path.join(self.dir, "shop")
        make_node_repo(self.shop)

    def test_node_work_is_classified(self):
        for command, cls in self.WRAPPED.items():
            j = S.classify(command, self.shop)
            self.assertIsNotNone(j, command)
            self.assertEqual(j["class"], cls, command)
            self.assertEqual(j["lang"], "node", command)

    def test_dev_watch_and_installs_stay_out(self):
        for command in self.LEFT_ALONE:
            self.assertIsNone(S.classify(command, self.shop), command)
        outside = os.path.join(self.dir, "no-package")
        os.makedirs(outside)
        self.assertIsNone(S.classify("npx vitest run", outside))  # bez package.json

    def test_switch_off_in_config(self):
        with open(S.CONFIG_PATH, "w") as f:
            json.dump({"node": False}, f)
        self.assertIsNone(S.classify("cd apps/web && npx vitest run", self.shop))
        self.assertIsNotNone(S.classify("cd apps/charter-service && go vet ./...", self.repo))

    def test_priors_and_learning(self):
        full = S.classify("cd apps/web && npx vitest run", self.shop)
        one = S.classify("cd apps/web && pnpm vitest run src/a.test.ts", self.shop)
        gb, s = S.prior(full, 4)
        self.assertLess(S.prior(one, 4)[0], gb)  # jeden plik testów lżejszy niż cały pakiet
        self.assertLess(gb, self.cfg["small_gb"])  # pakiet testów JS to mały job: wyprzedza Go
        rows = [{"where": "local", "class": full["class"], "peak_gb": 0.5, "wall_s": 3.0, "p": None}] * 3
        self.assertEqual(S.predict(full, 4, rows)[2], "history:3")

    def test_never_routed_to_depot(self):
        job = S.classify("cd apps/web && npx vitest run", self.shop)
        self.assertIsNone(S.depot_target(job, 30.0, 3000, self.cfg, {}))
        with mock.patch.object(S, "go_list", side_effect=AssertionError("go list dla JS")):
            self.assertFalse(S.uses_pg(job, {}))
            self.assertFalse(S.count1_safe(job, {}))
        self.assertFalse(S.likely_heavy(job))

    def rtk_excluded(self, segment):
        pats = []
        for p in S.RTK_EXCLUDES:
            pats.append(re.compile(p if p.startswith("^") else r"^" + re.escape(p) + r"($|\s)"))
        return any(r.search(segment) for r in pats)

    def test_rtk_exclusions_cover_what_we_wrap_and_nothing_else(self):
        for command in self.WRAPPED:
            words = [S.strip_prefix(w)[1] for w, _ in S.split_segments(command) if w]
            wrapped = [" ".join(w) for w in words if w and S.parse_node(w, self.shop)]
            self.assertTrue(wrapped, command)
            for g in wrapped:
                self.assertTrue(self.rtk_excluded(g), g)
        for command in ("pnpm install", "pnpm add -D vitest", "npx prettier --check .",
                        "git status", "npm ls", "yarn why react", "pnpm list"):
            self.assertFalse(self.rtk_excluded(command), command)

    def test_hook_wraps_node_commands(self):
        event = {"tool_name": "Bash", "cwd": self.shop,
                 "tool_input": {"command": "cd apps/web && pnpm test 2>&1 | tail -5"}}
        with mock.patch.object(S, "with_rtk", side_effect=lambda c: c):
            out = S.hook_rewrite(event)
        argv = shlex.split(out["hookSpecificOutput"]["updatedInput"]["command"])
        self.assertEqual(argv[:4], ["/usr/bin/python3", S.SELF, "run", "--via"])
        self.assertEqual(argv[-1], "cd apps/web && pnpm test 2>&1 | tail -5")


class WorktreeClassTest(Paths):
    def test_worktrees_learn_from_the_same_history(self):
        """Pomyłka, którą ten test łapie: klasa z nazwą katalogu worktree. Każdy nowy worktree
        zaczynał od priora (lint 2 GB) zamiast z historii projektu (portivo 2026-10-08: `pnpm lint`
        z trzech worktree przewidziany na 2,0 GB, zmierzony 3,6-4,4 GB, razem dwa razy więcej,
        niż scheduler zarezerwował). To samo z natywnym buildem: 10 GB z tabeli w każdym drzewie."""
        shop = os.path.join(self.dir, "shop")
        os.makedirs(os.path.join(shop, ".git/worktrees/wt-lint"))
        write(os.path.join(shop, "package.json"), '{"name": "shop"}')
        wt = os.path.join(self.dir, "wt-lint")
        write(os.path.join(wt, ".git"), f"gitdir: {shop}/.git/worktrees/wt-lint\n")
        write(os.path.join(wt, "package.json"), '{"name": "shop"}')
        main = S.classify("pnpm lint", shop)
        other = S.classify("pnpm lint", wt)
        self.assertEqual(main["class"], "shop:lint:.:lint")
        self.assertEqual(other["class"], main["class"])
        self.assertEqual(other["repo"], "wt-lint")  # panel dalej pokazuje, z którego drzewa
        rows = [{"where": "local", "class": main["class"], "peak_gb": 4.0, "wall_s": 30.0}] * 3
        self.assertEqual(S.predict(other, 4, rows)[:2], (4.6, 30.0))
        up_main = S.classify("portivo-mobile up storefront-mobile", shop)
        up_wt = S.classify("portivo-mobile up storefront-mobile", wt)
        self.assertEqual(up_wt["class"], up_main["class"])
        self.assertEqual(up_wt["class"], "shop:native:portivo-mobile:storefront-mobile")


class NativeTest(Paths):
    """Natywne buildy i start symulatora czekają w tej samej kolejce po pamięci. 2026-10-08 build
    iOS w Release (18:12-18:30) i build klienta deweloperskiego (od 18:48, przy rosnącym swapie)
    nie przeszły przez żadną bramkę pamięci, a o 18:57 Mac zamarzł. Pomyłki, które ten test łapie:
    natywna komenda poza kolejką, lekka komenda (wersja, lista, clean) w kolejce, build wpuszczony
    ponad pamięć, bo nic innego nie biegło, start mimo krytycznej presji strażnika, prognoza
    symulatora nauczona zera, i wyjątek rtk, który nie pokrywa owiniętej komendy."""

    WRAPPED = {
        "xcodebuild -scheme App build": "repo:native:xcodebuild:build",
        "xcodebuild -workspace App.xcworkspace -scheme App -configuration Release": "repo:native:xcodebuild:build",
        "xcodebuild clean test -scheme App": "repo:native:xcodebuild:test",
        "rtk proxy xcrun xcodebuild archive -scheme App": "repo:native:xcodebuild:archive",
        "cd apps/charter-service && xcodebuild build 2>&1 | tail -20": "repo:native:xcodebuild:build",
        "npx expo run:ios": "repo:native:expo-run:ios",
        "./node_modules/.bin/expo run:ios --no-bundler --device generic": "repo:native:expo-run:ios",
        "pnpm exec expo run:android": "repo:native:expo-run:android",
        "pnpm expo run:ios --configuration Release": "repo:native:expo-run:ios",
        "npx react-native run-ios": "repo:native:react-native-run:ios",
        "npx expo prebuild --platform ios": "repo:native:expo-prebuild",
        "eas build --platform ios --profile development --local": "repo:native:eas-local",
        "npx eas-cli build --local": "repo:native:eas-local",
        "pod install": "repo:native:pod",
        "arch -arm64 bundle exec pod install --repo-update": "repo:native:pod",
        "npx pod-install": "repo:native:pod",
        "./gradlew :app:assembleRelease": "repo:native:gradle",
        "xcrun simctl boot 1234-ABCD": "mac:native:simulator",
        "xcrun simctl bootstatus 1234-ABCD -b": "mac:native:simulator",
        "open -a Simulator": "mac:native:simulator",
        "portivo-mobile up storefront-mobile": "repo:native:portivo-mobile:storefront-mobile",
    }
    LEFT_ALONE = (
        "xcodebuild -version",
        "xcodebuild -list -workspace App.xcworkspace",
        "xcodebuild -scheme App -showBuildSettings",
        "xcodebuild clean",
        "xcodebuild -help",
        "xcrun simctl list devices booted",
        "xcrun simctl shutdown all",
        "xcrun simctl io booted screenshot /tmp/s.png",
        "xcrun simctl bootstatus 1234-ABCD",
        "pod --version",
        "pod repo update",
        "eas build --platform ios",  # w chmurze
        "eas build:list --limit 3",
        "./gradlew clean",
        "./gradlew tasks",
        "npx expo start",
        "./node_modules/.bin/expo start --port 8199",
        "npx expo run:ios --help",
        "npx expo export --platform ios",
        "portivo-mobile status",
        "portivo-mobile release",
        "open -a Safari",
        "kubectl get pod",
        "SCHED_OFF=1 xcodebuild -scheme App build",
    )

    def test_native_work_is_classified(self):
        for command, cls in self.WRAPPED.items():
            j = S.classify(command, self.repo)
            self.assertIsNotNone(j, command)
            self.assertEqual((j["class"], j["lang"]), (cls, "native"), command)

    def test_light_and_unrelated_commands_stay_out(self):
        for command in self.LEFT_ALONE:
            self.assertIsNone(S.classify(command, self.repo), command)

    def test_switch_off_in_config(self):
        with open(S.CONFIG_PATH, "w") as f:
            json.dump({"native": False}, f)
        self.assertIsNone(S.classify("xcodebuild -scheme App build", self.repo))
        self.assertIsNotNone(S.classify("cd apps/charter-service && go vet ./...", self.repo))

    def test_priors_and_what_history_may_teach(self):
        build = S.classify("xcodebuild -scheme App build", self.repo)
        boot = S.classify("xcrun simctl boot X", self.repo)
        self.assertEqual(S.prior(build, 4), (10.0, 600))
        self.assertEqual(S.prior(boot, 4), (2.5, 30))
        self.assertFalse(S.depot_target(build, 30.0, 3000, self.cfg, {}))
        tiny = {"where": "local", "peak_gb": 0.05, "wall_s": 4.0, "p": None}
        # build mierzy się w swoim drzewie procesów: historia uczy w obie strony
        rows = [dict(tiny, **{"class": build["class"], "peak_gb": 7.0})] * 3
        self.assertEqual(S.predict(build, 4, rows)[0], round(7.0 * 1.15, 2))
        # symulator żyje pod launchd_sim: szczyt `simctl boot` to tylko xcrun, więc historia
        # nie zbija prognozy poniżej tabeli (inaczej po trzech startach symulator „nic nie waży”)
        rows = [dict(tiny, **{"class": boot["class"]})] * 5
        gb, wall, src = S.predict(boot, 4, rows)
        self.assertEqual((gb, wall, src), (2.5, 4.0, "history:5"))

    def entry(self, jid, gb, lang="native", small=False, ago=0):
        return {"id": jid, "label": jid, "lang": lang, "mem_predicted_gb": gb, "small": small,
                "enqueued_at": time.time() - ago, "route": {"choice": "local"}}

    def test_admitted_only_when_it_fits(self):
        self.set_memory(30)  # 14,4 GB dostępne: wolne do wpuszczenia 14,4 - 4 - 4 = 6,4
        st = self.state()
        st["queue"] = [self.entry("xcb", 10.0, ago=600)]
        # sam na Macu: dostępne minus zapas to 10,4, więc się mieści
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"xcb": ("fits", None)})
        self.set_memory(25)  # 12 GB: zapas zostaje nietknięty
        st = self.state()
        st["queue"] = [self.entry("xcb", 10.0, ago=600)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        # ten sam rozmiar joba Go startuje po 30 s ponad pamięć (nikt inny jej nie zwolni,
        # pilnuje go SIGSTOP); natywny nie, bo symulator i demon Gradle są poza zasięgiem SIGSTOP
        st["queue"] = [self.entry("vet", 10.0, lang="go", ago=600)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vet": ("fits", None)})
        self.set_memory(60)  # 28,8 GB: wolne 20,8
        st = self.state()
        st["queue"] = [self.entry("xcb", 10.0), self.entry("boot", 2.5, small=True)]
        self.assertEqual(
            S.plan(st, self.cfg, time.time()), {"xcb": ("fits", None), "boot": ("fits", None)}
        )

    def test_small_native_job_does_not_use_memory_reserved_for_growth(self):
        st = self.state()  # 28,8 GB dostępne
        st["running"].append(
            {"id": "xcb", "where": "local", "label": "xcodebuild", "mem_predicted_gb": 10.0,
             "mem_now_gb": 0.5, "predicted_wall_s": 600, "started_at": time.time()}
        )
        st["running"].append(
            {"id": "big", "where": "local", "label": "go test ./...", "mem_predicted_gb": 12.0,
             "mem_now_gb": 1.0, "predicted_wall_s": 1500, "started_at": time.time()}
        )
        S.refresh_memory(st, self.cfg)  # wolne 20,8 - 9,5 - 11 = 0,3
        st["queue"] = [self.entry("boot", 2.5, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        st["queue"] = [self.entry("vitest", 2.5, lang="node", small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vitest": ("fits", None)})

    def test_guard_critical_pressure_holds_native_jobs(self):
        """Poziom jądra potrafi mówić „normal” przy pełnym i rosnącym swapie; strażnik liczy
        presję także ze swapu. Natywny job czeka na jego „critical”, Go i JS jak dotąd."""
        snap = {"snapshot": {"at": time.time(), "budget": 12 * S.GB, "total": 0,
                             "pressure": {"level": 2}}}
        with open(S.DEVGUARD_STATE, "w") as f:
            json.dump(snap, f)
        st = self.state()
        self.assertEqual(st["memory"]["guard_level"], 2)
        st["queue"] = [self.entry("xcb", 10.0), self.entry("boot", 2.5, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        S.update_queue_view(st, self.cfg)
        self.assertEqual(st["queue"][0]["reason"]["code"], "pressure")
        st["queue"] = [self.entry("vet", 10.0, lang="go")]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vet": ("fits", None)})
        snap["snapshot"]["at"] = time.time() - 300  # strażnik stoi: jego zdanie się nie liczy
        with open(S.DEVGUARD_STATE, "w") as f:
            json.dump(snap, f)
        st = self.state()
        self.assertIsNone(st["memory"]["guard_level"])
        st["queue"] = [self.entry("xcb", 10.0)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"xcb": ("fits", None)})

    def rtk_excluded(self, segment):
        pats = [re.compile(p if p.startswith("^") else r"^" + re.escape(p) + r"($|\s)") for p in S.RTK_EXCLUDES]
        return any(r.search(segment) for r in pats)

    def test_rtk_exclusions_cover_what_we_wrap(self):
        for command in self.WRAPPED:
            bares = [S.strip_prefix(w)[1] for w, _ in S.split_segments(command) if w]
            wrapped = [" ".join(b) for b in bares if b and S.parse_native(b, self.repo)]
            self.assertTrue(wrapped, command)
            for segment in wrapped:
                self.assertTrue(self.rtk_excluded(segment), segment)
        for command in ("./gradlew clean", "pod --version", "xcrun simctl list", "npx expo start",
                        "eas build:list", "open -a Safari"):
            self.assertFalse(self.rtk_excluded(command), command)

    def test_hook_wraps_native_commands(self):
        event = {"tool_name": "Bash", "cwd": self.repo,
                 "tool_input": {"command": "cd ios && xcodebuild -scheme App build 2>&1 | tail -5",
                                "run_in_background": True}}
        with mock.patch.object(S, "with_rtk", side_effect=lambda c: c):
            hook = S.hook_rewrite(event)["hookSpecificOutput"]
        out = hook["updatedInput"]
        argv = shlex.split(out["command"])
        self.assertEqual(argv[2:5], ["run", "--via", "hook"])
        self.assertEqual(argv[-1], "cd ios && xcodebuild -scheme App build 2>&1 | tail -5")
        self.assertTrue(out["run_in_background"])
        # build w kolejce czeka minutami: agent z komendą na pierwszym planie dostałby timeout
        # Basha (2 min) w trakcie czekania, a o -p i Depot nie ma tu mowy
        self.assertIn("run_in_background", hook["additionalContext"])
        self.assertIn("10 GB", hook["additionalContext"])
        self.assertNotIn("Depot", hook["additionalContext"])


class NativeSlotTest(Paths):
    """Jeden natywny build naraz na tym Macu, także gdy obok biegnie build spoza schedulera, i
    symulatory w limicie strażnika. Pomyłki, które ten test łapie: dwa buildy wpuszczone, bo
    każdy z osobna mieści się w pamięci (2026-10-08 18:48: build iOS obok buildu, Mac zamarł);
    natywny job czekający na swoją kolej, który blokuje joby Go i JS za sobą; build spoza
    schedulera (odczepiony builder portivo-mobile, Xcode) niewidoczny w rezerwie; build, który po
    skompilowaniu (expo run:ios zostaje z Metro) trzyma miejsce i rezerwację w nieskończoność;
    `portivo-mobile up`, który włącza trzeci symulator, gdy dwa są w użyciu."""

    def entry(self, jid, gb, ago=0, small=False, **extra):
        return dict({"id": jid, "label": jid, "mem_predicted_gb": gb, "predicted_wall_s": 600,
                     "small": small, "enqueued_at": time.time() - ago, "route": {"choice": "local"}},
                    **extra)

    def native(self, jid, gb=10.0, ago=0, **extra):
        return self.entry(jid, gb, ago, **dict({"lang": "native", "exclusive": True, "native_tool": "xcodebuild"}, **extra))

    def running_native(self, jid="a", now_gb=3.0, **extra):
        return dict({"id": jid, "where": "local", "label": jid, "lang": "native", "mem_predicted_gb": 10.0,
                     "mem_now_gb": now_gb, "predicted_wall_s": 600, "started_at": time.time(),
                     "exclusive": True}, **extra)

    def test_ios_builds_hold_the_slot_but_the_rest_runs_beside(self):
        """Miejsce na build trzymają tylko buildy, których kompilatory native_scan widzi (Xcode):
        inaczej Android albo `test-without-building` trzymałby je do upływu czasu, nic nie budując."""
        for command, exclusive in (
            ("portivo-mobile up storefront-mobile", True),
            ("npx expo run:ios", True),
            ("xcodebuild -scheme App build", True),
            ("eas build --platform ios --local", True),
            ("xcodebuild test-without-building -xctestrun x.xctestrun", False),
            ("npx expo run:android", False),
            ("./gradlew :app:assembleRelease", False),
            ("eas build --platform android --local", False),
            ("pod install", False),
            ("xcrun simctl boot 1234-ABCD", False),
        ):
            self.assertEqual(self.job(command)["exclusive"], exclusive, command)

    def test_one_native_build_at_a_time(self):
        self.set_memory(90)  # 43,2 GB dostępne: oba buildy z osobna się mieszczą
        st = self.state()
        st["running"].append(self.running_native())
        S.refresh_memory(st, self.cfg)
        st["queue"] = [self.native("b", ago=100), self.entry("vet", 6.0, ago=50)]
        # b czeka na swoją kolej, ale nie blokuje vet za sobą
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"vet": ("fits", None)})
        S.update_queue_view(st, self.cfg)
        self.assertEqual(st["queue"][0]["reason"]["code"], "native")
        st["running"][0]["native_done"] = True  # skompilował; zostało Metro
        S.refresh_memory(st, self.cfg)
        self.assertIn("b", S.plan(st, self.cfg, time.time()))
        st["running"] = []
        st["queue"] = [self.native("b", ago=10), self.native("c", ago=5)]
        S.refresh_memory(st, self.cfg)
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"b": ("fits", None)})

    def test_build_outside_the_scheduler_holds_the_slot_and_its_growth(self):
        self.set_memory(80)
        free = self.state()["memory"]["free_for_admission_gb"]
        self.set_memory(80, native={"gb": 2.0, "active": True})
        st = self.state()
        self.assertTrue(st["memory"]["native"]["outside"])
        # urośnie do przewidywanego szczytu buildu (10 GB z tabeli), teraz 2
        self.assertAlmostEqual(st["memory"]["free_for_admission_gb"], free - 8.0, places=1)
        st["queue"] = [self.native("b", ago=60), self.entry("tiny", 0.5, small=True)]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {"tiny": ("fits", None)})

    def test_only_compiling_xcode_processes_are_a_native_build(self):
        """maestro trzyma na symulatorze `xcodebuild test-without-building` przez całą sesję, a
        otwarty Xcode bezczynną usługę buildów: żadne z nich nie zajmuje miejsca na build ani nie
        dostaje rezerwy (2026-10-08 19:30: maestro i symulator, zero kompilacji)."""
        procs = {
            10: ("xcodebuild", ["xcodebuild", "test-without-building", "-xctestrun", "/tmp/x.xctestrun"]),
            11: ("SWBBuildService", None),  # dziecko powyższego
            12: ("swift-frontend", None),
            20: ("SWBBuildService", None),  # Xcode otwarty, nic nie buduje
            21: ("SWBBuildServiceHelper", None),  # pomocnik bezczynnej usługi
            30: ("launchd_sim", None),
        }
        parents = {11: 10, 12: 11, 20: 1, 21: 20, 30: 1}

        def scan(extra=None):
            table = {**procs, **(extra or {})}
            kids = {}
            for pid, ppid in parents.items():
                kids.setdefault(ppid, []).append(pid)
            for pid in extra or {}:
                kids.setdefault(1, []).append(pid)
            with mock.patch.dict(os.environ, {"SCHED_FAKE_MEMORY": ""}), \
                    mock.patch.object(S, "all_pids", return_value=list(table)), \
                    mock.patch.object(S, "proc_name", side_effect=lambda p: table[p][0]), \
                    mock.patch.object(S, "proc_args", side_effect=lambda p: table[p][1]), \
                    mock.patch.object(S, "proc_bsd", side_effect=lambda p, off, size=4: parents.get(p, 1)), \
                    mock.patch.object(S, "_listpids", side_effect=lambda kind, p: kids.get(p, [])), \
                    mock.patch.object(S, "pids_usage", side_effect=lambda pids: (len(pids) * 1.0, 0.0)):
                return S.native_scan()

        idle = scan()
        self.assertFalse(idle["active"])
        self.assertEqual(idle["pids"], set())
        building = scan({40: ("xcodebuild", ["xcodebuild", "-workspace", "App.xcworkspace", "-scheme", "App", "build"])})
        self.assertTrue(building["active"])
        self.assertEqual(building["pids"], {40})
        # Xcode buduje: kompilator pod usługą
        procs[22] = ("swift-frontend", None)
        parents[22] = 20
        xcode = scan()
        self.assertTrue(xcode["active"])
        self.assertEqual(xcode["pids"], {20, 21, 22})
        del procs[22], parents[22]

    def test_build_phase_holds_the_slot_through_a_slow_cold_start(self):
        """Zimny build (prebuild, pod install) długo nie rusza kompilatorów. Gdy historia `up` to
        szybkie biegi z klientem w cache (60 s), miejsce nie może wrócić po 10 minutach, bo drugi
        build wystartowałby przed kompilacją pierwszego: dolna granica to czas z tabeli NATIVE."""
        now = time.time()
        me = self.running_native("a", predicted_wall_s=60, native_tool="portivo-mobile", started_at=now)
        S.track_native(me, {"active": False}, now + 1000, {}, 1.0)
        self.assertFalse(me.get("native_done"))
        S.track_native(me, {"active": False}, now + 2 * S.NATIVE["portivo-mobile"][2] + 1, {}, 1.0)
        self.assertTrue(me["native_done"])

    def test_simulator_starts_wait_while_simulators_in_use_are_at_the_cap(self):
        def snapshot(in_use):
            sims = [{"udid": f"U{i}", "name": f"Portivo-{i}", "pool": True, "in_use": i < in_use,
                     "footprint": 2 * S.GB} for i in range(2)]
            # Twój symulator: liczy się do pamięci, ale nie do limitu agentów (strażnik go nie wyłączy)
            sims.append({"udid": "H", "name": "iPhone 17", "pool": False, "in_use": True, "footprint": 3 * S.GB})
            # chroniony symulator innej sesji (pomiary wydajności): z tego samego powodu poza limitem
            sims.append({"udid": "P", "name": "Portivo-Perf-iPhone", "pool": True, "protected": True,
                         "in_use": True, "footprint": 3 * S.GB})
            with open(S.DEVGUARD_STATE, "w") as f:
                json.dump({"snapshot": {"at": time.time(), "budget": 12 * S.GB, "total": 0,
                                        "simulators": sims, "simulator_cap": 2}}, f)

        self.set_memory(90)
        snapshot(2)
        st = self.state()
        self.assertEqual(st["memory"]["simulators"]["agents_in_use"], 2)
        st["queue"] = [self.native("up", native_tool="portivo-mobile", sim_lease=False, ago=5),
                       self.entry("boot", 2.5, small=True, lang="native", native_tool="simulator")]
        self.assertEqual(S.plan(st, self.cfg, time.time()), {})
        S.update_queue_view(st, self.cfg)
        self.assertEqual([j["reason"]["code"] for j in st["queue"]], ["simulators", "simulators"])
        st["queue"][0]["sim_lease"] = True  # sesja ma już swój symulator: nic nowego nie wstanie
        self.assertIn("up", S.plan(st, self.cfg, time.time()))
        snapshot(1)
        st = self.state()
        st["queue"] = [self.native("up", native_tool="portivo-mobile", sim_lease=False, ago=5)]
        self.assertIn("up", S.plan(st, self.cfg, time.time()))

    def test_build_phase_end_frees_slot_and_reservation(self):
        st = self.state()
        st["running"].append(dict(self.running_native("a", now_gb=0.4, mem_predicted_gb=1.0), pid=os.getpid()))
        S.save_state(st)
        started = time.time()
        # xcodebuild wstaje: prognoza rośnie do szczytu buildu, choć klasa znała tylko lekkie biegi
        S.heartbeat("a", self.cfg, 3.0, 3.0, 10.0, started, native={"active": True, "gb": 2.6})
        me = S.load_state(self.cfg)["running"][0]
        self.assertEqual(me["mem_predicted_gb"], 10.0)
        self.assertTrue(me["native_seen"])
        self.assertFalse(me.get("native_done"))
        with mock.patch.object(S.time, "time", return_value=time.time() + 31):
            S.heartbeat("a", self.cfg, 1.5, 9.0, 300.0, started, native={"active": False, "gb": 0.0})
        st = S.load_state(self.cfg)
        me = st["running"][0]
        self.assertTrue(me["native_done"])
        self.assertEqual(me["mem_predicted_gb"], 1.5)  # dalej tylko to, co zajmuje (Metro)
        S.refresh_memory(st, self.cfg)
        self.assertEqual(st["memory"]["reserved_gb"], 0.0)


class StateTest(Paths):
    def test_kernel_reads_load_on_first_use(self):
        footprint, cpu = S.proc_usage(os.getpid())
        self.assertGreater(footprint, 1024**2)
        self.assertGreater(cpu, 0)
        self.assertGreater(S.job_usage(os.getpid())[0], 0)
        self.assertGreater(S.sysctl_int("hw.memsize"), 1024**3)

    def test_state_file_format_is_unchanged(self):
        st = self.state()
        st["queue"].append({"id": "q", "label": "go test ./internal/zażółć"})
        S.save_state(st)
        with open(S.STATE_PATH) as f:
            self.assertEqual(f.read(), json.dumps(st, ensure_ascii=False, indent=1))

    def test_public_state_and_memory_gauge(self):
        st = self.state()
        S.save_state(st)
        with open(S.STATE_PATH) as f:
            saved = json.load(f)
        self.assertEqual(saved["version"], 1)
        self.assertIsNotNone(saved["idle_since"])
        mem = saved["memory"]
        total = sum(
            mem[k]
            for k in (
                "others_gb",
                "jobs_now_gb",
                "reserved_gb",
                "devserver_reserve_gb",
                "free_for_admission_gb",
                "headroom_gb",
            )
        )
        self.assertAlmostEqual(total, saved["host"]["ram_gb"], delta=0.05)
        self.assertEqual(mem["headroom_gb"], 4.0)
        self.assertNotIn("_internal", S.public_state(saved))
        self.assertEqual(saved["config"]["lambda_s_per_unit"], 6.0)

    def test_idle_max_learns_only_from_idle_readings(self):
        self.set_memory(80)  # 38,4 GB, ale biegnie job: nie uczy
        st = S.load_state(self.cfg)  # bez odczytu przed dodaniem joba
        st["running"].append(
            {"id": "r", "where": "local", "mem_now_gb": 20.0, "mem_predicted_gb": 20.0}
        )
        S.refresh_memory(st, self.cfg)
        self.assertEqual(
            st["memory"]["idle_max_gb"], round(0.65 * 48 - 4, 1)
        )  # podłoga
        st["running"] = []
        self.set_memory(70)  # 33,6 GB bez jobów: uczy
        S.refresh_memory(st, self.cfg)
        self.assertEqual(st["memory"]["idle_max_gb"], round(0.70 * 48 - 4, 1))

    def test_devserver_reserve_from_devguard(self):
        with open(S.DEVGUARD_STATE, "w") as f:
            json.dump(
                {
                    "snapshot": {
                        "at": time.time(),
                        "budget": 12 * S.GB,
                        "total": 10.5 * S.GB,
                    }
                },
                f,
            )
        self.assertEqual(S.devserver_reserve_gb(), 1.5)
        with open(S.DEVGUARD_CONFIG, "w") as f:
            json.dump({"max_server_gb": 1.0}, f)
        self.assertEqual(S.devserver_reserve_gb(), 1.0)

    def test_devserver_reserve_keeps_room_for_servers_the_guard_never_stops(self):
        """Chroniony stos (`pnpm dev` z korzenia portivo) ponad budżetem dawał rezerwę 0, choć
        rośnie dalej: strażnik go nie zatrzyma. Rezerwa to wtedy jego zmierzony powrót do szczytu
        (lifetime_max_phys_footprint), a nie zgadywany wzrost."""
        # stos: szczyt jednostki to suma teraz, a powrót procesów do ich szczytów liczy strażnik
        units = [
            {"protected": True, "footprint": 6 * S.GB, "peak": 6 * S.GB, "regrow": 2 * S.GB},
            {"protected": True, "footprint": 5 * S.GB, "peak": 5 * S.GB, "regrow": 1 * S.GB},
            {"protected": False, "footprint": 2 * S.GB, "peak": 7 * S.GB, "regrow": 5 * S.GB},  # ten strażnik przytnie
        ]
        with open(S.DEVGUARD_STATE, "w") as f:
            json.dump({"snapshot": {"at": time.time(), "budget": 12 * S.GB, "total": 13 * S.GB,
                                    "units": units}}, f)
        self.assertEqual(S.devserver_reserve_gb(), 3.0)
        # jeden skok nie rezerwuje na zawsze więcej niż jeden serwer
        units[0]["regrow"] = 20 * S.GB
        with open(S.DEVGUARD_STATE, "w") as f:
            json.dump({"snapshot": {"at": time.time(), "budget": 12 * S.GB, "total": 13 * S.GB,
                                    "units": units}}, f)
        self.assertEqual(S.devserver_reserve_gb(), 5.0)  # 4 (max_server_gb) + 1

    def test_today_rolls_over_and_reap(self):
        st = self.state()
        st["today"]["date"] = "2000-01-01"
        st["today"]["jobs_local"] = 9
        sleeper = subprocess.Popen(["sleep", "30"], preexec_fn=os.setpgrp)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        st["running"].append(
            {"id": "dead", "where": "local", "pid": 999999, "child_pgid": sleeper.pid}
        )
        S.save_state(st)
        st = S.load_state(self.cfg)
        self.assertEqual(st["today"]["jobs_local"], 0)
        S.reap(st)
        self.assertEqual(st["running"], [])
        self.assertIsNotNone(sleeper.wait(timeout=5))  # sierota zabita


FAKE_DEPOT_API = r"""#!%s
# podróbka CLI Depot dla `sched.py depot`: odpowiedzi z $FAKE_DEPOT_DATA, wywołania do $FAKE_DEPOT_CALLS
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DEPOT_CALLS"], "a") as f:
    f.write(" ".join(args) + "\n")
if os.environ.get("FAKE_DEPOT_FAIL"):
    sys.stderr.write("unauthenticated: Invalid token\n")
    sys.exit(1)
with open(os.environ["FAKE_DEPOT_DATA"]) as f:
    data = json.load(f)
if args[:3] == ["ci", "run", "list"]:
    out = data["runs"]
elif args[:2] == ["ci", "status"]:
    out = data["status"].get(args[2])
elif args[:3] == ["ci", "metrics", "--run"]:
    out = data["metrics"].get(args[3])
else:
    out = None
if out is None:
    sys.stderr.write("not_found: Run not found\n")
    sys.exit(1)
print(json.dumps(out))
""" % sys.executable


def depot_status(rid, workflow, jobs):
    return {
        "org_id": "org1",
        "run_id": rid,
        "workflows": [
            {
                "name": workflow,
                "jobs": [
                    {
                        "job_display_name": name,
                        "status": status,
                        "attempts": [{"view_url": f"https://depot.dev/orgs/org1/workflows/w?job={rid}-{name}"}],
                    }
                    for name, status in jobs
                ],
            }
        ],
    }


class DepotTest(Paths):
    """`sched.py depot`: biegi Depot CI spoza schedulera (bramka pushu, depot-ci.sh agenta)."""

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.dir, "bin")
        write(os.path.join(bindir, "depot"), FAKE_DEPOT_API, 0o755)
        self.calls = os.path.join(self.dir, "depot-calls.log")
        self.data = os.path.join(self.dir, "depot-data.json")
        patcher = mock.patch.dict(
            os.environ,
            {
                "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_DEPOT_CALLS": self.calls,
                "FAKE_DEPOT_DATA": self.data,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        gate = [("prepush", "finished")]
        with open(self.data, "w") as f:
            json.dump(
                {
                    "runs": [
                        {"run_id": "run0live1", "repo": "o/portivo", "status": "running", "created_at": "2026-10-05T22:30:00Z"},
                        {"run_id": "run1sched", "repo": "o/portivo", "status": "running", "created_at": "2026-10-05T22:29:00Z"},
                        {"run_id": "run2fail1", "repo": "o/portivo", "status": "failed", "created_at": "2026-10-05T22:15:56.626Z"},
                        {"run_id": "run3green", "repo": "o/portivo", "status": "finished", "created_at": "2026-10-05T22:00:00Z"},
                        {"run_id": "run4cancl", "repo": "o/portivo", "status": "cancelled", "created_at": "2026-10-05T21:50:00Z"},
                    ],
                    "status": {
                        "run0live1": depot_status("run0live1", "gates", [("prepush", "running")]),
                        "run1sched": depot_status("run1sched", "depot-exec-8", [("exec", "running")]),
                        "run2fail1": depot_status(
                            "run2fail1", "go-heavy", [("affected", "finished"), ("isolation", "failed")]
                        ),
                        "run3green": depot_status("run3green", "gates", gate),
                        "run4cancl": depot_status("run4cancl", "gates", gate),
                    },
                    "metrics": {
                        "run2fail1": {"run": {"started_at": "2026-10-05T22:15:56.919Z", "finished_at": "2026-10-05T22:19:06.268Z"}},
                        "run3green": {"run": {"started_at": "2026-10-05T22:00:00Z", "finished_at": "2026-10-05T22:05:00Z"}},
                    },
                },
                f,
            )
        # job schedulera, który sam poszedł na Depot: karta pokazuje go jako jego wiersz
        st = S.empty_state(self.cfg)
        st["running"] = [{"id": "j-1", "where": "depot", "depot": {"run_id": "run1sched"}}]
        S.save_state(st)
        self.now = S.iso_epoch("2026-10-05T22:31:00Z")

    def calls_for(self, rid):
        with open(self.calls) as f:
            return [line for line in f if rid in line]

    def test_runs_outside_the_scheduler_show_up(self):
        out = S.sync_depot({}, self.cfg, now=self.now)
        self.assertIsNone(out["error"])
        self.assertEqual([j["id"] for j in out["running"]], ["depot-run0live1"])
        live = out["running"][0]
        self.assertEqual(live["where"], "depot")
        self.assertEqual(live["label"], "gates · prepush")
        self.assertEqual(live["repo"], "portivo")
        self.assertEqual(live["elapsed_s"], 60.0)
        # ETA z mediany zielonych biegów tej samej etykiety (run3green: 5 min)
        self.assertEqual(live["eta_s"], 240.0)
        self.assertEqual(live["progress"], 0.2)
        self.assertEqual(live["depot"]["run_id"], "run0live1")
        self.assertIn("job=run0live1-prepush", live["depot"]["url"])
        recent = {r["id"]: r for r in out["recent"]}
        self.assertEqual(list(recent), ["depot-run2fail1", "depot-run3green", "depot-run4cancl"])
        fail = recent["depot-run2fail1"]
        self.assertEqual(fail["label"], "go-heavy · affected, isolation")
        self.assertEqual(fail["rc"], 1)
        self.assertEqual(fail["wall_s"], 189.3)
        self.assertIn("job=run2fail1-isolation", fail["url"])  # czerwony job pierwszy
        self.assertEqual(recent["depot-run3green"]["rc"], 0)
        cancelled = recent["depot-run4cancl"]
        self.assertEqual(cancelled["rc"], 130)
        self.assertIsNone(cancelled["wall_s"])  # metryk brak: koniec = utworzenie
        self.assertEqual(cancelled["finished_at"], S.iso_epoch("2026-10-05T21:50:00Z"))

    def test_finished_runs_are_asked_once(self):
        first = S.sync_depot({}, self.cfg, now=self.now)
        S.sync_depot(first, self.cfg, now=self.now + 15)
        self.assertEqual(len(self.calls_for("run2fail1")), 2)  # status i metrics, raz
        self.assertEqual(len(self.calls_for("run0live1")), 2)  # biegnący: status przy każdym odczycie

    def test_cli_error_keeps_the_last_runs_and_says_why(self):
        first = S.sync_depot({}, self.cfg, now=self.now)
        with mock.patch.dict(os.environ, {"FAKE_DEPOT_FAIL": "1"}):
            out = S.sync_depot(first, self.cfg, now=self.now + 60)
        self.assertEqual(out["error"], "unauthenticated: Invalid token")
        self.assertEqual(out["running"], [])
        self.assertEqual(out["recent"], first["recent"])

    def test_command_writes_the_file_and_honours_max_age(self):
        self.assertEqual(S.cmd_depot([]), 0)
        with open(S.DEPOT_PATH) as f:
            saved = json.load(f)
        self.assertEqual(saved["running"][0]["id"], "depot-run0live1")
        before = len(open(self.calls).readlines())
        S.cmd_depot(["--max-age", "60"])
        self.assertEqual(len(open(self.calls).readlines()), before)  # świeży plik: bez sieci


class RunTest(unittest.TestCase):
    """Prawdziwe procesy sched.py run w osobnym HOME."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="sched-run-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.sched_dir = os.path.join(self.home, ".local/share/claude-acc/sched")
        self.repo = os.path.join(self.dir, "repo")
        make_repo(self.repo)
        self.bin = os.path.join(self.dir, "bin")
        write(os.path.join(self.bin, "go"), FAKE_GO, 0o755)
        write(os.path.join(self.bin, "portivo-mobile"), FAKE_NATIVE, 0o755)
        self.log = os.path.join(self.dir, "go.log")
        self.memfile = os.path.join(self.dir, "mem.json")
        self.set_memory(60)
        self.env = dict(
            os.environ,
            HOME=self.home,
            PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
            SCHED_FAKE_MEMORY=self.memfile,
            FAKE_GO_LOG=self.log,
            SHELL="/bin/zsh",
        )
        self.env.pop("GOFLAGS", None)
        # testy puszczone przez hook schedulera biegną w jego jobie: sched.py run w środku
        # wykonałby komendę od razu, bez kolejki i historii
        self.env.pop("CLAUDE_ACC_SCHED_JOB", None)
        self.procs = []
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    def set_memory(self, level, swap=1.0, pressure="normal"):
        with open(self.memfile, "w") as f:
            json.dump(
                {"level": level, "ram_gb": 48, "swap_gb": swap, "pressure": pressure,
                 "native": {"gb": 0.0, "active": False}},
                f,
            )

    def start(self, command, sleep="0.3", rc="0", via="cli", extra=(), **env):
        e = dict(self.env, FAKE_GO_SLEEP=sleep, FAKE_GO_RC=rc, **env)
        p = subprocess.Popen(
            [
                "/usr/bin/python3",
                SCRIPT,
                "run",
                "--via",
                via,
                *extra,
                "--shell",
                command,
            ],
            cwd=self.repo,
            env=e,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.procs.append(p)
        return p

    def done(self, p, timeout=30):
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err

    def go_log(self):
        rows = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                for line in f:
                    kind, pid, ts, *rest = line.split(" ", 3)
                    rows.append(
                        (kind, int(pid), float(ts), rest[0].strip() if rest else "")
                    )
        return rows

    def history(self):
        path = os.path.join(self.sched_dir, "history.jsonl")
        return [json.loads(l) for l in open(path)] if os.path.exists(path) else []

    def state(self):
        with open(os.path.join(self.sched_dir, "state.json")) as f:
            return json.load(f)

    def wait_for(self, predicate, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if predicate():
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail("warunek nie spełniony")

    def test_exit_code_output_and_history(self):
        rc, out, err = self.done(
            self.start(
                "cd apps/charter-service && go test ./internal/moneyfmt/", rc="3"
            )
        )
        self.assertEqual(rc, 3)
        self.assertIn("fake go done: test ./internal/moneyfmt/", out)
        row = self.history()[-1]
        self.assertEqual(
            (row["where"], row["class"], row["rc"], row["choice"]),
            ("local", "charter-service:test:pkg:internal/moneyfmt", 3, "local"),
        )
        st = self.state()
        self.assertEqual(st["running"], [])
        self.assertEqual(st["today"]["jobs_local"], 1)
        self.assertIsNotNone(st["idle_since"])

    def test_not_go_runs_plain(self):
        rc, out, _ = self.done(self.start("echo hello && exit 4"))
        self.assertEqual((rc, out.strip()), (4, "hello"))
        self.assertFalse(os.path.exists(os.path.join(self.sched_dir, "state.json")))

    def test_two_small_jobs_run_together(self):
        a = self.start(
            "cd apps/charter-service && go test ./internal/moneyfmt/", sleep="1.0"
        )
        b = self.start(
            "cd apps/charter-service && go test ./internal/money/", sleep="1.0"
        )
        self.assertEqual(self.done(a)[0], 0)
        self.assertEqual(self.done(b)[0], 0)
        starts = [r[2] for r in self.go_log() if r[0] == "start"]
        ends = [r[2] for r in self.go_log() if r[0] == "end"]
        self.assertLess(max(starts), min(ends))  # nakładały się

    def test_heavy_waits_small_overtakes(self):
        first = self.start("cd apps/charter-service && go vet ./...", sleep="2.5")
        self.wait_for(lambda: len(self.state()["running"]) == 1)
        second = self.start("cd apps/charter-service && go vet ./...", sleep="0.3")
        self.wait_for(lambda: len(self.state()["queue"]) == 1)
        queued = self.state()["queue"][0]
        self.assertEqual(queued["reason"]["code"], "memory")
        self.assertIn("waiting for 8.3 GB", queued["reason"]["text"])
        self.assertEqual(queued["route"]["why"], "waits")
        small = self.start(
            "cd apps/charter-service && go test ./internal/moneyfmt/", sleep="0.2"
        )
        rc_small, _, err_small = self.done(small)
        self.assertEqual(rc_small, 0)
        for p in (first, second):
            self.assertEqual(self.done(p)[0], 0)
        log = self.go_log()
        first_end = [r for r in log if r[0] == "end"][0][2]
        starts = sorted(r[2] for r in log if r[0] == "start")
        second_start = [r[2] for r in log if r[0] == "start" and "vet" in r[3]][-1]
        small_start = [r[2] for r in log if r[0] == "start" and "moneyfmt" in r[3]][0]
        self.assertGreaterEqual(second_start, first_end - 0.05)  # drugi vet czekał
        self.assertLess(small_start, first_end)  # mały wyprzedził
        st = self.state()
        self.assertEqual(st["today"]["overtakes"], 1)
        self.assertEqual(st["overtakes"][-1]["label"], "go test ./internal/moneyfmt/")
        self.assertGreater(st["today"]["old_lock_wait_s"], 0)
        self.assertEqual(len(starts), 3)

    def test_p_and_ldflags_injection(self):
        self.assertEqual(
            self.done(
                self.start(
                    "cd apps/charter-service && go test -count=1 -run '^$' ./..."
                )
            )[0],
            0,
        )
        self.assertEqual(
            self.done(self.start("cd apps/charter-service && go build ./..."))[0], 0
        )
        self.assertEqual(
            self.done(
                self.start("cd apps/charter-service && GOFLAGS=-p=2 go vet ./...")
            )[0],
            0,
        )
        log = [r[3] for r in self.go_log() if r[0] == "start"]
        self.assertIn(
            "GOFLAGS=-p=8 ARGS=test", log[0]
        )  # 11,6 GB mieści się w 20,8 i jest najszybsze
        self.assertIn("-ldflags=-w", log[1])
        self.assertIn("GOFLAGS=-p=2 ARGS=vet", log[2])  # agent wybrał sam

    def test_cannot_fit_goes_to_depot_ci(self):
        rc, out, err = self.done(
            self.start(
                "cd apps/charter-service && go test -race ./...", FAKE_DEPOT_RC="0"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn("depot-ci output go-heavy.yml full", out)
        self.assertIn("needs 34 GB", err)
        row = self.history()[-1]
        self.assertEqual(
            (row["where"], row["why"], row["depot_run_id"]),
            ("depot", "cannot_fit", "abcd1234efgh"),
        )
        self.assertGreater(row["units"], 0)
        self.assertEqual(self.go_log(), [])

    def test_depot_exec_125_falls_back_to_local(self):
        os.remove(os.path.join(self.repo, "scripts/depot-ci.sh"))
        with open(
            os.path.join(
                self.sched_dir if os.path.isdir(self.sched_dir) else self.mk_sched(),
                "config.json",
            ),
            "w",
        ) as f:
            json.dump({"idle_floor_pct": 10}, f)
        self.set_memory(30)  # pusty Mac zmieści 10,4 GB, vet potrzebuje 13,7
        rc, out, err = self.done(
            self.start("cd apps/charter-service && go vet ./...", FAKE_EXEC_RC="125")
        )
        self.assertEqual(rc, 0)
        self.assertIn("uruchamiam lokalnie", err)
        self.assertIn("fake go done: vet ./...", out)
        rows = self.history()
        self.assertEqual([r["where"] for r in rows], ["depot", "local"])
        self.assertEqual(rows[0]["rc"], 125)

    def mk_sched(self):
        os.makedirs(self.sched_dir, exist_ok=True)
        return self.sched_dir

    def test_timeout_exit_75(self):
        first = self.start("cd apps/charter-service && go vet ./...", sleep="4")
        self.wait_for(lambda: len(self.state()["running"]) == 1)
        late = self.start(
            "cd apps/charter-service && go vet ./...", extra=("--timeout", "1")
        )
        rc, _, err = self.done(late)
        self.assertEqual(rc, 75)
        self.assertIn("timeout", err)
        self.assertEqual(self.state()["queue"], [])
        self.done(first)

    def cancel(self, *args):
        done = subprocess.run(
            ["/usr/bin/python3", SCRIPT, "cancel", *args, "--json"],
            cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=30,
        )
        return done.returncode, json.loads(done.stdout) if done.stdout.strip() else None

    def test_cancel_takes_a_queued_job_off_the_queue(self):
        first = self.start("cd apps/charter-service && go vet ./...", sleep="4")
        self.wait_for(lambda: len(self.state()["running"]) == 1)
        late = self.start("cd apps/charter-service && go vet ./...")
        self.wait_for(lambda: len(self.state()["queue"]) == 1)
        jid = self.state()["queue"][0]["id"]
        rc, out = self.cancel(jid)
        self.assertEqual(rc, 0)
        self.assertEqual([(j["id"], j["state"], j["result"]) for j in out["jobs"]], [(jid, "queued", "dequeued")])
        rc_late, _, err = self.done(late)
        self.assertEqual(rc_late, 130)
        self.assertIn("anulowane", err)
        st = self.state()
        self.assertEqual(st["queue"], [])
        self.assertEqual(len(st["running"]), 1)  # biegnący job nie dostał nic
        self.assertEqual(self.done(first)[0], 0)
        self.assertEqual(len([r for r in self.go_log() if r[0] == "start"]), 1)

    def test_cancel_by_pane_signals_the_running_job(self):
        p = self.start("cd apps/charter-service && go test ./internal/moneyfmt/", sleep="30",
                       ORCA_PANE_KEY="tab-1:pane-a")
        other = self.start("cd apps/charter-service && go test ./internal/money/", sleep="2",
                           ORCA_PANE_KEY="tab-2:pane-b")
        self.wait_for(lambda: len([r for r in self.go_log() if r[0] == "start"]) == 2)
        rc, out = self.cancel("tab-1:pane-a")
        self.assertEqual(rc, 0)
        self.assertEqual([(j["state"], j["result"]) for j in out["jobs"]], [("running", "ended")])
        rc_p, _, _ = self.done(p, timeout=10)
        self.assertNotEqual(rc_p, 0)
        self.assertEqual(self.done(other)[0], 0)  # cudzy panel biegnie dalej
        rows = {r["label"]: r for r in self.history()}
        self.assertTrue(rows["go test ./internal/moneyfmt/"]["cancelled"])
        self.assertFalse(rows["go test ./internal/money/"]["cancelled"])

    def test_cancel_with_nothing_matching(self):
        rc, out = self.cancel("j-1-abcd")
        self.assertEqual((rc, out["jobs"]), (1, []))

    def test_sigterm_reaches_the_command(self):
        p = self.start(
            "cd apps/charter-service && go test ./internal/moneyfmt/", sleep="30"
        )
        self.wait_for(lambda: any(r[0] == "start" for r in self.go_log()))
        child = [r[1] for r in self.go_log() if r[0] == "start"][0]
        p.send_signal(signal.SIGTERM)
        rc, _, _ = self.done(p, timeout=10)
        self.assertNotEqual(rc, 0)
        self.wait_for(
            lambda: (
                not os.path.exists(f"/proc/{child}")
                and subprocess.run(
                    ["kill", "-0", str(child)], capture_output=True
                ).returncode
                != 0
            )
        )
        self.assertEqual(self.state()["running"], [])

    def test_count1_dropped_for_pure_package_via_hook(self):
        rc, _, err = self.done(
            self.start(
                "cd apps/charter-service && go test -count=1 ./internal/moneyfmt/",
                via="hook",
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn("bez -count=1", err)
        args = [r[3] for r in self.go_log() if r[0] == "start"][0]
        self.assertNotIn("-count=1", args)
        self.assertTrue(self.history()[-1]["count1_dropped"])
        write(
            os.path.join(
                self.repo, "apps/charter-service/internal/money/money_test.go"
            ),
            'package money\n\nimport "os/exec"\n\nvar _ = exec.Command("git", "ls-files")\n',
        )
        rc, _, _ = self.done(
            self.start(
                "cd apps/charter-service && go test -count=1 ./internal/money/",
                via="hook",
            )
        )
        args = [r[3] for r in self.go_log() if r[0] == "start"][-1]
        self.assertIn("-count=1", args)

    def test_native_build_waits_for_room_then_runs(self):
        """Natywny build bez miejsca w pamięci nie startuje, czeka w kolejce z powodem, który
        widzi agent i panel, a gdy pamięć się zwolni, rusza sam: z wyjściem i kodem dla agenta i
        wierszem w historii, z której scheduler uczy się jego szczytu."""
        write(os.path.join(self.bin, "xcodebuild"), FAKE_GO, 0o755)
        self.set_memory(20)  # 9,6 GB dostępne: nawet sam na Macu 10 GB buildu się nie mieści
        p = self.start("xcodebuild -scheme App build", via="hook")
        self.wait_for(lambda: len(self.state()["queue"]) == 1)
        queued = self.state()["queue"][0]
        self.assertEqual((queued["lang"], queued["reason"]["code"]), ("native", "memory"))
        self.assertIn("waiting for 10.0 GB", queued["reason"]["text"])
        time.sleep(1.5)  # kilka obiegów kolejki (co 0,5 s): dalej czeka
        self.assertEqual(self.go_log(), [])
        self.set_memory(60)
        rc, out, err = self.done(p)
        self.assertEqual(rc, 0)
        self.assertIn("fake go done: -scheme App build", out)
        self.assertIn("natywny build albo symulator", err)
        row = self.history()[-1]
        self.assertEqual((row["class"], row["where"], row["rc"]), ("repo:native:xcodebuild:build", "local", 0))
        self.assertGreater(row["wait_s"], 1.0)

    def test_native_up_streams_output_and_returns_its_code(self):
        """`portivo-mobile up` agenci puszczają w tle: wyjście i kod mają być jego własne."""
        rc, out, err = self.done(self.start("portivo-mobile up storefront-mobile", rc="3", via="hook"))
        self.assertEqual(rc, 3)
        self.assertIn('"device": "Portivo-1"', out)
        self.assertIn("building the dev client", err)
        row = self.history()[-1]
        self.assertEqual((row["class"], row["rc"], row["where"]),
                         ("repo:native:portivo-mobile:storefront-mobile", 3, "local"))
        self.assertFalse(row["native_built"])  # nic się nie kompilowało: klient z cache

    def test_two_native_builds_never_overlap(self):
        self.set_memory(90)  # 43 GB dostępne: oba po 10 GB zmieściłyby się naraz
        first = self.start("portivo-mobile up storefront-mobile", sleep="1.5")
        self.wait_for(lambda: len(self.state()["running"]) == 1)
        second = self.start("portivo-mobile up charter-mobile", sleep="0.3")
        go = self.start("cd apps/charter-service && go test ./internal/moneyfmt/", sleep="0.2")
        self.assertEqual(self.done(go)[0], 0)  # Go nie czeka za natywnym w kolejce
        for p in (first, second):
            self.assertEqual(self.done(p)[0], 0)
        log = self.go_log()
        first_pid = [r[1] for r in log if r[0] == "start" and "storefront-mobile" in r[3]][0]
        first_end = [r[2] for r in log if r[0] == "end" and r[1] == first_pid][0]
        second_start = [r[2] for r in log if r[0] == "start" and "charter-mobile" in r[3]][0]
        go_start = [r[2] for r in log if r[0] == "start" and "moneyfmt" in r[3]][0]
        self.assertGreaterEqual(second_start, first_end - 0.05)
        self.assertLess(go_start, first_end)

    def test_status_command(self):
        self.done(self.start("cd apps/charter-service && go test ./internal/moneyfmt/"))
        out = subprocess.run(
            ["/usr/bin/python3", SCRIPT, "status"],
            env=self.env,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("Nic nie biegnie.", out)
        self.assertIn("Dziś: 1 lokalnie", out)
        data = json.loads(
            subprocess.run(
                ["/usr/bin/python3", SCRIPT, "status", "--json"],
                env=self.env,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertNotIn("_internal", data)
        self.assertEqual(data["recent"][0]["label"], "go test ./internal/moneyfmt/")


class HostTest(Paths):
    """Orca i Pod: CLI hosta po pełnej ścieżce to dalej to samo CLI, a panel agenta idzie z env."""

    def test_host_cli_by_full_path_is_not_a_project_script(self):
        import orcahost

        for app, cli in ((orcahost.orca().app, "orca"), ("/Applications/Pod.app", "podx")):
            for command in (f"{app}/Contents/Resources/bin/{cli} terminal list --json", f"{cli} worktree ps"):
                with self.subTest(command=command):
                    self.assertIsNone(S.classify(command, self.repo))

    def test_standalone_fallback_matches_orcahost(self):
        """sched.py wczytany bez __file__ i bez sąsiadów ma te same nazwy, co orcahost."""
        import orcahost

        self.assertEqual((S.HOST_CLIS, S.PANE_ENV), (orcahost.CLI_NAMES, orcahost.PANE_ENV))
        code = (
            "import sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            "m = type(sys)('acc_sched')\n"
            f"SourceFileLoader('acc_sched', {SCRIPT!r}).exec_module(m)\n"
            "print('orcahost' in sys.modules, m.HOST_CLIS, m.PANE_ENV)\n"
        )
        out = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), f"False {orcahost.CLI_NAMES} {orcahost.PANE_ENV}")

    def test_agent_pane_comes_from_the_host_env(self):
        with mock.patch.dict(os.environ, {"ORCA_PANE_KEY": "tab-1:pane-2"}):
            self.assertEqual(S.agent_info("abcdef123456", "worker")["pane"], "tab-1:pane-2")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(S.agent_info(None, None)["pane"])


class JamTest(Paths):
    """2026-10-09 kolejka stała ~28 min z 14 jobami. Prognozy z rodziny (`pnpm run tc:cli` 30,3 GB po
    `pnpm tc` z e2e w tej samej rodzinie pnpm, `ensure:electron-runtime` 30,3 GB, esbuild 11,9 GB,
    `tsc --help` 9,8 GB, go test w małym module 10 GB), rezerwy po prognozie zamiast po pomiarze (e2e
    14,8 GB przy 2,4 użytych), ETA „about 5s” przez 20 minut za jobami, które przeciągnęły, i trzy
    vitesty stojące bez CPU z rezerwą po 4 GB. Liczby z history.jsonl i state.json tego dnia."""

    def setUp(self):
        super().setUp()
        self.ide = os.path.join(self.dir, "ide")
        os.makedirs(os.path.join(self.ide, ".git"))
        write(os.path.join(self.ide, "package.json"), json.dumps({"scripts": {
            "tc": "pnpm run tc:node && pnpm run tc:web", "tc:cli": "tsc -p cli", "tc:web": "tsc -p web",
            "ensure:electron-runtime": "node scripts/ensure.mjs", "e2e": "playwright test"}}))
        for name in ("node_modules/.bin/esbuild", "node_modules/typescript/bin/tsc", "scripts/ensure.mjs"):
            write(os.path.join(self.ide, name), "#!/bin/sh\n", 0o755)
        self.small = self.cfg["small_gb"]

    def rows(self):
        """Rodzina pnpm repo `ide` tego dnia: `pnpm tc` dwumodalne (5,7-6,5 i 15-20 GB, z komendami
        złożonymi w środku) i ciężkie skrypty e2e obok."""
        rows = []
        for i, peak in enumerate([5.9, 20.4, 6.2, 18.1, 5.8, 9.1, 20.2, 6.5, 15.8, 5.9] * 2):
            rows.append({"where": "local", "lang": "generic", "class": "ide:script:pnpm-script:tc",
                         "peak_gb": peak, "wall_s": 10.0 + i})
        for peak in (11.5, 8.7, 9.3):
            rows.append({"where": "local", "lang": "generic", "class": "ide:script:pnpm-script:tc:node",
                         "peak_gb": peak, "wall_s": 30.0})
        for peak in (19.0, 22.0, 18.5, 21.0, 20.0):
            rows.append({"where": "local", "lang": "generic", "class": "ide:script:pnpm-script:e2e",
                         "peak_gb": peak, "wall_s": 300.0})
        return rows

    def predict(self, command, rows=None):
        job = S.classify(command, self.ide)
        self.assertIsNotNone(job, command)
        return S.predict(job, 4, self.rows() if rows is None else rows, self.small)

    def test_fallbacks_without_own_history_are_capped_and_per_verb(self):
        gb, _s, src = self.predict("pnpm run tc:cli")
        self.assertEqual(src, "family:typecheck:23")  # tc, tc:node: ten sam czasownik, bez e2e
        self.assertLessEqual(gb, self.small)  # było 30,32 GB
        # ensure i esbuild nie mają czasownika pracy: od 1.30.1 nie trafiają do kolejki wcale (30,32 i 11,92 GB)
        for command in ("pnpm run ensure:electron-runtime", "node_modules/.bin/esbuild src/main.ts --bundle"):
            self.assertIsNone(S.classify(command, self.ide), command)
        self.assertEqual(self.predict("node node_modules/typescript/bin/tsc --help --all")[2], "light")  # 9,85
        # nowy skrypt bez nikogo z tym samym czasownikiem: cała rodzina, odpornie i z sufitem
        self.assertLessEqual(self.predict("pnpm run e2e")[0], self.small)

    def test_one_run_teaches_the_class(self):
        """Po pierwszym biegu liczy się zmierzony szczyt, nie zgadywanie z rodziny."""
        rows = self.rows() + [{"where": "local", "lang": "generic", "class": "ide:script:pnpm-script:tc:cli",
                               "peak_gb": 2.92, "wall_s": 7.1}]
        gb, s, src = self.predict("pnpm run tc:cli", rows)
        self.assertEqual(src, "history:1")
        self.assertAlmostEqual(gb, max(2.92 * 1.15, self.small * 0.8), places=2)
        self.assertEqual(s, 7.1)

    def test_compound_runs_do_not_teach_a_single_command(self):
        """Szczyt komendy złożonej (oxlint; pnpm test; pnpm tc) to szczyt całości: nie uczy `pnpm tc`."""
        tc = "ide:script:pnpm-script:tc"
        single = [{"where": "local", "lang": "generic", "class": tc, "peak_gb": 6.0, "wall_s": 10.0}] * 5
        compound = [{"where": "local", "lang": "generic", "class": tc, "peak_gb": 20.0, "wall_s": 60.0,
                     "multi": True}] * 5
        self.assertAlmostEqual(self.predict("pnpm tc", single + compound)[0], 6.9)
        job = S.classify("pnpm test; pnpm tc", self.ide)
        self.assertTrue(job["multi"])
        self.assertTrue(S.new_entry(job, "pnpm test; pnpm tc", None, {})["multi"])
        # złożona bez własnych biegów: najcięższa część, czasy po kolei
        cmd = "pnpm run tc:web > /dev/null 2>&1; npx playwright test tests/e2e/a.spec.ts"
        job = S.classify(cmd, self.ide)
        self.assertEqual([p["kind"] for p in job["parts"]], ["e2e"])
        part_gb, part_s, _ = S.predict(dict(job["parts"][0], multi=False), 4, [], self.small)
        main_gb, main_s = S.GENERIC_PRIORS["script"]
        gb, s, _src = S.predict(job, 4, [], self.small)
        self.assertEqual((gb, s), (max(min(main_gb, self.small), part_gb), main_s + part_s))

    def test_go_tree_in_a_small_module_scales_with_its_packages(self):
        mod = os.path.join(self.dir, "tinymod")
        write(os.path.join(mod, "go.mod"), "module tinymod\n\ngo 1.22\n")
        for pkg in ("a", "b", "c", "d", "e"):
            write(os.path.join(mod, pkg, "x.go"), f"package {pkg}\n")
        job = S.classify("go test -tags permguard ./...", mod)
        gb, s, src = S.predict(job, 4, [], self.small)
        self.assertEqual(src, "prior")
        self.assertAlmostEqual(gb, 1.0 + 0.15 * 5)  # było 10 GB przy szczycie 0,29
        self.assertLessEqual(s, 30 + 4 * 5)
        # moduł z pomiarów w tabeli zostaje przy swojej prognozie
        charter = S.classify("go test ./...", self.charter)
        self.assertGreater(S.predict(charter, 4, [], self.small)[0], self.small)

    def jam(self, ensure_elapsed=120.0):
        """Stan z 2026-10-09 ~22:27: biegnie ensure z prognozą 30,32 GB przy 3,1 GB użytych, w kolejce
        `pnpm tc` (21,1 GB) jako głowa i joby po 4 GB za nim."""
        self.set_memory(57)  # 27,4 GB dostępne z 48
        st = self.state()
        now = time.time()
        st["running"] = [{"id": "ensure", "label": "pnpm run ensure:electron-runtime", "where": "local",
                          "mem_predicted_gb": 30.32, "mem_now_gb": 3.1, "mem_peak_gb": 3.1,
                          "predicted_wall_s": 34.0, "predicted_from": "family:61",
                          "started_at": now - ensure_elapsed}]
        S.refresh_memory(st, self.cfg)
        st["queue"] = [
            {"id": "tc", "label": "pnpm tc", "mem_predicted_gb": 21.1, "predicted_wall_s": 21.0,
             "predicted_from": "history:20", "small": False, "enqueued_at": now - 900, "route": {"choice": "local"}},
            {"id": "perm", "label": "./perm-guard", "mem_predicted_gb": 4.0, "predicted_wall_s": 60.0,
             "predicted_from": "prior", "small": True, "enqueued_at": now - 78, "route": {"choice": "local"}},
            {"id": "test", "label": "pnpm test", "mem_predicted_gb": 19.0, "predicted_wall_s": 90.0,
             "predicted_from": "history:20", "small": False, "enqueued_at": now - 44, "route": {"choice": "local"}},
        ]
        return st, now

    def test_reservation_follows_what_a_job_uses_after_warm_up(self):
        st, now = self.jam(ensure_elapsed=20.0)  # w rozgrzewce: cała prognoza
        self.assertAlmostEqual(st["memory"]["reserved_gb"], 30.32 - 3.1, places=1)
        self.assertLess(st["memory"]["free_for_admission_gb"], 0)
        st, now = self.jam(ensure_elapsed=120.0)  # po rozgrzewce: szczyt × 1,5 + 1 GB
        self.assertAlmostEqual(st["memory"]["reserved_gb"], 3.1 * 1.5 + 1.0 - 3.1, places=1)
        self.assertGreater(st["memory"]["free_for_admission_gb"], 15)
        # e2e z prognozą 14,8 GB, które bierze 2,4 GB, rezerwuje 2,2, a nie 12,4
        e2e = {"where": "local", "mem_predicted_gb": 14.81, "mem_now_gb": 2.4, "mem_peak_gb": 2.4,
               "predicted_wall_s": 93.0, "started_at": now - 100}
        self.assertAlmostEqual(S.growth_left(e2e, now), 2.2, places=2)
        # natywny build trzyma pamięć poza drzewem: rezerwa zostaje przy prognozie
        self.assertAlmostEqual(S.growth_left(dict(e2e, lang="native"), now), 14.81 - 2.4, places=2)

    def test_jobs_behind_an_unstartable_head_start_after_aging(self):
        st, now = self.jam()
        free = st["memory"]["free_for_admission_gb"]
        self.assertTrue(4.0 <= free < 21.1, free)
        # głowa twardo rezerwuje (czeka > 2 × starve_s): perm-guard (4 GB) jeszcze nie wchodzi...
        self.assertEqual(S.plan(st, self.cfg, now), {})
        # ...a po aging_s czekania wchodzi, choć głowa dalej się nie mieści
        st["queue"][1]["enqueued_at"] = now - self.cfg["aging_s"] - 1
        self.assertEqual(S.plan(st, self.cfg, now), {"perm": ("overtake", "tc")})
        # job, który się nie mieści (19 GB obok 4 GB perm-guard), dalej czeka
        st["queue"][2]["enqueued_at"] = now - self.cfg["aging_s"] - 1
        self.assertNotIn("test", S.plan(st, self.cfg, now))

    def test_eta_behind_an_overrun_job_is_not_five_seconds(self):
        now = time.time()
        late = {"id": "rerun", "label": "rerun-failed.sh", "where": "local", "mem_predicted_gb": 6.0,
                "mem_now_gb": 6.0, "predicted_wall_s": 300.0, "predicted_from": "history:20",
                "started_at": now - 1500}
        self.assertAlmostEqual(S.time_left(late, now), 750, delta=1)  # połowa tego, co już biegnie
        self.assertAlmostEqual(S.time_left(dict(late, started_at=now - 200), now), 100, delta=1)
        self.set_memory(30)
        st = self.state()
        st["running"] = [late]
        S.refresh_memory(st, self.cfg)
        st["queue"] = [{"id": "big", "label": "big", "mem_predicted_gb": 12.0, "predicted_wall_s": 60.0,
                        "predicted_from": "history:20", "small": False, "enqueued_at": now - 30,
                        "route": {"choice": "local"}}]
        S.update_queue_view(st, self.cfg)
        text = st["queue"][0]["reason"]["text"]
        self.assertIn("rerun-failed.sh", text)
        self.assertNotIn("about 5s", text)
        self.assertIn("about 12m", text)

    def test_stalled_job_releases_its_reservation_and_says_why(self):
        st, now = self.jam(ensure_elapsed=20.0)
        st["running"][0]["stalled_s"] = 1080
        S.refresh_memory(st, self.cfg)
        self.assertEqual(st["memory"]["reserved_gb"], 0.0)
        S.update_queue_view(st, self.cfg)
        reasons = [j["reason"]["text"] for j in st["queue"]]
        self.assertTrue(any("ensure:electron-runtime stalled 18m (no CPU, no output)" in r for r in reasons), reasons)

    def test_stall_watch(self):
        cfg = dict(self.cfg, stall_s=600)
        watch = S.StallWatch(cfg, {"class": "ide:test:vitest", "label": "vitest"}, "j-1")
        with mock.patch.object(S, "notify") as notify, mock.patch.object(S.StallWatch, "output_size", return_value=0):
            t = 1000.0
            self.assertIsNone(watch.sample(t, 5.0, {10, 11}))
            self.assertIsNone(watch.sample(t + 300, 5.2, {10, 11}))  # 0,2 s CPU: to jeszcze nie życie
            self.assertEqual(watch.sample(t + 600, 5.3, {10, 11}), 600)
            self.assertEqual(watch.sample(t + 700, 5.3, {10, 11}), 700)
            self.assertEqual(notify.call_count, 1)  # raz, nie co sekundę
            self.assertIsNone(watch.sample(t + 701, 6.0, {10, 11}))  # CPU wraca: pracuje
            self.assertIsNone(watch.sample(t + 1400, 6.0, {10, 11, 12}))  # nowy proces to też życie
        # wyjście do pliku też liczy się jako życie
        watch = S.StallWatch(cfg, {"class": "x", "label": "x"}, "j-2")
        sizes = iter([0, 10, 20])
        with mock.patch.object(S, "notify"), mock.patch.object(S.StallWatch, "output_size", side_effect=lambda: next(sizes)):
            watch.sample(0.0, 1.0, {1})
            self.assertIsNone(watch.sample(700.0, 1.0, {1}))
            self.assertIsNone(watch.sample(1400.0, 1.0, {1}))

    def test_class_timeout_is_off_by_default_and_terminates_when_set(self):
        import signal

        job = {"class": "ide:test:vitest", "label": "vitest"}
        with mock.patch.object(S.os, "killpg") as killpg:
            S.StallWatch(dict(self.cfg), job, "j").enforce(10**6, 4242)
            killpg.assert_not_called()
            watch = S.StallWatch(dict(self.cfg, class_timeout_s={"ide:test": 1800}), job, "j")
            watch.enforce(1799, 4242)
            killpg.assert_not_called()
            watch.enforce(1800, 4242)
            self.assertEqual(killpg.call_args_list[-1], mock.call(4242, signal.SIGTERM))
            watch.enforce(1805, 4242)
            self.assertEqual(killpg.call_args_list[-1], mock.call(4242, signal.SIGTERM))
            watch.enforce(1811, 4242)
            self.assertEqual(killpg.call_args_list[-1], mock.call(4242, signal.SIGKILL))


class StallRunTest(unittest.TestCase):
    """Prawdziwy `sched.py run` z jobem, który śpi: stoi, mówi o tym w stanie i historii, nie ginie."""

    setUp, kill_all, set_memory, done = RunTest.setUp, RunTest.kill_all, RunTest.set_memory, RunTest.done

    def test_sleeping_job_is_marked_stalled_and_finishes_untouched(self):
        os.makedirs(self.sched_dir, exist_ok=True)
        with open(os.path.join(self.sched_dir, "config.json"), "w") as f:
            json.dump({"stall_s": 1}, f)
        calls = os.path.join(self.dir, "osascript.log")
        write(os.path.join(self.bin, "osascript"), f"#!/bin/sh\necho \"$@\" >> {calls}\n", 0o755)
        shop = os.path.join(self.dir, "shop")
        make_generic_repo(shop)
        write(os.path.join(shop, "scripts/e2e.sh"), "#!/bin/sh\nsleep 4\necho done\n", 0o755)
        p = subprocess.Popen(["/usr/bin/python3", SCRIPT, "run", "--via", "hook", "--shell", "./scripts/e2e.sh"],
                             cwd=shop, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        rc, out, err = self.done(p)
        self.assertEqual((rc, out.strip()), (0, "done"), err)
        self.assertIn("stoi od", err)
        rows = [json.loads(l) for l in open(os.path.join(self.sched_dir, "history.jsonl"))]
        self.assertGreaterEqual(rows[-1]["stalled_s"] or 0, 1)
        self.assertIn("job stoi", open(calls).read())


if __name__ == "__main__":
    unittest.main()


def make_generic_repo(root):
    """Repo spoza Go i JS z ciężkimi komendami: skrypty, CLI projektu, Cargo, package.json."""
    os.makedirs(os.path.join(root, ".git"))
    for name in ("scripts/e2e.sh", "scripts/capture.py", "scripts/build_assets.py", "scripts/dev-server.sh", "bin/verify",
                 "plugins/cli/main.ts", "manage.py", "e2e.sh", "tools/emulator", "perm-guard", "tools/perm-guard",
                 "scripts/commit-pliki.sh"):
        write(os.path.join(root, name), "#!/bin/sh\necho ok\n", 0o755)
    write(os.path.join(root, "Cargo.toml"), "[package]\nname = 'x'\n")
    write(os.path.join(root, "package.json"), json.dumps({"scripts": {
        "sm": "node plugins/cli/main.ts", "app": "next dev", "format": "oxfmt", "build:addon": "node-gyp rebuild",
        "ensure:electron-runtime": "node scripts/ensure.mjs",
        "test": "vitest run", "e2e:ci": "playwright test"}}))


class GenericTest(Paths):
    """Ciężka praca spoza Go i JS: po kształcie komendy, z klasą po podpisie i nauką z historii.
    Błąd, który łapią: komenda, która stawia przeglądarkę albo kompilator, omija scheduler
    (08.10: `sm capture`, `sm verify` i skrypty owijające), albo scheduler owija serwer, watcher
    czy REPL, który nigdy się nie kończy i trzyma swoją rezerwę."""

    WRAPPED = {
        "cargo test": "shop:test:cargo:test",
        "RUST_LOG=1 cargo build --release 2>&1 | tail -5": "shop:build:cargo:build",
        "swift test": "shop:test:swift:test",
        "docker build .": "shop:build:docker:build",
        "pytest -x tests": "shop:test:pytest:tests",
        "python3 -m pytest": "shop:test:pytest",
        "uv run pytest": "shop:test:pytest",
        "python3 scripts/build_assets.py --url http://localhost:3000": "shop:script:python:scripts/build_assets.py",
        "uv run python scripts/build_assets.py": "shop:script:python:scripts/build_assets.py",
        "python3 manage.py test": "shop:script:python:manage.py:test",
        "node plugins/cli/main.ts build": "shop:script:node:plugins/cli/main.ts:build",
        "tsx plugins/cli/main.ts verify": "shop:script:tsx:plugins/cli/main.ts:verify",
        "./scripts/e2e.sh": "shop:script:script:scripts/e2e.sh",
        "bash scripts/e2e.sh": "shop:script:bash:scripts/e2e.sh",
        "e2e.sh": "shop:script:script:e2e.sh",
        "bin/verify all": "shop:script:script:bin/verify:all",
        "sh -c 'cargo test'": "shop:test:cargo:test",
        "npx lighthouse http://localhost:3000": "shop:e2e:lighthouse",
        "pnpm exec cypress run": "shop:e2e:cypress:run",
        "make e2e": "shop:script:make:e2e",
        "just test": "shop:script:just:test",
        "deno test": "shop:test:deno:test",
        "deno task build": "shop:test:deno:task",
        "npx nx run-many -t build": "shop:build:nx:run-many",
        "nx run app:build": "shop:build:nx:run",
    }
    LEFT_ALONE = (
        # serwery, watchery, REPL-e: nigdy się nie kończą
        "python3 manage.py runserver", "python3 -m http.server", "node server.js", "pnpm app",
        "./scripts/dev-server.sh", "make dev", "just serve", "npx webpack --watch", "expo start",
        "docker compose up", "cargo watch -x test", "tools/emulator -avd Pixel_9 -no-window",
        # skrypt projektu, który tylko pyta, i program systemowy po pełnej ścieżce
        "./scripts/e2e.sh status", "python3 scripts/capture.py help", "/opt/homebrew/opt/postgresql@18/bin/pg_dump -Fc",
        # chwila liczenia albo informacje
        "python3 -c 'print(1)'", "python3 - <<EOF", "node -e 1", "node --version", "cargo fmt",
        "cargo --version", "pnpm format", "pnpm install", "pnpm foo", "python3 missing.py",
        # zwykłe narzędzia i komendy, które już idą przez scheduler
        "git status", "ls -la", "gh pr list", "claude-acc sched run -- cargo test",
        "/Users/x/.local/bin/claude-acc status", "echo cargo test",
    )
    # bez czasownika pracy w nazwie (build, test, e2e, typecheck, lint, check...): od razu, bez kolejki.
    # 2026-10-09 `./perm-guard` (1 ms) czekał 2m24s za `ensure:electron-runtime`; `sm capture` i
    # skrypty bez takiej nazwy też (decyzja z 2026-10-10: tylko znane ciężkie czasowniki)
    PASSED_THROUGH = (
        "./perm-guard perm-guard", "cd tools && echo '{}' | ./perm-guard perm-guard",
        "bash scripts/commit-pliki.sh /tmp/x 'feat: y'", "make prepare-emails", "pnpm sm capture",
        "bun run sm", "python3 scripts/capture.py --url http://localhost:3000",
        "node plugins/cli/main.ts capture", "pnpm run ensure:electron-runtime",
        "node_modules/.bin/esbuild src/main.ts --bundle", "deno run main.ts", "deno task deploy",
        "nx run app:migrate", "lerna run publish-docs",
    )

    def setUp(self):
        super().setUp()
        self.shop = os.path.join(self.dir, "shop")
        make_generic_repo(self.shop)

    def test_commands_without_a_work_verb_pass_through(self):
        for command in self.PASSED_THROUGH:
            with self.subTest(command=command):
                self.assertIsNone(S.classify(command, self.shop), command)
        event = {"tool_name": "Bash", "cwd": self.shop, "tool_input": {"command": "./perm-guard perm-guard"}}
        self.assertIsNone(S.hook_rewrite(event))
        # ten sam program z czasownikiem pracy w podkomendzie idzie do kolejki
        self.assertIsNotNone(S.classify("./perm-guard build", self.shop))
        self.assertTrue(all(S.heavy_sig(s) for s in ("pnpm-script:build:ghostty-terminal-macos", "script:scripts/run-tests.sh",
                                                     "pnpm-script:tc:cli", "make:e2e", "script:scripts/stack.sh:build")))

    def test_heavy_shapes_are_classified_by_signature(self):
        for command, cls in self.WRAPPED.items():
            j = S.classify(command, self.shop)
            self.assertIsNotNone(j, command)
            self.assertEqual(j["class"], cls, command)
            self.assertEqual(j["lang"], "generic", command)

    def test_servers_repls_and_trivial_commands_stay_out(self):
        for command in self.LEFT_ALONE:
            self.assertIsNone(S.classify(command, self.shop), command)

    def test_switch_off_in_config(self):
        with open(S.CONFIG_PATH, "w") as f:
            json.dump({"generic": False}, f)
        self.assertIsNone(S.classify("cargo test", self.shop))
        self.assertIsNotNone(S.classify("cd apps/charter-service && go vet ./...", self.repo))

    def test_first_run_is_conservative_then_learns_from_the_signature(self):
        job = S.classify("python3 scripts/build_assets.py", self.shop)
        gb, s, src = S.predict(job, 4, [])
        self.assertEqual((gb, src), (S.GENERIC_PRIORS["script"][0], "prior"))
        rows = [{"where": "local", "lang": "generic", "class": job["class"], "peak_gb": 0.4, "wall_s": 9.0}] * 3
        gb, s, src = S.predict(job, 4, rows)
        self.assertEqual(src, "history:3")
        self.assertLess(gb, 1.0)

    def test_new_script_in_a_known_family_uses_the_family(self):
        # pięć innych skryptów Pythona w tym repo: nowy nie czeka na 4 GB
        rows = [{"where": "local", "lang": "generic", "class": f"shop:script:python:scripts/s{i}.py",
                 "peak_gb": 0.2, "wall_s": 3.0} for i in range(5)]
        job = S.classify("python3 scripts/build_assets.py", self.shop)
        gb, _s, src = S.predict(job, 4, rows)
        self.assertEqual(src, "family:5")
        self.assertAlmostEqual(gb, 0.5)  # p75 × 1,25, najmniej 0,5 GB
        # rodzina to repo, rodzaj i narzędzie: skrypty node się nie liczą
        node = S.classify("node plugins/cli/main.ts build", self.shop)
        self.assertEqual(S.predict(node, 4, rows)[2], "prior")

    def test_docker_never_predicts_below_its_floor(self):
        # praca dzieje się w maszynie wirtualnej Dockera: drzewo komendy waży prawie nic
        job = S.classify("docker build .", self.shop)
        rows = [{"where": "local", "lang": "generic", "class": job["class"], "peak_gb": 0.05, "wall_s": 60}] * 5
        self.assertEqual(S.predict(job, 4, rows)[0], S.DOCKER_FLOOR_GB)

    def test_generic_jobs_never_go_to_depot(self):
        job = S.classify("cargo test", self.shop)
        self.assertIsNone(S.depot_target(job, 30.0, 3000, self.cfg, {}))
        self.assertFalse(S.uses_pg(job, {}))

    def test_hook_wraps_generic_commands(self):
        event = {"tool_name": "Bash", "cwd": self.shop, "tool_input": {"command": "./scripts/e2e.sh --site x"}}
        with mock.patch.object(S, "with_rtk", side_effect=lambda c: c):
            out = S.hook_rewrite(event)
        argv = shlex.split(out["hookSpecificOutput"]["updatedInput"]["command"])
        self.assertEqual(argv[2:4], ["run", "--via"])
        self.assertEqual(argv[-1], "./scripts/e2e.sh --site x")

    def test_reservation_ends_for_a_job_that_outlived_its_prediction(self):
        # job, który biegnie trzy razy dłużej, niż miał (serwer puszczony przez skrypt), nie
        # trzyma rezerwy na wzrost, który nie przyjdzie
        now = time.time()
        job = {"where": "local", "mem_predicted_gb": 4.0, "mem_now_gb": 1.0, "predicted_wall_s": 120,
               "started_at": now - 30}
        self.assertEqual(S.growth_left(job, now), 3.0)  # w rozgrzewce: cała prognoza
        self.assertEqual(S.growth_left(dict(job, started_at=now - 700), now), 0.0)
        self.assertEqual(S.growth_left(dict(job, paused=True), now), 0.0)
        self.assertEqual(S.growth_left(dict(job, stalled_s=700), now), 0.0)


class GitGrepTest(unittest.TestCase):
    """2026-10-08: `git grep -nE` agenta po commicie z 368 MB binarek urósł do 10 GB w 2 s."""

    def test_agent_git_grep_gets_minus_i(self):
        cases = {
            'git grep -nE "(a|b)" 4b0ed7ef1 -- apps': 'git grep -I -nE "(a|b)" 4b0ed7ef1 -- apps',
            "cd x && git -C y grep foo | head": "cd x && git -C y grep -I foo | head",
            'for n in a b; do git grep -nE "$n" HEAD; done': 'for n in a b; do git grep -I -nE "$n" HEAD; done',
            "x=$(git grep -l foo)": "x=$(git grep -I -l foo)",
            "rtk proxy git grep foo": "rtk proxy git grep -I foo",
        }
        for command, want in cases.items():
            self.assertEqual(S.git_grep_text_only(command), want, command)

    def test_explicit_binary_choice_and_other_text_stay(self):
        for command in ("git grep -nI foo", "git grep -a foo", "git grep --text foo",
                        "git grep --binary-files=text foo", "echo git grep", "rg -n 'git grep' docs"):
            self.assertEqual(S.git_grep_text_only(command), command, command)


class RtkExcludesTest(unittest.TestCase):
    """Hook rtk i hook schedulera nie mogą przepisywać tej samej komendy (wynik losowy): rtk
    zostawia to, co owija scheduler, i nic więcej. Sprawdza samo rtk, bo ono normalizuje komendy
    po swojemu (`uv run pytest` to dla niego `pytest`)."""

    NOT_WRAPPED_RTK_REWRITES = ("git status", "cargo fmt", "docker ps", "pnpm install", "ls -la",
                                "pnpm add -D vitest", "npm ls", "ruff check .", "uv pip list")

    def rtk(self, home, command):
        r = subprocess.run(["rtk", "rewrite", command], capture_output=True, text=True,
                           env=dict(os.environ, HOME=home, RTK_TELEMETRY_DISABLED="1"), timeout=10)
        return r.returncode in (0, 3) and r.stdout.strip() != command

    @unittest.skipUnless(shutil.which("rtk"), "brak rtk")
    def test_rtk_leaves_alone_exactly_what_the_scheduler_wraps(self):
        home = tempfile.mkdtemp(prefix="rtk-home-")
        self.addCleanup(shutil.rmtree, home, True)
        config = os.path.join(home, "Library/Application Support/rtk/config.toml")
        write(config, "")  # rtk odrzuca cały plik z niepełną sekcją, więc tylko [hooks]
        self.assertTrue(S.write_rtk_excludes(config))
        empty = tempfile.mkdtemp(prefix="rtk-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        shop = tempfile.mkdtemp(prefix="rtk-shop-")
        self.addCleanup(shutil.rmtree, shop, True)
        make_generic_repo(os.path.join(shop, "shop"))
        for command in GenericTest.WRAPPED:
            self.assertIsNotNone(S.classify(command, os.path.join(shop, "shop")), command)
            self.assertFalse(self.rtk(home, command), f"rtk przepisałby {command}")
        for command in self.NOT_WRAPPED_RTK_REWRITES:
            if self.rtk(empty, command):  # rtk umie je skrócić: nasze wyjątki mu nie przeszkadzają
                self.assertTrue(self.rtk(home, command), command)


class CodexHookTest(unittest.TestCase):
    """`sched.py codex install`: hook obok cudzych (rtk, Orca), bez dubli, zdejmowany tylko nasz."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="codex-home-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "hooks.json")
        others = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk-rewrite.sh"}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": "orca-hook.sh"}]}]}}
        write(self.path, json.dumps(others))
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cmd(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return S.cmd_codex(list(args))

    def test_install_twice_then_uninstall(self):
        self.assertEqual(self.run_cmd("status"), 1)
        self.assertEqual(self.run_cmd("install"), 0)
        self.assertEqual(self.run_cmd("install"), 0)
        data = json.load(open(self.path))
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertEqual(pre[0]["hooks"][0]["command"], "rtk-rewrite.sh")
        self.assertTrue(S.is_codex_entry(pre[1]))
        self.assertEqual(self.run_cmd("status"), 0)
        self.assertEqual(self.run_cmd("uninstall"), 0)
        data = json.load(open(self.path))
        self.assertEqual(len(data["hooks"]["PreToolUse"]), 1)
        self.assertIn("Stop", data["hooks"])
        before = open(self.path).read()
        self.run_cmd("uninstall")
        self.assertEqual(open(self.path).read(), before)


class CancelSafetyTest(unittest.TestCase):
    """`cancel` wysyła sygnał tylko wrapperowi `sched.py run`: pid z wpisu mógł przejść na inny proces."""

    def test_a_pid_that_is_not_a_wrapper_gets_no_signal(self):
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        with mock.patch.object(os, "kill", wraps=os.kill) as kill:
            self.assertFalse(S.signal_job({"pid": sleeper.pid}, signal.SIGTERM))
            self.assertFalse(S.signal_job({"pid": None}, signal.SIGTERM))
        self.assertEqual([c.args[1] for c in kill.call_args_list], [0])  # tylko sprawdzenie, czy żyje
        # grupa, której lider nie jest dzieckiem wrappera z wpisu, zostaje w spokoju
        self.assertFalse(S.kill_child({"pid": 1, "child_pgid": sleeper.pid}))
        self.assertIsNone(sleeper.poll())

    def test_wrapper_recognised_through_acc_launcher(self):
        with mock.patch.object(S, "proc_args", return_value=["python", "/x/acc.py", "sched", "run"]):
            self.assertTrue(S.is_wrapper(1))
        with mock.patch.object(S, "proc_args", return_value=["python", "/x/acc.py", "devguard", "run"]):
            self.assertFalse(S.is_wrapper(1))


class NestedRunTest(unittest.TestCase):
    """Skrypt w środku wpuszczonego joba woła `sched run` sam (sm-heavy.sh, plock): drugi raz
    nie czeka na pamięć, którą jego job już ma; bez tego przy ciasnej pamięci czekałby na siebie."""

    setUp, kill_all, set_memory, done = RunTest.setUp, RunTest.kill_all, RunTest.set_memory, RunTest.done

    def test_job_child_sees_its_job_and_inner_run_does_not_queue(self):
        shop = os.path.join(self.dir, "shop")
        make_generic_repo(shop)
        inner = (f"/usr/bin/python3 {SCRIPT} run --via plock -- /bin/sh -c 'echo inner=$CLAUDE_ACC_SCHED_JOB'")
        write(os.path.join(shop, "scripts/e2e.sh"), f"#!/bin/sh\n{inner}\n", 0o755)
        p = subprocess.Popen(["/usr/bin/python3", SCRIPT, "run", "--via", "hook", "--shell", "./scripts/e2e.sh"],
                             cwd=shop, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        rc, out, err = self.done(p)
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"inner=j-\d+-[0-9a-f]{4}")
        rows = [json.loads(l) for l in open(os.path.join(self.sched_dir, "history.jsonl"))]
        self.assertEqual([r["class"] for r in rows], ["shop:script:script:scripts/e2e.sh"])
        self.assertEqual(rows[0]["lang"], "generic")
