"""Przebieg automatu kont przerwany sygnałem w dowolnym punkcie nie zostawia zużytego refresh
tokenu: serwer karze jego ponowne użycie unieważnieniem całego konta (wylogowanie).

Atrapy curl i security mają po dwa punkty na wywołanie (start i chwila tuż przed odpowiedzią).
Test przerywa tick po kolei w każdym z nich, potem puszcza zwykły tick i sprawdza, że serwer
nie dostał zużytego tokenu, a każda kopia konta trzyma token, który serwer jeszcze przyjmuje.
Kontrola: SIGKILL (którego ani launchd, ani acc-cored tickowi nie wysyłają) między odpowiedzią
serwera a zapisem do Pęku kluczy naprawdę zostawia zużyty token, więc test widzi, co ma widzieć.

Świat z tests/test_e2e.py (atrapy Pęku kluczy i API, osobny $HOME), prawdziwy accswitch.py.
Punkty idą po 4 naraz (każdy w swoim $HOME): po kolei cały przegląd trwał 3,5 minuty.
Uruchomienie: /usr/bin/python3 -m unittest tests.test_accswitch_uninterrupted
"""

import json
import os
import shutil
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

try:
    from tests.test_e2e import BASE, Env, scoped
except ImportError:  # uruchomione z katalogu tests
    from test_e2e import BASE, Env, scoped

TOKEN = "https://api.anthropic.com/v1/oauth/token"


def idle_refresh():
    """Aktywne konto z tokenem wygasłym pół godziny temu: żadna sesja go nie odświeża, robi to tick."""
    w = Env()
    a = w.account("a@x", expires_in=-30 * 60)
    w.runtime(a)
    w.write()
    return w


def switch_refresh():
    """Aktywne konto bez zapasu i drugie z wygasłym tokenem: tick je odświeża i na nie przełącza."""
    w = Env()
    a = w.account("a@x", session_used=97)
    w.account("b@x", expires_in=-30 * 60)
    w.runtime(a)
    w.write()
    return w


def in_refresh(n, points):
    """Punkty od wysłania refresh tokenu do ostatniego zapisu do Pęku kluczy po nim."""
    start = points.index(f"curl {TOKEN} start") + 1
    writes = [
        i + 1
        for i, p in enumerate(points)
        if p.startswith("security add-generic-password") and i + 1 > start
    ]
    return start <= n <= max(writes)


class UninterruptedRefreshTest(unittest.TestCase):
    def world(self, make):
        w = make()
        self.addCleanup(shutil.rmtree, w.home, True)
        return w

    def points(self, make):
        """Punkty niezakłóconego przebiegu, w kolejności."""
        w = self.world(make)
        r = w.run("tick", FAKE_SIGNAL_AT="1000000")
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertEqual(len(w.calls("/v1/oauth/token")), 1)
        return open(os.path.join(w.fake, "signal-points")).read().splitlines()

    def server(self, w):
        return json.load(open(os.path.join(w.fake, "server.json")))

    def assert_no_used_token(self, w, where):
        server = self.server(w)
        self.assertEqual(
            server.get("reuse_attempts", 0),
            0,
            f"{where}: zużyty refresh token poszedł do serwera",
        )
        alive = server["refresh"]
        for email in w.ids:
            self.assertIn(
                w.managed(email)["claudeAiOauth"]["refreshToken"],
                alive,
                f"{where}: kopia {email}",
            )
        for service in (BASE, scoped(w.config_dir)):
            entry = w.entry(service)
            if entry:
                self.assertIn(
                    entry["claudeAiOauth"]["refreshToken"],
                    alive,
                    f"{where}: wpis {service}",
                )

    def interrupt_everywhere(self, make, sig, only=None):
        points = self.points(make)

        def at(n):
            w = self.world(make)
            w.run("tick", FAKE_SIGNAL_AT=str(n), FAKE_SIGNAL=sig)
            w.run("tick")  # następny przebieg, bez sygnału: tu wyszłoby ponowne użycie
            self.assert_no_used_token(w, f"SIG{sig} w punkcie {n} ({points[n - 1]})")

        chosen = [n for n in range(1, len(points) + 1) if not only or only(n, points)]
        with ThreadPoolExecutor(4) as pool:
            for done in [pool.submit(at, n) for n in chosen]:
                done.result()  # pierwszy błąd wychodzi jako błąd testu

    def test_sigterm_at_every_point_of_an_idle_refresh(self):
        self.interrupt_everywhere(idle_refresh, "TERM")

    def test_sigterm_at_every_point_of_a_switch_with_refresh(self):
        self.interrupt_everywhere(switch_refresh, "TERM")

    def test_sigint_and_sighup_inside_the_refresh(self):
        for sig in ("INT", "HUP"):
            self.interrupt_everywhere(idle_refresh, sig, only=in_refresh)

    def test_a_signal_inside_the_refresh_waits_for_the_writes(self):
        points = self.points(idle_refresh)
        w = self.world(idle_refresh)
        r = w.run(
            "tick",
            FAKE_SIGNAL_AT=str(points.index(f"curl {TOKEN} answer") + 1),
            FAKE_SIGNAL="TERM",
        )

        self.assertEqual(
            r.returncode, -15, r.stderr
        )  # SIGTERM zrobił swoje, ale po zapisach
        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertIn(fresh["refreshToken"], self.server(w)["refresh"])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], fresh)

    def test_sigkill_after_the_answer_does_leave_a_used_token(self):
        # kontrola: tego punktu Python nie obroni, dlatego nikt ticku nie zabija
        points = self.points(idle_refresh)
        w = self.world(idle_refresh)
        w.run(
            "tick",
            FAKE_SIGNAL_AT=str(points.index(f"curl {TOKEN} answer") + 1),
            FAKE_SIGNAL="KILL",
        )
        w.run("tick")

        self.assertGreaterEqual(self.server(w).get("reuse_attempts", 0), 1)

    def test_a_refresh_starts_only_with_time_left_before_the_deadline(self):
        w = self.world(idle_refresh)

        r = w.run("tick", ACC_TICK_DEADLINE=str(int(time.time()) + 10))

        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assertEqual(
            w.saved_state()["tick_due"], {"quiet": False, "at": None, "why": ""}
        )
        self.assertIn(
            "termin przebiegu", open(os.path.join(w.state_dir, "switch.log")).read()
        )

        w.run("tick", ACC_TICK_DEADLINE=str(int(time.time()) + 100))

        self.assertEqual(len(w.calls("/v1/oauth/token")), 1)
        self.assert_no_used_token(w, "po terminie")


if __name__ == "__main__":
    unittest.main()
