#!/usr/bin/env python3
"""Joby bez człowieka: `claude-acc jobs run <job>` uruchamia jeden bieg bloga od początku do
końca i zostawia zapis, z którego czytają tik harmonogramu, panel i Filip rano.

Potok bloga to jedna komenda repo (np. `pnpm autopilot`) według kontraktu v2
(~/.claude/plans/credit-blogs-2026-10-09/CONTRACT-blog-pipeline.md): precheck, komenda wejścia,
kody wyjścia, JSON wyniku w $CLAUDE_ACC_JOB_RESULT ze znacznikiem {"phase":"side-effect"} przed
pierwszym pushem, blok WYNIK na końcu stdout. Jedna próba (`jobs run`) to:

1. definicja z jobs.json; job wyłączony albo nieznany to odmowa bez zapisu (kod 64);
2. runenv.sweep() i zapis biegów, których runner zginął (recover); blokada joba: drugi bieg tego
   samego joba to odmowa bez zapisu (kod 73, zapisuje go ten, który biegnie); potem caffeinate
   -i -s -w <pid runnera> (-s liczy się tylko na zasilaczu, więc zawsze oba);
3. jeden ciężki bieg naraz na wszystkie joby i `jobs hold`: inaczej POMINIĘTO (skip heavy, hold);
4. bramka pamięci: scheduler pamięci (`sched status --json`) bez hamulca i bez krytycznej presji,
   z footprint_gb do wpuszczenia; czeka najwyżej admit_min minut czuwania, potem POMINIĘTO (memory).
   Bieg nie jest jobem schedulera (hamulec gasi joby schedulera, safety() je zatrzymuje, a `sched
   run` w środku wpuszczonego joba nie czeka drugi raz, więc build potoku nie byłby wpuszczony).
   Ciężkie kroki, które potok stawia sam, idą przez $CLAUDE_ACC_JOB_HEAVY (`sched run --via plock`):
   każdy osobno, z limitem kolejki 30 min, a czas w kolejce nie liczy się do limitu biegu;
5. runenv.prepare(): płatnik na cały bieg (kredyt z puli, inaczej subskrypcja, nigdy Blazity),
   licznik, katalog biegu, PATH jak updates.tool_path() z przypiętym `claude` biegu na początku.
   Odmowa "payer" to POMINIĘTO (payer); "version" to canary (ok. 0,002 USD) i drugie podejście, a
   gdy canary nie przejdzie, POMINIĘTO (version); "config" to BŁĄD do ponowienia;
6. precheck, potem komenda wejścia, w środowisku biegu, każda we własnej grupie procesów, wyjście
   do logu próby; limit w minutach czuwania (mach_absolute_time stoi w czasie snu), liczony od
   startu komendy, bez czasu w kolejce schedulera; stop to SIGTERM do grupy, po grace_s SIGKILL;
   koniec komendy wejścia (kod, faza) trafia do current.json od razu, przed rozliczeniem;
7. runenv.finish(), wynik według tabeli niżej, sprawdzenie stron na żywo przy PILNE, zapis.

W trakcie runner co pół sekundy patrzy na: alarmy licznika (obcy płatnik albo wyciek: stop i BŁĄD
ostateczny; kredyt wyczerpany: stop; 401 i 429 to fakty), koszt (1,5 × budżet: stop ostateczny),
limit czuwania, sen (zegar ścienny skacze, czuwanie nie), nieświeżość (12 h zegara ściennego przed
fazą side-effect), znacznik side-effect w pliku wyniku, kolejkę schedulera, drzewo procesów (dla
hamulca pamięci, który zapisuje swoje ofiary w devguard-state.json). Hamulec najpierw zabija, a
zdarzenie zapisuje na końcu swojego tiku, już po wyjściu komendy: przy niezerowym końcu przed fazą
side-effect, którego runner nie zrobił sam, runner czeka na pierwszą zmianę devguard-state.json,
najwyżej 12 s (8 s łaski hamulca i zapas); bez tego pliku strażnik nigdy nie działał i nie czeka.

Wynik próby (kontrakt v2, punkty 3 do 5; fakty tylko z fazy, która skończyła próbę):
  przed fazą side-effect:
    stop przy 1,5 × budżet, obcy płatnik, wyciek    BŁĄD ostateczny z banerem
    Ctrl-C (SIGINT) na biegu ręcznym                 BŁĄD bez ponawiania
    fakt: kredyt wyczerpany (billing), 401 (auth), 429 (limit), hamulec pamięci (brake), sygnał
      spoza runnera (signal), sen (sleep), nieświeżość (stale), limit czasu (time_limit)
                                                     BŁĄD do ponowienia, override = ten fakt;
                                                     switch_payment przy billing, auth, limit
    precheck: 4                                      BEZ WPISU (powód: ostatnia linia stdout)
    precheck: inny kod                               BŁĄD do ponowienia
    kod 4                                            BEZ WPISU
    kod 1                                            BŁĄD, ponawiany tylko z "retryable": true
    inny kod                                         BŁĄD ostateczny (spoza kontraktu)
    błąd runnera (wyjątek)                           BŁĄD do ponowienia
  po fazie side-effect (znacznik w pliku wyniku, końcowy JSON z wynikiem OPUBLIKOWANO, ZAPARKOWANO
  albo PILNE, który potok pisze dopiero po znaczniku, albo kod 0, 2, 5, które same znaczą efekt):
    czysty koniec (kod z tabeli, bez stopu runnera, JSON wyniku z tym samym wynikiem, WYNIK
    się nie kłóci)                                   wynik z tabeli, nigdy do ponowienia
    każdy inny koniec, także błąd runnera            PILNE; runner sam sprawdza adresy z JSON-a,
                                                     a bez nich live_urls joba
  runner zabity (SIGKILL, hamulec, launchd): następna komenda `jobs` zamienia current.json w zapis,
    gdy runenv rozliczył już bieg (run.json zniknął); przed fazą side-effect BŁĄD do ponowienia
    (override runner), po niej PILNE ze sprawdzeniem stron
  przed komendą: brak płatnika, wstrzymanie, inny ciężki bieg, brak pamięci, wersja po canary,
    sygnał dla runnera                               POMINIĘTO (A2b ponawia; przyczyna w skip)

Pliki (~/.local/share/claude-acc/jobs/):
  jobs.json                  definicje: {"jobs": {nazwa: definicja}}
  hold.json                  `jobs hold`: {"until", "reason", "at"}
  heavy.lock, heavy.json     jeden ciężki bieg naraz (flock) i kto go trzyma
  <job>/lock                 jeden bieg joba (flock; jądro zwalnia go ze śmiercią runnera)
  <job>/current.json         zapis próby w toku (state "running"), co zmianę fazy i co 10 s
  <job>/history.jsonl        zapisy skończonych prób, najstarszy pierwszy
  <job>/logs/<czas>-<id>.log log próby: linie runnera ([jobs ...]), stdout i stderr komend
  <job>/results/<id>.json    $CLAUDE_ACC_JOB_RESULT tej próby

Definicja joba (jobs add / jobs set; walidacja przy każdej zmianie):
  cwd           katalog biegu (worktree runnera), ścieżka bezwzględna
  entry         komenda wejścia (/bin/sh -c)
  precheck      komenda prechecku albo null
  budget_usd    budżet próby; stop przy 1,5 ×
  mode          auto | credits | subscription (runenv.prepare)
  heavy         true: jeden ciężki bieg naraz na wszystkie joby
  enabled       false: `jobs run` odmawia, tik go pomija
  footprint_gb  ile pamięci bieg musi mieć do wpuszczenia (0: tylko hamulec i presja)
  limits        {precheck_min, run_min, admit_min: minuty czuwania; grace_s: SIGTERM -> SIGKILL}
  live_urls     [] albo do 10 adresów http(s) (np. indeks bloga): runner sprawdza je przy PILNE, gdy
                JSON wyniku nie ma urls (znacznik z kontraktu nie niesie adresów)
  schedule      dowolny JSON, należy do harmonogramu (A2b); jobs.py go nie czyta

Zapis próby (schemat v1, zamrożony; history.jsonl i current.json):
  v               1
  id              id próby (12 znaków hex; też w nazwie logu)
  job             nazwa joba
  state           "running" (tylko current.json) | "done"
  outcome         OPUBLIKOWANO | ZAPARKOWANO | BEZ WPISU | BŁĄD | PILNE | POMINIĘTO; null w toku
  reason          jedna linia po polsku: co się stało i co z tym zrobić
  title, pr       z JSON-a wyniku potoku jako tekst albo null
  urls            z JSON-a wyniku: lista tekstów (sam adres to lista z jednym); [] bez niego
  notes           z JSON-a wyniku jako tekst (lista łączona "; "); "" bez niego
  wynik           linie ostatniego bloku WYNIK ze stdout ([] bez niego)
  cost_usd        koszt próby z licznika (null, gdy żaden płatnik nie został wybrany)
  ledger_usd      ile poszło do dziennika wydatków puli (kredyt); 0 przy subskrypcji
  budget_usd      budżet próby
  payer           null | {mode: credits|subscription, email, org_id (kredyt) albo identity
                  (subskrypcja), verdict: ok | mismatch | unverified | null (w toku)}
  metering_complete   null | czy licznik dostał wszystko (inaczej płatnik "unverified")
  run_id          id biegu runenv (rachunek w runenv/history.jsonl) albo null
  version         przypięta wersja Claude Code albo null
  started_at, ended_at   zegar ścienny (epoch s); ended_at null w toku
  awake_seconds   czas czuwania próby (bez snu)
  slept_seconds   ile Mac spał w trakcie próby (ściana minus czuwanie)
  phase           najdalsza faza: start (nic nie wystartowało) | precheck | run | side-effect
                  (side-effect także po kodzie 0, 2, 5 albo końcowym JSON-ie z publikacją)
  exit_code       kod komendy wejścia jak w powłoce (128+N po sygnale) albo null; w current.json
                  od chwili, w której komenda wyszła
  precheck        null | {exit_code, reason (ostatnia linia stdout), awake_seconds}
  stopped         null | budget | payer | leak | exhausted | time_limit | stale | signal: dlaczego
                  runner zatrzymał komendę
  facts           [{kind, detail, phase}] fakty zaobserwowane przez runnera: billing, auth, limit,
                  payer, leak, budget, brake, signal, interrupt, sleep, stale, time_limit, version,
                  contradiction (JSON albo WYNIK kłóci się z kodem), runner (runner zginął)
  override        null | rodzaj faktu, który zdecydował o wyniku zamiast kodu wyjścia
  retryable       czy A2b może spróbować jeszcze raz w tym slocie (nigdy po fazie side-effect)
  final           not retryable: ten wynik zamyka slot
  switch_payment  następna próba powinna płacić innym źródłem (billing, auth, limit)
  skip            null | przyczyna POMINIĘTO, od niej zależy, kiedy A2b spróbuje znowu:
                    heavy      biegnie inny ciężki job (heavy.json mówi który)
                    hold       `jobs hold` do hold.json "until"
                    memory     bramka pamięci nie wpuściła przez admit_min
                    payer      runenv: nikt nie zapłaci (pula i subskrypcje bez zapasu)
                    version    przypięta wersja Claude Code zniknęła, a canary nie przeszedł
                    interrupt  runner dostał sygnał przed startem komendy (launchd, kill, Ctrl-C)
  banner          null | jedna linia, którą trzeba pokazać niezależnie od wyniku (obcy płatnik,
                  wyciek, stop przy 1,5 × budżet)
  live_check      null | [{url, status, ok, network, title_found?, error?}]: runner sam sprawdził
                  strony przy PILNE (adresy z JSON-a wyniku, inaczej live_urls joba; [] bez
                  adresów); status null i network true to brak odpowiedzi (sieć, DNS, TLS, czas),
                  czyli "nie wiadomo", a nie "strona nie działa"
  log             ścieżka logu próby
  pid             pid runnera
  slot            dowolny tekst z `--slot` (A2b); null przy biegu ręcznym

Komendy:
  jobs list [--json]                    joby, ostatni wynik, bieg w toku, wstrzymanie
  jobs run NAZWA [--slot S] [--json]    jedna próba; --json wypisuje zapis na stdout
  jobs log NAZWA [-n 5] [--json]        ostatnie próby i ogon logu
  jobs add NAZWA --cwd KATALOG --entry 'KOMENDA' --budget-usd N [--precheck 'KOMENDA']
       [--run-min 180] [--precheck-min 10] [--admit-min 15] [--grace-s 15] [--footprint-gb 2]
       [--mode auto|credits|subscription] [--live-urls URL,URL] [--light] [--disabled]
  jobs set NAZWA POLE=WARTOŚĆ...        cwd, entry, precheck, budget_usd, mode, heavy, enabled,
                                        footprint_gb, precheck_min, run_min, admit_min, grace_s,
                                        live_urls (po przecinku albo JSON), schedule (JSON)
  jobs remove NAZWA                     usuwa definicję (historia i logi zostają)
  jobs hold [--hours 4] [--reason TEKST] / jobs release

Kody `jobs run`: kod wyniku z kontraktu (0 OPUBLIKOWANO, 2 ZAPARKOWANO, 4 BEZ WPISU, 1 BŁĄD,
5 PILNE), 75 POMINIĘTO, 73 ten job już biegnie (bez zapisu), 64 zły argument albo job wyłączony
(bez zapisu; nie 2, bo 2 to tu ZAPARKOWANO).

Zmienne dla potoku (poza CLAUDE_ACC_JOB_SETTINGS i CLAUDE_ACC_JOB_BUDGET_USD od runenv):
CLAUDE_ACC_JOB (nazwa), CLAUDE_ACC_JOB_RESULT, CLAUDE_ACC_JOB_HEAVY, CLAUDE_ACC_JOB_AWAKE_MINUTES
(limit komendy wejścia). Testy: CLAUDE_ACC_JOBS_CLOCK (plik {"slept": s} dopisywany do zegara
ściennego runnera), CLAUDE_ACC_TOOL_PATH (updates.tool_path), SCHED_FAKE_MEMORY (sched.py).
"""

