"""Testy harmonogramu `claude-acc jobs` (jobs.py, etap A2b): tik, sloty, ponowienia, alarmy.

Szew: `jobs tick` jako proces z zegarem z pliku ($CLAUDE_ACC_JOBS_NOW: ściana, CLOCK_UPTIME_RAW i
start systemu), atrapą `pmset` (tests/fakes-jobs/pmset: full, dark, fail) i atrapą runnera A2a
(tests/fakes-jobs/runner na $CLAUDE_ACC_JOBS_RUNNER: blokada joba, current.json, zapis schematu v1
z polem slot, wynik z planu). Banery łapie atrapa osascript (tests/fakes-jobs/osascript), surowa
jak AppleScript: skrypt z \\uXXXX w literale nie kompiluje się i baner nie wychodzi. Tik biegnie z
TZ=America/New_York: harmonogram liczy Warszawę jawnie, nie strefę procesu.

Daty i godziny oczekiwane są liczone ręcznie z kalendarza (09.10.2026 to piątek) i reguł strefy
Europe/Warsaw (CEST +2 do 25.10.2026 01:00 UTC, potem CET +1 do 28.03.2027 01:00 UTC), funkcją
waw() poniżej, bez zoneinfo i bez kodu jobs.py. Scenariusze S1..S31: briefs/A2b-scenarios.md.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_jobs_schedule
"""

import calendar
import fcntl
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JOBS = os.path.join(ROOT, "jobs.py")
FAKES = os.path.join(HERE, "fakes")
FAKES_JOBS = os.path.join(HERE, "fakes-jobs")
PY = os.environ.get("CLAUDE_ACC_TEST_PYTHON") or "/usr/bin/python3"
MIN = 60
HOUR = 3600


def utc(y, mo, d, h=0, mi=0):
    return float(calendar.timegm((y, mo, d, h, mi, 0)))


def waw(y, mo, d, h=0, mi=0):
    """Chwila godziny h:mi w Warszawie, z reguł strefy policzonych ręcznie (godziny jednoznaczne)."""
    t = utc(y, mo, d, h, mi) - 2 * HOUR
    if t >= utc(2026, 10, 25, 1):
        t = utc(y, mo, d, h, mi) - HOUR
    if t >= utc(2027, 3, 28, 1):
        t = utc(y, mo, d, h, mi) - 2 * HOUR
    return t


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


class World:
    """$HOME z jobami, zegarem i atrapami; sprząta się sam (addCleanup w Base)."""

    def __init__(self, start):
        self.home = tempfile.mkdtemp(prefix="jobs-tick-test-")
        self.fake = os.path.join(self.home, "fake")
        self.state = os.path.join(self.home, ".local/share/claude-acc")
        self.jobs_dir = os.path.join(self.state, "jobs")
        os.makedirs(self.fake)
        os.makedirs(self.jobs_dir)
        self.now_path = os.path.join(self.fake, "now.json")
        self.wall, self.uptime, self.boot = start, 200000.0, 1000.0
        self.save_clock()
        self.power("full")
        self.runners = []

    # --- zegar i zasilanie ---

    def save_clock(self):
        with open(self.now_path, "w") as f:
            json.dump({"wall": self.wall, "uptime": self.uptime, "boot": self.boot}, f)

    def at(self, wall, awake=True, slept=None):
        """Zegar na wall: Mac czuwał cały ten czas (awake), spał (czuwanie stoi) albo spał slept
        sekund z tego czasu (seria krótkich DarkWake)."""
        if slept is not None:
            self.uptime += max(0.0, wall - self.wall - slept)
        else:
            self.uptime += max(0.0, wall - self.wall) if awake else 5.0
        self.wall = wall
        self.save_clock()

    def power(self, state):
        with open(os.path.join(self.fake, "pmset"), "w") as f:
            f.write(state)

    # --- jobs ---

    def env(self, **extra):
        env = {"HOME": self.home, "USER": "tester", "PATH": f"{FAKES_JOBS}:{FAKES}:/usr/bin:/bin:/usr/sbin:/sbin",
               "CLAUDE_ACC_JOBS_NOW": self.now_path, "CLAUDE_ACC_JOBS_RUNNER": os.path.join(FAKES_JOBS, "runner"),
               "TZ": "America/New_York"}  # fmt: skip
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def jobs(self, *args, ok=True, **extra):
        r = subprocess.run([PY, JOBS, *args], env=self.env(**extra), capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)  # fmt: skip
        if ok:
            assert r.returncode == 0, (args, r.stdout, r.stderr)
        return r

    def add(self, name, *schedule, light=False, disabled=False):
        args = ["add", name, "--cwd", self.home, "--entry", "true", "--budget-usd", "2", *schedule]
        args += (["--light"] if light else []) + (["--disabled"] if disabled else [])
        return self.jobs(*args)

    def raw_jobs(self):
        with open(os.path.join(self.jobs_dir, "jobs.json")) as f:
            return json.load(f)["jobs"]

    def write_jobs(self, jobs):
        with open(os.path.join(self.jobs_dir, "jobs.json"), "w") as f:
            json.dump({"jobs": jobs}, f)

    def plan(self, name, *steps):
        path = os.path.join(self.fake, "plan.json")
        plan = json.load(open(path)) if os.path.exists(path) else {}
        plan.setdefault(name, []).extend(steps)
        with open(path, "w") as f:
            json.dump(plan, f)

    def gate(self, name):
        open(os.path.join(self.fake, f"gate-{name}"), "w").close()

    def open_gate(self, name):
        os.remove(os.path.join(self.fake, f"gate-{name}"))
        self.wait_runners()

    def tick(self, wait=True, **extra):
        r = self.jobs("tick", "--json", ok=False, **extra)
        if r.returncode != 0 or not r.stdout.strip():
            return {"exit": r.returncode, "stderr": r.stderr, "started": [], "banners": []}
        out = json.loads(r.stdout)
        self.runners += [s["pid"] for s in out["started"]]
        if wait:
            self.wait_runners()
        return out

    def wait_runners(self, timeout=15):
        deadline = time.time() + timeout
        for pid in self.runners:
            while pid_alive(pid) and time.time() < deadline:
                if self.gated(pid):
                    break
                time.sleep(0.02)

    def gated(self, pid):
        for call in self.calls(busy=True):
            if call["pid"] == pid and os.path.exists(os.path.join(self.fake, f"gate-{call['job']}")):
                return True
        return False

    def wake(self, wall):
        """Mac spał do wall: pierwszy pełny tik tylko patrzy, drugi (2 min później) może startować."""
        self.at(wall, awake=False)
        first = self.tick()
        self.at(wall + 2 * MIN)
        return first, self.tick()

    def every(self, start, end, step=2 * MIN):
        """Ticki co step od start do end włącznie, Mac obudzony."""
        outs, t = [], start
        while t <= end:
            self.at(t)
            outs.append(self.tick())
            t += step
        return outs

    def calls(self, name=None, busy=False):
        path = os.path.join(self.fake, "runner-calls.jsonl")
        rows = [json.loads(x) for x in open(path).read().splitlines()] if os.path.exists(path) else []
        return [r for r in rows if (busy or not r.get("busy")) and (name is None or r["job"] == name)]

    def notes(self, name="notify.log"):
        """Banery, które atrapa osascript przyjęła ({title, text}); notify-failed.log: odrzucone."""
        path = os.path.join(self.fake, name)
        return [json.loads(x) for x in open(path).read().splitlines() if x.strip()] if os.path.exists(path) else []

    def banners(self, needle=""):
        lines = [f"{n.get('title')}: {n.get('text')}" for n in self.notes()]
        return [x for x in lines if needle in x]

    def tick_log(self):
        path = os.path.join(self.jobs_dir, "tick.log")
        return open(path).read() if os.path.exists(path) else ""

    def history(self, name):
        path = os.path.join(self.jobs_dir, name, "history.jsonl")
        return [json.loads(x) for x in open(path).read().splitlines() if x.strip()] if os.path.exists(path) else []

    def overview(self):
        return json.loads(self.jobs("--json").stdout)

    def slots(self, name):
        return {s["slot"]: s for s in next(j for j in self.overview()["jobs"] if j["name"] == name)["slots"]}

    def kill_all(self):
        for pid in self.runners:
            if pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


class Base(unittest.TestCase):
    def world(self, start):
        w = World(start)
        self.addCleanup(shutil.rmtree, w.home, True)
        self.addCleanup(w.kill_all)
        return w

    def slots_of(self, calls):
        return [c["slot"] for c in calls]


EVERY2_ODD = ("--every", "2d", "--from", "2026-10-10", "--at", "05:30")  # outofplace, matura: 10, 12, 14 ...
EVERY2_EVEN = ("--every", "2d", "--from", "2026-10-11", "--at", "05:30")  # renggli: 11, 13 ... 23, 25, 27
MONDAY = ("--days", "pn", "--at", "05:30")  # agromalz: 12, 19, 26.10


