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
  schedule      harmonogram tiku (niżej) albo null; walidacja przy add i set (check_schedule)

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
  jobs [--json]                         przegląd: każdy slot każdego joba z ostatnich 7 dni, następny
                                        slot, tik, wstrzymanie i to, co czeka na Ciebie
  jobs tick [--json]                    jeden obrót harmonogramu (launchd co 2 min; patrz niżej)
  jobs list [--json]                    joby, ostatni wynik, bieg w toku, wstrzymanie
  jobs run NAZWA [--slot S] [--mode M] [--json]
                                        jedna próba; --mode jednorazowo zamiast mode joba (tik:
                                        przełączenie płatności po switch_payment)
  jobs enable NAZWA / jobs disable NAZWA
  jobs log NAZWA [-n 5] [--json]        ostatnie próby i ogon logu
  jobs add NAZWA --cwd KATALOG --entry 'KOMENDA' --budget-usd N [--precheck 'KOMENDA']
       [--run-min 180] [--precheck-min 10] [--admit-min 15] [--grace-s 15] [--footprint-gb 2]
       [--mode auto|credits|subscription] [--live-urls URL,URL] [--light] [--disabled]
  jobs set NAZWA POLE=WARTOŚĆ...        cwd, entry, precheck, budget_usd, mode, heavy, enabled,
                                        footprint_gb, precheck_min, run_min, admit_min, grace_s,
                                        live_urls (po przecinku albo JSON), schedule (JSON albo null)
  jobs set NAZWA --every 2d [--from 2026-10-09] --at 05:30[,14:00]
  jobs set NAZWA --days pn,cz --at 05:30     harmonogram (czas Europe/Warsaw); też w jobs add
  jobs remove NAZWA                     usuwa definicję (historia i logi zostają)
  jobs hold [--hours 4] [--reason TEKST] / jobs release

Kody `jobs run`: kod wyniku z kontraktu (0 OPUBLIKOWANO, 2 ZAPARKOWANO, 4 BEZ WPISU, 1 BŁĄD,
5 PILNE), 75 POMINIĘTO, 73 ten job już biegnie (bez zapisu), 64 zły argument albo job wyłączony
(bez zapisu; nie 2, bo 2 to tu ZAPARKOWANO).

Zmienne dla potoku (poza CLAUDE_ACC_JOB_SETTINGS i CLAUDE_ACC_JOB_BUDGET_USD od runenv):
CLAUDE_ACC_JOB (nazwa), CLAUDE_ACC_JOB_RESULT, CLAUDE_ACC_JOB_HEAVY, CLAUDE_ACC_JOB_AWAKE_MINUTES
(limit komendy wejścia). Testy: CLAUDE_ACC_JOBS_CLOCK (plik {"slept": s} dopisywany do zegara
ściennego runnera), CLAUDE_ACC_TOOL_PATH (updates.tool_path), SCHED_FAKE_MEMORY (sched.py).

Harmonogram (`jobs tick`, launchd com.filip.claude-acc.jobs co 120 s; etap A2b):
  schedule      {"every_days": N, "anchor": "RRRR-MM-DD"} albo {"weekdays": [0..6], pn = 0}, plus
                "at": ["GG:MM", ...] (najwcześniejszy start, czas Europe/Warsaw), "since" (epoch:
                pierwszy harmonogram i każde włączenie joba) i "edited" (epoch: zmiana
                harmonogramu). Slot sprzed max(since, edited) istnieje tylko, gdy ma zapisy prób:
                edycja ani `jobs enable` nie odpalają slotu z przeszłości, a wyłączenie i włączenie
                nie gubią otwartego slotu ani historii
  slot          id "RRRR-MM-DD/n" (data w Warszawie i numer godziny tego dnia z "at", od 1; tik
                podaje go w `jobs run --slot`, a --at zmienione w trakcie slotu zostawia mu id);
                dni liczone w kalendarzu, nie co 48 h, więc zmiana czasu nie przesuwa godziny
                startu. Ids porównywane jako (data, n). Zapis z innym tekstem w slot to bieg spoza
                tiku, jak ręczny (slot null)

Stan slotu wynika z zapisów prób (history.jsonl, pole slot), bieżącej próby (current.json) i
zegara; tik pamięta tylko to, czego zapisy nie mają (tick-state.json: banery już pokazane, powód
czekania, ostatni tik, próba właśnie wypuszczona). Od pierwszego pasującego:
  zrobiony      jest zapis slotu z final (wynik z zapisu, nigdy nie ponawiany) albo bieg spoza tiku
                (ręczny albo z obcym slot) z OPUBLIKOWANO, ZAPARKOWANO, BEZ WPISU albo PILNE, który
                skończył się między startem slotu a startem następnego; BŁĄD ręcznego biegu (np.
                Ctrl-C) slotu nie zamyka
  biegnie       żywa próba tego slotu (current.json z blokadą joba albo próba wypuszczona przez tik)
  limit prób    3 próby liczone bez wyniku ostatecznego: koniec, baner (BEZ WYNIKU)
  zastąpiony    zaczął się nowszy slot tego joba (także taki, który przepadł, bo job był
                wyłączony) albo nowszy slot ma już zapis: koniec; bez żadnej próby, która ruszyła,
                to BRAK BIEGU, inaczej BEZ WYNIKU; baner przy pierwszym pełnym obudzeniu, w którym
                tik to widzi (zaległe sloty po śnie łączą się w jeden bieg najnowszego slotu)
  ponowienie    ostatnia próba do ponowienia, a backoff jeszcze trwa
  należny       start nie wcześniej niż "at"; czeka, póki choć jedna bramka stoi:
                  Mac nie w pełni obudzony (pmset -g systemstate bez Graphics: DarkWake; pmset nie
                    odpowiada: też nie startujemy), świeżo po śnie (pierwszy pełny tik po śnie albo
                    DarkWake tylko alarmuje, start od następnego), `jobs hold`, inna próba tego joba,
                    current.json martwej próby jeszcze bez zapisu (recover czeka na rachunek runenv),
                    inny ciężki bieg, odstęp 20 min od końca poprzedniego ciężkiego biegu;
                kolejka: najstarszy slot pierwszy, potem nazwa joba; jeden start na tik
Liczona próba: każdy zapis slotu poza POMINIĘTO z skip heavy, hold, memory (bramki tiku i pamięć
nie zjadają prób; pamięć ponawia co 20 min). Backoff po liczonej próbie: 1 h, potem 3 h; po
POMINIĘTO payer (nikt nie zapłaci) 2 h, potem 4 h (okna sesji subskrypcji wracają co 5 h).
Płatność: job auto zostaje na auto, bo runenv sam omija złe źródło: kredyt z billing oznacza
wyczerpany, 429 na subskrypcji trafia do avoid.json, a 401 na subskrypcji tik dopisuje tam sam
(runenv.avoid na 5 h, przy starcie następnej próby). Wyjątek: próba, która płaciła kredytem i
skończyła się 401 albo limitem (switch_payment, override auth albo limit), przełącza resztę slotu
na --mode subscription: runenv nie ma listy omijanych organizacji i wybrałby tę samą znowu.
Decyduje ostatni zapis slotu z płatnikiem kredytowym, nie ostatni zapis (POMINIĘTO bez płatnika
nie gubi przełączenia). Job z mode credits albo subscription nie dostaje --mode.
Banery (osascript; tekst jako argumenty skryptu, on run argv): zapis z BŁĄD ostatecznym,
ZAPARKOWANO albo PILNE ze slotem (tiku albo obcym) i każde PILNE, także ręczne, najwyżej 8 dni po
końcu zapisu; limit prób; slot zastąpiony bez wyniku (BRAK BIEGU, BEZ WYNIKU); o 20:00 (pierwszy
pełny tik po 20:00) każdy niedomknięty slot, który już się zaczął, z powodem czekania jak w
`jobs`, raz na wieczór; nic przy OPUBLIKOWANO ani BEZ WPISU. Tik w DarkWake pisze tylko heartbeat.
Ten sam baner nie wraca (tick-state.json); baner, którego osascript nie przyjął (kod różny od 0),
trafia do tick.log i nie jest oznaczony jako pokazany, więc wraca w następnym tiku; tik zabity w
pół drogi może pokazać baner drugi raz, nigdy go nie zgubi. Tik zabity po wypuszczeniu próby:
następny tik widzi próbę po blokadzie joba i current.json, a druga próba tego joba kończy się
kodem 73 bez zapisu. Wpis jobs.json, który nie jest poprawną definicją (nie obiekt, limity nie
liczbami, enabled albo heavy nie true/false), to ZŁA DEFINICJA tego joba, a zły harmonogram to ZŁY
HARMONOGRAM; pozostałe joby i heartbeat działają dalej. Każdy tik pisze w tick.log stan zasilania
(full, dark, unknown) i to, czy Mac właśnie się obudził.