import fcntl
import html
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import credits
import lastresort
import runenv
import updates

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = credits.STATE_DIR
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
JOBS_PATH = os.path.join(JOBS_DIR, "jobs.json")
HOLD_PATH = os.path.join(JOBS_DIR, "hold.json")
HEAVY_LOCK = os.path.join(JOBS_DIR, "heavy.lock")
HEAVY_PATH = os.path.join(JOBS_DIR, "heavy.json")
EDIT_LOCK = os.path.join(JOBS_DIR, ".edit.lock")
SCHED_STATE = os.path.join(STATE_DIR, "sched", "state.json")
DEVGUARD_STATE = os.path.join(STATE_DIR, "devguard-state.json")

VERSION = 1
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
CODE_OUTCOME = {0: "OPUBLIKOWANO", 2: "ZAPARKOWANO", 4: "BEZ WPISU", 1: "BŁĄD", 5: "PILNE"}
SIDE_EFFECT_CODES = (0, 2, 5)  # opublikowane, zaparkowane (PR albo szkic), wypchnięte: efekt już jest
# wynik w JSON-ie, który potok pisze dopiero po znaczniku (kontrakt, punkt 5): efekt już jest
PUBLISHED_OUTCOMES = ("OPUBLIKOWANO", "ZAPARKOWANO", "PILNE")
SKIPPED, BUSY, USAGE = 75, 73, 64
EXIT_CODES = dict({v: k for k, v in CODE_OUTCOME.items()}, **{"POMINIĘTO": SKIPPED})
DEFAULT_LIMITS = {"precheck_min": 10.0, "run_min": 180.0, "admit_min": 15.0, "grace_s": 15.0}
FIELDS = ("cwd", "entry", "precheck", "budget_usd", "mode", "heavy", "enabled", "footprint_gb", "limits",
          "live_urls", "schedule")  # fmt: skip
SKIP_KINDS = ("heavy", "hold", "memory", "payer", "version", "interrupt")
LOOP_S = 0.5
# hamulec zapisuje ofiarę na końcu tiku, po łasce SIGTERM -> SIGKILL (lastresort: brake_grace_seconds 8)
BRAKE_WAIT_S = 12.0
SLEEP_GAP_S = 5.0  # skok zegara ściennego ponad czuwanie w jednym obrocie pętli: Mac spał
STALE_S = 12 * 3600
ADMIT_POLL_S = 5.0
HEAVY_QUEUE_S = 1800  # ciężki krok potoku czeka w kolejce schedulera najwyżej tyle
SAVE_EVERY_S = 10.0
KEEP_LOGS = 60
KEEP_HISTORY = 500
# fakty, które przed fazą side-effect przebijają kod wyjścia, od najważniejszego (powód w zapisie)
OVERRIDING = ("billing", "auth", "limit", "brake", "signal", "sleep", "stale", "time_limit")
PAYMENT_FACTS = ("billing", "auth", "limit")
FINAL_STOPS = ("budget", "payer", "leak")
# fakt, który opisuje dany stop runnera
STOP_FACTS = {"exhausted": ("billing",), "signal": ("interrupt", "signal")}
STOP_ALARMS = ("payer", "leak", "exhausted")  # jak runenv.run_command: zatrzymać od razu
# subskrypcja: token żyje ok. 8 h, runenv odmawia awake_minutes + 30 > 450
SUBSCRIPTION_MAX_MIN = runenv.MAX_TOKEN_MINUTES - runenv.TOKEN_MARGIN_MIN
PHASE_TEXT = {"precheck": "precheck", "run": "komenda wejścia", "side-effect": "faza side-effect"}
BRAKE_LABELS = {code: label for code, label, *_rest in lastresort.CLASSES}
# zegar ścienny w testach: plik {"slept": s} udaje sen tylu sekund
CLOCK_SEAM = os.environ.get("CLAUDE_ACC_JOBS_CLOCK")


class Skip(Exception):
    """Próba nie wystartowała (POMINIĘTO); A2b spróbuje później. kind: jeden z SKIP_KINDS."""

    def __init__(self, kind, reason):
        if kind not in SKIP_KINDS:
            raise ValueError(f"nieznana przyczyna POMINIĘTO: {kind}")  # A2b zna tylko SKIP_KINDS
        super().__init__(reason)
        self.kind = kind


class Fail(Exception):
    """Próba nie wystartowała z powodu, który trzeba naprawić albo ponowić (BŁĄD)."""

    def __init__(self, reason, retryable):
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


# ---------- pliki ----------


def read_json(path, default=None):
    return runenv.read_json(path, default)


def job_dir(name):
    return os.path.join(JOBS_DIR, name)


