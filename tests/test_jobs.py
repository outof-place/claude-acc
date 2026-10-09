"""Testy `claude-acc jobs` (jobs.py): jeden bieg bloga od początku do końca i jego zapis.

Każdy test stawia osobny $HOME (świat z tests/test_runenv.py: przypięta atrapa Claude Code,
kredyt 1@example.com w puli, atrapy `security` i `claude-acc`) i uruchamia prawdziwy jobs.py jako
proces, z prawdziwym runenv (prepare, licznik, finish) i prawdziwym schedulerem pamięci
(sched.py z SCHED_FAKE_MEMORY). Potok bloga to atrapa tests/fakes-jobs/pipeline, która robi
to, co opisuje kontrakt v2: kody wyjścia, JSON wyniku, znacznik side-effect, blok WYNIK.

Wartości oczekiwane biorą się z kontraktu (tabela kodów, punkt 5) i z briefu etapu A2a (fakty,
które przebijają kod przed fazą side-effect, PILNE po niej, 1,5 × budżet, jeden bieg joba), nie
z kodu jobs.py.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_jobs
"""

import fcntl
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import unittest

try:
    from tests.test_credits import FAKES, FAKES_CREDITS, KEY_A, PY, ROOT, read, write_json
    from tests.test_runenv import FAKES_RUNENV, VERSION, accounts
    from tests.test_runenv import World as RunWorld
except ImportError:  # uruchomione z katalogu tests
    from test_credits import FAKES, FAKES_CREDITS, KEY_A, PY, ROOT, read, write_json
    from test_runenv import FAKES_RUNENV, VERSION, accounts
    from test_runenv import World as RunWorld

HERE = os.path.dirname(os.path.abspath(__file__))
FAKES_JOBS = os.path.join(HERE, "fakes-jobs")
PIPELINE = os.path.join(FAKES_JOBS, "pipeline")
JOBS = os.path.join(ROOT, "jobs.py")
PAYER = "1@example.com"

# tabela kodów z kontraktu v2, punkt 4: kod -> (wynik, czy ostateczny)
CONTRACT = {0: "OPUBLIKOWANO", 2: "ZAPARKOWANO", 4: "BEZ WPISU", 1: "BŁĄD", 5: "PILNE"}
# pola zapisu biegu z briefu A2a ("The run record schema freezes at this gate") i ich rodzaje
SCHEMA = {
    "v": int, "id": str, "job": str, "state": str, "outcome": str, "reason": str, "title": (str, type(None)),
    "urls": list, "pr": (str, type(None)), "notes": str, "wynik": list, "cost_usd": (float, int, type(None)),
    "ledger_usd": (float, int, type(None)), "budget_usd": (float, int), "payer": (dict, type(None)),
    "metering_complete": (bool, type(None)), "run_id": (str, type(None)), "version": (str, type(None)),
    "started_at": float, "ended_at": float, "awake_seconds": (float, int), "slept_seconds": (float, int),
    "phase": str, "exit_code": (int, type(None)), "precheck": (dict, type(None)), "stopped": (str, type(None)),
    "facts": list, "override": (str, type(None)), "retryable": bool, "final": bool, "switch_payment": bool,
    "banner": (str, type(None)), "live_check": (list, type(None)), "log": str, "pid": int,
    "slot": (str, type(None)), "skip": (str, type(None)),
}  # fmt: skip


def closed_url():
    """Adres, pod którym nikt nie słucha: połączenie odrzucone, czyli brak odpowiedzi HTTP."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/wpis"


def write_atomic(path, data):
    tmp = f"{path}.tmp"
    write_json(tmp, data)
    os.replace(tmp, path)


def read_json_file(path):
    try:
        return json.loads(read(path))
    except (OSError, ValueError):
        return None


def dead_pid():
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    return proc.pid


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # zombie (dziecko testu, którego nikt nie zebrał) nie żyje
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


class World(RunWorld):
    """Świat biegu z test_runenv plus joby: kredyt 1@example.com płaci, pamięci jest dużo."""

    def __init__(self):
        super().__init__()
        self.jobs_dir = os.path.join(self.state, "jobs")
        self.clock = os.path.join(self.fake, "clock.json")
        self.memory = os.path.join(self.fake, "memory.json")
        self.roomy()
        self.credit(PAYER, KEY_A, 500, 20)

    def roomy(self):
        write_json(self.memory, {"level": 80, "ram_gb": 48, "swap_gb": 0, "pressure": "normal"})

    def tight(self):
        write_json(self.memory, {"level": 5, "ram_gb": 48, "swap_gb": 0, "pressure": "normal"})

    def env(self, **extra):
        env = super().env()
        env.update(
            CLAUDE_ACC_TOOL_PATH=f"{FAKES_JOBS}:{FAKES_RUNENV}:{FAKES_CREDITS}:{FAKES}:/usr/bin:/bin:/usr/sbin:/sbin",
            SCHED_FAKE_MEMORY=self.memory,
            CLAUDE_ACC_JOBS_CLOCK=self.clock,
        )
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def jobs(self, *args, timeout=120, **extra):
        return subprocess.run([PY, JOBS, *args], env=self.env(**extra), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)  # fmt: skip

    def add_job(self, name="blog", precheck=True, budget="2", **opts):
        args = ["add", name, "--cwd", self.cwd, "--entry", f"{PY} {PIPELINE} entry", "--budget-usd", budget]
        if precheck:
            args += ["--precheck", f"{PY} {PIPELINE} precheck"]
        opts.setdefault("run_min", "1")
        opts.setdefault("precheck_min", "1")
        opts.setdefault("grace_s", "3")
        for key, value in opts.items():
            flag = "--" + key.replace("_", "-")
            args += [flag] if value is True else [flag, value]
        r = self.jobs(*args)
        assert r.returncode == 0, r.stderr
        return r

    def run_job(self, name="blog", *flags, **env):
        """`jobs run NAME --json`: (proces, zapis biegu albo None)."""
        r = self.jobs("run", name, "--json", *flags, **env)
        try:
            return r, json.loads(r.stdout)
        except ValueError:
            return r, None

    def start(self, name="blog", **env):
        return subprocess.Popen([PY, JOBS, "run", name, "--json"], env=self.env(**env), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)  # fmt: skip

    def current(self, name="blog"):
        path = os.path.join(self.jobs_dir, name, "current.json")
        try:
            return json.loads(read(path))
        except (OSError, ValueError):
            return None

    def wait_phase(self, phase, name="blog", timeout=30):
        return self.wait_for(lambda cur: cur.get("phase") == phase, name, timeout, f"fazy {phase}")

    def wait_for(self, check, name="blog", timeout=30, what="warunku"):
        """current.json biegu, gdy check(current) jest prawdą."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.current(name)
            if cur and check(cur):
                return cur
            time.sleep(0.05)
        raise AssertionError(f"bieg {name} nie doszedł do {what}: {self.current(name)}")

    def devguard(self, delay=1.0):
        """Strażnik pamięci jak na tym Macu: devguard-state.json już jest, a ofiarę hamulca z
        brake.log (atrapa potoku) zapisuje delay sekund po zabiciu, na końcu swojego tiku, z wątku
        testu, czyli spoza drzewa procesów biegu. "at" to początek tiku, sprzed zabicia."""
        path = os.path.join(self.state, "devguard-state.json")
        write_atomic(path, {"writer": os.getpid(), "saved_at": time.time(), "lastresort": []})
        log = os.path.join(self.fake, "brake.log")

        def tick():
            deadline = time.time() + 60
            while not os.path.exists(log):
                if time.time() > deadline:
                    return
                time.sleep(0.05)
            pid, at = read(log).split()
            time.sleep(delay)
            event = {"at": float(at) - 0.5, "code": "runner", "pid": int(pid), "size": 3 * 1024**3,
                     "result": "zatrzymany", "level": 2, "resume": None}  # fmt: skip
            try:
                write_atomic(path, {"writer": os.getpid(), "saved_at": time.time(), "lastresort": [event]})
            except OSError:
                pass  # test już się skończył i sprzątnął $HOME

        threading.Thread(target=tick, daemon=True).start()

    def set_job(self, name="blog", *pairs):
        r = self.jobs("set", name, *pairs)
        assert r.returncode == 0, r.stderr
        return r

    def history(self, name="blog"):
        path = os.path.join(self.jobs_dir, name, "history.jsonl")
        if not os.path.exists(path):
            return []
        return [json.loads(x) for x in read(path).splitlines() if x.strip()]

    def pids(self):
        return [int(x) for x in self.recorded("pids.log").split()]

    def started(self, what):
        return [x for x in self.recorded("started.log").splitlines() if x.startswith(what)]

    def sched_history(self):
        path = os.path.join(self.state, "sched", "history.jsonl")
        return [json.loads(x) for x in read(path).splitlines() if x.strip()] if os.path.exists(path) else []


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.clean)

    def clean(self):
        for pid in self.w.pids():
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(self.w.home, ignore_errors=True)

    def assertNothingLeft(self):
        """Żaden proces potoku nie przeżył biegu (grupa procesów, finish, sweep)."""
        left = [p for p in self.w.pids() if alive(p)]
        self.assertEqual(left, [], "procesy potoku przeżyły bieg")