Pliki tiku (~/.local/share/claude-acc/jobs/): tick.lock (jeden tik naraz), tick-state.json,
tick.log (tik i wyjście wypuszczonych prób), heartbeat.json ({wall, uptime, boot, power,
interval_s, stale_after_s, attention}: `claude-acc status` i panel mówią, gdy tik stoi dłużej niż
stale_after_s czuwania), held-accounts.json (konto subskrypcji, które trzyma żywy bieg, z "until":
automat kont nie przełącza na nie Twoich sesji; pisze runner po wyborze płatnika, czyści runner i
tik). Zegar czuwania między procesami to CLOCK_UPTIME_RAW (stoi w czasie snu; time.monotonic() w
3.9 liczy od startu procesu). Testy: CLAUDE_ACC_JOBS_NOW (plik {wall, uptime, boot}),
CLAUDE_ACC_JOBS_RUNNER (atrapa `jobs run`), CLAUDE_ACC_JOBS_TICK_DIE (punkt, w którym tik zabija się
SIGKILL: after-spawn, after-banner).
"""

import fcntl
import html
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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

# harmonogram (tik)
TICK_LOCK = os.path.join(JOBS_DIR, "tick.lock")
TICK_STATE = os.path.join(JOBS_DIR, "tick-state.json")
TICK_LOG = os.path.join(JOBS_DIR, "tick.log")
HEARTBEAT_PATH = os.path.join(JOBS_DIR, "heartbeat.json")
HELD_PATH = os.path.join(JOBS_DIR, "held-accounts.json")
STALE_ALARM_PATH = os.path.join(JOBS_DIR, "stale-alarm.json")
WARSAW = ZoneInfo("Europe/Warsaw")
DAY = 86400
TICK_INTERVAL_S = 120  # launchd StartInterval (launchd/com.filip.claude-acc.jobs.plist.template)
STALE_AFTER_S = 15 * 60  # tyle czuwania bez tiku to alarm (status, panel, baner z automatu kont)
MAX_ATTEMPTS = 3
BACKOFF_S = (3600, 3 * 3600)  # po 1. i 2. liczonej próbie: sieć albo API mają czas wrócić tego samego dnia
PAYER_BACKOFF_S = (2 * 3600, 4 * 3600)  # nikt nie zapłaci: okna sesji subskrypcji wracają co 5 h
MEMORY_RETRY_S = 20 * 60
LAUNCH_RETRY_S = 20 * 60  # wypuszczona próba nie zostawiła zapisu (kod 64, 73, awaria przed zapisem)
STAGGER_S = 20 * 60  # odstęp między ciężkimi biegami: jedno obudzenie nie wrzuca wszystkich blogów naraz
EVENING_HOUR = 20
# ściana uciekła czuwaniu o tyle między tikami: Mac spał. W czuwaniu oba zegary idą razem (dryf rzędu
# 0,1 ms na minutę), a seria DarkWake 08.10 to 45 s czuwania i ok. 9 s snu (pmset -g log)
SLEEP_GAP_TICK_S = 5
HISTORY_DAYS = 7
BANNER_DAYS = 8  # zapis próby woła banerem najwyżej tyle dni po końcu (klucze "sent" żyją 9 dni)
NOT_COUNTED = ("heavy", "hold", "memory")  # POMINIĘTO, które nie zjada próby
SLOT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})/(\d+)")  # id slotu tiku; inne teksty z --slot to biegi spoza tiku
CLOSES_BY_HAND = ("OPUBLIKOWANO", "ZAPARKOWANO", "BEZ WPISU", "PILNE")  # bieg spoza tiku, który zamyka slot
SWITCH_FROM_CREDITS = ("auth", "limit")  # kredyt z 401 albo limitem: reszta slotu płaci subskrypcją
# baner: tekst i tytuł idą jako argumenty skryptu, nie w jego treści (AppleScript nie zna \uXXXX z json.dumps)
NOTIFY_SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")
HINT_REFRESH_S = 15 * 60
HINT_MARGIN_S = 30 * 60
DAY_NAMES = ("pn", "wt", "śr", "cz", "pt", "so", "nd")
DAY_TOKENS = {
    "pn": 0, "pon": 0, "mon": 0, "wt": 1, "wto": 1, "tue": 1, "śr": 2, "sr": 2, "śro": 2, "sro": 2, "wed": 2,
    "cz": 3, "czw": 3, "thu": 3, "pt": 4, "pią": 4, "pia": 4, "fri": 4, "so": 5, "sob": 5, "sat": 5,
    "nd": 6, "nie": 6, "ndz": 6, "sun": 6,
}  # fmt: skip
SCHEDULE_HELP = ("harmonogram: --every 2d [--from RRRR-MM-DD] --at GG:MM[,GG:MM] albo --days pn,cz --at GG:MM "
                 "(dni: pn wt śr cz pt so nd albo mon tue wed thu fri sat sun; czas Europe/Warsaw)")  # fmt: skip
OUTCOME_BANNERS = ("ZAPARKOWANO", "PILNE")


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


def held(now=None):
    """Wstrzymanie (`jobs hold`) albo None, gdy go nie ma albo minęło."""
    hold = read_json(HOLD_PATH)
    try:
        if isinstance(hold, dict) and float(hold.get("until") or 0) > (wall_now() if now is None else now):
            return hold
    except (TypeError, ValueError):
        pass
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


def tick_clock():
    """(ściana, czuwanie, start systemu) dla tiku i heartbeatu, porównywalne między procesami.
    Czuwanie to CLOCK_UPTIME_RAW (wspólny dla procesów, stoi w czasie snu; time.monotonic() w 3.9
    liczy od pierwszego wywołania w procesie). Start systemu to ściana minus CLOCK_MONOTONIC (ten
    biegnie także w czasie snu): stały w obrębie jednego uruchomienia, inny po restarcie.
    Testy podają wszystkie trzy w pliku $CLAUDE_ACC_JOBS_NOW."""
    wall = time.time()
    uptime = time.clock_gettime(time.CLOCK_UPTIME_RAW)
    boot = wall - time.clock_gettime(time.CLOCK_MONOTONIC)
    seam = os.environ.get("CLAUDE_ACC_JOBS_NOW")
    fake = read_json(seam) if seam else None
    if isinstance(fake, dict):
        wall, uptime, boot = (float(fake.get(k, v)) for k, v in (("wall", wall), ("uptime", uptime), ("boot", boot)))
    return wall, uptime, boot


def wall_now():
    return tick_clock()[0]


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
        self.hint = False
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
        if self.hint:
            free_accounts(pid=os.getpid())
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
        if self.run.mode == "subscription":
            # wskazówka dla automatu kont od chwili wyboru płatnika, a nie od następnego tiku
            self.hint = hold_account(self.run.payer.get("email"), self.name, os.getpid(), hint_until(job, time.time()))
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


# ---------- wskazówka dla automatu kont: konto subskrypcji, które trzyma bieg ----------


def hint_until(job, now):
    """Najpóźniej tyle żyje wskazówka bez odświeżenia: najdłuższy bieg w minutach czuwania (pamięć,
    precheck, komenda) plus zapas. Żywy bieg odświeża ją tik; sen dłuższy niż zapas skraca ją, ale
    wtedy token subskrypcji biegu (ok. 8 h) i tak wygasa."""
    lim = job["limits"]
    return now + (float(lim["admit_min"]) + float(lim["precheck_min"]) + float(lim["run_min"])) * 60 + HINT_MARGIN_S


def edit_hints(change):
    """held-accounts.json pod blokadą definicji; change(lista wpisów) -> nowa lista. Zepsuty plik
    zaczyna od zera. Błąd zapisu nie zatrzymuje ani biegu, ani tiku (False)."""
    try:
        with Edit():
            data = read_json(HELD_PATH)
            items = data.get("accounts") if isinstance(data, dict) else None
            items = [e for e in items or [] if isinstance(e, dict) and isinstance(e.get("email"), str)]
            new = change([dict(e) for e in items])  # kopie: zmiana w miejscu też jest zmianą
            if new != items:
                credits.write_json(HELD_PATH, {"v": 1, "accounts": new})
        return True
    except OSError:
        return False


def hold_account(email, name, pid, until):
    """Bieg `name` (pid) płaci kontem subskrypcji email do until (epoch): automat kont nie
    przełącza na nie Twoich sesji (accswitch.jobs_free w ticku)."""
    if not email:
        return False
    entry = {"email": email.lower(), "job": name, "pid": pid, "since": time.time(), "until": until}
    return edit_hints(lambda items: [e for e in items if e.get("pid") != pid] + [entry])


def free_accounts(pid=None, keep=None):
    """Zdejmuje wskazówki biegu pid albo (keep) wszystkie, dla których keep(wpis) jest fałszem."""
    if keep is None:
        keep = lambda e: e.get("pid") != pid  # noqa: E731
    return edit_hints(lambda items: [e for e in items if keep(e)])


def pid_alive(pid):
    try:
        return bool(pid) and runenv.pid_state(int(pid))[0]
    except (TypeError, ValueError, OSError):
        return False


# ---------- harmonogram: sloty ----------

Slot = namedtuple("Slot", "id start hhmm")


def parse_hhmm(text):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(text).strip())
    if not m:
        raise credits.UsageError(f"godzina {text!r}: GG:MM od 00:00 do 23:59, np. 05:30; {SCHEDULE_HELP}")
    return f"{int(m[1]):02d}:{m[2]}"


def parse_days(text):
    tokens = [t.strip().lower() for t in re.split(r"[,\s]+", str(text)) if t.strip()]
    bad = [t for t in tokens if t not in DAY_TOKENS]
    if not tokens or bad:
        raise credits.UsageError(f"dni {', '.join(bad) or '(puste)'}: nie znam; {SCHEDULE_HELP}")
    return sorted({DAY_TOKENS[t] for t in tokens})


def check_schedule(raw):
    """Harmonogram po normalizacji albo None (job tylko ręczny); UsageError z tym, co poprawić."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise credits.UsageError(SCHEDULE_HELP)
    unknown = set(raw) - {"every_days", "anchor", "weekdays", "at", "since", "edited"}
    if unknown or ("every_days" in raw) == ("weekdays" in raw):
        raise credits.UsageError(SCHEDULE_HELP)
    out = {}
    if "every_days" in raw:
        n = raw["every_days"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 60:
            raise credits.UsageError(f"--every: od 1 do 60 dni, np. --every 2d; {SCHEDULE_HELP}")
        try:
            anchor = date.fromisoformat(str(raw.get("anchor")))
        except ValueError:
            raise credits.UsageError(f"--from: data RRRR-MM-DD, np. 2026-10-10; {SCHEDULE_HELP}")
        out.update(every_days=n, anchor=anchor.isoformat())
    else:
        days = raw["weekdays"]
        if not isinstance(days, list) or not days or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
            raise credits.UsageError(f"--days: co najmniej jeden dzień; {SCHEDULE_HELP}")
        out["weekdays"] = sorted(set(days))
    at = raw.get("at")
    at = [at] if isinstance(at, str) else at
    if not isinstance(at, list) or not 1 <= len(at) <= 6:
        raise credits.UsageError(f"--at: od jednej do sześciu godzin GG:MM; {SCHEDULE_HELP}")
    out["at"] = sorted({parse_hhmm(t) for t in at})
    for key in ("since", "edited"):
        value = raw.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise credits.UsageError(f"schedule.{key}: chwila w sekundach epoch")
            out[key] = float(value)
    return out


BAD_DEFINITION, BAD_SCHEDULE = "zła definicja", "zły harmonogram"


def checked_job(raw):
    """Wpis jobs.json dla tiku i przeglądu: (definicja z harmonogramem po check_schedule, None) albo
    (None, (rodzaj, powód)), rodzaj BAD_DEFINITION albo BAD_SCHEDULE. Ręcznie zepsuty wpis (nie
    obiekt, limity nie liczbami, enabled albo heavy nie true/false) psuje tylko swój job."""
    try:
        if not isinstance(raw, dict):
            raise credits.UsageError(f"wpis to {type(raw).__name__}, nie obiekt JSON")
        if not isinstance(raw.get("limits") or {}, dict):
            raise credits.UsageError("limits to nie obiekt JSON")
        job = normalized(raw)
        for key in DEFAULT_LIMITS:
            float(job["limits"][key])
        for key in ("enabled", "heavy"):
            if not isinstance(job[key], bool):
                raise credits.UsageError(f"{key}: true albo false")
    except credits.UsageError as exc:
        return None, (BAD_DEFINITION, str(exc))
    except (TypeError, ValueError) as exc:
        return None, (BAD_DEFINITION, f"limity to liczby ({exc})")
    try:
        job["schedule"] = check_schedule(job.get("schedule"))
    except credits.UsageError as exc:
        return None, (BAD_SCHEDULE, str(exc))
    return job, None


def raw_enabled(raw):
    """enabled z surowego wpisu (także złego): bez pola włączony."""
    return raw.get("enabled", True) is not False if isinstance(raw, dict) else True


def schedule_text(sched):
    at = " i ".join(sched["at"])
    if "every_days" in sched:
        n = sched["every_days"]
        when_ = "codziennie" if n == 1 else f"co {n} dni"
        return f"{when_} od {date.fromisoformat(sched['anchor']):%d.%m.%Y}, o {at}"
    return f"w {', '.join(DAY_NAMES[d] for d in sched['weekdays'])}, o {at}"


def day_matches(sched, day):
    if "every_days" in sched:
        delta = (day - date.fromisoformat(sched["anchor"])).days
        return delta >= 0 and delta % sched["every_days"] == 0
    return day.weekday() in sched["weekdays"]


def local_start(day, hhmm):
    """Epoch najwcześniejszego startu: GG:MM tego dnia w Warszawie, liczone w kalendarzu. Godzina,
    której nie ma (zmiana na czas letni), to pierwsza chwila po przeskoku (02:30 -> 03:00 CEST);
    godzina podwójna (zmiana na zimowy) to jej pierwsze wystąpienie (fold 0)."""
    h, m = (int(x) for x in hhmm.split(":"))
    base = datetime(day.year, day.month, day.day, h, m)
    for k in range(0, 121):
        local = base + timedelta(minutes=k)
        ts = local.replace(tzinfo=WARSAW).timestamp()
        if datetime.fromtimestamp(ts, WARSAW).replace(tzinfo=None) == local:
            return ts
    return base.replace(tzinfo=WARSAW).timestamp()


def slots_between(sched, lo, hi):
    """Sloty z początkiem w [lo, hi], od najstarszego. Id slotu to data w Warszawie i numer
    godziny dnia ("2026-10-12/1"): zmiana --at w trakcie slotu zostawia mu id, próby i wynik."""
    day = datetime.fromtimestamp(lo, WARSAW).date() - timedelta(days=1)
    last = datetime.fromtimestamp(hi, WARSAW).date() + timedelta(days=1)
    out = []
    while day <= last:
        if day_matches(sched, day):
            for i, hhmm in enumerate(sched["at"]):
                start = local_start(day, hhmm)
                if lo <= start <= hi:
                    out.append(Slot(f"{day.isoformat()}/{i + 1}", start, hhmm))
        day += timedelta(days=1)
    return out


def next_slot(sched, now):
    since = max(sched.get("since") or 0, sched.get("edited") or 0, now)
    found = slots_between(sched, since + 1, since + 62 * DAY)
    return found[0] if found else None


def warsaw(ts, fmt="%d.%m %H:%M"):
    return datetime.fromtimestamp(ts, WARSAW).strftime(fmt) if ts else "?"


def slot_label(slot_id, start=None):
    """"pn 12.10 05:30" (z godziną z obecnego harmonogramu) albo sam dzień z id."""
    try:
        day = date.fromisoformat(slot_id.split("/")[0])
    except ValueError:
        return slot_id
    text = f"{DAY_NAMES[day.weekday()]} {day:%d.%m}"
    return f"{text} {warsaw(start, '%H:%M')}" if start else text


def retry_at(last, counted, now):
    """Najwcześniejsza następna próba po zapisie last (n = liczba liczonych prób). Koniec "z
    przyszłości" (zegar cofnięty) liczy się jako teraz, żeby slot nie czekał na powrót zegara."""
    if last is None:
        return 0.0
    ended = min(float(last.get("ended_at") or last.get("started_at") or 0), now)
    skip = last.get("skip")
    if skip in ("heavy", "hold"):
        return ended  # bramki tiku same pilnują, kiedy można
    if skip == "memory":
        return ended + MEMORY_RETRY_S
    curve = PAYER_BACKOFF_S if skip == "payer" else BACKOFF_S
    return ended + curve[min(max(counted, 1), len(curve)) - 1]


def slot_key(slot_id):
    """(data, n) dla id slotu tiku ("2026-10-12/1") albo None dla innego tekstu z `--slot`."""
    m = SLOT_RE.fullmatch(slot_id) if isinstance(slot_id, str) else None
    return (m[1], int(m[2])) if m else None


def next_mode(job, recs):
    """--mode dla następnej próby slotu (recs: jego zapisy) albo None (mode joba). Job auto zostaje
    na auto: runenv sam omija wyczerpany kredyt (billing) i konta z avoid.json (429 zapisuje runenv,
    401 na subskrypcji dopisuje tik w spawn()). Wyjątek: ostatnia próba slotu, która płaciła kredytem,
    skończyła się 401 albo limitem; runenv nie ma listy omijanych organizacji i w auto wybrałby tę
    samą organizację znowu, więc reszta slotu płaci subskrypcją. Liczy się ostatni zapis z płatnikiem
    kredytowym, nie ostatni zapis: POMINIĘTO bez płatnika (pamięć, inny ciężki bieg) i próba na
    subskrypcji, która padła z innego powodu, nie gubią przełączenia."""
    if job["mode"] != "auto":
        return None
    paid = next((r for r in reversed(recs) if (r.get("payer") or {}).get("mode") == "credits"), None)
    if paid and paid.get("switch_payment") and paid.get("override") in SWITCH_FROM_CREDITS:
        return "subscription"
    return None


def last_paid(recs):
    """Ostatni zapis slotu z płatnikiem (POMINIĘTO przed wyborem płatnika go nie ma) albo None."""
    return next((r for r in reversed(recs) if r.get("payer")), None)


def slot_views(name, job, records, now, live=None, lo=None):
    """Stan każdego slotu joba od lo (domyślnie 8 dni wstecz), od najstarszego. live: żywa próba
    joba (zapis w toku albo wypuszczona przez tik) albo None. Nic tu nie pisze: przegląd i tik
    liczą to samo."""
    sched = job["schedule"]
    since, edited = sched.get("since") or 0.0, sched.get("edited") or 0.0
    by_slot, outside = {}, []
    for rec in records:
        if slot_key(rec.get("slot")):
            by_slot.setdefault(rec["slot"], []).append(rec)
        elif rec.get("state") == "done" and rec.get("outcome") in CLOSES_BY_HAND:
            outside.append(rec)  # ręczny albo z obcym --slot: zamyka slot tylko efektem albo BEZ WPISU
    lo = now - (HISTORY_DAYS + 1) * DAY if lo is None else lo
    everything = slots_between(sched, lo, now + 2 * DAY)
    # sprzed since i edited tylko sloty z zapisami: wyłączenie i włączenie nie gubi otwartego slotu
    slots = [s for s in everything if s.id in by_slot or max(since, edited) <= s.start <= now]
    kept = {s.id for s in slots}
    views = []
    for s in slots:
        # nowszy slot, który się zaczął: istniejący albo taki, który przepadł, bo job był wyłączony
        # (sprzed since); slot sprzed samej edycji (edited) nie zamyka starszego, ten biegnie dalej
        newer = next((n for n in everything if n.start > s.start and n.start <= now and (n.id in kept or n.start < since)), None)
        later_record = any(slot_key(other) > slot_key(s.id) for other in by_slot)  # zegar cofnięty: nowszy slot już biegł
        recs = by_slot.get(s.id, [])
        until = newer.start if newer else float("inf")
        by_hand = next((r for r in reversed(outside) if s.start <= float(r.get("ended_at") or 0) < until), None)
        views.append(slot_view(job, s, recs, now, newer, later_record, by_hand, live))
    return views


def slot_view(job, s, recs, now, newer, later_record, by_hand, live):
    counted = [r for r in recs if r.get("skip") not in NOT_COUNTED]
    last = recs[-1] if recs else None
    v = {"slot": s.id, "start": s.start, "label": slot_label(s.id, s.start), "attempts": len(counted),
         "records": recs, "last": last, "outcome": None, "reason": "", "next_at": None, "mode": None,
         "closed_at": None}  # fmt: skip
    final = next((r for r in reversed(recs) if r.get("final")), None)
    if final:
        return dict(v, state="done", outcome=final.get("outcome"), reason=final.get("reason") or "")
    if by_hand:
        who = f"bieg spoza tiku (--slot {by_hand['slot']})" if by_hand.get("slot") else "ręcznie"
        return dict(v, state="done", outcome=by_hand.get("outcome"), manual=True,
                    reason=f"{who} {warsaw(by_hand.get('started_at'))}: {by_hand.get('reason') or ''}")  # fmt: skip
    if live and live.get("slot") == s.id:
        return dict(v, state="running", reason=f"biegnie od {warsaw(live.get('started_at'), '%H:%M')}")
    if len(counted) >= MAX_ATTEMPTS:
        return dict(v, state="capped", outcome="BEZ WYNIKU", closed_at=float(last.get("ended_at") or now),
                    reason=f"{len(counted)}/{MAX_ATTEMPTS} prób bez wyniku; ostatnia: {last.get('outcome')}: {last.get('reason')}")  # fmt: skip
    if newer or later_record:
        closed = newer.start if newer else now
        ran = [r for r in recs if r.get("skip") is None]
        if not ran:
            why = f"{last.get('outcome')}: {last.get('reason')}" if last else "Mac spał albo tik nie biegł w czasie slotu"
            return dict(v, state="missed", outcome="BRAK BIEGU", closed_at=closed, reason=why)
        return dict(v, state="dropped", outcome="BEZ WYNIKU", closed_at=closed,
                    reason=f"{len(counted)}/{MAX_ATTEMPTS} prób, potem nowszy slot; ostatnia: {last.get('outcome')}: {last.get('reason')}")  # fmt: skip
    nxt = max(retry_at(last, len(counted), now), s.start)
    mode = next_mode(job, recs)
    if nxt > now:
        tail = f"; ostatnia: {last.get('outcome')}: {last.get('reason')}" if last else ""
        return dict(v, state="backoff", next_at=nxt, mode=mode,
                    reason=f"{len(counted)}/{MAX_ATTEMPTS}, następna próba {warsaw(nxt, '%H:%M')}{tail}")  # fmt: skip
    tail = f"{len(counted)}/{MAX_ATTEMPTS}; ostatnia: {last.get('outcome')}: {last.get('reason')}" if last else "jeszcze bez próby"
    return dict(v, state="due", next_at=nxt, mode=mode, reason=tail)


# ---------- harmonogram: stan systemu ----------


def power_state():
    """"full" (pełne obudzenie), "dark" (DarkWake) albo "unknown". `pmset -g systemstate` wypisuje
    "Current System Capabilities are: CPU Graphics Audio Network"; w DarkWake bez Graphics i Audio
    (pmset -g log: DarkWake [CDNP], Wake [CDNVA]). launchd odpala StartInterval także w DarkWake."""
    tool = shutil.which("pmset") or "/usr/bin/pmset"
    try:
        out = subprocess.run([tool, "-g", "systemstate"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    line = next((ln for ln in out.splitlines() if "Capabilities" in ln and ":" in ln), None)
    if line is None:
        return "unknown"
    return "full" if "Graphics" in line.split(":", 1)[1].split() else "dark"


def slept_between(prev, wall, uptime, boot):
    """Czy od poprzedniego tiku Mac spał albo się restartował (albo poprzedniego tiku nie ma)."""
    try:
        if abs(boot - float(prev["boot"])) > 300 or uptime < float(prev["uptime"]):
            return True
        return (wall - float(prev["wall"])) - (uptime - float(prev["uptime"])) > SLEEP_GAP_TICK_S
    except (KeyError, TypeError, ValueError):
        return True


def awake_age(beat, uptime, boot):
    """Sekundy czuwania od zapisu beat ({uptime, boot}); po restarcie czuwanie od startu systemu."""
    try:
        if abs(boot - float(beat["boot"])) <= 300 and uptime >= float(beat["uptime"]):
            return uptime - float(beat["uptime"])
    except (KeyError, TypeError, ValueError):
        pass
    return uptime


def tick_log(line):
    try:
        os.makedirs(JOBS_DIR, mode=0o700, exist_ok=True)
        if os.path.exists(TICK_LOG) and os.path.getsize(TICK_LOG) > 1024 * 1024:
            with open(TICK_LOG, errors="replace") as f:
                tail = f.readlines()[-6000:]  # linia stanu zasilania co tik: ok. 8 dni
            with open(TICK_LOG, "w") as f:
                f.writelines(tail)
        with open(TICK_LOG, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    except OSError:
        pass


def notify_argv(title, text, tool="osascript"):
    """Wywołanie osascript dla banera. Tekst i tytuł to argumenty skryptu (on run argv), nie jego
    treść: AppleScript nie zna escape'ów \\uXXXX, które robi json.dumps z polskich liter, i nie
    kompiluje takiego skryptu (-2741). "--" kończy opcje osascript, więc tekst z "-" na początku
    też przechodzi."""
    argv = [tool]
    for line in NOTIFY_SCRIPT:
        argv += ["-e", line]
    return argv + ["--", text, title]


def notify(title, text):
    """Baner macOS (osascript z PATH; launchd ma /usr/bin). True, gdy osascript go przyjął; inaczej
    linia w tick.log, a wołający nie oznacza banera jako pokazanego."""
    tool = shutil.which("osascript") or "/usr/bin/osascript"
    try:
        r = subprocess.run(notify_argv(title, text, tool), capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        tick_log(f"baner nie wyszedł ({exc.__class__.__name__}): {title}: {text}")
        return False
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        tick_log(f"baner nie wyszedł (osascript kod {r.returncode}: {err[-1] if err else 'bez komunikatu'}): {title}: {text}")
        return False
    tick_log(f"baner: {title}: {text}")
    return True


def die_at(point):
    """Test: tik zabija się SIGKILL w tym miejscu ($CLAUDE_ACC_JOBS_TICK_DIE)."""
    if os.environ.get("CLAUDE_ACC_JOBS_TICK_DIE") == point:
        os.kill(os.getpid(), signal.SIGKILL)


def live_attempt(name, launched):
    """Żywa próba joba: current.json, którego blokadę ktoś trzyma (runner albo `jobs run` ręczny),
    albo próba, którą tik wypuścił, a która jeszcze nie wzięła blokady. Liczy się blokada, nie pid
    z current.json (pid może należeć już do innego procesu)."""
    path = os.path.join(job_dir(name), "current.json")
    if os.path.isfile(path):
        fd = try_lock(os.path.join(job_dir(name), "lock"))
        if fd is None:
            cur = read_json(path)
            return dict(cur, live="runner") if isinstance(cur, dict) else {"live": "runner"}
        os.close(fd)
    entry = (launched or {}).get(name)
    if isinstance(entry, dict) and pid_alive(entry.get("pid")) and runenv.pid_state(int(entry["pid"]))[1] == entry.get("pid_start"):
        return {"slot": entry.get("slot"), "started_at": entry.get("at"), "pid": entry.get("pid"), "live": "launched"}
    return None


def load_tick_state(wall):
    state = read_json(TICK_STATE)
    if not isinstance(state, dict) or not isinstance(state.get("sent"), dict):
        # nowy albo zepsuty stan: banery tylko za to, co zamknie się od teraz
        state = {"v": 1, "since": wall, "sent": {}, "launched": {}, "waiting": {}, "last": {}}
    for key in ("launched", "waiting", "last"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def evening_of(wall):
    """Ostatni wieczór (20:00 w Warszawie) nie później niż wall: (data, epoch)."""
    local = datetime.fromtimestamp(wall, WARSAW)
    day = local.date() if local.hour >= EVENING_HOUR else local.date() - timedelta(days=1)
    return day.isoformat(), datetime(day.year, day.month, day.day, EVENING_HOUR, tzinfo=WARSAW).timestamp()


def needs_banner(rec):
    """Zapis próby, który sam w sobie woła Filipa: BŁĄD ostateczny, ZAPARKOWANO, PILNE (i banner)."""
    outcome = rec.get("outcome")
    return outcome in OUTCOME_BANNERS or (outcome == "BŁĄD" and rec.get("final")) or bool(rec.get("banner"))


# ---------- harmonogram: tik ----------


def hold_text(hold):
    return f"wstrzymane do {warsaw(float(hold.get('until') or 0))}: {hold.get('reason') or 'bez powodu'}"


def waiting_reason(name, v, waiting):
    """Powód slotu jak w `jobs`: bramka, na której tik go trzyma (należny), i stan prób."""
    w = (waiting or {}).get(name) or {}
    if v["state"] == "due" and w.get("slot") == v["slot"] and w.get("reason"):
        return f"{w['reason']}; {v['reason']}"
    return v["reason"]


def record_label(rec):
    """Gdzie był zapis: slot tiku ("pn 12.10"), obcy --slot (sam tekst) albo bieg ręczny."""
    return slot_label(rec["slot"]) if rec.get("slot") else f"ręcznie {warsaw(rec.get('started_at'))}"


class Tick:
    """Jeden obrót: zegar, stan systemu, sloty każdego joba, banery, co najwyżej jeden start."""

    def __init__(self):
        self.wall, self.uptime, self.boot = tick_clock()
        self.state = load_tick_state(self.wall)
        self.out = {"power": None, "started": [], "banners": [], "waiting": {}, "errors": {}}
        self.notify_failed = False
        self.raw = {}

    def run(self):
        st, wall = self.state, self.wall
        prev = st.get("last") or {}
        power = self.out["power"] = power_state()
        fresh = slept_between(prev, wall, self.uptime, self.boot) or prev.get("power") != "full"
        # co tik: R1 porównuje to przez noc z pmset -g log (DarkWake [CDNP], Wake [CDNVA])
        tick_log(f"tik: zasilanie {power}" + (", świeżo po śnie" if fresh else ""))
        # po długiej nieobecności alarm obejmuje wszystko od ostatniego tiku (najwyżej 31 dni)
        lo = max(wall - 31 * DAY, min(wall - (HISTORY_DAYS + 1) * DAY, float(prev.get("wall") or wall) - DAY))
        st["last"] = {"wall": wall, "uptime": self.uptime, "boot": self.boot, "power": power}
        if power == "dark":
            # DarkWake: Filip nic nie zobaczy, a Mac zaraz zaśnie; tylko znak życia
            self.save()
            self.heartbeat(power, (read_json(HEARTBEAT_PATH) or {}).get("attention") or [])
            return self.out
        try:
            recover(runenv.sweep())
        except Exception as exc:  # noqa: BLE001 - zapis zaległej próby nie może zatrzymać harmonogramu
            tick_log(f"recover: {exc.__class__.__name__}: {exc}")
        self.raw = load_jobs()
        hold = held(wall)
        jobs, views, live, bad = {}, {}, {}, {}
        for name in sorted(self.raw):
            try:  # jeden zły job (definicja, harmonogram, zapisy) nie wycisza pozostałych ani heartbeatu
                live[name] = live_attempt(name, st["launched"])
                job, problem = checked_job(self.raw[name])
                if problem:
                    bad[name] = problem
                    continue
                jobs[name] = job
                if job["schedule"] is not None:
                    views[name] = (job, slot_views(name, job, history(name), wall, live[name], lo))
            except Exception as exc:  # noqa: BLE001
                bad[name] = ("błąd tiku", f"{exc.__class__.__name__}: {exc}")
        for name, (kind, err) in bad.items():
            self.out["errors"][name] = f"{kind}: {err}"
        self.reconcile_hints(jobs, live)
        self.settle_launches(live)
        self.banners(views)
        if power == "full" and not fresh:
            self.start(jobs, views, live, hold)
        else:
            reason = "Mac dopiero się obudził: start od następnego tiku" if power == "full" else \
                "pmset nie mówi, czy Mac nie śpi: bez startu"  # fmt: skip
            self.wait_all(views, reason)
        self.evening(views, bad, hold)
        # klucze starsze niż 9 dni nie mają już czego blokować (zapisy wołają najwyżej BANNER_DAYS)
        for key, at in list(st["sent"].items()):
            if not isinstance(at, (int, float)) or at < wall - 9 * DAY:
                st["sent"].pop(key, None)
        st["waiting"] = self.out["waiting"]
        self.save()
        self.heartbeat(power, attention(views, bad, hold, power, wall))
        return self.out

    def save(self):
        try:
            credits.write_json(TICK_STATE, self.state)
        except OSError as exc:
            tick_log(f"zapis stanu tiku: {exc}")

    def heartbeat(self, power, attn):
        try:
            credits.write_json(HEARTBEAT_PATH, {
                "v": 1, "wall": self.wall, "uptime": self.uptime, "boot": self.boot, "power": power,
                "interval_s": TICK_INTERVAL_S, "stale_after_s": STALE_AFTER_S, "attention": attn,
            })  # fmt: skip
        except OSError as exc:
            tick_log(f"heartbeat: {exc}")

    def reconcile_hints(self, jobs, live):
        """Wskazówka dla automatu kont z current.json żywych biegów na subskrypcji (runner pisze ją
        sam, tik ją odświeża i zdejmuje po biegach, których już nie ma)."""
        live_pids = {}
        for name, cur in live.items():
            payer = (cur or {}).get("payer") or {}
            if cur and cur.get("live") == "runner" and payer.get("mode") == "subscription" and payer.get("email") and cur.get("pid"):
                live_pids[int(cur["pid"])] = (name, payer["email"].lower())
        now = time.time()

        def change(items):
            keep = [e for e in items if pid_alive(e.get("pid"))]
            for e in keep:
                if e.get("pid") in live_pids:
                    e["until"] = max(float(e.get("until") or 0), now + HINT_REFRESH_S)
            known = {e.get("pid") for e in keep}
            for pid, (name, email) in live_pids.items():
                if pid not in known:
                    job = jobs.get(name) or normalized({})  # zły wpis jobs.json: domyślne limity
                    keep.append({"email": email, "job": name, "pid": pid, "since": now, "until": hint_until(job, now)})
            return keep

        edit_hints(change)

    def settle_launches(self, live):
        """Wypuszczona próba: zniknęła z zapisem (gotowe) albo bez (kod 64, 73, awaria: następna
        dopiero po LAUNCH_RETRY_S, żeby nie wołać runnera co tik)."""
        launched = self.state["launched"]
        for name, entry in list(launched.items()):
            if not isinstance(entry, dict) or live.get(name):
                continue
            count = sum(1 for r in history(name) if r.get("slot") == entry.get("slot"))
            if count > int(entry.get("records") or 0) or self.wall - float(entry.get("at") or 0) > LAUNCH_RETRY_S:
                launched.pop(name, None)
            else:
                entry["failed"] = True

    def show(self, keys, title, text):
        """Baner raz na klucz (tick-state.json "sent"). osascript go nie przyjął: bez znaku, więc
        następny tik spróbuje znowu, a reszta banerów tego tiku czeka (osascript może wisieć 20 s)."""
        sent = self.state["sent"]
        if all(k in sent for k in keys):
            return True  # już pokazany (np. tik zabity po banerze, przed resztą zapisu stanu)
        if self.notify_failed:
            return False
        if not notify(title, text):
            self.notify_failed = True
            return False
        self.out["banners"].append({"key": keys[0] if len(keys) == 1 else "miss", "title": title, "text": text})
        die_at("after-banner")
        for k in keys:
            sent[k] = self.wall
        return True

    def banners(self, views):
        """Zapisy, które wołają Filipa, limit prób i sloty zastąpione bez wyniku."""
        st, wall = self.state, self.wall
        since = float(st.get("since") or wall)
        # zapisy prób: BŁĄD ostateczny, ZAPARKOWANO, PILNE ze slotem (tiku albo obcym) i każde PILNE,
        # także ręczne (np. agent, którego sesja zginęła po pushu); także zapisy, które oddał recover().
        # Najwyżej BANNER_DAYS po końcu, czyli zawsze w zasięgu kluczy "sent" (9 dni): baner nie wraca
        floor = max(since, wall - BANNER_DAYS * DAY)
        for name in sorted(self.raw):
            for rec in history(name)[-50:]:
                if float(rec.get("ended_at") or 0) < floor or not needs_banner(rec):
                    continue
                if rec.get("slot") or rec.get("outcome") == "PILNE":
                    extra = f" UWAGA: {rec['banner']}" if rec.get("banner") and rec["banner"] not in (rec.get("reason") or "") else ""
                    links = " ".join(list(rec.get("urls") or []) + ([rec["pr"]] if rec.get("pr") else []))
                    text = f"{record_label(rec)}: {rec.get('reason')}{extra}" + (f" {links}" if links else "")
                    self.show([f"rec:{rec.get('id')}"], f"jobs: {name} {rec.get('outcome')}", text)
        missed = []
        for name, (job, vs) in sorted(views.items()):
            if not job["enabled"]:
                continue
            for v in vs:
                if v["state"] == "capped" and float(v["closed_at"] or 0) >= since:
                    who = "nikt nie zapłacił" if (v["last"] or {}).get("skip") == "payer" else "bez wyniku"
                    self.show([f"cap:{name}:{v['slot']}"], f"jobs: {name} {who} ({v['attempts']}/{MAX_ATTEMPTS})",
                              f"{v['label']}: {v['reason']}")  # fmt: skip
                elif v["state"] in ("missed", "dropped") and float(v["closed_at"] or 0) >= since and f"miss:{name}:{v['slot']}" not in st["sent"]:
                    missed.append((name, v))
        if missed:
            text = "; ".join(f"{name} {v['label']} {v['outcome']}" for name, v in missed)
            first = missed[0][1]["reason"]
            self.show([f"miss:{name}:{v['slot']}" for name, v in missed], "jobs: sloty bez biegu",
                      f"{text} ({first})" if len(missed) == 1 else text)  # fmt: skip

    def evening(self, views, bad, hold):
        """20:00: ostatni wieczór, którego tik jeszcze nie przerobił (Mac spał o 20:00: pierwszy pełny
        tik). Woła po start(), więc linia mówi, czemu slot czeka, tak jak `jobs`."""
        st, wall = self.state, self.wall
        day, evening = evening_of(wall)
        done = st.get("evening_done")
        if done is None:
            st["evening_done"] = day  # pierwszy tik w ogóle: bez alarmu za wieczór sprzed instalacji
            return
        if day <= done:
            return
        # wieczór przerabiany na czas mówi o każdym niedomkniętym slocie; przerabiany po śnie (rano)
        # tylko o slotach, które już próbowały: te bez prób tik właśnie nadrabia
        on_time = datetime.fromtimestamp(wall, WARSAW).date().isoformat() == day
        started = {(s["job"], s["slot"]) for s in self.out["started"]}
        lines = [f"{name}: {kind} ({err})" for name, (kind, err) in sorted(bad.items()) if raw_enabled(self.raw.get(name))]
        for name, (job, vs) in sorted(views.items()):
            if not job["enabled"]:
                continue
            for v in vs:
                if v["state"] in ("due", "backoff") and v["start"] <= evening and (on_time or v["records"]) \
                        and (name, v["slot"]) not in started:  # fmt: skip
                    why = waiting_reason(name, v, self.out["waiting"])
                    if hold and v["state"] == "due" and hold_text(hold) not in why:
                        why += f"; {hold_text(hold)}"
                    lines.append(f"{name} {v['label']}: {why}")
        if lines and not self.show([f"eve:{day}"], f"jobs: bez wyniku ({warsaw(evening, '%d.%m')} 20:00)", "; ".join(lines)):
            return  # osascript nie przyjął: następny tik próbuje znowu
        st["evening_done"] = day

    def wait_all(self, views, reason):
        for name, (job, vs) in views.items():
            if job["enabled"] and any(v["state"] == "due" for v in vs):
                self.out["waiting"][name] = {"slot": next(v["slot"] for v in vs if v["state"] == "due"), "reason": reason}

    def start(self, jobs, views, live, hold):
        """Kolejka należnych slotów (najstarszy slot, potem nazwa joba) i co najwyżej jeden start."""
        queue = sorted(((v["start"], name, v) for name, (job, vs) in views.items() if job["enabled"]
                        for v in vs if v["state"] == "due"), key=lambda q: q[:2])  # fmt: skip
        if not queue:
            return
        heavy = {n: (jobs[n]["heavy"] if n in jobs else True) for n in self.raw}  # zły wpis: jak ciężki
        heavy_busy = next((n for n, cur in live.items() if cur and heavy.get(n, True)), None)
        other = read_json(HEAVY_PATH)
        if not heavy_busy and isinstance(other, dict) and pid_alive(other.get("pid")):
            heavy_busy = other.get("job") or "?"
        last_end, last_job = 0.0, None
        for n in self.raw:
            if not heavy[n]:
                continue
            for r in history(n)[-20:]:
                if r.get("skip") is None and float(r.get("ended_at") or 0) > last_end:
                    last_end, last_job = min(float(r["ended_at"]), self.wall), n  # zegar cofnięty: odstęp od teraz
        chosen = None
        for _start, name, v in queue:
            job = views[name][0]
            entry = self.state["launched"].get(name)
            if live.get(name):
                reason = f"biegnie próba {name} ({live[name].get('slot') or 'ręczna'})"
            elif os.path.isfile(os.path.join(job_dir(name), "current.json")):
                # runner zginął, a recover() jeszcze nie zapisał próby (run.json bez rachunku, sweep w
                # innym procesie): nowa próba nadpisałaby current.json, a martwa przepadłaby bez zapisu
                reason = "poprzednia próba zginęła i czeka na zapis (rachunek runenv); start po nim"
            elif isinstance(entry, dict) and entry.get("failed"):
                reason = f"próba wypuszczona {warsaw(entry.get('at'), '%H:%M')} nie zostawiła zapisu; ponownie po {warsaw(float(entry.get('at') or 0) + LAUNCH_RETRY_S, '%H:%M')}"
            elif hold:
                reason = hold_text(hold)
            elif job["heavy"] and heavy_busy:
                reason = f"biegnie inny ciężki job: {heavy_busy}"
            elif job["heavy"] and self.wall < last_end + STAGGER_S:
                reason = f"odstęp po {last_job} do {warsaw(last_end + STAGGER_S, '%H:%M')}"
            elif chosen is None:
                chosen = (name, v)
                if job["heavy"]:
                    heavy_busy = name
                continue
            else:
                reason = "kolejka: jeden start na tik"
            self.out["waiting"][name] = {"slot": v["slot"], "reason": reason}
        if chosen:
            self.spawn(*chosen)

    def spawn(self, name, v):
        runner = os.environ.get("CLAUDE_ACC_JOBS_RUNNER")
        argv = [runner] if runner else [sys.executable, os.path.join(HERE, "acc.py"), "jobs"]
        argv += ["run", name, "--slot", v["slot"]] + (["--mode", v["mode"]] if v["mode"] else [])
        paid = last_paid(v["records"]) or {}
        payer = paid.get("payer") or {}
        if paid.get("switch_payment") and payer.get("mode") == "subscription" and payer.get("email"):
            # 401 albo 429 na tym koncie (także gdy potem było POMINIĘTO bez płatnika): następna próba
            # nie może wziąć go znowu; runenv omija konta z avoid.json, a sam zapisuje tam tylko 429
            try:
                runenv.avoid(payer["email"], time.time() + 5 * 3600, f"jobs: {paid.get('override')} w próbie {paid.get('id')}")
            except (OSError, credits.CreditsError) as exc:
                tick_log(f"avoid {payer['email']}: {exc}")
        os.makedirs(JOBS_DIR, mode=0o700, exist_ok=True)
        log_fd = os.open(TICK_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # własna sesja: bootout tiku (launchd zabija grupę procesów joba) nie dotyka biegu
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, cwd=HERE,
                                    start_new_session=True)  # fmt: skip
        except OSError as exc:
            tick_log(f"start {name} {v['slot']}: {exc}")
            return
        finally:
            os.close(log_fd)
        die_at("after-spawn")
        count = len(v["records"])
        self.state["launched"][name] = {"slot": v["slot"], "pid": proc.pid, "pid_start": runenv.pid_state(proc.pid)[1],
                                        "at": self.wall, "records": count}  # fmt: skip
        tick_log(f"start {name} slot {v['slot']} próba {v['attempts'] + 1}" + (f" (--mode {v['mode']})" if v["mode"] else "") + f", pid {proc.pid}")
        self.out["started"].append({"job": name, "slot": v["slot"], "pid": proc.pid, "mode": v["mode"]})


def attention(views, bad, hold, power, wall):
    """Linie dla Filipa (status, panel): to, co z ostatnich 48 h wymaga jego ruchu, i stan tiku."""
    out = [f"{name}: {kind}" for name, (kind, _err) in sorted(bad.items())]
    for name, (job, vs) in sorted(views.items()):
        if not job["enabled"]:
            continue
        for v in vs:
            if v["start"] < wall - 2 * DAY:
                continue
            if v["state"] in ("capped", "missed", "dropped") or (v["state"] == "done" and (v["outcome"] in OUTCOME_BANNERS or v["outcome"] == "BŁĄD")):
                out.append(f"{name} {v['label']} {v['outcome']}")
    if hold:
        out.append(f"wstrzymane do {warsaw(float(hold.get('until') or 0))}")
    if power == "unknown":
        out.append("pmset nie mówi, czy Mac nie śpi: tik nie startuje biegów")
    return out


def heartbeat_view():
    """(heartbeat albo None, sekundy czuwania od niego albo None, czy są włączone joby z harmonogramem)."""
    jobs = load_jobs()
    scheduled = any(isinstance(j, dict) and j.get("schedule") is not None and j.get("enabled", True) for j in jobs.values())
    beat = read_json(HEARTBEAT_PATH)
    if not isinstance(beat, dict):
        return None, None, scheduled
    _wall, uptime, boot = tick_clock()
    return beat, awake_age(beat, uptime, boot), scheduled


def heartbeat_text():
    """Opis tiku dla `claude-acc status` i `jobs`, z flagą "stoi": (tekst, stoi) albo (None, False)."""
    beat, age, scheduled = heartbeat_view()
    if beat is None:
        return ("tik jeszcze nie biegł (launchd com.filip.claude-acc.jobs)", True) if scheduled else (None, False)
    limit = float(beat.get("stale_after_s") or STALE_AFTER_S)
    at = warsaw(float(beat.get("wall") or 0), "%d.%m %H:%M")
    if age > limit:
        return f"tik stoi od {at} ({duration(age)} czuwania bez tiku; launchctl print gui/$UID/com.filip.claude-acc.jobs)", True
    power = {"dark": ", DarkWake", "unknown": ", pmset nie odpowiada"}.get(beat.get("power"), "")
    return f"tik {warsaw(float(beat.get('wall') or 0), '%H:%M')}{power}", False


def status_line():
    """Jedna linia jobów dla `claude-acc status`: świeżość tiku, konta trzymane przez biegi i to, co
    czeka na Filipa. Same pliki: bez recover, sieci i blokad. None, gdy jobów z harmonogramem nie ma."""
    text, stale = heartbeat_text()
    beat = read_json(HEARTBEAT_PATH)
    data = read_json(HELD_PATH)
    now, holding = time.time(), []
    for e in (data.get("accounts") if isinstance(data, dict) else None) or []:
        try:
            if float(e["until"]) > now and pid_alive(e.get("pid")):
                holding.append(f"{e['email']} trzyma {e.get('job')}")
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if text is None and not holding:
        return None
    parts = [("UWAGA, " if stale else "") + (text or "bez harmonogramu")] + holding
    attn = (beat or {}).get("attention") if isinstance(beat, dict) else None
    if attn:
        parts.append("do sprawdzenia: " + ", ".join(attn[:3]) + (f" (+{len(attn) - 3})" if len(attn) > 3 else ""))
    return "joby: " + "; ".join(parts) + " (claude-acc jobs)"


def heartbeat_alarm():
    """Baner, gdy tik jobów stoi (heartbeat bez świeżego zapisu przez stale_after_s czuwania), raz na
    epizod. Woła go automat kont (accswitch cmd_tick, co 2 min): proces inny niż tik. Tylko w pełnym
    obudzeniu: czuwanie rośnie też w nocnych DarkWake, a baner tam nikt nie zobaczy, więc epizod
    zostaje nieoznaczony do pierwszego pełnego obudzenia; tak samo, gdy osascript banera nie przyjął.
    Tylko pliki i pmset, bez sieci i recover; nigdy nie rzuca."""
    try:
        beat, age, scheduled = heartbeat_view()
        if not scheduled:
            return
        if beat is not None and age <= float(beat.get("stale_after_s") or STALE_AFTER_S):
            return
        episode = str((beat or {}).get("wall") or "brak")
        seen = read_json(STALE_ALARM_PATH)
        if isinstance(seen, dict) and seen.get("episode") == episode:
            return
        if power_state() != "full":
            return
        # znak przed banerem: dwa automaty naraz nie pokażą go dwa razy; nieudany baner go cofa
        credits.write_json(STALE_ALARM_PATH, {"episode": episode, "at": time.time()})
        text, _stale = heartbeat_text()
        if not notify("jobs: harmonogram stoi", f"{text}; blogi nie wystartują same"):
            if isinstance(seen, dict):
                credits.write_json(STALE_ALARM_PATH, seen)
            else:
                os.unlink(STALE_ALARM_PATH)
    except Exception:  # noqa: BLE001 - alarm nie może zatrzymać automatu kont
        pass


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
    job["schedule"] = check_schedule(job["schedule"])
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
    mode = credits.flag(args, "--mode")
    if len(args) != 1:
        raise credits.UsageError("usage: claude-acc jobs run NAZWA [--slot S] [--mode auto|credits|subscription] [--json]")
    name = args[0]
    job = load_job(name)
    if not job["enabled"]:
        raise credits.UsageError(f"job {name} jest wyłączony; włącz: claude-acc jobs enable {name}")
    if mode is not None:
        if mode not in runenv.MODES:
            raise credits.UsageError("--mode: auto, credits albo subscription")
        job = dict(job, mode=mode)  # jednorazowo: tik przełącza źródło płatności po switch_payment
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
    sched = schedule_flags(args, None)
    job = parse_job_options(args)
    credits.no_leftovers(args)
    if "cwd" in job:
        job["cwd"] = os.path.abspath(os.path.expanduser(job["cwd"]))
    if sched is not None:
        job["schedule"] = dict(sched, since=wall_now(), edited=wall_now())
    job = validate(name, job)
    save_job(name, job, new=True)
    print(describe_job(name, job))
    print_next_slot(job)
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
        before = normalized(jobs[name])
        job = normalized(jobs[name])
        sched = schedule_flags(args, job.get("schedule"))
        if sched is not None:
            job["schedule"] = sched
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
        job = validate(name, stamped(before, job))
        jobs[name] = job
        credits.write_json(JOBS_PATH, {"jobs": jobs})
    print(describe_job(name, job))
    print_next_slot(job)
    return 0


def schedule_flags(args, old):
    """Harmonogram z flag --every/--from/--days/--at (zdejmuje je z args) albo None bez nich.
    --every bez --from zostawia dotychczasową kotwicę (parzystość dni blogów), --at sam zmienia
    tylko godziny."""
    every, start, days, at = (credits.flag(args, f) for f in ("--every", "--from", "--days", "--at"))
    if every is None and start is None and days is None and at is None:
        return None
    if every is not None and days is not None:
        raise credits.UsageError(f"--every albo --days, nie oba; {SCHEDULE_HELP}")
    try:
        base = dict(check_schedule(old)) if old is not None else {}
    except credits.UsageError:
        base = {}  # zły harmonogram sprzed etapu A2b: flagi budują go od nowa
    if every is not None:
        m = re.fullmatch(r"(\d{1,2})\s*d?", every.strip().lower())
        if not m or not 1 <= int(m[1]) <= 60:
            raise credits.UsageError(f"--every {every!r}: od 1 do 60 dni, np. --every 2d; {SCHEDULE_HELP}")
        if "every_days" not in base:
            base.pop("weekdays", None)
            base["anchor"] = datetime.fromtimestamp(wall_now(), WARSAW).date().isoformat()
        base["every_days"] = int(m[1])
    elif days is not None:
        base.pop("every_days", None)
        base.pop("anchor", None)
        base["weekdays"] = parse_days(days)
    if start is not None:
        if "every_days" not in base:
            raise credits.UsageError(f"--from tylko z --every (kotwica co N dni); {SCHEDULE_HELP}")
        try:
            base["anchor"] = date.fromisoformat(start.strip()).isoformat()
        except ValueError:
            raise credits.UsageError(f"--from {start!r}: data RRRR-MM-DD; {SCHEDULE_HELP}")
    if at is not None:
        base["at"] = [parse_hhmm(t) for t in at.split(",") if t.strip()] or [parse_hhmm(at)]
    if "at" not in base or ("every_days" not in base and "weekdays" not in base):
        raise credits.UsageError(SCHEDULE_HELP)
    return check_schedule(base)


def stamped(before, job):
    """Chwile harmonogramu: since (sloty sprzed niej nie istnieją) przy pierwszym harmonogramie i
    przy włączeniu joba; edited (sloty sprzed niej żyją tylko, gdy mają już próby) przy zmianie
    harmonogramu. Edycja ani `jobs enable` nie odpalają więc slotu z przeszłości."""
    new = job.get("schedule")
    if not isinstance(new, dict):
        return job
    old = before.get("schedule") if isinstance(before.get("schedule"), dict) else None
    now = wall_now()
    core = lambda s: {k: v for k, v in (s or {}).items() if k not in ("since", "edited")}  # noqa: E731
    new = dict(new)
    if old is None or (job.get("enabled", True) and not before.get("enabled", True)):
        new.update(since=now, edited=now)
    else:
        new["since"] = old.get("since", now) if not isinstance(old.get("since"), bool) else now
        new["edited"] = now if core(new) != core(old) else old.get("edited", new["since"])
    return dict(job, schedule=new)


def print_next_slot(job):
    sched = job.get("schedule")
    if not job.get("enabled") or not isinstance(sched, dict):
        return
    nxt = next_slot(sched, wall_now())
    if nxt:
        print(f"harmonogram: {schedule_text(sched)}; następny slot: {slot_label(nxt.id, nxt.start)}")


def cmd_enable(args, enabled=True):
    if len(args) != 1:
        raise credits.UsageError(f"usage: claude-acc jobs {'enable' if enabled else 'disable'} NAZWA")
    return cmd_set([args[0], f"enabled={'true' if enabled else 'false'}"])


def cmd_disable(args):
    return cmd_enable(args, enabled=False)


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
    now = wall_now()
    until = now + hours * 3600
    with Edit():
        credits.write_json(HOLD_PATH, {"until": until, "reason": reason, "at": now})
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


def cmd_tick(args):
    as_json = credits.switch(args, "--json")
    credits.no_leftovers(args)
    fd = try_lock(TICK_LOCK)
    if fd is None:
        print("inny tik jobów właśnie trwa; ten kończy bez zmian", file=sys.stderr)
        return 0
    try:
        out = Tick().run()
    finally:
        os.close(fd)
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def overview(wall):
    """Przegląd dla `jobs` (i --json): sloty 7 dni każdego joba liczone z harmonogramu i historii
    (bez tiku, bez recover i sieci), ręczne biegi, tik, wstrzymanie, konta trzymane przez biegi."""
    state = read_json(TICK_STATE)
    launched = state.get("launched") if isinstance(state, dict) and isinstance(state.get("launched"), dict) else {}
    waiting = state.get("waiting") if isinstance(state, dict) and isinstance(state.get("waiting"), dict) else {}
    items = []
    for name, raw in sorted(load_jobs().items()):
        job, problem = checked_job(raw)
        records = history(name)
        live = live_attempt(name, launched)
        shown = job or {"enabled": raw_enabled(raw), "heavy": None, "mode": None, "schedule": None}
        item = {"name": name, "enabled": shown["enabled"], "heavy": shown["heavy"], "mode": shown["mode"], "schedule": None,
                "error": problem[1] if problem else None, "error_kind": problem[0] if problem else None, "next": None,
                "slots": [], "manual": [], "running": live}  # fmt: skip
        sched = shown["schedule"]
        if sched is not None:
            item["schedule"] = schedule_text(sched)
            nxt = next_slot(sched, wall)
            item["next"] = {"slot": nxt.id, "start": nxt.start, "label": slot_label(nxt.id, nxt.start)} if nxt else None
            if job["enabled"]:
                for v in slot_views(name, job, records, wall, live):
                    v["reason"] = waiting_reason(name, v, waiting)
                    v["records"] = len(v["records"])
                    item["slots"].append(v)
        # biegi spoza tiku: ręczne (slot null) i z obcym tekstem w --slot
        item["manual"] = [r for r in records if not slot_key(r.get("slot")) and float(r.get("started_at") or 0) >= wall - HISTORY_DAYS * DAY]
        items.append(item)
    text, stale = heartbeat_text()
    if stale:
        for item in items:
            for v in item["slots"]:
                if v["state"] in ("due", "backoff"):
                    v["reason"] = f"tik stoi, sam nie wystartuje; {v['reason']}"
    return {"tick": {"text": text, "stale": stale}, "hold": held(wall), "jobs": items}


STATE_TEXT = {"done": None, "running": "W TOKU", "capped": "BEZ WYNIKU", "missed": "BRAK BIEGU", "dropped": "BEZ WYNIKU",
              "backoff": "PONOWIENIE", "due": "CZEKA"}  # fmt: skip


def cmd_overview(args):
    as_json = credits.switch(args, "--json")
    credits.no_leftovers(args)
    wall = wall_now()
    data = overview(wall)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    tick, hold = data["tick"], data["hold"]
    head = f"Joby: {'UWAGA, ' if tick['stale'] else ''}{tick['text'] or 'tik jeszcze nie biegł'}"
    if hold:
        head += f"; {hold_text(hold)} (claude-acc jobs release)"
    print(head)
    if not data["jobs"]:
        print("nie ma jobów; dodaj: claude-acc jobs add NAZWA --cwd KATALOG --entry 'KOMENDA' --budget-usd N --every 2d --at 05:30")
    for item in data["jobs"]:
        name = item["name"]
        if item["error"]:
            print(f"{name}: {item['error_kind'].upper()}: {item['error']}")
        elif not item["enabled"]:
            print(f"{name}: WYŁĄCZONY" + (f" ({item['schedule']}); włącz: claude-acc jobs enable {name}" if item["schedule"] else ""))
        elif item["schedule"] is None:
            print(f"{name}: bez harmonogramu (tylko ręcznie: claude-acc jobs run {name})")
        else:
            nxt = f"; następny slot: {item['next']['label']}" if item["next"] else ""
            print(f"{name}: {item['schedule']}{nxt}")
        for v in reversed(item["slots"]):
            word = STATE_TEXT[v["state"]] or v["outcome"]
            line = f"  {v['label']}  {word}"
            last = v["last"] or {}
            if v["state"] == "done" and not v.get("manual"):
                title = f" {last.get('title')}" if last.get("title") else ""
                links = " ".join(list(last.get("urls") or []) + ([last["pr"]] if last.get("pr") else []))
                cost = f", {runenv.usd4(last['cost_usd'])}" if last.get("cost_usd") is not None else ""
                line += f"{title}  {links}".rstrip() + f"  ({v['attempts']}/{MAX_ATTEMPTS}, {payer_text(last)}{cost})"
                if last.get("outcome") in ("BEZ WPISU", "BŁĄD", "PILNE", "ZAPARKOWANO"):
                    line += f": {last.get('reason')}"
            else:
                line += f": {v['reason']}"
            print(line)
        for rec in reversed(item["manual"]):
            who = f"spoza tiku (--slot {rec['slot']})" if rec.get("slot") else "ręcznie"
            print(f"  {warsaw(rec.get('started_at'))} {who}: {rec.get('outcome') or 'w toku'}: {rec.get('reason')}")
        cur = item["running"]
        if cur and not any(v["state"] == "running" for v in item["slots"]):
            print(f"  W TOKU od {warsaw(cur.get('started_at'), '%H:%M')} ({cur.get('slot') or 'ręcznie'}, faza {cur.get('phase', 'start')})")
    return 0


COMMANDS = {"run": cmd_run, "list": cmd_list, "log": cmd_log, "add": cmd_add, "set": cmd_set,
            "remove": cmd_remove, "hold": cmd_hold, "release": cmd_release, "tick": cmd_tick,
            "enable": cmd_enable, "disable": cmd_disable}  # fmt: skip


def main(argv):
    if not argv or argv[0] in ("--json", "status"):
        try:
            return cmd_overview([a for a in argv if a != "status"])
        except credits.UsageError as exc:
            print(f"błąd: {exc}", file=sys.stderr)
            return USAGE
    if argv[0] not in COMMANDS:
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
