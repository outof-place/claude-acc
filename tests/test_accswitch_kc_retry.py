"""Zapis nowej pary po odświeżeniu tokenu (#155): serwer zużył już stary refresh token, więc para żyje
tylko w pamięci przebiegu. Zapis jest ponawiany; po porażce konto jest odstawione (wymaga ponownego
logowania), a zużyty token nie idzie ani do serwera, ani z runtime do kopii konta. Do pliku para nie
trafia nigdy, a w logu, na wyjściu i w stanie nie ma żadnego tokenu.

Świat z tests/test_e2e.py (atrapy Pęku kluczy i API, osobny $HOME), prawdziwy accswitch.py.
Uruchomienie: /usr/bin/python3 -m unittest tests.test_accswitch_kc_retry
"""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

try:
    from tests.test_e2e import BASE, HERE, MANAGED, USER, Env, scoped
except ImportError:  # uruchomione z katalogu tests
    from test_e2e import BASE, HERE, MANAGED, USER, Env, scoped

TOKEN = "/v1/oauth/token"


class KeychainWriteRetryTest(unittest.TestCase):
    def world(self):
        """Aktywne konto z tokenem wygasłym pół godziny temu: odświeża je tick."""
        w = Env()
        self.addCleanup(shutil.rmtree, w.home, True)
        self.old = w.account("a@x", expires_in=-30 * 60)["claudeAiOauth"]
        w.runtime({"claudeAiOauth": self.old})
        w.write()
        return w

    def server(self, w):
        with open(os.path.join(w.fake, "server.json")) as f:
            return json.load(f)

    def assert_no_token_anywhere(self, w, *runs):
        """Żaden token, który serwer kiedykolwiek wydał, w logu, w stanie ani na wyjściu przebiegów."""
        server = self.server(w)
        tokens = (
            set(server["access"])
            | set(server["refresh"])
            | set(server.get("consumed", {}))
        )
        tokens |= {self.old["accessToken"], self.old["refreshToken"]}
        texts = [r.stdout + r.stderr for r in runs]
        for name in os.listdir(w.state_dir):
            path = os.path.join(w.state_dir, name)
            if os.path.isfile(path):
                with open(path, errors="replace") as f:
                    texts.append(f.read())
        for token in tokens:
            for text in texts:
                self.assertNotIn(token, text)

    def test_a_write_that_fails_twice_is_retried_within_the_tick(self):
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=MANAGED, FAKE_FAIL_WRITE_TIMES="2")

        self.assertEqual(len(w.calls(TOKEN)), 1)
        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertIn(
            fresh["refreshToken"], self.server(w)["refresh"]
        )  # the new pair, alive
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], fresh)
        self.assertNotIn("a@x", w.saved_state().get("needs_login", {}))
        again = w.run("tick")
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assertNotIn("nieudany", r.stderr)
        self.assert_no_token_anywhere(w, r, again)

    def test_a_write_that_keeps_failing_parks_the_account_and_the_runtime_copy_heals_it(
        self,
    ):
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=MANAGED, ACC_KC_RETRY_BUDGET="4")

        # the account's copy still holds the used token; the runtime entries got the new pair
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], self.old)
        self.assertIn(self.old["refreshToken"], self.server(w)["consumed"])
        fresh = w.entry(BASE)["claudeAiOauth"]
        self.assertIn(fresh["refreshToken"], self.server(w)["refresh"])
        self.assertIn("zapis odświeżonego tokenu do Keychain nieudany", r.stderr)
        self.assertIn("nowa para jest we wpisach", r.stderr)
        self.assertIn("a@x", w.saved_state()["needs_login"])
        self.assertGreater(len(w.keychain_calls()), 0)
        writes = [
            key
            for cmd, key in w.keychain_calls()
            if cmd == "add-generic-password" and key.startswith(MANAGED)
        ]
        self.assertGreater(len(writes), 1)  # retried

        # the next tick: no used token goes out, and the account's copy takes the runtime's pair
        again = w.run("tick")
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assertEqual(len(w.calls(TOKEN)), 1)
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], fresh)
        self.assert_no_token_anywhere(w, r, again)

    def test_a_used_token_never_goes_to_the_server_even_hours_later(self):
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_ALL="1", ACC_KC_RETRY_BUDGET="2")

        # every write failed: the pair is gone, every copy holds the used token
        self.assertIn("nowa para przepadła", r.stderr)
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], self.old)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], self.old)
        # needs_login gives an account another chance after 6 h: not with a token the server used
        w.state(needs_login={"a@x": int(time.time()) - 7 * 3600})
        again = w.run("tick")
        self.assertEqual(len(w.calls(TOKEN)), 1)
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assertIn("a@x", w.saved_state()["needs_login"])
        self.assert_no_token_anywhere(w, r, again)

    def test_a_runtime_entry_left_with_the_used_token_gets_the_new_pair_next_tick(self):
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=BASE, ACC_KC_RETRY_BUDGET="2")

        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertIn(fresh["refreshToken"], self.server(w)["refresh"])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], self.old)
        self.assertIn(f"wpisy {BASE} zostały ze zużytym refresh tokenem", r.stderr)
        self.assertNotIn("a@x", w.saved_state().get("needs_login", {}))

        again = w.run("tick")
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)
        self.assertEqual(
            w.managed("a@x")["claudeAiOauth"], fresh
        )  # never the used pair from runtime
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assert_no_token_anywhere(w, r, again)


    def test_runtime_entries_left_with_the_used_pair_never_overwrite_the_account_s_copy(self):
        # both runtime entries keep the used pair, and its access token still answers the profile
        # API: read-back would take it for a session's newer token and write it over the new pair
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=f"{BASE},{scoped(w.config_dir)}", ACC_KC_RETRY_BUDGET="2")

        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertNotEqual(fresh, self.old)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], self.old)
        self.assertIn(self.old["accessToken"], self.server(w)["access"])  # the profile still says a@x

        again = w.run("tick")
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], fresh)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], fresh)
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assert_no_token_anywhere(w, r, again)


    def test_a_lost_write_parks_the_account_at_once(self):
        # one ensure_fresh and nothing after it (a tick would come back to the account and find the
        # used token): the mark comes from the failed write itself. A process of its own, since
        # accswitch takes its paths from $HOME when imported
        w = self.world()
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import accswitch as a; "
            "creds, note = a.ensure_fresh(a.Account(sys.argv[2], 'a@x'), a.load_config()); print(creds is None)"
        )
        r = subprocess.run(["/usr/bin/python3", "-B", "-c", code, os.path.dirname(HERE), w.ids["a@x"]],
                           env=w.env(FAKE_FAIL_WRITE_ALL="1", ACC_KC_RETRY_BUDGET="2"), capture_output=True, text=True,
                           timeout=60, check=False)  # fmt: skip
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")
        self.assertIn("a@x", w.saved_state()["needs_login"])
        self.assertEqual(len(w.notifications()), 1)
        self.assert_no_token_anywhere(w, r)


