"""Testy kredytów API (credits.py) na atrapach Pęku kluczy, okna z kluczem i API Anthropic.

Każdy test stawia osobny $HOME i uruchamia prawdziwy skrypt jako proces, z atrapami `security`,
`osascript` i `curl` na początku PATH (tests/fakes-credits, potem tests/fakes). Sprawdza to, co
widzi wywołujący (Jarvis, Polid): JSON kontraktu, kody wyjścia 0/75/76, wyjście i środowisko
dziecka, oraz że klucz nie wycieka do argumentów procesów, plików i wyjścia. Prawdziwy Pęk
kluczy, sieć i konta nie są dotykane; klucze to atrapy `sk-ant-api03-fake-...`.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_credits
"""

import importlib.util
import io
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "credits.py")
ACCSWITCH = os.path.join(ROOT, "accswitch.py")
FAKES = os.path.join(HERE, "fakes")
FAKES_CREDITS = os.path.join(HERE, "fakes-credits")
# interpreter skryptów pod testem; CLAUDE_ACC_TEST_PYTHON sprawdza ten, na którym biegnie instalacja
# (~/.local/share/claude-acc/python), a komendy helpera i strażnika biorą sys.executable
PY = os.environ.get("CLAUDE_ACC_TEST_PYTHON") or "/usr/bin/python3"

KEY_A = "sk-ant-api03-fake-own-a"
KEY_D = "sk-ant-api03-fake-own-d"
KEY_B = "sk-ant-api03-fake-client-b"
KEY_U = "sk-ant-usr-fake-own-u"  # klucz powiązany z użytkownikiem, tak jak wydaje go teraz Console
SOURCE = "polid/anthropic-key-1@outofplace.space"  # usługa/konto wpisu, z którego importujemy
CONTRACT = {"email", "org_id", "scope", "granted_usd", "spent_usd", "remaining_usd", "cycle_resets_at", "checked_at", "state"}


