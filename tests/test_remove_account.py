"""`claude-acc remove <email>`: usuwa konto dodane w claude-acc (add, adopt) i nic poza nim.

Pod (Ustawienia, Konta) woła `remove <email> --json` i czyta jeden z trzech kształtów:
{"removed": e-mail} z kodem 0 albo {"error": zdanie, "reason": kod} z kodem 1. Testy stawiają
osobny $HOME z atrapami `security`, `claude` i API, uruchamiają prawdziwy skrypt jako proces
i sprawdzają to, co widać z zewnątrz: pliki, wpisy w Pęku kluczy, stan i wyjście. Żywe konta,
żywy Pęk kluczy i żywe sesje nie są dotykane; tablicę procesów podstawia CLAUDE_ACC_SESSIONS_FILE
(jedyny test na prawdziwej tablicy stawia własny proces).

Uruchomienie: /usr/bin/python3 -m unittest tests.test_remove_account
"""
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_e2e import BASE, SCRIPT, USER, scoped  # noqa: E402
from test_own_accounts import World  # noqa: E402
from test_pod_profiles import add_profile  # noqa: E402

PROMPT = "Remove {} from claude-acc? [y/N] "
# stan, który remove przepisuje (bez konta) albo dopisuje do niego linię; reszta drzewa ma zostać co do bajtu
REWRITTEN = ("state.json", "usage-cache.json", "history.jsonl", "status.json", "config.json")


def edit_json(path, change):
    with open(path) as f:
        data = json.load(f)
    change(data)
    with open(path, "w") as f:
        json.dump(data, f)


class RemoveWorld(World):
    def own(self, w, email, **kwargs):
        """Konto dodane w claude-acc: ten sam układ co po `add` ($STATE/accounts/<id>/auth/oauth-account.json
        plus kopia w Pęku kluczy pod id konta)."""
        blob = w.account(email, **kwargs)
        acct_id = w.ids[email]
        os.makedirs(os.path.join(w.state_dir, "accounts"), exist_ok=True)
        shutil.move(os.path.join(w.user_data, "claude-accounts", acct_id), os.path.join(w.state_dir, "accounts", acct_id))
        return acct_id, blob

    def edit_state(self, w, change, name="state.json"):
        path = os.path.join(w.state_dir, name)
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump({}, f)
        edit_json(path, change)

    def run_remove(self, w, *args, sessions=(), **extra):
        path = os.path.join(w.fake, "sessions.json")
        with open(path, "w") as f:
            json.dump([{"config_dir": d} for d in sessions], f)
        return w.run("remove", *args, CLAUDE_ACC_SESSIONS_FILE=path, **extra)

    def tree(self, w):
        """{ścieżka względna: bajty} wszystkiego pod $HOME poza tym, co zmienia się samo z każdym
        uruchomieniem: atrapy (logi rosną z każdym wywołaniem), pamięć podręczna interpretera, plik
        blokady i dziennik switch.log."""
        found = {}
        for root, dirs, files in os.walk(w.home):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in (w.fake, os.path.join(w.home, "Library", "Caches"))]
            for name in files:
                if name in ("lock", "switch.log"):
                    continue
                path = os.path.join(root, name)
                with open(path, "rb") as f:
                    found[os.path.relpath(path, w.home)] = f.read()
        return found

    def keychain(self, w):
        with open(os.path.join(w.fake, "keychain.json")) as f:
            return json.load(f)

    def state_file(self, w, name):
        with open(os.path.join(w.state_dir, name)) as f:
            return json.load(f)

    def refusal(self, r, reason):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(sorted(out), ["error", "reason"])
        self.assertEqual(out["reason"], reason)
        self.assertIsInstance(out["error"], str)
        return out["error"]

    def assert_nothing_changed(self, w, before_tree, before_keychain):
        self.assertEqual(self.tree(w), before_tree)
        self.assertEqual(self.keychain(w), before_keychain)

    def on_terminal(self, w, args, answer=None):
        """Uruchamia remove z terminalem (pty) pod stdin, stdout i stderr. Z answer czeka na pytanie
        i odpowiada; bez niego (odmowa) czeka na koniec. Zwraca (kod, wszystko, co poszło na terminal)."""
        path = os.path.join(w.fake, "sessions.json")
        with open(path, "w") as f:
            json.dump([], f)
        master, slave = pty.openpty()
        # stderr też na terminalu: input() pisze pytanie na stderr, a odmowa idzie tam samo
        proc = subprocess.Popen(["/usr/bin/python3", SCRIPT, "remove", *args], stdin=slave, stdout=slave,
                                stderr=slave, env=w.env(CLAUDE_ACC_SESSIONS_FILE=path))
        os.close(slave)
        seen = b""

        def pump(until=None):
            nonlocal seen
            deadline = time.time() + 30
            while time.time() < deadline:
                if until is not None and until.encode() in seen:
                    return
                if not select.select([master], [], [], 0.2)[0]:
                    if proc.poll() is not None:
                        return
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # EIO: dziecko zamknęło terminal
                    return
                if not chunk:
                    return
                seen += chunk
            self.fail(f"remove nie odpowiedział w 30 s: {seen!r}")

        try:
            if answer is not None:
                pump(until=PROMPT.format(args[0]))
                self.assertIn(PROMPT.format(args[0]).encode(), seen)
                os.write(master, answer.encode() + b"\n")
            pump()
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
        return code, seen.decode().replace("\r\n", "\n")


