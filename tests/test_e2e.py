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
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "accswitch.py")
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

    # --- odczyt ---

    def entry(self, service, account=USER):
        raw = json.load(open(os.path.join(self.fake, "keychain.json"))).get(f"{service}|{account}")
        return json.loads(raw) if raw else None

    def managed(self, email):
        return self.entry(MANAGED, self.ids[email])

    def calls(self, suffix):
        log = json.load(open(os.path.join(self.fake, "server.json")))["log"]
        return [u for u in log if u.endswith(suffix)]

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
        # status subskrypcji sprawdzany w profilu raz na 12 h: po odnowieniu konto
        # wraca do kolejki bez ręcznego grzebania w stanie
        w = Env()
        a = w.account("a@x")
        w.account("b@x", weekly_used=20)
        w.runtime(a)
        w.write()
        w.state(identity={w.ids["b@x"]: {"ts": int(time.time()) - 13 * 3600, "email": "b@x",
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


if __name__ == "__main__":
    unittest.main()