class Calendar(Base):
    """Sloty z kalendarza w Warszawie, nie co 48 h i nie ze strefy procesu (brief, S21)."""

    def test_every_two_days_fires_at_0530_warsaw_across_the_dst_end(self):
        w = self.world(waw(2026, 10, 22, 12))
        w.add("renggli", *EVERY2_EVEN)
        moments = [  # (chwila UTC, oczekiwany nowy slot albo None); 23.10 CEST, 25 i 27.10 CET
            (utc(2026, 10, 23, 3, 25), None), (utc(2026, 10, 23, 3, 35), "2026-10-23/1"),
            (utc(2026, 10, 24, 5, 0), None),
            (utc(2026, 10, 25, 3, 45), None),  # 04:45 CET: 48 h po 23.10 03:30Z, ale jeszcze nie 05:30
            (utc(2026, 10, 25, 4, 31), "2026-10-25/1"),
            (utc(2026, 10, 26, 4, 31), None), (utc(2026, 10, 27, 4, 25), None), (utc(2026, 10, 27, 4, 31), "2026-10-27/1"),
        ]  # fmt: skip
        w.at(moments[0][0] - 2 * MIN)
        w.tick()  # pierwszy tik w ogóle tylko patrzy
        for moment, slot in moments:
            before = len(w.calls())
            w.at(moment)
            w.tick()
            new = self.slots_of(w.calls()[before:])
            self.assertEqual(new, [slot] if slot else [], f"tik {time.strftime('%d.%m %H:%M', time.gmtime(moment))}Z")
            if slot == "2026-10-25/1":
                self.assertEqual(w.slots("renggli")[slot]["label"], "nd 25.10 05:30")

    def test_weekly_monday_and_night_slots_use_the_warsaw_calendar_day(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("agromalz", *MONDAY)
        w.add("tue", "--days", "wt", "--at", "00:30", light=True)
        cases = [
            (utc(2026, 10, 12, 3, 25), []), (utc(2026, 10, 12, 3, 33), [("agromalz", "2026-10-12/1")]),
            (utc(2026, 10, 12, 21, 0), []),  # pn 23:00 w Warszawie: wtorek jeszcze się nie zaczął
            (utc(2026, 10, 12, 22, 35), [("tue", "2026-10-13/1")]),
            ("disable tue", None),
            (utc(2026, 10, 19, 3, 33), [("agromalz", "2026-10-19/1")]),
            (utc(2026, 10, 26, 3, 33), []),  # 04:33 CET: 7 × 24 h od 19.10 03:30Z, a slot to 05:30
            (utc(2026, 10, 26, 4, 33), [("agromalz", "2026-10-26/1")]),
        ]  # fmt: skip
        w.at(cases[0][0] - 2 * MIN)
        w.tick()
        for moment, want in cases:
            if want is None:
                w.jobs("disable", "tue")  # bez tików między wtorkami wtorek nadrabiałby się w poniedziałek
                continue
            before = len(w.calls())
            w.at(moment)
            w.tick()
            got = [(c["job"], c["slot"]) for c in w.calls()[before:]]
            self.assertEqual(got, want, time.strftime("%d.%m %H:%MZ", time.gmtime(moment)))

    def test_a_doubled_hour_runs_once_and_a_missing_hour_runs_right_after_the_jump(self):
        # 25.10.2026 02:30 jest dwa razy (00:30Z CEST, 01:30Z CET); 28.03.2027 02:30 nie ma wcale
        w = self.world(waw(2026, 10, 24, 12))
        w.add("night", "--days", "nd", "--at", "02:30", light=True)
        w.at(utc(2026, 10, 25, 0, 20))
        w.tick()
        got = {}
        for moment in (utc(2026, 10, 25, 0, 25), utc(2026, 10, 25, 0, 35), utc(2026, 10, 25, 1, 35), utc(2026, 10, 25, 1, 45)):
            w.at(moment)
            w.tick()
            got[moment] = len(w.calls())
        self.assertEqual(list(got.values()), [0, 1, 1, 1])
        spring = self.world(waw(2027, 3, 27, 12))
        spring.add("night", "--days", "nd", "--at", "02:30", light=True)
        spring.at(utc(2027, 3, 28, 0, 50))
        spring.tick()
        spring.at(utc(2027, 3, 28, 0, 55))  # 01:55 CET: przed skokiem
        spring.tick()
        self.assertEqual(spring.calls(), [])
        spring.at(utc(2027, 3, 28, 1, 5))  # 03:05 CEST: 02:30 nie było, slot zaczął się o 03:00
        spring.tick()
        self.assertEqual(self.slots_of(spring.calls()), ["2027-03-28/1"])

    def test_two_slots_a_day_keep_attempts_and_finality_apart(self):
        # S30: 06:00 wyczerpuje 3 próby, 18:00 ma własne; sen 05:00-19:00 następnego dnia daje jeden bieg
        w = self.world(waw(2026, 10, 11, 20))
        w.add("twice", "--every", "1d", "--at", "06:00,18:00")
        fail = {"outcome": "BŁĄD", "retryable": True, "reason": "precheck: sieć"}
        w.plan("twice", fail, fail, fail, {"outcome": "OPUBLIKOWANO"}, {"outcome": "OPUBLIKOWANO"})
        w.every(waw(2026, 10, 12, 5, 58), waw(2026, 10, 12, 6, 2))
        w.at(waw(2026, 10, 12, 7, 3))
        w.tick()  # 1 h po pierwszej
        w.at(waw(2026, 10, 12, 10, 5))
        w.tick()  # 3 h po drugiej
        w.every(waw(2026, 10, 12, 10, 7), waw(2026, 10, 12, 10, 9))
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-12/1"] * 3)
        self.assertEqual(len(w.banners("3/3")), 1)
        w.every(waw(2026, 10, 12, 17, 58), waw(2026, 10, 12, 18, 2))
        self.assertEqual(self.slots_of(w.calls())[3:], ["2026-10-12/2"])
        slots = w.slots("twice")
        self.assertEqual((slots["2026-10-12/1"]["outcome"], slots["2026-10-12/2"]["outcome"]), ("BEZ WYNIKU", "OPUBLIKOWANO"))
        w.at(waw(2026, 10, 13, 5, 0))
        w.tick()
        w.wake(waw(2026, 10, 13, 19, 0))
        self.assertEqual(self.slots_of(w.calls())[4:], ["2026-10-13/2"])
        self.assertEqual(w.slots("twice")["2026-10-13/1"]["outcome"], "BRAK BIEGU")


class SleepAndWake(Base):
    """S8, S20, S29: DarkWake nie startuje i nie alarmuje; po śnie jeden bieg na slot, bez spamu."""

    def morning(self):
        w = self.world(waw(2026, 10, 11, 12))
        for name, sched in (("outofplace", EVERY2_ODD), ("matura", EVERY2_ODD), ("agromalz", MONDAY)):
            w.add(name, *sched)
        w.every(waw(2026, 10, 12, 0, 26), waw(2026, 10, 12, 0, 30))
        return w

    def test_darkwake_never_starts_and_first_full_wake_catches_up_one_run_silently(self):
        w = self.morning()
        w.gate("agromalz")
        w.plan("agromalz", {"gate": True})
        w.power("dark")
        for moment in (waw(2026, 10, 12, 3, 12), waw(2026, 10, 12, 5, 41), waw(2026, 10, 12, 6, 20)):
            w.at(moment, awake=False)
            out = w.tick()
            self.assertEqual((out["power"], out["started"], out["banners"]), ("dark", [], []))
        state = json.load(open(os.path.join(w.jobs_dir, "tick-state.json")))
        self.assertEqual(state["sent"], {})  # w DarkWake nic nie jest "pokazane"
        w.power("full")
        first, second = w.wake(waw(2026, 10, 12, 8, 11))
        self.assertEqual(first["started"], [])  # pierwszy pełny tik po śnie: start dopiero od następnego
        self.assertEqual([(s["job"], s["slot"]) for s in second["started"]], [("agromalz", "2026-10-12/1")])
        self.assertEqual(w.banners(), [])  # nadrabianie to nie "brak biegu"
        states = {j["name"]: [s["state"] for s in j["slots"]] for j in w.overview()["jobs"]}
        self.assertEqual(states, {"agromalz": ["running"], "matura": ["due"], "outofplace": ["due"]})
        text = w.jobs().stdout
        self.assertEqual((text.count("W TOKU"), text.count("CZEKA")), (1, 2), text)
        self.assertIn("biegnie inny ciężki job: agromalz", text)

    def test_unknown_power_state_never_starts(self):
        w = self.morning()
        w.power("fail")
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 10))
        self.assertEqual(w.calls(), [])
        self.assertIn("pmset", w.jobs().stdout)

    def test_short_lid_glances_do_not_burn_attempts(self):
        # S20: 40 s otwarcia o 07:00, 07:30, 08:00 (jeden tik każde), potem dzień od 09:00
        w = self.morning()
        for moment in (waw(2026, 10, 12, 7, 0), waw(2026, 10, 12, 7, 30), waw(2026, 10, 12, 8, 0)):
            w.at(moment, awake=False)
            w.tick()
        self.assertEqual(w.calls(), [])
        w.wake(waw(2026, 10, 12, 9, 0))
        self.assertEqual(len(w.calls()), 1)

    def test_several_missed_slots_coalesce_into_one_catch_up_each_and_one_banner(self):
        # S29: sen od sob 10.10 20:00 do pn 19.10 09:00
        w = self.world(waw(2026, 10, 10, 12))
        for name, sched in (("outofplace", EVERY2_ODD), ("renggli", EVERY2_EVEN), ("agromalz", MONDAY)):
            w.add(name, *sched)
        w.every(waw(2026, 10, 10, 19, 56), waw(2026, 10, 10, 20, 0))
        w.wake(waw(2026, 10, 19, 9, 0))
        w.every(waw(2026, 10, 19, 9, 4), waw(2026, 10, 19, 11, 0), step=4 * MIN)
        # najstarszy zaległy slot pierwszy, potem nazwa, każdy po 20 min odstępu; 19.10 to też dzień renggli
        # (11, 13 ... 19), więc jego zaległym slotem jest 19.10, a 17.10 to BRAK BIEGU
        self.assertEqual([(c["job"], c["slot"]) for c in w.calls()],
                         [("outofplace", "2026-10-18/1"), ("agromalz", "2026-10-19/1"), ("renggli", "2026-10-19/1")])  # fmt: skip
        missed = w.banners("bez biegu")
        self.assertEqual((len(missed), len(w.banners())), (1, 1), w.banners())  # jeden baner, nie 10
        for label in ("outofplace pn 12.10", "renggli nd 11.10", "agromalz pn 12.10", "renggli so 17.10"):
            self.assertIn(label, missed[0])
        slots = w.slots("outofplace")
        self.assertEqual([slots[f"2026-10-{d}/1"]["outcome"] for d in (12, 14, 16, 18)],
                         ["BRAK BIEGU", "BRAK BIEGU", "BRAK BIEGU", "OPUBLIKOWANO"])  # fmt: skip


