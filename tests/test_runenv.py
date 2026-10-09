"""Testy środowiska biegu (runenv.py) i licznika (meter.py) na atrapach.

Każdy test stawia osobny $HOME i uruchamia prawdziwe skrypty jako procesy, z atrapami na początku
PATH (tests/fakes-runenv, potem fakes-credits i fakes): `security` (Pęk kluczy w pliku, dziennik
argumentów i stdin), `claude-acc` (status --json i token --json z $HOME/fake/accounts.json) i
Claude Code (zainstalowany jako przypięta wersja i jako ~/.local/bin/claude), który płaci tak,
jak zmierzyło A0: helper z ustawień katalogu, wpis Pęku kluczy katalogu albo bazowy (tu: konto
Blazity), i zgłasza zapytania do OpenTelemetry z `env` ustawień. Kto naprawdę zapłacił, atrapa
zapisuje w $HOME/fake/api-calls.jsonl; testy porównują to z rachunkiem biegu, dziennikiem
wydatków i kodem wyjścia. Wartości oczekiwane biorą się z FACTS-A0 i planu (budżet × 1,5 + zapas
20 USD, Blazity bb6c7f81-..., tylko źródło rotation), nie z kodu.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_runenv
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

try:
    from tests.test_credits import FAKES, FAKES_CREDITS, KEY_A, KEY_D, PY, ROOT, SCRIPT, day, read, write_json
    from tests.test_credits import World as CreditsWorld
except ImportError:  # uruchomione z katalogu tests
    from test_credits import FAKES, FAKES_CREDITS, KEY_A, KEY_D, PY, ROOT, SCRIPT, day, read, write_json
    from test_credits import World as CreditsWorld

HERE = os.path.dirname(os.path.abspath(__file__))
FAKES_RUNENV = os.path.join(HERE, "fakes-runenv")
RUNENV = os.path.join(ROOT, "runenv.py")
VERSION = "2.1.294"
BLAZITY = "filip.maszota@blazity.com"
BLAZITY_ORG = "bb6c7f81-65b7-4ea3-90bc-3d0ae91eb837"
SECRET = re.compile(r"sk-ant-|oat01|eyJ")
HEAD, TAIL, ACTIVE = "10@example.com", "5@example.com", "7@example.com"

sys.path.insert(0, ROOT)
import meter  # noqa: E402
import runenv  # noqa: E402


def accounts(*extra):
    """Konta subskrypcji jak w `claude-acc status --json`: głowa kolejki, ogon, aktywne i Blazity."""
    rows = [
        {"email": HEAD, "queue": 1, "session_used": 0, "weekly_used": 10},
        {"email": ACTIVE, "queue": 2, "active": True, "session_used": 5, "weekly_used": 5},
        {"email": TAIL, "queue": 3, "session_used": 20, "weekly_used": 40, "session_resets_at": time.time() + 7200},
        {"email": BLAZITY, "queue": None, "usable": False, "session_used": 0, "weekly_used": 0},
    ]
    return rows + list(extra)


class World(CreditsWorld):
    """Świat biegu: $HOME z przypiętą atrapą Claude Code, kontami subskrypcji i bazowym logowaniem Blazity."""

    def __init__(self):
        super().__init__()
        self.versions = os.path.join(self.home, ".local/share/claude/versions")
        os.makedirs(self.versions)
        self.install(VERSION)
        os.makedirs(os.path.join(self.home, ".local/bin"))
        os.symlink(os.path.join(FAKES_RUNENV, "claude"), os.path.join(self.home, ".local/bin/claude"))
        self.runenv = os.path.join(self.state, "runenv")
        os.makedirs(self.runenv)
        write_json(os.path.join(self.runenv, "pin.json"), {"version": VERSION})
        self.subscriptions(accounts())
        # bazowy wpis Pęku kluczy (bez CLAUDE_CONFIG_DIR) to dziś konto Blazity (FACTS-A0, Q1)
        server = {"access": {"sk-ant-oat01-fake-blazity-base": BLAZITY}, "counter": 100}
        write_json(os.path.join(self.fake, "server.json"), server)
        self.stash("Claude Code-credentials", "tester", json.dumps({"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-fake-blazity-base", "refreshToken": "sk-ant-ort01-fake-blazity",
            "expiresAt": int(time.time() * 1000) + 3600000}}))
        self.cwd = os.path.join(self.home, "work")
        os.makedirs(self.cwd)

    def install(self, version):
        os.symlink(os.path.join(FAKES_RUNENV, "claude"), os.path.join(self.versions, version))

    def env(self, **extra):
        env = {"HOME": self.home, "USER": "tester", "PATH": f"{FAKES_RUNENV}:{FAKES_CREDITS}:{FAKES}:/usr/bin:/bin"}
        env.update(extra)
        return env

    def subscriptions(self, rows, **token):
        write_json(os.path.join(self.fake, "accounts.json"), {"accounts": rows, "token": dict({"source": "rotation"}, **token)})

    def credit(self, email, key, remaining, expires_in_days):
        """Konto z kredytem i odczytem z Console (zostało `remaining`, wygasa za tyle dni)."""
        r = self.add(email, key, "--scope", "own", "--resets-at", day(expires_in_days))
        assert r.returncode == 0, r.stderr
        r = self.run("balance", email, "--remaining-usd", str(remaining))
        assert r.returncode == 0, r.stderr

    def go(self, *options, cmd=("claude", "-p", "hi"), budget="2", mode=None, **extra):
        """`credits run` jak u człowieka; (proces, rachunek albo None)."""
        summary = os.path.join(self.home, f"summary-{time.time_ns()}.json")
        argv = [PY, SCRIPT, "run", "--purpose", "blog-x", "--budget-usd", budget, "--summary", summary]
        if mode:
            argv += ["--mode", mode]
        r = subprocess.run(argv + list(options) + ["--"] + list(cmd), env=self.env(**extra), cwd=self.cwd,
                           capture_output=True, text=True, timeout=120)
        return r, (json.loads(read(summary)) if os.path.exists(summary) else None)

    def calls(self):
        return [json.loads(x) for x in self.recorded("api-calls.jsonl").splitlines() if x.strip()]

    def ledger(self):
        out = []
        if os.path.isdir(self.credits):
            for name in sorted(os.listdir(self.credits)):
                if name.startswith("ledger-"):
                    out += [json.loads(x) for x in read(os.path.join(self.credits, name)).splitlines() if x.strip()]
        return out

    def runs_left(self):
        """Biegi z katalogiem w toku (z run.json); po skończonym zostaje tylko odmawiający bin/claude."""
        path = os.path.join(self.runenv, "runs")
        names = sorted(os.listdir(path)) if os.path.isdir(path) else []
        for name in names:
            left = sorted(os.listdir(os.path.join(path, name)))
            if "finished" in left:
                assert left == ["bin", "finished"], left
                assert os.listdir(os.path.join(path, name, "bin")) == ["claude"]
        return [n for n in names if os.path.exists(os.path.join(path, n, "run.json"))]

    def avoid(self):
        path = os.path.join(self.runenv, "avoid.json")
        return json.loads(read(path)) if os.path.exists(path) else {}

    def run_entries(self):
        """Wpisy Pęku kluczy katalogów biegów (bazowy i klucze kredytów pomijamy)."""
        return [k for k in self.keychain() if k.startswith("Claude Code-credentials-")]

    def logins_written(self):
        """Bloby logowania zapisane przez `security -i` z -X, rozkodowane ze stdin atrapy."""
        blobs = []
        for line in self.recorded("security-stdin.log").splitlines():
            m = re.search(r"-X ([0-9a-f]+)", line)
            if m:
                blobs.append(json.loads(bytes.fromhex(m.group(1)).decode()))
        return blobs


def projects_folder(world):
    """~/.claude/projects/<katalog biegu> tak, jak nazywa go Claude Code (ścieżka po rozwinięciu dowiązań)."""
    return os.path.join(world.home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(world.cwd)))


# transkrypt sesji w katalogu biegu, zapisany w trakcie biegu: punkt wejścia jak u Claude Code
TRANSCRIPT = """
import json, os, sys
os.makedirs(sys.argv[1], exist_ok=True)
with open(os.path.join(sys.argv[1], sys.argv[2] + ".jsonl"), "w") as f:
    f.write(json.dumps({"type": "last-prompt"}) + "\\n")
    f.write(json.dumps({"type": "user", "entrypoint": sys.argv[3]}) + "\\n")
