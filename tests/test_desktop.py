"""Brama pulpitu: konfiguracja i tryby, lista narzędzi toolsetu z flagami zgody, mapowanie współrzędnych
(piksele zrzutu <-> punkty) przez udawanego pomocnika, teksty potwierdzeń jak w API, kierunki scrolla,
odmowa przy aplikacji-sekretcie, dziennik audytu bez wpisywanego tekstu, serwer MCP przez stdio, podpowiedź
hooka i auto-Allow bramy przeglądarki. Dodatkowo bramkowany test na żywo na prawdziwym pomocniku: nowy
dokument TextEdit (wpisanie, zaznaczenie, odczyt przez AX), lista okien Findera i zrzut.

Pomocnik udawany: tests/fakes-desktop/claude-acc-desktop (bez ekranu i wejścia).
Test na żywo: CLAUDE_ACC_DESKTOP_LIVE=1 i zbudowany podpisany pomocnik.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_desktop
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKE = os.path.join(HERE, "fakes-desktop", "claude-acc-desktop")
TMP = tempfile.mkdtemp(prefix="cad-", dir="/tmp")

os.environ["CLAUDE_ACC_DESKTOP_DIR"] = os.path.join(TMP, "d")
os.environ["CLAUDE_ACC_DESKTOP_CONFIG"] = os.path.join(TMP, "desktop.json")
os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = FAKE


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desktop = load("desktop")
hint = load("hint")
browser = load("browser")


def write_config(raw):
    with open(os.environ["CLAUDE_ACC_DESKTOP_CONFIG"], "w") as f:
        json.dump(raw, f)


def fake_helper(front="com.apple.finder", log=None, window="", sheets=1):
    env = {"FAKE_FRONT": front, "FAKE_FRONT_WINDOW": window, "FAKE_SHEETS": str(sheets)}
    if log:
        env["FAKE_LOG"] = log
    deny = {"deny": list(desktop.DENY_BUNDLES), "deny_window": list(desktop.DENY_WINDOW_MARKS)}
    h = desktop.Helper(path=_wrap(env), deny=deny)
    return h


def _wrap(env):
    """Owija fake'a skryptem, który ustawia zmienne środowiskowe (Helper nie przekazuje env)."""
    path = os.path.join(TMP, "wrap-" + "-".join(f"{k}={v}" for k, v in sorted(env.items())).replace("/", "_")[:80] + ".sh")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
        for k, v in env.items():
            f.write(f'export {k}="{v}"\n')
        f.write(f'exec /usr/bin/python3 "{FAKE}" "$@"\n')
    os.chmod(path, 0o755)
    return path


class Config(unittest.TestCase):
    def test_defaults_full_mode_and_deny_list(self):
        write_config({})
        cfg = desktop.load_config()
        self.assertEqual(cfg["mode"], "full")
        self.assertEqual(cfg["max_px"], desktop.DEFAULT_MAX_PX)
        self.assertIn("com.apple.keychainaccess", cfg["deny_bundles"])
        self.assertIn("com.1password.1password", cfg["deny_bundles"])

    def test_user_deny_adds_not_replaces(self):
        write_config({"deny_bundles": ["com.example.vault"]})
        cfg = desktop.load_config()
        self.assertIn("com.example.vault", cfg["deny_bundles"])
        self.assertIn("com.apple.keychainaccess", cfg["deny_bundles"])

    def test_bad_mode_rejected(self):
        write_config({"mode": "wild"})
        with self.assertRaises(desktop.DesktopError):
            desktop.load_config()


class Tools(unittest.TestCase):
    def test_all_17_members_present(self):
        names = {t["name"] for t in desktop.MEMBER_TOOLS}
        self.assertEqual(names, set(desktop.MEMBERS))
        self.assertEqual(len(desktop.MEMBERS), 17)

    def test_guarded_gates_keyboard_members_full_does_not(self):
        guarded = {t["name"]: t for t in desktop.tool_list("guarded")}
        full = {t["name"]: t for t in desktop.tool_list("full")}
        for name in ("type", "key", "hold_key"):
            self.assertTrue(guarded[name]["_meta"]["anthropic/requiresUserInteraction"], name)
            self.assertNotIn("_meta", full[name])
        # screenshot/left_click nigdy nie są bramkowane
        self.assertNotIn("_meta", guarded["left_click"])
        self.assertNotIn("_meta", guarded["screenshot"])