class ExitCodes(Base):
    """Kod wyjścia komendy według tabeli kontraktu, gdy nic poza nim się nie wydarzyło."""

    def test_every_contract_exit_code_maps_to_its_outcome(self):
        self.w.add_job()
        rows = [  # (kod, znacznik side-effect, retryable w JSON, oczekiwane retryable)
            (0, True, False, False), (2, True, False, False), (4, False, False, False),
            (1, False, True, True), (1, False, False, False), (5, True, False, False),
        ]  # fmt: skip
        for code, marker, retry_flag, retryable in rows:
            with self.subTest(code=code, retryable=retry_flag):
                env = {"FAKE_JOB_EXIT": str(code), "FAKE_JOB_MARKER": "1" if marker else None,
                       "FAKE_JOB_RETRYABLE": "1" if retry_flag else None}  # fmt: skip
                r, rec = self.w.run_job(**env)
                self.assertIsNotNone(rec, r.stderr)
                self.assertEqual(rec["outcome"], CONTRACT[code], rec["reason"])
                self.assertEqual(rec["exit_code"], code)
                self.assertEqual(rec["retryable"], retryable)
                self.assertEqual(rec["final"], not retryable)
                self.assertEqual(rec["phase"], "side-effect" if marker else "run")
                self.assertIsNone(rec["override"])
                self.assertEqual(rec["reason"], f"atrapa: {CONTRACT[code]}")
                self.assertEqual(r.returncode, code)  # `jobs run` kończy się kodem wyniku z kontraktu

    def test_published_run_record_has_every_field_cost_payer_and_log(self):
        self.w.add_job()
        r, rec = self.w.run_job("blog", "--slot", "2026-10-09T10:00", FAKE_JOB_CLAUDE="1", FAKE_JOB_MARKER="1",
                           FAKE_JOB_URLS="https://example.com/blog/wpis", FAKE_JOB_TITLE="Wpis o czymś")
        self.assertEqual(r.returncode, 0, r.stderr)
        for key, kind in SCHEMA.items():
            self.assertIn(key, rec)
            self.assertIsInstance(rec[key], kind, key)
        self.assertEqual((rec["outcome"], rec["state"], rec["job"]), ("OPUBLIKOWANO", "done", "blog"))
        self.assertEqual(rec["payer"], {"mode": "credits", "email": PAYER, "org_id": "org-a", "verdict": "ok"})
        self.assertTrue(rec["metering_complete"])
        self.assertAlmostEqual(rec["cost_usd"], 0.001)  # jedno zapytanie atrapy Claude Code
        self.assertAlmostEqual(rec["ledger_usd"], rec["cost_usd"])
        self.assertAlmostEqual(sum(e["usd"] for e in self.w.ledger() if e["run"] == rec["run_id"]), rec["ledger_usd"])
        self.assertEqual((rec["title"], rec["urls"], rec["notes"]), ("Wpis o czymś", ["https://example.com/blog/wpis"], "uwaga atrapy"))
        self.assertEqual(rec["wynik"][0], "WYNIK: OPUBLIKOWANO")
        self.assertIsNone(rec["skip"])  # przyczyna tylko przy POMINIĘTO
        self.assertEqual(rec["version"], VERSION)
        self.assertEqual(rec["slot"], "2026-10-09T10:00")
        self.assertEqual(rec["precheck"]["exit_code"], 0)
        self.assertEqual(rec["precheck"]["reason"], "precheck: przebieg może ruszyć")  # ostatnia linia stdout
        log = read(rec["log"])
        self.assertIn("autopilot: start", log)
        self.assertIn("ostrzeżenie na stderr", log)
        self.assertIn("WYNIK: OPUBLIKOWANO", log)
        self.assertEqual(self.w.history(), [rec])
        self.assertIsNone(self.w.current())  # znacznik biegu w toku znika z końcem biegu
        self.assertNothingLeft()

    def test_caffeinate_holds_the_run_awake_and_goes_with_it(self):
        self.w.add_job()
        r, rec = self.w.run_job()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.w.recorded("caffeinate.log").splitlines()
        self.assertEqual(len(lines), 1, lines)
        pid, args = lines[0].split(" ", 1)
        self.assertEqual(args, f"-i -s -w {rec['pid']}")  # -s działa tylko na zasilaczu (man caffeinate)
        self.assertFalse(alive(int(pid)))

    def test_missing_result_json_with_exit_1_is_an_error_not_retried(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="1", FAKE_JOB_NO_RESULT="1")
        self.assertEqual(rec["outcome"], "BŁĄD")
        self.assertFalse(rec["retryable"])  # kontrakt: kod 1 ponawiany tylko z retryable: true
        self.assertIn("JSON", rec["reason"])

    def test_wynik_that_contradicts_the_exit_code_loses_to_it_before_the_marker(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="1", FAKE_JOB_RETRYABLE="1", FAKE_JOB_WYNIK="OPUBLIKOWANO")
        self.assertEqual(rec["outcome"], "BŁĄD")
        self.assertTrue(rec["retryable"])
        self.assertTrue(any(f["kind"] == "contradiction" and "OPUBLIKOWANO" in f["detail"] for f in rec["facts"]), rec["facts"])

    def test_after_the_marker_a_result_that_contradicts_the_exit_code_is_urgent(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="0", FAKE_JOB_MARKER="1", FAKE_JOB_OUTCOME="BŁĄD")
        self.assertEqual(rec["outcome"], "PILNE")
        self.assertTrue(rec["final"])
        self.assertFalse(rec["retryable"])

    def test_exit_0_after_the_marker_without_a_final_result_is_urgent(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="0", FAKE_JOB_MARKER="1", FAKE_JOB_NO_RESULT="1")
        self.assertEqual(rec["outcome"], "PILNE")
        self.assertIn("JSON", rec["reason"])

    def test_exit_code_outside_the_contract_is_a_final_error(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="3")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["exit_code"]), ("BŁĄD", False, 3))
        self.assertIn("3", rec["reason"])

    def test_exit_1_after_the_marker_is_never_retried(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="1", FAKE_JOB_MARKER="1", FAKE_JOB_RETRYABLE="1")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"]), ("BŁĄD", False, True))

    def test_exit_0_2_or_5_without_a_marker_is_never_retried_even_with_an_overriding_fact(self):
        # kod 0, 2, 5 znaczy, że efekt już jest (kontrakt, punkt 4): sen przed końcem nie może dać
        # BŁĘDU do ponowienia, bo ponowienie opublikowałoby wpis drugi raz
        self.w.add_job()
        for code in (0, 2, 5):
            for with_result in (True, False):
                with self.subTest(code=code, with_result=with_result):
                    write_json(self.w.clock, {"slept": 0})  # zegar atrapy od zera w każdej próbie
                    r, rec = self.w.run_job(FAKE_JOB_EXIT=str(code), FAKE_JOB_SLEEP_GAP="900",
                                            FAKE_JOB_NO_RESULT=None if with_result else "1")  # fmt: skip
                    self.assertIn("sleep", [f["kind"] for f in rec["facts"]])
                    self.assertEqual((rec["retryable"], rec["final"]), (False, True), rec["reason"])
                    self.assertEqual((rec["phase"], rec["override"]), ("side-effect", None))
                    # bez końcowego JSON-a koniec jest nieczysty: PILNE, nie BŁĄD
                    self.assertEqual(rec["outcome"], CONTRACT[code] if with_result else "PILNE")

    def test_a_published_result_with_a_failing_exit_code_is_urgent_not_retried(self):
        # końcowy JSON z OPUBLIKOWANO potok pisze dopiero po znaczniku: wpis już jest, nawet gdy
        # runner znacznika nie widział, a kod mówi 1 z "retryable": true
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="1", FAKE_JOB_OUTCOME="OPUBLIKOWANO", FAKE_JOB_RETRYABLE="1")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"], rec["phase"]), ("PILNE", False, True, "side-effect"))

    def test_notes_list_and_a_single_url_string_are_normalized(self):
        # Renggli wysyła notes jako string[]; urls jako jeden tekst to jeden adres, nie lista znaków
        self.w.add_job()
        extra = json.dumps({"notes": ["fakt 1", "fakt 2"], "urls": "https://example.com/blog/wpis", "pr": 12})
        r, rec = self.w.run_job(FAKE_JOB_MARKER="1", FAKE_JOB_RESULT_EXTRA=extra)
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        self.assertEqual(rec["notes"], "fakt 1; fakt 2")
        self.assertEqual(rec["urls"], ["https://example.com/blog/wpis"])
        self.assertEqual(rec["pr"], "12")