"""


def blazity_paid(world):
    return [c for c in world.calls() if c["payer"] == f"oauth:{BLAZITY}"]


class Payer(unittest.TestCase):
    """Wybór płatnika przed startem: kredyt, inaczej subskrypcja, inaczej 75."""

    def setUp(self):
        self.w = World()

    def test_credits_pay_when_an_org_has_budget_times_1_5_plus_reserve(self):
        self.w.pool()  # a: 200 USD (20 dni), d: 200 USD (3 dni), b: klient
        r, s = self.w.go(budget="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s["mode"], "credits")
        self.assertEqual(s["payer"], {"email": "d@example.com", "org_id": "org-d"})  # own, wygasa pierwszy
        self.assertEqual([c["payer"] for c in self.w.calls()], ["key:org-d"])
        self.assertEqual(s["payer_check"], {"verdict": "ok", "problems": []})
        self.assertEqual((s["requests"], s["cost_usd"]), (1, 0.001))
        self.assertIn("płaci kredyt d@example.com", r.stderr)
        # 22,50 USD to budżet + zapas, ale nie budżet × 1,5 + zapas (23): ta organizacja odpada
        self.assertEqual(self.w.run("balance", "d@example.com", "--remaining-usd", "22.50").returncode, 0)
        r, s = self.w.go(budget="2")
        self.assertEqual(s["payer"]["email"], "a@example.com", r.stderr)

    def test_subscription_when_credit_is_below_budget_plus_reserve_and_the_summary_says_why(self):
        self.w.credit("a@example.com", KEY_A, 24, 20)  # S12: 24 USD, budżet 5, zapas 20 -> 27,50 potrzebne
        r, s = self.w.go(budget="5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s["mode"], "subscription")
        self.assertEqual(s["payer"]["email"], TAIL)
        self.assertIn("$27.50", s["reason"])
        self.assertIn("$24.00", s["reason"])
        self.assertEqual([c["payer"] for c in self.w.calls()], [f"oauth:{TAIL}"])
        self.assertEqual(self.w.ledger(), [])  # subskrypcja nie zjada puli

    def test_no_credit_and_no_subscription_exits_75_with_a_reason_and_starts_nothing(self):
        self.w.subscriptions(accounts(), fail=True)
        marker = os.path.join(self.w.home, "started")
        r, s = self.w.go(cmd=("touch", marker))
        self.assertEqual(r.returncode, 75)
        self.assertIsNone(s)
        self.assertIn("kredyt:", r.stderr)
        self.assertIn("subskrypcja:", r.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual((self.w.calls(), self.w.runs_left(), self.w.run_entries()), ([], [], []))

    def test_blazity_is_never_chosen_even_as_the_only_account_with_headroom(self):
        self.w.subscriptions([{"email": BLAZITY, "queue": 1, "session_used": 0, "weekly_used": 0},
                              {"email": ACTIVE, "queue": 2, "active": True}])
        r, _ = self.w.go()
        self.assertEqual(r.returncode, 75)
        self.assertNotIn("token", self.w.recorded("claude-acc-argv.log"))  # nawet nie pyta o token
        # token, który mimo --avoid wraca z kontem Blazity, też odpada
        self.w.subscriptions(accounts(), force_email=BLAZITY)
        r, _ = self.w.go()
        self.assertEqual(r.returncode, 75, r.stderr)
        self.assertIn(f"--avoid {BLAZITY}", self.w.recorded("claude-acc-argv.log"))
        self.assertEqual(blazity_paid(self.w), [])
        self.assertEqual(self.w.run_entries(), [])
        # inne konto z `last_resort` w konfiguracji claude-acc też zostaje dla sesji człowieka
        self.w.subscriptions(accounts({"email": "9@example.com", "queue": 4, "last_resort": True}))
        r, s = self.w.go()
        self.assertEqual(s["payer"]["email"], TAIL, r.stderr)
        self.assertIn("--avoid 9@example.com", self.w.recorded("claude-acc-argv.log").splitlines()[-1])

    def test_fallback_and_active_tokens_are_refused(self):
        for source in ("fallback", "active"):
            self.w.subscriptions(accounts(), source=source)
            r, _ = self.w.go()
            self.assertEqual(r.returncode, 75, source)
            self.assertIn("nie z rotacji", r.stderr, source)
        self.assertEqual(self.w.calls(), [])

    def test_the_active_account_is_never_chosen(self):
        self.w.subscriptions(accounts(), force_email=ACTIVE)  # accswitch oddał konto aktywne
        r, _ = self.w.go()
        self.assertEqual(r.returncode, 75)
        self.assertIn(f"--avoid {ACTIVE}", self.w.recorded("claude-acc-argv.log"))
        self.assertEqual(self.w.calls(), [])

    def test_tail_of_the_queue_is_preferred_and_an_almost_empty_session_is_skipped(self):
        r, s = self.w.go()
        self.assertEqual(s["payer"]["email"], TAIL, r.stderr)
        argv = self.w.recorded("claude-acc-argv.log")
        self.assertIn(f"--prefer {TAIL}", argv)
        self.assertIn("--min-minutes 150", argv)  # czuwanie 120 min + 30 min zapasu
        # S11: ogon z 11% sesji (próg claude-acc to 10%) nie bierze wielogodzinnego biegu
        rows = accounts()
        rows[2]["session_used"] = 89
        self.w.subscriptions(rows)
        r, s = self.w.go()
        self.assertEqual(s["payer"]["email"], HEAD, r.stderr)
        self.assertIn(f"--avoid {TAIL}", self.w.recorded("claude-acc-argv.log").splitlines()[-1])

    def test_unreadable_credit_key_refuses_before_start_with_no_request(self):
        self.w.pool()
        r, _ = self.w.go(mode="credits", FAKE_SECURITY_READ_FAIL="51")  # S12: Pęk kluczy nie oddaje klucza
        self.assertEqual(r.returncode, 75)
        self.assertIn("klucza nie da się odczytać", r.stderr)
        self.assertEqual(self.w.calls(), [])
        self.assertEqual(self.w.account("d@example.com")["state"], "linked")  # klucz jest, tylko chwilowo nieczytelny
        r, s = self.w.go(FAKE_SECURITY_READ_FAIL="51")  # auto: subskrypcja
        self.assertEqual((r.returncode, s["mode"]), (0, "subscription"), r.stderr)

    def test_a_blazity_row_usable_at_the_tail_is_never_chosen_or_preferred(self):
        # R-T4: konto firmowe na samym końcu kolejki, z zapasem i bez last_resort, i tak nie płaci
        rows = accounts()
        rows[3] = {"email": BLAZITY, "queue": 9, "usable": True, "last_resort": False, "session_used": 0, "weekly_used": 0}
        self.w.subscriptions(rows)
        r, s = self.w.go()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s["payer"]["email"], TAIL)
        argv = self.w.recorded("claude-acc-argv.log").splitlines()[-1]
        self.assertIn(f"--prefer {TAIL}", argv)
        self.assertIn(f"--avoid {BLAZITY}", argv)
        self.assertEqual(blazity_paid(self.w), [])

    def test_a_long_awake_limit_refuses_a_subscription_without_asking_for_a_token(self):
        # K-4: token dostępu żyje ok. 8 h; `token --min-minutes` ponad to odświeżyłby każde konto i i tak odmówił
        r, _ = self.w.go("--awake-minutes", "600", mode="subscription")
        self.assertEqual(r.returncode, 75, r.stderr)
        self.assertIn("8 h", r.stderr)
        self.assertNotIn("token", self.w.recorded("claude-acc-argv.log"))
        self.assertEqual((self.w.calls(), self.w.run_entries()), ([], []))
        self.w.subscriptions(accounts(), expires_in_min=480)  # świeży token: ok. 8 h
        r, s = self.w.go("--awake-minutes", "420", mode="subscription")  # 420 + 30 = 450 min: jeszcze się mieści
        self.assertEqual((r.returncode, s["mode"]), (0, "subscription"), r.stderr)
        self.assertIn("--min-minutes 450", self.w.recorded("claude-acc-argv.log"))
        self.w.pool()  # kredyt nie ma tego limitu: ten sam długi bieg płaci z puli
        r, s = self.w.go("--awake-minutes", "600")
        self.assertEqual((r.returncode, s["mode"]), (0, "credits"), r.stderr)

    def test_parallel_credit_runs_hold_their_stop_threshold(self):
        # R-O3: organizacja ma 60 USD; bieg za 25 USD trzyma 37,50 (próg zatrzymania), więc drugi taki
        # bieg nie zmieści się z zapasem 20 USD, dopóki pierwszy trwa; mały bieg (1,50 + 20) jeszcze tak
        self.w.credit("d@example.com", KEY_D, 60, 3)
        driver = (
            "import json, sys; sys.path.insert(0, sys.argv[1]); import runenv\n"
            "a = runenv.prepare('blog-x', 25, mode='credits')\n"
            "try:\n"
            "    runenv.prepare('blog-y', 25, mode='credits'); second = 'paid'\n"
            "except runenv.Refused as exc:\n"
            "    second = exc.reason\n"
            "small = runenv.prepare('blog-z', 1, mode='credits')\n"
            "runenv.finish(small, exit_code=0); runenv.finish(a, exit_code=0)\n"
            "c = runenv.prepare('blog-y', 25, mode='credits'); runenv.finish(c, exit_code=0)\n"
            "print(json.dumps({'second': second, 'small': small.payer['org_id'], 'c': c.payer['org_id']}))\n"
        )
        r = subprocess.run([PY, "-c", driver, ROOT], env=self.w.env(), cwd=self.w.cwd, capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotEqual(out["second"], "paid")
        self.assertIn("$37.50", out["second"])  # ile trzyma bieg w toku
        self.assertEqual((out["small"], out["c"]), ("org-d", "org-d"))  # po końcu pierwszego zapas wraca
        self.assertEqual(self.w.runs_left(), [])

    def test_project_settings_with_their_own_login_refuse_before_start(self):
        # R-O6: ustawienia projektu stoją wyżej niż katalog biegu (user < project < local < flag < policy)
        self.w.pool()
        folder = os.path.join(self.w.cwd, ".claude")
        os.makedirs(folder)
        marker = os.path.join(self.w.home, "started")
        cases = (("settings.local.json", {"apiKeyHelper": "/usr/local/bin/other-helper"}, "settings.local.json: apiKeyHelper"),
                 ("settings.json", {"env": {"ANTHROPIC_API_KEY": "x"}}, "settings.json: env.ANTHROPIC_API_KEY"))
        for name, data, why in cases:
            write_json(os.path.join(folder, name), data)
            r, s = self.w.go(cmd=("touch", marker))
            self.assertEqual(r.returncode, 75, name)
            self.assertIn(why, r.stderr)
            self.assertFalse(os.path.exists(marker))
            os.unlink(os.path.join(folder, name))
        self.assertEqual((self.w.calls(), self.w.runs_left(), self.w.run_entries()), ([], [], []))
        write_json(os.path.join(folder, "settings.json"), {"permissions": {"allow": ["Bash(git:*)"]}, "env": {"FOO": "1"}})
        r, s = self.w.go()  # zwykłe ustawienia projektu nie przeszkadzają
        self.assertEqual((r.returncode, s["payer_check"]["verdict"]), (0, "ok"), r.stderr)

    def test_a_login_check_that_fails_after_the_keychain_write_leaves_nothing(self):
        # R-T3: logowanie zapisane, a `auth status` pokazuje Blazity albo brak logowania: 75, nic nie zostaje
        marker = os.path.join(self.w.home, "started")
        for extra in ({"FAKE_CLAUDE_AUTH_ORG": BLAZITY_ORG}, {"FAKE_CLAUDE_AUTH_LOGGED_OUT": "1"}):
            r, s = self.w.go(cmd=("touch", marker), mode="subscription", **extra)
            self.assertEqual(r.returncode, 75, (extra, r.stderr))
            self.assertIn("nic nie uruchomiono", r.stderr)
            self.assertFalse(os.path.exists(marker))
        log = self.w.recorded("security.log").splitlines()
        writes = [x.split(" ", 1)[1] for x in log if x.startswith("add-generic-password Claude Code-credentials-")]
        deletes = [x.split(" ", 1)[1] for x in log if x.startswith("delete-generic-password Claude Code-credentials-")]
        self.assertEqual((len(writes), sorted(deletes)), (2, sorted(writes)))
        self.assertEqual((self.w.run_entries(), self.w.runs_left(), self.w.calls()), ([], [], []))


class Settings(unittest.TestCase):
    """Co biegi dostają w katalogu konfiguracji i w CLAUDE_ACC_JOB_SETTINGS."""

    def setUp(self):
        self.w = World()

    def seen(self, mode):
        """(ustawienia, które widział Claude Code biegu, CLAUDE_ACC_JOB_SETTINGS, rachunek)."""
        copy = os.path.join(self.w.home, f"settings-{mode}.json")
        job = os.path.join(self.w.home, f"job-{mode}.json")
        r, s = self.w.go(mode=mode, cmd=("sh", "-c", f'claude -p hi && cp "$CLAUDE_ACC_JOB_SETTINGS" {job}'),
                         FAKE_CLAUDE_COPY_SETTINGS=copy)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(read(copy)), json.loads(read(job)), s

    def check_common(self, settings):
        env = settings["env"]
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertEqual(env["OTEL_LOGS_EXPORTER"], "otlp")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_PROTOCOL"], "http/json")
        self.assertRegex(env["OTEL_EXPORTER_OTLP_ENDPOINT"], r"^http://127\.0\.0\.1:\d+$")
        self.assertEqual(env["OTEL_LOGS_EXPORT_INTERVAL"], "1000")
        self.assertRegex(env["OTEL_RESOURCE_ATTRIBUTES"], r"^job\.run=[0-9a-f]{16},job\.purpose=blog-x$")
        self.assertEqual((env["BASH_MAX_TIMEOUT_MS"], env["DISABLE_AUTOUPDATER"]), ("3600000", "1"))
        self.assertEqual(env["CLAUDE_CODE_PROMPT_CACHE_TTL"], "1h")
        # jeden hook: strażnik sekretów; żadnej pauzy, Orki, rtk ani hooków użytkownika
        self.assertEqual(list(settings["hooks"]), ["PreToolUse"])
        self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)
        entry = settings["hooks"]["PreToolUse"][0]
        self.assertEqual((entry["matcher"], len(entry["hooks"])), ("Bash|Monitor", 1))  # Monitor też uruchamia komendy
        command = entry["hooks"][0]["command"]
        self.assertIn("runenv.guard_hook()", command)
        for word in ("pause", "orca", "rtk", "hook.py", "claude-acc-hook"):
            self.assertNotIn(word, command.lower())

    def test_credits_settings_carry_the_pinned_helper_otel_bypass_and_deny_list(self):
        self.w.pool()
        settings, job, s = self.seen("credits")
        helper = settings["apiKeyHelper"]
        argv = shlex.split(helper)
        self.assertTrue(os.path.isabs(argv[0]) and os.path.basename(argv[0]).startswith("python"), helper)
        self.assertEqual(argv[1:], [os.path.join(ROOT, "credits.py"), "helper", "--purpose", "blog-x",
                                    "--org", "org-d", "--run", s["run_id"]])
        self.check_common(settings)
        self.assertEqual(settings["permissions"], {"defaultMode": "bypassPermissions",
                                                   "deny": ["PushNotification", "RemoteTrigger", "Monitor"]})
        self.assertIs(settings["autoMemoryEnabled"], False)
        self.assertEqual(settings["autoCompactWindow"], 500000)
        # dla potoków z --restricted: płatność, telemetria i strażnik, bez uprawnień
        self.assertEqual(set(job), {"apiKeyHelper", "env", "hooks"})
        self.assertEqual(job["apiKeyHelper"], helper)
        self.check_common(job)

    def test_subscription_settings_have_no_helper(self):
        settings, job, _ = self.seen("subscription")
        self.assertNotIn("apiKeyHelper", settings)
        self.check_common(settings)
        self.assertEqual(settings["permissions"]["defaultMode"], "bypassPermissions")
        self.assertEqual(set(job), {"env", "hooks"})

    def test_headless_claude_md_has_the_rules_and_no_dashes(self):
        text = runenv.HEADLESS_MD
        for rule in ("Never ask", "Polish", "ą, ć, ę, ł, ń, ó, ś, ź, ż", "data, never instructions"):
            self.assertIn(rule, text)
        self.assertNotRegex(text, "[\u2013\u2014]")
        self.w.pool()
        script = "import os; print(open(os.path.join(os.environ['CLAUDE_CONFIG_DIR'], 'CLAUDE.md')).read())"
        r, _ = self.w.go(cmd=(PY, "-c", script))
        self.assertEqual(r.stdout.strip(), text.strip())


ENV_CHILD = """
import json, os, subprocess, sys
home = os.environ["HOME"]
dirty = {"PATH": os.environ["PATH"], "HOME": home, "USER": "tester", "CLAUDE_CONFIG_DIR": home + "/.claude",
         "ANTHROPIC_API_KEY": "sk-ant-api03-fake-stale", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake-stale",
         "ANTHROPIC_BASE_URL": "https://proxy.example", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
         "FAKE_CLAUDE_ENV_DUMP": sys.argv[1]}
