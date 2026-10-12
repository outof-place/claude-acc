"""Hook pauzy limitów uruchamiany tak, jak robi to Claude Code: JSON na stdin,
odpowiedź na stdout, budzik w tle kończący się kodem 2.

Polecenia hooka idą tak, jak wpisuje je instalacja: przez prawdziwą powłokę ze
strażnikiem albo, z natywnym claude-acc-hook, wprost przez ten program (exec form),
z HOME w katalogu tymczasowym, więc nic nie dotyka Twoich sesji. Instalacja
pisze tylko do settings.json w katalogu tymczasowym.

Uruchomienie: /usr/bin/python3 -m unittest discover -s tests
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hook.py")
ACC = os.path.join(os.path.dirname(HERE), "acc.py")

spec = importlib.util.spec_from_file_location("hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

REAL_SETTINGS = os.path.expanduser("~/.claude/settings.json")
# natywne programy z `swift build -c release` w app/; bez nich testy exec form się pomijają
BUILT = os.path.join(os.path.dirname(HERE), "app/.build/release/claude-acc-hook")
BUILT_PAUSE = os.path.join(os.path.dirname(HERE), "app/.build/release/claude-acc-pause")
needs_native = unittest.skipUnless(os.access(BUILT, os.X_OK), "brak app/.build/release/claude-acc-hook")
needs_pause = unittest.skipUnless(os.access(BUILT_PAUSE, os.X_OK), "brak app/.build/release/claude-acc-pause")


def stamp(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class Guarded(unittest.TestCase):
    """Siatka bezpieczeństwa: prawdziwe ustawienia Claude nie mogą się zmienić w teście."""

    # hooki w powłoce (False) albo bez niej przez natywny program: "swift" to claude-acc-hook
    # pause, "c" to claude-acc-pause (oba programy na miejscu, jak po instalacji)
    native = False
    # z acc.py i zarządzanym interpreterem ($STATE/python) w katalogu stanu, jak po setup.sh
    launcher = False

    def setUp(self):
        self.real_settings = stamp(REAL_SETTINGS)
        World.native = self.native
        World.launcher = self.launcher

    def tearDown(self):
        World.native = False
        World.launcher = False
        self.assertEqual(stamp(REAL_SETTINGS), self.real_settings, "test zmienił prawdziwy ~/.claude/settings.json")


class World:
    native = False
    launcher = False

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="claude-acc-hook-")
        self.dir = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.dir)
        shutil.copy(HOOK, os.path.join(self.dir, "hook.py"))
        if self.native:
            shutil.copy(BUILT, os.path.join(self.dir, "claude-acc-hook"))
        if self.native == "c":
            shutil.copy(BUILT_PAUSE, os.path.join(self.dir, "claude-acc-pause"))
        self.transcript = os.path.join(self.home, "transcript.jsonl")
        open(self.transcript, "w").write('{"type":"assistant","message":"robię"}\n')
        if self.launcher:
            shutil.copy(ACC, os.path.join(self.dir, "acc.py"))
            self.link_python()

    def link_python(self):
        """$STATE/python, który linkuje setup.sh: zapisuje swoje argumenty i uruchamia systemowy."""
        python = os.path.join(self.dir, "python")
        with open(python, "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.home}/python-runs"\nexec /usr/bin/python3 "$@"\n')
        os.chmod(python, 0o755)

    def python_runs(self):
        try:
            return open(os.path.join(self.home, "python-runs")).read().splitlines()
        except FileNotFoundError:
            return []

    def pause(self, episode="100", resume_at=None):
        json.dump({"episode": episode, "since": 100, "account": "a@x", "reason": "test",
                   "resume_at": resume_at}, open(os.path.join(self.dir, "pause.json"), "w"))

    def unpause(self):
        os.remove(os.path.join(self.dir, "pause.json"))

    extra_env = {}

    def env(self):
        return {"HOME": self.home, "PATH": "/usr/bin:/bin", "CLAUDE_ACC_HOOK_POLL": "0.1", **self.extra_env}

    def argv(self, event):
        """Wpis z instalacji w tym HOME i to, co Claude Code z nim uruchamia."""
        with mock.patch.object(hook, "NATIVE", os.path.join(self.dir, "claude-acc-hook")), \
                mock.patch.object(hook, "PAUSE_NATIVE", os.path.join(self.dir, "claude-acc-pause")):
            entry = hook.entries()[event]["hooks"][0]
        # z programem na miejscu instalacja daje exec form; budzik po ścianie zostaje w powłoce
        assert ("args" in entry) == bool(self.native and event != "StopFailure"), entry
        if self.native == "c" and event != "StopFailure":
            assert entry["command"].endswith("claude-acc-pause"), entry
        if "args" in entry:
            return [entry["command"], *entry["args"]]
        return ["/bin/sh", "-c", entry["command"]]

    def payload(self, session="s1", agent=None, **extra):
        data = {"session_id": session, "transcript_path": self.transcript, "hook_event_name": "x"}
        if agent:
            data.update(agent_id=agent, agent_type="general-purpose")
        data.update(extra)
        return json.dumps(data)

    def fire(self, event, **payload):
        return subprocess.run(self.argv(event), input=self.payload(**payload),
                              env=self.env(), capture_output=True, text=True, timeout=20)

    def spawn(self, event, **payload):
        p = subprocess.Popen(self.argv(event), env=self.env(), text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.write(self.payload(**payload))
        p.stdin.close()
        return p

    def context(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"].get("additionalContext") if r.stdout.strip() else None


class OutsidePauseTest(Guarded):
    def test_every_hook_is_silent_without_pause(self):
        w = World()
        for event in ("PostToolUse", "PreToolUse", "UserPromptSubmit", "Stop"):
            r = w.fire(event)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), event)

    def test_python_never_starts_without_pause(self):
        # hook odpala się przy każdym narzędziu każdej sesji: poza pauzą ma kosztować
        # tylko test pliku w powłoce, bez startu interpretera (~50 ms na wywołanie)
        w = World()
        started = os.path.join(w.home, "python-started")
        with open(os.path.join(w.dir, "hook.py"), "w") as f:
            f.write(f"open({started!r}, 'w').close()\n")

        for event in ("PostToolUse", "PreToolUse", "UserPromptSubmit", "Stop"):
            w.fire(event)

        self.assertFalse(os.path.exists(started))

    def test_session_with_its_own_pause_file_follows_only_that_file(self):
        # CLAUDE_ACC_PAUSE_FILE: jedna sesja testowa pauzuje bez wstrzymywania pozostałych
        w = World()
        w.pause()
        own = os.path.join(w.home, "own-pause.json")
        w.extra_env = {"CLAUDE_ACC_PAUSE_FILE": own}

        self.assertEqual(w.fire("PostToolUse").stdout, "")

        os.replace(os.path.join(w.dir, "pause.json"), own)
        self.assertIn("TASKS.md", w.context(w.fire("PostToolUse")))

    def test_missing_script_never_blocks_a_session(self):
        # katalog stanu usunięty, a wpisy w settings.json zostały: python3 z brakującym
        # plikiem kończy się kodem 2, czyli blokadą narzędzia albo fałszywą pobudką
        w = World()
        w.pause()
        os.remove(os.path.join(w.dir, "hook.py"))

        for event in ("PostToolUse", "PreToolUse", "UserPromptSubmit", "Stop", "StopFailure"):
            r = w.fire(event)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), event)


class CheckpointTest(Guarded):
    def test_session_and_each_subagent_hear_the_checkpoint_once(self):
        w = World()
        w.pause(resume_at=time.time() + 3600)

        first = w.context(w.fire("PostToolUse"))
        again = w.fire("PostToolUse")
        sub = w.context(w.fire("PostToolUse", agent="ag1"))
        sub_again = w.fire("PostToolUse", agent="ag1")
        other = w.context(w.fire("PostToolUse", session="s2"))

        self.assertIn("TASKS.md", first)
        self.assertIn("Zapas wróci ok.", first)
        self.assertEqual(again.stdout, "")
        self.assertIn("SendMessage", sub)
        self.assertNotIn("TASKS.md", sub)  # subagent wraca z raportem, stan zapisuje rodzic
        self.assertEqual(sub_again.stdout, "")
        self.assertIn("TASKS.md", other)

    def test_new_subagents_are_held_until_you_write_in_that_session(self):
        w = World()
        w.pause()

        denied = json.loads(w.fire("PreToolUse").stdout)["hookSpecificOutput"]
        w.fire("PostToolUse")
        note = w.fire("UserPromptSubmit")
        after = w.fire("PreToolUse")
        post_after = w.fire("PostToolUse", agent="ag9")

        self.assertEqual(denied["hookEventName"], "PreToolUse")
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("Pauza limitów", denied["permissionDecisionReason"])
        self.assertIn("pracuj normalnie", note.stdout)
        self.assertEqual(after.stdout, "")
        self.assertEqual(post_after.stdout, "")  # subagenci tej sesji też nie dostają pauzy

    def test_subagent_report_is_not_mistaken_for_your_message(self):
        # Claude Code przepuszcza powiadomienie o skończonym subagencie przez
        # UserPromptSubmit (sprawdzone na żywo w 2.1.286): to nie Ty piszesz
        w = World()
        w.pause()
        w.fire("PostToolUse", agent="ag1")

        for prompt in ("<task-notification>\n<task-id>ag1</task-id>",
                       "Your claude.ai usage limit has reset. Continue the task you were working on"):
            w.fire("UserPromptSubmit", prompt=prompt)

        denied = w.fire("PreToolUse").stdout
        self.assertIn("deny", denied)

    def test_new_episode_checkpoints_again(self):
        w = World()
        w.pause(episode="1")
        w.fire("PostToolUse")
        w.unpause()
        w.pause(episode="2")

        self.assertIsNotNone(w.context(w.fire("PostToolUse")))


def wake_timeout(base=10.0):
    """Czas na budzik: budzik to proces w tle, który pod obciążeniem (load average ponad liczbę rdzeni,
    np. 60-240 przy kilkunastu agentach) dostaje CPU z opóźnieniem. Na spokojnym Macu 10 s jak dotąd,
    pod obciążeniem proporcjonalnie dłużej, najwyżej 90 s; sprawdzamy, czy się obudzi, nie jak szybko."""
    cores = os.cpu_count() or 1
    return min(90.0, base * max(1.0, os.getloadavg()[0] / cores))


class WakeTest(Guarded):
    def stopped_session(self, w, session="s1"):
        w.pause()
        w.fire("PostToolUse", session=session)  # sesja dostała polecenie i kończy turę
        return w.spawn("Stop", session=session)

    def test_stopped_session_wakes_when_pause_ends(self):
        w = World()
        p = self.stopped_session(w)
        time.sleep(0.6)
        self.assertIsNone(p.poll())  # czeka, nie budzi w trakcie pauzy

        w.unpause()
        code = p.wait(timeout=wake_timeout())

        self.assertEqual(code, 2)
        self.assertIn("Limity wróciły", p.stderr.read())

    def test_session_resumed_by_you_is_not_woken_again(self):
        w = World()
        p = self.stopped_session(w)
        time.sleep(0.3)
        with open(w.transcript, "a") as f:
            f.write('{"type":"user","message":"lecimy dalej"}\n')

        code = p.wait(timeout=wake_timeout())

        self.assertEqual((code, p.stderr.read()), (0, ""))

    def test_one_alarm_per_session(self):
        w = World()
        first = self.stopped_session(w)
        time.sleep(0.3)

        second = w.spawn("Stop")
        self.assertEqual(second.wait(timeout=wake_timeout()), 0)
        w.unpause()
        self.assertEqual(first.wait(timeout=wake_timeout()), 2)

    def test_parent_waiting_on_a_stopped_subagent_gets_an_alarm(self):
        # rodzic tylko czekał na subagenta, więc polecenie dostał sam subagent;
        # bez budzika rodzic po jego raporcie stanąłby na dobre
        w = World()
        w.pause()
        w.fire("PostToolUse", agent="ag1")
        p = w.spawn("Stop")
        time.sleep(0.4)
        self.assertIsNone(p.poll())

        w.unpause()

        self.assertEqual(p.wait(timeout=wake_timeout()), 2)

    def test_session_that_was_idle_before_pause_gets_no_alarm(self):
        w = World()
        w.pause()

        r = w.fire("Stop", session="idle")

        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_wall_hit_wakes_after_switch_to_another_account(self):
        w = World()
        json.dump({"switched_at": 100}, open(os.path.join(w.dir, "state.json"), "w"))
        p = w.spawn("StopFailure")
        # przełączenie dopiero po starcie budzika: wolny start (load 40) czytał już nowy
        # switched_at jako punkt wyjścia i czekał w nieskończoność
        lock = os.path.join(w.dir, "pause-watchers", "s1.lock")
        deadline = time.time() + wake_timeout()
        while not os.path.exists(lock) and time.time() < deadline and p.poll() is None:
            time.sleep(0.05)
        time.sleep(0.4)
        self.assertIsNone(p.poll())

        json.dump({"switched_at": int(time.time())}, open(os.path.join(w.dir, "state.json"), "w"))
        code = p.wait(timeout=wake_timeout())

        self.assertEqual(code, 2)
        self.assertIn("innym koncie", p.stderr.read())


@needs_native
class NativeOutsidePauseTest(OutsidePauseTest):
    native = "swift"


@needs_native
class NativeCheckpointTest(CheckpointTest):
    native = "swift"


@needs_native
class NativeWakeTest(WakeTest):
    native = "swift"


@needs_native
@needs_pause
class CPauseOutsidePauseTest(OutsidePauseTest):
    native = "c"


@needs_native
@needs_pause
class CPauseCheckpointTest(CheckpointTest):
    native = "c"


@needs_native
@needs_pause
class CPauseWakeTest(WakeTest):
    native = "c"


@needs_native
@needs_pause
class LauncherOutsidePauseTest(OutsidePauseTest):
    native = "c"
    launcher = True


@needs_native
@needs_pause
class LauncherCheckpointTest(CheckpointTest):
    native = "c"
    launcher = True


@needs_native
@needs_pause
class LauncherWakeTest(WakeTest):
    native = "c"
    launcher = True


@needs_native
@needs_pause
class LauncherTest(Guarded):
    """W pauzie oba natywne fronty uruchamiają hook.py przez acc.py na zarządzanym
    interpreterze (20 ms na wywołanie zamiast 30), a bez nich przez /usr/bin/python3."""

    FRONTS = ("swift", "c")

    def world(self, native):
        World.native = native
        w = World()
        w.pause()
        return w

    def test_pause_runs_hook_through_acc_py_on_the_managed_interpreter(self):
        for native in self.FRONTS:
            with self.subTest(native=native):
                w = self.world(native)
                shutil.copy(ACC, os.path.join(w.dir, "acc.py"))
                w.link_python()

                self.assertIn("TASKS.md", w.context(w.fire("PostToolUse")))
                self.assertEqual(w.python_runs(), [f"{w.dir}/acc.py hook post"])

    def test_without_acc_py_hook_runs_on_the_system_interpreter(self):
        for native in self.FRONTS:
            with self.subTest(native=native):
                w = self.world(native)
                w.link_python()

                self.assertIn("TASKS.md", w.context(w.fire("PostToolUse")))
                self.assertEqual(w.python_runs(), [])

    def test_interpreter_that_does_not_run_is_skipped(self):
        for native in self.FRONTS:
            with self.subTest(native=native):
                w = self.world(native)
                shutil.copy(ACC, os.path.join(w.dir, "acc.py"))
                w.link_python()
                os.chmod(os.path.join(w.dir, "python"), 0o644)

                self.assertIn("TASKS.md", w.context(w.fire("PostToolUse")))
                self.assertEqual(w.python_runs(), [])


@needs_native
class HandOverTest(Guarded):
    """claude-acc-hook oddaje komendę `devguard.py admit` z tymi samymi bajtami na stdin:
    przez acc.py, gdy leży w katalogu stanu, a bez niego wprost."""

    native = "swift"

    def hand_over(self, w):
        # Python udaje skrypt: zapisuje argumenty i stdin; bez hook-words.json front oddaje wszystko
        with open(os.path.join(w.dir, "python"), "w") as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$*" > "{w.home}/argv"\ncat > "{w.home}/stdin"\n')
        os.chmod(os.path.join(w.dir, "python"), 0o755)
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "npm run dev"}})
        r = subprocess.run([os.path.join(w.dir, "claude-acc-hook")], input=event, env=w.env(),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(os.path.join(w.home, "stdin")).read(), event)
        return open(os.path.join(w.home, "argv")).read().strip()

    def test_hand_over_goes_through_acc_py(self):
        w = World()
        shutil.copy(ACC, os.path.join(w.dir, "acc.py"))

        self.assertEqual(self.hand_over(w), f"{w.dir}/acc.py devguard admit")

    def test_hand_over_without_acc_py_runs_devguard_directly(self):
        w = World()

        self.assertEqual(self.hand_over(w), f"{w.dir}/devguard.py admit")


ORIGINAL = {
    "statusLine": {"type": "command", "command": "orca-statusline"},
    "env": {"NODE_COMPILE_CACHE": "/x/cache"},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk-rewrite.sh"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "orca-hook.sh", "timeout": 10}]}],
    },
}

EVENTS = ("PostToolUse", "PreToolUse", "UserPromptSubmit", "Stop", "StopFailure")


class InstallTest(Guarded):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="claude-acc-settings-")
        self.path = os.path.join(self.dir, "settings.json")
        self.limit_pause(True)

    def limit_pause(self, on):
        """`limit_pause` w config.json tego HOME, tak jak zapisuje go `claude-acc pause on|off`."""
        state = os.path.join(self.dir, ".local/share/claude-acc")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "config.json"), "w") as f:
            json.dump({"limit_pause": on}, f)

    def write(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def run_hook(self, *args):
        return subprocess.run(["/usr/bin/python3", HOOK, *args, self.path], capture_output=True, text=True,
                              env={"HOME": self.dir, "PATH": "/usr/bin:/bin"})

    def read(self):
        return json.load(open(self.path))

    def ours(self, groups):
        return [g for g in groups if any(hook.ours(h) for h in g["hooks"])]

    def test_native_hooks_replace_the_shell_ones_and_go_without_a_trace(self):
        """Instalacja po zainstalowaniu natywnego programu: te same zdarzenia, tylko bez powłoki."""
        self.write(ORIGINAL)
        before = open(self.path).read()
        self.run_hook("install")
        native = os.path.join(self.dir, ".local/share/claude-acc/claude-acc-hook")
        os.makedirs(os.path.dirname(native), exist_ok=True)
        with open(native, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(native, 0o755)

        r = self.run_hook("install")

        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.read()
        for event, mode in (("PostToolUse", "post"), ("PreToolUse", "agent"),
                            ("UserPromptSubmit", "prompt"), ("Stop", "watch")):
            [group] = self.ours(data["hooks"][event])
            [entry] = group["hooks"]
            self.assertEqual((entry["command"], entry["args"]), (native, ["pause", mode]), event)
        [wall] = self.ours(data["hooks"]["StopFailure"])
        self.assertNotIn("args", wall["hooks"][0])
        self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "rtk-rewrite.sh")

        # potem program w C: zastępuje wpisy claude-acc-hook pause, po jednym na zdarzenie
        pause = os.path.join(os.path.dirname(native), "claude-acc-pause")
        shutil.copy(native, pause)
        self.assertEqual(self.run_hook("install").returncode, 0)
        data = self.read()
        for event, mode in (("PostToolUse", "post"), ("PreToolUse", "agent"),
                            ("UserPromptSubmit", "prompt"), ("Stop", "watch")):
            [group] = self.ours(data["hooks"][event])
            [entry] = group["hooks"]
            self.assertEqual((entry["command"], entry["args"]), (pause, [mode]), event)
        self.run_hook("uninstall")
        self.assertEqual(open(self.path).read(), before)

    def test_without_the_pause_only_the_wall_alarm_is_installed(self):
        # pauza jest opcjonalna i domyślnie wyłączona: config bez klucza to brak pauzy
        os.remove(os.path.join(self.dir, ".local/share/claude-acc/config.json"))
        self.write(ORIGINAL)
        before = open(self.path).read()

        r = self.run_hook("install")

        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.read()
        self.assertEqual([e for e in data["hooks"] if self.ours(data["hooks"][e])], ["StopFailure"])
        [wall] = self.ours(data["hooks"]["StopFailure"])
        self.assertEqual(wall["matcher"], "rate_limit")
        self.assertIn("watch-wall", wall["hooks"][0]["command"])
        self.run_hook("uninstall")
        self.assertEqual(open(self.path).read(), before)

    def test_turning_the_pause_off_drops_its_hooks_and_keeps_the_wall_alarm(self):
        self.write(ORIGINAL)
        self.run_hook("install")
        data = self.read()
        self.assertEqual(sorted(e for e in data["hooks"] if self.ours(data["hooks"][e])), sorted(EVENTS))

        self.limit_pause(False)
        r = self.run_hook("install")

        self.assertEqual(r.returncode, 0, r.stderr)
        data = self.read()
        self.assertEqual([e for e in data["hooks"] if self.ours(data["hooks"][e])], ["StopFailure"])
        self.assertEqual(data["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "rtk-rewrite.sh")
        self.limit_pause(True)
        self.run_hook("install")
        data = self.read()
        self.assertEqual(sorted(e for e in data["hooks"] if self.ours(data["hooks"][e])), sorted(EVENTS))

    def test_only_the_pause_entries_of_the_native_program_are_ours(self):
        native = "/u/.local/share/claude-acc/claude-acc-hook"
        self.assertTrue(hook.ours({"command": native, "args": ["pause", "post"]}))
        self.assertFalse(hook.ours({"command": native, "args": []}))  # hook strażnika z Ultry
        self.assertFalse(hook.ours({"command": native}))
        self.assertFalse(hook.ours({"command": "/x/other-hook", "args": ["pause", "post"]}))
        self.assertTrue(hook.ours({"command": "/u/.local/share/claude-acc/claude-acc-pause", "args": ["post"]}))

    def test_install_keeps_other_settings_and_uninstall_restores_the_file(self):
        self.write(ORIGINAL)
        before = open(self.path).read()

        self.run_hook("install")
        installed = self.read()
        self.run_hook("uninstall")

        for event in EVENTS:
            self.assertEqual(len(self.ours(installed["hooks"][event])), 1, event)
        self.assertEqual(installed["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "rtk-rewrite.sh")
        self.assertEqual(installed["statusLine"], ORIGINAL["statusLine"])
        self.assertEqual(installed["env"], ORIGINAL["env"])
        self.assertEqual(open(self.path).read(), before)  # bajt w bajt, z końcem linii

    def test_second_install_changes_nothing(self):
        # setup.sh woła install przy każdej aktualizacji: zapis bez zmian budził
        # obserwatora plików Claude Code i rozjeżdżał zapisy perf.py
        self.write(ORIGINAL)
        self.run_hook("install")
        first = (open(self.path).read(), stamp(self.path))
        time.sleep(0.01)

        r = self.run_hook("install")

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((open(self.path).read(), stamp(self.path)), first)

    def test_alarms_outlive_the_longest_pause_after_an_update(self):
        # Claude Code ubija hook z asyncRewake po jego timeout, bez niego po 600 s:
        # budziki ze starej instalacji umierały, zanim kończyła się pauza dłuższa niż 10 min
        old = json.loads(json.dumps(ORIGINAL))
        with mock.patch.object(hook, "NATIVE", os.path.join(self.dir, "brak")), \
                mock.patch.object(hook, "PAUSE_NATIVE", os.path.join(self.dir, "brak")):
            groups = hook.entries()
        for event, group in groups.items():
            group = json.loads(json.dumps(group))
            for h in group["hooks"]:
                if h.get("asyncRewake"):
                    h.pop("timeout", None)
            old["hooks"].setdefault(event, []).append(group)
        self.write(old)

        r = self.run_hook("install")

        self.assertEqual(r.returncode, 0, r.stderr)
        for event in ("Stop", "StopFailure"):
            [group] = self.ours(self.read()["hooks"][event])
            [alarm] = group["hooks"]
            self.assertTrue(alarm.get("asyncRewake"))
            self.assertGreater(alarm.get("timeout", 600), hook.MAX_WAIT, event)

    def test_backup_keeps_settings_from_before_the_first_install(self):
        self.write(ORIGINAL)
        before = open(self.path).read()
        self.run_hook("install")
        changed = self.read()
        changed["model"] = "opus"
        self.write(changed)

        self.run_hook("install")
        self.run_hook("uninstall")

        self.assertEqual(open(self.path + ".bak-claude-acc").read(), before)

    def test_uninstall_without_settings_creates_nothing(self):
        r = self.run_hook("uninstall")

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.path))

    def test_install_creates_settings_when_there_are_none(self):
        # Claude Code jeszcze nigdy nie zapisał ustawień: nie ma nawet ~/.claude
        self.path = os.path.join(self.dir, ".claude", "settings.json")

        r = self.run_hook("install")

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(self.read()["hooks"]), sorted(EVENTS))

    def test_uninstall_keeps_a_foreign_hook_sharing_our_group(self):
        # ktoś dopisał swój hook do naszej grupy: zdejmujemy tylko nasz wpis
        self.write(ORIGINAL)
        self.run_hook("install")
        data = self.read()
        group = self.ours(data["hooks"]["PostToolUse"])[0]
        group["hooks"].append({"type": "command", "command": "my-logger.sh"})
        self.write(data)

        self.run_hook("uninstall")

        post = self.read()["hooks"]["PostToolUse"]
        self.assertEqual([h["command"] for g in post for h in g["hooks"]], ["my-logger.sh"])

    def test_broken_settings_are_left_alone(self):
        with open(self.path, "w") as f:
            f.write('{"hooks": {')

        r = self.run_hook("install")

        self.assertEqual(r.returncode, 1)
        self.assertEqual(open(self.path).read(), '{"hooks": {')


if __name__ == "__main__":
    unittest.main()
