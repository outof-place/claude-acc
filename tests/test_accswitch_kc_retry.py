"""Zapis nowej pary po odświeżeniu tokenu (#155): serwer zużył już stary refresh token, więc para żyje
tylko w pamięci przebiegu. Najpierw runtime, potem kopia konta, potem ponowienia. Para, która przeżyła
w runtime, zostaje (konto nie stoi, przełączenie jej nie nadpisze); para, która przepadła, odstawia
konto. Skrót tokenu zapisany przed żądaniem trzyma każdy przebieg z dala od zużytego tokenu: nie idzie
do serwera ani z runtime do kopii konta. Do pliku para nie trafia nigdy, a w logu, na wyjściu i w
stanie nie ma żadnego tokenu.

Świat z tests/test_e2e.py (atrapy Pęku kluczy i API, osobny $HOME), prawdziwy accswitch.py.
Uruchomienie: /usr/bin/python3 -m unittest tests.test_accswitch_kc_retry
"""

import hashlib
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


def mark(refresh):
    return hashlib.sha256(refresh.encode()).hexdigest()[:12]


class KeychainWriteRetryTest(unittest.TestCase):
    def world(self, *others, services=None):
        """Aktywne konto z tokenem wygasłym pół godziny temu (odświeża je tick); `others`:
        (e-mail, session_used) kolejnych kont z ważnym tokenem."""
        w = Env()
        self.addCleanup(shutil.rmtree, w.home, True)
        self.old = w.account(
            "a@x", expires_in=-30 * 60, session_used=self.session_used
        )["claudeAiOauth"]
        for email, used in others:
            w.account(email, session_used=used)
        w.runtime({"claudeAiOauth": self.old}, services=services)
        w.write()
        return w

    session_used = 10

    def server(self, w):
        with open(os.path.join(w.fake, "server.json")) as f:
            return json.load(f)

    def set_server(self, w, **fields):
        server = self.server(w)
        server.update(fields)
        with open(os.path.join(w.fake, "server.json"), "w") as f:
            json.dump(server, f)

    def py(self, w, code, *args, **env):
        """accswitch w osobnym procesie ze światem `w` (bierze ścieżki z $HOME przy imporcie)."""
        prog = (
            "import sys; sys.path.insert(0, sys.argv[1]); import accswitch as a\n"
            + code
        )
        return subprocess.run(["/usr/bin/python3", "-B", "-c", prog, os.path.dirname(HERE), *args], env=w.env(**env),
                              capture_output=True, text=True, timeout=60, check=False)  # fmt: skip

    def assert_no_token_anywhere(self, w, *runs):
        """Żaden token, który serwer kiedykolwiek wydał: w logach, stanie (z podkatalogami), powiadomieniach
        ani na wyjściu przebiegów."""
        server = self.server(w)
        tokens = (
            set(server["access"])
            | set(server["refresh"])
            | set(server.get("consumed", {}))
        )
        tokens |= {self.old["accessToken"], self.old["refreshToken"]}
        texts = [r.stdout + r.stderr for r in runs]
        paths = [
            os.path.join(root, name)
            for root, _, names in os.walk(w.state_dir)
            for name in names
        ]
        paths.append(os.path.join(w.fake, "notify.log"))
        for path in paths:
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

    def test_the_runtime_is_written_before_the_account_s_copy(self):
        # a Claude Code session starting during the account copy's retries would refresh with the
        # used token: the runtime gets the pair first
        w = self.world()
        w.run("tick")
        writes = [
            key.split("|")[0]
            for cmd, key in w.keychain_calls()
            if cmd == "add-generic-password"
        ]
        self.assertLess(writes.index(BASE), writes.index(MANAGED), writes)
        self.assertLess(
            writes.index(scoped(w.config_dir)), writes.index(MANAGED), writes
        )

    def test_an_account_copy_that_keeps_failing_leaves_the_pair_in_runtime_and_the_next_tick_heals_it(
        self,
    ):
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=MANAGED, ACC_KC_RETRY_BUDGET="4")

        # the account's copy still holds the used token; the runtime entries got the new pair, so the
        # account isn't parked
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], self.old)
        self.assertIn(self.old["refreshToken"], self.server(w)["consumed"])
        fresh = w.entry(BASE)["claudeAiOauth"]
        self.assertIn(fresh["refreshToken"], self.server(w)["refresh"])
        self.assertIn("kopia konta bez nowej pary", r.stderr)
        self.assertIn("para jest we wpisach", r.stderr)
        self.assertNotIn("a@x", w.saved_state().get("needs_login", {}))
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

    def test_a_switch_in_the_same_tick_never_drops_the_runtime_s_only_pair(self):
        # a@x is out of room and b@x has it: the tick switches. The new pair of a@x is only in the
        # runtime (its copy's write fails): the switch must not overwrite the runtime before the copy
        # has the pair, and refuses while it can't get it there
        self.session_used = 97
        w = self.world(("b@x", 10))

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=MANAGED, ACC_KC_RETRY_BUDGET="2")

        fresh = w.entry(BASE)["claudeAiOauth"]
        self.assertNotEqual(fresh, self.old)
        self.assertIn(
            fresh["refreshToken"], self.server(w)["refresh"]
        )  # a@x's new pair, still there
        self.assertNotEqual(w.saved_state().get("active_email"), "b@x")
        self.assertIn("zapis do Keychain nieudany", r.stderr)

        again = w.run("tick")  # the copy takes the pair, then the switch goes through
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], fresh)
        self.assertEqual(w.saved_state().get("active_email"), "b@x")
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assert_no_token_anywhere(w, r, again)

    def test_a_used_token_never_goes_to_the_server_even_hours_later(self):
        w = self.world()

        r = w.run("tick", FAKE_FAIL_WRITE_ALL="1", ACC_KC_RETRY_BUDGET="2")

        # every write failed: the pair is gone, every copy holds the used token, the account is parked
        self.assertIn("nowa para przepadła", r.stderr)
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], self.old)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], self.old)
        self.assertIn("a@x", w.saved_state()["needs_login"])
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

    def test_another_config_directory_left_with_the_used_token_gets_the_new_pair_next_tick(
        self,
    ):
        # propagate writes every config directory's entry, read-back aligns only the two live ones:
        # the other directory's entry is healed from the list of entries left behind
        w = self.world()
        other = scoped(w.other_dir)
        w.runtime(
            {"claudeAiOauth": self.old}, services=(scoped(w.config_dir), BASE, other)
        )
        w.write()

        r = w.run("tick", FAKE_FAIL_WRITE_SERVICE=other, ACC_KC_RETRY_BUDGET="2")

        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertEqual(w.entry(other)["claudeAiOauth"], self.old)
        self.assertEqual(w.saved_state()["stale_runtime"], {other: "a@x"})
        again = w.run("tick")
        self.assertEqual(w.entry(other)["claudeAiOauth"], fresh)
        self.assertEqual(w.saved_state()["stale_runtime"], {})
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assert_no_token_anywhere(w, r, again)

    def test_runtime_entries_left_with_the_used_pair_never_overwrite_the_account_s_copy(
        self,
    ):
        # both runtime entries keep the used pair, and its access token still answers the profile
        # API: read-back would take it for a session's newer token and write it over the new pair
        w = self.world()

        r = w.run(
            "tick",
            FAKE_FAIL_WRITE_SERVICE=f"{BASE},{scoped(w.config_dir)}",
            ACC_KC_RETRY_BUDGET="2",
        )

        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertNotEqual(fresh, self.old)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], self.old)
        self.assertIn(
            self.old["accessToken"], self.server(w)["access"]
        )  # the profile still says a@x

        again = w.run("tick")
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], fresh)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], fresh)
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assert_no_token_anywhere(w, r, again)

    def test_a_used_pair_never_goes_into_runtime_while_its_access_token_is_still_valid(
        self,
    ):
        # b@x's copy holds a used refresh token (its forced refresh lost the pair) with an access token
        # that hasn't expired: a manual switch must not hand it to the sessions
        w = self.world(("b@x", 10))
        used = w.managed("b@x")["claudeAiOauth"]["refreshToken"]
        w.state(consumed_tokens={"b@x": [mark(used)]})

        r = w.run("switch", "b@x")

        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], self.old)
        self.assertIn("b@x", w.saved_state()["needs_login"])
        self.assertEqual(w.calls(TOKEN), [])

    def test_no_refresh_starts_when_the_state_cant_be_written(self):
        # the marker goes to the state before the request (write-ahead); a full disk or a read-only
        # state folder means no request at all (fail closed), not a pair nobody can protect
        w = self.world()
        os.chmod(w.state_dir, 0o500)
        self.addCleanup(os.chmod, w.state_dir, 0o755)
        r = self.py(w, "creds, note = a.ensure_fresh(a.Account(sys.argv[2], 'a@x'), a.load_config()); print(creds is None, note)",
                    w.ids["a@x"])  # fmt: skip
        os.chmod(w.state_dir, 0o755)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("True"), r.stdout)
        self.assertEqual(w.calls(TOKEN), [])
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], self.old)

    def test_a_full_disk_after_the_marker_never_costs_the_pair(self):
        # the marker went in, then the disk filled: lifting the parked account's needs_login (a state
        # write) comes after the Keychain writes, and its failure doesn't stop them
        w = self.world()
        w.state(needs_login={"a@x": int(time.time()) - 7 * 3600}, needs_login_token={"a@x": "0" * 12})
        r = self.py(w, "orig, n = a.save_state, [0]\n"
                       "def full(state):\n"
                       "    n[0] += 1\n"
                       "    if n[0] > 1: raise OSError(28, 'No space left on device')\n"
                       "    orig(state)\n"
                       "a.save_state = full\n"
                       "creds, note = a.ensure_fresh(a.Account(sys.argv[2], 'a@x'), a.load_config()); print(creds is not None)",
                    w.ids["a@x"])  # fmt: skip
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")
        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertIn(fresh["refreshToken"], self.server(w)["refresh"])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)

    def test_no_answer_after_the_request_went_out_keeps_the_marker(self):
        # the server used the token up, but its answer never arrived (curl timed out): the marker stays,
        # the account is parked, and no tick sends that token again
        w = self.world()
        self.set_server(w, drop_answer=True)
        r = w.run("tick")
        self.assertIn(self.old["refreshToken"], self.server(w)["consumed"])
        self.assertIn("brak odpowiedzi serwera po wysłaniu", r.stderr)
        self.assertIn("a@x", w.saved_state()["needs_login"])
        self.assertIn(
            mark(self.old["refreshToken"]), w.saved_state()["consumed_tokens"]["a@x"]
        )
        self.set_server(w, drop_answer=False)
        w.state(needs_login={"a@x": int(time.time()) - 7 * 3600})
        again = w.run("tick")
        self.assertEqual(len(w.calls(TOKEN)), 1)
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)
        self.assert_no_token_anywhere(w, r, again)

    def test_a_request_that_never_left_drops_the_marker(self):
        # offline (curl couldn't connect): the server never saw the token, so it isn't used up
        w = self.world()
        self.set_server(w, offline=True)
        w.run("tick")
        self.assertNotIn(
            mark(self.old["refreshToken"]),
            w.saved_state().get("consumed_tokens", {}).get("a@x", []),
        )
        self.assertNotIn("a@x", w.saved_state().get("needs_login", {}))
        self.set_server(w, offline=False)
        w.run("tick")
        self.assertEqual(len(w.calls(TOKEN)), 1)
        self.assertIn(
            w.managed("a@x")["claudeAiOauth"]["refreshToken"], self.server(w)["refresh"]
        )
        self.assertEqual(self.server(w).get("reuse_attempts", 0), 0)

    def test_the_markers_are_kept_per_account(self):
        # many rotations of another account never push out the marker of one that stands
        w = self.world()
        r = self.py(w, "aa, cc = a.Account('id-a', 'a@x'), a.Account('id-c', 'c@x')\n"
                       "a.remember_consumed(aa, 'rt-a-used')\n"
                       "for i in range(40): a.remember_consumed(cc, f'rt-c-{i}')\n"
                       "marks = a.consumed_marks(a.load_state())\n"
                       "print(a.consumed('{\"claudeAiOauth\": {\"refreshToken\": \"rt-a-used\"}}', aa), len(marks['c@x']),"
                       " a.consumed('{\"claudeAiOauth\": {\"refreshToken\": \"rt-a-used\"}}', cc))")  # fmt: skip
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["True", "16", "False"])

    def test_adopt_never_takes_a_used_pair_from_runtime(self):
        # the runtime holds a used pair whose access token still answers: adopt (a manual switch with
        # no account recognized) must not write it over the account's live copy
        w = Env()
        self.addCleanup(shutil.rmtree, w.home, True)
        live = w.account("a@x")["claudeAiOauth"]
        self.old = live
        used = dict(live, refreshToken="rt-a@x-used")
        w.runtime({"claudeAiOauth": used})
        w.write()
        w.state(consumed_tokens={"a@x": [mark("rt-a@x-used")]})
        r = self.py(w, "a.adopt_runtime(a.load_accounts(), a.load_config())")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], live)

    def test_a_lost_write_parks_the_account_at_once(self):
        # one ensure_fresh and nothing after it (a tick would come back to the account and find the
        # used token): the mark comes from the failed write itself
        w = self.world()
        r = self.py(w, "creds, note = a.ensure_fresh(a.Account(sys.argv[2], 'a@x'), a.load_config()); print(creds is None)",
                    w.ids["a@x"], FAKE_FAIL_WRITE_ALL="1", ACC_KC_RETRY_BUDGET="2")  # fmt: skip
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
        for patcher in (
            mock.patch.dict(os.environ, self.w.env()),
            mock.patch.object(accswitch, "KEYCHAIN_USER", USER),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for cache in (accswitch._KC_SEEN, accswitch._TOOLS):
            cache.clear()
            self.addCleanup(cache.clear)
        # these tests write and time out writes: never through the real `security` (login keychain)
        self.assertEqual(
            accswitch.tool("security"), os.path.join(HERE, "fakes", "security")
        )

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