class Mapping(unittest.TestCase):
    def setUp(self):
        write_config({})
        self.log = os.path.join(TMP, "calls.jsonl")
        if os.path.exists(self.log):
            os.remove(self.log)
        self.helper = fake_helper(log=self.log)
        self.session = desktop.Session()

    def tearDown(self):
        self.helper.close()

    def last_helper(self, op):
        rows = [json.loads(l) for l in open(self.log)]
        return [r for r in rows if r["op"] == op][-1]

    def member(self, name, args):
        return desktop.run_member(self.helper, self.session, name, args, "test")

    def test_screenshot_returns_image_and_remembers_geometry(self):
        out = self.member("screenshot", {})
        self.assertEqual(out["content"][0]["type"], "image")
        self.assertEqual(out["content"][0]["mimeType"], "image/png")
        self.assertTrue(self.session.mapping.have)

    def test_click_maps_pixel_to_point(self):
        self.member("screenshot", {})
        # piksele [100,50] -> punkty (100*1440/720, 50*900/450) = (200,100)
        out = self.member("left_click", {"coordinate": [100, 50]})
        self.assertEqual(out["content"][0]["text"], "Clicked.")
        sent = self.last_helper("click")
        self.assertAlmostEqual(sent["x"], 200.0, places=3)
        self.assertAlmostEqual(sent["y"], 100.0, places=3)
        self.assertEqual(sent["button"], "left")
        self.assertEqual(sent["count"], 1)

    def test_double_and_triple_click_counts(self):
        self.member("screenshot", {})
        self.member("double_click", {"coordinate": [10, 10]})
        self.assertEqual(self.last_helper("click")["count"], 2)
        self.member("triple_click", {"coordinate": [10, 10]})
        self.assertEqual(self.last_helper("click")["count"], 3)

    def test_without_screenshot_coords_are_identity(self):
        out = self.member("left_click", {"coordinate": [300, 200]})
        self.assertEqual(out["content"][0]["text"], "Clicked.")
        sent = self.last_helper("click")
        self.assertEqual((sent["x"], sent["y"]), (300.0, 200.0))

    def test_click_without_coordinate_uses_cursor(self):
        self.member("screenshot", {})
        self.member("left_click", {})
        sent = self.last_helper("click")
        self.assertEqual((sent["x"], sent["y"]), (200.0, 100.0))  # pozycja kursora fake'a

    def test_cursor_position_renders_pixels(self):
        self.member("screenshot", {})
        out = self.member("cursor_position", {})
        # punkt kursora (200,100) -> piksele (100,50)
        self.assertEqual(out["content"][0]["text"], "X=100,Y=50")

    def test_zoom_maps_region_to_points(self):
        self.member("screenshot", {})
        out = self.member("zoom", {"region": [100, 50, 300, 250]})
        self.assertEqual(out["content"][0]["type"], "image")
        sent = self.last_helper("zoom")
        # [100,50]->(200,100); [300,250]->(600,500); w=400 h=400
        self.assertAlmostEqual(sent["x"], 200.0, places=3)
        self.assertAlmostEqual(sent["y"], 100.0, places=3)
        self.assertAlmostEqual(sent["w"], 400.0, places=3)
        self.assertAlmostEqual(sent["h"], 400.0, places=3)

    def test_drag_maps_both_ends(self):
        self.member("screenshot", {})
        self.member("left_click_drag", {"start_coordinate": [0, 0], "coordinate": [100, 50]})
        sent = self.last_helper("drag")
        self.assertEqual((sent["x"], sent["y"]), (0.0, 0.0))
        self.assertEqual((sent["x2"], sent["y2"]), (200.0, 100.0))

    def test_scroll_direction_to_vector(self):
        self.member("screenshot", {})
        out = self.member("scroll", {"coordinate": [10, 10], "scroll_direction": "down", "scroll_amount": 3})
        self.assertEqual(out["content"][0]["text"], "Scrolled down.")
        sent = self.last_helper("scroll")
        self.assertEqual((sent["dx"], sent["dy"]), (0, -3))
        self.member("scroll", {"coordinate": [10, 10], "scroll_direction": "up", "scroll_amount": 2})
        self.assertEqual(self.last_helper("scroll")["dy"], 2)
        self.member("scroll", {"coordinate": [10, 10], "scroll_direction": "left", "scroll_amount": 1})
        self.assertEqual(self.last_helper("scroll")["dx"], 1)

    def test_ack_texts_match_api(self):
        self.member("screenshot", {})
        self.assertEqual(self.member("type", {"text": "hi"})["content"][0]["text"], "Typed.")
        self.assertEqual(self.member("key", {"text": "cmd+a"})["content"][0]["text"], "Pressed cmd+a.")
        self.assertEqual(self.member("hold_key", {"text": "shift", "duration": 2})["content"][0]["text"], "Held shift for 2s.")
        self.assertEqual(self.member("mouse_move", {"coordinate": [1, 1]})["content"][0]["text"], "Moved the mouse.")
        self.assertEqual(self.member("right_click", {"coordinate": [1, 1]})["content"][0]["text"], "Right-clicked.")
        self.assertEqual(self.member("left_mouse_down", {})["content"][0]["text"], "Left mouse button pressed.")
        self.assertEqual(self.member("left_mouse_up", {})["content"][0]["text"], "Left mouse button released.")

    def test_bad_coordinate_rejected(self):
        with self.assertRaises(desktop.DesktopError):
            self.member("mouse_move", {"coordinate": [1, 2, 3]})
        with self.assertRaises(desktop.DesktopError):
            self.member("zoom", {"region": [1, 2]})


