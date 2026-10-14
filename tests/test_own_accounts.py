"""Konta claude-acc: `add`, `adopt` i przełączenie, które nigdy nie kasuje nieznanego logowania.

Od Pod z profilami (#26801) konto dodane w Pod jest jego profilem: claude-acc pokazuje mu tylko
limity i go nie przełącza. Właścicielem kont jest więc claude-acc: `add` dodaje konto przez
logowanie w przeglądarce, `adopt` przejmuje logowanie zrobione przez /login w sesji Claude Code,
a przełączenie najpierw zapisuje każde logowanie z wpisów runtime, zanim je nadpisze. Wszystko na
atrapach `security`, `claude` i API po prawdziwym przebiegu skryptu.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_own_accounts
"""
import json
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_e2e import BASE, MANAGED, USER, Env, scoped  # noqa: E402
from test_pod_profiles import add_profile  # noqa: E402


class World(unittest.TestCase):
    def world(self, **kwargs):
        w = Env(**kwargs)
        self.addCleanup(shutil.rmtree, w.home, True)
        return w

    def usage(self, w, email, weekly_used=10):
        reset = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 3600))
        week = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 86400))
        w.server["usage"][email] = {"five_hour": {"utilization": 10, "resets_at": reset},
                                    "seven_day": {"utilization": weekly_used, "resets_at": week}}

    def login_elsewhere(self, w, email, services=(BASE,), expired_for=None, access_alive=True, mcp=None):
        """/login w sesji Claude Code: para tokenów konta, której nie zna żadna kopia, tylko we wpisach runtime."""
        w.server["counter"] += 1
        n = w.server["counter"]
        access, refresh = f"at-{email}-{n}", f"rt-{email}-{n}"
        if access_alive:
            w.server["access"][access] = email
        w.server["refresh"][refresh] = email
        self.usage(w, email)
        expires = time.time() - expired_for if expired_for is not None else time.time() + 8 * 3600
        oauth = {"accessToken": access, "refreshToken": refresh, "expiresAt": int(expires * 1000)}
        for service in services:
            blob = {"claudeAiOauth": oauth}
            if mcp is not None:
                blob["mcpOAuth"] = mcp
            w.keychain[f"{service}|{USER}"] = json.dumps(blob)
        return oauth

    def own_accounts(self, w):
        """{e-mail: id} kont dodanych w claude-acc ($STATE/accounts/<id>/auth/oauth-account.json)."""
        root = os.path.join(w.state_dir, "accounts")
        found = {}
        for acct_id in os.listdir(root) if os.path.isdir(root) else []:
            with open(os.path.join(root, acct_id, "auth", "oauth-account.json")) as f:
                found[json.load(f)["emailAddress"]] = acct_id
        return found

    def copy(self, w, acct_id):
        return w.entry(w.service, acct_id)

    def server(self, w):
        with open(os.path.join(w.fake, "server.json")) as f:
            return json.load(f)

    def writes(self, w):
        return [(cmd, key) for cmd, key in w.keychain_calls() if cmd != "find-generic-password"]

    def snapshot(self, w):
        r = w.run("status", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return {row["email"]: row for row in json.loads(r.stdout)["accounts"]}

    def assert_no_secret_in_argv(self, w, *oauths):
        with open(os.path.join(w.fake, "security-argv.log")) as f:
            argv = f.read()
        for oauth in oauths:
            self.assertNotIn(oauth["accessToken"], argv)
            self.assertNotIn(oauth["refreshToken"], argv)


class AddTest(World):
    def test_add_makes_a_normal_switchable_account(self):
        w = self.world()
        a = w.account("a@x")
        w.runtime(a)
        self.usage(w, "new@x")
        w.write()

        r = w.run("add", "new@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dodano new@x", r.stdout)
        own = self.own_accounts(w)
        self.assertEqual(list(own), ["new@x"])
        oauth = self.copy(w, own["new@x"])["claudeAiOauth"]
        self.assertIn(oauth["refreshToken"], self.server(w)["refresh"])
        self.assertIsNone(w.login_entry())
        # wpisy runtime zostają przy aktywnym koncie: add niczego nie przełącza
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], a["claudeAiOauth"])
        rows = self.snapshot(w)
        self.assertTrue(rows["new@x"]["usable"])
        self.assertNotEqual(rows["new@x"].get("source"), "profile")
        self.assert_no_secret_in_argv(w, oauth)

        r = w.run("switch", "new@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for service in (BASE, scoped(w.config_dir)):
            self.assertEqual(w.entry(service)["claudeAiOauth"], self.copy(w, own["new@x"])["claudeAiOauth"])

    def test_add_without_email_takes_whatever_the_browser_signs_in(self):
        w = self.world()
        w.account("a@x")
        self.usage(w, "new@x")
        w.write()

        r = w.run("add", FAKE_LOGIN_AS="new@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(list(self.own_accounts(w)), ["new@x"])

    def test_add_of_a_known_account_signs_it_in_again(self):
        w = self.world()
        dead = w.account("a@x", alive=False)
        w.runtime(dead)
        w.write()

        r = w.run("add", FAKE_LOGIN_AS="a@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("już jest", r.stdout)
        self.assertEqual(self.own_accounts(w), {})
        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertNotEqual(fresh["accessToken"], dead["claudeAiOauth"]["accessToken"])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)

    def test_add_with_a_known_email_is_login(self):
        w = self.world()
        dead = w.account("a@x", alive=False)
        w.write()

        r = w.run("add", "a@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("zalogowano a@x", r.stdout)
        self.assertEqual(self.own_accounts(w), {})
        self.assertNotEqual(w.managed("a@x")["claudeAiOauth"]["accessToken"], dead["claudeAiOauth"]["accessToken"])

    def test_add_refuses_when_the_browser_signs_in_someone_else(self):
        w = self.world()
        w.write()

        r = w.run("add", "new@x", FAKE_LOGIN_AS="other@x")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("other@x", r.stdout)
        self.assertEqual(self.own_accounts(w), {})
        self.assertIsNone(w.login_entry())

    def test_failed_sign_in_adds_nothing(self):
        w = self.world()
        w.write()

        r = w.run("add", "new@x", FAKE_LOGIN_FAIL="1")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("logowanie nieudane", r.stdout)
        self.assertEqual(self.own_accounts(w), {})
        self.assertEqual([k for _, k in self.writes(w) if not k.startswith(BASE + "-")], [])

    def test_login_of_an_unknown_email_points_at_add(self):
        w = self.world()
        w.write()

        r = w.run("login", "new@x")

        self.assertEqual(r.returncode, 1)
        self.assertIn("claude-acc add new@x", r.stdout)

    def test_pod_add_goes_to_pods_service_and_hides_the_profile_row(self):
        # to samo konto co profil Pod: od teraz zwykłe konto claude-acc, a profil zostaje nietknięty
        w = self.world(pod=True)
        profile = add_profile(w, "new@x")
        w.write()

        r = w.run("add", "new@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        own = self.own_accounts(w)
        self.assertNotEqual(w.service, MANAGED)
        self.assertIsNotNone(w.entry(w.service, own["new@x"]))
        rows = json.loads(w.run("status", "--json").stdout)["accounts"]
        self.assertEqual([(x["email"], x.get("source")) for x in rows], [("new@x", None)])
        with open(os.path.join(w.fake, "keychain.json")) as f:
            self.assertEqual(json.load(f)[f"{profile['service']}|{USER}"], profile["blob"])
        self.assertIn(profile["refresh"], self.server(w)["refresh"])


class AdoptTest(World):
    def test_adopt_takes_the_session_login_and_makes_it_active(self):
        # 14.10: /login w sesji zapisał nowe konto tylko do wpisu bazowego, a wpis katalogu
        # zarządzanego dalej trzymał stare: claude-acc nie znał nowego, a przełączenie by je skasowało
        w = self.world()
        a = w.account("a@x", weekly_used=100)
        w.runtime(a, mcp={"srv": "runtime-own"})
        new = self.login_elsewhere(w, "new@x", mcp={"srv": "base-own"})
        w.write()

        r = w.run("adopt")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("przejęto new@x", r.stdout)
        own = self.own_accounts(w)
        self.assertEqual(self.copy(w, own["new@x"]), {"claudeAiOauth": new})  # bez cudzych tokenów MCP
        self.assertEqual(w.entry(BASE), {"claudeAiOauth": new, "mcpOAuth": {"srv": "base-own"}})
        self.assertEqual(w.entry(scoped(w.config_dir)), {"claudeAiOauth": new, "mcpOAuth": {"srv": "runtime-own"}})
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], a["claudeAiOauth"])
        self.assertEqual(self.server(w).get("consumed", {}), {})  # ważnego tokenu nikt nie odświeżał
        rows = self.snapshot(w)
        self.assertTrue(rows["new@x"]["active"])
        self.assertEqual(w.saved_state()["active_email"], "new@x")
        self.assert_no_secret_in_argv(w, new)

    def test_adopt_with_nothing_unknown_writes_nothing(self):
        w = self.world()
        a = w.account("a@x")
        w.runtime(a)
        w.write()

        r = w.run("adopt")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nie ma żywego logowania spoza", r.stdout)
        self.assertEqual(self.writes(w), [])
        self.assertEqual(self.own_accounts(w), {})

    def test_dead_login_is_nobodys(self):
        w = self.world()
        w.account("a@x")
        self.login_elsewhere(w, "gone@x", services=(BASE, scoped(w.config_dir)), access_alive=False)
        w.write()

        r = w.run("adopt")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.own_accounts(w), {})
        self.assertEqual(self.writes(w), [])

    def test_an_expired_unknown_login_is_never_refreshed(self):
        # wygasła para spoza kopii bywa starą parą, której refresh token sesja już zużyła: jego
        # ponowne użycie unieważnia całe konto, więc takiego logowania nie odświeżamy wcale
        w = self.world()
        w.account("a@x")
        self.login_elsewhere(w, "new@x", expired_for=3600, access_alive=False)
        w.write()

        r = w.run("adopt")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nie ma żywego logowania", r.stdout)
        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assertEqual(self.own_accounts(w), {})
        self.assertEqual(self.writes(w), [])

    def test_adopt_says_so_when_the_profile_api_is_down(self):
        w = self.world()
        w.account("a@x")
        new = self.login_elsewhere(w, "new@x")
        w.server["profile_down_for"] = ["new@x"]
        w.write()

        r = w.run("adopt")

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nie wiem", r.stdout)
        self.assertEqual(self.own_accounts(w), {})
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], new)

    def test_adopt_never_reads_a_pod_profile_item(self):
        # zarządzany katalog wskazuje na profil Pod: jego wpis należy do Pod (reguła z accswitch.py)
        w = self.world(pod=True)
        w.account("a@x")
        profile = add_profile(w, "p@x")
        cfg_path = os.path.join(w.state_dir, "config.json")
        cfg = json.load(open(cfg_path))
        cfg["config_dir"] = profile["home"]
        json.dump(cfg, open(cfg_path, "w"))
        w.write()

        r = w.run("adopt")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.own_accounts(w), {})
        self.assertEqual([k for _, k in w.keychain_calls() if k.startswith(profile["service"] + "|")], [])