class Retries(Base):
    """Brief, S3, S18, S19: ponowienie z przełączeniem płatności, limit 3 prób, jeden baner."""

    def test_retry_after_401_on_a_subscription_avoids_the_account_then_caps_with_one_banner(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        sub = {"mode": "subscription", "email": "7@example.com", "identity": "7@example.com", "verdict": "ok"}
        w.plan("matura",
               {"outcome": "BŁĄD", "retryable": True, "override": "auth", "switch_payment": True, "payer": sub,
                "reason": "401 w liczniku; następna próba innym źródłem płatności"},
               {"outcome": "BŁĄD", "retryable": True, "reason": "precheck (kod 3): sieć"},
               {"outcome": "BŁĄD", "retryable": True, "reason": "precheck (kod 3): sieć nadal"})  # fmt: skip
        w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 12))
        first = w.calls()[0]
        self.assertIsNone(first["mode"])
        # S19: przegląd mówi, kiedy następna próba (godzina po końcu pierwszej, 08:12 + 1 h)
        slot = w.slots("matura")["2026-10-12/1"]
        self.assertEqual((slot["state"], slot["next_at"]), ("backoff", first["at"] + HOUR))
        line = next(x for x in w.jobs().stdout.splitlines() if "pn 12.10 05:30" in x)
        self.assertIn("PONOWIENIE", line)
        self.assertIn(time.strftime("%H:%M", time.gmtime(first["at"] + HOUR + 2 * HOUR)), line)  # CEST
        w.every(waw(2026, 10, 12, 8, 14), waw(2026, 10, 12, 9, 14))  # tik co 2 min: nic przed godziną
        second = w.calls()[1]
        self.assertGreaterEqual(second["at"] - first["at"], HOUR)
        self.assertIsNone(second["mode"])  # job auto zostaje na auto: runenv wybiera sam (S18)
        avoid = json.load(open(os.path.join(w.state, "runenv", "avoid.json")))
        self.assertIn("7@example.com", avoid)  # 401 na 7@: tik dopisuje je do avoid.json, runenv go omija
        w.every(waw(2026, 10, 12, 9, 16), waw(2026, 10, 12, 12, 16), step=4 * MIN)  # nic przed 3 h
        third = w.calls()[2]
        self.assertGreaterEqual(third["at"] - second["at"], 3 * HOUR)
        self.assertIsNone(third["mode"])
        self.assertGreaterEqual(third["at"] - first["at"], 4 * HOUR - 5 * MIN)  # S19: sieć ma czas wrócić
        cap = w.banners("3/3")
        self.assertEqual(len(cap), 1, w.banners())
        self.assertIn("sieć nadal", cap[0])
        w.every(waw(2026, 10, 12, 12, 18), waw(2026, 10, 12, 20, 30), step=37 * MIN)
        w.every(waw(2026, 10, 13, 9, 0), waw(2026, 10, 13, 9, 2))
        self.assertEqual(len(w.calls()), 3)
        self.assertEqual(len(w.banners("3/3")), 1)
        self.assertEqual(w.banners("20:00"), [])  # slot zamknięty limitem: wieczór o nim nie woła
        self.assertEqual(w.slots("matura")["2026-10-12/1"]["outcome"], "BEZ WYNIKU")

    def test_cap_reached_after_the_evening_alarm_is_announced_at_once(self):
        # S3: dwie próby i wieczorny alarm "2/3", trzecia po 20:00 to POMINIĘTO payer
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        nobody = {"outcome": "POMINIĘTO", "skip": "payer", "retryable": True, "reason": "nikt nie zapłaci: pula pusta"}
        w.plan("matura", nobody, nobody, nobody)
        w.every(waw(2026, 10, 12, 16, 26), waw(2026, 10, 12, 16, 30))
        w.every(waw(2026, 10, 12, 18, 30), waw(2026, 10, 12, 18, 34))  # 2 h po pierwszej
        w.every(waw(2026, 10, 12, 20, 1), waw(2026, 10, 12, 20, 3))
        evening = w.banners("20:00")
        self.assertEqual(len(evening), 1)
        self.assertIn("2/3", evening[0])
        self.assertIn("następna próba 22:3", evening[0])
        w.every(waw(2026, 10, 12, 22, 30), waw(2026, 10, 12, 22, 38))  # 4 h po drugiej
        self.assertEqual(len(w.calls()), 3)
        cap = w.banners("3/3")
        self.assertEqual(len(cap), 1, w.banners())
        self.assertIn("nikt nie zapłacił", cap[0])
        w.every(waw(2026, 10, 12, 23, 0), waw(2026, 10, 12, 23, 30), step=10 * MIN)
        self.assertEqual((len(w.calls()), len(w.banners("3/3")), len(w.banners("20:00"))), (3, 1, 1))

    def test_skips_at_the_tick_gates_never_spend_attempts(self):
        # wyścig z bramkami tiku: runner sam zobaczył inny ciężki bieg albo wstrzymanie (POMINIĘTO heavy, hold)
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        race = [{"outcome": "POMINIĘTO", "skip": kind, "retryable": True} for kind in ("heavy", "hold", "heavy", "hold")]
        w.plan("matura", *race, {"outcome": "OPUBLIKOWANO"})
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 14))
        self.assertEqual(len(w.calls()), 5)
        self.assertEqual(w.slots("matura")["2026-10-12/1"]["outcome"], "OPUBLIKOWANO")
        self.assertEqual(w.banners(), [])

    def test_final_outcomes_are_never_retried_and_banner_only_where_filip_must_act(self):
        # S1 i brief: ZAPARKOWANO, PILNE, BŁĄD ostateczny po jednym banerze; OPUBLIKOWANO, BEZ WPISU cicho
        w = self.world(waw(2026, 10, 11, 12))
        outcomes = {"pub": "OPUBLIKOWANO", "none": "BEZ WPISU", "park": "ZAPARKOWANO", "urgent": "PILNE", "err": "BŁĄD"}
        for name, outcome in outcomes.items():
            w.add(name, *EVERY2_ODD, light=True)
            w.plan(name, {"outcome": outcome, "final": True})
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 14))
        self.assertEqual(sorted(c["job"] for c in w.calls()), sorted(outcomes))
        w.every(waw(2026, 10, 12, 9, 45), waw(2026, 10, 14, 5, 29), step=97 * MIN)
        self.assertEqual(len(w.calls()), 5)  # nic nie wraca przed 14.10 05:30
        for name in ("park", "urgent", "err"):
            self.assertEqual(len(w.banners(f"jobs: {name} ")), 1, (name, w.banners()))
        for name in ("pub", "none"):
            self.assertEqual(w.banners(f"jobs: {name} "), [], name)

    def test_record_recovered_by_filips_own_jobs_call_banners_once_and_is_not_retried(self):
        # S1: runner zginął po fazie side-effect; zapis PILNE robi `claude-acc jobs list` (A2a), nie tik
        w = self.world(waw(2026, 10, 11, 12))
        w.add("urgent", *EVERY2_ODD, light=True)
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 0))
        cur = {"v": 1, "id": "abc123abc123", "job": "urgent", "state": "running", "slot": "2026-10-12/1",
               "pid": 99999, "started_at": waw(2026, 10, 12, 7, 50), "phase": "side-effect", "run_id": None,
               "budget_usd": 2.0, "outcome": None, "urls": [], "facts": []}  # fmt: skip
        os.makedirs(os.path.join(w.jobs_dir, "urgent"), exist_ok=True)
        path = os.path.join(w.jobs_dir, "urgent", "current.json")
        with open(path, "w") as f:
            json.dump(cur, f)
        os.utime(path, (waw(2026, 10, 12, 8, 1), waw(2026, 10, 12, 8, 1)))
        w.jobs("list")
        self.assertEqual(w.history("urgent")[-1]["outcome"], "PILNE")
        w.every(waw(2026, 10, 12, 8, 2), waw(2026, 10, 12, 8, 10))
        self.assertEqual(w.calls(), [])
        self.assertEqual(len(w.banners("jobs: urgent PILNE")), 1)


