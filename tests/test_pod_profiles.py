"""Profile Claude w Pod (upstream #26801): accswitch widzi ich limity i niczego w nich nie zmienia.

Konto dodane w Pod ma własny katalog konfiguracji (<userData>/claude-profiles/<id>/home) i własny
wpis w Pęku kluczy, który trzyma i odświeża Pod. Twarda reguła z accswitch.py: wpisu profilu nigdy
nie zapisujemy, nie kasujemy i nie odświeżamy, jego access tokenu używamy tylko do odczytu limitów,
póki jest ważny, i nigdy nie kopiujemy jego danych logowania do wpisu bazowego ani do sejfu kont.
Każdy test sprawdza to na atrapach `security` i API po prawdziwym przebiegu skryptu.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_pod_profiles
"""
import hashlib
import json
import os
import sys
import time
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_e2e import BASE, USER, Env, scoped  # noqa: E402


def profile_service(home):
    """Wpis Claude Code dla katalogu: sha256(NFC(katalog))[:8], jak liczy go Claude Code i Pod."""
    home = unicodedata.normalize("NFC", home)
    return f"{BASE}-{hashlib.sha256(home.encode()).hexdigest()[:8]}"


def add_profile(w, email, profile_id="p1", expired=False, alive=True, weekly_used=20, user_data=None,
                marker=True, runtime="host"):
    """Profil tak, jak zostawia go Pod po zalogowaniu: marker, .claude.json z kontem i wpis z tokenami."""
    root = os.path.join(user_data or w.user_data, "claude-profiles", profile_id)
    home = os.path.join(root, "home")
    os.makedirs(home)
    if marker:
        with open(os.path.join(root, "profile.json"), "w") as f:
            json.dump({"version": 1, "accountId": profile_id, "runtime": runtime, "distro": None}, f)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"oauthAccount": {"emailAddress": email, "subscriptionCreatedAt": "2026-09-20T08:00:00Z"}}, f)
    w.server["counter"] += 1
    n = w.server["counter"]
    access, refresh = f"at-{email}-{n}", f"rt-{email}-{n}"
    if alive:
        w.server["access"][access] = email
        w.server["refresh"][refresh] = email
    reset = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 3600))
    week = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 86400))
    w.server["usage"][email] = {"five_hour": {"utilization": 10, "resets_at": reset},
                                "seven_day": {"utilization": weekly_used, "resets_at": week}}
    expires = (time.time() - 60 if expired else time.time() + 8 * 3600) * 1000
    blob = {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": int(expires),
                              "rateLimitTier": "default_claude_max_20x"}}
    service = profile_service(home)
    w.keychain[f"{service}|{USER}"] = json.dumps(blob)
    return {"home": home, "service": service, "blob": json.dumps(blob), "access": access, "refresh": refresh}