class LiveCheck(Base):
    """PILNE: runner sam sprawdza adresy z JSON-a wyniku."""

    def setUp(self):
        super().setUp()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/stary":  # jak outofplace.space -> www.outofplace.space
                    self.send_response(308)
                    self.send_header("Location", "/ok")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/ok":
                    body = "<html><title>Wpis o czymś</title><h1>Wpis o czymś</h1></html>".encode()
                    self.send_response(200)
                else:
                    body = b"nie ma"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_exit_5_is_urgent_and_the_runner_checks_each_url_itself(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="5", FAKE_JOB_MARKER="1", FAKE_JOB_TITLE="Wpis o czymś",
                           FAKE_JOB_URLS=f"{self.base}/ok,{self.base}/brak")
        self.assertEqual(rec["outcome"], "PILNE")
        checks = {c["url"]: c for c in rec["live_check"]}
        self.assertEqual(checks[f"{self.base}/ok"]["status"], 200)
        self.assertTrue(checks[f"{self.base}/ok"]["ok"])
        self.assertEqual(checks[f"{self.base}/brak"]["status"], 404)
        self.assertFalse(checks[f"{self.base}/brak"]["ok"])

    def test_a_page_that_moved_with_308_counts_as_live(self):
        # outofplace.space odpowiada 308 na www, a urllib w Pythonie 3.9 sam za 308 nie idzie
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="5", FAKE_JOB_MARKER="1", FAKE_JOB_URLS=f"{self.base}/stary")
        self.assertEqual(rec["outcome"], "PILNE")
        self.assertEqual([(c["url"], c["status"], c["ok"]) for c in rec["live_check"]], [(f"{self.base}/stary", 200, True)])

    def test_side_effect_then_sigkill_is_urgent_never_retried_and_live_checks_the_jobs_urls(self):
        # znacznik z kontraktu nie niesie adresów, a potok zginął przed końcowym JSON-em: zostają
        # live_urls joba (np. indeks bloga). Brak odpowiedzi to co innego niż martwa strona.
        offline = closed_url()
        self.w.add_job()
        self.w.set_job("blog", f"live_urls={self.base}/ok,{self.base}/brak,{offline}")
        r, rec = self.w.run_job(FAKE_JOB_MARKER="1", FAKE_JOB_KILL_SELF="1")
        self.assertEqual(rec["outcome"], "PILNE", rec["reason"])
        self.assertEqual((rec["retryable"], rec["final"], rec["phase"]), (False, True, "side-effect"))
        checks = {c["url"]: c for c in rec["live_check"]}
        self.assertEqual((checks[f"{self.base}/ok"]["status"], checks[f"{self.base}/ok"]["ok"]), (200, True))
        self.assertEqual((checks[f"{self.base}/brak"]["status"], checks[f"{self.base}/brak"]["network"]), (404, False))
        self.assertEqual((checks[offline]["status"], checks[offline]["ok"], checks[offline]["network"]), (None, False, True))
        self.assertIn(f"nie działa: {self.base}/brak (404)", rec["reason"])
        self.assertIn(f"błąd sieci, nie wiadomo, czy działa): {offline}", rec["reason"])
        self.assertNothingLeft()

    def test_runner_error_after_the_marker_is_urgent_and_live_checked(self):
        # wyjątek w runnerze po znaczniku (tu: katalog joba tylko do odczytu, zapis current.json
        # pada): to nieczysty koniec po fazie side-effect, PILNE z własnym sprawdzeniem stron
        self.w.add_job()
        self.w.set_job("blog", f"live_urls={self.base}/ok")
        folder = os.path.join(self.w.jobs_dir, "blog")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, "history.jsonl"), "a").close()  # zapis próby dopisuje do istniejącego pliku
        self.addCleanup(os.chmod, folder, 0o700)
        r, rec = self.w.run_job(FAKE_JOB_LOCK_STATE="1", FAKE_JOB_MARKER="1", FAKE_JOB_SLEEP="30")
        self.assertIsNotNone(rec, r.stderr)
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"], rec["phase"]), ("PILNE", False, True, "side-effect"))
        self.assertIn("błąd runnera", rec["reason"])
        self.assertEqual([(c["url"], c["status"]) for c in rec["live_check"]], [(f"{self.base}/ok", 200)])
        self.assertEqual(r.returncode, 5)
        self.assertNothingLeft()  # finish() zatrzymał potok, który jeszcze biegł
        os.chmod(folder, 0o700)
        self.assertEqual(self.w.jobs("list").returncode, 0)
        self.assertEqual(self.w.history(), [rec])  # current.json, którego runner nie zdołał usunąć, nie daje drugiego zapisu

    def test_side_effect_then_sigkill_without_any_urls_says_there_is_nothing_to_check(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_MARKER="1", FAKE_JOB_KILL_SELF="1")
        self.assertEqual(rec["outcome"], "PILNE")
        self.assertEqual(rec["live_check"], [])
        self.assertIn("adres", rec["reason"])


