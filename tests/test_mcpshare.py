"""Wspólne serwery MCP (mcpshare.py) bez prawdziwego launchd i bez ~/.claude.json.

Serwer HTTP działa w wątku na losowym porcie, a dzieckiem jest atrapa stdio
(tests/fakes-mcpshare/fake_server.py). Klienci rozmawiają z nim przez urllib, jak Claude Code:
initialize, sesje, równoległe wywołania z tym samym id, postęp w SSE, restart dziecka, era 2026.
share/unshare działa na tymczasowym .claude.json z atrapami launchctl (uruchamia `serve` z plisty)
i claude (`mcp remove/add-json`).

Uruchomienie: /usr/bin/python3 -m unittest tests.test_mcpshare
"""

import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKES = os.path.join(HERE, "fakes-mcpshare")
FAKE_SERVER = os.path.join(FAKES, "fake_server.py")
sys.path.insert(0, ROOT)
_spec = importlib.util.spec_from_file_location("mcpshare", os.path.join(ROOT, "mcpshare.py"))
mcpshare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcpshare)

TOKEN = "t" * 43
SPEC = {"command": sys.executable, "args": [FAKE_SERVER]}
ACCEPT = "application/json, text/event-stream"


class Client:
    """Klient Streamable HTTP w stylu Claude Code (tylko to, czego potrzebują testy)."""

    def __init__(self, port, token=TOKEN):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.token = token
        self.sid = None

    def post(self, msg, headers=None, accept="application/json"):
        h = {"Content-Type": "application/json", "Accept": accept, "Authorization": f"Bearer {self.token}"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        h.update(headers or {})
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def initialize(self):
        status, headers, body = self.post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.sid = headers.get("Mcp-Session-Id")
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return status, json.loads(body)

    def call(self, name, args=None, rid=1, meta=None):
        params = {"name": name, "arguments": args or {}}
        if meta:
            params["_meta"] = meta
        status, _, body = self.post({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params})
        return status, json.loads(body)

    @staticmethod
    def text(reply):
        return reply["result"]["content"][0]["text"]


def sse_events(body):
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


class HandlerShapeTest(unittest.TestCase):
    def test_handler_does_not_shadow_base_methods(self):
        # finish() zasłonięte przez metodę mostu wywracało każde żądanie po wysłaniu odpowiedzi
        import http.server
        handler = mcpshare.make_handler(None)
        own = {k for k in vars(handler) if callable(vars(handler)[k]) and not k.startswith("__")}
        allowed = {"do_GET", "do_POST", "do_DELETE", "log_message"}
        self.assertEqual(sorted((own & set(dir(http.server.BaseHTTPRequestHandler))) - allowed), [])


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.httpd, self.hub = mcpshare.start("fake", SPEC, TOKEN)
        self.port = self.hub.port
        self.assertTrue(self.hub.child.ready.wait(10), "dziecko nie wstało")

    def tearDown(self):
        self.hub.child.stop()
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_auth_origin_host_and_path(self):
        c = Client(self.port, token="zly")
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 401)
        c.token = ""
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Authorization": ""})[0], 401)
        c.token = TOKEN
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                {"Origin": "http://127.0.0.1.evil.example"})[0], 403)
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Host": "evil.example"})[0], 403)
        status, _, _ = c.initialize() + ("",)
        self.assertEqual(status, 200)
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 2, "method": "ping"},
                                {"Origin": f"http://localhost:{self.port}"})[0], 200)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/other", headers={"Authorization": f"Bearer {TOKEN}"})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(err.exception.code, 404)

    def test_initialize_cached_once_per_child(self):
        a, b = Client(self.port), Client(self.port)
        sa, ra = a.initialize()
        sb, rb = b.initialize()
        self.assertEqual((sa, sb), (200, 200))
        self.assertTrue(a.sid and b.sid and a.sid != b.sid)
        self.assertEqual(ra["result"]["serverInfo"], {"name": "fake", "version": "1.2.3"})
        self.assertEqual(ra["result"]["instructions"], "fake instructions")
        self.assertEqual(ra["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("subscribe", ra["result"]["capabilities"]["resources"])
        stats = json.loads(Client.text(a.call("stats")[1]))
        self.assertEqual(stats["inits"], 1)  # dwa klienty, jedno initialize u dziecka

    def test_same_ids_from_two_sessions_do_not_mix(self):
        a, b = Client(self.port), Client(self.port)
        a.initialize()
        b.initialize()
        out = {}

        def run(client, tag):
            out[tag] = client.call("slow", {"seconds": 0.4, "tag": tag}, rid=1)[1]

        threads = [threading.Thread(target=run, args=(a, "A")), threading.Thread(target=run, args=(b, "B"))]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLess(time.time() - started, 0.75)  # równolegle, nie po kolei
        self.assertEqual((out["A"]["id"], Client.text(out["A"])), (1, "A"))
        self.assertEqual((out["B"]["id"], Client.text(out["B"])), (1, "B"))
        self.assertEqual(self.hub.child.pending, {})

    def test_progress_streams_with_client_token(self):
        a = Client(self.port)
        a.initialize()
        status, headers, body = a.post({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "progress", "arguments": {}, "_meta": {"progressToken": "tok-1"}}}, accept=ACCEPT)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        events = sse_events(body)
        notes = [e for e in events if e.get("method") == "notifications/progress"]
        self.assertEqual([n["params"]["progressToken"] for n in notes], ["tok-1", "tok-1"])
        self.assertEqual(events[-1]["id"], 7)
        self.assertEqual(Client.text(events[-1]), "done")

    def test_child_requests_are_refused_not_routed(self):
        a = Client(self.port)
        a.initialize()
        reply = json.loads(Client.text(a.call("elicit")[1]))
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertIn("shared", reply["error"]["message"])

    def test_child_restart_after_crash(self):
        a = Client(self.port)
        a.initialize()
        first = json.loads(Client.text(a.call("stats")[1]))["pid"]
        _, died = a.call("die", rid=5)
        self.assertEqual(died["id"], 5)
        self.assertEqual(died["error"]["code"], -32603)
        deadline = time.time() + 10
        while time.time() < deadline:
            _, reply = a.call("stats", rid=6)
            if "result" in reply:
                break
            time.sleep(0.2)
        second = json.loads(Client.text(reply))
        self.assertNotEqual(second["pid"], first)
        self.assertEqual(second["inits"], 1)
        self.assertEqual(self.hub.child.restarts, 1)

    def test_sessions_and_notifications(self):
        a = Client(self.port)
        status, _, body = a.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual((status, json.loads(body)["error"]["message"]), (400, "Session ID required"))
        a.sid = "nieznana"
        self.assertEqual(a.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0], 404)
        a.sid = None
        a.initialize()
        status, _, body = a.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, ""))
        status, _, body = a.post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual([t["name"] for t in json.loads(body)["result"]["tools"]][:2], ["echo", "slow"])
        req = urllib.request.Request(a.url, method="DELETE",
                                     headers={"Authorization": f"Bearer {TOKEN}", "Mcp-Session-Id": a.sid})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(a.post({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})[0], 404)

    def test_modern_era_without_session(self):
        a = Client(self.port)
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"}}
        status, _, body = a.post({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}})
        found = json.loads(body)["result"]
        self.assertEqual((status, found["supportedVersions"], found["resultType"]), (200, ["2026-07-28"], "complete"))
        status, _, body = a.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "echo", "arguments": {"x": 1}, "_meta": meta}})
        reply = json.loads(body)
        self.assertEqual((reply["id"], reply["result"]["resultType"]), (2, "complete"))
        self.assertEqual(Client.text(reply), '{"x": 1}')
        _, _, body = a.post({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"_meta": meta}},
                            accept=ACCEPT)
        listed = sse_events(body)[-1]["result"]
        # bez ttlMs i cacheScope Claude Code 2.1.295 odrzuca listę i sesja nie widzi narzędzi
        self.assertEqual((listed["ttlMs"], listed["cacheScope"], listed["resultType"]), (300000, "private", "complete"))
        self.assertIn("echo", [t["name"] for t in listed["tools"]])
        meta["io.modelcontextprotocol/protocolVersion"] = "2099-01-01"
        _, _, body = a.post({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"_meta": meta}})
        self.assertEqual(json.loads(body)["error"]["code"], -32022)


    def test_modern_listen_acknowledges_then_streams_list_changed(self):
        import http.client
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp", body=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "subscriptions/listen",
                     "params": {"_meta": meta, "notifications": {"toolsListChanged": True, "promptsListChanged": True}}}),
                     headers={"Content-Type": "application/json", "Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)

        def next_event():
            while True:
                line = resp.fp.readline().decode()
                if line.startswith("data: "):
                    return json.loads(line[len("data: "):])

        ack = next_event()
        self.assertEqual(ack["method"], "notifications/subscriptions/acknowledged")
        self.assertEqual(ack["params"]["notifications"], {"toolsListChanged": True})  # dziecko nie ma promptów
        sub = ack["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"]
        deadline = time.time() + 5
        while not self.hub.listeners and time.time() < deadline:
            time.sleep(0.05)
        self.hub.broadcast({"jsonrpc": "2.0", "method": "notifications/prompts/list_changed"})  # nie zamówione
        self.hub.broadcast({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        note = next_event()
        self.assertEqual(note["method"], "notifications/tools/list_changed")
        self.assertEqual(note["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"], sub)
        conn.close()


class ShareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcpshare-test-")
        self.cfg = os.path.join(self.tmp, "claude.json")
        self.original = {"type": "stdio", "command": sys.executable, "args": [FAKE_SERVER], "env": {"FAKE": "1"}}
        with open(self.cfg, "w") as f:
            json.dump({"numStartups": 3, "mcpServers": {
                "fake": self.original,
                "mail": {"type": "stdio", "command": "/x/claude-acc", "args": ["mail", "mcp"]},
                "remote": {"type": "http", "url": "https://example.com/mcp"}}}, f)
        os.chmod(self.cfg, 0o644)
        self.env = {
            "CLAUDE_ACC_MCPSHARE_DIR": os.path.join(self.tmp, "state"),
            "CLAUDE_ACC_CLAUDE_JSON": self.cfg,
            "CLAUDE_ACC_LAUNCH_AGENTS": os.path.join(self.tmp, "agents"),
            "CLAUDE_ACC_LAUNCHCTL": os.path.join(FAKES, "launchctl"),
            "CLAUDE_ACC_CLAUDE_BIN": os.path.join(FAKES, "claude"),
            "CLAUDE_ACC_MCPSHARE_BACKEND": "cli",
            "CLAUDE_ACC_PYTHON": sys.executable,
            "FAKE_LAUNCHCTL_DB": os.path.join(self.tmp, "launchctl.json"),
        }
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        mcpshare.cmd_unshare([])  # gdyby test padł w połowie, serwer nie zostaje
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def servers(self):
        with open(self.cfg) as f:
            return json.load(f)["mcpServers"]

    def test_refuses_stateful_and_non_stdio(self):
        before = open(self.cfg).read()
        self.assertEqual(mcpshare.cmd_share("mail", wait=5), 1)
        self.assertEqual(mcpshare.cmd_share("remote", wait=5), 1)
        self.assertEqual(mcpshare.cmd_share("brak", wait=5), 1)
        self.assertEqual(open(self.cfg).read(), before)
        self.assertEqual(mcpshare.load_state(), {})

    def test_share_status_unshare_round_trip(self):
        self.assertEqual(mcpshare.cmd_share("fake", wait=20), 0)
        entry = self.servers()["fake"]
        self.assertEqual(entry["type"], "http")
        self.assertTrue(entry["url"].startswith("http://127.0.0.1:") and entry["url"].endswith("/mcp"))
        self.assertTrue(entry["headers"]["Authorization"].startswith("Bearer "))
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        state = mcpshare.load_state()["fake"]
        self.assertEqual(state["original"], self.original)
        self.assertEqual(stat.S_IMODE(os.stat(state["token_file"]).st_mode), 0o600)
        self.assertTrue(os.path.exists(state["backup"]))
        self.assertEqual(json.load(open(state["backup"]))["mcpServers"]["fake"], self.original)
        plist = os.path.join(self.env["CLAUDE_ACC_LAUNCH_AGENTS"], "com.filip.claude-acc.mcpshare.fake.plist")
        self.assertTrue(os.path.exists(plist))

        # sesja Claude Code łączy się z wpisem, który dostała w konfiguracji, i woła narzędzie dziecka
        port = int(entry["url"].rsplit(":", 1)[1].split("/")[0])
        client = Client(port, token=entry["headers"]["Authorization"].split(" ", 1)[1])
        self.assertEqual(client.initialize()[0], 200)
        reply = client.call("echo", {"y": 2})[1]
        self.assertEqual(Client.text(reply), '{"y": 2}')
        self.assertEqual(json.loads(Client.text(client.call("stats", rid=2)[1]))["inits"], 1)

        self.assertEqual(mcpshare.cmd_share("fake", wait=5), 1)  # drugi raz nie
        self.assertEqual(mcpshare.cmd_status(as_json=False), 0)
        info = mcpshare.probe(state)
        self.assertTrue(info["ready"])
        self.assertEqual(info["sessions"], 1)

        self.assertEqual(mcpshare.cmd_unshare(["fake"]), 0)
        self.assertEqual(self.servers()["fake"], self.original)  # dokładnie oryginał stdio
        self.assertEqual(mcpshare.load_state(), {})
        self.assertFalse(os.path.exists(plist))
        self.assertFalse(os.path.exists(state["token_file"]))
        with self.assertRaises(Exception):
            mcpshare.probe(state, timeout=1)

    def test_file_backend_and_foreign_entry_left_alone(self):
        os.environ["CLAUDE_ACC_MCPSHARE_BACKEND"] = "file"
        self.assertEqual(mcpshare.cmd_share("fake", wait=20), 0)
        servers = self.servers()
        self.assertEqual(servers["fake"]["type"], "http")
        self.assertEqual(servers["mail"]["args"], ["mail", "mcp"])  # reszta pliku bez zmian
        with open(self.cfg) as f:
            self.assertEqual(json.load(f)["numStartups"], 3)
        # ktoś w międzyczasie przepiął wpis: unshare zatrzymuje serwer, ale wpisu nie rusza
        cfg = json.load(open(self.cfg))
        cfg["mcpServers"]["fake"] = {"type": "http", "url": "https://elsewhere.example/mcp"}
        json.dump(cfg, open(self.cfg, "w"))
        self.assertEqual(mcpshare.cmd_unshare(["fake"]), 0)
        self.assertEqual(self.servers()["fake"]["url"], "https://elsewhere.example/mcp")
        self.assertEqual(mcpshare.load_state(), {})

    def test_failed_start_rolls_back(self):
        cfg = json.load(open(self.cfg))
        cfg["mcpServers"]["broken"] = {"type": "stdio", "command": "/nonexistent/server", "args": []}
        json.dump(cfg, open(self.cfg, "w"))
        before = self.servers()["broken"]
        self.assertEqual(mcpshare.cmd_share("broken", wait=3), 1)
        self.assertEqual(self.servers()["broken"], before)
        self.assertEqual(mcpshare.load_state(), {})
        plist = os.path.join(self.env["CLAUDE_ACC_LAUNCH_AGENTS"], "com.filip.claude-acc.mcpshare.broken.plist")
        self.assertFalse(os.path.exists(plist))


if __name__ == "__main__":
    unittest.main()