def try_lock(path):
    """Deskryptor z flock albo None, gdy trzyma go ktoś inny. Jądro zwalnia go ze śmiercią procesu."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


class Edit:
    """Blokada zapisu definicji (jobs.json), żeby dwie zmiany naraz się nie zjadły."""

    def __enter__(self):
        os.makedirs(JOBS_DIR, mode=0o700, exist_ok=True)
        self.fd = os.open(EDIT_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def load_jobs():
    data = read_json(JOBS_PATH, {})
    jobs = (data or {}).get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, dict) else {}


def load_job(name):
    job = load_jobs().get(name)
    if not isinstance(job, dict):
        raise credits.UsageError(f"nie ma joba {name!r}; lista: claude-acc jobs list")
    return normalized(job)


def normalized(job):
    out = {"precheck": None, "mode": "auto", "heavy": True, "enabled": True, "footprint_gb": 2.0, "live_urls": [],
           "schedule": None}  # fmt: skip
    out.update({k: v for k, v in job.items() if k in FIELDS})
    out["limits"] = dict(DEFAULT_LIMITS, **(job.get("limits") or {}))
    return out


def history(name, limit=None):
    items = runenv.jsonl(os.path.join(job_dir(name), "history.jsonl"))
    return items[-limit:] if limit else items


def last_record(name):
    items = history(name, 1)
    return items[-1] if items else None


def append_history(name, record):
    path = os.path.join(job_dir(name), "history.jsonl")
    runenv.append_line(path, record)
    if runenv.count_lines(path) > 2 * KEEP_HISTORY:
        keep = history(name)[-KEEP_HISTORY:]
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
        os.replace(tmp, path)


def prune(folder, keep, first=()):
    """Zostawia keep plików: najpierw idą nazwy z first (logi prób POMINIĘTO), potem najstarsze
    (po czasie zmiany), żeby seria pominięć nie zjadła logu ostatniego prawdziwego biegu."""
    try:
        paths = [os.path.join(folder, n) for n in os.listdir(folder)]
        paths.sort(key=lambda p: os.stat(p).st_mtime)
    except OSError:
        return
    excess = len(paths) - keep
    if excess <= 0:
        return
    paths.sort(key=lambda p: os.path.basename(p) not in first)  # stabilne: w obu grupach od najstarszego
    for path in paths[:excess]:
        try:
            os.unlink(path)
        except OSError:
            pass


def held():
    """Wstrzymanie (`jobs hold`) albo None, gdy go nie ma albo minęło."""
    hold = read_json(HOLD_PATH)
    if isinstance(hold, dict) and float(hold.get("until") or 0) > time.time():
        return hold
    return None


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def when(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"


def duration(seconds):
    seconds = max(0, int(round(seconds or 0)))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h".replace(".", ",")


def gb(value):
    return f"{value:.1f}".replace(".", ",") + " GB"


# ---------- zegary ----------


def clocks():
    """(czuwanie, ściana) w sekundach. time.monotonic() to tu mach_absolute_time(), który stoi w
    czasie snu (man clock_gettime: CLOCK_UPTIME_RAW); CLOCK_MONOTONIC biegnie i w czasie snu."""
    awake = time.monotonic()
    wall = time.clock_gettime(time.CLOCK_MONOTONIC)
    if CLOCK_SEAM:
        seam = read_json(CLOCK_SEAM)
        wall += float((seam or {}).get("slept") or 0) if isinstance(seam, dict) else 0.0
    return awake, wall


# ---------- pamięć, hamulec, kolejka schedulera ----------


def memory_verdict(footprint_gb):
    """(wpuścić?, opis) z `sched status --json`. Scheduler, który nie odpowiada, nie zatrzymuje
    blogów: wpuszczamy z uwagą w logu."""
    try:
        out = subprocess.run([sys.executable, os.path.join(HERE, "acc.py"), "sched", "status", "--json"],
                             capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)  # fmt: skip
        mem = json.loads(out.stdout)["memory"]
        free = float(mem["free_for_admission_gb"])
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        return True, f"scheduler pamięci nie odpowiedział ({exc.__class__.__name__}); wpuszczam bez sprawdzenia"
    brake = mem.get("brake") or "normal"
    if brake in ("brake", "emergency"):
        return False, f"hamulec pamięci: {brake}"
    if mem.get("pressure") == "critical":
        return False, "presja pamięci krytyczna"
    if footprint_gb > 0 and free < footprint_gb:
        return False, f"do wpuszczenia {gb(free)}, bieg potrzebuje {gb(footprint_gb)}"
    return True, f"do wpuszczenia {gb(free)}"


def heavy_wrapper(name):
    """$CLAUDE_ACC_JOB_HEAVY: potoki dzielą go po białych znakach, więc ścieżki bez spacji; inaczej
    `claude-acc run` z PATH (tool_path ma ~/.local/bin)."""
    words = [sys.executable, os.path.join(HERE, "acc.py"), "sched", "run"]
    if any(re.search(r"\s", w) for w in words):
        words = ["claude-acc", "run"]
    return " ".join(words + ["--via", "plock", "--agent", f"jobs-{name}", "--timeout", str(HEAVY_QUEUE_S), "--"])


class SchedQueue:
    """Czy proces z drzewa biegu czeka w kolejce schedulera (sched/state.json, wpis bez "where")."""

    def __init__(self):
        self.mtime, self.waiting = None, set()

    def waits(self, pids):
        try:
            mtime = os.stat(SCHED_STATE).st_mtime
        except OSError:
            return False
        if mtime != self.mtime:
            self.mtime = mtime
            state = read_json(SCHED_STATE, {})
            queue = state.get("queue") if isinstance(state, dict) else None
            self.waiting = {j.get("pid") for j in queue or [] if isinstance(j, dict) and j.get("where") is None}
        return bool(self.waiting & pids)


def brake_kills(pids, since):
    """Ofiary hamulca pamięci (devguard-state.json, "lastresort") z drzewa biegu od chwili since."""
    state = read_json(DEVGUARD_STATE, {})
    events = state.get("lastresort") if isinstance(state, dict) else None
    return [e for e in events or [] if isinstance(e, dict) and e.get("pid") in pids and float(e.get("at") or 0) >= since]


def state_stamp():
    try:
        return os.stat(DEVGUARD_STATE).st_mtime_ns
    except OSError:
        return None


def await_brake(pids, since, stop):
    """Ofiary hamulca z drzewa biegu, gdy ich zapis może jeszcze nie leżeć na dysku. lastresort.reap
    najpierw zatrzymuje drzewo (SIGTERM, po łasce SIGKILL), a strażnik zapisuje zdarzenie dopiero
    na końcu swojego tiku (devguard_core: save_state po tick), czyli już po wyjściu potoku. Czeka
    więc na pierwszą zmianę devguard-state.json, najwyżej BRAKE_WAIT_S czuwania; stop() przerywa.
    Bez tego pliku strażnik nigdy nie działał i nie ma na co czekać."""
    stamp = state_stamp()  # przed odczytem: zapis między odczytem a czekaniem zmienia stempel
    if stamp is None:
        return []
    kills = brake_kills(pids, since)
    deadline = time.monotonic() + BRAKE_WAIT_S
    while not kills and time.monotonic() < deadline and not stop():
        time.sleep(0.25)
        if state_stamp() != stamp:
            return brake_kills(pids, since)
    return kills


# ---------- wynik potoku ----------


def read_result(path):
    data = read_json(path)
    return data if isinstance(data, dict) else None


def side_effect_seen(result):
    """Plik wyniku mówi, że efekt jest na zewnątrz: znacznik albo końcowy JSON z wynikiem, który
    potok pisze po znaczniku (Renggli nadpisuje znacznik końcowym JSON-em bez "phase")."""
    return bool(result) and (result.get("phase") == "side-effect" or result.get("outcome") in PUBLISHED_OUTCOMES)


def result_fields(result):
    """title, urls, pr, notes z JSON-a wyniku w kształcie zapisu: urls to lista tekstów (sam adres
    to lista z jednym, nigdy lista znaków), notes to tekst (lista, jak u Renggli, łączona "; ")."""
    result = result or {}
    urls = result.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()] if isinstance(urls, list) else []
    notes = result.get("notes")
    if isinstance(notes, list):
        notes = "; ".join(str(n).strip() for n in notes if n is not None and str(n).strip())

    def text(value):
        return None if value is None else str(value)

    return {"title": text(result.get("title")), "urls": urls, "pr": text(result.get("pr")),
            "notes": "" if notes is None else str(notes)}  # fmt: skip


def wynik_block(stdout):
    """Linie ostatniego bloku WYNIK ze stdout komendy (do pustej linii albo końca)."""
    lines = stdout.decode(errors="replace").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("WYNIK:")]
    if not starts:
        return []
    block = []
    for line in lines[starts[-1]:]:
        if not line.strip():
            break
        block.append(line.rstrip())
    return block


def wynik_outcome(block):
    if not block:
        return None
    word = block[0][len("WYNIK:"):].strip()
    return next((o for o in list(CODE_OUTCOME.values()) if word == o or word.startswith(o + " ")), word)


def last_line(stdout):
    lines = [x.strip() for x in stdout.decode(errors="replace").splitlines() if x.strip()]
    return lines[-1] if lines else ""


def shell_code(returncode):
    return 128 - returncode if returncode < 0 else returncode


def signal_of(code):
    """Numer sygnału, od którego zginęła komenda (kod jak w powłoce 129-159), albo None."""
    return code - 128 if code is not None and 128 < code < 160 else None


def signal_name(num):
    try:
        return signal.Signals(num).name
    except ValueError:
        return f"sygnał {num}"


class Follow308(urllib.request.HTTPRedirectHandler):
    """urllib w Pythonie 3.9 nie idzie za 308 (dopiero 3.11), a outofplace.space odpowiada 308 na
    www: bez tego żywa strona wyglądałaby na zepsutą. 308 to 307 bez zmiany metody, a GET to GET."""

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_302(req, fp, 307, msg, headers)


def live_check(urls, title=None):
    """Runner sam patrzy na strony po nieczystym końcu: status 200 i, gdy JSON ma tytuł, czy jest
    na stronie. network: true, gdy nie było odpowiedzi HTTP (sieć, DNS, TLS, czas): to nie znaczy,
    że strona nie działa, tylko że runner tego nie wie."""
    opener = urllib.request.build_opener(Follow308)
    out = []
    for url in [u for u in urls if isinstance(u, str)][:10]:
        if not re.match(r"https?://", url):
            out.append({"url": url, "status": None, "ok": False, "network": False, "error": "to nie adres http(s)"})
            continue
        body = b""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "claude-acc-jobs live check"})
            with opener.open(req, timeout=20) as resp:
                status = resp.status
                body = resp.read(2_000_000)
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:  # noqa: BLE001 - sieć, DNS, TLS, czas: brak odpowiedzi strony
            error = f"{exc.__class__.__name__}: {exc}"[:200]
            out.append({"url": url, "status": None, "ok": False, "network": True, "error": error})
            continue
        item = {"url": url, "status": status, "ok": status == 200, "network": False}
        if title and status == 200:
            item["title_found"] = title.lower() in html.unescape(body.decode(errors="replace")).lower()
        out.append(item)
    return out


def live_text(checks):
    if checks is None:
        return ""
    if not checks:
        return "; nie ma adresów do sprawdzenia (JSON wyniku bez urls, job bez live_urls): zajrzyj do logu i na stronę"
    good = [c for c in checks if c["ok"]]
    dead = ", ".join(f"{c['url']} ({c['status'] or c.get('error')})" for c in checks if not c["ok"] and not c.get("network"))
    offline = ", ".join(f"{c['url']} ({c.get('error')})" for c in checks if c.get("network"))
    text = f"; sprawdzenie na żywo: {len(good)} z {len(checks)} adresów odpowiada 200"
    if dead:
        text += f", nie działa: {dead}"
    if offline:
        text += f", bez odpowiedzi (błąd sieci, nie wiadomo, czy działa): {offline}"
    return text


# ---------- decyzja ----------


def decide(phase, code, stopped, side, result, wynik, facts, precheck_reason="", problem=None):
    """Wynik próby: dict(outcome, reason, retryable, override, switch_payment, banner, unclean).

    phase: "precheck" albo "run" (komenda, która skończyła próbę); code: jej kod jak w powłoce;
    stopped: dlaczego runner ją zatrzymał; side: czy bieg doszedł do fazy side-effect; result:
    JSON wyniku; wynik: słowo z bloku WYNIK; facts: {rodzaj: opis} z tej fazy; problem: błąd
    runnera po fazie side-effect. unclean: nieczysty koniec po fazie side-effect (PILNE, do
    którego runner dopisuje swoje sprawdzenie stron); nie trafia do zapisu."""
    banner = next((facts[k] for k in FINAL_STOPS if k in facts), None)
    out = {"override": None, "switch_payment": False, "banner": banner, "retryable": False, "unclean": False}
    if side:
        want = CODE_OUTCOME.get(code)
        said = (result or {}).get("outcome")
        problems = [problem] if problem else []
        if stopped:
            problems.append(next((facts[k] for k in STOP_FACTS.get(stopped, (stopped,)) if k in facts), f"zatrzymany ({stopped})"))
        elif code is None:
            if not problems:
                problems.append("komenda wejścia nie skończyła się sama")
        elif want is None:
            sig = signal_of(code)
            problems.append(f"komenda zginęła od {signal_name(sig)}" if sig else f"komenda skończyła się kodem {code} spoza kontraktu")
        elif not said:
            problems.append(f"kod {code}, ale bez końcowego JSON-a wyniku")
        elif said != want:
            problems.append(f"JSON wyniku mówi {said}, a kod {code} to {want}")
        elif wynik and wynik != want:
            problems.append(f"WYNIK mówi {wynik}, a kod {code} to {want}")
        if not problems:
            return dict(out, outcome=want, reason=(result or {}).get("reason") or want)
        return dict(out, outcome="PILNE", reason="po fazie side-effect: " + "; ".join(problems), unclean=True)
    if "budget" in facts or "payer" in facts or "leak" in facts:
        return dict(out, outcome="BŁĄD", reason=banner)
    if "interrupt" in facts:
        return dict(out, outcome="BŁĄD", reason=facts["interrupt"])
    found = [k for k in OVERRIDING if k in facts]
    if found:
        payment = any(k in facts for k in PAYMENT_FACTS)
        reason = facts[found[0]] + ("; następna próba innym źródłem płatności" if payment else "; do ponowienia")
        return dict(out, outcome="BŁĄD", reason=reason, retryable=True, override=found[0], switch_payment=payment)
    if phase == "precheck":
        if code == 4:
            return dict(out, outcome="BEZ WPISU", reason=precheck_reason or "precheck: dziś nic do zrobienia")
        return dict(out, outcome="BŁĄD", retryable=True, reason=f"precheck (kod {code}): {precheck_reason or 'bez powodu na stdout'}")
    reason = (result or {}).get("reason")
    if code == 4:
        return dict(out, outcome="BEZ WPISU", reason=reason or "kod 4 bez powodu w JSON-ie wyniku")
    if code == 1:
        if not result or not result.get("outcome"):
            return dict(out, outcome="BŁĄD", reason="kod 1 bez JSON-a wyniku: potok padł, zanim zapisał wynik; zajrzyj do logu")
        return dict(out, outcome="BŁĄD", reason=reason or "kod 1", retryable=result.get("retryable") is True)
    if code is None:
        return dict(out, outcome="BŁĄD", reason=facts.get("start") or "komenda nie wystartowała")
    return dict(out, outcome="BŁĄD", reason=f"kod {code} spoza kontraktu (0, 1, 2, 4, 5); zajrzyj do logu")


# ---------- próba ----------


def base_record(name, job, attempt_id, log, slot):
    return {
        "v": VERSION, "id": attempt_id, "job": name, "state": "running", "outcome": None, "reason": "",
        "title": None, "urls": [], "pr": None, "notes": "", "wynik": [], "cost_usd": None, "ledger_usd": None,
        "budget_usd": float(job["budget_usd"]), "payer": None, "metering_complete": None, "run_id": None,
        "version": None, "started_at": time.time(), "ended_at": None, "awake_seconds": 0.0, "slept_seconds": 0.0,
        "phase": "start", "exit_code": None, "precheck": None, "stopped": None, "facts": [], "override": None,
        "retryable": False, "final": False, "switch_payment": False, "skip": None, "banner": None,
        "live_check": None, "log": log, "pid": os.getpid(), "slot": slot,
    }  # fmt: skip


def apply_bill(rec, bill):
    """Rachunek runenv (finish() albo sweep()) w zapisie: koszt, dziennik puli, płatnik, licznik."""
    rec["payer"] = dict(rec.get("payer") or {}, verdict=bill["payer_check"]["verdict"])
    rec.update(cost_usd=bill["cost_usd"], ledger_usd=bill["ledger_usd"], metering_complete=bill["metering"]["complete"])


class Attempt:
    """Jedna próba joba: od blokady do zapisu."""

    def __init__(self, name, job, slot):
        self.name, self.job = name, job
        self.dir = job_dir(name)
        skipped = {os.path.basename(r.get("log") or "") for r in history(name) if r.get("outcome") == "POMINIĘTO"}
        for sub in ("logs", "results"):
            os.makedirs(os.path.join(self.dir, sub), mode=0o700, exist_ok=True)
            prune(os.path.join(self.dir, sub), KEEP_LOGS, first=skipped if sub == "logs" else ())
        self.id = os.urandom(6).hex()
        self.log_path = os.path.join(self.dir, "logs", f"{datetime.now():%Y-%m-%d_%H%M%S}-{self.id}.log")
        self.result_path = os.path.join(self.dir, "results", f"{self.id}.json")
        self.log_fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.record = base_record(name, job, self.id, self.log_path, slot)
        self.awake0, self.wall0 = clocks()
        self.last = (self.awake0, self.wall0)
        self.facts = []  # [{"kind", "detail", "phase"}], jeden na rodzaj i fazę
        self.seen = set()
        self.queue = SchedQueue()
        self.run, self.summary = None, None
        self.side = False
        self.interrupted = None
        self.heavy_fd, self.caffeinate = None, None
        self.saved_at = 0.0
        self.previous = {}

    # --- drobne ---

    def say(self, line):
        text = f"[jobs {datetime.now():%H:%M:%S}] {line}\n"
        self.write(text.encode())
        try:
            print(f"{self.name}: {line}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass  # stderr zamknięty (launchd, `| head`): log i tak ma tę linię

    def write(self, data):
        try:
            credits.write_all(self.log_fd, data)
        except OSError:
            pass

    def fact(self, kind, detail, phase=None):
        phase = phase or self.record["phase"]
        if not any(f["kind"] == kind and f["phase"] == phase for f in self.facts):
            self.facts.append({"kind": kind, "detail": detail, "phase": phase})
            self.say(f"fakt ({kind}): {detail}")

    def phase_facts(self, phases):
        return {f["kind"]: f["detail"] for f in self.facts if f["phase"] in phases}

    def tick(self):
        """Zegary co obrót pętli: (czuwanie od startu próby, przyrost czuwania); sen jako fakt."""
        awake, wall = clocks()
        d_awake, d_wall = awake - self.last[0], wall - self.last[1]
        self.last = (awake, wall)
        if d_wall - d_awake >= SLEEP_GAP_S:
            self.record["slept_seconds"] += d_wall - d_awake
            self.fact("sleep", f"Mac spał {duration(self.record['slept_seconds'])} w trakcie biegu")
        return awake - self.awake0, d_awake

    def wall_age(self):
        return clocks()[1] - self.wall0

    def save(self, force=True):
        """current.json: zapis próby w toku dla panelu i dla recover() po śmierci runnera."""
        awake = clocks()[0] - self.awake0
        if not force and awake - self.saved_at < SAVE_EVERY_S:
            return
        self.saved_at = awake
        rec = dict(self.record, awake_seconds=round(awake, 1), facts=list(self.facts), result_path=self.result_path)
        if self.run is not None and self.summary is None:
            rec["cost_usd"] = round(self.run.spent(), 6)
        credits.write_json(os.path.join(self.dir, "current.json"), rec)

    # --- przebieg ---

    def execute(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.previous[sig] = signal.signal(sig, self.on_signal)
        self.save()
        self.say(f"próba {self.id}; log: {self.log_path}")
        self.keep_awake()
        try:
            self.body()
        except Skip as exc:
            self.close_run("skipped")
            self.conclude_early("POMINIĘTO", str(exc), retryable=True, skip=exc.kind)
        except Fail as exc:
            self.close_run("error")
            self.conclude_early("BŁĄD", exc.reason, retryable=exc.retryable)
        except Exception as exc:  # noqa: BLE001 - błąd runnera też zostawia zapis, nie dziurę
            import traceback

            self.write(traceback.format_exc().encode())
            self.crashed(f"błąd runnera: {exc.__class__.__name__}: {exc}")
        finally:
            self.close_run("error")
            self.release()
        return self.record

    def crashed(self, problem):
        """Wyjątek w runnerze. Najpierw stop potoku (finish), potem znacznik czytany jeszcze raz: potok
        mógł go zapisać po ostatnim obrocie pętli. Przed fazą side-effect to BŁĄD do ponowienia; po
        niej PILNE ze sprawdzeniem stron, jak każdy nieczysty koniec (kontrakt, punkt 5)."""
        self.close_run("error")
        self.watch_marker(save=False)
        if not self.side:
            return self.conclude_early("BŁĄD", f"{problem}; do ponowienia", retryable=True)
        if self.summary is not None:
            self.account()
        return self.conclude("run", self.record["exit_code"], None, problem=problem)

    def close_run(self, stopped):
        """runenv.finish() dla biegu, którego komenda nie doszła do własnego końca (odmowa, błąd)."""
        if self.run is not None and self.summary is None:
            self.summary = runenv.finish(self.run, stopped=stopped, grace=5.0)

    def on_signal(self, signum, _frame):
        self.interrupted = self.interrupted or signum

    def keep_awake(self):
        """caffeinate z PATH biegu (tool_path): -i przed uśpieniem z bezczynności, -s przed każdym
        uśpieniem, ważne tylko na zasilaczu (man caffeinate); -w puszcza asercję ze śmiercią runnera."""
        import shutil

        tool = shutil.which("caffeinate", path=updates.tool_path())
        if not tool:
            self.say("nie ma caffeinate: Mac może zasnąć w trakcie biegu")
            return
        self.caffeinate = subprocess.Popen([tool, "-i", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip

    def release(self):
        if self.caffeinate is not None and self.caffeinate.poll() is None:
            self.caffeinate.terminate()
            try:
                self.caffeinate.wait(5)
            except subprocess.TimeoutExpired:
                self.caffeinate.kill()
        if self.heavy_fd is not None:
            try:
                os.unlink(HEAVY_PATH)
            except OSError:
                pass
            os.close(self.heavy_fd)
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
        os.close(self.log_fd)

    def body(self):
        job, limits = self.job, self.job["limits"]
        if not os.path.isdir(job["cwd"]):
            raise Fail(f"nie ma katalogu biegu {job['cwd']}: worktree runnera zniknął", retryable=False)
        if job["heavy"]:
            self.heavy_fd = try_lock(HEAVY_LOCK)
            if self.heavy_fd is None:
                other = read_json(HEAVY_PATH, {}) or {}
                raise Skip("heavy", f"inny ciężki bieg trwa ({other.get('job', '?')} od {hhmm(other.get('since'))})")
            credits.write_json(HEAVY_PATH, {"job": self.name, "pid": os.getpid(), "since": time.time()})
        hold = held()
        if hold:
            raise Skip("hold", f"joby wstrzymane do {hhmm(hold.get('until'))}: {hold.get('reason') or 'bez powodu'}")
        self.admit(float(job["footprint_gb"]), float(limits["admit_min"]) * 60)
        self.run = self.prepare(limits)
        payer = dict({"mode": self.run.mode}, **self.run.payer, verdict=None)
        self.record.update(run_id=self.run.run_id, payer=payer, version=self.run.version)
        self.say(f"płaci {runenv.describe(self.run.meta)}: {self.run.reason}; Claude Code {self.run.version}")
        if self.interrupted:
            raise Skip("interrupt", f"przerwane przed startem komendy ({signal_name(self.interrupted)})")
        if job["precheck"]:
            self.record["phase"] = "precheck"
            self.save()
            code, stopped, out, exhausted, awake = self.supervise("precheck", job["precheck"], float(limits["precheck_min"]) * 60)
            reason = last_line(out)
            self.record["precheck"] = {"exit_code": code, "reason": reason, "awake_seconds": round(awake, 1)}
            self.say(f"precheck: kod {code}: {reason}")
            if stopped or code != 0:
                self.settle(code, stopped, exhausted)
                return self.conclude("precheck", code, stopped, precheck_reason=reason)
        self.record["phase"] = "run"
        self.save()
        code, stopped, out, exhausted, awake = self.supervise("run", job["entry"], float(limits["run_min"]) * 60)
        self.record["exit_code"] = code
        self.record["wynik"] = wynik_block(out)
        self.say(f"komenda wejścia: kod {code}" + (f", zatrzymana ({stopped})" if stopped else ""))
        self.save()  # kod, faza i WYNIK na dysku przed finish(): runner zabity w rozliczeniu nie zgubi publikacji
        self.settle(code, stopped, exhausted)
        return self.conclude("run", code, stopped)

    def admit(self, footprint, wait_s):
        """Bramka pamięci przed biegiem; POMINIĘTO, gdy nie wpuści przez wait_s sekund czuwania."""
        start = clocks()[0]
        told = None
        while True:
            ok, text = memory_verdict(footprint)
            if ok:
                if told:
                    self.say(f"pamięć jest ({text}) po {duration(clocks()[0] - start)} czekania")
                return
            if text != told:
                self.say(f"czekam na pamięć: {text}")
                told = text
            if self.interrupted:
                raise Skip("interrupt", f"przerwane w czekaniu na pamięć ({signal_name(self.interrupted)})")
            if clocks()[0] - start >= wait_s:
                raise Skip("memory", f"brak pamięci przez {duration(wait_s)}: {text}")
            deadline = clocks()[0] + ADMIT_POLL_S
            while clocks()[0] < deadline and not self.interrupted:
                time.sleep(0.2)
                self.tick()

    def base_env(self, limits):
        env = dict(os.environ)
        env.update(
            PATH=updates.tool_path(),
            CLAUDE_ACC_JOB=self.name,
            CLAUDE_ACC_JOB_RESULT=self.result_path,
            CLAUDE_ACC_JOB_HEAVY=heavy_wrapper(self.name),
            CLAUDE_ACC_JOB_AWAKE_MINUTES=f"{float(limits['run_min']):g}",
        )
        return env

    def prepare(self, limits):
        """runenv.prepare(); odmowa "version": canary i drugie podejście (brief A2a, z bramki A1)."""
        minutes = max(1, int(-(-(float(limits["precheck_min"]) + float(limits["run_min"])) // 1)))
        opts = dict(budget_usd=float(self.job["budget_usd"]), mode=self.job["mode"], awake_minutes=minutes,
                    cwd=self.job["cwd"], base_env=self.base_env(limits))  # fmt: skip
        try:
            try:
                return runenv.prepare(self.name, **opts)
            except runenv.Refused as exc:
                if exc.kind != "version":
                    raise
                self.fact("version", exc.reason, "start")
                self.say("sprawdzam nową wersję Claude Code (canary, ok. 0,002 USD)")
                try:
                    summary = runenv.canary()
                except runenv.Refused as again:
                    raise Skip("version", f"wersja Claude Code: {exc.reason}; canary nie wystartował: {again.reason}")
                check = summary["canary"]
                if not check["ok"]:
                    raise Skip("version", f"wersja Claude Code: {exc.reason}; canary odrzucił {check['version']}: {'; '.join(check['problems'])}")
                self.say(f"canary: Claude Code {check['version']} przypięty ({runenv.usd4(summary['cost_usd'])})")
                return runenv.prepare(self.name, **opts)
        except runenv.Refused as exc:
            if exc.kind == "payer":
                raise Skip("payer", f"nikt nie zapłaci: {exc.reason}")
            if exc.kind == "version":
                raise Skip("version", f"wersja Claude Code: {exc.reason}")
            raise Fail(f"środowisko biegu: {exc.reason}", retryable=True)
        except credits.UsageError as exc:
            raise Fail(f"zła definicja joba: {exc}", retryable=False)
        except credits.CreditsError as exc:
            raise Fail(f"środowisko biegu: {exc}", retryable=False)

    def supervise(self, phase, command, limit_s):
        """Komenda w środowisku biegu i własnej grupie procesów; wyjście bajt w bajt do logu.
        Zwraca (kod jak w powłoce, powód stopu albo None, stdout, zdanie o pustym kredycie?, czuwanie)."""
        self.say(f"{PHASE_TEXT[phase]}: {command} (limit {duration(limit_s)} czuwania)")
        started_at = time.time()
        start_awake, _ = self.tick()
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=self.job["cwd"], env=self.run.env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)  # fmt: skip
        except OSError as exc:
            self.fact("start", f"{PHASE_TEXT[phase]} nie wystartowała: {exc}")
            return None, None, b"", False, 0.0
        self.run.attach(proc.pid)
        self.seen.add(proc.pid)
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, 1)
        sel.register(proc.stderr, selectors.EVENT_READ, 2)
        out, tails, exhausted = bytearray(), {1: b"", 2: b""}, False
        queued, stopped, stop_at, exited_at = 0.0, None, None, None
        limit = float(self.job["budget_usd"]) * runenv.STOP_FACTOR
        grace = float(self.job["limits"]["grace_s"])
        while sel.get_map() or proc.poll() is None:
            for key, _ in sel.select(timeout=LOOP_S):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                self.write(chunk)
                window = tails[key.data] + chunk
                exhausted = exhausted or bool(credits.EXHAUSTED_RE.search(window))
                tails[key.data] = window[-64:]
                if key.data == 1:
                    out += chunk
                    del out[:-262144]
            now, d_awake = self.tick()
            pids = runenv.tree({proc.pid}) if proc.poll() is None else set()
            self.seen |= pids
            if pids and self.queue.waits(pids):
                queued += d_awake
            if phase == "run":
                self.watch_marker()
            if stopped is None:
                stopped = self.stop_reason(phase, now - start_awake - queued, limit_s, limit)
                if stopped:
                    self.say(f"zatrzymuję {PHASE_TEXT[phase]}: {stopped}")
                    stop_at = now
                    kill_group(proc.pid, signal.SIGTERM)
            elif stop_at is not None and now - stop_at > grace:
                self.say(f"{PHASE_TEXT[phase]} nie wyszła po SIGTERM: SIGKILL dla grupy")
                stop_at = None
                kill_group(proc.pid, signal.SIGKILL)
            if proc.poll() is not None:
                exited_at = exited_at or now
                if phase == "run":
                    self.entry_exited(shell_code(proc.returncode))
                if now - exited_at > 5:
                    break  # wnuk trzyma rury po wyjściu komendy; grupa i finish go zatrzymają
            self.save(force=False)
        code = shell_code(proc.wait())
        if phase == "run":
            self.entry_exited(code)
        stop_group(proc.pid)
        if phase == "run":
            self.watch_marker()
        awake = self.tick()[0] - start_awake
        sig = signal_of(code)
        if sig and not stopped:
            self.fact("signal", f"{PHASE_TEXT[phase]} zginęła od {signal_name(sig)} spoza runnera (kod {code})")
        # zapis hamulca liczy się tylko przed fazą side-effect, przy końcu, którego runner nie zrobił sam
        late = not stopped and not self.side and code not in (0, None)
        since = started_at - 1
        kills = await_brake(self.seen, since, lambda: self.interrupted) if late else brake_kills(self.seen, since)
        for kill in kills:
            what = BRAKE_LABELS.get(kill.get("code"), kill.get("code") or "proces")
            size = gb(float(kill.get("size") or 0) / 1024**3)
            self.fact("brake", f"hamulec pamięci zatrzymał {what} (pid {kill.get('pid')}, {size}) w trakcie biegu")
        return code, stopped, bytes(out), exhausted, awake

    def stop_reason(self, phase, counted, limit_s, budget_stop):
        """Powód zatrzymania komendy teraz albo None; zapisuje fakty z licznika."""
        if self.interrupted:
            name = signal_name(self.interrupted)
            if self.interrupted == signal.SIGINT:
                self.fact("interrupt", "przerwane ręcznie (Ctrl-C); bez ponawiania")
            else:
                self.fact("signal", f"runner dostał {name} (launchd, wyłączenie Maca albo kill); bieg zatrzymany")
            return "signal"
        stop = None
        for alarm in self.run.alarms():
            kind = {"exhausted": "billing"}.get(alarm["kind"], alarm["kind"])
            self.fact(kind, ALARM_TEXT.get(kind, "{}").format(runenv.scrub(alarm.get("reason") or "")[:200]))
            if alarm["kind"] in STOP_ALARMS and stop is None:
                stop = alarm["kind"]
        if stop:
            return stop
        spent = self.run.spent()
        if spent >= budget_stop:
            self.fact("budget", f"koszt {runenv.usd4(spent)} przekroczył 1,5 × budżet ({runenv.usd4(budget_stop)}); bieg zatrzymany")
            return "budget"
        if counted > limit_s:
            self.fact("time_limit", f"przekroczony limit {duration(limit_s)} czuwania ({PHASE_TEXT[phase]}); zatrzymana")
            return "time_limit"
        if not self.side and self.wall_age() > STALE_S:
            self.fact("stale", f"bieg trwa {duration(self.wall_age())} zegara ściennego (Mac spał); zatrzymany przed fazą side-effect")
            return "stale"
        return None

    def watch_marker(self, save=True):
        """Faza side-effect z pliku wyniku: znacznik albo końcowy JSON z wynikiem po publikacji."""
        if self.side:
            return
        if side_effect_seen(read_result(self.result_path)):
            self.side = True
            self.record["phase"] = "side-effect"
            self.say("faza side-effect: od teraz bez ponawiania, nieczysty koniec to PILNE")
            if save:
                self.save()

    def entry_exited(self, code):
        """Koniec komendy wejścia na dysku od razu, przed rozliczeniem (stop grupy, finish() potrafi
        czekać na marudera kilka sekund): kod 0, 2 albo 5 znaczy, że efekt już jest na zewnątrz, nawet
        gdy runner nie zdążył zobaczyć znacznika. Runner zabity potem zostawia PILNE, nie BŁĄD."""
        if self.record["exit_code"] is not None:
            return
        self.record["exit_code"] = code
        self.watch_marker(save=False)
        if code in SIDE_EFFECT_CODES and not self.side:
            self.side = True
            self.record["phase"] = "side-effect"
            self.say(f"komenda wejścia: kod {code}, efekt już jest na zewnątrz; od teraz bez ponawiania")
        self.save()

    def settle(self, code, stopped, exhausted):
        """runenv.finish(): rachunek, płatnik, licznik; fakty z rachunku."""
        self.summary = runenv.finish(self.run, exit_code=code, stopped=stopped, exhausted=exhausted, grace=5.0)
        self.account()

    def account(self):
        """Rachunek z runenv.finish() w zapisie i fakty z niego (błędy płatności, obcy płatnik)."""
        s = self.summary
        apply_bill(self.record, s)
        for error in s.get("errors") or []:
            if error["kind"] in PAYMENT_FACTS:
                self.fact(error["kind"], ALARM_TEXT[error["kind"]].format(runenv.scrub(error.get("message") or "")[:200]))
        if s["payer_check"]["verdict"] == "mismatch":
            self.fact("payer", ALARM_TEXT["payer"].format("; ".join(s["payer_check"]["problems"])[:300]))

    def conclude(self, phase, code, stopped, precheck_reason="", problem=None):
        result = read_result(self.result_path) if phase == "run" else None
        side = self.side or (phase == "run" and (code in SIDE_EFFECT_CODES or side_effect_seen(result)))
        if side:
            self.record["phase"] = "side-effect"
        result_final = result if result and result.get("outcome") else None
        wynik = wynik_outcome(self.record["wynik"])
        want = CODE_OUTCOME.get(code)
        if not side and want:
            if result_final and result_final.get("outcome") != want:
                self.fact("contradiction", f"JSON wyniku mówi {result_final.get('outcome')}, a kod {code} to {want}; liczy się kod")
            if wynik and wynik != want:
                self.fact("contradiction", f"WYNIK mówi {wynik}, a kod {code} to {want}; liczy się kod")
        # fakty tylko z fazy, która skończyła próbę: sen w czekaniu na pamięć albo w prechecku nie
        # zmienia wyniku komendy wejścia
        phases = ("precheck",) if phase == "precheck" else ("run", "side-effect")
        verdict = decide(phase, code, stopped, side, result_final, wynik, self.phase_facts(phases), precheck_reason,
                         problem=problem)  # fmt: skip
        unclean = verdict.pop("unclean")
        if result is not None:
            self.record.update(result_fields(result))
        if verdict["outcome"] == "PILNE":
            # kontrakt: znacznik nie niesie adresów, więc po śmierci potoku zostają adresy z joba
            urls = self.record["urls"] or list(self.job.get("live_urls") or [])
            self.say(f"PILNE: sprawdzam strony na żywo ({len(urls)})")
            self.record["live_check"] = live_check(urls, self.record["title"])
            if unclean:
                verdict["reason"] += live_text(self.record["live_check"])
        self.finalize(verdict, stopped)

    def conclude_early(self, outcome, reason, retryable, skip=None):
        if self.summary is not None:
            self.account()
        verdict = {"outcome": outcome, "reason": reason, "retryable": retryable, "override": None,
                   "switch_payment": False, "banner": None, "skip": skip}  # fmt: skip
        self.finalize(verdict, None)

    def finalize(self, verdict, stopped):
        awake = clocks()[0] - self.awake0
        self.record.update(verdict)
        self.record.update(state="done", ended_at=time.time(), awake_seconds=round(awake, 1), stopped=stopped,
                           final=not verdict["retryable"], facts=list(self.facts),
                           slept_seconds=round(self.record["slept_seconds"], 1))  # fmt: skip
        if self.record["retryable"] and self.record["phase"] == "side-effect":
            self.record.update(retryable=False, final=True)  # po fazie side-effect nigdy
        self.say(f"{self.record['outcome']}: {self.record['reason']}")
        append_history(self.name, self.record)
        try:
            os.unlink(os.path.join(self.dir, "current.json"))
        except OSError:
            pass


ALARM_TEXT = {
    "billing": "kredyt skończył się w trakcie (credit balance too low): {}",
    "auth": "błąd logowania API (401) w liczniku: {}",
    "limit": "limit zapytań API (429) w liczniku: {}",
    "payer": "zapłacił ktoś inny niż wybrany płatnik: {}",
    "leak": "wyciek logowania: {}",
}


def kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def stop_group(pgid):
    """To, co zostało w grupie procesów komendy (dzieci w tle), dostaje SIGTERM, potem SIGKILL."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.1)
    kill_group(pgid, signal.SIGKILL)


