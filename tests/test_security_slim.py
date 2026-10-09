"""Testy poprawki Ultry `security-slim` (perf.py SecuritySlim i hooks/sec_slim.py).

Wszystko w katalogu tymczasowym: ~/.claude (settings.json, installed_plugins.json, wtyczka
security-guidance z wymyślonym patterns.py) i marketplace claude-acc. `claude plugin` gra atrapa,
która zmienia pliki tak jak prawdziwe CLI; jeden test woła prawdziwe CLI w osobnym HOME. Test
zgodności porównuje odpowiedzi z prawdziwą, zainstalowaną security-guidance i bez niej się pomija.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_security_slim
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import janitor  # noqa: E402
import perf  # noqa: E402

OFFICIAL = perf.OFFICIAL_SECURITY
ID = perf.SecuritySlim.ID

FAKE_PATTERNS = '''
SECURITY_PATTERNS = [
    {"ruleName": "github_actions_workflow", "reminder": "Workflow files run with repo secrets.",
     "path_check": lambda p: ".github/workflows/" in p and p.endswith((".yml", ".yaml"))},
    {"ruleName": "eval_injection", "reminder": "eval() runs arbitrary code.",
     "substrings": ["eval("], "path_filter": lambda p: not p.endswith((".md", ".py"))},
    {"ruleName": "pickle_deserialization", "reminder": "pickle runs code on load.",
     "regex": r"pickle\\.loads?\\("},
]
_RULE_NAME_TO_ID = {"github_actions_workflow": 1, "eval_injection": 4, "pickle_deserialization": 8}


def rule_names_to_mask(names):
    mask = 0
    for name in names:
        mask |= 1 << _RULE_NAME_TO_ID.get(name, 0)
    return mask
'''


class FakeClaude:
    """`claude plugin` jak prawdziwe CLI: settings.json, installed_plugins.json, cache; puste
    słowniki zostają po usunięciu, tak jak zostawia je Claude Code 2.1.294."""

    def __init__(self, home, fail=None):
        self.home, self.fail, self.calls = home, fail, []

    def edit(self, rel, change):
        path = os.path.join(self.home, rel)
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        change(data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")

    def claude_plugin(self, *args):
        self.calls.append(args)
        if self.fail and args[0] == self.fail:
            return 1, "Error: boom"
        if args[:2] == ("marketplace", "add"):
            self.edit("settings.json", lambda d: d.setdefault("extraKnownMarketplaces", {}).update(
                {"claude-acc": {"source": {"source": "directory", "path": args[2]}}}))
        elif args[:2] == ("marketplace", "remove"):
            self.edit("settings.json", lambda d: d.setdefault("extraKnownMarketplaces", {}).pop(args[2], None))
        elif args[0] == "install":
            cache = os.path.join(self.home, "plugins/cache/claude-acc/security-slim/1.0.0")
            os.makedirs(cache, exist_ok=True)
            self.edit("plugins/installed_plugins.json", lambda d: d.setdefault("plugins", {}).update(
                {args[1]: [{"scope": "user", "installPath": cache, "version": "1.0.0"}]}))
            self.edit("settings.json", lambda d: d.setdefault("enabledPlugins", {}).update({args[1]: True}))
        elif args[0] == "uninstall":
            self.edit("plugins/installed_plugins.json", lambda d: d.setdefault("plugins", {}).pop(args[1], None))
            self.edit("settings.json", lambda d: d.setdefault("enabledPlugins", {}).pop(args[1], None))
        return 0, "ok"


class SlimCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="security-slim-test-")
        self.home = os.path.join(self.dir, ".claude")
        self.settings = os.path.join(self.home, "settings.json")
        self.out = os.path.join(self.dir, "state", "plugins")
        self.item = perf.SecuritySlim()
        self.item.path, self.item.claude_dir, self.item.out_dir = self.settings, self.home, self.out
        self.item.python = sys.executable
        self.real = self.stamp(os.path.expanduser("~/.claude/settings.json"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def tearDown(self):
        self.assertEqual(self.stamp(os.path.expanduser("~/.claude/settings.json")), self.real,
                         "test zmienił prawdziwy ~/.claude/settings.json")

    @staticmethod
    def stamp(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def official(self, version="9.8.7", patterns=FAKE_PATTERNS, scope="user", extra=()):
        """Zainstalowana security-guidance w cache Claude Code i jej wpis w installed_plugins.json."""
        root = os.path.join(self.home, "plugins/cache/claude-plugins-official/security-guidance", version)
        os.makedirs(os.path.join(root, "hooks"), exist_ok=True)
        with open(os.path.join(root, "hooks/patterns.py"), "w") as f:
            f.write(patterns)
        with open(os.path.join(root, "hooks/_base.py"), "w") as f:
            f.write('PROVENANCE_TAG = "[from security-guidance@claude-code-plugins plugin]"\n')
        entries = [{"scope": scope, "installPath": root, "version": version}] + list(extra)
        path = os.path.join(self.home, "plugins/installed_plugins.json")
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {"version": 2, "plugins": {}}
        data["plugins"][OFFICIAL] = entries
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return root

    def write_settings(self, data):
        os.makedirs(self.home, exist_ok=True)
        text = json.dumps(data, indent=2) + "\n"
        with open(self.settings, "w") as f:
            f.write(text)
        return text

    def read_settings(self):
        with open(self.settings) as f:
            return f.read()

    def hook(self, event):
        """Uruchamia wygenerowany hook tak jak Claude Code (exec z hooks.json): (stdout, kod)."""
        with open(os.path.join(self.out, "security-slim/hooks/hooks.json")) as f:
            spec = json.load(f)["hooks"]["PostToolUse"][0]["hooks"][0]
        state = os.path.join(self.dir, "sg-state")
        env = dict(os.environ, SECURITY_WARNINGS_STATE_DIR=state, CLAUDE_PROJECT_DIR=self.dir)
        done = subprocess.run([spec["command"], *spec["args"]], input=json.dumps(event).encode(),
                              capture_output=True, env=env, cwd=self.dir, timeout=30)
        return done.stdout.decode(), done.returncode


def edit_event(path, new, sid="s1"):
    return {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": path, "old_string": "x", "new_string": new}}


class ApplyUndoTest(SlimCase):
    def test_replaces_enabled_official_and_undo_restores_bytes(self):
        source = self.official()
        before = self.write_settings({"enabledPlugins": {OFFICIAL: True, "other@x": True}, "theme": "dark"})
        claude = FakeClaude(self.home)

        record, changed = self.item.apply({}, claude, None)

        settings = json.loads(self.read_settings())
        self.assertIs(settings["enabledPlugins"][OFFICIAL], False)
        self.assertIs(settings["enabledPlugins"][ID], True)
        self.assertIs(settings["enabledPlugins"]["other@x"], True)
        self.assertEqual(settings["extraKnownMarketplaces"]["claude-acc"]["source"]["path"], self.out)
        hooks = os.path.join(self.out, "security-slim/hooks")
        with open(os.path.join(source, "hooks/patterns.py")) as a, open(os.path.join(hooks, "patterns.py")) as b:
            self.assertEqual(a.read(), b.read())
        with open(os.path.join(hooks, "source.py")) as f:
            self.assertIn("PV = 90807", f.read())
        self.assertIn(("install", ID, "--scope", "user"), claude.calls)
        self.assertTrue(any("9.8.7" in c for c in changed), changed)
        self.assertEqual(record["source"]["version"], "9.8.7")

        out, code = self.hook(edit_event(os.path.join(self.dir, "a.ts"), "const r = eval(input)"))
        self.assertEqual(code, 0)
        reply = json.loads(out)
        self.assertEqual(reply["hookSpecificOutput"]["additionalContext"],
                         "[from security-guidance@claude-code-plugins plugin]\n\neval() runs arbitrary code.")
        self.assertEqual(reply["metrics"], {"pattern_hits": 1, "rule_id": 4, "rule_mask": 16, "pv": 90807})

        self.item.undo(record, claude)
        self.assertEqual(self.read_settings(), before)
        self.assertFalse(os.path.exists(self.out))
        self.assertIn(("marketplace", "remove", "claude-acc"), claude.calls)

    def test_second_apply_changes_nothing(self):
        self.official()
        self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        claude = FakeClaude(self.home)
        record, _ = self.item.apply({}, claude, None)
        calls = len(claude.calls)
        record, changed = self.item.apply({}, claude, record)
        self.assertEqual(changed, [])
        self.assertEqual(len(claude.calls), calls)

    def test_adopts_hand_made_predecessor(self):
        """Ręczna sec-patterns@filip-local przy wyłączonej oficjalnej: zostaje jedna wtyczka, nasza."""
        self.official()
        before = self.write_settings({"enabledPlugins": {OFFICIAL: False, "sec-patterns@filip-local": True}})
        claude = FakeClaude(self.home)

        record, changed = self.item.apply({}, claude, None)

        plugins = json.loads(self.read_settings())["enabledPlugins"]
        self.assertEqual([k for k, v in plugins.items() if v is True], [ID])
        self.assertIn("sec-patterns@filip-local wyłączona", changed)
        self.item.undo(record, claude)
        self.assertEqual(self.read_settings(), before)

    def test_official_turned_off_by_user_is_left_alone(self):
        self.official()
        before = self.write_settings({"enabledPlugins": {OFFICIAL: False}})
        claude = FakeClaude(self.home)
        record, changed = self.item.apply({}, claude, None)
        self.assertEqual((record, changed, claude.calls), ({}, [], []))
        self.assertEqual(self.read_settings(), before)
        self.assertIn("nie ma czego", self.item.describe(record, claude))

    def test_without_official_does_nothing(self):
        self.write_settings({"enabledPlugins": {}})
        claude = FakeClaude(self.home)
        self.assertEqual(self.item.apply({}, claude, None), ({}, []))
        self.assertEqual(claude.calls, [])

    def test_keep_regenerates_after_official_update(self):
        self.official("9.8.7")
        self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        claude = FakeClaude(self.home)
        record, _ = self.item.apply({}, claude, None)
        calls = len(claude.calls)

        newer = FAKE_PATTERNS.replace("eval() runs arbitrary code.", "eval() is code injection.")
        self.official("9.9.0", newer)
        record, changed = self.item.apply({}, claude, record)

        self.assertEqual(len(claude.calls), calls, "aktualizacja nie potrzebuje claude plugin")
        self.assertTrue(any("9.9.0" in c for c in changed), changed)
        self.assertEqual(record["source"]["version"], "9.9.0")
        out, _ = self.hook(edit_event(os.path.join(self.dir, "b.ts"), "eval(x)", sid="s2"))
        self.assertIn("eval() is code injection.", out)
        self.assertIn('"pv": 90900', out)

    def test_status_names_project_settings_that_reenable_official(self):
        project = os.path.join(self.dir, "proj")
        os.makedirs(os.path.join(project, ".claude"))
        with open(os.path.join(project, ".claude/settings.local.json"), "w") as f:
            json.dump({"enabledPlugins": {OFFICIAL: True}}, f)
        gone = {"scope": "local", "projectPath": os.path.join(self.dir, "removed-worktree"), "installPath": "x"}
        here = {"scope": "local", "projectPath": project, "installPath": "x"}
        self.official(extra=[here, gone])
        self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        claude = FakeClaude(self.home)
        record, _ = self.item.apply({}, claude, None)
        text = self.item.describe(record, claude)
        self.assertIn("włączają ją z powrotem", text)
        self.assertIn("proj/.claude/settings.local.json", text)
        self.assertNotIn("removed-worktree", text)

    def test_failed_install_leaves_nothing_behind(self):
        self.official()
        before = self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        claude = FakeClaude(self.home, fail="install")
        with self.assertRaises(RuntimeError):
            self.item.apply({}, claude, None)
        self.assertEqual(self.read_settings(), before)
        self.assertFalse(os.path.exists(self.out))

    def test_broken_interpreter_is_refused(self):
        self.official(patterns="this is not python(\n")
        before = self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        claude = FakeClaude(self.home)
        with self.assertRaises(RuntimeError) as err:
            self.item.apply({}, claude, None)
        self.assertIn("nie startuje", str(err.exception))
        self.assertEqual(self.read_settings(), before)
        self.assertEqual(claude.calls, [], "bez działającego hooka nic nie rejestrujemy")


class HookTest(SlimCase):
    def setUp(self):
        super().setUp()
        self.official()
        self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        self.item.apply({}, FakeClaude(self.home), None)

    def test_warns_once_per_session_file_and_rule(self):
        path = os.path.join(self.dir, "c.ts")
        first, _ = self.hook(edit_event(path, "eval(a)"))
        again, _ = self.hook(edit_event(path, "eval(b)"))
        self.assertIn("additionalContext", first)
        self.assertEqual(json.loads(again), {"metrics": {"pattern_hits": 0, "rule_id": 4, "rule_mask": 16, "pv": 90807}})

    def test_quiet_cases(self):
        for event in (
            edit_event(os.path.join(self.dir, "x.ts"), "const b = 1"),
            edit_event(os.path.join(self.dir, "README.md"), "never eval(x)"),
            {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": "eval x"}},
        ):
            self.assertEqual(self.hook(event), ("", 0), event)

    def test_malformed_stdin(self):
        with open(os.path.join(self.out, "security-slim/hooks/hooks.json")) as f:
            spec = json.load(f)["hooks"]["PostToolUse"][0]["hooks"][0]
        done = subprocess.run([spec["command"], *spec["args"]], input=b"{not json", capture_output=True)
        self.assertEqual(json.loads(done.stdout), {"metrics": {"pv": 90807, "skipped": True, "skip_reason": -2}})


@unittest.skipUnless(shutil.which("claude"), "claude CLI nie jest zainstalowany")
class RealCliTest(SlimCase):
    """Prawdziwe `claude plugin` w osobnym HOME: rejestracja, instalacja i dokładne cofnięcie."""

    def test_real_cli_apply_and_undo(self):
        self.official()
        before = self.write_settings({"enabledPlugins": {OFFICIAL: True}})
        env = dict(janitor.ENV, HOME=self.dir, CLAUDE_CONFIG_DIR=self.home)
        env.pop("CLAUDE_ACC_CLAUDE_BIN", None)
        with mock.patch.object(janitor, "ENV", env):
            system = perf.System()
            record, _ = self.item.apply({}, system, None)
            settings = json.loads(self.read_settings())
            self.assertIs(settings["enabledPlugins"][ID], True)
            self.assertIs(settings["enabledPlugins"][OFFICIAL], False)
            with open(os.path.join(self.home, "plugins/installed_plugins.json")) as f:
                installed = json.load(f)["plugins"]
            self.assertEqual([e["scope"] for e in installed[ID]], ["user"])
            self.item.undo(record, system)
        self.assertEqual(self.read_settings(), before)
        with open(os.path.join(self.home, "plugins/installed_plugins.json")) as f:
            self.assertNotIn(ID, json.load(f)["plugins"])


def installed_official():
    """Prawdziwa security-guidance tego Maca (zakres użytkownika) albo None."""
    item = perf.SecuritySlim()
    item.claude_dir = os.path.expanduser("~/.claude")
    return item.source()


@unittest.skipUnless(installed_official(), "security-guidance nie jest zainstalowana")
class ParityTest(SlimCase):
    """Te same zdarzenia PostToolUse do security-guidance i do wygenerowanego hooka: ten sam
    JSON i kod wyjścia (17 przypadków z pomiarów 2026-10-09)."""

    def test_same_answers_as_installed_security_guidance(self):
        src = installed_official()
        self.write_settings({})
        self.item.generate(src, sys.executable)
        repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(repo, "src"))
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        with open(os.path.join(repo, "src/legacy.ts"), "w") as f:
            f.write("export const run = (s: string) => eval(s);\n")
        with open(os.path.join(repo, "src/clean.ts"), "w") as f:
            f.write("export const a = 1;\n")
        git("add", ".")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        P = repo + "/src/"
        plans = os.path.expanduser("~/.claude/plans/p.ts")

        def ev(sid, tool, tool_input, event="PostToolUse"):
            d = {"session_id": sid, "transcript_path": self.dir + "/t.jsonl", "cwd": repo,
                 "permission_mode": "bypassPermissions", "hook_event_name": event}
            if tool:
                d.update(tool_name=tool, tool_input=tool_input, tool_response={}, tool_use_id="toolu_parity")
            return json.dumps(d).encode()

        cases = [
            ("Edit .ts eval(", [ev("s1", "Edit", {"file_path": P + "a.ts", "old_string": "x", "new_string": "const r = eval(input)"})]),
            ("Edit .tsx exec + dangerouslySetInnerHTML", [ev("s2", "Edit", {"file_path": P + "b.tsx", "old_string": "x", "new_string": "child_process.exec(cmd); <div dangerouslySetInnerHTML={{__html: h}} />"})]),
            ("Write GitHub workflow", [ev("s3", "Write", {"file_path": repo + "/.github/workflows/ci.yml", "content": "on: push\n"})]),
            ("Write .py pickle.load", [ev("s4", "Write", {"file_path": P + "load.py", "content": "import pickle\nobj = pickle.load(open('f','rb'))\n"})]),
            ("MultiEdit .py yaml.load", [ev("s5", "MultiEdit", {"file_path": P + "cfg.py", "edits": [{"old_string": "a", "new_string": "import yaml"}, {"old_string": "b", "new_string": "data = yaml.load(raw)"}]})]),
            ("Edit .go exec.Command sh -c", [ev("s6", "Edit", {"file_path": P + "run.go", "old_string": "x", "new_string": 'exec.Command("sh", "-c", userInput)'})]),
            ("same Edit twice", [ev("s7", "Edit", {"file_path": P + "a.ts", "old_string": "x", "new_string": "eval(a)"})] * 2),
            ("second rule new", [ev("s8", "Edit", {"file_path": P + "c.ts", "old_string": "x", "new_string": "eval(a)"}),
                                 ev("s8", "Edit", {"file_path": P + "c.ts", "old_string": "x", "new_string": "eval(b); el.innerHTML = v"})]),
            ("Write file whose HEAD has eval(", [ev("s9", None, None, event="UserPromptSubmit"),
                                                ev("s9", "Write", {"file_path": P + "legacy.ts", "content": "export const run = (s: string) => eval(s);\nexport const x = 2;\n"})]),
            ("Write clean tracked file adding eval(", [ev("s10", None, None, event="UserPromptSubmit"),
                                                      ev("s10", "Write", {"file_path": P + "clean.ts", "content": "export const a = eval('1');\n"})]),
            ("Edit .ts clean", [ev("s11", "Edit", {"file_path": P + "x.ts", "old_string": "a", "new_string": "const b = 1"})]),
            ("Edit .md eval(", [ev("s12", "Edit", {"file_path": P + "README.md", "old_string": "a", "new_string": "never call eval(x) here"})]),
            ("Edit in ~/.claude/plans", [ev("s13", "Edit", {"file_path": plans, "old_string": "a", "new_string": "eval(x)"})]),
            ("Edit without file_path", [ev("s14", "Edit", {"old_string": "a", "new_string": "eval(x)"})]),
            ("Edit .py model.eval()", [ev("s15", "Edit", {"file_path": P + "m.py", "old_string": "a", "new_string": "model.eval()"})]),
            ("NotebookEdit", [ev("s16", "NotebookEdit", {"notebook_path": P + "n.ipynb", "new_source": "eval(x)"})]),
            ("malformed stdin", [b"{not json"]),
        ]
        with open(os.path.join(src["path"], "hooks/hooks.json")) as f:
            groups = json.load(f)["hooks"]["PostToolUse"]
        official = next(g for g in groups if "Edit" in g["matcher"])["hooks"][0]["command"]
        with open(os.path.join(self.out, "security-slim/hooks/hooks.json")) as f:
            spec = json.load(f)["hooks"]["PostToolUse"][0]["hooks"][0]
        ours = [spec["command"], *spec["args"]]

        def run(argv, payloads, label):
            state = tempfile.mkdtemp(prefix=label + "-", dir=self.dir)
            open(os.path.join(state, ".sdk_bootstrap_spawned"), "w").close()
            env = dict(os.environ, SECURITY_WARNINGS_STATE_DIR=state, CLAUDE_PROJECT_DIR=repo,
                       ENABLE_CODE_SECURITY_REVIEW="0", CLAUDE_PLUGIN_ROOT=src["path"])
            for p in payloads:
                done = subprocess.run(argv, input=p, capture_output=True, cwd=repo, env=env, timeout=60)
            text = done.stdout.strip()
            return (json.loads(text) if text else None), done.returncode

        warned = 0
        for name, payloads in cases:
            with self.subTest(name):
                old = run(["/bin/sh", "-c", official], payloads, "old")
                new = run(ours, payloads, "new")
                self.assertEqual(new, old)
                warned += bool(old[0] and "hookSpecificOutput" in old[0])
        self.assertGreaterEqual(warned, 8, "próbki mają sprawdzać także ostrzeżenia, nie tylko ciszę")


if __name__ == "__main__":
    unittest.main()