class Limits(Base):
    """Limity czasu czuwania, sen i nieświeżość."""

    def test_hung_precheck_is_killed_at_its_awake_limit_and_retried(self):
        self.w.add_job(precheck_min="0.05")  # 3 s
        r, rec = self.w.run_job(FAKE_PRECHECK_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["phase"]), ("BŁĄD", True, "precheck"))
        self.assertEqual(rec["stopped"], "time_limit")
        self.assertLess(rec["precheck"]["awake_seconds"], 15)
        self.assertEqual(self.w.started("entry"), [])
        self.assertNothingLeft()

    def test_hung_entry_is_killed_at_its_awake_limit_and_retried_before_the_marker(self):
        self.w.add_job(run_min="0.05")
        r, rec = self.w.run_job(FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["stopped"]), ("BŁĄD", True, "time_limit"))
        self.assertEqual(rec["override"], "time_limit")
        self.assertLess(rec["awake_seconds"], 20)
        self.assertNothingLeft()

    def test_hung_entry_after_the_marker_is_urgent(self):
        self.w.add_job(run_min="0.05")
        r, rec = self.w.run_job(FAKE_JOB_MARKER="1", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["stopped"]), ("PILNE", False, "time_limit"))
        self.assertNothingLeft()

    def test_entry_that_ignores_sigterm_gets_sigkill_after_the_grace(self):
        self.w.add_job(run_min="0.05", grace_s="2")
        r, rec = self.w.run_job(FAKE_JOB_SLEEP="60", FAKE_JOB_IGNORE_TERM="1")
        self.assertEqual(rec["stopped"], "time_limit")
        self.assertLess(rec["awake_seconds"], 25)
        self.assertNothingLeft()

    def test_sleep_gap_overrides_the_exit_code_before_the_marker(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="4", FAKE_JOB_SLEEP_GAP="900")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "sleep"))
        self.assertGreaterEqual(rec["slept_seconds"], 900)
        self.assertFalse(rec["switch_payment"])  # sen nie ma nic wspólnego z płatnikiem

    def test_sleep_gap_after_the_marker_is_only_recorded(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_EXIT="0", FAKE_JOB_MARKER="1", FAKE_JOB_SLEEP_GAP="900")
        self.assertEqual((rec["outcome"], rec["override"]), ("OPUBLIKOWANO", None))
        self.assertIn("sleep", [f["kind"] for f in rec["facts"]])

    def test_run_that_went_stale_through_a_long_sleep_is_stopped_before_the_marker(self):
        self.w.add_job(run_min="5")
        r, rec = self.w.run_job(FAKE_JOB_SLEEP_GAP=str(13 * 3600), FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["stopped"]), ("BŁĄD", True, "stale"))
        self.assertLess(rec["awake_seconds"], 25)
        self.assertNothingLeft()