# ---------- runner, który zginął ----------


def discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass  # inny proces już go zapisał i sprzątnął


def unsettled(run_id):
    """Bieg runenv jeszcze bez rachunku: run.json leży, dopóki finish() albo sweep() go nie rozliczy
    (sweep w innym procesie, np. tiku, właśnie trwa). Zapis bez rachunku nie miałby kosztu."""
    if not runenv.RUN_ID_RE.fullmatch(run_id or ""):
        return False
    meta = read_json(os.path.join(runenv.RUNS_DIR, run_id, "run.json"))
    return isinstance(meta, dict) and meta.get("run_id") == run_id


def recover(swept=None):
    """Zapis prób, których runner zginął (SIGKILL, hamulec, launchd, awaria): current.json bez
    żywego właściciela staje się zapisem w historii. Przed fazą side-effect to BŁĄD do ponowienia,
    po niej PILNE ze sprawdzeniem stron. Koszt z rachunku runenv (sweep() rozlicza martwe biegi);
    bieg, którego nikt jeszcze nie rozliczył, czeka do następnego razu."""
    try:
        names = sorted(os.listdir(JOBS_DIR))
    except OSError:
        return []
    done = []
    for name in names:
        path = os.path.join(job_dir(name), "current.json")
        if not os.path.isfile(path):
            continue
        fd = try_lock(os.path.join(job_dir(name), "lock"))
        if fd is None:
            continue  # runner żyje i trzyma blokadę
        try:
            cur = read_json(path)
            if not isinstance(cur, dict) or cur.get("state") != "running" or (last_record(name) or {}).get("id") == cur.get("id"):
                discard(path)  # runner zginął między zapisem w historii a sprzątnięciem: zapis już jest
                continue
            if swept is None:
                swept = runenv.sweep()
            if unsettled(cur.get("run_id")):
                continue
            # koniec to ostatni znak życia runnera (current.json co 10 s), nie chwila odkrycia
            done.append(recover_one(name, cur, swept, os.path.getmtime(path)))
            discard(path)
        finally:
            os.close(fd)
    return done