class RemoveTest(RemoveWorld):
    def test_removes_what_add_created_and_leaves_everything_else_alone(self):
        w = self.world()
        a = w.account("a@x")  # konto z Orca, aktywne
        w.account("b@x")  # drugie konto z Orca
        w.runtime(a)
        c_id, _ = self.own(w, "c@x")  # drugie konto claude-acc
        self.usage(w, "new@x")
        w.write()
        self.assertEqual(w.run("add", "new@x").returncode, 0)
        new_id = self.own_accounts(w)["new@x"]
        # stan, w którym każde z kont (nowe, b@x, c@x) ma swoje wpisy; po usunięciu mają zostać tylko cudze
        self.edit_state(w, lambda s: s.update(
            needs_login={"new@x": 100, "b@x": 200},
            needs_login_token={"new@x": "dead-new", "b@x": "dead-b"},
            api_backoff={"new@x": {"until": 9e9, "step": 1}, "b@x": {"until": 9e9, "step": 2}},
            fingerprints={"new@x": "mark-new", "b@x": "mark-b"},
            identity=dict(s.get("identity", {}), **{c_id: {"email": "c@x"}}),
            active_email="a@x", depot_email="new@x", depot_expires_at=9000, depot_token_mark="m", depot_synced_at=8000,
            last_tick=5))
        window = {"utilization": 5, "resets_at": "2099-01-01T00:00:00+00:00"}
        data = {"five_hour": window, "seven_day": window}
        cache = {"new@x": {"ts": 1, "data": data}, "b@x": {"ts": 2, "data": data}}
        with open(os.path.join(w.state_dir, "usage-cache.json"), "w") as f:
            json.dump(cache, f)
        rows = [{"ts": 1, "email": "new@x", "session_used": 1, "weekly_used": 1},
                {"ts": 2, "email": "b@x", "session_used": 2, "weekly_used": 2},
                {"ts": 3, "email": "new@x", "session_used": 3, "weekly_used": 3},
                {"ts": 4, "email": "c@x", "session_used": 4, "weekly_used": 4}]
        with open(os.path.join(w.state_dir, "history.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        status = {"generated_at": 7, "active_email": "a@x", "accounts": [
            {"id": "id-b", "email": "b@x"}, {"id": new_id, "email": "new@x"}, {"id": c_id, "email": "c@x"},
            {"id": "profile:p1", "email": "p@x", "source": "profile"}]}
        with open(os.path.join(w.state_dir, "status.json"), "w") as f:
            json.dump(status, f)
        self.edit_state(w, lambda c: c.update(last_resort=["new@x", "b@x"], never=["new@x"]), "config.json")
        before, kc_before = self.tree(w), self.keychain(w)
        state_before = self.state_file(w, "state.json")
        w.forget_keychain_calls()

        r = self.run_remove(w, "new@x", "--json")

        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '{"removed":"new@x"}\n', ""))
        # katalog i wpis w Pęku kluczy tego konta zniknęły, nic więcej nie zostało skasowane ani zapisane
        self.assertEqual(list(self.own_accounts(w)), ["c@x"])
        self.assertFalse(os.path.exists(os.path.join(w.state_dir, "accounts", new_id)))
        self.assertEqual(self.keychain(w), {k: v for k, v in kc_before.items() if k != f"{w.service}|{new_id}"})
        self.assertEqual([c for c in w.keychain_calls() if c[0] != "find-generic-password"],
                         [("delete-generic-password", f"{w.service}|{new_id}")])
        # każdy plik, którego remove nie musiał ruszyć, ma te same bajty
        after = self.tree(w)
        gone = os.path.join(".local/share/claude-acc/accounts", new_id, "auth", "oauth-account.json")
        self.assertEqual(set(before) - set(after), {gone})
        for path in set(after):
            if os.path.basename(path) not in REWRITTEN:
                self.assertEqual(after[path], before[path], path)
        # a w plikach stanu zniknęły wpisy tego konta i tylko one
        expected = dict(state_before)
        expected.update(
            needs_login={"b@x": 200}, needs_login_token={"b@x": "dead-b"},
            api_backoff={"b@x": {"until": 9e9, "step": 2}}, fingerprints={"b@x": "mark-b"},
            identity={k: v for k, v in state_before["identity"].items() if k != new_id})
        for key in ("depot_email", "depot_expires_at", "depot_token_mark", "depot_synced_at"):
            del expected[key]
        self.assertEqual(self.state_file(w, "state.json"), expected)
        self.assertEqual(self.state_file(w, "usage-cache.json"), {"b@x": cache["b@x"]})
        with open(os.path.join(w.state_dir, "history.jsonl")) as f:
            self.assertEqual(f.read(), "".join(json.dumps(rows[i]) + "\n" for i in (1, 3)))
        self.assertEqual(self.state_file(w, "status.json"),
                         dict(status, accounts=[status["accounts"][i] for i in (0, 2, 3)]))
        config = self.state_file(w, "config.json")
        self.assertEqual((config["last_resort"], config["never"]), (["b@x"], []))
        # panel i lista nie znają już konta, a przełączenie na nie nie ma dokąd
        listed = self.snapshot(w)
        self.assertEqual(sorted(listed), ["a@x", "b@x", "c@x"])
        r = w.run("switch", "new@x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nie znam konta new@x", r.stdout)

    def test_a_refusal_changes_nothing(self):
        w = self.world()
        w.runtime(w.account("a@x"))  # konto z Orca, aktywne
        self.own(w, "c@x")
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        self.refusal(self.run_remove(w, "nobody@x", "--json"), "unknown")
        self.refusal(self.run_remove(w, "a@x", "--json"), "not_owned")  # nie nasze: dodała je aplikacja

        self.assert_nothing_changed(w, before, kc_before)

    def test_json_shapes_and_the_second_remove_is_unknown(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()

        first = self.run_remove(w, "c@x", "--json")
        second = self.run_remove(w, "c@x", "--json")

        self.assertEqual((first.returncode, json.loads(first.stdout)), (0, {"removed": "c@x"}))
        sentence = self.refusal(second, "unknown")
        self.assertIn("c@x", sentence)
        self.assertTrue(sentence.endswith("."))

    def test_the_email_is_matched_without_regard_to_case(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()

        r = self.run_remove(w, "C@X", "--json")

        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"removed": "c@x"}))

    def test_a_missing_email_is_a_usage_error_and_removes_nothing(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        for args in ((), ("--json",), ("c@x", "d@x", "--json"), ("--auto", "--json")):
            r = self.run_remove(w, *args)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
            self.assertIn("usage: claude-acc remove <email> [--json]", r.stderr)
        self.assert_nothing_changed(w, before, kc_before)


class StatusTest(RemoveWorld):
    """Co Pod czyta ze `status --json`, zanim pokaże Usuń: czy ta wersja w ogóle umie remove
    (capabilities) i które wiersze da się usunąć (removable), żeby nie pokazywać przycisku, który
    zawsze skończy się odmową not_owned."""

    def test_status_says_remove_is_there_and_which_rows_it_can_remove(self):
        w = self.world(pod=True)
        w.runtime(w.account("a@x"))  # konto z folderu danych Poda, aktywne
        w.account("b@x")  # drugie konto Poda
        self.own(w, "c@x")  # konto claude-acc
        add_profile(w, "p@x")  # profil Poda
        w.write()

        r = w.run("status", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status = json.loads(r.stdout)
        self.assertIn("remove", status["capabilities"])
        removable = {row["email"]: row["removable"] for row in status["accounts"]}
        self.assertEqual(removable, {"a@x": False, "b@x": False, "c@x": True, "p@x": False})
        # to samo, co remove powie po kliknięciu: wiersz z removable false odmawia not_owned
        self.refusal(self.run_remove(w, "b@x", "--json"), "not_owned")


class RefusalsTest(RemoveWorld):
    def test_the_account_sessions_use_now_is_refused_with_a_way_out(self):
        w = self.world()
        w.account("a@x")
        _, blob = self.own(w, "c@x")
        w.runtime(blob)  # oba wpisy runtime trzymają konto c@x
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json")

        sentence = self.refusal(r, "active")
        self.assertIn("switch", sentence)
        self.assert_nothing_changed(w, before, kc_before)

    def test_a_login_in_just_one_live_entry_is_also_active(self):
        # tick robi z logowania w którymkolwiek żywym wpisie konto claude-acc: skasowane wróciłoby w 2 minuty
        w = self.world()
        a = w.account("a@x")
        _, blob = self.own(w, "c@x")
        w.runtime(a, services=[scoped(w.config_dir)])
        w.runtime(blob, services=[BASE])
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json")

        self.refusal(r, "active")
        self.assert_nothing_changed(w, before, kc_before)

    def test_active_account_with_a_session_rotated_token_is_still_found(self):
        # sesja odświeżyła token po ostatniej kopii: wpis runtime nie zgadza się z kopią co do tokenów
        w = self.world()
        w.account("a@x")
        _, blob = self.own(w, "c@x")
        w.runtime(blob)
        w.write()
        w.session_refresh("c@x")
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json")

        self.refusal(r, "active")
        self.assert_nothing_changed(w, before, kc_before)

    def test_a_live_entry_nobody_can_be_matched_to_while_the_api_is_down_blocks_instead_of_guessing(self):
        w = self.world()
        w.account("a@x")
        self.own(w, "c@x")
        self.login_elsewhere(w, "stranger@x", services=(BASE,))
        w.server["profile_down_for"] = ["stranger@x"]
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json")

        sentence = self.refusal(r, "busy")
        self.assertIn("Try again", sentence)
        self.assert_nothing_changed(w, before, kc_before)

    def test_sessions_on_another_config_dir_hold_the_account(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        _, blob = self.own(w, "c@x")
        w.keychain[f"{scoped(w.other_dir)}|{USER}"] = json.dumps(blob)
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        # dwie sesje czytają wpis z logowaniem c@x, jedna wpis bazowy konta aktywnego
        r = self.run_remove(w, "c@x", "--json", sessions=[w.other_dir, w.other_dir, None])

        sentence = self.refusal(r, "in_use")
        self.assertIn("2 Claude Code sessions", sentence)
        self.assert_nothing_changed(w, before, kc_before)

    def test_a_login_in_a_side_entry_without_a_session_on_it_does_not_hold_the_account(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        _, blob = self.own(w, "c@x")
        w.keychain[f"{scoped(w.other_dir)}|{USER}"] = json.dumps(blob)
        w.write()

        r = self.run_remove(w, "c@x", "--json", sessions=[None])

        self.assertEqual(json.loads(r.stdout), {"removed": "c@x"})
        # wpis sesji zostaje: to nie nasze
        self.assertEqual(self.keychain(w)[f"{scoped(w.other_dir)}|{USER}"], json.dumps(blob))

    def test_sessions_of_another_account_do_not_hold_this_one(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        _, other = self.own(w, "d@x")
        w.keychain[f"{scoped(w.other_dir)}|{USER}"] = json.dumps(other)
        w.write()

        r = self.run_remove(w, "c@x", "--json", sessions=[w.other_dir])

        self.assertEqual(json.loads(r.stdout), {"removed": "c@x"})

    def test_one_session_is_named_in_the_singular(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        _, blob = self.own(w, "c@x")
        w.keychain[f"{scoped(w.other_dir)}|{USER}"] = json.dumps(blob)
        w.write()

        r = self.run_remove(w, "c@x", "--json", sessions=[w.other_dir])

        sentence = self.refusal(r, "in_use")
        self.assertIn("1 Claude Code session.", sentence)
        self.assertIn("Close it first", sentence)

    def test_a_running_scheduled_run_holds_the_account_but_a_dead_or_expired_one_does_not(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        held = os.path.join(w.state_dir, "jobs", "held-accounts.json")
        os.makedirs(os.path.dirname(held))

        def hold(*entries):
            with open(held, "w") as f:
                json.dump({"v": 1, "accounts": [dict(job="blog", since=1, **e) for e in entries]}, f)

        later = time.time() + 3600
        hold({"email": "c@x", "pid": os.getpid(), "until": later}, {"email": "C@x", "pid": os.getppid(), "until": later})
        before, kc_before = self.tree(w), self.keychain(w)

        sentence = self.refusal(self.run_remove(w, "c@x", "--json"), "in_use")

        self.assertIn("2 scheduled runs", sentence)
        self.assert_nothing_changed(w, before, kc_before)
        hold({"email": "c@x", "pid": gone.pid, "until": later},  # proces już nie żyje
             {"email": "c@x", "pid": os.getpid(), "until": time.time() - 60},  # wskazówka wygasła
             {"email": "d@x", "pid": os.getpid(), "until": later})  # inne konto
        r = self.run_remove(w, "c@x", "--json")
        self.assertEqual(json.loads(r.stdout), {"removed": "c@x"})

    def test_a_process_on_the_real_process_table_counts(self):
        # prawdziwa tablica procesów (KERN_PROCARGS2), bez podstawki: program o nazwie "claude" z
        # CLAUDE_CONFIG_DIR. Kopia /bin/sleep podpisana na nowo, bo interpreter Pythona sam podmienia argv[0]
        w = self.world()
        w.runtime(w.account("a@x"))
        _, blob = self.own(w, "c@x")
        w.keychain[f"{scoped(w.other_dir)}|{USER}"] = json.dumps(blob)
        w.write()
        program = os.path.join(w.home, "claude")
        shutil.copy("/bin/sleep", program)
        signed = subprocess.run(["codesign", "--force", "--sign", "-", program], capture_output=True)
        if signed.returncode:
            self.skipTest(f"codesign: {signed.stderr.decode().strip()}")
        session = subprocess.Popen([program, "60"], env=dict(os.environ, CLAUDE_CONFIG_DIR=w.other_dir))
        self.addCleanup(session.wait)
        self.addCleanup(session.kill)
        time.sleep(0.5)

        r = w.run("remove", "c@x", "--json")

        self.assertIn("1 Claude Code session", self.refusal(r, "in_use"))
        session.kill()
        session.wait()
        r = w.run("remove", "c@x", "--json")
        self.assertEqual(json.loads(r.stdout), {"removed": "c@x"})

    def test_accounts_of_the_host_app_are_not_ours_to_remove(self):
        w = self.world(pod=True)
        w.runtime(w.account("a@x"))
        w.account("b@x")  # konto z folderu danych Poda
        add_profile(w, "p@x")  # profil Poda
        w.write()
        before, kc_before = self.tree(w), self.keychain(w)

        for email in ("b@x", "p@x"):
            sentence = self.refusal(self.run_remove(w, email, "--json"), "not_owned")
            self.assertIn("Pod", sentence)
        self.assert_nothing_changed(w, before, kc_before)

    def test_a_busy_run_is_reported_not_waited_on_forever(self):
        import fcntl

        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        holder = open(os.path.join(w.state_dir, "lock"), "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.time()

        r = self.run_remove(w, "c@x", "--json")

        self.refusal(r, "busy")
        self.assertLess(time.time() - started, 40)
        self.assertEqual(list(self.own_accounts(w)), ["c@x"])


class FailureTest(RemoveWorld):
    def test_when_the_keychain_item_cannot_be_deleted_nothing_else_is_removed(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        self.edit_state(w, lambda s: s.update(needs_login={"c@x": 1}))
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json", FAKE_FAIL_DELETE_SERVICE=w.service)

        sentence = self.refusal(r, "failed")
        self.assertIn("Keychain", sentence)
        self.assert_nothing_changed(w, before, kc_before)
        self.assertEqual(list(self.own_accounts(w)), ["c@x"])  # dalej na liście, można spróbować jeszcze raz

    def test_a_locked_keychain_is_not_taken_for_a_deleted_item(self):
        # zablokowany Pęk kluczy nie usuwa wpisu i nie daje go odczytać; to nie dowód, że wpisu nie ma
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        self.edit_state(w, lambda s: s.update(needs_login={"c@x": 1}))
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json", FAKE_LOCKED_SERVICE=w.service)

        self.assertIn("Keychain", self.refusal(r, "failed"))
        self.assert_nothing_changed(w, before, kc_before)

    def test_an_interrupted_remove_is_finished_by_the_next_one(self):
        # wpis w Pęku kluczy zniknął, katalog i stan zostały: konto jest już niewidoczne, ale remove je dokończy
        w = self.world()
        w.runtime(w.account("a@x"))
        c_id, _ = self.own(w, "c@x")
        w.write()
        edit_json(os.path.join(w.fake, "keychain.json"), lambda kc: kc.pop(f"{w.service}|{c_id}"))
        self.assertNotIn("c@x", self.snapshot(w))
        self.edit_state(w, lambda s: s.update(needs_login={"c@x": 1, "a@x": 2}))

        r = self.run_remove(w, "c@x", "--json")

        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"removed": "c@x"}))
        self.assertFalse(os.path.exists(os.path.join(w.state_dir, "accounts", c_id)))
        self.assertEqual(self.state_file(w, "state.json")["needs_login"], {"a@x": 2})
        self.refusal(self.run_remove(w, "c@x", "--json"), "unknown")

    def test_a_folder_that_is_a_link_is_left_alone(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        c_id, _ = self.own(w, "c@x")
        w.write()
        folder = os.path.join(w.state_dir, "accounts", c_id)
        elsewhere = os.path.join(w.home, "elsewhere")
        shutil.move(folder, elsewhere)
        os.symlink(elsewhere, folder)
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x", "--json")

        self.assertIn("not a plain folder", self.refusal(r, "failed"))
        self.assert_nothing_changed(w, before, kc_before)
        self.assertTrue(os.path.islink(folder))

    def test_what_another_account_has_in_shared_state_stays(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        self.edit_state(w, lambda s: s.update(depot_email="a@x", depot_expires_at=9000, active_email="a@x"))

        r = self.run_remove(w, "c@x", "--json")

        self.assertEqual(json.loads(r.stdout), {"removed": "c@x"})
        state = self.state_file(w, "state.json")
        self.assertEqual((state["depot_email"], state["depot_expires_at"], state["active_email"]), ("a@x", 9000, "a@x"))


class TerminalTest(RemoveWorld):
    def setup_world(self):
        w = self.world()
        w.runtime(w.account("a@x"))
        self.own(w, "c@x")
        w.write()
        return w

    def test_without_a_terminal_and_without_json_it_refuses_with_a_hint(self):
        w = self.setup_world()
        before, kc_before = self.tree(w), self.keychain(w)

        r = self.run_remove(w, "c@x")

        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertIn("--json", r.stderr)
        self.assertIn("terminal", r.stderr)
        self.assert_nothing_changed(w, before, kc_before)

    def test_on_a_terminal_it_asks_and_removes_only_after_a_yes(self):
        w = self.setup_world()

        code, shown = self.on_terminal(w, ["c@x"], answer="y")

        self.assertEqual(code, 0, shown)
        self.assertIn(PROMPT.format("c@x"), shown)
        self.assertIn("Removed c@x from claude-acc.", shown)
        self.assertEqual(self.own_accounts(w), {})

    def test_on_a_terminal_anything_but_yes_keeps_the_account(self):
        for answer in ("", "n", "no", "nope", "yy"):
            with self.subTest(answer=answer):
                w = self.setup_world()
                before, kc_before = self.tree(w), self.keychain(w)

                code, shown = self.on_terminal(w, ["c@x"], answer=answer)

                self.assertEqual(code, 1, shown)
                self.assertIn("Not removed.", shown)
                self.assert_nothing_changed(w, before, kc_before)

    def test_on_a_terminal_a_refusal_comes_before_the_question(self):
        w = self.setup_world()

        code, shown = self.on_terminal(w, ["nobody@x"])

        self.assertEqual(code, 1, shown)
        self.assertNotIn("[y/N]", shown)
        self.assertIn("claude-acc has no account nobody@x.", shown)
        self.assertEqual(list(self.own_accounts(w)), ["c@x"])


if __name__ == "__main__":
    unittest.main()