class HoldManualDisable(Base):
    """S13, S23, S24: wstrzymanie, bieg ręczny, wyłączenie i odmowy runnera."""

    def test_hold_defers_without_spending_attempts_and_catches_up(self):
        for case in ("release", "expire", "long"):
            with self.subTest(case):
                w = self.world(waw(2026, 10, 11, 12))
                w.add("outofplace", *EVERY2_ODD)
                w.at(waw(2026, 10, 12, 5, 0))
                w.jobs("hold", "--hours", "48" if case == "long" else "4", "--reason", "build matura")
                w.every(waw(2026, 10, 12, 5, 26), waw(2026, 10, 12, 7, 0), step=8 * MIN)
                self.assertEqual((w.calls(), w.history("outofplace")), ([], []))
                if case == "release":
                    w.jobs("release")
                    w.every(waw(2026, 10, 12, 7, 10), waw(2026, 10, 12, 7, 12))
                    self.assertEqual(len(w.calls()), 1)
                elif case == "expire":
                    w.every(waw(2026, 10, 12, 8, 58), waw(2026, 10, 12, 9, 2))
                    self.assertEqual([c["at"] >= waw(2026, 10, 12, 9, 0) for c in w.calls()], [True])
                else:
                    w.every(waw(2026, 10, 12, 20, 0), waw(2026, 10, 12, 20, 2))
                    self.assertEqual(w.calls(), [])
                    evening = w.banners("20:00")
                    self.assertEqual(len(evening), 1)
                    self.assertIn("build matura", evening[0])

    def test_manual_run_during_the_slot_closes_it_and_busy_is_not_an_attempt(self):
        # S13: próba 1 BŁĄD do ponowienia, Filip odpala `jobs run` ręcznie (slot null), publikuje
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.plan("outofplace", {"outcome": "BŁĄD", "retryable": True}, {"gate": True, "outcome": "OPUBLIKOWANO", "final": True})
        w.every(waw(2026, 10, 12, 8, 16), waw(2026, 10, 12, 8, 20))
        w.at(waw(2026, 10, 12, 9, 0))
        w.gate("outofplace")
        manual = subprocess.Popen([os.path.join(FAKES_JOBS, "runner"), "run", "outofplace"], env=w.env())
        self.addCleanup(manual.kill)
        deadline = time.time() + 10
        while not os.path.exists(os.path.join(w.jobs_dir, "outofplace", "current.json")) and time.time() < deadline:
            time.sleep(0.02)
        w.every(waw(2026, 10, 12, 9, 30), waw(2026, 10, 12, 9, 36))  # backoff minął, ale ręczny bieg trwa
        self.assertEqual(len(w.calls(busy=True)), 2)  # próba 1 i ręczny bieg; tik nie wołał runnera
        w.at(waw(2026, 10, 12, 9, 50))
        os.remove(os.path.join(w.fake, "gate-outofplace"))
        manual.wait(10)
        w.every(waw(2026, 10, 12, 9, 52), waw(2026, 10, 12, 18, 0), step=53 * MIN)
        self.assertEqual(len(w.calls()), 2)  # jeden wpis dziś
        slot = w.slots("outofplace")["2026-10-12/1"]
        self.assertEqual((slot["state"], slot["outcome"]), ("done", "OPUBLIKOWANO"))
        self.assertIn("ręcznie", w.jobs().stdout)
        self.assertEqual(w.banners(), [])

    def test_disabled_mid_run_finishes_without_retry_and_exit_64_is_not_spammed(self):
        w = self.world(waw(2026, 10, 12, 12))
        w.add("renggli", *EVERY2_EVEN)
        w.plan("renggli", {"gate": True, "outcome": "BŁĄD", "retryable": True})
        w.gate("renggli")
        w.every(waw(2026, 10, 13, 8, 26), waw(2026, 10, 13, 8, 30), )
        w.at(waw(2026, 10, 13, 8, 40))
        w.jobs("set", "renggli", "enabled=false")
        w.at(waw(2026, 10, 13, 9, 10))
        w.open_gate("renggli")
        self.assertEqual(w.history("renggli")[-1]["outcome"], "BŁĄD")
        w.every(waw(2026, 10, 13, 9, 12), waw(2026, 10, 13, 21, 0), step=47 * MIN)
        self.assertEqual(len(w.calls()), 1)
        self.assertEqual(w.banners(), [])
        # golden: runner odmawia kodem 64 (wyłączony między decyzją tiku a startem): bez zapisu i bez wołania co tik
        g = self.world(waw(2026, 10, 15, 12))
        g.add("golden", "--days", "wt,pt", "--at", "06:00")
        g.plan("golden", {"exit": 64})
        g.every(waw(2026, 10, 16, 6, 0), waw(2026, 10, 16, 6, 16))
        self.assertEqual(len(g.calls()), 1)
        self.assertEqual((g.history("golden"), g.banners()), ([], []))
        g.every(waw(2026, 10, 16, 6, 24), waw(2026, 10, 16, 6, 26))  # po 20 min próbuje znowu
        self.assertEqual(len(g.calls()), 2)


class StartRules(Base):
    """S9, S10, S12, S14, S15, S22: kolejka, odstęp, start po zmianach i bez harmonogramu."""

    def test_one_heavy_run_at_a_time_staggered_without_head_of_line_blocking(self):
        # S9: agromalz (pierwszy w kolejce) POMINIĘTO payer i backoff; matura nie czeka na niego
        w = self.world(waw(2026, 10, 11, 12))
        for name, sched in (("outofplace", EVERY2_ODD), ("matura", EVERY2_ODD), ("agromalz", MONDAY)):
            w.add(name, *sched)
        w.plan("agromalz", {"outcome": "POMINIĘTO", "skip": "payer", "retryable": True})
        w.plan("matura", {"gate": True})
        w.gate("matura")
        w.every(waw(2026, 10, 12, 8, 8), waw(2026, 10, 12, 8, 12))
        self.assertEqual([c["job"] for c in w.calls()], ["agromalz", "matura"])  # kolejka: slot, potem nazwa
        w.every(waw(2026, 10, 12, 8, 14), waw(2026, 10, 12, 8, 30))  # matura biegnie: nikt inny ciężki
        self.assertEqual(len(w.calls(busy=True)), 2)
        w.at(waw(2026, 10, 12, 8, 31))
        w.open_gate("matura")
        w.every(waw(2026, 10, 12, 8, 33), waw(2026, 10, 12, 8, 49))  # odstęp 20 min po końcu matury
        self.assertEqual(len(w.calls()), 2)
        w.every(waw(2026, 10, 12, 8, 51), waw(2026, 10, 12, 8, 53))
        self.assertEqual([c["job"] for c in w.calls()], ["agromalz", "matura", "outofplace"])
        w.every(waw(2026, 10, 12, 10, 8), waw(2026, 10, 12, 10, 12))  # 2 h po POMINIĘTO payer
        self.assertEqual([c["job"] for c in w.calls()], ["agromalz", "matura", "outofplace", "agromalz"])
        # czekający joby nie zostawiły zapisów POMINIĘTO heavy: tik nie wołał ich w trakcie biegu
        self.assertEqual([r.get("skip") for j in ("agromalz", "matura", "outofplace") for r in w.history(j)],
                         ["payer", None, None, None])  # fmt: skip

    def test_bootout_of_the_tick_leaves_the_run_alive(self):
        # S10: launchd po wyjściu joba zabija jego grupę procesów; bieg ma własną sesję
        w = self.world(waw(2026, 10, 12, 12))
        w.add("renggli", *EVERY2_EVEN)
        w.plan("renggli", {"gate": True})
        w.gate("renggli")
        w.at(waw(2026, 10, 13, 10, 10))
        w.tick()
        w.at(waw(2026, 10, 13, 10, 12))
        # tik jak pod launchd: własna grupa procesów, którą launchd zabija po wyjściu joba
        tick = subprocess.Popen([PY, JOBS, "tick"], env=w.env(), start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        tick.wait(30)
        deadline = time.time() + 10
        while not w.calls() and time.time() < deadline:
            time.sleep(0.02)
        call = w.calls()[0]
        w.runners.append(call["pid"])
        self.assertEqual((call["sid"], call["pgid"]), (call["pid"], call["pid"]))  # runner w swojej sesji
        try:
            os.killpg(tick.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # w grupie tiku nie ma już nikogo: bieg do niej nie należy
        time.sleep(0.2)
        self.assertTrue(pid_alive(call["pid"]))
        w.open_gate("renggli")
        self.assertEqual(w.history("renggli")[-1]["slot"], "2026-10-13/1")

    def test_runner_killed_mid_run_is_recovered_and_retried_after_backoff(self):
        # S12: SIGKILL runnera; current.json zostaje, blokada wolna; także z cudzym żywym pid
        for variant in ("dead", "foreign pid"):
            with self.subTest(variant):
                w = self.world(waw(2026, 10, 11, 12))
                w.add("matura", *EVERY2_ODD)
                w.plan("matura", {"die": True})
                w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 12))
                path = os.path.join(w.jobs_dir, "matura", "current.json")
                os.utime(path, (waw(2026, 10, 12, 8, 12), waw(2026, 10, 12, 8, 12)))  # ostatni znak życia runnera
                if variant == "foreign pid":
                    cur = json.load(open(path))
                    cur["pid"] = os.getpid()
                    json.dump(cur, open(path, "w"))
                    os.utime(path, (waw(2026, 10, 12, 8, 12), waw(2026, 10, 12, 8, 12)))
                # przegląd bez tiku: żywy pid w current.json bez blokady joba to nie bieg w toku
                self.assertNotEqual(w.slots("matura")["2026-10-12/1"]["state"], "running")
                w.every(waw(2026, 10, 12, 9, 0), waw(2026, 10, 12, 9, 2))
                rec = w.history("matura")[-1]
                self.assertEqual((rec["outcome"], rec["retryable"], rec["override"]), ("BŁĄD", True, "runner"))
                self.assertEqual(len(w.calls()), 1)
                self.assertEqual(w.slots("matura")["2026-10-12/1"]["state"], "backoff")
                w.every(waw(2026, 10, 12, 10, 0), waw(2026, 10, 12, 10, 4))
                self.assertEqual(len(w.calls()), 2)

    def test_enable_and_first_schedule_never_fire_a_slot_from_before(self):
        # S14: golden wyłączony od 01.10, ręczny bieg 16.10 10:00, enable 10:30; renggli dostaje harmonogram 11.10 14:00
        w = self.world(waw(2026, 10, 1, 12))
        w.add("golden", "--days", "wt,pt", "--at", "06:00", disabled=True)
        w.at(waw(2026, 10, 11, 14, 0))
        w.add("renggli", *EVERY2_EVEN)
        w.every(waw(2026, 10, 11, 14, 2), waw(2026, 10, 16, 10, 28), step=4 * HOUR + 7 * MIN)
        w.at(waw(2026, 10, 16, 10, 30))
        w.jobs("enable", "golden")
        w.every(waw(2026, 10, 16, 10, 32), waw(2026, 10, 16, 23, 0), step=61 * MIN)
        self.assertEqual([c["job"] for c in w.calls()], ["renggli", "renggli"])  # 13 i 15.10, nie 11.10
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-13/1", "2026-10-15/1"])
        golden = next(j for j in w.overview()["jobs"] if j["name"] == "golden")
        self.assertEqual((golden["slots"], golden["next"]["slot"]), ([], "2026-10-20/1"))
        self.assertEqual(w.banners(), [])

    def test_schedule_input_is_validated_in_polish_and_the_old_schedule_kept(self):
        # S27: dni po polsku i po angielsku dają wt i pt; złe wejście to kod 64, lista form i stary harmonogram
        w = self.world(waw(2026, 10, 12, 12))
        w.add("golden", "--days", "wt,pt", "--at", "06:00")
        self.assertEqual(w.raw_jobs()["golden"]["schedule"]["weekdays"], [1, 4])
        w.jobs("set", "golden", "--days", "tue,fri")
        self.assertEqual(w.raw_jobs()["golden"]["schedule"]["weekdays"], [1, 4])
        kept = w.raw_jobs()["golden"]["schedule"]
        for bad in (("--at", "25:30"), ("--every", "0d"), ("--days", "xx,yy"), ("--days", "wt,funday")):
            r = w.jobs("set", "golden", *bad, ok=False)
            self.assertEqual(r.returncode, 64, bad)
            self.assertIn("pn wt śr cz pt so nd", r.stderr)  # przyjmowane formy w odpowiedzi
            self.assertEqual(w.raw_jobs()["golden"]["schedule"], kept, bad)
        w.jobs("set", "golden", "--every", "2")
        self.assertEqual(w.raw_jobs()["golden"]["schedule"]["every_days"], 2)

    def test_job_without_schedule_never_runs_or_alarms(self):
        # S15: smoke dokładnie jak w jobs.json tego Maca
        w = self.world(waw(2026, 10, 9, 12))
        w.add("smoke", light=True)
        w.every(waw(2026, 10, 9, 12, 2), waw(2026, 10, 16, 21, 0), step=7 * HOUR + 11 * MIN)
        self.assertEqual((w.calls(), w.banners()), ([], []))
        self.assertIn("smoke: bez harmonogramu (tylko ręcznie", w.jobs().stdout)
        beat = json.load(open(os.path.join(w.jobs_dir, "heartbeat.json")))
        self.assertEqual(beat["attention"], [])

    def test_edit_mid_slot_keeps_the_slot_its_attempts_and_the_anchor(self):
        # S22: (a) --at 07:00 o 08:30 po nieudanej próbie 1 (godzina już minęła); (b) --at 06:00 we wtorek
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        fail = {"outcome": "BŁĄD", "retryable": True}
        w.plan("outofplace", fail, fail, fail, fail)
        w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 12))
        w.at(waw(2026, 10, 12, 8, 30))
        w.jobs("set", "outofplace", "--at", "07:00")
        w.every(waw(2026, 10, 12, 9, 10), waw(2026, 10, 12, 9, 14))
        w.every(waw(2026, 10, 12, 12, 14), waw(2026, 10, 12, 12, 18))
        w.every(waw(2026, 10, 12, 16, 0), waw(2026, 10, 12, 16, 2))
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-12/1"] * 3)
        w.at(waw(2026, 10, 13, 10, 0))
        out = w.jobs("set", "outofplace", "--at", "06:00").stdout
        self.assertIn("następny slot: śr 14.10 06:00", out)
        w.every(waw(2026, 10, 13, 10, 2), waw(2026, 10, 13, 23, 0), step=67 * MIN)
        self.assertEqual(len(w.calls()), 3)