def recover_one(name, cur, swept, ended_at):
    rec = {k: cur.get(k) for k in base_record(name, {"budget_usd": cur.get("budget_usd") or 0}, "", "", None)}
    bill = next((s for s in swept or [] if s.get("run_id") == cur.get("run_id")), None)
    if bill is None and cur.get("run_id"):
        bill = next((s for s in reversed(runenv.jsonl(runenv.HISTORY_PATH)) if s.get("run_id") == cur["run_id"]), None)
    if bill:
        apply_bill(rec, bill)
    result = read_result(cur.get("result_path") or "") or {}
    code = cur.get("exit_code")
    # efekt na zewnątrz: faza zapisana przez runnera, znacznik albo końcowy JSON po publikacji, kod 0, 2, 5
    side = cur.get("phase") == "side-effect" or side_effect_seen(result) or code in SIDE_EFFECT_CODES
    facts = list(cur.get("facts") or [])
    facts.append({"kind": "runner", "detail": f"runner zginął (pid {cur.get('pid')}) w fazie {cur.get('phase')}",
                  "phase": cur.get("phase")})  # fmt: skip
    rec.update(facts=facts, state="done", ended_at=ended_at, skip=None, **result_fields(result))
    if side:
        job = load_jobs().get(name)
        urls = rec["urls"] or list((normalized(job) if isinstance(job, dict) else {}).get("live_urls") or [])
        rec["phase"] = "side-effect"
        rec["live_check"] = live_check(urls, rec["title"])
        after = f" po kodzie {code} komendy wejścia, przed rozliczeniem" if code is not None else ", potok mógł nie skończyć publikacji"
        reason = f"po fazie side-effect: runner zginął (pid {cur.get('pid')}){after}"
        rec.update(outcome="PILNE", reason=reason + live_text(rec["live_check"]), retryable=False, final=True)
    else:
        rec.update(outcome="BŁĄD", retryable=True, final=False, override="runner",
                   reason=f"runner zginął (pid {cur.get('pid')}) w fazie {cur.get('phase')}: SIGKILL, hamulec pamięci "
                          "albo launchd; do ponowienia")  # fmt: skip
    append_history(name, rec)
    return rec


