"""Termin następnego przebiegu automatu kont (tick_due w state.json): spokojny przebieg podaje,
do kiedy jego wynik się nie zmieni bez zmiany na wejściu, każdy inny zostawia następny slot.
Czyta to acc-cored, który uruchamia tick tylko wtedy, gdy przebieg mógłby zrobić coś innego.

Świat z tests/test_e2e.py (atrapy Pęku kluczy i API, osobny $HOME), prawdziwy accswitch.py.
Uruchomienie: /usr/bin/python3 -m unittest tests.test_accswitch_due
"""

import shutil
import time
import unittest

try:
    from tests.test_e2e import Env, depot_store, with_depot
except ImportError:  # uruchomione z katalogu tests
    from test_e2e import Env, depot_store, with_depot


class TickDueTest(unittest.TestCase):
    def world(self, w=None):
        w = w or Env()
        self.addCleanup(shutil.rmtree, w.home, True)
        return w

    def due(self, w):
        return w.saved_state()["tick_due"]

    def test_account_far_from_its_threshold_is_quiet_until_its_reading_expires(self):
        # 90% sesji zapasu: active_max_age to kwadrans, token ważny 8 h
        w = self.world()
        a = w.account("a@x", session_used=10)
        w.runtime(a)
        w.write()

        before = time.time()
        w.run("tick")

        due = self.due(w)
        self.assertTrue(due["quiet"])
        self.assertGreaterEqual(due["at"], before + 900)
        self.assertLessEqual(due["at"], time.time() + 900)
        self.assertEqual(due["why"], "odczyt limitów aktywnego konta")

    def test_a_token_that_expires_sooner_is_the_deadline(self):
        w = self.world()
        a = w.account("a@x", session_used=10, expires_in=3 * 60)
        w.runtime(a)
        w.write()

        w.run("tick")

        due = self.due(w)
        self.assertTrue(due["quiet"])
        self.assertEqual(due["at"], a["claudeAiOauth"]["expiresAt"] / 1000)
        self.assertEqual(due["why"], "token aktywnego konta wygasa")

    def test_a_switch_leaves_the_next_slot(self):
        w = self.world()
        a = w.account("a@x", session_used=97)
        w.account("b@x", session_used=10)
        w.runtime(a)
        w.write()

        w.run("tick")

        self.assertIn("przełączono na b@x", open(w.state_dir + "/switch.log").read())
        self.assertEqual(self.due(w), {"quiet": False, "at": None, "why": ""})

    def test_an_account_selected_in_the_host_app_is_quiet_without_a_deadline(self):
        w = self.world()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.orca_selects("b@x")
        w.write()

        w.run("tick")

        due = self.due(w)
        self.assertTrue(due["quiet"])
        self.assertIsNone(due["at"])

    def test_depot_fast_path_adds_its_own_deadline_and_a_push_leaves_the_next_slot(self):
        w = self.world(with_depot(Env()))
        a = w.account("a@x", session_used=10)
        w.account("b@x", weekly_used=5)
        w.runtime(a)
        w.write()

        w.run("tick")  # pierwsza wysyłka do Depot: przegląd wszystkich kont
        self.assertEqual(self.due(w)["quiet"], False)
        self.assertEqual(len([c for c in depot_store(w)["calls"] if c[:3] == ["claude", "secrets", "add"]]), 1)

        w.run("tick")  # konto sandboxów niesie: szybka ścieżka i jej termin (odczyt b@x starcza 10 min)
        due = self.due(w)
        self.assertTrue(due["quiet"])
        self.assertLessEqual(due["at"], time.time() + 600)
        self.assertEqual(due["why"], "token albo konto sandboxów Depot")


if __name__ == "__main__":
    unittest.main()