class Robustness(Base):
    """S2, S11, S25, S26, S31: zły job, zabity tik, zepsuty stan, martwy tik, skoki zegara."""

    def test_bad_schedule_of_one_job_does_not_silence_the_others(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.add("renggli", *EVERY2_EVEN)
        jobs = w.raw_jobs()
        jobs["renggli"]["schedule"] = {"every": "2x"}  # A2a przyjmował dowolny JSON
        w.write_jobs(jobs)
        w.every(waw(2026, 10, 12, 8, 3), waw(2026, 10, 12, 8, 5))
        self.assertEqual([c["job"] for c in w.calls()], ["outofplace"])
        self.assertIn("renggli: ZŁY HARMONOGRAM", w.jobs().stdout)
        beat = json.load(open(os.path.join(w.jobs_dir, "heartbeat.json")))
        self.assertIn("renggli: zły harmonogram", beat["attention"])
        w.every(waw(2026, 10, 12, 20, 0), waw(2026, 10, 12, 20, 0))
        self.assertIn("renggli: zły harmonogram", w.banners("20:00")[0])

    def test_tick_killed_after_spawn_or_after_a_banner_leaves_nothing_stuck(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.plan("outofplace", {"gate": True, "outcome": "PILNE", "final": True})
        w.gate("outofplace")
        w.at(waw(2026, 10, 12, 8, 3))
        w.tick()
        w.at(waw(2026, 10, 12, 8, 5))
        dead = w.tick(CLAUDE_ACC_JOBS_TICK_DIE="after-spawn")
        self.assertEqual(dead["exit"], -signal.SIGKILL)
        deadline = time.time() + 10
        while not w.calls() and time.time() < deadline:
            time.sleep(0.02)
        w.every(waw(2026, 10, 12, 8, 7), waw(2026, 10, 12, 8, 11))  # bieg żyje: żadnego drugiego
        self.assertEqual(len(w.calls(busy=True)), 1)
        w.runners.append(w.calls()[0]["pid"])
        w.at(waw(2026, 10, 12, 8, 12))
        w.open_gate("outofplace")
        w.at(waw(2026, 10, 12, 8, 13))
        killed = w.tick(CLAUDE_ACC_JOBS_TICK_DIE="after-banner")
        self.assertEqual(killed["exit"], -signal.SIGKILL)
        w.every(waw(2026, 10, 12, 8, 15), waw(2026, 10, 12, 8, 25))
        self.assertIn(len(w.banners("PILNE")), (1, 2))  # najwyżej raz za dużo, nigdy zgubiony
        self.assertEqual(len(w.history("outofplace")), 1)
        self.assertEqual(len(w.calls(busy=True)), 1)

    def test_corrupt_state_and_half_written_history_line_rebuild_from_records(self):
        # S11 (a), (b): stan ucięty do 40 bajtów, połowa linii w historii, próba "w starcie" bez runnera
        w = self.world(waw(2026, 10, 9, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.add("matura", *EVERY2_ODD)
        fail = {"outcome": "BŁĄD", "retryable": True}
        w.plan("matura", fail, fail, fail)
        w.every(waw(2026, 10, 10, 8, 0), waw(2026, 10, 10, 8, 2))  # matura (nazwa) pierwsza
        w.every(waw(2026, 10, 10, 8, 30), waw(2026, 10, 10, 8, 32))  # outofplace po odstępie
        state_path = os.path.join(w.jobs_dir, "tick-state.json")
        text = open(state_path).read()
        with open(state_path, "w") as f:
            f.write(text[:40])
        with open(os.path.join(w.jobs_dir, "outofplace", "history.jsonl"), "a") as f:
            f.write('{"v": 1, "id": "half", "job": "outofp')
        w.every(waw(2026, 10, 10, 9, 40), waw(2026, 10, 10, 9, 44))
        self.assertEqual([c["job"] for c in w.calls()], ["matura", "outofplace", "matura"])
        self.assertEqual(w.banners(), [])
        # stan mówi "matura wypuszczona", ale runnera nie ma, a od wypuszczenia minęło 20 min
        state = json.load(open(state_path))
        state["launched"]["matura"] = {"slot": "2026-10-10/1", "pid": 99999, "pid_start": 1, "at": w.wall - 25 * MIN,
                                       "records": 2}  # fmt: skip
        json.dump(state, open(state_path, "w"))
        w.every(waw(2026, 10, 10, 12, 44), waw(2026, 10, 10, 12, 46))
        self.assertEqual(len(w.calls("matura")), 3)  # trzecia próba, limit nadal 3
        w.every(waw(2026, 10, 10, 16, 0), waw(2026, 10, 10, 16, 2))
        self.assertEqual(len(w.calls("matura")), 3)

    def test_concurrent_tick_exits_at_once_without_side_effects(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 0))
        fd = os.open(os.path.join(w.jobs_dir, "tick.lock"), os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        w.at(waw(2026, 10, 12, 8, 2))
        start = time.time()
        r = w.jobs("tick", ok=False)
        self.assertLess(time.time() - start, 10)
        self.assertEqual(r.returncode, 0)
        self.assertIn("inny tik jobów właśnie trwa", r.stderr)
        self.assertEqual(w.calls(), [])
        fcntl.flock(fd, fcntl.LOCK_UN)
        w.tick()
        self.assertEqual(len(w.calls()), 1)

    def test_overview_tells_the_truth_with_the_tick_dead(self):
        # S25: tik nie żyje od 10.10 05:00, teraz 13.10 09:00; przegląd bez tiku
        w = self.world(waw(2026, 10, 9, 12))
        for name, sched in (("outofplace", EVERY2_ODD), ("matura", EVERY2_ODD), ("renggli", EVERY2_EVEN), ("agromalz", MONDAY)):
            w.add(name, *sched)
        w.every(waw(2026, 10, 10, 4, 56), waw(2026, 10, 10, 5, 0))
        w.at(waw(2026, 10, 13, 9, 0))
        text = w.jobs().stdout
        self.assertIn("UWAGA, tik stoi od 10.10 05:00", text)
        for name, day in (("outofplace", "2026-10-10/1"), ("matura", "2026-10-10/1"), ("renggli", "2026-10-11/1")):
            self.assertEqual(w.slots(name)[day]["outcome"], "BRAK BIEGU", (name, day))
        # najnowszy slot każdego joba da się jeszcze nadrobić, gdy tik wróci; przegląd mówi, że sam nie ruszy
        for name, day in (("outofplace", "2026-10-12/1"), ("matura", "2026-10-12/1"), ("renggli", "2026-10-13/1"),
                          ("agromalz", "2026-10-12/1")):  # fmt: skip
            slot = w.slots(name)[day]
            self.assertEqual(slot["state"], "due", (name, day))
            self.assertIn("tik stoi", slot["reason"])

    def test_clock_set_back_or_forward_never_repeats_a_slot(self):
        # S31; 10.10 nie biegł (pierwszy tik 12.10), więc cofnięcie na 11.10 nie może go "obudzić"
        w = self.world(waw(2026, 10, 9, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.every(waw(2026, 10, 12, 9, 38), waw(2026, 10, 12, 9, 40))
        w.at(waw(2026, 10, 11, 10, 0))
        w.every(waw(2026, 10, 11, 10, 0), waw(2026, 10, 11, 10, 30))  # dłużej niż odstęp 20 min
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-12/1"])
        self.assertEqual(w.slots("outofplace")["2026-10-10/1"]["outcome"], "BRAK BIEGU")
        w.at(waw(2026, 10, 12, 8, 0))  # zegar cofnięty o 1 h 40 min
        w.tick()
        w.every(waw(2026, 10, 12, 8, 2), waw(2026, 10, 12, 8, 10))
        self.assertEqual(len(w.calls()), 1)
        w.at(waw(2026, 10, 14, 9, 0))  # skok o 2 dni naprzód: slot 14.10 biegnie
        w.tick()
        w.every(waw(2026, 10, 14, 9, 2), waw(2026, 10, 14, 9, 4))
        w.at(waw(2026, 10, 12, 10, 0))  # i z powrotem
        w.every(waw(2026, 10, 12, 10, 0), waw(2026, 10, 12, 10, 8))
        w.every(waw(2026, 10, 14, 9, 30), waw(2026, 10, 14, 9, 34))
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-12/1", "2026-10-14/1"])


class EveningAlarm(Base):
    """S17: alarm 20:00 w czuwaniu i po śnie, raz na slot i wieczór, bez biegów w toku i przyszłych slotów."""

    def test_evening_alarm_awake(self):
        w = self.world(waw(2026, 10, 12, 0, 10))
        w.add("oop", *EVERY2_ODD)
        w.add("mat", "--every", "1d", "--at", "19:00", light=True)
        w.add("agro", *MONDAY, light=True)
        w.add("late", "--every", "1d", "--at", "21:00", light=True)
        nobody = {"outcome": "POMINIĘTO", "skip": "payer", "retryable": True, "reason": "nikt nie zapłaci"}
        w.plan("oop", nobody, nobody)
        w.plan("agro", {"outcome": "BEZ WPISU", "final": True})
        w.plan("mat", {"gate": True})
        w.every(waw(2026, 10, 12, 16, 26), waw(2026, 10, 12, 16, 34))
        w.every(waw(2026, 10, 12, 18, 30), waw(2026, 10, 12, 18, 34))
        w.gate("mat")
        w.every(waw(2026, 10, 12, 19, 18), waw(2026, 10, 12, 19, 22))
        w.every(waw(2026, 10, 12, 20, 0), waw(2026, 10, 12, 20, 40), step=10 * MIN)
        evening = w.banners("20:00")
        self.assertEqual(len(evening), 1, w.banners())
        self.assertIn("oop pn 12.10 05:30: 2/3, następna próba 22:3", evening[0])
        text = next(n["text"] for n in w.notes() if "20:00" in n["title"])
        for other in ("mat", "agro", "late"):
            self.assertNotIn(f"{other} ", text)

    def test_evening_alarm_while_asleep_comes_once_at_the_first_full_wake(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("oop", *EVERY2_ODD)
        nobody = {"outcome": "POMINIĘTO", "skip": "payer", "retryable": True, "reason": "nikt nie zapłaci"}
        w.plan("oop", nobody, nobody)
        w.every(waw(2026, 10, 12, 8, 26), waw(2026, 10, 12, 8, 30))
        w.every(waw(2026, 10, 12, 10, 30), waw(2026, 10, 12, 10, 34))
        w.at(waw(2026, 10, 12, 13, 0))
        w.tick()
        w.power("dark")
        w.at(waw(2026, 10, 12, 20, 3), awake=False)
        self.assertEqual(w.tick()["banners"], [])
        w.power("full")
        first, _second = w.wake(waw(2026, 10, 13, 7, 55))
        self.assertEqual(len(first["banners"]), 1)
        # powód jak w `jobs` (O5): ten tik tylko patrzy, trzecia próba rusza z następnym
        self.assertIn("oop pn 12.10 05:30: Mac dopiero się obudził: start od następnego tiku; 2/3", first["banners"][0]["text"])
        self.assertEqual(len(w.calls()), 3)  # trzecia próba zaraz po alarmie, zgodnie z jego treścią
        self.assertEqual(len(w.banners("20:00")), 1)


class Hints(Base):
    """S5, S6: wskazówka dla automatu kont tylko dla subskrypcji, od current.json do końca biegu."""

    def test_subscription_run_gets_a_hint_that_goes_with_the_run_and_credits_never(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        w.add("outofplace", *EVERY2_ODD, light=True)
        sub = {"mode": "subscription", "email": "7@example.com", "identity": "7@example.com", "verdict": None}
        w.plan("matura", {"gate": True, "payer": sub})
        w.plan("outofplace", {"gate": True})  # kredyt 1@example.com
        w.gate("matura")
        w.gate("outofplace")
        w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 16))
        path = os.path.join(w.jobs_dir, "held-accounts.json")
        hint = json.load(open(path))["accounts"]
        self.assertEqual([(e["email"], e["job"]) for e in hint], [("7@example.com", "matura")])
        self.assertGreater(hint[0]["until"], time.time())
        # S6 (b): bieg dłuższy niż pierwszy termin wskazówki: tik ją odświeża, póki bieg żyje
        hint[0]["until"] = time.time() - 1
        json.dump({"v": 1, "accounts": hint}, open(path, "w"))
        w.every(waw(2026, 10, 12, 8, 18), waw(2026, 10, 12, 8, 18))
        self.assertGreater(json.load(open(path))["accounts"][0]["until"], time.time() + 10 * MIN)
        w.open_gate("matura")
        w.open_gate("outofplace")
        w.every(waw(2026, 10, 12, 11, 15), waw(2026, 10, 12, 11, 17))
        hint = json.load(open(os.path.join(w.jobs_dir, "held-accounts.json")))["accounts"]
        self.assertEqual(hint, [])


class Banners(Base):
    """Bramka A2b (O1, O2): baner z polskim tekstem naprawdę wychodzi, nieudany wraca, zapis woła raz."""

    def test_polish_banner_gets_through_applescript(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("park", *EVERY2_ODD, light=True)
        w.plan("park", {"outcome": "ZAPARKOWANO", "final": True, "reason": "PR do przeglądu: źródła się kłócą"})
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 4))
        self.assertEqual(w.banners("jobs: park"), ["jobs: park ZAPARKOWANO: pn 12.10: PR do przeglądu: źródła się kłócą"])
        self.assertEqual(w.notes("notify-failed.log"), [])

    def test_the_banner_script_compiles_in_real_applescript_for_polish_text(self):
        # dokładne wywołanie, które buduje notify(); osacompile tylko kompiluje, nic się nie pokazuje
        w = self.world(waw(2026, 10, 11, 12))
        title, text = "jobs: matura BŁĄD", "-pn 12.10: próba źle zakończona, żółć"
        code = "import json, sys, jobs; print(json.dumps(jobs.notify_argv(sys.argv[1], sys.argv[2], '/usr/bin/osascript')))"
        r = subprocess.run([PY, "-c", code, title, text], cwd=ROOT, env=w.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = json.loads(r.stdout)
        cut = argv.index("--")
        script = [argv[i + 1] for i in range(1, cut, 2) if argv[i] == "-e"]
        self.assertEqual(argv[cut:], ["--", text, title])  # tekst i tytuł bez zmian, poza skryptem
        compiled = subprocess.run(["/usr/bin/osacompile", "-o", os.path.join(w.home, "banner.scpt")]
                                  + [x for line in script for x in ("-e", line)], capture_output=True, text=True)  # fmt: skip
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertTrue(any("display notification" in line for line in script))
        # osascript oddaje skryptowi argumenty po "--" bez zmian (return zamiast banera: nic się nie pokazuje)
        echo = ['return (item 1 of argv) & "|" & (item 2 of argv)' if "display notification" in x else x for x in script]
        back = subprocess.run(["/usr/bin/osascript"] + [x for line in echo for x in ("-e", line)] + argv[cut:],
                              capture_output=True, text=True)  # fmt: skip
        self.assertEqual((back.returncode, back.stdout.strip()), (0, f"{text}|{title}"), back.stderr)

    def test_a_banner_osascript_refused_is_logged_and_shown_on_a_later_tick(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("park", *EVERY2_ODD, light=True)
        w.plan("park", {"outcome": "ZAPARKOWANO", "final": True, "reason": "PR do przegladu"})  # ASCII: tylko awaria
        fail = os.path.join(w.fake, "osascript-fail")
        open(fail, "w").close()
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 6))
        self.assertEqual(w.banners(), [])
        self.assertIn("baner nie wyszedł (osascript kod 1", w.tick_log())
        os.remove(fail)
        w.every(waw(2026, 10, 12, 8, 8), waw(2026, 10, 12, 8, 14))
        self.assertEqual(len(w.banners("jobs: park ZAPARKOWANO")), 1)

    def test_a_record_banner_never_comes_back_after_its_key_expires(self):
        # O2: klucze "sent" żyją 9 dni; zapis sprzed 8 dni już nie woła, więc 21.10 nie ma powtórki
        w = self.world(waw(2026, 10, 11, 12))
        w.add("park", *EVERY2_ODD, light=True)
        w.plan("park", {"outcome": "ZAPARKOWANO", "final": True, "reason": "PR do przeglądu"})
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 4))
        self.assertEqual(len(w.banners("ZAPARKOWANO")), 1)
        for day in range(13, 25):
            w.every(waw(2026, 10, day, 9, 0), waw(2026, 10, day, 9, 4), step=4 * MIN)
        self.assertEqual(len(w.banners("ZAPARKOWANO")), 1, w.banners())
        self.assertEqual(len(w.calls()), 7)  # 12, 14 ... 24.10: każdy slot raz


class Payment(Base):
    """Bramka A2b (S18, O7): job auto zostaje na auto; tylko kredyt z 401 albo limitem przełącza slot."""

    def test_subscription_429_in_auto_stays_auto_and_lands_on_another_account(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        pool = {"credits": [], "subscription": ["7@example.com", "9@example.com"]}  # pula kredytu pusta
        w.plan("matura", {"pool": pool, "outcome": "BŁĄD", "retryable": True, "override": "limit", "switch_payment": True,
                          "reason": "429 w liczniku; następna próba innym kontem"},
               {"pool": pool, "outcome": "OPUBLIKOWANO"})  # fmt: skip
        w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 12))
        w.every(waw(2026, 10, 12, 9, 10), waw(2026, 10, 12, 9, 16))  # godzina po pierwszej
        self.assertEqual([c["mode"] for c in w.calls()], [None, None])
        got = [(r["outcome"], (r["payer"] or {}).get("email")) for r in w.history("matura")]
        self.assertEqual(got, [("BŁĄD", "7@example.com"), ("OPUBLIKOWANO", "9@example.com")])  # nie POMINIĘTO, nie 7@
        self.assertEqual(w.slots("matura")["2026-10-12/1"]["outcome"], "OPUBLIKOWANO")

    def test_credits_401_moves_the_rest_of_the_slot_to_subscription_even_after_a_memory_skip(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        pool = {"credits": ["org-a"], "subscription": ["7@example.com"]}
        w.plan("matura", {"pool": pool, "outcome": "BŁĄD", "retryable": True, "override": "auth", "switch_payment": True,
                          "reason": "401 w liczniku: klucz organizacji"},
               {"outcome": "POMINIĘTO", "skip": "memory", "reason": "bramka pamięci nie wpuściła"},
               {"pool": pool, "outcome": "OPUBLIKOWANO"})  # fmt: skip
        w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 12))
        w.every(waw(2026, 10, 12, 9, 12), waw(2026, 10, 12, 9, 14))  # godzina po 401
        w.every(waw(2026, 10, 12, 9, 16), waw(2026, 10, 12, 9, 30))  # pamięć: nic przed 20 min
        self.assertEqual(len(w.calls()), 2)
        w.every(waw(2026, 10, 12, 9, 32), waw(2026, 10, 12, 9, 34))
        self.assertEqual([c["mode"] for c in w.calls()], [None, "subscription", "subscription"])
        last = w.history("matura")[-1]
        self.assertEqual((last["outcome"], last["payer"]["mode"], last["payer"]["email"]), ("OPUBLIKOWANO", "subscription", "7@example.com"))
        self.assertEqual(w.slots("matura")["2026-10-12/1"]["attempts"], 2)  # POMINIĘTO memory nie zjada próby


class OutsideRuns(Base):
    """Bramka A2b (O3, S13): biegi spoza tiku (ręczne, obcy --slot) zamykają slot tylko wynikiem z efektem."""

    def runner(self, w, *args):
        subprocess.run([os.path.join(FAKES_JOBS, "runner"), "run", *args], env=w.env(), timeout=30, stdin=subprocess.DEVNULL)

    def test_a_free_text_slot_record_never_stops_the_job(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("oop", *EVERY2_ODD, light=True)
        w.every(waw(2026, 10, 11, 12, 2), waw(2026, 10, 11, 12, 4))
        w.at(waw(2026, 10, 11, 13, 0))
        self.runner(w, "oop", "--slot", "test")  # A2a przyjmuje każdy tekst w --slot
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 6))
        self.assertEqual(self.slots_of(w.calls()), ["test", "2026-10-12/1"])
        slot = w.slots("oop")["2026-10-12/1"]
        self.assertEqual((slot["state"], slot["outcome"]), ("done", "OPUBLIKOWANO"))
        self.assertEqual(w.banners(), [])
        # obcy id (dawny format z docstringu) w oknie slotu 14.10 zamyka go jak bieg ręczny: bez drugiego wpisu
        w.at(waw(2026, 10, 14, 6, 0))
        self.runner(w, "oop", "--slot", "2026-10-14T05:30")
        w.every(waw(2026, 10, 14, 8, 0), waw(2026, 10, 14, 8, 4))
        self.assertEqual(len(w.calls()), 3)
        slot = w.slots("oop")["2026-10-14/1"]
        self.assertEqual((slot["state"], slot["outcome"]), ("done", "OPUBLIKOWANO"))
        self.assertIn("spoza tiku (--slot 2026-10-14T05:30)", slot["reason"])
        self.assertIn("spoza tiku (--slot test)", w.jobs().stdout)

    def test_a_manual_abort_leaves_the_slot_open_for_the_next_attempt(self):
        # skeptyk P1: próba 1 do ponowienia, Filip przerywa ręczny bieg (BŁĄD ostateczny, slot null)
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.plan("outofplace", {"outcome": "BŁĄD", "retryable": True, "reason": "precheck: sieć"},
               {"outcome": "BŁĄD", "final": True, "reason": "Ctrl-C (SIGINT) na biegu ręcznym"},
               {"outcome": "OPUBLIKOWANO"})  # fmt: skip
        w.every(waw(2026, 10, 12, 8, 16), waw(2026, 10, 12, 8, 20))
        w.at(waw(2026, 10, 12, 8, 40))
        self.runner(w, "outofplace")
        w.every(waw(2026, 10, 12, 9, 30), waw(2026, 10, 12, 21, 0), step=31 * MIN)
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-12/1", None, "2026-10-12/1"])
        slot = w.slots("outofplace")["2026-10-12/1"]
        self.assertEqual((slot["state"], slot["outcome"]), ("done", "OPUBLIKOWANO"))
        self.assertEqual(w.banners(), [])  # ręczny BŁĄD Filip widział w terminalu

    def test_a_manual_pilne_is_bannered_once_and_closes_the_slot(self):
        # np. agent, którego sesja zginęła po pushu: zapis bez slotu, recover() albo runner
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.plan("outofplace", {"outcome": "PILNE", "final": True, "reason": "po fazie side-effect: runner zginął"})
        w.every(waw(2026, 10, 12, 4, 0), waw(2026, 10, 12, 4, 2))
        w.at(waw(2026, 10, 12, 6, 0))
        self.runner(w, "outofplace")
        w.every(waw(2026, 10, 12, 6, 2), waw(2026, 10, 12, 6, 30))
        self.assertEqual(self.slots_of(w.calls()), [None])
        urgent = w.banners("jobs: outofplace PILNE")
        self.assertEqual(len(urgent), 1, w.banners())
        self.assertIn("ręcznie", urgent[0])
        self.assertEqual(w.slots("outofplace")["2026-10-12/1"]["outcome"], "PILNE")