# ---------- definicje ----------


def validate(name, job):
    """Definicja joba po normalizacji; UsageError z tym, co poprawić."""
    if not NAME_RE.fullmatch(name or ""):
        raise credits.UsageError("nazwa joba: małe litery, cyfry i -, do 32 znaków")
    job = normalized(job)
    cwd = job.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise credits.UsageError(f"katalog biegu (--cwd) nie istnieje albo nie jest ścieżką bezwzględną: {cwd}")
    if not isinstance(job.get("entry"), str) or not job["entry"].strip():
        raise credits.UsageError("--entry: komenda wejścia potoku, np. 'pnpm autopilot'")
    if job["precheck"] is not None and (not isinstance(job["precheck"], str) or not job["precheck"].strip()):
        raise credits.UsageError("--precheck: komenda albo nic")
    try:
        budget = float(job.get("budget_usd"))
    except (TypeError, ValueError):
        budget = 0.0
    if not 0 < budget <= 1000:
        raise credits.UsageError("budget_usd (--budget-usd): kwota większa od zera, najwyżej 1000")
    job["budget_usd"] = budget
    if job["mode"] not in runenv.MODES:
        raise credits.UsageError("mode (--mode): auto, credits albo subscription")
    for key in ("heavy", "enabled"):
        if not isinstance(job[key], bool):
            raise credits.UsageError(f"{key}: true albo false")
    try:
        job["footprint_gb"] = float(job["footprint_gb"])
        limits = {k: float(v) for k, v in job["limits"].items() if k in DEFAULT_LIMITS}
    except (TypeError, ValueError):
        raise credits.UsageError("footprint_gb i limity to liczby")
    if not 0 <= job["footprint_gb"] <= 64:
        raise credits.UsageError("footprint_gb (--footprint-gb): od 0 do 64")
    if not (0 < limits["precheck_min"] <= 120 and 0 < limits["run_min"] <= 24 * 60):
        raise credits.UsageError("limity: precheck_min od 0 do 120, run_min od 0 do 1440 minut czuwania")
    if not (0 <= limits["admit_min"] <= 240 and 0 <= limits["grace_s"] <= 600):
        raise credits.UsageError("limity: admit_min od 0 do 240 minut, grace_s od 0 do 600 sekund")
    job["limits"] = limits
    urls = job["live_urls"]
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    good = isinstance(urls, list) and len(urls) <= 10
    if not good or not all(isinstance(u, str) and re.fullmatch(r"https?://\S+", u) for u in urls):
        raise credits.UsageError("live_urls (--live-urls): adresy http(s) po przecinku, najwyżej 10, np. indeks bloga")
    job["live_urls"] = urls
    total = limits["precheck_min"] + limits["run_min"]
    if job["mode"] == "subscription" and total > SUBSCRIPTION_MAX_MIN:
        raise credits.UsageError(
            f"tryb subskrypcji: precheck + bieg to {total:g} min, a token subskrypcji wystarczy na "
            f"{SUBSCRIPTION_MAX_MIN} min; skróć limity albo --mode credits"
        )
    return {k: job[k] for k in FIELDS}