class ProfileWorld(unittest.TestCase):
    def world(self):
        w = Env(pod=True)
        self.addCleanup(__import__("shutil").rmtree, w.home, True)
        return w

    def keychain(self, w):
        with open(os.path.join(w.fake, "keychain.json")) as f:
            return json.load(f)

    def assert_untouched(self, w, profile):
        """Reguła: wpis profilu bez zapisu, kasowania i odświeżenia; jego tokeny nigdzie indziej."""
        key = f"{profile['service']}|{USER}"
        self.assertEqual(self.keychain(w).get(key), profile["blob"], "wpis profilu się zmienił")
        writes = [(cmd, k) for cmd, k in w.keychain_calls() if cmd != "find-generic-password"]
        self.assertFalse([c for c in writes if c[1].startswith(profile["service"] + "|")], writes)
        server = json.load(open(os.path.join(w.fake, "server.json")))
        self.assertNotIn(profile["refresh"], server.get("consumed", {}), "refresh token profilu poszedł do API")
        self.assertIn(profile["refresh"], server["refresh"], "refresh token profilu przestał działać")
        for other, value in self.keychain(w).items():
            if other != key:
                self.assertNotIn(profile["access"], value, f"access token profilu w {other}")
                self.assertNotIn(profile["refresh"], value, f"refresh token profilu w {other}")
        # access token profilu idzie tylko po limity
        auth_log = os.path.join(w.fake, "curl-auth.log")
        sent = [json.loads(line) for line in open(auth_log)] if os.path.exists(auth_log) else []
        used = {url for url, token in sent if token == profile["access"]}
        self.assertLessEqual(used, {"https://api.anthropic.com/api/oauth/usage"}, used)

    def snapshot(self, w):
        r = w.run("status", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def row(self, snap, email):
        return next((a for a in snap["accounts"] if a["email"] == email), None)


class ProfileUsageTest(ProfileWorld):
    def test_profile_shows_usage_only(self):
        w = self.world()
        a = w.account("a@x")
        w.runtime(a)
        p = add_profile(w, "p@x", weekly_used=42)
        w.write()

        snap = self.snapshot(w)

        row = self.row(snap, "p@x")
        self.assertIsNotNone(row, snap["accounts"])
        self.assertEqual(row["source"], "profile")
        self.assertEqual(row["id"], "profile:p1")
        self.assertEqual(row["weekly"]["used"], 42)
        self.assertEqual(row["status"], "ok")
        self.assertFalse(row["stale"])
        self.assertFalse(row["usable"])
        self.assertIsNone(row["queue"])
        self.assertFalse(row["active"])
        self.assertEqual(row["tier"], "Max 20x")
        self.assert_untouched(w, p)

    def test_expired_token_is_stale_and_never_refreshed(self):
        w = self.world()
        p = add_profile(w, "p@x", expired=True)
        w.write()

        snap = self.snapshot(w)

        row = self.row(snap, "p@x")
        self.assertTrue(row["stale"])
        self.assertEqual(row["status"], "error")  # nic jeszcze nie odczytano
        self.assertIn("wygasł", row["note"])
        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assertEqual(w.calls("/api/oauth/usage"), [])
        self.assert_untouched(w, p)

    def test_expired_token_keeps_last_known_numbers_as_stale(self):
        w = self.world()
        p = add_profile(w, "p@x", weekly_used=30)
        w.write()
        self.assertFalse(self.row(self.snapshot(w), "p@x")["stale"])
        # Pod nie odświeżył jeszcze tokenu, a odczyt z pamięci się zestarzał
        keychain = self.keychain(w)
        blob = json.loads(keychain[f"{p['service']}|{USER}"])
        blob["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
        p["blob"] = keychain[f"{p['service']}|{USER}"] = json.dumps(blob)
        json.dump(keychain, open(os.path.join(w.fake, "keychain.json"), "w"))
        w.age_usage_cache(31 * 60)

        row = self.row(self.snapshot(w), "p@x")

        self.assertTrue(row["stale"])
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["weekly"]["used"], 30)
        self.assertGreater(row["data_age"], 30 * 60)
        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assert_untouched(w, p)

    def test_rejected_token_is_stale_and_never_refreshed(self):
        # token unieważniony przed czasem (np. Pod go właśnie obrócił): 401 nie prowadzi do odświeżenia
        w = self.world()
        p = add_profile(w, "p@x", alive=False)
        w.server["refresh"][p["refresh"]] = "p@x"
        w.write()

        row = self.row(self.snapshot(w), "p@x")

        self.assertTrue(row["stale"])
        self.assertIn("HTTP 401", row["note"])
        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assert_untouched(w, p)

    def test_selected_profile_is_marked(self):
        w = self.world()
        p = add_profile(w, "p@x")
        with open(os.path.join(w.user_data, "claude-profiles", "selected-host"), "w") as f:
            f.write(p["home"])
        w.write()

        self.assertTrue(self.row(self.snapshot(w), "p@x")["host_selected"])

    def test_own_account_with_the_same_email_wins(self):
        w = self.world()
        a = w.account("a@x")
        w.runtime(a)
        p = add_profile(w, "a@x")
        w.write()

        rows = [r for r in self.snapshot(w)["accounts"] if r["email"] == "a@x"]

        self.assertEqual([r.get("source") for r in rows], [None])
        self.assert_untouched(w, p)

    def test_unfinished_or_wsl_profiles_are_left_out(self):
        w = self.world()
        add_profile(w, "half@x", profile_id="p1", marker=False)
        add_profile(w, "wsl@x", profile_id="p2", runtime="wsl")
        w.write()

        emails = [r["email"] for r in self.snapshot(w)["accounts"]]

        self.assertNotIn("half@x", emails)
        self.assertNotIn("wsl@x", emails)

    def test_entry_name_is_nfc(self):
        # Claude Code i Pod liczą nazwę wpisu z NFC katalogu; userData z „ż” rozłożonym (NFD)
        w = self.world()
        data = os.path.join(w.home, unicodedata.normalize("NFD", "Dane Poża"))
        p = add_profile(w, "p@x", user_data=data)
        w.write()

        r = w.run("status", "--json", CLAUDE_ACC_HOST_DATA=data)

        row = self.row(json.loads(r.stdout), "p@x")
        self.assertEqual(row["weekly"]["used"], 20, row)
        self.assert_untouched(w, p)

    def test_text_status_lists_profiles(self):
        w = self.world()
        a = w.account("a@x")
        w.runtime(a)
        p = add_profile(w, "p@x")
        w.write()

        r = w.run("status")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("profile Pod (tylko limity", r.stdout)
        self.assertIn("p@x", r.stdout)
        self.assert_untouched(w, p)


class ProfileRotationTest(ProfileWorld):
    def test_tick_never_switches_to_a_profile(self):
        # aktywne konto wypalone, jedyny zapas ma profil Pod: automat go nie bierze
        w = self.world()
        a = w.account("a@x", weekly_used=99)
        w.runtime(a)
        p = add_profile(w, "p@x", weekly_used=5)
        w.write()

        w.run("tick")

        with open(os.path.join(w.state_dir, "switch.log")) as f:
            self.assertIn("brak konta z zapasem", f.read())
        self.assertEqual(w.entry(BASE)["claudeAiOauth"]["accessToken"], a["claudeAiOauth"]["accessToken"])
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"]["accessToken"],
                         a["claudeAiOauth"]["accessToken"])
        self.assert_untouched(w, p)

    def test_tick_near_profile_expiry_refreshes_nothing_of_the_profile(self):
        w = self.world()
        a = w.account("a@x", expires_in=3 * 60)
        w.runtime(a)
        p = add_profile(w, "p@x")
        keychain_blob = json.loads(w.keychain[f"{p['service']}|{USER}"])
        keychain_blob["claudeAiOauth"]["expiresAt"] = int((time.time() + 30) * 1000)
        p["blob"] = w.keychain[f"{p['service']}|{USER}"] = json.dumps(keychain_blob)
        w.write()

        w.run("tick")
        w.run("status", "--json")

        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assert_untouched(w, p)

    def test_switch_to_a_profile_is_refused(self):
        w = self.world()
        a = w.account("a@x")
        w.runtime(a)
        p = add_profile(w, "p@x")
        w.write()

        r = w.run("switch", "p@x")

        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"]["accessToken"], a["claudeAiOauth"]["accessToken"])
        self.assert_untouched(w, p)


if __name__ == "__main__":
    unittest.main()