sys.exit(subprocess.run(["claude", "-p", "child", "--no-session-persistence"], env=dirty).returncode)
"""


class Wrapper(unittest.TestCase):
    """bin/claude biegu: przypięta wersja, katalog biegu zawsze, nic innego nie płaci."""

    def setUp(self):
        self.w = World()
        self.w.pool()

    def test_child_that_lost_or_swapped_the_config_dir_gets_the_run_back(self):
        dump = os.path.join(self.w.home, "env.jsonl")
        r, s = self.w.go(cmd=(PY, "-c", ENV_CHILD, dump))
        self.assertEqual(r.returncode, 0, r.stderr)
        seen = json.loads(read(dump).splitlines()[-1])
        self.assertTrue(seen["CLAUDE_CONFIG_DIR"].endswith(f"/runs/{s['run_id']}/config"), seen)
        self.assertEqual((seen["CLAUDE_ACC_CREDITS_ORG"], seen["CLAUDE_ACC_CREDITS_RUN"]), ("org-d", s["run_id"]))
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
            self.assertIsNone(seen[name], name)
        self.assertEqual([c["payer"] for c in self.w.calls()], ["key:org-d"])
        self.assertEqual(s["payer_check"]["verdict"], "ok")

    def test_wrapper_refuses_once_the_run_is_over(self):
        driver = (
            "import json, os, subprocess, sys; sys.path.insert(0, sys.argv[1]); import runenv\n"
            "run = runenv.prepare('blog-x', 2)\n"
            "wrapper = os.path.join(run.bin_dir, 'claude')\n"
            "runenv.finish(run, exit_code=0)\n"
            "r = subprocess.run([wrapper, '-p', 'late'], env=run.env, capture_output=True, text=True)\n"
            "print(json.dumps({'code': r.returncode, 'err': r.stderr}))\n"
        )
        r = subprocess.run([PY, "-c", driver, ROOT], env=self.w.env(), cwd=self.w.cwd, capture_output=True, text=True, timeout=60)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["code"], 78, r.stderr)
        self.assertIn("już się skończył", out["err"])
        self.assertEqual(self.w.calls(), [])

    def test_claude_by_absolute_path_without_the_run_dir_fails_the_payer_check(self):
        # S2: przebudowane środowisko i ~/.local/bin/claude z absolutnej ścieżki: płaci bazowe logowanie (Blazity)
        leak = (f"exec env -i HOME={self.w.home} USER=tester PATH=/usr/bin:/bin FAKE_CLAUDE_SLEEP=2 "
                f"{self.w.home}/.local/bin/claude -p leak --no-session-persistence")
        r, s = self.w.go(cmd=("sh", "-c", f"claude -p ok && {leak}"))
        paid = blazity_paid(self.w)  # atrapa płaci kontem bazowym, jeśli zdąży przed zatrzymaniem
        self.assertLessEqual(len(paid), 1)
        if not paid:  # pod obciążeniem strażnik zatrzymuje wyciek, zanim ten zapłaci; to też poprawny wynik
            self.assertEqual(s["stopped"], "leak", s)
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertEqual(s["payer_check"]["verdict"], "mismatch")
        self.assertTrue(any("bez katalogu biegu" in x for x in s["leaks"]), s["leaks"])

    def test_session_written_outside_the_run_dir_fails_the_payer_check(self):
        # ten sam wyciek bez czasu na obserwację procesu: zostaje plik sesji w ~/.claude/projects
        leak = f"env -i HOME={self.w.home} USER=tester PATH=/usr/bin:/bin {self.w.home}/.local/bin/claude -p leak"
        r, s = self.w.go(cmd=("sh", "-c", f"claude -p ok && {leak}"))
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertTrue(any("powstała w trakcie biegu" in x for x in s["leaks"]), s["leaks"])

    def test_your_interactive_session_in_the_run_dir_is_not_a_leak(self):
        # R-O2: Twoja sesja interaktywna ("cli", także po /clear) w tym katalogu w trakcie biegu to nie wyciek;
        # sesja `claude -p` sprzed startu biegu też nie
        folder = projects_folder(self.w)
        subprocess.run([PY, "-c", TRANSCRIPT, folder, "before", "sdk-cli"], check=True)
        time.sleep(0.05)
        mine = f"{PY} -c {shlex.quote(TRANSCRIPT)} {shlex.quote(folder)} mine cli"
        r, s = self.w.go(cmd=("sh", "-c", f"claude -p ok && {mine}"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((s["payer_check"]["verdict"], s["leaks"]), ("ok", []))
        self.assertTrue(os.path.exists(os.path.join(folder, "mine.jsonl")))


class Metering(unittest.TestCase):
    """Licznik: koszt na sesję z OpenTelemetry, dziennik wydatków i sprawdzenie płatnika."""

    def setUp(self):
        self.w = World()

    def test_nested_call_through_the_bash_tool_is_metered_and_booked_per_session(self):
        self.w.pool()
        r, s = self.w.go(cmd=("claude", "-p", "top"), FAKE_CLAUDE_BASH="claude -p nested", FAKE_CLAUDE_REQUESTS="2",
                         FAKE_CLAUDE_COST="0.0125")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.w.calls()
        self.assertEqual(sorted(c["depth"] for c in calls), [0, 0, 1, 1])  # 2 zapytania na górze i 2 w zagnieżdżonym
        self.assertEqual({c["payer"] for c in calls}, {"key:org-d"})
        self.assertEqual((s["sessions_seen"], s["sessions_started"], s["requests"]), (2, 2, 4))
        self.assertAlmostEqual(s["cost_usd"], 0.05)
        self.assertTrue(s["metering"]["complete"], s["metering"])
        ledger = [e for e in self.w.ledger() if e.get("run") == s["run_id"]]
        self.assertEqual(len(ledger), 2)  # wpis na sesję
        self.assertEqual({e["org_id"] for e in ledger}, {"org-d"})
        self.assertAlmostEqual(sum(e["usd"] for e in ledger), 0.05)
        self.assertEqual(s["ledger_usd"], s["cost_usd"])
        self.assertAlmostEqual(self.w.account("d@example.com")["spent_usd"], 0.05)

    def test_subscription_cost_is_in_the_summary_and_not_in_the_ledger(self):
        r, s = self.w.go(FAKE_CLAUDE_REQUESTS="3")
        self.assertEqual((r.returncode, s["mode"]), (0, "subscription"), r.stderr)
        self.assertAlmostEqual(s["cost_usd"], 0.003)
        self.assertEqual((s["ledger_usd"], self.w.ledger()), (0.0, []))
        self.assertEqual(s["payer_check"]["verdict"], "ok")

    def test_payer_check_fails_on_an_email_in_a_credits_run(self):
        self.w.pool()
        r, s = self.w.go(FAKE_CLAUDE_FORCE_EMAIL=HEAD)
        self.assertEqual(r.returncode, 78)
        self.assertEqual(s["payer_check"]["verdict"], "mismatch")
        self.assertIn("zapłaciła subskrypcja", s["payer_check"]["problems"][0])

    def test_payer_check_fails_on_another_account_or_the_blazity_org_in_a_subscription_run(self):
        for extra, why in (({"FAKE_CLAUDE_FORCE_EMAIL": HEAD}, HEAD), ({"FAKE_CLAUDE_FORCE_ORG": BLAZITY_ORG}, "Blazity")):
            r, s = self.w.go(**extra)
            self.assertEqual(r.returncode, 78, extra)
            self.assertEqual(s["payer_check"]["verdict"], "mismatch", extra)
            self.assertIn(why, " ".join(s["payer_check"]["problems"]))

    def test_billing_error_marks_the_org_empty_ends_76_and_books_what_was_spent(self):
        self.w.pool()
        r, s = self.w.go(FAKE_CLAUDE_ERROR="billing", FAKE_CLAUDE_COST="0.02")
        self.assertEqual(r.returncode, 76, r.stderr)
        self.assertTrue(s["exhausted"])
        self.assertEqual(self.w.account("d@example.com")["remaining_usd"], 0.0)
        ledger = [e for e in self.w.ledger() if e.get("run") == s["run_id"]]
        self.assertEqual([(e["org_id"], e["usd"]) for e in ledger], [("org-d", 0.02)])
        r, s2 = self.w.go()  # następny bieg: inna organizacja, nie ta wyczerpana
        self.assertEqual(s2["payer"]["org_id"], "org-a", r.stderr)

    def test_missing_session_or_cost_makes_metering_incomplete_and_the_payer_unverified(self):
        self.w.pool()
        cases = (
            ("claude -p a && FAKE_CLAUDE_NO_OTEL=1 claude -p b", "1 z 2"),  # sesja bez zdarzeń
            ("FAKE_CLAUDE_NO_COST=1 claude -p a", "bez cost_usd"),
            ("FAKE_CLAUDE_NO_OTEL=1 claude -p a", "0 z 1"),  # licznik nic nie dostał: zero zapytań to nie dowód
        )
        for command, why in cases:
            r, s = self.w.go(cmd=("sh", "-c", command))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(s["metering"]["complete"], command)
            self.assertIn(why, " ".join(s["metering"]["problems"]), command)
            self.assertEqual(s["payer_check"]["verdict"], "unverified", command)
            for entry in self.w.ledger():
                self.assertGreater(entry["usd"], 0)  # żadnego cichego 0 USD

    def test_429_on_a_subscription_account_makes_later_runs_avoid_it_and_401_marks_nothing(self):
        r, s = self.w.go(FAKE_CLAUDE_ERROR="401")
        self.assertEqual(r.returncode, 1, (r.stderr, s))
        self.assertEqual([e["kind"] for e in s["errors"]], ["auth"])
        self.assertEqual(self.w.avoid(), {})  # S11a: nic nie odświeża i nic nie oznacza
        self.assertNotIn("login", self.w.recorded("claude-acc-argv.log"))
        r, s = self.w.go(FAKE_CLAUDE_ERROR="429")
        self.assertEqual(r.returncode, 1, (r.stderr, s))
        self.assertIn(TAIL, self.w.avoid())
        r, s = self.w.go()
        self.assertEqual(s["payer"]["email"], HEAD, r.stderr)
        self.assertIn(f"--avoid {TAIL}", self.w.recorded("claude-acc-argv.log").splitlines()[-1])


class Isolation(unittest.TestCase):
    """Biegi obok siebie, sprzątanie po końcu i po śmierci właściciela, sekrety."""

    def setUp(self):
        self.w = World()

    def test_keychain_entry_and_run_dir_are_removed_after_the_run(self):
        r, s = self.w.go()
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.w.recorded("security.log").splitlines()
        writes = [x.split(" ", 1)[1] for x in log if x.startswith("add-generic-password Claude Code-credentials-")]
        deletes = [x.split(" ", 1)[1] for x in log if x.startswith("delete-generic-password Claude Code-credentials-")]
        self.assertEqual(len(writes), 1)
        self.assertEqual(deletes, writes)
        self.assertEqual((self.w.run_entries(), self.w.runs_left()), ([], []))
        self.assertIn("Claude Code-credentials|tester", self.w.keychain())  # bazowego nikt nie dotyka

    def test_token_never_reaches_argv_files_or_output_and_the_login_cannot_refresh(self):
        r, s = self.w.go(cmd=("claude", "-p", "hi"))
        self.assertEqual(r.returncode, 0, r.stderr)
        (blob,) = self.w.logins_written()
        oauth = blob["claudeAiOauth"]
        self.assertTrue(oauth["accessToken"].startswith("sk-ant-oat01-fake-5@example.com"))
        self.assertIsNone(oauth["refreshToken"])  # bieg nie obróci konta i nie wyloguje jego sesji
        self.assertGreater(oauth["expiresAt"], (time.time() + 150 * 60) * 1000)
        for name in ("security-argv.log", "claude-acc-argv.log", "curl-argv.log"):
            self.assertNotRegex(self.w.recorded(name), SECRET, name)
        self.assertNotRegex(r.stdout + r.stderr, SECRET)
        fixtures = {os.path.join(self.w.fake, n) for n in ("keychain.json", "server.json", "credits-api.json", "security-stdin.log")}
        bytecode = os.path.join(self.w.home, "Library/Caches/com.apple.python")  # Python Apple'a: bajtkod naszych modułów
        for folder, _, files in os.walk(self.w.home):
            if folder.startswith(bytecode):
                continue
            for name in files:
                path = os.path.join(folder, name)
                if path not in fixtures and not os.path.islink(path):
                    self.assertNotRegex(read(path), SECRET, path)

    def test_malformed_token_output_refuses_without_echoing_it(self):
        self.w.subscriptions(accounts(), malformed=True)
        r, s = self.w.go()
        self.assertEqual(r.returncode, 75)
        self.assertIn("nie jest JSON-em", r.stderr)
        self.assertNotRegex(r.stdout + r.stderr, SECRET)
        self.assertEqual((self.w.run_entries(), self.w.calls()), ([], []))

    def test_pinned_helper_org_survives_a_child_that_lost_the_org_variable(self):
        # S3: A ma 10 USD i wygasa pierwsza, B 150 USD; budżet 2 + zapas 20: płaci B, także dziecko bez CLAUDE_ACC_CREDITS_ORG
        self.w.credit("a@example.com", KEY_A, 10, 6)
        self.w.credit("d@example.com", KEY_D, 150, 23)
        pinned = os.path.join(self.w.versions, VERSION)
        r, s = self.w.go(cmd=("sh", "-c", f"claude -p top && env -u CLAUDE_ACC_CREDITS_ORG -u CLAUDE_ACC_CREDITS_RUN {pinned} -p bare"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s["payer"]["org_id"], "org-d")
        self.assertEqual([c["payer"] for c in self.w.calls()], ["key:org-d", "key:org-d"])
        self.assertEqual({e["org_id"] for e in self.w.ledger() if e.get("run")}, {"org-d"})
        self.assertEqual(s["payer_check"]["verdict"], "ok")
        # helper tego biegu, który oddał klucz innej organizacji, psuje werdykt
        other = f"{PY} {SCRIPT} helper --purpose blog-x --org org-a --run $CLAUDE_ACC_RUN > /dev/null"
        r, s = self.w.go(cmd=("sh", "-c", f"claude -p top && {other}"))
        self.assertEqual(r.returncode, 78)
        self.assertIn("org-a", " ".join(s["payer_check"]["problems"]))

    def test_restricted_pipeline_pays_and_is_metered_only_with_the_job_settings_merged(self):
        # S5: --restricted gubi ustawienia katalogu; scalone CLAUDE_ACC_JOB_SETTINGS przywraca płatnika i licznik
        self.w.pool()
        r, s = self.w.go(cmd=("sh", "-c", "claude --restricted -p bare"))
        self.assertEqual(r.returncode, 1)  # Not logged in, nic nie zapłacone
        self.assertEqual(self.w.calls(), [])
        r, s = self.w.go(cmd=("sh", "-c", 'claude --restricted --settings "$CLAUDE_ACC_JOB_SETTINGS" -p merged'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["payer"] for c in self.w.calls()], ["key:org-d"])
        self.assertEqual((s["requests"], s["payer_check"]["verdict"]), (1, "ok"))
        r, s = self.w.go(mode="subscription", cmd=("sh", "-c", 'claude --restricted --settings "$CLAUDE_ACC_JOB_SETTINGS" -p merged'))
        self.assertEqual((r.returncode, s["requests"], s["payer_check"]["verdict"]), (0, 1, "ok"), r.stderr)

    def test_finish_stops_processes_left_with_the_run_dir_and_keeps_their_cost(self):
        # S9: komenda kończy się, a zagnieżdżony claude zostaje w tle
        self.w.pool()
        bg = "FAKE_CLAUDE_SLEEP=60 nohup claude -p background > /dev/null 2>&1 &"
        posted = f"for i in $(seq 100); do [ -s {self.w.fake}/api-calls.jsonl ] && break; sleep 0.1; done; sleep 1"
        r, s = self.w.go(cmd=("sh", "-c", f"{bg} {posted}; echo done"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(s["killed"], 1)
        self.assertEqual(s["requests"], 1)  # zapytanie z tła doszło przed zatrzymaniem
        pid = self.w.calls()[0]["pid"]
        self.assertFalse(runenv.pid_state(pid)[0])

    TWO_RUNS = (
        "import json, os, subprocess, sys, urllib.request; sys.path.insert(0, sys.argv[1]); import runenv\n"
        "mode = sys.argv[2]\n"
        "a = runenv.prepare('blog-x', 2, mode=mode); b = runenv.prepare('blog-x', 2, mode=mode)\n"
        "def post(run, port, cost, rid):\n"
        "    attrs = [('event.name', 'api_request'), ('session.id', 's-' + rid), ('request_id', rid)]\n"
        "    if run.mode == 'subscription': attrs += [('user.email', run.payer['email']), ('organization.id', 'org-x')]\n"
        "    kv = [{'key': k, 'value': {'stringValue': v}} for k, v in attrs] + [{'key': 'cost_usd', 'value': {'doubleValue': cost}}]\n"
        "    body = {'resourceLogs': [{'resource': {'attributes': [{'key': 'job.run', 'value': {'stringValue': run.run_id}}]},"
        " 'scopeLogs': [{'logRecords': [{'attributes': kv}]}]}]}\n"
        "    urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/v1/logs', json.dumps(body).encode())).read()\n"
        "subprocess.run(['claude', '-p', 'b'], env=b.env, check=True)\n"
        "post(b, a.meta['port'], 5.0, 'req-b-on-a')\n"  # paczka biegu B na porcie A
        "post(a, a.meta['port'], 0.002, 'req-a')\n"  # zapytanie A bez wywołania helpera A
        "info = {'dirs': [a.dir, b.dir], 'ports': [a.meta['port'], b.meta['port']], 'a_spent': a.spent(),\n"
        "        'services': [a.meta.get('keychain_service'), b.meta.get('keychain_service')]}\n"
        "sa = runenv.finish(a, exit_code=0)\n"
        "info['b_intact'] = os.path.isdir(b.config_dir)\n"
        "info['b_after'] = subprocess.run(['claude', '-p', 'b2'], env=b.env).returncode\n"
        "sb = runenv.finish(b, exit_code=0)\n"
        "print(json.dumps({'info': info, 'a': sa, 'b': sb}))\n"
    )

    def two_runs(self, mode):
        r = subprocess.run([PY, "-c", self.TWO_RUNS, ROOT, mode], env=self.w.env(), cwd=self.w.cwd,
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        info, a, b = out["info"], out["a"], out["b"]
        self.assertNotEqual(*info["dirs"])
        self.assertNotEqual(*info["ports"])
        self.assertNotEqual(a["run_id"], b["run_id"])
        self.assertEqual(info["a_spent"], 0.002)  # paczka biegu B, która trafiła na port A, się nie liczy
        self.assertTrue(info["b_intact"])
        self.assertEqual(info["b_after"], 0)  # koniec A nie zabrał B katalogu ani logowania
        self.assertEqual((a["requests"], b["requests"]), (1, 2))
        self.assertEqual(b["payer_check"]["verdict"], "ok")
        self.assertEqual(self.w.runs_left(), [])
        return info, a, b

    def test_two_credit_runs_of_one_purpose_stay_apart(self):
        # S7: helper B nie zaświadcza za A
        self.w.pool()
        _, a, _ = self.two_runs("credits")
        self.assertEqual(a["payer_check"]["verdict"], "mismatch")
        self.assertIn("helper kredytów nie był wywołany", " ".join(a["payer_check"]["problems"]))
        self.assertFalse(os.listdir(os.path.join(self.w.credits, "runs")))  # znaczniki obu biegów sprzątnięte

    def test_two_subscription_runs_of_one_purpose_stay_apart(self):
        info, a, _ = self.two_runs("subscription")
        self.assertNotEqual(*info["services"])
        self.assertTrue(all(info["services"]))
        self.assertEqual(a["payer_check"]["verdict"], "ok")
        self.assertEqual(self.w.run_entries(), [])

    def test_finish_twice_and_a_resent_batch_count_once(self):
        # S10
        self.w.pool()
        driver = (
            "import json, os, subprocess, sys, urllib.request; sys.path.insert(0, sys.argv[1]); import runenv\n"
            "run = runenv.prepare('blog-x', 2)\n"
            "body = {'resourceLogs': [{'resource': {'attributes': [{'key': 'job.run', 'value': {'stringValue': run.run_id}}]},"
            " 'scopeLogs': [{'logRecords': [{'attributes': ["
            "{'key': 'event.name', 'value': {'stringValue': 'api_request'}},"
            "{'key': 'cost_usd', 'value': {'doubleValue': 0.25}}, {'key': 'session.id', 'value': {'stringValue': 's1'}},"
            "{'key': 'request_id', 'value': {'stringValue': 'req-1'}}]}]}]}]}\n"
            "url = f\"http://127.0.0.1:{run.meta['port']}/v1/logs\"\n"
            "for _ in range(3): urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode())).read()\n"
            # rozliczenie, które zaksięgowało i padło przed sprzątnięciem: następne nie księguje drugi raz
            "runenv.book_ledger(run.meta, {'s1': 0.25})\n"
            "first = runenv.finish(run, exit_code=0); second = runenv.finish(run, exit_code=0)\n"
            "print(json.dumps([first, second]))\n"
        )
        r = subprocess.run([PY, "-c", driver, ROOT], env=self.w.env(), cwd=self.w.cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(first, second)
        self.assertEqual((first["requests"], first["cost_usd"]), (1, 0.25))
        ledger = [e for e in self.w.ledger() if e.get("run") == first["run_id"]]
        self.assertEqual([e["usd"] for e in ledger], [0.25])
        history = [json.loads(x) for x in read(os.path.join(self.w.runenv, "history.jsonl")).splitlines()]
        self.assertEqual([h["run_id"] for h in history], [first["run_id"]])

    def test_sweep_after_the_owner_was_killed_books_once_and_leaves_live_runs_alone(self):
        # S8: właściciel zabity SIGKILL z dzieckiem w trakcie; drugi bieg żyje
        self.w.pool()
        owner = (
            "import json, os, subprocess, sys, time; sys.path.insert(0, sys.argv[1]); import runenv\n"
            "run = runenv.prepare('blog-x', 2, mode=sys.argv[2])\n"
            "env = dict(run.env, FAKE_CLAUDE_REQUESTS='2', FAKE_CLAUDE_SLEEP='120', FAKE_CLAUDE_COST='0.04')\n"
            "child = subprocess.Popen(['claude', '-p', 'long'], env=env, stdout=subprocess.DEVNULL)\n"
            "run.attach(child.pid)\n"
            "while run.spent() < 0.08: time.sleep(0.05)\n"
            "print(json.dumps({'run': run.run_id, 'dir': run.dir, 'mode': run.mode, 'service': run.meta.get('keychain_service')}), flush=True)\n"
            "time.sleep(600)\n"
        )

        def start(mode):
            proc = subprocess.Popen([PY, "-c", owner, ROOT, mode], env=self.w.env(), cwd=self.w.cwd,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return proc, json.loads(proc.stdout.readline())

        dead_credit, info_c = start("credits")
        dead_sub, info_s = start("subscription")
        live, info_live = start("subscription")
        children = sorted({c["pid"] for c in self.w.calls()})
        self.assertEqual(len(children), 3)
        try:
            for proc in (dead_credit, dead_sub):
                proc.send_signal(signal.SIGKILL)
                proc.wait()
            for _ in range(2):  # drugie sprzątanie niczego nie księguje drugi raz
                r = subprocess.run([PY, RUNENV, "sweep"], env=self.w.env(), capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
            ledger = [e for e in self.w.ledger() if e.get("run") == info_c["run"]]
            self.assertEqual([e["usd"] for e in ledger], [0.08])
            self.assertEqual(self.w.runs_left(), [info_live["run"]])
            self.assertEqual(self.w.run_entries(), [f"{info_live['service']}|tester"])
            history = [json.loads(x) for x in read(os.path.join(self.w.runenv, "history.jsonl")).splitlines()]
            self.assertEqual(sorted(h["run_id"] for h in history), sorted([info_c["run"], info_s["run"]]))
            self.assertTrue(all(h["crashed"] and not h["metering"]["complete"] for h in history))
            alive = [pid for pid in children if runenv.pid_state(pid)[0]]
            self.assertEqual(len(alive), 1)  # zostało tylko dziecko żywego biegu
        finally:
            live.send_signal(signal.SIGKILL)
            live.wait()
            for proc in (dead_credit, dead_sub, live):
                proc.stdout.close()
                proc.stderr.close()
            subprocess.run([PY, RUNENV, "sweep"], env=self.w.env(), capture_output=True, timeout=60)
        self.assertEqual((self.w.runs_left(), self.w.run_entries()), ([], []))

    def killed_owner(self, *steps):
        """Właściciel biegu na kredycie, który wykonał steps (kod Pythona z `run`), a potem zginął (SIGKILL)."""
        owner = (
            "import json, os, subprocess, sys, time; sys.path.insert(0, sys.argv[1]); import runenv\n"
            "run = runenv.prepare('blog-x', 2, mode='credits')\n"
            + "".join(step + "\n" for step in steps)
            + "print(json.dumps({'run': run.run_id, 'events': run.meta['events_path']}), flush=True)\n"
            "time.sleep(600)\n"
        )
        proc = subprocess.Popen([PY, "-c", owner, ROOT], env=self.w.env(), cwd=self.w.cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertTrue(line, proc.stderr.read() if proc.poll() is not None else "")
            return json.loads(line)
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def sweep(self):
        r = subprocess.run([PY, RUNENV, "sweep"], env=self.w.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        history = [json.loads(x) for x in read(os.path.join(self.w.runenv, "history.jsonl")).splitlines()]
        return history[-1]

    def test_a_swept_run_reports_a_session_in_its_dir_as_unverified_not_a_mismatch(self):
        # R-O2: bez właściciela okno biegu jest przybliżone, a `claude -p` w tym katalogu mógł być Twój
        self.w.pool()
        folder = projects_folder(self.w)
        info = self.killed_owner(  # sesja `claude -p` w tym katalogu w trakcie biegu, potem dalsza praca biegu
            f"subprocess.run([sys.executable, '-c', {TRANSCRIPT!r}, {folder!r}, 'other', 'sdk-cli'], check=True)",
            "time.sleep(0.05)",
            "subprocess.run(['claude', '-p', 'a', '--no-session-persistence'], env=run.env, check=True, capture_output=True)",
            "while run.spent() < 0.001: time.sleep(0.05)",
        )
        h = self.sweep()
        self.assertEqual(h["run_id"], info["run"])
        self.assertEqual(h["payer_check"]["verdict"], "unverified", h)
        self.assertTrue(any("możliwy wyciek" in x and "other.jsonl" in x for x in h["metering"]["problems"]), h["metering"])
        self.assertEqual(h["leaks"], [])

    def test_a_swept_session_is_booked_at_its_last_request_not_at_the_sweep(self):
        # R-O7: odczyt z Console zrobiony po zapytaniu, a przed rozliczeniem, już ten koszt zawiera
        self.w.pool()
        info = self.killed_owner(
            "subprocess.run(['claude', '-p', 'a', '--no-session-persistence'], env=dict(run.env, FAKE_CLAUDE_COST='0.5'), "
            "check=True, capture_output=True)",
            "while run.spent() < 0.5: time.sleep(0.05)",
        )
        asked = time.time() - 7200  # zapytanie dwie godziny temu
        events = [json.loads(x) for x in read(info["events"]).splitlines()]
        with open(info["events"], "w") as f:
            f.write("".join(json.dumps(dict(e, t=asked)) + "\n" for e in events))
        self.assertEqual(self.w.run("balance", "d@example.com", "--remaining-usd", "100").returncode, 0)
        h = self.sweep()
        self.assertEqual(h["cost_usd"], 0.5)
        (entry,) = [e for e in self.w.ledger() if e.get("run") == info["run"]]
        self.assertAlmostEqual(entry["at"], asked, places=2)
        self.assertEqual(self.w.account("d@example.com")["remaining_usd"], 100.0)  # nie 99,50


class Version(unittest.TestCase):
    """Przypięta wersja Claude Code i canary."""

    def setUp(self):
        self.w = World()
        self.w.pool()

    def test_missing_pinned_version_refuses_before_start_and_names_it(self):
        # S13: aktualizator dołożył 2.1.300 i usunął przypiętą 2.1.294
        self.w.install("2.1.300")
        os.unlink(os.path.join(self.w.versions, VERSION))
        r, s = self.w.go()
        self.assertEqual(r.returncode, 75)
        self.assertIn("2.1.294", r.stderr)
        self.assertIn("2.1.300", r.stderr)
        self.assertEqual(self.w.calls(), [])
        os.unlink(os.path.join(self.w.runenv, "pin.json"))
        r, _ = self.w.go()
        self.assertEqual(r.returncode, 75)
        self.assertIn("canary", r.stderr)

    def test_canary_pins_a_new_version_only_when_its_cost_is_metered(self):
        self.w.install("2.1.300")
        pin = os.path.join(self.w.runenv, "pin.json")
        r = self.w.run("canary", FAKE_CLAUDE_NO_COST="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(read(pin))["version"], VERSION)
        r = self.w.run("canary")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(read(pin))["version"], "2.1.300")
        canary_calls = self.w.calls()
        self.assertTrue(all(c["argv0"].endswith("/2.1.300") for c in canary_calls), canary_calls)
        # pierwszy canary nie miał kosztu (nic do zaksięgowania), drugi ma jeden wpis
        self.assertEqual(len([e for e in self.w.ledger() if e.get("purpose") == "jobs-canary"]), 1)
        r, s = self.w.go()
        self.assertEqual((r.returncode, s["version"]), (0, "2.1.300"), r.stderr)


class Guard(unittest.TestCase):
    """Jedyny hook biegu: agent nie wyciągnie klucza ani logowania, a Claude Code dalej płaci."""

    def test_reads_of_keys_and_logins_are_denied_and_ordinary_commands_are_not(self):
        helper = f"{PY} {SCRIPT} helper --purpose blog-x --org 087b13f0 --run 0123456789abcdef"
        for command in (
            "security find-generic-password -s claude-acc-credits -a 1@outofplace.space -w",
            helper,
            f"'{PY}' '{SCRIPT}' helper --purpose x",
            "claude-acc credits helper --purpose x",
            'security find-generic-password -s "Claude Code-credentials-1a2b3c4d" -a filip -w',
            "security find-generic-password -s Claude\\ Code-credentials -w",
            'security find-generic-password -s "Orca Claude Code Managed Credentials" -a x -g',
            "security dump-keychain -d",
            "claude-acc token --json",
            "python3 ~/.local/share/claude-acc/accswitch.py token",
        ):
            self.assertTrue(runenv.guard_reason(command), command)
        # K-1: każda podkomenda kredytów poza status daje drogę do klucza; odpowiedź nie odsyła do `credits exec`
        for command in (
            "claude-acc credits exec --purpose x -- printenv ANTHROPIC_API_KEY",
            f"{PY} {SCRIPT} exec --purpose x -- printenv ANTHROPIC_API_KEY",
            f"'{PY}' '{SCRIPT}' exec --no-env --purpose x -- sh -c 'echo $ANTHROPIC_API_KEY'",
            "claude-acc credits key --purpose x --json",
            "python3 ~/.local/share/claude-acc/acc.py credits exec --purpose x -- env",
            "claude-acc credits helper --purpose x",
            helper,
            "security find-generic-password -s claude-acc-credits -a 1@outofplace.space -w",
        ):
            self.assertEqual(runenv.guard_reason(command), runenv.GUARD_HELPER, command)
        self.assertNotIn("credits exec", runenv.GUARD_HELPER)
        for command in ("git status", "pnpm autopilot", "claude -p 'Reply OK'", "cat docs/credits/helper.md",
                        "security find-generic-password -s github.com -a me", "claude-acc credits status --json",
                        "git commit -m 'Explain how API credits exec budgets work'", f"{PY} {SCRIPT} status"):
            self.assertIsNone(runenv.guard_reason(command), command)

    def test_the_guard_fails_closed_when_its_code_cannot_load(self):
        # K-5: kod 1 Claude Code przepuszcza; zepsuty import strażnika albo nieczytelne zdarzenie to 2 (odmowa)
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        ok = subprocess.run(["/bin/sh", "-c", runenv.guard_command()], input=event, capture_output=True, text=True, timeout=30)
        self.assertEqual((ok.returncode, ok.stdout), (0, ""), ok.stderr)
        broken_dir = tempfile.mkdtemp(prefix="guard-broken-")
        self.addCleanup(shutil.rmtree, broken_dir, True)
        with mock.patch.object(runenv, "HERE", broken_dir):
            broken = runenv.guard_command()
        r = subprocess.run(["/bin/sh", "-c", broken], input=event, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("strażnik biegu nie działa", r.stderr)
        r = subprocess.run(["/bin/sh", "-c", runenv.guard_command()], input="nie JSON", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        # Monitor (komenda ze strumieniem wyjścia do kontekstu) przechodzi przez ten sam sprawdzian
        monitor = json.dumps({"tool_name": "Monitor", "tool_input": {"command": "claude-acc token --json", "description": "x"}})
        r = subprocess.run(["/bin/sh", "-c", runenv.guard_command()], input=monitor, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", r.stderr)

    def test_monitor_is_denied_in_the_run_and_guarded_in_a_restricted_pipeline(self):
        # K-2: Monitor uruchamia komendę powłoki i strumieniuje jej wyjście do kontekstu agenta
        w = World()
        r, _ = w.go(mode="subscription", FAKE_CLAUDE_BASH="@login", FAKE_CLAUDE_TOOL="Monitor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("denied: Monitor w permissions.deny", w.recorded("bash-tool.log"))
        # potok z --restricted scala tylko CLAUDE_ACC_JOB_SETTINGS, bez naszej listy deny: strażnik i tak odmawia
        merged = 'claude --restricted --settings "$CLAUDE_ACC_JOB_SETTINGS" -p merged'
        r, _ = w.go(mode="subscription", cmd=("sh", "-c", merged), FAKE_CLAUDE_BASH="@login", FAKE_CLAUDE_TOOL="Monitor")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = w.recorded("bash-tool.log")
        self.assertEqual(log.split("\n---\n")[-2], f"denied: {runenv.GUARD_LOGIN}")
        self.assertNotRegex(log, SECRET)

    def test_the_run_hook_blocks_the_agent_and_the_helper_still_pays(self):
        w = World()
        w.pool()
        for bash in ("@helper", "security find-generic-password -s claude-acc-credits -a d@example.com -w"):
            r, s = w.go(FAKE_CLAUDE_BASH=bash)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(s["payer_check"]["verdict"], "ok")
        r, s = w.go(mode="subscription", FAKE_CLAUDE_BASH="@login")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = w.recorded("bash-tool.log")
        self.assertEqual(log.count("denied:"), 3, log)
        self.assertNotIn(KEY_D, log)
        self.assertNotRegex(log, SECRET)
        self.assertEqual({c["payer"] for c in w.calls()}, {"key:org-d", f"oauth:{TAIL}"})


DRIVE_CLI = (
    "import sys; sys.path.insert(0, sys.argv[1]); import runenv\n"
    "{patch}\n"
    "sys.exit(runenv.main(sys.argv[2:]))\n"
)


class Stops(unittest.TestCase):
    """`credits run` zatrzymuje bieg w trakcie: budżet, obcy płatnik po 429, komenda głucha na SIGTERM, wyjątek."""

    def setUp(self):
        self.w = World()

    def cli(self, patch, *args, **extra):
        """`credits run` przez runenv.main z poprawką modułu (patch) w tym samym procesie."""
        return subprocess.run([PY, "-c", DRIVE_CLI.format(patch=patch), ROOT, "run", "--purpose", "blog-x", *args],
                              env=self.w.env(**extra), cwd=self.w.cwd, capture_output=True, text=True, timeout=120)

    def test_budget_stop_at_1_5_times_the_budget_comes_before_the_next_session(self):
        # R-T1: budżet 0,001 USD, próg 0,0015; pierwsza sesja kosztuje 0,002, druga nie może wystartować
        self.w.pool()
        r, s = self.w.go(budget="0.001", cmd=("sh", "-c", "claude -p a; sleep 3; claude -p b"), FAKE_CLAUDE_COST="0.002")
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertEqual(s["stopped"], "budget")
        self.assertEqual((len(self.w.calls()), s["sessions_started"], s["cost_usd"]), (1, 1, 0.002))
        self.assertIn("zatrzymane: budget", r.stderr)

    def test_a_payer_mismatch_after_a_429_still_stops_the_run(self):
        # R-T2: 429 w pierwszej sesji, potem sesja z cudzym kontem; alarm płatnika zatrzymuje bieg w trakcie
        after = os.path.join(self.w.home, "after")
        cmd = f"FAKE_CLAUDE_ERROR=429 claude -p a; FAKE_CLAUDE_FORCE_EMAIL={HEAD} FAKE_CLAUDE_SLEEP=20 claude -p b; touch {after}"
        started = time.time()
        r, s = self.w.go(cmd=("sh", "-c", cmd))
        self.assertEqual(s["stopped"], "payer", r.stderr)
        self.assertEqual(r.returncode, 78)
        self.assertLess(time.time() - started, 18)
        self.assertFalse(os.path.exists(after))
        self.assertEqual([e["kind"] for e in s["errors"]], ["limit"])

    def test_a_command_deaf_to_sigterm_has_the_whole_run_stopped_after_the_grace(self):
        # R-O5: komenda ignoruje SIGTERM; po STOP_GRACE (tu 2 s zamiast 30) całe drzewo biegu dostaje stop
        self.w.pool()
        summary = os.path.join(self.w.home, "deaf.json")
        after = os.path.join(self.w.home, "after")
        started = time.time()
        r = self.cli("runenv.STOP_GRACE = 2.0", "--budget-usd", "0.001", "--summary", summary, "--",
                     "sh", "-c", f"trap '' TERM; claude -p a; sleep 60; touch {after}", FAKE_CLAUDE_COST="0.002")
        s = json.loads(read(summary))
        self.assertEqual(s["stopped"], "budget", r.stderr)
        self.assertEqual(r.returncode, 137)  # SIGKILL po SIGTERM, którego komenda nie słuchała
        self.assertLess(time.time() - started, 40)
        self.assertFalse(os.path.exists(after))
        self.assertEqual(self.w.runs_left(), [])

    def test_an_exception_mid_run_still_stops_books_and_removes_the_login(self):
        # przegląd, punkt 4: wyjątek w trakcie `credits run` nie zostawia tokenu w Pęku kluczy ani katalogu biegu
        patch = "def broken(run, command): raise RuntimeError('awaria w trakcie')\nrunenv.run_command = broken"
        r = self.cli(patch, "--budget-usd", "2", "--", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("awaria w trakcie", r.stderr)
        self.assertEqual((self.w.run_entries(), self.w.runs_left()), ([], []))
        history = [json.loads(x) for x in read(os.path.join(self.w.runenv, "history.jsonl")).splitlines()]
        self.assertEqual([(h["mode"], h["stopped"]) for h in history], [("subscription", "error")])


class LeakRules(unittest.TestCase):
    """Kiedy proces Claude Code to wyciek logowania, bez fałszywych alarmów z odczytu procesów macOS."""

    meta = {"bin_dir": "/runs/r1/bin", "config_dir": "/runs/r1/config", "run_id": "r1"}

    def test_claude_without_the_run_dir_is_a_leak_and_system_programs_are_not_judged(self):
        pinned = os.path.join(runenv.VERSIONS_DIR, VERSION)
        leak = runenv.leak_of
        self.assertIn("bez katalogu biegu", leak(self.meta, pinned, [pinned, "-p", "x"], {}))
        self.assertIn("bez katalogu biegu", leak(self.meta, pinned, ["claude"], {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.claude")}))
        self.assertIn("bez katalogu biegu", leak(self.meta, pinned, ["claude"], {"CLAUDE_CONFIG_DIR": "/runs/r1/config",
                                                                               "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""}))
        self.assertIsNone(leak(self.meta, pinned, ["claude"], {"CLAUDE_CONFIG_DIR": "/runs/r1/config"}))
        # nakładka /usr/bin/python3 i powłoki: jądro nie pokazuje ich środowiska (zmierzone), więc puste nic nie znaczy
        self.assertIsNone(leak(self.meta, "/usr/bin/python3", ["/usr/bin/python3", pinned, "-p"], {}))
        self.assertIsNone(leak(self.meta, "/bin/sh", ["sh", "/runs/r1/bin/claude", "-p"], {}))
        self.assertIsNone(leak(self.meta, "/bin/sh", ["/bin/sh", "/runs/r1/bin/claude"], {}))  # wrapper biegu
        self.assertIsNone(leak(self.meta, "", ["ython3", "python3"], {}))  # odczyt urwany w trakcie exec
        self.assertIsNone(leak(self.meta, "/opt/homebrew/bin/node", ["node", "server.js"], {}))  # nie Claude Code


class MeterUnit(unittest.TestCase):
    """Licznik bez procesów: paczki OTLP, cudze biegi, ponowienia, alarmy."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="meter-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "events.jsonl")

    def body(self, run, *requests, error=None):
        def kv(k, v):
            key = "doubleValue" if isinstance(v, float) else "intValue" if isinstance(v, int) else "stringValue"
            return {"key": k, "value": {key: str(v) if key == "intValue" else v}}

        records = [{"attributes": [kv("event.name", "api_request")] + [kv(k, v) for k, v in r.items()]} for r in requests]
        if error:
            records.append({"attributes": [kv("event.name", "api_error")] + [kv(k, v) for k, v in error.items()]})
        return {"resourceLogs": [{"resource": {"attributes": [kv("job.run", run)]}, "scopeLogs": [{"logRecords": records}]}]}

    def post(self, port, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/logs", json.dumps(body).encode())
        return urllib.request.urlopen(req, timeout=5).status

    def test_cost_per_session_once_per_request_and_only_for_this_run(self):
        m = meter.Meter("run-a", "credits", None, self.path)
        port = m.start()
        try:
            batch = self.body("run-a", {"session.id": "s1", "request_id": "r1", "cost_usd": 0.5, "model": "opus"},
                              {"session.id": "s2", "request_id": "r2", "cost_usd": 0.25, "model": "haiku"})
            self.assertEqual(self.post(port, batch), 200)
            self.assertEqual(self.post(port, batch), 200)  # eksporter ponowił paczkę
            self.post(port, self.body("run-b", {"session.id": "s9", "request_id": "r9", "cost_usd": 9.0}))
            self.assertEqual(m.spent(), 0.75)
            self.assertIsNone(m.alarm())
        finally:
            m.stop()
        rep = meter.report(meter.load_events(self.path), "credits", None, started=2)
        self.assertEqual(rep["sessions"], {"s1": 0.5, "s2": 0.25})
        self.assertEqual(rep["by_model"], {"haiku": 0.25, "opus": 0.5})
        self.assertEqual((rep["requests"], rep["payer_check"]["verdict"]), (2, "ok"))
        # po restarcie z tego samego pliku nic się nie dubluje
        again = meter.Meter("run-a", "credits", None, self.path)
        self.assertEqual(again.spent(), 0.75)

    def test_identity_alarms_and_error_callbacks(self):
        fired = []
        m = meter.Meter("run-s", "subscription", TAIL, self.path, on_error=lambda kind, e: fired.append(kind))
        m.ingest(self.body("run-s", {"session.id": "s", "request_id": "r1", "cost_usd": 0.1, "user.email": TAIL,
                                     "organization.id": "org-5"}))
        self.assertIsNone(m.alarm())
        m.ingest(self.body("run-s", {"session.id": "s", "request_id": "r2", "cost_usd": 0.1, "user.email": TAIL,
                                     "organization.id": BLAZITY_ORG},
                           error={"session.id": "s", "status_code": 429, "error": "rate_limit_error", "attempt": 1}))
        self.assertEqual(m.alarm()["kind"], "payer")
        self.assertEqual(fired, ["limit"])
        c = meter.Meter("run-c", "credits", None, os.path.join(self.dir, "c.jsonl"), on_error=lambda kind, e: fired.append(kind))
        c.ingest(self.body("run-c", error={"session.id": "s", "status_code": 400, "attempt": 1,
                                           "error": "Your credit balance is too low to access the Anthropic API."}))
        self.assertEqual(c.alarm()["kind"], "exhausted")
        self.assertEqual(fired, ["limit", "billing"])
        self.assertEqual(meter.report(meter.load_events(c.path), "credits", None, 0)["payer_check"]["verdict"], "unverified")

    def test_a_429_never_hides_a_later_payer_alarm(self):
        # R-O1: pierwszy alarm każdego rodzaju zostaje, a najważniejszy wygrywa (payer > ... > auth > limit)
        m = meter.Meter("run-s", "subscription", TAIL, self.path)
        m.ingest(self.body("run-s", error={"session.id": "s1", "status_code": 429, "error": "rate_limit_error", "attempt": 1}))
        self.assertEqual(m.alarm()["kind"], "limit")
        m.ingest(self.body("run-s", {"session.id": "s2", "request_id": "r2", "cost_usd": 0.1, "user.email": HEAD,
                                     "organization.id": "org-1"}))
        m.ingest(self.body("run-s", error={"session.id": "s3", "status_code": 401, "error": "authentication_error", "attempt": 1}))
        self.assertEqual(m.alarm()["kind"], "payer")
        self.assertEqual([a["kind"] for a in m.alarms()], ["payer", "auth", "limit"])
        self.assertIn(HEAD, m.alarm()["reason"])

    def test_zero_requests_never_verify_a_payer(self):
        for mode in ("credits", "subscription"):
            rep = meter.report([], mode, TAIL, started=1)
            self.assertEqual(rep["payer_check"]["verdict"], "unverified", mode)
            self.assertFalse(rep["metering"]["complete"])


if __name__ == "__main__":
    unittest.main()