class Facts(Base):
    """Fakty, które runner widzi sam: licznik, płatnik, budżet, hamulec, sygnały."""

    def test_billing_error_overrides_and_asks_for_another_payment_source(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_CLAUDE="1", FAKE_CLAUDE_ERROR="billing", FAKE_JOB_EXIT="4")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "billing"))
        self.assertTrue(rec["switch_payment"])

    def test_auth_and_rate_limit_errors_override_and_ask_for_another_payment_source(self):
        self.w.add_job()
        for error, kind in (("401", "auth"), ("429", "limit")):
            with self.subTest(error=error):
                r, rec = self.w.run_job(FAKE_JOB_CLAUDE="1", FAKE_CLAUDE_ERROR=error, FAKE_JOB_EXIT="4")
                self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, kind))
                self.assertTrue(rec["switch_payment"])

    def test_payer_mismatch_kills_the_run_and_is_final_with_a_banner(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_CLAUDE="1", FAKE_CLAUDE_FORCE_EMAIL="obcy@example.com", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"]), ("BŁĄD", False, True))
        self.assertEqual(rec["stopped"], "payer")
        self.assertTrue(rec["banner"])
        self.assertLess(rec["awake_seconds"], 25)
        self.assertNothingLeft()

    def test_claude_outside_the_run_dir_is_a_leak_that_kills_the_run_and_is_final(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_LEAK="1", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"]), ("BŁĄD", False, True), rec["reason"])
        # strażnik wycieku albo licznik z obcym płatnikiem: który zobaczy pierwszy, ten zatrzymuje
        self.assertIn(rec["stopped"], ("leak", "payer"))
        self.assertTrue(rec["banner"])
        self.assertLess(rec["awake_seconds"], 25)
        self.assertNothingLeft()

    def test_budget_stop_at_one_and_a_half_times_the_budget_is_final(self):
        self.w.add_job(budget="0.5")
        r, rec = self.w.run_job(FAKE_JOB_CLAUDE="1", FAKE_CLAUDE_COST="1.0", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"], rec["stopped"]), ("BŁĄD", False, True, "budget"))
        self.assertGreaterEqual(rec["cost_usd"], 0.75)
        self.assertTrue(rec["banner"])
        self.assertNothingLeft()

    def test_budget_stop_after_the_marker_is_urgent(self):
        self.w.add_job(budget="0.5")
        # znacznik przed drogim zapytaniem: inaczej stop przychodzi przed fazą side-effect (BŁĄD)
        r, rec = self.w.run_job(FAKE_JOB_EARLY_MARKER="1", FAKE_JOB_CLAUDE="1", FAKE_CLAUDE_COST="1.0", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["stopped"], rec["phase"]), ("PILNE", "budget", "side-effect"), rec["reason"])
        self.assertTrue(rec["banner"])

    def test_budget_stop_fires_at_exactly_one_and_a_half_times_the_budget(self):
        # kontrakt, punkt 7: stop "at 1.5x the budget", czyli już przy równości
        self.w.add_job(budget="0.5")
        r, rec = self.w.run_job(FAKE_JOB_CLAUDE="1", FAKE_CLAUDE_COST="0.75", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["final"], rec["stopped"]), ("BŁĄD", True, "budget"), rec["reason"])
        self.assertAlmostEqual(rec["cost_usd"], 0.75)
        self.assertNothingLeft()

    def test_memory_brake_kill_before_the_marker_is_retried(self):
        # jak naprawdę: hamulec zabija, potok kończy się kodem 1 od razu, a strażnik zapisuje
        # zdarzenie sekundę później, na końcu swojego tiku
        self.w.add_job()
        self.w.devguard(delay=1.0)
        r, rec = self.w.run_job(FAKE_JOB_BRAKE="1", FAKE_JOB_EXIT="1")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "brake"), rec["reason"])
        self.assertIn("hamulec", rec["reason"])

    def test_entry_killed_by_a_signal_from_outside_is_retried_before_the_marker(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_KILL_SELF="1")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "signal"))
        self.assertEqual(rec["exit_code"], 137)

    def test_sigterm_to_the_runner_stops_the_run_and_records_it(self):
        self.w.add_job()
        proc = self.w.start(FAKE_JOB_SLEEP="60")
        self.w.wait_phase("run")
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        rec = json.loads(out)
        self.assertEqual((rec["outcome"], rec["retryable"], rec["stopped"]), ("BŁĄD", True, "signal"))
        self.assertEqual(self.w.history(), [rec])
        self.assertNothingLeft()

    def test_ctrl_c_on_a_manual_run_is_not_retried(self):
        self.w.add_job()
        proc = self.w.start(FAKE_JOB_SLEEP="60")
        self.w.wait_phase("run")
        time.sleep(1)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        rec = json.loads(out)
        self.assertEqual((rec["outcome"], rec["retryable"], rec["stopped"]), ("BŁĄD", False, "signal"))
        self.assertNothingLeft()


class Precheck(Base):
    def test_precheck_exit_4_is_no_post_with_its_last_stdout_line_and_no_entry(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_PRECHECK_EXIT="4", FAKE_PRECHECK_REASON="precheck: dziś już opublikowane")
        self.assertEqual((rec["outcome"], rec["final"], rec["phase"]), ("BEZ WPISU", True, "precheck"))
        self.assertEqual(rec["reason"], "precheck: dziś już opublikowane")
        self.assertEqual(self.w.started("entry"), [])
        self.assertEqual(r.returncode, 4)

    def test_precheck_failure_is_a_retryable_error_with_its_reason(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_PRECHECK_EXIT="1", FAKE_PRECHECK_REASON="precheck: git fetch nie przeszedł")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["phase"]), ("BŁĄD", True, "precheck"))
        self.assertIn("git fetch nie przeszedł", rec["reason"])
        self.assertEqual(self.w.started("entry"), [])


class Refusals(Base):
    """Co nie może wystartować: drugi bieg joba, drugi ciężki bieg, brak płatnika, wstrzymanie."""

    def test_second_run_of_the_same_job_is_refused_and_writes_nothing(self):
        self.w.add_job()
        first = self.w.start(FAKE_JOB_SLEEP="5")
        self.w.wait_phase("run")
        r, rec = self.w.run_job()
        self.assertEqual(r.returncode, 73, r.stderr)
        self.assertIn("już biegnie", r.stderr)
        self.assertIsNone(rec)
        out, _ = first.communicate(timeout=60)
        self.assertEqual(json.loads(out)["outcome"], "OPUBLIKOWANO")
        self.assertEqual(len(self.w.history()), 1)

    def test_heavy_run_is_skipped_while_another_heavy_run_goes_and_a_light_one_runs(self):
        self.w.add_job("a")
        self.w.add_job("b")
        self.w.add_job("c", light=True)
        first = self.w.start("a", FAKE_JOB_SLEEP="6")
        self.w.wait_phase("run", "a")
        r, rec = self.w.run_job("b")
        self.assertEqual(r.returncode, 75)
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"], rec["phase"]), ("POMINIĘTO", True, False, "start"))
        self.assertEqual(rec["skip"], "heavy")
        self.assertIn("a", rec["reason"])
        self.assertIsNone(rec["payer"])  # nic nie wystartowało, nikt nie płacił
        r, rec = self.w.run_job("c")
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        first.communicate(timeout=60)

    def test_no_payer_is_skipped_and_nothing_starts(self):
        self.w.add_job()
        self.assertEqual(self.w.run("balance", PAYER, "--remaining-usd", "1").returncode, 0)
        self.w.subscriptions(accounts(), fail=True)
        r, rec = self.w.run_job()
        self.assertEqual(r.returncode, 75)
        self.assertEqual((rec["outcome"], rec["retryable"], rec["payer"], rec["skip"]), ("POMINIĘTO", True, None, "payer"))
        self.assertEqual(self.w.started("precheck"), [])

    def test_project_settings_with_their_own_login_are_an_error_before_anything_starts(self):
        # ustawienia projektu stoją wyżej niż katalog biegu: runenv odmawia (kind "config")
        self.w.add_job()
        os.makedirs(os.path.join(self.w.cwd, ".claude"))
        write_json(os.path.join(self.w.cwd, ".claude", "settings.local.json"), {"apiKeyHelper": "/usr/local/bin/other"})
        r, rec = self.w.run_job()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual((rec["outcome"], rec["phase"], rec["payer"], rec["skip"]), ("BŁĄD", "start", None, None))
        self.assertIn("apiKeyHelper", rec["reason"])
        self.assertEqual(self.w.started("precheck"), [])

    def test_hold_skips_the_run_until_released(self):
        self.w.add_job()
        self.assertEqual(self.w.jobs("hold", "--hours", "1", "--reason", "build iOS").returncode, 0)
        r, rec = self.w.run_job()
        self.assertEqual((rec["outcome"], rec["retryable"], rec["skip"]), ("POMINIĘTO", True, "hold"))
        self.assertIn("build iOS", rec["reason"])
        self.assertEqual(self.w.jobs("release").returncode, 0)
        r, rec = self.w.run_job()
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO")

    def test_disabled_and_unknown_jobs_are_refused_without_a_record(self):
        self.w.add_job(disabled=True)
        r = self.w.jobs("run", "blog")
        self.assertEqual(r.returncode, 64)
        self.assertIn("wyłączony", r.stderr)
        r = self.w.jobs("run", "nie-ma")
        self.assertEqual(r.returncode, 64)
        self.assertEqual(self.w.history(), [])

    def test_missing_pinned_version_runs_the_canary_then_the_job(self):
        self.w.add_job()
        write_json(os.path.join(self.w.runenv, "pin.json"), {"version": "2.1.100"})  # zniknęła po `claude update`
        r, rec = self.w.run_job()
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        self.assertEqual(json.loads(read(os.path.join(self.w.runenv, "pin.json")))["version"], VERSION)
        self.assertIn("version", [f["kind"] for f in rec["facts"]])

    def test_failed_canary_skips_the_run(self):
        self.w.add_job()
        write_json(os.path.join(self.w.runenv, "pin.json"), {"version": "2.1.100"})
        r, rec = self.w.run_job(FAKE_CLAUDE_NO_OTEL="1")  # canary bez licznika nie przypnie wersji
        self.assertEqual((rec["outcome"], rec["retryable"], rec["skip"]), ("POMINIĘTO", True, "version"))
        self.assertIn("canary", rec["reason"])
        self.assertEqual(self.w.started("precheck"), [])