def read(path):
    with open(path, errors="replace") as f:
        return f.read()


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def day(offset_days):
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class World:
    """Świat jednego testu: $HOME, Pęk kluczy i API w plikach atrap."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="credits-test-")
        self.fake = os.path.join(self.home, "fake")
        self.state = os.path.join(self.home, ".local/share/claude-acc")
        self.credits = os.path.join(self.state, "credits")
        os.makedirs(self.fake)
        os.makedirs(self.state)
        self.api({"keys": {KEY_A: "org-a", KEY_D: "org-d", KEY_B: "org-b", KEY_U: "org-u"}})

    def api(self, data):
        write_json(os.path.join(self.fake, "credits-api.json"), data)

    def env(self, **extra):
        env = {"HOME": self.home, "USER": "tester", "PATH": f"{FAKES_CREDITS}:{FAKES}:/usr/bin:/bin"}
        env.update(extra)
        return env

    def run(self, *args, input=None, script=SCRIPT, **extra):
        return subprocess.run([PY, script, *args], env=self.env(**extra), input=input,
                              capture_output=True, text=True, timeout=60)

    def add(self, email, key, *flags, **extra):
        r = self.run("add", email, *flags, FAKE_DIALOG_ANSWER=key, **extra)
        return r

    def keychain(self):
        path = os.path.join(self.fake, "keychain.json")
        return json.loads(read(path)) if os.path.exists(path) else {}

    def stash(self, service, account, secret):
        """Wpis w Pęku kluczy, który założył ktoś inny niż claude-acc, np. Polid."""
        keychain = self.keychain()
        keychain[f"{service}|{account}"] = secret
        write_json(os.path.join(self.fake, "keychain.json"), keychain)

    def recorded(self, name):
        """Dziennik wywołań atrapy (argumenty `security` i `curl`, okno z kluczem); pusty, gdy jej nie wołano."""
        path = os.path.join(self.fake, name)
        return read(path) if os.path.exists(path) else ""

    def registry(self):
        path = os.path.join(self.credits, "accounts.json")
        return json.loads(read(path))["accounts"] if os.path.exists(path) else {}

    def status(self):
        r = self.run("status", "--json")
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def account(self, email):
        return next(a for a in self.status()["accounts"] if a["email"] == email)

    def identity(self, email, since, tier="default_claude_max_20x"):
        """Konto w state.json tak, jak zapisuje je `claude-acc status` po odczycie profilu."""
        path = os.path.join(self.state, "state.json")
        state = json.loads(read(path)) if os.path.exists(path) else {}
        state.setdefault("identity", {})[f"id-{email}"] = {
            "ts": int(time.time()), "email": email, "tier": tier, "subscription_since": since, "status": "active"}
        write_json(path, state)

    def spend_at(self, org, usd, when):
        """Wydatek z przeszłości w formacie dziennika (record zapisuje zawsze z bieżącym czasem)."""
        os.makedirs(self.credits, exist_ok=True)
        path = os.path.join(self.credits, f"ledger-{datetime.fromtimestamp(when):%Y-%m}.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps({"at": when, "org_id": org, "email": "x@example.com", "usd": usd, "purpose": "past"}) + "\n")

    def pool(self):
        """Dwa konta own (a: 20 dni do odnowienia, d: 3 dni) i jedno klienta, zakres client (b: 1 dzień)."""
        for email, key, scope, resets in (("a@example.com", KEY_A, "own", 20), ("d@example.com", KEY_D, "own", 3),
                                          ("b@example.com", KEY_B, "client", 1)):
            r = self.add(email, key, "--scope", scope, "--resets-at", day(resets))
            assert r.returncode == 0, r.stderr


# dziecko `exec`: co dostało w środowisku i na stdin, bez wypisywania samego klucza
CHILD = """
import os, sys
data = sys.stdin.read()
sys.stdout.write(data.upper())
key = os.environ.get("ANTHROPIC_API_KEY")
print("org=" + os.environ.get("CLAUDE_ACC_CREDITS_ORG", "-"))
print("key=" + ("expected" if key == sys.argv[1] else "missing" if key is None else "other"))
print("auth_token=" + ("set" if "ANTHROPIC_AUTH_TOKEN" in os.environ else "unset"))
print("base_url=" + ("set" if "ANTHROPIC_BASE_URL" in os.environ else "unset"))
print("to stderr", file=sys.stderr)
sys.exit(int(sys.argv[2]))
"""


class AddingKeys(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_key_goes_only_to_keychain_never_to_argv_files_or_output(self):
        outputs = []
        r = self.w.add("a@example.com", KEY_A, "--scope", "own")
        outputs.append(r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        # organizacja z nagłówka anthropic-organization-id, bez --org
        self.assertEqual(self.w.registry()["a@example.com"]["org_id"], "org-a")
        self.assertEqual(self.w.keychain(), {"claude-acc-credits|a@example.com": KEY_A})
        for args in (("status",), ("status", "--json"), ("key", "--purpose", "polid-t", "--json")):
            r = self.w.run(*args)
            outputs.append(r.stdout + r.stderr)
        r = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", CHILD, KEY_A, "0", input="")
        outputs.append(r.stdout + r.stderr)
        self.assertIn("key=expected", r.stdout)
        r = self.w.run("status", script=ACCSWITCH)
        outputs.append(r.stdout + r.stderr)
        for out in outputs:
            self.assertNotIn(KEY_A, out)
        # argumenty każdego wywołania `security` i `curl`: klucz szedł tylko przez stdin
        for log in ("security-argv.log", "curl-argv.log"):
            self.assertNotIn(KEY_A, read(os.path.join(self.w.fake, log)), log)
        # wszystkie pliki w $HOME poza magazynami atrap (Pęk kluczy i klucze znane atrapie API)
        fixtures = {os.path.join(self.w.fake, n) for n in ("keychain.json", "credits-api.json")}
        for folder, _, files in os.walk(self.w.home):
            for name in files:
                path = os.path.join(folder, name)
                if path not in fixtures:
                    self.assertNotIn(KEY_A, read(path), path)
        for name in os.listdir(self.w.credits):
            mode = os.stat(os.path.join(self.w.credits, name)).st_mode & 0o777
            self.assertEqual(mode, 0o600, name)

    def test_key_of_another_organization_is_not_stored(self):
        r = self.w.add("a@example.com", KEY_A, "--scope", "own", "--org", "org-x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("org-a", r.stderr)
        self.assertEqual(self.w.keychain(), {})
        self.assertEqual(self.w.registry(), {})

    def test_wrong_pastes_and_cancel_store_nothing(self):
        self.w.api({"keys": {KEY_A: "org-a"}, "unscoped": [KEY_D]})
        cases = (
            (KEY_D, {}, "workspace"),  # klucz bez workspace: każde wywołanie wymaga nagłówka workspace
            ("sk-ant-api03-fake-revoked", {}, "401"),
            ("sk-ant-admin01-fake", {}, "Admin"),
            ("sk-ant-usr-fake-revoked", {}, "401"),
            ("sk-ant-oat01-fake", {}, "sk-ant-usr"),  # token logowania, nie klucz API
            ("hunter2", {}, "sk-ant-api"),
            (KEY_A, {"FAKE_DIALOG_CANCEL": "1"}, "anulowane"),
        )
        for key, extra, why in cases:
            r = self.w.add("a@example.com", key, "--scope", "own", **extra)
            self.assertEqual(r.returncode, 1, key)
            self.assertIn(why, r.stderr, key)
            self.assertNotIn(key, r.stdout + r.stderr)
        self.assertEqual(self.w.keychain(), {})
        self.assertEqual(self.w.registry(), {})

    def test_offline_add_needs_org_from_console(self):
        self.w.api({"offline": True})
        r = self.w.add("a@example.com", KEY_A, "--scope", "own")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.w.keychain(), {})
        r = self.w.add("a@example.com", KEY_A, "--scope", "own", "--org", "org-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bez sprawdzenia", r.stdout)
        self.assertEqual(self.w.registry()["a@example.com"]["org_id"], "org-a")

    def test_user_linked_key_is_accepted_and_stays_out_of_argv_files_and_output(self):
        r = self.w.add("u@example.com", KEY_U, "--scope", "own")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.w.registry()["u@example.com"]["org_id"], "org-u")
        self.assertEqual(self.w.keychain(), {"claude-acc-credits|u@example.com": KEY_U})
        r2 = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", CHILD, KEY_U, "0", input="")
        self.assertIn("key=expected", r2.stdout)  # dziecko dostaje ten sam klucz
        for out in (r.stdout + r.stderr, r2.stdout + r2.stderr):
            self.assertNotIn(KEY_U, out)
        for log in ("security-argv.log", "curl-argv.log"):
            self.assertNotIn(KEY_U, self.w.recorded(log), log)

    def test_message_for_a_rejected_key_names_both_accepted_shapes(self):
        for key in ("sk-ant-admin01-fake", "hunter2"):
            r = self.w.add("u@example.com", key, "--scope", "own")
            self.assertEqual(r.returncode, 1, key)
            self.assertIn("sk-ant-api", r.stderr, key)
            self.assertIn("sk-ant-usr", r.stderr, key)

    def test_import_from_another_keychain_item_needs_no_dialog_and_no_key_in_argv(self):
        self.w.stash("polid", "anthropic-key-1@outofplace.space", KEY_U)
        r = self.w.run("add", "u@example.com", "--scope", "own", "--from-keychain", SOURCE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.w.recorded("dialog.log"), "")  # żadnego okna
        self.assertTrue(self.w.recorded("curl-argv.log"))  # sprawdzony w API jak przy oknie
        self.assertEqual(self.w.registry()["u@example.com"]["org_id"], "org-u")
        self.assertEqual(self.w.keychain(), {
            "polid|anthropic-key-1@outofplace.space": KEY_U,  # źródło zostaje
            "claude-acc-credits|u@example.com": KEY_U,
        })
        argv = self.w.recorded("security-argv.log").splitlines()
        self.assertIn("find-generic-password -s polid -a anthropic-key-1@outofplace.space -w", argv)
        self.assertIn("-i", argv)  # zapis idzie stdin-em `security -i`
        self.assertFalse([line for line in argv if line.startswith("add-generic-password")])
        for log in ("security-argv.log", "curl-argv.log"):
            self.assertNotIn(KEY_U, self.w.recorded(log), log)
        self.assertNotIn(KEY_U, r.stdout + r.stderr)
        self.assertIn(SOURCE, r.stdout)  # mówi, że wpis źródłowy zostaje
        fixtures = {os.path.join(self.w.fake, n) for n in ("keychain.json", "credits-api.json")}
        for folder, _, files in os.walk(self.w.home):
            for name in files:
                path = os.path.join(folder, name)
                if path not in fixtures:
                    self.assertNotIn(KEY_U, read(path), path)
        # i dalej płaci tym kluczem
        r = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", CHILD, KEY_U, "0", input="")
        self.assertIn("key=expected", r.stdout)

    def test_import_replaces_the_key_already_stored_for_that_account(self):
        self.assertEqual(self.w.add("u@example.com", KEY_A, "--scope", "own").returncode, 0)  # konto zajęte starym kluczem
        self.w.stash("polid", "anthropic-key-1@outofplace.space", KEY_U)
        r = self.w.run("add", "u@example.com", "--scope", "own", "--from-keychain", SOURCE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.w.keychain()["claude-acc-credits|u@example.com"], KEY_U)
        self.assertEqual(self.w.registry()["u@example.com"]["org_id"], "org-u")

    def test_import_stores_nothing_when_the_source_is_missing_unusable_or_rejected(self):
        self.w.stash("polid", "secret-not-a-key", "hunter2")
        self.w.stash("polid", "revoked", "sk-ant-usr-fake-revoked")
        self.w.stash("polid", "admin", "sk-ant-admin01-fake")
        before = self.w.keychain()
        cases = (
            ("polid/nope", "polid/nope"),  # wpisu nie ma
            ("polid/secret-not-a-key", "sk-ant-api"),  # to nie klucz API
            ("polid/admin", "Admin"),
            ("polid/revoked", "401"),  # API odrzuca
        )
        for source, why in cases:
            r = self.w.run("add", "u@example.com", "--scope", "own", "--from-keychain", source)
            self.assertEqual(r.returncode, 1, source)
            self.assertIn(why, r.stderr, source)
            for secret in ("hunter2", "sk-ant-usr-fake-revoked", "sk-ant-admin01-fake"):
                self.assertNotIn(secret, r.stdout + r.stderr, source)
        self.assertEqual(self.w.keychain(), before)
        self.assertEqual(self.w.registry(), {})
        self.assertEqual(self.w.recorded("dialog.log"), "")  # brak wpisu nie otwiera okna na wklejenie
        self.assertNotIn("hunter2", self.w.recorded("curl-argv.log"))  # nie-klucz nie wychodzi do sieci

    def test_import_flag_wants_service_and_account(self):
        for value in ("polid", "polid/", "/account"):
            r = self.w.run("add", "u@example.com", "--scope", "own", "--from-keychain", value)
            self.assertEqual(r.returncode, 2, value)
            self.assertIn("USŁUGA/KONTO", r.stderr, value)
        self.assertEqual(self.w.run("add", "u@example.com", "--scope", "own", "--from-keychain").returncode, 2)
        self.assertEqual(self.w.recorded("security-argv.log"), "")  # nic nie dotknęło Pęku kluczy

    def test_scope_is_required(self):
        for flags in ((), ("--scope", "Own Stuff")):
            r = self.w.add("a@example.com", KEY_A, *flags)
            self.assertEqual(r.returncode, 2, flags)
        self.assertEqual(self.w.keychain(), {})


class Balance(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_contract_counts_only_this_cycle_and_only_linked_accounts(self):
        today = datetime.now()
        # subskrypcja od 1. dnia miesiąca rok temu: cykl od 1. tego miesiąca do 1. następnego
        self.w.identity("a@example.com", f"{today.year - 1}-{today.month:02d}-01")
        self.assertEqual(self.w.add("a@example.com", KEY_A, "--scope", "own").returncode, 0)
        self.assertEqual(self.w.add("b@example.com", KEY_B, "--scope", "client", "--granted-usd", "100",
                                    "--resets-at", day(5)).returncode, 0)
        self.assertEqual(self.w.run("pending", "c@example.com").returncode, 0)
        cycle_start = datetime(today.year, today.month, 1)
        self.w.spend_at("org-a", 50.0, (cycle_start - timedelta(hours=12)).timestamp())  # poprzedni cykl
        self.assertEqual(self.w.run("record", "--org", "org-a", "--usd", "20.25", "--purpose", "polid-x").returncode, 0)

        data = self.w.status()
        self.assertEqual(set(data), {"total_remaining_usd", "accounts"})
        for row in data["accounts"]:
            self.assertEqual(set(row), CONTRACT)
        a = self.w.account("a@example.com")
        self.assertEqual((a["granted_usd"], a["spent_usd"], a["remaining_usd"]), (200.0, 20.25, 179.75))
        nxt = datetime(today.year + (today.month == 12), today.month % 12 + 1, 1)
        self.assertEqual(datetime.fromisoformat(a["cycle_resets_at"]).replace(tzinfo=None), nxt)
        self.assertEqual(a["state"], "linked")
        self.assertIsNone(a["checked_at"])
        b = self.w.account("b@example.com")
        self.assertEqual(b["cycle_resets_at"][:10], day(5))
        c = self.w.account("c@example.com")
        self.assertEqual((c["state"], c["org_id"], c["remaining_usd"]), ("pending", None, 0.0))
        self.assertEqual(data["total_remaining_usd"], 279.75)

    def test_console_reading_rebases_and_later_spend_counts(self):
        self.assertEqual(self.w.add("a@example.com", KEY_A, "--scope", "own", "--resets-at", day(9)).returncode, 0)
        self.w.spend_at("org-a", 30.0, time.time() - 3600)  # przed odczytem: jest już w liczbie z Console
        r = self.w.run("balance", "a@example.com", "--remaining-usd", "150", "--expires-at", day(12))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.w.run("record", "--org", "org-a", "--usd", "10", "--purpose", "polid-x").returncode, 0)
        a = self.w.account("a@example.com")
        self.assertEqual(a["remaining_usd"], 140.0)
        self.assertEqual(a["cycle_resets_at"][:10], day(12))
        self.assertIsNotNone(a["checked_at"])

    def test_parallel_records_all_land(self):
        self.assertEqual(self.w.add("a@example.com", KEY_A, "--scope", "own", "--resets-at", day(9)).returncode, 0)
        procs = [subprocess.Popen([PY, SCRIPT, "record", "--org", "org-a", "--usd", "0.5", "--purpose", f"polid-{i}"],
                                  env=self.w.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for i in range(30)]
        self.assertEqual([p.wait() for p in procs], [0] * 30)
        self.assertEqual(self.w.account("a@example.com")["spent_usd"], 15.0)
        r = self.w.run("record", "--org", "org-nope", "--usd", "1", "--purpose", "polid-x")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.w.account("a@example.com")["spent_usd"], 15.0)

    def test_menu_bar_snapshot_and_text_status_show_the_total(self):
        self.w.pool()
        self.w.run("record", "--org", "org-d", "--usd", "12.5", "--purpose", "polid-x")
        write_json(os.path.join(self.w.state, "config.json"), {"depot_sync": False})
        r = self.w.run("status", "--json", script=ACCSWITCH)
        self.assertEqual(r.returncode, 0, r.stderr)
        credits = json.loads(r.stdout)["credits"]
        self.assertEqual(credits["total_remaining_usd"], 587.5)  # 200 + 200 - 12,5 own i 200 klienta
        self.assertEqual(credits["by_scope"], {"own": 387.5, "client": 200.0})
        self.assertEqual(credits["next_expiry_usd"], 200.0)  # b, za dzień
        r = self.w.run("status", script=ACCSWITCH)
        self.assertIn("kredyty API: zostało $587.50", r.stdout)


class Exec(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.pool()

    def org_of(self, *flags, **extra):
        r = self.w.run("exec", "--purpose", "polid-t", *flags, "--", PY, "-c", "import os; print(os.environ['CLAUDE_ACC_CREDITS_ORG'])", **extra)
        return r.returncode, r.stdout.strip()

    def test_pays_with_the_credit_that_expires_first_within_scope(self):
        self.assertEqual(self.org_of(), (0, "org-d"))  # own: d wygasa przed a; b (klient) wcześniej, ale to inny zakres
        self.assertEqual(self.org_of("--scope", "client"), (0, "org-b"))
        self.w.run("balance", "d@example.com", "--remaining-usd", "3")
        self.assertEqual(self.org_of(), (0, "org-a"))  # d ma mniej niż próg 5 USD
        self.assertEqual(self.org_of("--min-remaining-usd", "2"), (0, "org-d"))

    def test_org_pin_and_scope_never_cross(self):
        self.assertEqual(self.org_of("--org", "org-a"), (0, "org-a"))
        code, out = self.org_of("--org", "org-b", "--scope", "own")
        self.assertEqual((code, out), (1, ""))
        self.w.run("balance", "b@example.com", "--remaining-usd", "1")
        code, out = self.org_of("--scope", "client")
        self.assertEqual((code, out), (75, ""))  # klient bez zapasu: 75, nigdy prywatne konto
        code, out = self.org_of("--org", "org-b")
        self.assertEqual((code, out), (75, ""))

    def test_streams_exit_code_and_key_only_in_child_env(self):
        r = self.w.run("exec", "--purpose", "polid-t", "--org", "org-a", "--", PY, "-c", CHILD, KEY_A, "7",
                       input="hello\n", ANTHROPIC_AUTH_TOKEN="tok", ANTHROPIC_BASE_URL="https://proxy.example",
                       ANTHROPIC_API_KEY="sk-ant-api03-fake-stale")
        self.assertEqual(r.returncode, 7)
        self.assertEqual(r.stdout.splitlines(), ["HELLO", "org=org-a", "key=expected", "auth_token=unset", "base_url=unset"])
        self.assertEqual(r.stderr, "to stderr\n")

    def test_without_headroom_exits_75_and_never_starts(self):
        marker = os.path.join(self.w.home, "ran")
        for email in ("a@example.com", "d@example.com"):
            self.w.run("balance", email, "--remaining-usd", "4")
        r = self.w.run("exec", "--purpose", "polid-t", "--", "touch", marker)
        self.assertEqual(r.returncode, 75)
        self.assertIn("brak kredytu API", r.stderr)
        self.assertFalse(os.path.exists(marker))
        r = self.w.run("key", "--purpose", "polid-t", "--json")
        self.assertEqual((r.returncode, r.stdout), (75, ""))

    def test_account_whose_key_vanished_is_skipped_and_marked(self):
        keychain = self.w.keychain()
        del keychain["claude-acc-credits|d@example.com"]
        write_json(os.path.join(self.w.fake, "keychain.json"), keychain)
        self.assertEqual(self.org_of(), (0, "org-a"))
        self.assertEqual(self.w.account("d@example.com")["state"], "error")
        self.assertEqual(self.w.status()["total_remaining_usd"], 400.0)  # konto bez klucza nie zasila sumy

    def test_credit_running_out_mid_run_exits_76_and_is_not_reused(self):
        # tak kończy się `claude -p`, gdy API odpowie "credit balance is too low": komunikat na stdout, kod 1;
        # zdanie przychodzi w dwóch kawałkach, rozcięte w środku
        child = ("import sys, time; sys.stdout.write('Credit bal'); sys.stdout.flush(); time.sleep(0.3); "
                 "print('ance is too low'); sys.exit(1)")
        r = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", child)
        self.assertEqual(r.returncode, 76)
        self.assertEqual(r.stdout, "Credit balance is too low\n")
        self.assertIn("nie ponawiaj", r.stderr)
        self.assertEqual(self.w.account("d@example.com")["remaining_usd"], 0.0)
        self.assertEqual(self.org_of(), (0, "org-a"))  # następny bieg już nie trafia w wyczerpane konto

    def test_phrase_in_a_successful_run_is_not_an_interruption(self):
        child = "print('the model wrote: Your credit balance is too low')"
        r = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", child)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.w.account("d@example.com")["remaining_usd"], 200.0)


# dziecko `exec --no-env`: woła helper tak jak Claude Code apiKeyHelper (sh, stdout do rury)
HELPER_CHILD = """
import os, subprocess, sys
print("env_key=" + ("missing" if "ANTHROPIC_API_KEY" not in os.environ else "present"))
out = subprocess.run(["/bin/sh", "-c", sys.argv[1]], capture_output=True, text=True)
print("helper=" + ("expected" if out.stdout == sys.argv[2] else "rc%d" % out.returncode))
"""


class Helper(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.pool()
        self.helper = f"{PY} {SCRIPT} helper --purpose polid-t"

    def test_no_env_run_gets_the_key_only_through_the_helper(self):
        r = self.w.run("exec", "--no-env", "--purpose", "polid-t", "--", PY, "-c", HELPER_CHILD, self.helper, KEY_D,
                       ANTHROPIC_API_KEY="sk-ant-api03-fake-stale")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["env_key=missing", "helper=expected"])
        self.assertNotIn("helper kredytów nie został wywołany", r.stderr)

    def test_no_env_run_that_never_calls_the_helper_is_flagged(self):
        r = self.w.run("exec", "--no-env", "--purpose", "polid-t", "--", "true")
        self.assertEqual(r.returncode, 0)
        self.assertIn("helper kredytów nie został wywołany", r.stderr)

    def test_helper_follows_the_org_exec_chose_and_respects_scope(self):
        r = self.w.run("helper", "--purpose", "polid-t", CLAUDE_ACC_CREDITS_ORG="org-a")
        self.assertEqual((r.returncode, r.stdout), (0, KEY_A))
        r = self.w.run("helper", "--purpose", "polid-t", "--scope", "client", CLAUDE_ACC_CREDITS_ORG="org-a")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        r = self.w.run("helper", "--purpose", "polid-t", "--scope", "client")
        self.assertEqual((r.returncode, r.stdout), (0, KEY_B))

    def test_helper_does_not_print_the_key_to_a_terminal(self):
        leader, follower = pty.openpty()
        proc = subprocess.Popen([PY, SCRIPT, "helper", "--purpose", "polid-t"], env=self.w.env(),
                                stdout=follower, stderr=subprocess.PIPE)
        os.close(follower)
        _, err = proc.communicate(timeout=30)
        shown = b""
        try:
            while True:
                chunk = os.read(leader, 4096)
                if not chunk:
                    break
                shown += chunk
        except OSError:
            pass
        os.close(leader)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(KEY_D.encode(), shown + err)


class HelperRun(unittest.TestCase):
    """`helper --run ID`: komenda w ustawieniach biegu sama wskazuje bieg i zapisuje, kto zapłacił."""

    def setUp(self):
        self.w = World()
        self.w.pool()

    def run_json(self, run_id, pid, start):
        """runenv/runs/<id>/run.json tak, jak zapisuje go prepare(): właściciel biegu (pid i czas startu)."""
        path = os.path.join(self.w.state, "runenv", "runs", run_id)
        os.makedirs(path, exist_ok=True)
        write_json(os.path.join(path, "run.json"), {"run_id": run_id, "owner_pid": pid, "owner_start": start})
        self.addCleanup(shutil.rmtree, path, True)

    def test_run_flag_records_the_paying_org_and_the_log_line_stays_as_it_was(self):
        sys.path.insert(0, ROOT)
        import runenv

        run_id = "0123456789abcdef"
        self.run_json(run_id, os.getpid(), runenv.pid_state(os.getpid())[1])  # bieg trwa: właścicielem jest ten test
        r = self.w.run("helper", "--purpose", "polid-t", "--org", "org-a", "--run", run_id)
        self.assertEqual((r.returncode, r.stdout), (0, KEY_A))
        marker = os.path.join(self.w.credits, "runs", run_id)
        lines = [json.loads(x) for x in read(marker).splitlines()]
        self.assertEqual([(x["org_id"], x["email"], x["purpose"]) for x in lines], [("org-a", "a@example.com", "polid-t")])
        self.assertNotIn(KEY_A, read(marker))
        last = read(os.path.join(self.w.credits, "credits.log")).splitlines()[-1]
        self.assertRegex(last, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  helper polid-t: a@example\.com \(org-a\)$")
        for bad in ("xyz", "0123", "0123456789ABCDEF"):
            r = self.w.run("helper", "--purpose", "polid-t", "--run", bad)
            self.assertEqual((r.returncode, r.stdout), (2, ""), bad)

    def test_run_flag_gives_no_key_once_the_run_is_over_or_its_owner_died(self):
        # R-O4: `claude`, który przeżył zabitego właściciela biegu, nie płaci dalej z puli
        run_id = "fedcba9876543210"
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        for case, owner in (("brak run.json", None), ("właściciel nie żyje", (gone.pid, None)),
                            ("ten pid, ale inny proces (czas startu)", (os.getpid(), 1))):
            if owner:
                self.run_json(run_id, *owner)
            r = self.w.run("helper", "--purpose", "polid-t", "--org", "org-a", "--run", run_id)
            self.assertEqual((r.returncode, r.stdout), (75, ""), case)
            self.assertIn("klucza nie wydaję", r.stderr, case)
        self.assertFalse(os.path.exists(os.path.join(self.w.credits, "runs", run_id)))  # nikt nie zapłacił
        self.assertIn(f"odmowa, bieg {run_id}", read(os.path.join(self.w.credits, "credits.log")))
        # bez --run (exec --no-env, Polid) helper działa jak dotąd
        r = self.w.run("helper", "--purpose", "polid-t", "--org", "org-a")
        self.assertEqual((r.returncode, r.stdout), (0, KEY_A))


class PlanEnd(unittest.TestCase):
    """Anulowany plan nie dostaje nowego przydziału w rocznicę cyklu (credits.py view)."""

    def setUp(self):
        self.w = World()
        self.assertEqual(self.w.add("a@example.com", KEY_A, "--scope", "own").returncode, 0)
        # jak 1@ na dysku: resets_at z `balance --expires-at` (rocznica cyklu) i odczyt 168,94 sprzed
        # rocznicy; rocznica była wczoraj, nowego odczytu nie ma
        path = os.path.join(self.w.credits, "accounts.json")
        registry = json.loads(read(path))
        registry["accounts"]["a@example.com"].update(
            resets_at=day(-1), balance={"at": time.time() - 3 * 86400, "remaining_usd": 168.94})
        write_json(path, registry)

    def test_without_a_plan_end_the_anniversary_grants_as_before(self):
        a = self.w.account("a@example.com")
        self.assertEqual((a["granted_usd"], a["remaining_usd"]), (200.0, 200.0))
        # nowy cykl od wczorajszej rocznicy: następna za mniej więcej miesiąc
        resets = datetime.fromisoformat(a["cycle_resets_at"]).replace(tzinfo=None)
        self.assertGreater(resets, datetime.now() + timedelta(days=26))

    def test_after_the_plan_end_nothing_is_left_and_exec_exits_75_with_the_usual_message(self):
        r = self.w.run("balance", "a@example.com", "--plan-ends-at", day(-1))
        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.w.status()
        self.assertEqual(set(data), {"total_remaining_usd", "accounts"})
        self.assertEqual(set(data["accounts"][0]), CONTRACT)  # klucze kontraktu bez zmian
        a = data["accounts"][0]
        self.assertEqual((a["granted_usd"], a["remaining_usd"], a["cycle_resets_at"]), (0.0, 0.0, None))
        self.assertEqual(data["total_remaining_usd"], 0.0)
        marker = os.path.join(self.w.home, "ran")
        r = self.w.run("exec", "--no-env", "--purpose", "polid-t", "--", "touch", marker)
        self.assertEqual(r.returncode, 75)
        self.assertIn("brak kredytu API", r.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.w.run("key", "--purpose", "polid-t", "--json").returncode, 75)
        self.assertIn("plan do", self.w.run("status").stdout)

    def test_a_reading_after_the_end_counts_and_none_lifts_the_end(self):
        self.assertEqual(self.w.run("balance", "a@example.com", "--plan-ends-at", day(-1)).returncode, 0)
        self.assertEqual(self.w.run("balance", "a@example.com", "--remaining-usd", "50").returncode, 0)
        self.assertEqual(self.w.run("record", "--org", "org-a", "--usd", "10", "--purpose", "polid-x").returncode, 0)
        self.assertEqual(self.w.account("a@example.com")["remaining_usd"], 40.0)
        r = self.w.run("balance", "a@example.com", "--plan-ends-at", "none")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("plan_ends_at", self.w.registry()["a@example.com"])
        self.assertIsNotNone(self.w.account("a@example.com")["cycle_resets_at"])

    def test_a_plan_ending_soon_is_spent_first(self):
        self.assertEqual(self.w.add("d@example.com", KEY_D, "--scope", "own", "--resets-at", day(3)).returncode, 0)
        r = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", "import os; print(os.environ['CLAUDE_ACC_CREDITS_ORG'])")
        self.assertEqual(r.stdout.strip(), "org-d")  # d wygasa za 3 dni, a dopiero w następnej rocznicy
        self.assertEqual(self.w.run("balance", "a@example.com", "--plan-ends-at", day(1)).returncode, 0)
        r = self.w.run("exec", "--purpose", "polid-t", "--", PY, "-c", "import os; print(os.environ['CLAUDE_ACC_CREDITS_ORG'])")
        self.assertEqual(r.stdout.strip(), "org-a")  # kredyt a przepada jutro z końcem planu

    def test_balance_still_needs_an_amount_without_a_plan_end(self):
        r = self.w.run("balance", "a@example.com")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--remaining-usd", r.stderr)
        r = self.w.run("balance", "a@example.com", "--plan-ends-at", "jutro")
        self.assertEqual(r.returncode, 2)


class Guard(unittest.TestCase):
    """Strażnik w sesjach Claude Code: agent nie wyciąga kluczy kredytów ani nie woła helpera."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("devguard_for_credits", os.path.join(ROOT, "devguard.py"))
        cls.devguard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.devguard)

    def decide(self, command):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.devguard.admit(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"}))
        return "deny" if '"deny"' in out.getvalue() else "allow"

    def test_reading_credit_keys_is_denied_and_using_credits_is_not(self):
        import re

        gate = re.compile(self.devguard.HOOK_GATE)
        for command in ("security find-generic-password -s claude-acc-credits -a a@example.com -w",
                        "claude-acc credits helper --purpose polid-x"):
            self.assertEqual(self.decide(command), "deny", command)
            self.assertTrue(gate.search(command), command)  # natywny front oddaje ją Pythonowi
        for command in ("claude-acc credits exec --purpose polid-x -- true", "claude-acc credits status --json",
                        # import z cudzego wpisu niczego nie wypisuje: klucz idzie z Pęku kluczy do Pęku kluczy
                        f"claude-acc credits add u@example.com --scope own --from-keychain {SOURCE}"):
            self.assertEqual(self.decide(command), "allow", command)


if __name__ == "__main__":
    unittest.main()
