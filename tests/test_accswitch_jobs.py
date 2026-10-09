"""Automat kont a biegi `claude-acc jobs` (etap A2b): wskazówka "konto trzyma job", linia jobów w
`claude-acc status` i baner, gdy tik jobów stoi.

Świat z tests/test_e2e.py (atrapy Pęku kluczy, API, osobny $HOME), przed nimi atrapy jobów
(tests/fakes-jobs: pmset i osascript surowy jak AppleScript), prawdziwy accswitch.py jako proces. Wartości oczekiwane z briefu A2b i scenariuszy S4 do S7 i S16: konto,
którym płaci żywy bieg, nie dostaje Twoich sesji, póki wskazówka jest świeża; queue(), `token
--json` i `status --json` zostają bajt w bajt; zepsuty plik wskazówki to zwykłe przełączenie.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_accswitch_jobs
"""

import json
import os
import shutil
import time
import unittest

try:
    from tests.test_e2e import BASE, FAKES, Env, configure
except ImportError:  # uruchomione z katalogu tests
    from test_e2e import BASE, FAKES, Env, configure

FAKES_JOBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-jobs")


def timeless(data):
    """status --json bez pól, które zmieniają się z samym upływem sekund (generated_at, *age)."""
    if isinstance(data, dict):
        return {k: timeless(v) for k, v in data.items()
                if not (k == "generated_at" or (k.endswith("age") and isinstance(v, (int, float))))}
    if isinstance(data, list):
        return [timeless(v) for v in data]
    return data


def uptime():
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)


def boot():
    return time.time() - time.clock_gettime(time.CLOCK_MONOTONIC)


class World(Env):
    def __init__(self):
        super().__init__()
        self.jobs_dir = os.path.join(self.state_dir, "jobs")
        os.makedirs(self.jobs_dir)

    def env(self, **extra):
        # pmset dla stanu zasilania (heartbeat_alarm) i osascript, który odrzuca \uXXXX jak AppleScript
        return super().env(**dict({"PATH": f"{FAKES_JOBS}:{FAKES}:/usr/bin:/bin"}, **extra))

    def power(self, state):
        with open(os.path.join(self.fake, "pmset"), "w") as f:
            f.write(state)

    def hint(self, email, until_in, pid=None):
        """Wskazówka jobs.py: bieg (żywy pid testu) płaci kontem email jeszcze until_in sekund."""
        data = {"v": 1, "accounts": [{"email": email, "job": "matura", "pid": pid or os.getpid(),
                                      "since": time.time() - 600, "until": time.time() + until_in}]}
        self.raw_hint(json.dumps(data))

    def raw_hint(self, text):
        with open(os.path.join(self.jobs_dir, "held-accounts.json"), "w") as f:
            f.write(text)

    def scheduled_job(self, schedule=True):
        job = {"cwd": self.home, "entry": "true", "budget_usd": 2.0, "enabled": True,
               "schedule": {"every_days": 2, "anchor": "2026-10-10", "at": ["05:30"], "since": 0} if schedule else None}
        with open(os.path.join(self.jobs_dir, "jobs.json"), "w") as f:
            json.dump({"jobs": {"matura" if schedule else "smoke": job}}, f)

    def heartbeat(self, awake_ago, wall_ago=None, boot_at=None, stored_uptime=None):
        beat = {"v": 1, "wall": time.time() - (awake_ago if wall_ago is None else wall_ago),
                "uptime": uptime() - awake_ago if stored_uptime is None else stored_uptime,
                "boot": boot() if boot_at is None else boot_at, "power": "full", "interval_s": 120,
                "stale_after_s": 900, "attention": []}
        with open(os.path.join(self.jobs_dir, "heartbeat.json"), "w") as f:
            json.dump(beat, f)

    def switch_log(self):
        path = os.path.join(self.state_dir, "switch.log")
        return open(path).read() if os.path.exists(path) else ""