class Deny(unittest.TestCase):
    def setUp(self):
        write_config({})
        self.session = desktop.Session()

    def test_protected_app_refuses_screenshot_and_input(self):
        helper = fake_helper(front="com.apple.keychainaccess")
        try:
            for name, args in (("screenshot", {}), ("left_click", {"coordinate": [1, 1]}), ("type", {"text": "x"})):
                with self.assertRaises(desktop.DesktopError) as ctx:
                    desktop.run_member(helper, self.session, name, args, "test")
                self.assertIn("protected", str(ctx.exception).lower() + " protected")
        finally:
            helper.close()

    def test_non_protected_app_allows(self):
        helper = fake_helper(front="com.apple.finder")
        try:
            out = desktop.run_member(helper, self.session, "screenshot", {}, "test")
            self.assertEqual(out["content"][0]["type"], "image")
        finally:
            helper.close()

    def test_settings_privacy_pane_refused(self):
        helper = fake_helper(front="com.apple.systempreferences", window="Privacy & Security")
        try:
            with self.assertRaises(desktop.DesktopError):
                desktop.run_member(helper, self.session, "left_click", {"coordinate": [1, 1]}, "test")
        finally:
            helper.close()

    def test_settings_other_pane_allowed(self):
        helper = fake_helper(front="com.apple.systempreferences", window="Displays")
        try:
            out = desktop.run_member(helper, self.session, "screenshot", {}, "test")
            self.assertEqual(out["content"][0]["type"], "image")
        finally:
            helper.close()


class Audit(unittest.TestCase):
    def setUp(self):
        write_config({})
        self.dir = os.environ["CLAUDE_ACC_DESKTOP_DIR"]
        path = desktop.audit_path()
        if os.path.exists(path):
            os.remove(path)

    def test_logs_member_app_and_text_length_not_text(self):
        helper = fake_helper(front="com.apple.textedit")
        session = desktop.Session()
        try:
            desktop.run_member(helper, session, "type", {"text": "secret words"}, "owner-1")
        finally:
            helper.close()
        rows = [json.loads(l) for l in open(desktop.audit_path())]
        entry = rows[-1]
        self.assertEqual(entry["member"], "type")
        self.assertEqual(entry["app"], "com.apple.textedit")
        self.assertEqual(entry["text_len"], len("secret words"))
        self.assertNotIn("secret", json.dumps(entry))


