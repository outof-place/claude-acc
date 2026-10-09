"""Testy e2e claude-acc na atrapach Pęku kluczy, API Anthropic i `claude`.

Każdy test stawia osobny $HOME z kontami Orca, uruchamia prawdziwy skrypt jako
proces i sprawdza to, co widać z zewnątrz: wpisy w Pęku kluczy, stan i kod
wyjścia. Żywe konta nie są dotykane.

Uruchomienie: /usr/bin/python3 -m unittest discover -s ~/.local/share/claude-acc/tests
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "accswitch.py")
ACC = os.path.join(os.path.dirname(HERE), "acc.py")
FAKES = os.path.join(HERE, "fakes")
USER = "tester"
MANAGED = "Orca Claude Code Managed Credentials"
BASE = "Claude Code-credentials"


def scoped(config_dir):
    return f"{BASE}-{hashlib.sha256(config_dir.encode()).hexdigest()[:8]}"


class Env:
    """Świat jednego testu: konta, serwer i Pęk kluczy w plikach."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="claude-acc-test-")
        self.fake = os.path.join(self.home, "fake")
        self.state_dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.fake)
        os.makedirs(self.state_dir)
        self.config_dir = os.path.join(self.home, ".claude")
        self.other_dir = os.path.join(self.home, ".claude-work")
        json.dump({"config_dir": self.config_dir, "other_config_dirs": [self.other_dir],
                   "hard_session_left": 5, "hard_weekly_left": 3, "min_weekly_left": 6,
                   "min_session_left": 10, "last_resort": [], "never": [],
                   # prawdziwe CLI Depot na maszynie testującej nie może dostać tokenów z atrap
                   "depot_sync": False},
                  open(os.path.join(self.state_dir, "config.json"), "w"))
        self.server = {"access": {}, "refresh": {}, "usage": {}, "log": [], "counter": 0}
        self.keychain = {}
        self.ids = {}

    # --- budowanie świata ---

    def account(self, email, session_used=10, weekly_used=10, alive=True, expired=False, mcp=None,
                expires_in=8 * 3600):
        acct_id = f"id-{email.split('@')[0]}"
        self.ids[email] = acct_id
        info = os.path.join(self.home, "Library/Application Support/orca/claude-accounts", acct_id, "auth")
        os.makedirs(info)
        json.dump({"emailAddress": email}, open(os.path.join(info, "oauth-account.json"), "w"))
        self.server["counter"] += 1
        n = self.server["counter"]
        access, refresh = f"at-{email}-{n}", f"rt-{email}-{n}"
        if alive:
            self.server["access"][access] = email
            self.server["refresh"][refresh] = email
        reset = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 3600))
        week = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3 * 86400))
        self.server["usage"][email] = {"five_hour": {"utilization": session_used, "resets_at": reset},
                                       "seven_day": {"utilization": weekly_used, "resets_at": week}}
        expires = (time.time() - 60 if expired else time.time() + expires_in) * 1000
        blob = {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": int(expires)}}
        if mcp is not None:
            blob["mcpOAuth"] = mcp
        self.keychain[f"{MANAGED}|{acct_id}"] = json.dumps(blob)
        return blob

    def runtime(self, blob, mcp=None, services=None):
        """Wpisy runtime zarządzanego katalogu z danymi konta i własnymi tokenami MCP."""
        blob = dict(blob)
        if mcp is not None:
            blob["mcpOAuth"] = mcp
        for service in services or (scoped(self.config_dir), BASE):
            self.keychain[f"{service}|{USER}"] = json.dumps(blob)

    def orca_selects(self, email):
        """Orca w trybie kont zarządzanych: wybrane konto w jej ustawieniach, jak zapisuje je Orca."""
        profile = os.path.join(self.home, "Library/Application Support/orca/profiles/local-default")
        os.makedirs(profile, exist_ok=True)
        selected = self.ids[email] if email else None
        json.dump({"settings": {"activeClaudeManagedAccountId": selected,
                                "activeClaudeManagedAccountIdsByRuntime": {"host": selected, "wsl": {}}}},
                  open(os.path.join(profile, "orca-data.json"), "w"))

    def write(self):
        json.dump(self.server, open(os.path.join(self.fake, "server.json"), "w"))
        json.dump(self.keychain, open(os.path.join(self.fake, "keychain.json"), "w"))

    def state(self, **fields):
        path = os.path.join(self.state_dir, "state.json")
        current = json.load(open(path)) if os.path.exists(path) else {}
        current.update(fields)
        json.dump(current, open(path, "w"))

    # --- uruchamianie ---

    def env(self, **extra):
        env = {"HOME": self.home, "USER": USER, "PATH": f"{FAKES}:/usr/bin:/bin"}
        env.update(extra)
        return env

    def run(self, *args, **extra):
        return subprocess.run(["/usr/bin/python3", SCRIPT, *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def spawn(self, *args, **extra):
        return subprocess.Popen(["/usr/bin/python3", SCRIPT, *args], env=self.env(**extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def session_refresh(self, email, service=BASE):
        """Sesja Claude Code odświeża token konta i zapisuje nową parę tylko do swojego wpisu."""
        server = json.load(open(os.path.join(self.fake, "server.json")))
        keychain = json.load(open(os.path.join(self.fake, "keychain.json")))
        blob = json.loads(keychain[f"{service}|{USER}"])
        old = blob["claudeAiOauth"]["refreshToken"]
        assert server["refresh"].pop(old) == email
        server.setdefault("consumed", {})[old] = email
        server["counter"] += 1
        n = server["counter"]
        access, refresh = f"at-{email}-{n}", f"rt-{email}-{n}"
        server["access"][access] = email
        server["refresh"][refresh] = email
        blob["claudeAiOauth"] = {"accessToken": access, "refreshToken": refresh,
                                 "expiresAt": int((time.time() + 8 * 3600) * 1000)}
        keychain[f"{service}|{USER}"] = json.dumps(blob)
        json.dump(server, open(os.path.join(self.fake, "server.json"), "w"))
        json.dump(keychain, open(os.path.join(self.fake, "keychain.json"), "w"))
        return blob

    def set_usage(self, email, session_used=None, weekly_used=None, rate_limited=None):
        """Zmiana po stronie serwera w trakcie testu: reset okna, zużycie, 429."""
        path = os.path.join(self.fake, "server.json")
        server = json.load(open(path))
        if session_used is not None:
            server["usage"][email]["five_hour"]["utilization"] = session_used
        if weekly_used is not None:
            server["usage"][email]["seven_day"]["utilization"] = weekly_used
        if rate_limited is not None:
            server["rate_limited"] = rate_limited
        json.dump(server, open(path, "w"))

    def forget_usage_cache(self):
        """Kolejny przebieg czyta limity z serwera, a nie z pamięci podręcznej."""
        path = os.path.join(self.state_dir, "usage-cache.json")
        if os.path.exists(path):
            os.remove(path)

    # --- odczyt ---

    def pause(self):
        path = os.path.join(self.state_dir, "pause.json")
        return json.load(open(path)) if os.path.exists(path) else None

    def notifications(self):
        path = os.path.join(self.fake, "notify.log")
        return open(path).read().splitlines() if os.path.exists(path) else []

    def entry(self, service, account=USER):
        raw = json.load(open(os.path.join(self.fake, "keychain.json"))).get(f"{service}|{account}")
        return json.loads(raw) if raw else None

    def managed(self, email):
        return self.entry(MANAGED, self.ids[email])

    def calls(self, suffix):
        log = json.load(open(os.path.join(self.fake, "server.json")))["log"]
        return [u for u in log if u.endswith(suffix)]

    def keychain_calls(self):
        """[(polecenie, "usługa|konto")] wszystkich wywołań `security` w kolejności."""
        path = os.path.join(self.fake, "security.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [tuple(line.rstrip("\n").split(" ", 1)) for line in f]

    def forget_keychain_calls(self):
        path = os.path.join(self.fake, "security.log")
        if os.path.exists(path):
            os.remove(path)

    def saved_state(self):
        return json.load(open(os.path.join(self.state_dir, "state.json")))

    def login_entry(self):
        return self.entry(scoped(os.path.join(self.state_dir, "login")))


class LoginTest(unittest.TestCase):
    def test_login_keeps_mcp_tokens_everywhere(self):
        # konto aktywne padło: martwy token w kopii Orca i w runtime
        w = Env()
        dead = w.account("a@x", alive=False, mcp={"srv": "orca-copy"})
        w.runtime(dead, mcp={"srv": "runtime-own"})
        w.write()

        r = w.run("login", "a@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        managed = w.managed("a@x")
        self.assertNotEqual(managed["claudeAiOauth"]["accessToken"], dead["claudeAiOauth"]["accessToken"])
        self.assertEqual(managed.get("mcpOAuth"), {"srv": "orca-copy"})
        for service in (scoped(w.config_dir), BASE):
            live = w.entry(service)
            self.assertEqual(live["claudeAiOauth"], managed["claudeAiOauth"], service)
            self.assertEqual(live.get("mcpOAuth"), {"srv": "runtime-own"}, service)
        self.assertIsNone(w.login_entry())

    def test_login_as_other_account_changes_nothing(self):
        w = Env()
        before = w.account("a@x", alive=False)
        w.account("b@x")
        w.write()

        r = w.run("login", "a@x", FAKE_LOGIN_AS="b@x")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("b@x", r.stdout)
        self.assertEqual(w.managed("a@x"), before)
        self.assertIsNone(w.login_entry())

    def test_cancel_during_write_phase_finishes_consistently(self):
        # Anuluj po powrocie z przeglądarki nie może zostawić kopii Orca i runtime w rozjeździe
        w = Env()
        dead = w.account("a@x", alive=False)
        w.runtime(dead)
        w.write()

        proc = w.spawn("login", "a@x", FAKE_SLOW_WRITE_SERVICE=MANAGED)
        marker = os.path.join(w.fake, "slow-write-started")
        deadline = time.time() + 20
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(marker), "zapis do kopii Orca się nie zaczął")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)

        self.assertEqual(proc.returncode, 0, out + err)
        managed = w.managed("a@x")
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], managed["claudeAiOauth"])
        self.assertIsNone(w.login_entry())


class SwitchTest(unittest.TestCase):
    def test_switch_keeps_runtime_mcp_tokens(self):
        w = Env()
        a = w.account("a@x", mcp={"srv": "a-copy"})
        b = w.account("b@x", mcp={"srv": "b-copy"})
        w.runtime(a, mcp={"srv": "runtime-own"})
        w.write()

        r = w.run("switch", "b@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        live = w.entry(scoped(w.config_dir))
        self.assertEqual(live["claudeAiOauth"]["refreshToken"], b["claudeAiOauth"]["refreshToken"])
        self.assertEqual(live.get("mcpOAuth"), {"srv": "runtime-own"})

    def test_switch_works_while_usage_endpoint_rate_limits(self):
        # 429 z endpointu limitów blokował ręczne przełączanie na kwadrans, więc przełączało
        # się w menu Orca; token sprawdza teraz API profilu, a limity nie są potrzebne
        w = Env()
        a = w.account("a@x")
        b = w.account("b@x")
        w.runtime(a)
        w.server["rate_limited"] = True
        w.write()
        w.state(api_backoff_until=time.time() + 600)

        r = w.run("switch", "b@x")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"]["refreshToken"], b["claudeAiOauth"]["refreshToken"])
        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assertNotIn("b@x", w.saved_state().get("needs_login", {}))

    def test_switch_refuses_while_orca_has_its_own_account_selected(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.write()
        w.orca_selects("a@x")

        r = w.run("switch", "b@x")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("System default", r.stdout)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], a["claudeAiOauth"])


class StatusTest(unittest.TestCase):
    def test_status_json_shows_active_account_even_when_excluded_from_rotation(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.write()
        cfg = os.path.join(w.state_dir, "config.json")
        conf = json.load(open(cfg))
        conf["never"] = ["a@x"]
        json.dump(conf, open(cfg, "w"))
        w.orca_selects("a@x")

        snap = json.loads(w.run("status", "--json").stdout)

        active = [x for x in snap["accounts"] if x["active"]]
        self.assertEqual([x["email"] for x in active], ["a@x"])
        self.assertFalse(snap["foreign_runtime"])
        self.assertEqual(snap["orca_selected"], "a@x")

    def test_status_json_never_refreshes_tokens_a_session_may_hold(self):
        # b@x żyje w sesjach innego katalogu konfiguracji: odświeżenie z panelu
        # ścigałoby się z tymi sesjami o refresh token
        w = Env()
        a = w.account("a@x")
        b = w.account("b@x", expired=True)
        w.runtime(a)
        w.runtime(b, services=[scoped(w.other_dir)])
        w.write()

        r = w.run("status", "--json")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        emails = [x["email"] for x in json.loads(r.stdout)["accounts"]]
        self.assertEqual(sorted(emails), ["a@x", "b@x"])
        self.assertEqual(w.calls("/v1/oauth/token"), [])

    def test_tick_leaves_the_app_what_status_json_prints(self):
        # przy zamkniętym panelu aplikacja czyta migawkę z ticku, zamiast co minutę
        # uruchamiać `status --json`, więc obie muszą mówić to samo, także po przełączeniu
        w = Env()
        a = w.account("a@x", session_used=99)
        w.account("b@x", weekly_used=20)
        w.runtime(a)
        w.write()

        self.assertEqual(w.run("tick").returncode, 0)
        saved = json.load(open(os.path.join(w.state_dir, "status.json")))
        r = w.run("status", "--json")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        printed = json.loads(r.stdout)
        self.assertEqual(saved["active_email"], "b@x")
        self.assertEqual(saved["last_tick"], w.saved_state()["last_tick"])
        for snap in (saved, printed):  # zegar i wiek danych płyną między przebiegami
            snap.pop("generated_at")
            for row in snap["accounts"]:
                row.pop("data_age")
        self.assertEqual(saved, printed)


class PanelFreshnessTest(unittest.TestCase):
    def test_status_json_refreshes_idle_account_only_orca_holds(self):
        # nieaktywne konto, którego tokenu nie trzyma żadna sesja: panel sam go
        # odświeża, zamiast pokazywać dane sprzed kilkunastu godzin
        w = Env()
        a = w.account("a@x")
        b = w.account("b@x", expired=True, weekly_used=42)
        w.runtime(a)
        w.write()

        snap = json.loads(w.run("status", "--json").stdout)

        self.assertEqual(len(w.calls("/v1/oauth/token")), 1)
        self.assertNotEqual(w.managed("b@x")["claudeAiOauth"]["refreshToken"], b["claudeAiOauth"]["refreshToken"])
        row = next(x for x in snap["accounts"] if x["email"] == "b@x")
        self.assertEqual(row["weekly"]["used"], 42)
        self.assertEqual(row["status"], "ok")

    def test_rate_limit_backoff_grows_and_resets(self):
        # 15 minut po każdym 429 oślepiało automat, choć API wracało po paru minutach
        w = Env()
        a = w.account("a@x")
        w.runtime(a)
        w.server["rate_limited"] = True
        w.write()

        w.run("status", "--json")
        first = w.saved_state()["api_backoff_until"] - time.time()
        w.state(api_backoff_until=0)
        w.run("status", "--json")
        second = w.saved_state()["api_backoff_until"] - time.time()
        server = json.load(open(os.path.join(w.fake, "server.json")))
        server["rate_limited"] = False
        json.dump(server, open(os.path.join(w.fake, "server.json"), "w"))
        w.state(api_backoff_until=0)
        w.run("status", "--json")

        self.assertAlmostEqual(first, 120, delta=15)
        self.assertAlmostEqual(second, 240, delta=15)
        self.assertNotIn("api_backoff_until", w.saved_state())
        self.assertNotIn("api_backoff_step", w.saved_state())

    def test_status_json_stops_polling_usage_of_canceled_subscription(self):
        # API limitów odpowiada anulowanemu kontu 403, a panel pytał o nie co minutę,
        # przybliżając 429 dla wszystkich kont; w panelu wisiał przy tym błąd
        w = Env()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.server["subscription"] = {"b@x": "canceled"}
        w.write()
        w.state(identity={w.ids["b@x"]: {"ts": int(time.time()), "email": "b@x", "status": "canceled"}})

        w.run("status", "--json")
        snap = json.loads(w.run("status", "--json").stdout)

        self.assertEqual(len(w.calls("/api/oauth/usage")), 1)  # tylko aktywne konto, raz
        row = next(x for x in snap["accounts"] if x["email"] == "b@x")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["note"], "subskrypcja: canceled, automat pomija")
        self.assertIsNone(row["queue"])

    def test_renewed_subscription_returns_to_rotation_by_itself(self):
        # status anulowanego konta sprawdzany w profilu co godzinę: po odnowieniu konto
        # wraca do kolejki bez ręcznego grzebania w stanie (przy 12 h czekało pół dnia)
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=20)
        w.runtime(a)
        w.write()
        w.state(identity={w.ids["b@x"]: {"ts": int(time.time()) - 2 * 3600, "email": "b@x",
                                         "status": "canceled"}})

        snap = json.loads(w.run("status", "--json").stdout)

        row = next(x for x in snap["accounts"] if x["email"] == "b@x")
        self.assertEqual(row["subscription_status"], "active")
        self.assertTrue(row["usable"])
        self.assertIsNotNone(row["queue"])
        self.assertEqual(row["weekly"]["used"], 20)


class TickTest(unittest.TestCase):
    def test_tick_follows_session_refresh_instead_of_refreshing_itself(self):
        # Codzienne wylogowania: 5 minut przed końcem ważności sesja odświeża token we
        # wpisie bazowym, a tick odświeżał tę samą, już zużytą parę ze swojej kopii.
        # Serwer traktował to jako kradzież i unieważniał całe konto.
        w = Env()
        a = w.account("a@x", expires_in=3 * 60)
        w.account("b@x")
        w.runtime(a)
        w.write()
        live = w.session_refresh("a@x")

        w.run("tick")

        self.assertEqual(w.calls("/v1/oauth/token"), [])
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], live["claudeAiOauth"])
        self.assertEqual(w.entry(scoped(w.config_dir))["claudeAiOauth"], live["claudeAiOauth"])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], live["claudeAiOauth"])
        self.assertNotIn("a@x", w.saved_state().get("needs_login", {}))

    def test_tick_leaves_token_refresh_of_active_account_to_sessions(self):
        w = Env()
        a = w.account("a@x", expires_in=3 * 60)
        w.runtime(a)
        w.write()

        w.run("tick")

        self.assertEqual(w.calls("/v1/oauth/token"), [])

    def test_tick_refreshes_active_account_when_sessions_are_idle(self):
        # token wygasł dawno, więc żadna sesja go nie odświeża: wtedy robi to automat
        w = Env()
        a = w.account("a@x", expires_in=-30 * 60)
        w.runtime(a)
        w.write()

        w.run("tick")

        self.assertEqual(len(w.calls("/v1/oauth/token")), 1)
        fresh = w.managed("a@x")["claudeAiOauth"]
        self.assertNotEqual(fresh["refreshToken"], a["claudeAiOauth"]["refreshToken"])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], fresh)

    def test_tick_stands_down_while_orca_has_its_own_account_selected(self):
        # Orca w trybie kont zarządzanych cofa przełączenia i sama odświeża tokeny:
        # automat, który by się z nią przepychał, wylogowuje konta
        w = Env()
        a = w.account("a@x", session_used=99, expires_in=3 * 60)
        w.account("b@x")
        w.runtime(a)
        w.write()
        w.orca_selects("a@x")

        w.run("tick")

        self.assertEqual(w.entry(BASE)["claudeAiOauth"], a["claudeAiOauth"])
        self.assertEqual(w.calls("/v1/oauth/token"), [])

    def test_tick_never_switches_to_account_with_canceled_subscription(self):
        w = Env()
        a = w.account("a@x", session_used=99)
        w.account("b@x", weekly_used=10)
        w.account("c@x", weekly_used=50)
        w.runtime(a)
        w.write()
        w.state(identity={w.ids["b@x"]: {"ts": int(time.time()), "email": "b@x", "status": "canceled"}})

        w.run("tick")

        live = w.entry(BASE)["claudeAiOauth"]["accessToken"]
        self.assertEqual(live, w.managed("c@x")["claudeAiOauth"]["accessToken"])

    def test_tick_leaves_dead_active_account(self):
        # aktywne konto ma martwe tokeny w runtime i w kopii Orca: automat przechodzi na zdrowe
        w = Env()
        dead = w.account("a@x", alive=False)
        b = w.account("b@x")
        w.runtime(dead)
        w.write()

        w.run("tick")
        w.run("tick")

        live = w.entry(scoped(w.config_dir))
        self.assertEqual(live["claudeAiOauth"]["accessToken"], w.managed("b@x")["claudeAiOauth"]["accessToken"])