class Base(unittest.TestCase):
    def world(self, **cfg):
        w = configure(World(), **cfg) if cfg else World()
        self.addCleanup(shutil.rmtree, w.home, True)
        return w

    def active(self, w):
        token = w.entry(BASE)["claudeAiOauth"]["accessToken"]
        return next(e for e in w.ids if w.managed(e)["claudeAiOauth"]["accessToken"] == token)


class HeldAccountHint(Base):
    """S5, S6: Twoje sesje nie wchodzą na konto, którym płaci żywy bieg, póki wskazówka jest świeża."""

    def switching_world(self):
        # a@x aktywne pod progiem sesji; b@x ma najwięcej zapasu (głowa kolejki), c@x mniej
        w = self.world()
        a = w.account("a@x", session_used=97, weekly_used=40)
        w.account("b@x", session_used=10, weekly_used=10)
        w.account("c@x", session_used=10, weekly_used=30)
        w.runtime(a)
        w.write()
        return w

    def test_tick_skips_the_held_head_of_the_queue_while_the_hint_is_fresh(self):
        w = self.switching_world()
        w.hint("b@x", until_in=3600)
        w.run("tick")
        self.assertEqual(self.active(w), "c@x")

    def test_expired_hint_no_longer_fences_the_account_off(self):
        w = self.switching_world()
        w.hint("b@x", until_in=-5)
        w.run("tick")
        self.assertEqual(self.active(w), "b@x")

    def test_drain_never_lands_on_the_held_account(self):
        # S5 (b): nikt nie ma zapasu, dobijanie włączone, b@x ma najwięcej resztek, ale trzyma je bieg
        w = self.world(drain=True)
        a = w.account("a@x", session_used=100, weekly_used=40)
        w.account("b@x", session_used=97, weekly_used=40)
        w.account("c@x", session_used=98, weekly_used=40)
        w.runtime(a)
        w.write()
        w.hint("b@x", until_in=3600)
        w.forget_usage_cache()
        w.run("tick")
        self.assertEqual(self.active(w), "c@x")

    def test_broken_hint_file_is_ignored_with_one_log_line(self):
        # S4: ucięty zapis, brak pola, until jako tekst, katalog, plik bez prawa odczytu
        cases = {
            "truncated": '{"v": 1, "accounts": [{"email": "b@x", "pi',
            "no accounts": '{"account": null}',
            "string until": json.dumps({"accounts": [{"email": "b@x", "pid": 1, "until": "jutro"}]}),
            "directory": None,
            "unreadable": json.dumps({"accounts": [{"email": "b@x", "pid": 1, "until": time.time() + 3600}]}),
        }
        for label, text in cases.items():
            with self.subTest(label):
                w = self.switching_world()
                path = os.path.join(w.jobs_dir, "held-accounts.json")
                if text is None:
                    os.makedirs(path)
                else:
                    w.raw_hint(text)
                if label == "unreadable":
                    os.chmod(path, 0)
                    self.addCleanup(os.chmod, path, 0o600)
                r = w.run("tick")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.active(w), "b@x")  # jak bez pliku: głowa kolejki
                self.assertEqual(w.switch_log().count("wskazówka jobów nieczytelna"), 1)

    def test_token_status_json_and_queue_line_stay_byte_identical(self):
        # S7: token, status --json i linia kolejki nie widzą wskazówki; status ma tylko jedną linię więcej
        w = self.switching_world()
        path = os.path.join(w.jobs_dir, "held-accounts.json")
        token = ("token", "--json", "--prefer", "b@x", "--min-minutes", "240")
        for args in (token, ("status", "--json")):
            before = w.run(*args)
            w.hint("b@x", until_in=3600)
            after = w.run(*args)
            os.remove(path)
            if args[0] == "status":  # migawka ma chwilę zapisu i wiek odczytów; reszta bajt w bajt
                before.stdout, after.stdout = (json.dumps(timeless(json.loads(r.stdout))) for r in (before, after))
            self.assertEqual((after.returncode, after.stdout), (before.returncode, before.stdout), args)
        before = w.run("status").stdout.splitlines()
        w.hint("b@x", until_in=3600)
        after = w.run("status").stdout.splitlines()
        queue_line = lambda lines: [x for x in lines if x.startswith("następne w kolejce")]  # noqa: E731
        self.assertEqual(queue_line(after), queue_line(before))
        self.assertIn("b@x", queue_line(after)[0])  # kolejka dla oka nadal pokazuje b@x
        added = [x for x in after if x.startswith("joby:")]
        self.assertEqual(len(added), 1, after)
        self.assertIn("b@x trzyma matura", added[0])
        self.assertEqual(len(after), len(before) + 2)  # pusta linia odstępu i linia jobów