class KeychainWriteUnitTest(unittest.TestCase):
    """kc_write i propagate w procesie testu, na atrapie `security`."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import accswitch

        self.acc = accswitch
        self.w = Env()
        self.addCleanup(shutil.rmtree, self.w.home, True)
        for patcher in (mock.patch.dict(os.environ, self.w.env()), mock.patch.object(accswitch, "KEYCHAIN_USER", USER)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for cache in (accswitch._KC_SEEN, accswitch._TOOLS):
            cache.clear()
            self.addCleanup(cache.clear)

    def test_a_write_that_times_out_says_nothing_of_the_secret(self):
        self.w.write()
        os.environ["FAKE_SLOW_WRITE_SERVICE"] = "svc"
        with self.assertRaises(RuntimeError) as caught:
            self.acc.kc_write("svc", "u", "rt-secret-value", timeout=1)
        self.assertNotIn("rt-secret-value", str(caught.exception))
        self.assertIn("brak odpowiedzi po 1 s", str(caught.exception))
        self.assertTrue(
            caught.exception.__suppress_context__
        )  # the traceback leaves TimeoutExpired out

    def test_propagate_writes_every_entry_when_the_log_cannot_be_written(self):
        old = {"accessToken": "at-old", "refreshToken": "rt-old", "expiresAt": 1}
        new = {"accessToken": "at-new", "refreshToken": "rt-new", "expiresAt": 2}
        self.w.runtime(
            {"claudeAiOauth": old},
            services=(BASE, scoped(self.w.config_dir), scoped(self.w.other_dir)),
        )
        self.w.write()
        cfg = {"config_dir": self.w.config_dir, "other_config_dirs": [self.w.other_dir]}
        with mock.patch.object(
            self.acc, "log", side_effect=OSError(28, "No space left on device")
        ):
            written, failed = self.acc.propagate(
                "rt-old", json.dumps({"claudeAiOauth": new}), cfg
            )
        self.assertEqual(failed, [])
        self.assertEqual(len(written), 3)
        for service in (BASE, scoped(self.w.config_dir), scoped(self.w.other_dir)):
            self.assertEqual(self.w.entry(service)["claudeAiOauth"], new)

    def test_the_retry_budget_can_only_shrink(self):
        for value, want in (("2", 2.0), ("999", 45), ("-1", 0.0), ("x", 45)):
            with mock.patch.dict(os.environ, {"ACC_KC_RETRY_BUDGET": value}):
                self.assertEqual(self.acc.kc_retry_budget(), want)


if __name__ == "__main__":
    unittest.main()