class Memory(Base):
    """Bramka pamięci przed biegiem i ciężkie kroki przez scheduler, bez zakleszczenia."""

    def test_run_is_skipped_when_memory_never_admits_its_footprint(self):
        self.w.add_job(admit_min="0.05", footprint_gb="2")
        self.w.tight()
        r, rec = self.w.run_job()
        self.assertEqual((rec["outcome"], rec["retryable"], rec["skip"]), ("POMINIĘTO", True, "memory"))
        self.assertIn("pamię", rec["reason"])
        self.assertIsNone(rec["payer"])
        self.assertEqual(self.w.started("precheck"), [])

    def test_sigterm_while_waiting_for_memory_is_skipped_as_an_interrupt(self):
        self.w.add_job(admit_min="2", footprint_gb="2")
        self.w.tight()
        proc = self.w.start()
        cur = self.w.wait_phase("start")
        deadline = time.time() + 30
        while "czekam na pamięć" not in read(cur["log"]) and time.time() < deadline:
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        rec = json.loads(out)
        self.assertEqual((rec["outcome"], rec["retryable"], rec["skip"]), ("POMINIĘTO", True, "interrupt"), rec["reason"])
        self.assertEqual(proc.returncode, 75)

    def test_precheck_limit_counts_from_its_own_start_not_from_the_wait_for_memory(self):
        self.w.add_job(admit_min="1", precheck_min="0.05", footprint_gb="2")  # precheck: 3 s
        self.w.tight()
        threading.Timer(4, self.w.roomy).start()
        r, rec = self.w.run_job(FAKE_PRECHECK_SLEEP="1.5")
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        self.assertGreaterEqual(rec["awake_seconds"], 4)
        self.assertLess(rec["precheck"]["awake_seconds"], 3)

    def test_heavy_step_is_admitted_by_the_memory_scheduler_while_the_run_goes(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_HEAVY="1")
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        self.assertEqual(read(os.path.join(self.w.fake, "built")).strip(), "built")
        steps = [h for h in self.w.sched_history() if h.get("agent") == "jobs-blog"]
        self.assertEqual([(s["where"], s["rc"]) for s in steps], [("local", 0)])

    def test_time_a_precheck_step_waits_in_the_memory_queue_does_not_count_toward_its_limit(self):
        # Renggli 09.10: precheck czekał 5 min 20 s w kolejce schedulera za cudzym `go test`
        self.w.add_job(precheck_min="0.1", footprint_gb="0")  # precheck: 6 s
        self.w.tight()
        threading.Timer(9, self.w.roomy).start()
        r, rec = self.w.run_job(FAKE_PRECHECK_HEAVY="1")
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        self.assertEqual(rec["precheck"]["exit_code"], 0)
        steps = [h for h in self.w.sched_history() if h.get("agent") == "jobs-blog"]
        self.assertGreaterEqual(steps[0]["wait_s"], 7)

    def test_time_a_heavy_step_waits_in_the_memory_queue_does_not_count_toward_the_limit(self):
        self.w.add_job(run_min="0.1", footprint_gb="0")  # bieg: 6 s; bramka pamięci tylko na hamulec
        self.w.tight()
        threading.Timer(9, self.w.roomy).start()
        r, rec = self.w.run_job(FAKE_JOB_HEAVY="1")
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        self.assertIsNone(rec["stopped"])
        steps = [h for h in self.w.sched_history() if h.get("agent") == "jobs-blog"]
        self.assertGreaterEqual(steps[0]["wait_s"], 7)