FAKE_DEPOT = os.path.join(HERE, "fakes-depot", "depot")
DEPOT_FALLBACK = "Claude Acc Depot fallback token"


def configure(w, **fields):
    """Zmiana config.json tak, jak robi ją użytkownik albo `claude-acc pause on|off`."""
    path = os.path.join(w.state_dir, "config.json")
    cfg = json.load(open(path))
    cfg.update(fields)
    json.dump(cfg, open(path, "w"))
    return w


def with_depot(w):
    """Włącza synchronizację Depot z atrapą CLI zamiast prawdziwego `depot`."""
    path = os.path.join(w.state_dir, "config.json")
    cfg = json.load(open(path))
    cfg.update({"depot_sync": True, "depot_bin": FAKE_DEPOT})
    json.dump(cfg, open(path, "w"))
    return w


def depot_store(w):
    path = os.path.join(w.fake, "depot.json")
    return json.load(open(path)) if os.path.exists(path) else {"secrets": {}, "calls": []}


class DepotTest(unittest.TestCase):
    def test_sandboxes_get_account_with_most_headroom_other_than_local(self):
        w = with_depot(Env())
        a = w.account("a@x", weekly_used=5)
        w.account("b@x", weekly_used=60)
        c = w.account("c@x", weekly_used=20)
        w.runtime(a)
        w.write()

        w.run("tick")

        self.assertEqual(depot_store(w)["secrets"].get("CLAUDE_CODE_OAUTH_TOKEN"),
                         w.managed("c@x")["claudeAiOauth"]["accessToken"])
        self.assertEqual(w.saved_state()["depot_email"], "c@x")
        self.assertEqual(w.managed("c@x")["claudeAiOauth"], c["claudeAiOauth"])  # ważny token: bez odświeżania

    def test_token_is_not_resent_while_account_still_carries(self):
        w = with_depot(Env())
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.write()

        w.run("tick")
        w.run("tick")

        adds = [c for c in depot_store(w)["calls"] if c[:3] == ["claude", "secrets", "add"]]
        self.assertEqual(len(adds), 1)

    def test_sandboxes_leave_account_that_became_local(self):
        w = with_depot(Env())
        a = w.account("a@x")
        w.account("b@x", weekly_used=5)
        w.account("c@x", weekly_used=30)
        w.runtime(a)
        w.write()
        w.run("tick")
        self.assertEqual(w.saved_state()["depot_email"], "b@x")

        w.run("switch", "b@x")

        # b jest teraz lokalne, a zwolnione ma najwięcej zapasu (90% tygodnia) przed c (70%)
        self.assertEqual(w.saved_state()["depot_email"], "a@x")
        self.assertEqual(depot_store(w)["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"],
                         w.managed("a@x")["claudeAiOauth"]["accessToken"])

    def test_short_lived_token_of_idle_account_is_refreshed_before_sending(self):
        w = with_depot(Env())
        a = w.account("a@x", expires_in=3 * 60)
        b = w.account("b@x", expires_in=3600)
        w.runtime(a)
        w.write()

        w.run("tick")

        fresh = w.managed("b@x")["claudeAiOauth"]
        self.assertNotEqual(fresh["accessToken"], b["claudeAiOauth"]["accessToken"])
        self.assertEqual(depot_store(w)["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], fresh["accessToken"])
        # token aktywnego konta rotują sesje Claude Code: automat go nie dotyka
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], a["claudeAiOauth"])

    def test_depot_failure_does_not_stop_switching(self):
        w = with_depot(Env())
        a = w.account("a@x", session_used=99)
        w.account("b@x")
        w.runtime(a)
        w.write()

        w.run("tick", FAKE_DEPOT_FAIL="1")

        self.assertEqual(w.saved_state()["active_email"], "b@x")
        self.assertNotIn("depot_email", w.saved_state())

    def test_fallback_token_when_no_other_account_has_headroom(self):
        w = with_depot(Env())
        a = w.account("a@x")
        w.account("b@x", weekly_used=99)
        w.runtime(a)
        w.keychain[f"{DEPOT_FALLBACK}|{USER}"] = "sk-ant-oat01-fallback"
        w.write()

        w.run("tick")

        self.assertEqual(depot_store(w)["secrets"]["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-fallback")
        self.assertEqual(w.saved_state()["depot_email"], "fallback")

    def test_sync_is_off_without_the_setting(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.write()

        w.run("tick", PATH=f"{os.path.dirname(FAKE_DEPOT)}:{FAKES}:/usr/bin:/bin")

        self.assertEqual(depot_store(w)["calls"], [])


TOKEN_FALLBACK = "Claude Acc token fallback"


class TokenTest(unittest.TestCase):
    def test_token_comes_from_account_with_most_headroom_other_than_local(self):
        w = Env()
        a = w.account("a@x", weekly_used=5)
        w.account("b@x", weekly_used=60)
        w.account("c@x", weekly_used=20)
        w.runtime(a)
        w.write()

        out = w.run("token", "--json")

        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["email"], "c@x")
        self.assertEqual(got["source"], "rotation")
        self.assertEqual(got["token"], w.managed("c@x")["claudeAiOauth"]["accessToken"])

    def test_token_avoids_the_depot_account_while_another_carries(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=5)
        w.account("c@x", weekly_used=30)
        w.runtime(a)
        w.state(depot_email="b@x")
        w.write()

        got = json.loads(w.run("token", "--json").stdout)

        self.assertEqual(got["email"], "c@x")

    def test_short_lived_idle_token_is_refreshed_and_active_is_never_touched(self):
        w = Env()
        a = w.account("a@x", expires_in=3 * 60)
        b = w.account("b@x", expires_in=10 * 60)
        w.runtime(a)
        w.write()

        got = json.loads(w.run("token", "--json", "--min-minutes", "30").stdout)

        fresh = w.managed("b@x")["claudeAiOauth"]
        self.assertNotEqual(fresh["accessToken"], b["claudeAiOauth"]["accessToken"])
        self.assertEqual(got["token"], fresh["accessToken"])
        self.assertEqual(w.managed("a@x")["claudeAiOauth"], a["claudeAiOauth"])

    def test_prefer_keeps_the_previous_account_while_it_carries(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=5)
        w.account("c@x", weekly_used=40)
        w.runtime(a)
        w.write()

        self.assertEqual(json.loads(w.run("token", "--json", "--prefer", "c@x").stdout)["email"], "c@x")
        # konto, które odbiło proces, odpada od razu, mimo limitów z pamięci podręcznej
        got = json.loads(w.run("token", "--json", "--prefer", "c@x", "--avoid", "c@x").stdout)
        self.assertEqual(got["email"], "b@x")

    def test_active_returns_the_live_token_of_the_local_account_without_refresh(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=5)
        w.runtime(a)
        w.write()
        live = w.session_refresh("a@x")

        got = json.loads(w.run("token", "--active", "--json").stdout)

        self.assertEqual((got["email"], got["source"]), ("a@x", "active"))
        self.assertEqual(got["token"], live["claudeAiOauth"]["accessToken"])
        self.assertEqual(w.calls("/v1/oauth/token"), [])

    def test_active_refuses_a_short_lived_token_instead_of_refreshing_it(self):
        w = Env()
        a = w.account("a@x", expires_in=5 * 60)
        w.runtime(a)
        w.write()

        out = w.run("token", "--active", "--json", "--min-minutes", "30")

        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")
        self.assertEqual(w.calls("/v1/oauth/token"), [])

    def test_unknown_flag_or_help_never_prints_a_token(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x")
        w.runtime(a)
        w.write()

        for args in (["--help"], ["-h"], ["--jsn"], ["--min-minutes"], ["--min-minutes", "x"]):
            out = w.run("token", *args)
            self.assertNotIn("at-", out.stdout + out.stderr, args)
            self.assertEqual(out.returncode, 0 if args[0] in ("--help", "-h") else 2, args)

    def test_fallback_token_without_headroom_and_error_without_either(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=99)
        w.runtime(a)
        w.write()

        out = w.run("token", "--json")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")

        w.keychain[f"{TOKEN_FALLBACK}|{USER}"] = "sk-ant-oat01-fallback"
        w.write()
        got = json.loads(w.run("token", "--json").stdout)
        self.assertEqual((got["token"], got["source"]), ("sk-ant-oat01-fallback", "fallback"))


@unittest.skipUnless(os.path.exists(ACC), "brak acc.py")
class LauncherTest(unittest.TestCase):
    """Po setup.sh aplikacja i launchd startują `<python> acc.py accswitch ...`."""

    def test_status_json_and_tick_through_launcher(self):
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=50)
        w.runtime(a)
        w.write()
        direct = json.loads(w.run("status", "--json").stdout)

        def launch(*args):
            return subprocess.run([sys.executable, ACC, "accswitch", *args], env=w.env(),
                                  capture_output=True, text=True, timeout=60, check=False)

        via = launch("status", "--json")
        self.assertEqual(via.returncode, 0, via.stderr)
        snap = json.loads(via.stdout)
        for d in (direct, snap):
            d.pop("generated_at")
            for row in d["accounts"]:
                row.pop("data_age")
        self.assertEqual(snap, direct)
        self.assertEqual(launch("tick").returncode, 0)
        self.assertIn("last_tick", w.saved_state())


class KeychainReadsTest(unittest.TestCase):
    """Jeden przebieg czyta wpis konta raz do rozpoznania; zapisy i odświeżenia czytają na świeżo."""

    def test_status_json_reads_each_idle_account_entry_once(self):
        w = Env()
        a = w.account("a@x")
        for email in ("b@x", "c@x", "d@x"):
            w.account(email)
        w.runtime(a)
        w.write()
        w.run("status", "--json")  # pierwszy przebieg napełnia pamięć limitów
        w.forget_keychain_calls()

        r = w.run("status", "--json")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        reads = [key for cmd, key in w.keychain_calls() if cmd == "find-generic-password"]
        for email in ("b@x", "c@x", "d@x"):
            self.assertEqual(reads.count(f"{MANAGED}|{w.ids[email]}"), 1, (email, reads))
        # aktywne: rozpoznanie i świeży odczyt przed read-back, jak zawsze
        self.assertLessEqual(reads.count(f"{MANAGED}|{w.ids['a@x']}"), 3, reads)


class KeychainMemoTest(unittest.TestCase):
    """kc_peek, kc_read_many i kc_write w procesie testu, na atrapie `security`."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import accswitch
        self.acc = accswitch
        self.w = Env()
        self.w.keychain["svc|u"] = "A"
        self.w.write()
        patcher = mock.patch.dict(os.environ, self.w.env())
        patcher.start()
        self.addCleanup(patcher.stop)
        for cache in (accswitch._KC_SEEN, accswitch._TOOLS):
            cache.clear()
            self.addCleanup(cache.clear)

    def behind_our_back(self, value):
        """Inny proces (sesja Claude Code, Orca) zapisuje wpis."""
        path = os.path.join(self.w.fake, "keychain.json")
        with open(path) as f:
            keychain = json.load(f)
        keychain["svc|u"] = value
        with open(path, "w") as f:
            json.dump(keychain, f)

    def test_peek_reuses_what_the_run_read_and_read_is_always_fresh(self):
        acc = self.acc
        self.assertEqual(acc.kc_peek("svc", "u"), "A")
        self.behind_our_back("B")
        self.assertEqual(acc.kc_peek("svc", "u"), "A")
        self.assertEqual(acc.kc_read("svc", "u"), "B")
        self.assertEqual(acc.kc_peek("svc", "u"), "B")
        self.assertEqual(acc.tool("security"), os.path.join(FAKES, "security"))

    def test_write_is_remembered_and_a_failed_write_forgets_the_entry(self):
        acc = self.acc
        acc.kc_write("svc", "u", "C")
        self.behind_our_back("D")
        self.assertEqual(acc.kc_peek("svc", "u"), "C")

        os.environ["FAKE_FAIL_WRITE_SERVICE"] = "svc"
        with self.assertRaises(RuntimeError):
            acc.kc_write("svc", "u", "E")
        self.assertEqual(acc.kc_peek("svc", "u"), "D")  # po nieudanym zapisie czytamy od nowa

    def test_watch_starts_every_tick_without_the_previous_ticks_reads(self):
        acc = self.acc
        seen_at_start = []

        def tick(cfg, args):
            seen_at_start.append(dict(acc._KC_SEEN))
            acc._KC_SEEN[("svc", "u")] = "z poprzedniego ticku"

        with mock.patch.object(acc, "cmd_tick", side_effect=tick), mock.patch.object(
                acc.time, "sleep", side_effect=[None, KeyboardInterrupt]), mock.patch("builtins.print"), \
                self.assertRaises(KeyboardInterrupt):
            acc.cmd_watch({}, ["1"])
        self.assertEqual(seen_at_start, [{}, {}])

    def test_read_many_gives_what_reads_one_by_one_give(self):
        acc = self.acc
        self.w.keychain.update({f"s{i}|u": f"blob-{i}" for i in range(11)})
        self.w.write()
        keys = [(f"s{i}", "u") for i in range(12)]  # s11 nie istnieje

        acc.kc_read_many(keys, batch=5)
        many = {key: acc._KC_SEEN[key] for key in keys}
        acc._KC_SEEN.clear()

        self.assertEqual(many, {key: acc.kc_read(*key) for key in keys})
        self.assertIsNone(many[("s11", "u")])


class HistoryTrimTest(unittest.TestCase):
    """history.jsonl: wyniki jak przy pełnym odczycie, przepisanie pliku raz na godzinę, nie co odczyt."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import accswitch
        self.acc = accswitch
        self.path = os.path.join(tempfile.mkdtemp(prefix="claude-acc-history-"), "history.jsonl")
        patcher = mock.patch.object(accswitch, "HISTORY_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def text(self):
        with open(self.path) as f:
            return f.read()

    def write(self, ages_minutes, email="a@x"):
        now = int(time.time())
        rows = [{"ts": now - age * 60, "email": email, "session_used": 10.0, "weekly_used": float(age)}
                for age in ages_minutes]
        with open(self.path, "w") as f:
            f.writelines(json.dumps(row) + "\n" for row in rows)

    def test_recent_rows_in_file_order_without_rewriting_inside_the_slack(self):
        keep_hours = 48
        # najstarsza próbka 30 min poza oknem 48 h: jeszcze w zapasie, plik zostaje
        ages = list(range(48 * 60 + 30, 0, -2))
        self.write(ages)
        with open(self.path, "a") as f:
            f.write(json.dumps({"ts": int(time.time()) - 60, "email": "b@x",
                                "session_used": 1.0, "weekly_used": 1.0}) + "\n")
        before = self.text()

        rows = self.acc.read_history("a@x", 60, keep_hours)

        self.assertEqual(self.text(), before)
        # próbka sprzed równo 60 min leży na granicy okna: zostawiamy ją poza porównaniem
        self.assertEqual([r["weekly_used"] for r in rows if r["weekly_used"] < 60], [float(a) for a in ages if a < 60])
        self.assertTrue(all(r["email"] == "a@x" for r in rows))

    def test_trims_once_the_oldest_row_leaves_the_slack(self):
        ages = [48 * 60 + 90, 48 * 60 + 70, 48 * 60 - 10, 30, 10]
        self.write(ages)
        with open(self.path) as f:
            lines = f.readlines()
        lines.insert(3, "{uszkodzona linia\n")
        with open(self.path, "w") as f:
            f.writelines(lines)

        rows = self.acc.read_history("a@x", 60, 48)

        self.assertEqual([r["weekly_used"] for r in rows], [30.0, 10.0])
        kept = [json.loads(line)["weekly_used"] for line in self.text().splitlines()]
        self.assertEqual(kept, [float(48 * 60 - 10), 30.0, 10.0])


class OptionalPauseTest(unittest.TestCase):
    """Pauza limitów jest opcjonalna i domyślnie wyłączona: sesje pracują do ściany limitu,
    Claude Code wznawia je po resecie, a budzik watch-wall po przełączeniu konta."""

    def exhausted_world(self):
        w = Env()
        a = w.account("a@x", session_used=97, weekly_used=40)
        w.account("b@x", weekly_used=99)
        w.runtime(a)
        w.write()
        return w

    def warnings(self, w):
        return [n for n in w.notifications() if '"Claude: brak konta z zapasem"' in n]

    def tick(self, w):
        w.forget_usage_cache()
        return w.run("tick")

    def snapshot(self, w):
        return json.loads(w.run("status", "--json").stdout)

    def ours(self, w):
        settings = json.load(open(os.path.join(w.config_dir, "settings.json")))
        return sorted(e for e, groups in settings.get("hooks", {}).items()
                      if any("claude-acc/hook.py" in h.get("command", "") for g in groups for h in g["hooks"]))

    def test_without_the_option_sessions_keep_working_and_hear_once(self):
        w = self.exhausted_world()

        for _ in range(3):
            self.tick(w)

        self.assertIsNone(w.pause())
        self.assertEqual(len(self.warnings(w)), 1)  # raz na epizod, nie co 2 minuty
        self.assertFalse([n for n in w.notifications() if '"Claude: pauza limit' in n])
        self.assertEqual(w.entry(BASE)["claudeAiOauth"]["accessToken"],
                         w.managed("a@x")["claudeAiOauth"]["accessToken"])

    def test_the_warning_comes_back_in_the_next_episode(self):
        w = self.exhausted_world()
        self.tick(w)

        w.set_usage("a@x", session_used=0)
        self.tick(w)
        w.set_usage("a@x", session_used=97)
        self.tick(w)

        self.assertEqual(len(self.warnings(w)), 2)

    def test_tick_wakes_sessions_from_a_pause_once_the_option_is_off(self):
        # pauza z czasu, gdy opcja była włączona (albo sprzed aktualizacji): plik musi zniknąć,
        # bo z nim sesje czekają na budzik, którego automat już nie odpali
        w = configure(self.exhausted_world(), limit_pause=True)
        self.tick(w)
        self.assertIsNotNone(w.pause())

        configure(w, limit_pause=False)
        self.tick(w)

        self.assertIsNone(w.pause())
        self.assertEqual(len(self.warnings(w)), 1)

    def test_pause_command_switches_the_option_hooks_and_a_running_pause(self):
        w = self.exhausted_world()
        self.assertIn("wyłączona", w.run("pause").stdout)

        self.assertIs(self.snapshot(w)["limit_pause"], False)  # przełącznik w panelu czyta to pole

        r = w.run("pause", "on")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.ours(w), ["PostToolUse", "PreToolUse", "Stop", "StopFailure", "UserPromptSubmit"])
        self.assertIs(self.snapshot(w)["limit_pause"], True)
        self.tick(w)
        self.assertIsNotNone(w.pause())

        r = w.run("pause", "off")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(w.pause())
        self.assertEqual(self.ours(w), ["StopFailure"])  # budzik po ścianie limitu zostaje
        self.assertFalse(json.load(open(os.path.join(w.state_dir, "config.json")))["limit_pause"])
        self.assertIs(self.snapshot(w)["limit_pause"], False)
        self.assertEqual(w.run("pause", "maybe").returncode, 2)


class DrainTest(unittest.TestCase):
    """Tryb dobijania: gdy żadne konto nie ma zapasu, aktywne pracuje do ostatniego procenta,
    a potem automat przełącza po kolei na resztki pozostałych kont."""

    def world(self, active_used, drain=True, **accounts):
        """Aktywne a@x z zużyciem sesji active_used; pozostałe konta: email -> (sesja, tydzień)."""
        w = configure(Env(), drain=drain)
        a = w.account("a@x", session_used=active_used, weekly_used=40)
        for email, (session_used, weekly_used) in accounts.items():
            w.account(email.replace("_", "@"), session_used=session_used, weekly_used=weekly_used)
        w.runtime(a)
        w.write()
        return w

    def tick(self, w):
        w.forget_usage_cache()
        return w.run("tick")

    def active(self, w):
        token = w.entry(BASE)["claudeAiOauth"]["accessToken"]
        return next(e for e in w.ids if w.managed(e)["claudeAiOauth"]["accessToken"] == token)

    def said(self, w, title):
        return [n for n in w.notifications() if f'"{title}"' in n]

    def test_active_account_works_to_its_last_percent(self):
        # 3% sesji to poniżej progu porzucenia (5%), ale bez konta z zapasem szkoda go zostawiać
        w = self.world(97, b_x=(10, 98))

        self.tick(w)

        self.assertEqual(self.active(w), "a@x")
        self.assertIsNone(w.pause())
        self.assertEqual(len(self.said(w, "Claude: dobijam resztki kont")), 1)

    def test_empty_account_hands_over_to_the_biggest_scrap(self):
        # b@x ma 2% tygodnia, c@x 3% sesji, d@x nic: pierwsze idzie c@x (słabsze okno 3% > 2%)
        w = self.world(100, b_x=(10, 98), c_x=(97, 40), d_x=(100, 100))

        self.tick(w)

        self.assertEqual(self.active(w), "c@x")
        self.assertIsNone(w.pause())

    def test_scraps_are_used_one_after_another_with_one_notification(self):
        w = self.world(100, b_x=(10, 98), c_x=(97, 40))
        self.tick(w)
        self.assertEqual(self.active(w), "c@x")

        w.set_usage("c@x", session_used=100)
        self.tick(w)
        self.assertEqual(self.active(w), "b@x")

        w.set_usage("b@x", weekly_used=100)
        self.tick(w)
        self.assertEqual(self.active(w), "b@x")  # nic już nie zostało: zostaje, ostrzeżenie zamiast skoku
        self.assertEqual(len(self.said(w, "Claude: dobijam resztki kont")), 1)
        self.assertEqual(len(self.said(w, "Claude: brak konta z zapasem")), 1)
        self.assertFalse(self.said(w, "Claude: zmiana konta"))

    def test_an_account_with_real_headroom_takes_over_from_scraps(self):
        w = self.world(100, b_x=(10, 98), c_x=(100, 99))
        self.tick(w)
        self.assertEqual(self.active(w), "b@x")

        w.set_usage("c@x", session_used=0, weekly_used=10)
        self.tick(w)

        self.assertEqual(self.active(w), "c@x")
        self.assertEqual(len(self.said(w, "Claude: zmiana konta")), 1)
        self.assertFalse(w.saved_state().get("draining"))  # zwykłe przełączenie kończy epizod

    def test_company_account_scraps_come_last(self):
        # c@x ma 5% sesji, b@x 2% tygodnia: bez last_resort wygrałoby c@x
        w = configure(self.world(100, b_x=(10, 98), c_x=(95, 40)), last_resort=["c@x"])

        self.tick(w)

        self.assertEqual(self.active(w), "b@x")

    def test_without_the_mode_scraps_stay_untouched(self):
        w = self.world(100, drain=False, b_x=(10, 98))

        self.tick(w)

        self.assertEqual(self.active(w), "a@x")
        self.assertEqual(len(self.said(w, "Claude: brak konta z zapasem")), 1)

    def test_draining_wakes_a_pause_and_holds_it_off_while_scraps_last(self):
        w = configure(self.world(97, drain=False, b_x=(10, 98)), limit_pause=True)
        self.tick(w)
        self.assertIsNotNone(w.pause())

        self.assertEqual(w.run("drain", "on").returncode, 0)
        self.tick(w)

        self.assertIsNone(w.pause())
        self.assertEqual(self.active(w), "a@x")
        w.set_usage("a@x", session_used=100)
        self.tick(w)
        self.assertEqual(self.active(w), "b@x")
        self.assertIsNone(w.pause())

    def test_drain_command_and_status_field(self):
        w = self.world(10, drain=False)
        self.assertIn("wyłączone", w.run("drain").stdout)
        self.assertIs(json.loads(w.run("status", "--json").stdout)["drain"], False)

        self.assertEqual(w.run("drain", "on").returncode, 0)

        self.assertIs(json.loads(w.run("status", "--json").stdout)["drain"], True)
        self.assertTrue(json.load(open(os.path.join(w.state_dir, "config.json")))["drain"])
        self.assertEqual(w.run("drain", "maybe").returncode, 2)


class PauseTest(unittest.TestCase):
    """Pauza limitów: gdy aktywne konto się kończy, a żadne inne nie ma zapasu,
    sesje dostają czas na punkt kontrolny zamiast paść w połowie pracy agentów."""

    def exhausted_world(self):
        # a@x ma 3% sesji (próg 5%), b@x ma 1% tygodnia: nie ma dokąd przełączyć
        w = configure(Env(), limit_pause=True)
        a = w.account("a@x", session_used=97, weekly_used=40)
        w.account("b@x", weekly_used=99)
        w.runtime(a)
        w.write()
        return w, a

    def test_pause_starts_when_no_account_has_headroom(self):
        w, a = self.exhausted_world()

        w.run("tick")

        pause = w.pause()
        self.assertIsNotNone(pause)
        self.assertEqual(pause["account"], "a@x")
        # najwcześniej zapas wraca z resetem okna 5h aktywnego konta (za 3 h w atrapie)
        self.assertAlmostEqual(pause["resume_at"], time.time() + 3 * 3600, delta=120)
        self.assertEqual(w.entry(BASE)["claudeAiOauth"], a["claudeAiOauth"])
        snap = json.loads(w.run("status", "--json").stdout)
        self.assertEqual(snap["pause"]["account"], "a@x")

    def test_pause_is_announced_once_per_episode(self):
        # powiadomienie co 2 minuty przez całą pauzę to spam, a nie ostrzeżenie
        w, _ = self.exhausted_world()

        for _ in range(3):
            w.forget_usage_cache()
            w.run("tick")

        # osascript dostaje tekst z json.dumps, więc polskie litery są tam jako \uXXXX
        self.assertEqual(len([n for n in w.notifications() if '"Claude: pauza limit' in n]), 1)
        self.assertIsNotNone(w.pause())

    def test_pause_ends_when_active_window_resets(self):
        w, _ = self.exhausted_world()
        w.run("tick")

        w.set_usage("a@x", session_used=0)
        w.forget_usage_cache()
        w.run("tick")

        self.assertIsNone(w.pause())

    def test_pause_ends_by_switching_once_another_account_recovers(self):
        w, _ = self.exhausted_world()
        w.run("tick")

        w.set_usage("b@x", weekly_used=10)
        w.forget_usage_cache()
        w.run("tick")

        self.assertIsNone(w.pause())
        self.assertEqual(w.entry(BASE)["claudeAiOauth"]["accessToken"],
                         w.managed("b@x")["claudeAiOauth"]["accessToken"])

    def test_pause_waits_for_real_headroom_not_the_switch_threshold(self):
        # 6% sesji to już ponad próg porzucenia (5%), ale za mało na sensowną pracę:
        # zdjęcie pauzy przy 6% budziłoby sesje na minutę
        w, _ = self.exhausted_world()
        w.run("tick")

        w.set_usage("a@x", session_used=94)
        w.forget_usage_cache()
        w.run("tick")

        self.assertIsNotNone(w.pause())

    def test_rate_limited_read_keeps_pause_as_it_was(self):
        w, _ = self.exhausted_world()
        w.run("tick")

        w.set_usage("a@x", session_used=0, rate_limited=True)
        w.forget_usage_cache()
        w.run("tick")

        self.assertIsNotNone(w.pause())

    def test_resume_by_hand_lasts_until_limits_recover(self):
        w, _ = self.exhausted_world()
        w.run("tick")

        r = w.run("resume")
        w.run("tick")

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(w.pause())  # ręczne wznowienie nie wraca przy następnym przebiegu
        w.set_usage("a@x", session_used=0)
        w.forget_usage_cache()
        w.run("tick")
        w.set_usage("a@x", session_used=97)
        w.forget_usage_cache()
        w.run("tick")
        self.assertIsNotNone(w.pause())  # nowy epizod po powrocie i ponownym wyczerpaniu

    def test_no_pause_while_orca_has_its_own_account_selected(self):
        w, _ = self.exhausted_world()
        w.run("tick")

        w.orca_selects("a@x")
        w.run("tick")

        self.assertIsNone(w.pause())

    def test_no_pause_while_runtime_holds_an_account_outside_orca(self):
        # automat nie pilnuje obcego konta, więc nikt by tej pauzy nie zdjął
        w, _ = self.exhausted_world()
        w.run("tick")

        path = os.path.join(w.fake, "keychain.json")
        keychain = json.load(open(path))
        stranger = {"claudeAiOauth": {"accessToken": "at-stranger", "refreshToken": "rt-stranger",
                                      "expiresAt": int((time.time() + 3600) * 1000)}}
        for service in (scoped(w.config_dir), BASE):
            keychain[f"{service}|{USER}"] = json.dumps(stranger)
        json.dump(keychain, open(path, "w"))
        w.run("tick")

        self.assertIsNone(w.pause())
        self.assertTrue(w.saved_state().get("hands_off_notified"))  # to była gałąź obcego konta


if __name__ == "__main__":
    unittest.main()