def save_job(name, job, new=False):
    with Edit():
        data = read_json(JOBS_PATH, {}) or {}
        jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        if new and name in jobs:
            raise credits.UsageError(f"job {name} już jest; zmiany: claude-acc jobs set {name} POLE=WARTOŚĆ")
        jobs[name] = job
        credits.write_json(JOBS_PATH, {"jobs": jobs})


def describe_job(name, job):
    lim = job["limits"]
    parts = ["włączony" if job["enabled"] else "WYŁĄCZONY", "ciężki" if job["heavy"] else "lekki",
             f"budżet {credits.money(job['budget_usd'])}", f"płatnik {job['mode']}",
             f"limity: precheck {lim['precheck_min']:g} min, bieg {lim['run_min']:g} min"]  # fmt: skip
    return f"{name}  ({', '.join(parts)})"


def payer_text(rec):
    p = rec.get("payer")
    if not p:
        return "bez płatnika"
    verdict = {"ok": "sprawdzony", "mismatch": "NIEZGODNY", "unverified": "niesprawdzony"}.get(p.get("verdict"), "w toku")
    kind = "kredyt" if p.get("mode") == "credits" else "subskrypcja"
    return f"{kind} {p.get('email')} ({verdict})"


def record_lines(rec, indent="  "):
    lines = [f"{indent}{when(rec.get('started_at'))}: {rec.get('outcome')}: {rec.get('reason')}"]
    cost = runenv.usd4(rec["cost_usd"]) if rec.get("cost_usd") is not None else "koszt -"
    extra = [cost, payer_text(rec), f"czuwanie {duration(rec.get('awake_seconds'))}"]
    if rec.get("slept_seconds"):
        extra.append(f"sen {duration(rec['slept_seconds'])}")
    lines.append(f"{indent}  " + ", ".join(extra))
    links = list(rec.get("urls") or []) + ([rec["pr"]] if rec.get("pr") else [])
    if links:
        lines.append(f"{indent}  " + " ".join(links))
    if rec.get("banner"):
        lines.append(f"{indent}  UWAGA: {rec['banner']}")
    return lines


def running(name):
    cur = read_json(os.path.join(job_dir(name), "current.json"))
    if not isinstance(cur, dict) or cur.get("state") != "running":
        return None
    alive = runenv.pid_state(int(cur.get("pid") or 0))[0] if cur.get("pid") else False
    return cur if alive else None


# ---------- komendy ----------