class JobsStatusLine(Base):
    """S16: linia jobów w `claude-acc status` i baner z automatu kont, gdy tik jobów stoi."""

    def jobs_lines(self, w):
        r = w.run("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        return [line for line in r.stdout.splitlines() if line.startswith("joby:")]

    def plain_world(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        w.write()
        return w

    def test_tick_down_for_20_awake_minutes_is_stale_with_one_banner(self):
        w = self.plain_world()
        w.scheduled_job()
        w.heartbeat(awake_ago=20 * 60)
        lines = self.jobs_lines(w)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("UWAGA, tik stoi od", lines[0])
        w.run("tick")
        w.run("tick")
        stale = [n for n in w.notifications() if "harmonogram stoi" in n]
        self.assertEqual(len(stale), 1, w.notifications())

    def test_stale_tick_seen_only_in_darkwake_waits_for_the_full_wake(self):
        # O4: czuwanie rośnie też w nocnych DarkWake; baner tam nikt nie zobaczy, a epizod by przepadł
        w = self.plain_world()
        w.scheduled_job()
        w.heartbeat(awake_ago=20 * 60)
        alarm = os.path.join(w.jobs_dir, "stale-alarm.json")
        w.power("dark")
        w.run("tick")
        w.run("tick")
        self.assertEqual([n for n in w.notifications() if "harmonogram stoi" in n], [])
        self.assertFalse(os.path.exists(alarm))
        w.power("full")
        w.run("tick")
        w.run("tick")
        stale = [n for n in w.notifications() if "harmonogram stoi" in n]
        self.assertEqual(len(stale), 1, w.notifications())
        self.assertIn("blogi nie wystartują same", stale[0])  # polski tekst przeszedł przez AppleScript

    def test_long_sleep_or_a_long_run_is_not_stale(self):
        # (b), (c): 10 h ściany, ale minuta czuwania od ostatniego tiku
        w = self.plain_world()
        w.scheduled_job()
        w.heartbeat(awake_ago=60, wall_ago=10 * 3600)
        lines = self.jobs_lines(w)
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("UWAGA", lines[0])
        w.run("tick")
        self.assertEqual([n for n in w.notifications() if "harmonogram stoi" in n], [])

    def test_reboot_counts_awake_time_since_boot_never_negative(self):
        # (d): zapis z poprzedniego uruchomienia systemu, z czuwaniem większym niż obecne
        w = self.plain_world()
        w.scheduled_job()
        for stored in (uptime() + 50000, uptime() - 60):  # czuwanie mniejsze albo większe niż zapisane
            w.heartbeat(awake_ago=0, boot_at=boot() - 3 * 86400, stored_uptime=stored)
            lines = self.jobs_lines(w)
            self.assertIn("tik stoi", lines[0])  # ten Mac czuwa od startu dłużej niż 15 min
            self.assertNotIn("-", lines[0].split("(")[1].split(" ")[0])

    def test_no_heartbeat_is_silent_without_schedules_and_a_warning_with_them(self):
        w = self.plain_world()
        w.scheduled_job(schedule=False)  # (e): dziś na tym Macu tylko smoke bez harmonogramu
        self.assertEqual(self.jobs_lines(w), [])
        w.run("tick")
        self.assertEqual([n for n in w.notifications() if "harmonogram stoi" in n], [])
        w.scheduled_job()  # (f)
        lines = self.jobs_lines(w)
        self.assertIn("tik jeszcze nie biegł", lines[0])


if __name__ == "__main__":
    unittest.main()