class EnableDisable(Base):
    """Bramka A2b (S14, S23): wyłączenie i włączenie nie gubi otwartego slotu ani historii."""

    def disabled_mid_retry(self):
        w = self.world(waw(2026, 10, 9, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.plan("outofplace", {"outcome": "OPUBLIKOWANO"}, {"outcome": "BŁĄD", "retryable": True, "reason": "precheck: sieć"},
               {"outcome": "OPUBLIKOWANO"})  # fmt: skip
        w.every(waw(2026, 10, 10, 8, 0), waw(2026, 10, 10, 8, 2))
        w.every(waw(2026, 10, 12, 8, 10), waw(2026, 10, 12, 8, 12))
        w.at(waw(2026, 10, 12, 8, 30))
        w.jobs("disable", "outofplace")
        return w

    def test_disable_and_enable_mid_slot_keep_the_open_slot_and_the_history(self):
        w = self.disabled_mid_retry()  # skeptyk P2
        w.at(waw(2026, 10, 12, 8, 50))
        w.jobs("enable", "outofplace")
        w.every(waw(2026, 10, 12, 9, 20), waw(2026, 10, 12, 21, 0), step=31 * MIN)
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-10/1", "2026-10-12/1", "2026-10-12/1"])
        got = {k: v["outcome"] for k, v in w.slots("outofplace").items()}
        self.assertEqual(got, {"2026-10-10/1": "OPUBLIKOWANO", "2026-10-12/1": "OPUBLIKOWANO"})
        self.assertEqual(w.banners(), [])

    def test_a_long_disable_never_catches_up_an_old_slot_that_has_records(self):
        # włączenie 16.10: sloty 14 i 16.10 przepadły w wyłączeniu, więc 12.10 jest zamknięty (BEZ WYNIKU)
        w = self.disabled_mid_retry()
        w.at(waw(2026, 10, 16, 10, 0))
        w.jobs("enable", "outofplace")
        w.every(waw(2026, 10, 16, 10, 2), waw(2026, 10, 17, 23, 0), step=67 * MIN)
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-10/1", "2026-10-12/1"])
        self.assertEqual(w.slots("outofplace")["2026-10-12/1"]["outcome"], "BEZ WYNIKU")
        missed = w.banners("bez biegu")
        self.assertEqual(len(missed), 1, w.banners())
        self.assertIn("outofplace pn 12.10 05:30 BEZ WYNIKU", missed[0])


class Hardening(Base):
    """Bramka A2b (O4 do O9, S2): zła definicja, martwa próba bez zapisu, seria DarkWake, powód o 20:00."""

    def test_malformed_definitions_stop_only_their_own_job(self):
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.add("golden", *EVERY2_ODD, light=True)
        jobs = w.raw_jobs()
        jobs["renggli"] = None  # ręczna edycja jobs.json
        jobs["golden"]["limits"] = ["broken"]
        w.write_jobs(jobs)
        outs = w.every(waw(2026, 10, 12, 8, 3), waw(2026, 10, 12, 8, 7))
        self.assertEqual([o.get("exit", 0) for o in outs], [0, 0, 0], outs)
        self.assertEqual([c["job"] for c in w.calls()], ["outofplace"])
        beat = json.load(open(os.path.join(w.jobs_dir, "heartbeat.json")))
        self.assertEqual(beat["wall"], waw(2026, 10, 12, 8, 7))
        self.assertIn("renggli: zła definicja", beat["attention"])
        self.assertIn("golden: zła definicja", beat["attention"])
        text = w.jobs().stdout
        for line in ("golden: ZŁA DEFINICJA", "renggli: ZŁA DEFINICJA", "outofplace: co 2 dni"):
            self.assertIn(line, text)
        w.every(waw(2026, 10, 12, 20, 0), waw(2026, 10, 12, 20, 0))
        evening = w.banners("20:00")
        self.assertEqual(len(evening), 1)
        self.assertIn("golden: zła definicja", evening[0])
        self.assertIn("renggli: zła definicja", evening[0])

    def test_a_dead_attempt_without_its_record_blocks_a_new_start_until_recovered(self):
        # O8: runner zginął, a sweep trzyma inny proces: recover() czeka na rachunek, tik nie startuje
        w = self.world(waw(2026, 10, 11, 12))
        w.add("matura", *EVERY2_ODD)
        run_id = "0123456789abcdef"
        run_dir = os.path.join(w.state, "runenv", "runs", run_id)
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "run.json"), "w") as f:
            json.dump({"run_id": run_id, "owner_pid": 99999}, f)
        cur = {"v": 1, "id": "dead00dead00", "job": "matura", "state": "running", "slot": "2026-10-12/1", "pid": 99999,
               "started_at": waw(2026, 10, 12, 7, 50), "phase": "run", "run_id": run_id, "budget_usd": 2.0,
               "outcome": None, "urls": [], "facts": []}  # fmt: skip
        path = os.path.join(w.jobs_dir, "matura", "current.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cur, f)
        os.utime(path, (waw(2026, 10, 12, 8, 1), waw(2026, 10, 12, 8, 1)))
        lock = os.open(os.path.join(w.state, "runenv", ".sweep.lock"), os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        outs = w.every(waw(2026, 10, 12, 8, 2), waw(2026, 10, 12, 8, 10))
        self.assertEqual(w.calls(), [])
        self.assertIn("czeka na zapis", outs[-1]["waiting"]["matura"]["reason"])
        shutil.rmtree(run_dir)  # runenv rozliczył bieg
        fcntl.flock(lock, fcntl.LOCK_UN)
        w.every(waw(2026, 10, 12, 8, 12), waw(2026, 10, 12, 8, 12))
        rec = w.history("matura")[0]
        self.assertEqual((rec["id"], rec["outcome"], rec["override"]), ("dead00dead00", "BŁĄD", "runner"))
        w.every(waw(2026, 10, 12, 9, 0), waw(2026, 10, 12, 9, 4))  # godzina po końcu martwej próby
        self.assertEqual(self.slots_of(w.calls()), ["2026-10-12/1"])

    def test_back_to_back_darkwakes_with_short_sleeps_never_start_even_if_pmset_says_full(self):
        # O9: pmset -g log 08.10: DarkWake co ok. 54 s, 45 s czuwania i 9 s snu; tu pmset (błędnie) mówi full
        w = self.world(waw(2026, 10, 11, 12))
        w.add("outofplace", *EVERY2_ODD)
        w.every(waw(2026, 10, 12, 5, 0), waw(2026, 10, 12, 5, 2))  # przed slotem, czuwanie
        t = waw(2026, 10, 12, 5, 32)
        w.at(t, slept=18)
        w.tick()
        for _ in range(4):
            t += 54
            w.at(t, slept=9)
            w.tick()
        self.assertEqual(w.calls(), [])
        self.assertEqual(w.tick_log().count("tik: zasilanie full, świeżo po śnie"), 6)  # pierwszy tik w ogóle też
        w.at(t + 2 * MIN)
        w.tick()  # czuwanie bez snu: start
        self.assertEqual(len(w.calls()), 1)
        self.assertEqual(w.tick_log().count("tik: zasilanie "), 8)

    def test_the_evening_alarm_says_why_the_slot_still_waits(self):
        # O5: pmset nie odpowiada cały dzień; alarm mówi to samo co `jobs`, nie "jeszcze bez próby"
        w = self.world(waw(2026, 10, 11, 12))
        w.add("oop", *EVERY2_ODD)
        w.every(waw(2026, 10, 12, 0, 10), waw(2026, 10, 12, 0, 12))
        w.power("fail")
        w.every(waw(2026, 10, 12, 8, 0), waw(2026, 10, 12, 8, 2))
        w.every(waw(2026, 10, 12, 20, 0), waw(2026, 10, 12, 20, 2))
        evening = w.banners("20:00")
        self.assertEqual(len(evening), 1, w.banners())
        self.assertIn("oop pn 12.10 05:30: pmset nie mówi, czy Mac nie śpi: bez startu; jeszcze bez próby", evening[0])
        line = next(x for x in w.jobs().stdout.splitlines() if "pn 12.10 05:30" in x)
        self.assertIn("pmset nie mówi, czy Mac nie śpi: bez startu; jeszcze bez próby", line)


class Install(unittest.TestCase):
    """S28: launchd odpala tik co 2 min, bez klasy, która dławi bieg, i bez zabijania biegu przy bootout."""

    def test_plist_template_renders_lints_and_does_not_throttle_the_run(self):
        src = os.path.join(ROOT, "launchd", "com.filip.claude-acc.jobs.plist.template")
        folder = tempfile.mkdtemp(prefix="jobs-plist-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "jobs.plist")
        with open(src) as f, open(path, "w") as out:
            out.write(f.read().replace("__HOME__", "/Users/tester"))
        self.assertEqual(subprocess.run(["plutil", "-lint", path], capture_output=True).returncode, 0)

        def get(key):
            return subprocess.run(["plutil", "-extract", key, "raw", "-o", "-", path], capture_output=True, text=True).stdout.strip()

        self.assertEqual(get("ProcessType"), "Interactive")  # Background i brak klasy dławią (man launchd.plist)
        self.assertEqual(get("AbandonProcessGroup"), "true")
        self.assertEqual(get("LowPriorityIO"), "")
        self.assertLessEqual(int(get("StartInterval")), 300)
        self.assertEqual(get("ProgramArguments.2") + " " + get("ProgramArguments.3"), "jobs tick")
        with open(os.path.join(ROOT, "setup.sh")) as f:
            self.assertIn("com.filip.claude-acc.jobs", next(x for x in f if x.startswith("JOBS=")))


class FullPath(unittest.TestCase):
    """Tik z prawdziwym runnerem A2a i atrapami potoku, runenv i Claude Code (świat tests/test_jobs.py)."""

    def setUp(self):
        try:
            from tests import test_jobs
        except ImportError:  # uruchomione z katalogu tests
            import test_jobs
        self.base = test_jobs.Base()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.w = self.base.w
        self.w.add_job("blog")

    def env(self):
        env = self.w.env()
        env["PATH"] = f"{FAKES_JOBS}:{env['PATH']}"  # atrapa pmset: pełne obudzenie
        return env

    def test_tick_starts_the_real_runner_for_the_due_slot_once(self):
        path = os.path.join(self.w.jobs_dir, "jobs.json")
        data = json.load(open(path))
        past = time.time() - 2 * 86400
        data["jobs"]["blog"]["schedule"] = {"every_days": 1, "anchor": "2026-01-01", "at": ["00:00"], "since": past, "edited": past}
        with open(path, "w") as f:
            json.dump(data, f)
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + (7200 if time.time() < utc(2026, 10, 25, 1) else 3600)))
        slot = f"{day}/1"

        def tick():
            r = subprocess.run([PY, JOBS, "tick", "--json"], env=self.env(), capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

        self.assertEqual(tick()["started"], [])  # pierwszy tik tylko patrzy
        started = tick()["started"]
        self.assertEqual([(s["job"], s["slot"]) for s in started], [("blog", slot)])
        deadline = time.time() + 90
        while not self.w.history("blog") and time.time() < deadline:
            time.sleep(0.2)
        rec = self.w.history("blog")[-1]
        self.assertEqual((rec["slot"], rec["outcome"], rec["payer"]["mode"]), (slot, "OPUBLIKOWANO", "credits"), rec["reason"])
        while pid_alive(started[0]["pid"]) and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(tick()["started"], [])
        self.assertEqual(len(self.w.history("blog")), 1)

    def test_subscription_run_holds_its_account_for_the_switcher_until_it_ends(self):
        run = subprocess.Popen([PY, JOBS, "run", "blog", "--mode", "subscription", "--json"], env=self.w.env(FAKE_JOB_SLEEP="3"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # fmt: skip
        self.addCleanup(run.kill)
        hint_path = os.path.join(self.w.jobs_dir, "held-accounts.json")
        seen, deadline = [], time.time() + 60
        while run.poll() is None and time.time() < deadline:
            data = json.load(open(hint_path)) if os.path.exists(hint_path) else {}
            seen += [(e["email"], e["job"], e["pid"]) for e in data.get("accounts") or [] if e not in seen]
            time.sleep(0.05)
        out, err = run.communicate(timeout=60)
        rec = json.loads(out)
        self.assertEqual(rec["payer"]["mode"], "subscription", err)
        self.assertIn((rec["payer"]["email"].lower(), "blog", rec["pid"]), seen)
        self.assertEqual(json.load(open(hint_path))["accounts"], [])  # koniec biegu zdejmuje wskazówkę


if __name__ == "__main__":
    unittest.main()