class SwitchKeepsUnknownLoginTest(World):
    def test_switch_saves_a_session_login_before_overwriting_it(self):
        w = self.world()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        new = self.login_elsewhere(w, "new@x")
        w.write()

        r = w.run("switch", "b@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        own = self.own_accounts(w)
        self.assertEqual(self.copy(w, own["new@x"])["claudeAiOauth"], new)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], w.managed("b@x")["claudeAiOauth"])

        r = w.run("switch", "new@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"]["refreshToken"], new["refreshToken"])
        self.assertIn(new["refreshToken"], self.server(w)["refresh"])

    def test_tick_adopts_a_session_login_and_keeps_it_in_place(self):
        # 14.10 na żywo: /login w sesji w wpisie bazowym, wpis katalogu zarządzanego przy starym,
        # wypalonym koncie. Tick ma przejąć nowe konto i zostawić je sesjom, a nie cofnąć do starego
        w = self.world()
        a = w.account("a@x", weekly_used=100)
        w.account("b@x", weekly_used=20)
        w.runtime(a, services=[scoped(w.config_dir)])
        new = self.login_elsewhere(w, "new@x")
        w.write()

        r = w.run("tick")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("new@x", self.own_accounts(w))
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], new)
        saved = json.load(open(os.path.join(w.state_dir, "status.json")))
        self.assertEqual(saved["active_email"], "new@x")
        self.assertIn("new@x", [x["email"] for x in saved["accounts"]])
        self.assertTrue(any("new@x" in n for n in w.notifications()))

    def test_tick_changes_nothing_while_it_cannot_tell_whose_login_it_is(self):
        w = self.world()
        a = w.account("a@x", weekly_used=100)
        w.account("b@x", weekly_used=20)
        w.runtime(a, services=[scoped(w.config_dir)])
        new = self.login_elsewhere(w, "new@x")
        w.server["profile_down_for"] = ["new@x"]
        w.write()

        r = w.run("tick")

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], new)
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], a["claudeAiOauth"])
        self.assertEqual(self.own_accounts(w), {})

    def test_switch_refuses_when_it_cannot_tell_whose_login_it_would_overwrite(self):
        w = self.world()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a, services=[scoped(w.config_dir)])
        new = self.login_elsewhere(w, "new@x")
        w.server["profile_down_for"] = ["new@x"]
        w.write()

        r = w.run("switch", "b@x")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nie nadpisuję", r.stdout + r.stderr)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], new)
        self.assertEqual(self.own_accounts(w), {})

    def test_switch_of_a_rotated_known_login_adds_no_duplicate(self):
        # sesja obróciła token aktywnego konta: to dalej a@x, nie nowe konto
        w = self.world()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.write()
        live = w.session_refresh("a@x")

        r = w.run("switch", "b@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.own_accounts(w), {})
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], live["claudeAiOauth"])


if __name__ == "__main__":
    unittest.main()