class Crash(Base):
    """Runner zabity w dowolnej chwili zostawia zapis, nie dziurę."""

    def kill_runner(self, phase, **env):
        self.w.add_job()
        proc = self.w.start(**env)
        self.w.wait_phase(phase)
        time.sleep(1)
        killed_at = time.time()
        proc.kill()
        proc.communicate(timeout=30)
        time.sleep(1)
        r = self.w.jobs("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.w.current())
        rec = self.w.history()[-1]
        # koniec to ostatni znak życia runnera, nie chwila, w której ktoś zajrzał do listy
        self.assertLessEqual(rec["ended_at"], killed_at)
        return rec

    def test_runner_killed_before_the_marker_leaves_a_retryable_error(self):
        rec = self.kill_runner("run", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "runner"))
        self.assertIsNotNone(rec["run_id"])
        self.assertNothingLeft()  # sweep A1 zabił osierocony potok

    def test_runner_killed_between_its_record_and_its_cleanup_is_not_recorded_twice(self):
        self.w.add_job()
        r, rec = self.w.run_job()
        # zapis już w historii, current.json jeszcze nie usunięty
        write_json(os.path.join(self.w.jobs_dir, "blog", "current.json"), dict(rec, state="running"))
        self.assertEqual(self.w.jobs("list").returncode, 0)
        self.assertEqual(self.w.history(), [rec])
        self.assertIsNone(self.w.current())

    def test_runner_killed_after_the_marker_leaves_urgent(self):
        rec = self.kill_runner("side-effect", FAKE_JOB_MARKER="1", FAKE_JOB_SLEEP="60")
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"]), ("PILNE", False, True))
        self.assertNothingLeft()

    def test_runner_killed_after_a_publishing_exit_before_its_record_is_never_retried(self):
        # przegląd A2a, exp_kill: kod 0 bez znacznika, który runner widział, i maruder głuchy na
        # SIGTERM, więc runenv.finish() czeka; runner zabity w tej chwili nie może zostawić BŁĘDU
        # do ponowienia, bo ponowienie opublikuje wpis drugi raz
        self.w.add_job()
        proc = self.w.start(FAKE_JOB_NO_RESULT="1", FAKE_JOB_STRAGGLER="1")
        cur = self.w.wait_for(lambda c: c.get("exit_code") == 0, what="zapisu kodu 0")
        self.assertEqual(cur["phase"], "side-effect")  # na dysku przed rozliczeniem
        proc.kill()
        proc.communicate(timeout=30)
        self.assertEqual(self.w.history(), [])  # runner zginął, zanim zapisał próbę
        self.assertEqual(self.w.jobs("list").returncode, 0)
        rec = self.w.history()[-1]
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"], rec["phase"]), ("PILNE", False, True, "side-effect"))
        self.assertEqual(rec["exit_code"], 0)
        self.assertNothingLeft()

    def test_runner_killed_while_the_pipeline_lingers_after_a_published_result_is_never_retried(self):
        # końcowy JSON bez "phase" (jak u Renggli) nadpisał znacznik, a potok jeszcze żyje
        self.w.add_job()
        proc = self.w.start(FAKE_JOB_LINGER="60")
        # pod obciążeniem runner bywa już w fazie side-effect, zanim test zajrzy do current.json
        cur = self.w.wait_for(lambda c: c.get("phase") in ("run", "side-effect"), what="fazy run")
        deadline = time.time() + 30
        while (read_json_file(cur["result_path"]) or {}).get("outcome") != "OPUBLIKOWANO" and time.time() < deadline:
            time.sleep(0.05)
        proc.kill()
        proc.communicate(timeout=30)
        self.assertEqual(self.w.jobs("list").returncode, 0)
        rec = self.w.history()[-1]
        self.assertEqual((rec["outcome"], rec["retryable"], rec["final"], rec["phase"]), ("PILNE", False, True, "side-effect"))
        self.assertNothingLeft()

    def test_recovery_reads_the_side_effect_from_the_exit_code_or_the_published_result(self):
        # current.json zapisany w fazie run (runner nie widział znacznika): o fazie side-effect mówi
        # kod 0, 2, 5 albo końcowy JSON z publikacją; bez nich BŁĄD do ponowienia
        self.w.add_job()
        folder = os.path.join(self.w.jobs_dir, "blog")
        os.makedirs(folder, exist_ok=True)
        result = os.path.join(folder, "wynik.json")
        rows = [  # (kod w current.json, JSON wyniku, oczekiwany wynik)
            (0, None, "PILNE"), (2, None, "PILNE"), (5, None, "PILNE"),
            (None, {"outcome": "OPUBLIKOWANO", "reason": "jest"}, "PILNE"),
            (None, {"outcome": "ZAPARKOWANO", "reason": "PR"}, "PILNE"),
            (None, {"phase": "side-effect"}, "PILNE"),
            (None, {"outcome": "BŁĄD", "reason": "x", "retryable": True}, "BŁĄD"),
            (None, None, "BŁĄD"),
        ]  # fmt: skip
        for i, (code, data, outcome) in enumerate(rows):
            with self.subTest(code=code, result=data):
                if data is None:
                    if os.path.exists(result):
                        os.unlink(result)
                else:
                    write_json(result, data)
                cur = {"v": 1, "id": f"crafted{i}", "job": "blog", "state": "running", "phase": "run", "exit_code": code,
                       "pid": dead_pid(), "run_id": None, "started_at": time.time(), "budget_usd": 2.0, "facts": [],
                       "result_path": result, "log": os.path.join(folder, "x.log")}  # fmt: skip
                write_json(os.path.join(folder, "current.json"), cur)
                self.assertEqual(self.w.jobs("list").returncode, 0)
                rec = self.w.history()[-1]
                self.assertEqual(rec["id"], f"crafted{i}")
                self.assertEqual((rec["outcome"], rec["retryable"]), (outcome, outcome == "BŁĄD"))

    def test_recovery_waits_until_runenv_has_settled_the_run(self):
        # sweep w innym procesie (tik) trzyma blokadę: rachunku jeszcze nie ma, więc `jobs list`
        # nie zapisuje próby bez kosztu i płatnika, tylko czeka do następnego razu
        self.w.add_job()
        proc = self.w.start(FAKE_JOB_CLAUDE="1", FAKE_JOB_SLEEP="60")
        self.w.wait_phase("run")
        deadline = time.time() + 30
        while len(self.w.pids()) < 3 and time.time() < deadline:  # precheck, wejście, potem `sleep` po `claude`
            time.sleep(0.05)
        proc.kill()
        proc.communicate(timeout=30)
        fd = os.open(os.path.join(self.w.runenv, ".sweep.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.w.jobs("list").returncode, 0)
            self.assertEqual(self.w.history(), [])
            self.assertEqual(self.w.current()["state"], "running")
        finally:
            os.close(fd)
        self.assertEqual(self.w.jobs("list").returncode, 0)
        [rec] = self.w.history()
        self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "runner"))
        self.assertAlmostEqual(rec["cost_usd"], 0.001)  # z rachunku runenv
        self.assertIn(rec["payer"]["verdict"], ("ok", "unverified"))
        self.assertIsNotNone(rec["metering_complete"])
        self.assertIsNone(self.w.current())
        self.assertNothingLeft()

    def test_concurrent_recovery_does_not_crash_when_current_json_vanishes(self):
        # dwa `jobs list` naraz: drugi widzi current.json, a zanim go przeczyta, pierwszy już go
        # zapisał i usunął. Wywołanie w procesie testu, bo to wyścig między dwoma odczytami.
        import importlib
        import sys
        from unittest import mock

        sys.path.insert(0, ROOT)
        self.addCleanup(sys.path.remove, ROOT)
        jobs = importlib.import_module("jobs")
        root = os.path.join(self.w.home, "jobs-unit")
        os.makedirs(os.path.join(root, "blog"))
        write_json(os.path.join(root, "blog", "current.json"), {"state": "running", "id": "x"})

        def vanish(path, default=None):
            os.unlink(path)  # inny proces zrobił swoje między isfile() a odczytem
            return default

        def no_sweep(*args, **kwargs):
            raise AssertionError("bez current.json nie ma czego rozliczać")

        with mock.patch.object(jobs, "JOBS_DIR", root), mock.patch.object(jobs, "read_json", vanish), \
                mock.patch.object(jobs.runenv, "sweep", no_sweep):  # fmt: skip
            self.assertEqual(jobs.recover(), [])