def cmd_run(args):
    as_json = credits.switch(args, "--json")
    slot = credits.flag(args, "--slot")
    if len(args) != 1:
        raise credits.UsageError("usage: claude-acc jobs run NAZWA [--slot S] [--json]")
    name = args[0]
    job = load_job(name)
    if not job["enabled"]:
        raise credits.UsageError(f"job {name} jest wyłączony; włącz: claude-acc jobs set {name} enabled=true")
    swept = runenv.sweep()
    recover(swept)
    lock = try_lock(os.path.join(job_dir(name), "lock"))
    if lock is None:
        cur = read_json(os.path.join(job_dir(name), "current.json"), {}) or {}
        print(f"{name} już biegnie: od {hhmm(cur.get('started_at'))}, faza {cur.get('phase', '?')}, pid {cur.get('pid', '?')}; "
              f"log: {cur.get('log', '?')}; podgląd: claude-acc jobs log {name}", file=sys.stderr)  # fmt: skip
        return BUSY
    try:
        record = Attempt(name, job, slot).execute()
    finally:
        os.close(lock)
    for line in record_lines(record, indent=""):
        print(line, file=sys.stderr)
    print(f"log: {record['log']}", file=sys.stderr)
    if as_json:
        print(json.dumps(record, ensure_ascii=False, indent=1))
    return EXIT_CODES[record["outcome"]]


def cmd_list(args):
    as_json = credits.switch(args, "--json")
    credits.no_leftovers(args)
    recover()
    jobs = load_jobs()
    items = []
    for name in sorted(jobs):
        item = dict({"name": name}, **normalized(jobs[name]))
        item.update(last=last_record(name), running=running(name))
        items.append(item)
    hold = held()
    if as_json:
        print(json.dumps({"jobs": items, "hold": hold}, ensure_ascii=False, indent=1))
        return 0
    if hold:
        print(f"joby wstrzymane do {hhmm(hold['until'])}: {hold.get('reason') or 'bez powodu'} (claude-acc jobs release)")
    if not items:
        print("nie ma jobów; dodaj: claude-acc jobs add NAZWA --cwd KATALOG --entry 'KOMENDA' --budget-usd N")
    for item in items:
        print(describe_job(item["name"], item))
        cur = item["running"]
        if cur:
            cost = f", {runenv.usd4(cur['cost_usd'])} do teraz" if cur.get("cost_usd") is not None else ""
            print(f"  biegnie od {hhmm(cur.get('started_at'))} (faza {cur.get('phase')}, "
                  f"{duration(cur.get('awake_seconds'))} czuwania{cost}); log: {cur.get('log')}")  # fmt: skip
        if item["last"]:
            print("\n".join(record_lines(item["last"])))
        elif not cur:
            print("  jeszcze nie biegł")
    return 0


def cmd_log(args):
    as_json = credits.switch(args, "--json")
    count = credits.flag(args, "-n")
    if len(args) != 1:
        raise credits.UsageError("usage: claude-acc jobs log NAZWA [-n 5] [--json]")
    name = args[0]
    count = "5" if count is None else count
    if not count.isdigit() or int(count) < 1:
        raise credits.UsageError("-n: liczba prób, np. 5")
    load_job(name)
    recover()
    runs = history(name, int(count))
    cur = running(name)
    path = (cur or {}).get("log") or (runs[-1]["log"] if runs else None)
    tail = []
    if path:
        try:
            with open(path, errors="replace") as f:
                tail = [line.rstrip("\n") for line in f.readlines()[-40:]]
        except OSError:
            tail = []
    if as_json:
        print(json.dumps({"runs": runs, "running": cur, "log": path, "log_tail": tail}, ensure_ascii=False, indent=1))
        return 0
    if not runs and not cur:
        print(f"{name}: jeszcze nie biegł")
    for rec in runs:
        print("\n".join(record_lines(rec, indent="")))
    if cur:
        print(f"biegnie od {hhmm(cur.get('started_at'))} (faza {cur.get('phase')})")
    if path:
        print(f"\nlog: {path} (ostatnie {len(tail)} linii)")
        print("\n".join(tail))
    return 0


def parse_job_options(args):
    job = {"limits": {}}
    for flag, key in (("--cwd", "cwd"), ("--entry", "entry"), ("--precheck", "precheck"), ("--mode", "mode"),
                      ("--live-urls", "live_urls")):  # fmt: skip
        value = credits.flag(args, flag)
        if value is not None:
            job[key] = value
    for flag, key in (("--budget-usd", "budget_usd"), ("--footprint-gb", "footprint_gb")):
        value = credits.flag(args, flag)
        if value is not None:
            job[key] = number(value, flag)
    for key in DEFAULT_LIMITS:
        value = credits.flag(args, "--" + key.replace("_", "-"))
        if value is not None:
            job["limits"][key] = number(value, "--" + key.replace("_", "-"))
    if credits.switch(args, "--light"):
        job["heavy"] = False
    if credits.switch(args, "--disabled"):
        job["enabled"] = False
    return job


def number(text, name):
    try:
        return float(text)
    except (TypeError, ValueError):
        raise credits.UsageError(f"{name}: liczba, np. 2.5")


def cmd_add(args):
    if not args or args[0].startswith("-"):
        raise credits.UsageError("usage: claude-acc jobs add NAZWA --cwd KATALOG --entry 'KOMENDA' --budget-usd N ...")
    name = args.pop(0)
    job = parse_job_options(args)
    credits.no_leftovers(args)
    if "cwd" in job:
        job["cwd"] = os.path.abspath(os.path.expanduser(job["cwd"]))
    job = validate(name, job)
    save_job(name, job, new=True)
    print(describe_job(name, job))
    total = job["limits"]["precheck_min"] + job["limits"]["run_min"]
    if job["mode"] == "auto" and total > SUBSCRIPTION_MAX_MIN:
        print(f"uwaga: {total:g} min to za długo dla subskrypcji: bez kredytu w puli bieg będzie POMINIĘTO", file=sys.stderr)
    return 0


def parse_value(key, text):
    if key == "schedule":
        try:
            return json.loads(text)
        except ValueError:
            raise credits.UsageError('schedule: JSON, np. {"every_days": 2}')
    if key == "live_urls" and text.strip().startswith("["):
        try:
            return json.loads(text)
        except ValueError:
            raise credits.UsageError('live_urls: adresy po przecinku albo JSON, np. ["https://example.com/blog"]')
    if key in ("heavy", "enabled"):
        value = {"true": True, "1": True, "tak": True, "false": False, "0": False, "nie": False}.get(text.lower())
        if value is None:
            raise credits.UsageError(f"{key}: true albo false")
        return value
    if key in ("budget_usd", "footprint_gb") or key in DEFAULT_LIMITS:
        return number(text, key)
    if key == "precheck" and not text.strip():
        return None
    return text


def cmd_set(args):
    if len(args) < 2:
        raise credits.UsageError("usage: claude-acc jobs set NAZWA POLE=WARTOŚĆ ...")
    name = args.pop(0)
    with Edit():
        jobs = load_jobs()
        if name not in jobs:
            raise credits.UsageError(f"nie ma joba {name!r}")
        job = normalized(jobs[name])
        for item in args:
            key, sep, text = item.partition("=")
            if not sep or (key not in FIELDS and key not in DEFAULT_LIMITS) or key == "limits":
                raise credits.UsageError(f"nieznane pole {key!r}: {', '.join(f for f in FIELDS if f != 'limits')}, "
                                         f"{', '.join(DEFAULT_LIMITS)}")  # fmt: skip
            value = parse_value(key, text)
            if key in DEFAULT_LIMITS:
                job["limits"][key] = value
            else:
                job[key] = os.path.abspath(os.path.expanduser(value)) if key == "cwd" else value
        job = validate(name, job)
        jobs[name] = job
        credits.write_json(JOBS_PATH, {"jobs": jobs})
    print(describe_job(name, job))
    return 0


def cmd_remove(args):
    if len(args) != 1:
        raise credits.UsageError("usage: claude-acc jobs remove NAZWA")
    name = args[0]
    with Edit():
        jobs = load_jobs()
        if jobs.pop(name, None) is None:
            raise credits.UsageError(f"nie ma joba {name!r}")
        credits.write_json(JOBS_PATH, {"jobs": jobs})
    print(f"usunięty job {name} (historia i logi zostają w {job_dir(name)})")
    return 0


def cmd_hold(args):
    hours = number(credits.flag(args, "--hours") or "4", "--hours")
    reason = credits.flag(args, "--reason") or "ręcznie"
    credits.no_leftovers(args)
    if not 0 < hours <= 72:
        raise credits.UsageError("--hours: od 0 do 72")
    until = time.time() + hours * 3600
    with Edit():
        credits.write_json(HOLD_PATH, {"until": until, "reason": reason, "at": time.time()})
    print(f"joby wstrzymane do {when(until)}: {reason}; zdjęcie: claude-acc jobs release")
    return 0


def cmd_release(args):
    credits.no_leftovers(args)
    with Edit():
        try:
            os.unlink(HOLD_PATH)
        except FileNotFoundError:
            pass
    print("joby nie są wstrzymane")
    return 0


COMMANDS = {"run": cmd_run, "list": cmd_list, "log": cmd_log, "add": cmd_add, "set": cmd_set,
            "remove": cmd_remove, "hold": cmd_hold, "release": cmd_release}  # fmt: skip


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        print(__doc__[__doc__.index("Komendy:"):__doc__.index("Kody `jobs run`")].rstrip(), file=sys.stderr)
        return USAGE
    try:
        return COMMANDS[argv[0]](list(argv[1:]))
    except credits.UsageError as exc:
        print(f"błąd: {exc}", file=sys.stderr)
        return USAGE
    except credits.CreditsError as exc:
        print(f"błąd: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
