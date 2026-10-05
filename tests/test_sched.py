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
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

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

    def set_memory(self, level, swap=1.0, pressure="normal", ram=48):
        with open(self.memfile, "w") as f:
            json.dump(
                {"level": level, "ram_gb": ram, "swap_gb": swap, "pressure": pressure},
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
        with mock.patch.object(S.shutil, "which", return_value="/opt/homebrew/bin/rtk"), \
                mock.patch.object(S.subprocess, "run", side_effect=run):
            self.assertEqual(S.with_rtk("go test ./..."), "rtk go test ./...")
        argv, kw = calls[0]
        self.assertEqual(argv, ["/opt/homebrew/bin/rtk", "rewrite", "go test ./..."])
        self.assertNotEqual(kw["env"]["HOME"], os.environ.get("HOME"))
        self.assertTrue(kw["env"]["HOME"].startswith(S.STATE_DIR))

    def test_keeps_the_command_when_rtk_has_nothing(self):
        for rc, out in ((1, ""), (0, ""), (2, "garbage")):
            calls, run = self.fake_rtk(rc, out)
            with mock.patch.object(S.shutil, "which", return_value="/x/rtk"), \
                    mock.patch.object(S.subprocess, "run", side_effect=run):
                self.assertEqual(S.with_rtk("go run ./cmd/x"), "go run ./cmd/x", rc)
        with mock.patch.object(S.shutil, "which", return_value=None):
            self.assertEqual(S.with_rtk("go test ./..."), "go test ./...")
        with mock.patch.object(S.shutil, "which", return_value="/x/rtk"), mock.patch.object(
            S.subprocess, "run", side_effect=subprocess.TimeoutExpired("rtk", 3)
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


class StateTest(Paths):
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
                {"level": level, "ram_gb": 48, "swap_gb": swap, "pressure": pressure}, f
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


if __name__ == "__main__":
    unittest.main()