class Logs(Base):
    def test_skipped_attempts_logs_go_first_so_the_last_real_runs_log_survives(self):
        # 60 logów na job: seria POMINIĘTO (np. noc z wstrzymaniem) nie może zjeść logu ostatniego
        # prawdziwego biegu, który Filip otworzy rano
        self.w.add_job()
        logs = os.path.join(self.w.jobs_dir, "blog", "logs")
        os.makedirs(logs)
        now = time.time()
        real = os.path.join(logs, "real.log")
        rows = [{"id": "real", "outcome": "BŁĄD", "log": real}]
        for i in range(61):
            path = real if i == 0 else os.path.join(logs, f"skip-{i:02d}.log")
            with open(path, "w") as f:
                f.write("x\n")
            os.utime(path, (now - 10000 + i, now - 10000 + i))
            if i:
                rows.append({"id": f"skip{i}", "outcome": "POMINIĘTO", "log": path})
        with open(os.path.join(self.w.jobs_dir, "blog", "history.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(self.w.jobs("hold", "--hours", "1").returncode, 0)
        r, rec = self.w.run_job()
        self.assertEqual(rec["outcome"], "POMINIĘTO")
        self.assertTrue(os.path.exists(real), "log ostatniego prawdziwego biegu zniknął")
        self.assertFalse(os.path.exists(os.path.join(logs, "skip-01.log")))  # najstarsze pominięcie idzie pierwsze
        self.assertEqual(len(os.listdir(logs)), 61)  # 60 zostało i log tej próby


class Environment(Base):
    def test_launchd_like_environment_resolves_the_tools_and_the_runs_claude(self):
        homebrew = {t: f"/opt/homebrew/bin/{t}" for t in ("pnpm", "node", "vercel")}
        if not all(os.access(p, os.X_OK) for p in homebrew.values()):
            self.skipTest("na tym Macu nie ma pnpm, node albo vercel w /opt/homebrew/bin")
        gh = os.path.join(self.w.home, ".local/bin/gh")
        with open(gh, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(gh, 0o755)
        self.w.add_job()
        dump = os.path.join(self.w.fake, "tools.json")
        # jak launchd: bez CLAUDE_ACC_TOOL_PATH, PATH runnera bez Homebrew i ~/.local/bin (atrapy
        # zostają tylko dla `security` i `claude-acc`, które woła sam runenv)
        env = {"CLAUDE_ACC_TOOL_PATH": None, "PATH": f"{FAKES_RUNENV}:{FAKES_CREDITS}:{FAKES}:/usr/bin:/bin:/usr/sbin:/sbin"}
        r, rec = self.w.run_job(FAKE_JOB_TOOLS=dump, **env)
        self.assertEqual(rec["outcome"], "OPUBLIKOWANO", rec["reason"])
        seen = json.loads(read(dump))
        for tool, path in homebrew.items():
            self.assertEqual(seen["tools"][tool], path)
        self.assertEqual(seen["tools"]["gh"], gh)
        runs = os.path.join(self.w.runenv, "runs")
        self.assertTrue(seen["tools"]["claude"].startswith(runs + os.sep), seen["tools"]["claude"])
        self.assertTrue(seen["tools"]["claude"].endswith("/bin/claude"))
        job_env = seen["env"]
        self.assertTrue(job_env["CLAUDE_ACC_JOB_RESULT"].startswith(self.w.jobs_dir))
        self.assertEqual(job_env["CLAUDE_ACC_JOB_BUDGET_USD"], "2")
        self.assertIn("sched run --via plock", job_env["CLAUDE_ACC_JOB_HEAVY"])
        self.assertEqual(job_env["CLAUDE_ACC_JOB_AWAKE_MINUTES"], "1")


class Cli(Base):
    def test_list_and_log_show_the_last_run(self):
        self.w.add_job()
        r, rec = self.w.run_job(FAKE_JOB_MARKER="1", FAKE_JOB_CLAUDE="1", FAKE_JOB_URLS="https://example.com/blog/wpis")
        out = self.w.jobs("list").stdout
        self.assertIn("blog", out)
        self.assertIn("OPUBLIKOWANO", out)
        self.assertIn("https://example.com/blog/wpis", out)
        self.assertIn(PAYER, out)
        data = json.loads(self.w.jobs("list", "--json").stdout)
        self.assertEqual(data["jobs"][0]["last"], rec)
        out = self.w.jobs("log", "blog").stdout
        self.assertIn("atrapa: OPUBLIKOWANO", out)
        self.assertIn("WYNIK: OPUBLIKOWANO", out)  # ogon logu
        self.assertIn(rec["log"], out)
        data = json.loads(self.w.jobs("log", "blog", "--json").stdout)
        self.assertEqual(data["runs"], [rec])

    def test_list_shows_a_running_job(self):
        self.w.add_job()
        proc = self.w.start(FAKE_JOB_SLEEP="4")
        self.w.wait_phase("run")
        out = self.w.jobs("list").stdout
        self.assertIn("biegnie", out)
        proc.communicate(timeout=60)

    def test_add_validates_the_definition(self):
        cases = [
            (["add", "blog", "--cwd", "/nie/ma/takiego", "--entry", "x", "--budget-usd", "1"], "katalog"),
            (["add", "blog", "--cwd", self.w.cwd, "--entry", "x", "--budget-usd", "0"], "budget"),
            (["add", "Zła Nazwa", "--cwd", self.w.cwd, "--entry", "x", "--budget-usd", "1"], "nazwa"),
            (["add", "blog", "--cwd", self.w.cwd, "--entry", "x", "--budget-usd", "1", "--mode", "subscription",
              "--run-min", "500"], "subskrypc"),
        ]  # fmt: skip
        for args, word in cases:
            with self.subTest(args=args):
                r = self.w.jobs(*args)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertIn(word, r.stderr.lower())
        self.assertFalse(os.path.exists(os.path.join(self.w.jobs_dir, "jobs.json")))

    def test_set_changes_one_field_and_keeps_the_rest(self):
        self.w.add_job()
        # harmonogram należy do etapu A2b i jest sprawdzany (tests/test_jobs_schedule.py)
        sched = {"every_days": 2, "anchor": "2026-10-10", "at": ["05:30"]}
        r = self.w.jobs("set", "blog", "budget_usd=3.5", "enabled=false", f"schedule={json.dumps(sched)}")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(self.w.jobs("list", "--json").stdout)["jobs"][0]
        core = {k: v for k, v in data["schedule"].items() if k not in ("since", "edited")}
        self.assertEqual((data["budget_usd"], data["enabled"], core), (3.5, False, sched))
        self.assertEqual(data["entry"], f"{PY} {PIPELINE} entry")
        self.assertEqual(self.w.jobs("set", "blog", "budget_usd=-1").returncode, 64)
        self.assertEqual(data["live_urls"], [])
        self.w.set_job("blog", 'live_urls=["https://example.com/blog"]')
        self.assertEqual(json.loads(self.w.jobs("list", "--json").stdout)["jobs"][0]["live_urls"], ["https://example.com/blog"])
        for bad in ("live_urls=ftp://example.com", "live_urls=example.com/blog", 'live_urls=["https://a", 3]'):
            with self.subTest(bad=bad):
                r = self.w.jobs("set", "blog", bad)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertIn("live_urls", r.stderr)

    def test_acc_py_routes_jobs(self):
        self.w.add_job()
        r = subprocess.run([PY, os.path.join(ROOT, "acc.py"), "jobs", "list"], env=self.w.env(), capture_output=True,
                           text=True, timeout=60)  # fmt: skip
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("blog", r.stdout)


if __name__ == "__main__":
    unittest.main()