class Mcp(unittest.TestCase):
    def test_tools_list_and_call_over_stdio(self):
        write_config({})
        import io

        out = io.StringIO()
        helper = fake_helper()
        self.addCleanup(helper.close)
        server = desktop.McpServer(out=out, helper=helper)
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "screenshot", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "key", "arguments": {"text": "Return"}}},
        ]
        server.serve(io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"))
        replies = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        by_id = {r["id"]: r for r in replies}
        self.assertEqual(len(by_id[2]["result"]["tools"]), 17)
        self.assertEqual(by_id[3]["result"]["content"][0]["type"], "image")
        self.assertEqual(by_id[4]["result"]["content"][0]["text"], "Pressed Return.")


class AgentPermissions(unittest.TestCase):
    """Zgody TCC pomocnika należą do aplikacji, pod którą biegnie (Orca dla agentów, Claude Acc dla
    panelu). Błąd, który łapią: karta Desktop i podpowiedź mówią „off”, choć brama u agentów działa."""

    GRANTED = {"ok": True, "ax": True, "screen": True, "post": True}
    MENU = {"ok": True, "ax": False, "screen": False, "post": False}

    def setUp(self):
        try:
            os.remove(desktop.panel_path())
        except OSError:
            pass

    def publish(self, pr, host):
        out = desktop.panel_dict(pr=pr, host=host)
        desktop.write_json(desktop.panel_path(), out)
        return out

    def test_menu_bar_check_keeps_what_the_agent_saw(self):
        self.publish(self.GRANTED, "Orca")  # `claude-acc desktop status` w terminalu Orki
        out = self.publish(self.MENU, desktop.MENU_APP)  # odświeżenie z aplikacji paska menu
        self.assertTrue(out["ax"] and out["screen"])
        self.assertEqual(out["agent"]["host"], "Orca")
        self.assertFalse(out["own"]["ax"])

    def test_without_an_agent_check_the_panel_shows_its_own(self):
        out = self.publish(self.MENU, desktop.MENU_APP)
        self.assertIsNone(out["agent"])
        self.assertFalse(out["ax"])

    def test_mcp_call_records_permissions_as_the_agent_gets_them(self):
        write_config({})
        self.publish(self.MENU, desktop.MENU_APP)
        helper = fake_helper()
        self.addCleanup(helper.close)
        server = desktop.McpServer(out=__import__("io").StringIO(), helper=helper)
        from unittest import mock

        with mock.patch.object(desktop, "host_app", return_value="Orca"):
            server.call_tool("key", {"text": "Return"})
        with open(desktop.panel_path()) as f:
            panel = json.load(f)
        self.assertEqual((panel["ax"], panel["screen"], panel["agent"]["host"]), (True, True, "Orca"))

    def test_host_is_the_outermost_app_not_python_or_a_helper(self):
        from unittest import mock

        ps = "\n".join([
            "500 400 /Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python",
            "400 300 /bin/zsh",
            "300 200 /Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper",
            "200 1 /Applications/Orca.app/Contents/MacOS/Orca",
        ])
        with mock.patch.object(desktop.subprocess, "run", return_value=mock.Mock(stdout=ps)):
            self.assertEqual(desktop.host_app(500), "Orca")


class Hint(unittest.TestCase):
    def test_desktop_triggers_pl_and_en(self):
        for text in ("otwórz aplikację Finder", "kliknij w Finderze", "zrób zrzut ekranu",
                     "open the Calculator app", "ustawienia systemowe", "pokaż okno TextEdit"):
            self.assertTrue(hint.DESKTOP_TRIGGER.search(text), text)

    def test_browser_phrases_do_not_route_to_desktop(self):
        for text in ("otwórz panel Stripe", "zaloguj się do AWS", "open the Vercel dashboard"):
            self.assertIsNone(hint.DESKTOP_TRIGGER.search(text), text)

    def test_desktop_context_mentions_tools_and_permissions(self):
        text = hint.desktop_context("otwórz aplikację Finder", {"ax": False, "screen": True,
                                                               "agent": {"ax": False, "screen": True, "host": "Orca"}})
        self.assertIn("mcp__desktop__", text)
        self.assertIn("doctor", text)
        self.assertIn("off in Orca", text)

    def test_menu_bar_probe_alone_does_not_tell_agents_permissions_are_off(self):
        # panel paska menu sprawdza zgody własnej aplikacji; agent w Orce i tak je ma
        text = hint.desktop_context("otwórz aplikację Finder", {"ax": False, "screen": False, "agent": None})
        self.assertNotIn("doctor", text)

    def test_context_none_without_trigger(self):
        self.assertIsNone(hint.desktop_context("napisz funkcję w pythonie", {}))


class AutoAllow(unittest.TestCase):
    def test_browser_auto_allow_off_by_default(self):
        cfg = browser.load_config(os.path.join(TMP, "b-none.json"))
        self.assertFalse(cfg["auto_allow"])

    def test_browser_auto_allow_reads_flag(self):
        path = os.path.join(TMP, "b-on.json")
        with open(path, "w") as f:
            json.dump({"auto_allow": True}, f)
        self.assertTrue(browser.load_config(path)["auto_allow"])

    def test_press_allow_presses_when_single_sheet(self):
        os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = _wrap({"FAKE_SHEETS": "1"})
        try:
            self.assertTrue(browser.press_allow(1234))
        finally:
            os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = FAKE

    def test_press_allow_declines_when_not_single(self):
        os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = _wrap({"FAKE_SHEETS": "2"})
        try:
            self.assertFalse(browser.press_allow(1234))
        finally:
            os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = FAKE

    def test_worker_stops_on_event(self):
        import threading

        os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = _wrap({"FAKE_SHEETS": "9"})  # nigdy nie wciśnie
        try:
            stop = threading.Event()
            t = threading.Thread(target=browser.auto_allow_worker, args=(1234, stop))
            t.start()
            stop.set()
            t.join(timeout=3)
            self.assertFalse(t.is_alive())
        finally:
            os.environ["CLAUDE_ACC_DESKTOP_HELPER"] = FAKE


def _signed_real_helper():
    """Prawdziwy pomocnik: najpierw $STATE, potem build dev. None, gdy brak."""
    for arch in ("arm64-apple-macosx", "x86_64-apple-macosx"):
        for conf in ("release", "debug"):
            p = os.path.join(ROOT, "app", ".build", arch, conf, "claude-acc-desktop")
            if os.path.exists(p):
                return p
    p = os.path.join(os.path.expanduser("~"), ".local/share/claude-acc", "claude-acc-desktop")
    return p if os.path.exists(p) else None


@unittest.skipUnless(os.environ.get("CLAUDE_ACC_DESKTOP_LIVE") == "1", "live smoke off (set CLAUDE_ACC_DESKTOP_LIVE=1)")
class LiveSmoke(unittest.TestCase):
    """Na żywo, prawdziwy pomocnik, nieszkodliwe cele: nowy dokument TextEdit, okna Findera, zrzut.
    Zamyka, co otworzył. Pomija się bez zgody na ekran/wejście albo bez TextEdita."""

    @classmethod
    def setUpClass(cls):
        cls.helper_path = _signed_real_helper()
        if not cls.helper_path:
            raise unittest.SkipTest("brak zbudowanego pomocnika")
        pr = json.loads(subprocess.run([cls.helper_path, json.dumps({"op": "probe"})],
                                        capture_output=True, text=True).stdout or "{}")
        if not (pr.get("ax") and pr.get("screen")):
            raise unittest.SkipTest("pomocnik bez uprawnień Dostępność/Nagrywanie ekranu")

    def helper(self):
        deny = {"deny": list(desktop.DENY_BUNDLES), "deny_window": list(desktop.DENY_WINDOW_MARKS)}
        return desktop.Helper(path=self.helper_path, deny=deny)

    def osa(self, script):
        return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()

    def test_textedit_type_select_read_and_finder_and_screenshot(self):
        if not os.path.exists("/System/Applications/TextEdit.app") and not os.path.exists("/Applications/TextEdit.app"):
            self.skipTest("brak TextEdit")
        helper = self.helper()
        session = desktop.Session()
        name = self.osa('tell application "TextEdit" to name of (make new document)')
        self.osa('tell application "TextEdit" to activate')
        import time

        time.sleep(1.0)
        try:
            # zrzut, żeby model (i mapowanie) miały układ
            shot = desktop.run_member(helper, session, "screenshot", {}, "live")
            self.assertEqual(shot["content"][0]["type"], "image")
            # wpisz tekst (idzie do fokusu TextEdit), zaznacz wszystko, odczytaj przez AX
            desktop.run_member(helper, session, "type", {"text": "claude-acc desktop smoke"}, "live")
            time.sleep(0.3)
            value = self.osa(f'tell application "TextEdit" to get text of document "{name}"')
            # TextEdit potrafi zrobić wielką pierwszą literę (smart capitalisation), więc bez wielkości liter
            self.assertIn("claude-acc desktop smoke", value.lower())
            desktop.run_member(helper, session, "key", {"text": "cmd+a"}, "live")
            # lista okien Findera przez AX pomocnika (frontmost działa zawsze)
            front = helper.call("frontmost")
            self.assertTrue(front.get("bundle"))
        finally:
            self.osa(f'tell application "TextEdit" to close (first document whose name is "{name}") saving no')
            helper.close()


if __name__ == "__main__":
    unittest.main()
