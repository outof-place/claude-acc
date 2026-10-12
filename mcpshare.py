"""Wspólne serwery MCP: jeden proces stdio dla wszystkich sesji Claude Code.

    mcpshare.py share <nazwa> [--force] [--port N]   przełącz serwer z ~/.claude.json na wspólny
    mcpshare.py unshare <nazwa> | --all               wróć do osobnej kopii stdio w każdej sesji
    mcpshare.py status [--json]
    mcpshare.py refresh [--restart]                   plisty mostów z tej wersji (woła je setup.sh)
    mcpshare.py serve --name <nazwa>                  (to uruchamia launchd)

Każda sesja Claude Code startuje własną kopię każdego serwera stdio z ~/.claude.json, więc przy
kilku równoległych agentach serwer siedzi w pamięci kilka razy (cavemem ładuje model embeddingów
w każdej kopii). `serve` trzyma JEDNO dziecko stdio i wystawia je jako Streamable HTTP na
127.0.0.1: każdy klient dostaje własną sesję (Mcp-Session-Id) i odpowiedź na `initialize`
z pamięci (dziecko inicjalizuje się raz), a jego id JSON-RPC i progressToken są przed wysłaniem do
dziecka zamieniane na unikalne i wracają do niego w odpowiedzi.

Tylko dla serwerów bez stanu per klient, które o nic klienta nie pytają (elicitation, sampling,
roots): wspólny proces nie wie, której sesji dotyczy takie pytanie, więc odpowiada dziecku błędem.
Dlatego `share` odmawia bramce poczty, przeglądarki, chrome-devtools i MCP Magic (bez --force).
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import hmac
import itertools
import json
import plistlib
import queue
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import mcpbase  # noqa: E402

VERSION = "1.0.0"
CALL_TIMEOUT = 15 * 60  # wywołanie narzędzia może czekać na sieć albo człowieka
INIT_TIMEOUT = 60
IDLE = 24 * 3600  # sesja, której klient zniknął bez DELETE
KEEPALIVE = 20  # komentarz SSE, żeby długie wywołanie nie wyglądało na martwe połączenie
MAX_SESSIONS = 512
DEBUG = bool(os.environ.get("CLAUDE_ACC_MCPSHARE_DEBUG"))
# era 2026-07-28: listy i odczyt zasobu muszą nieść ttlMs i cacheScope (Claude Code odrzuca wynik bez nich);
# dziecko mówi starszą wersją, więc dopisuje je most
CACHE_TTL = {"tools/list": 300000, "prompts/list": 300000, "resources/list": 300000,
             "resources/templates/list": 300000, "resources/read": 0}
# powiadomienie dziecka -> flaga, o którą klient prosi w subscriptions/listen
LISTEN_FLAGS = {"notifications/tools/list_changed": "toolsListChanged",
                "notifications/prompts/list_changed": "promptsListChanged",
                "notifications/resources/list_changed": "resourcesListChanged"}
SUBSCRIPTION_KEY = "io.modelcontextprotocol/subscriptionId"

# serwery, których wspólny proces by zepsuł: (fragment nazwy albo komendy, powód)
STATEFUL = (
    ("mail mcp", "zgodę na wysyłkę zbiera przez MCP elicitation, a wspólny proces nie wie, której sesji o nią spytać"),
    ("browser mcp", "pamięta, które karty otworzyła która sesja; wspólny proces pomieszałby je między agentami"),
    ("desktop mcp", "pyta o zgodę przez MCP elicitation i liczy współrzędne z ostatniego zrzutu ekranu sesji"),
    ("chrome-devtools", "prowadzi jedną przeglądarkę i stronę na klienta"),
    ("mcp-magic", "każda sesja osobno dołącza do kanału Figmy"),
    ("mcpmagic", "każda sesja osobno dołącza do kanału Figmy"),
)
# nazwy wpisów, które claude-acc i popularne serwery dostają przy instalacji
NAMED = {"mail": "mail mcp", "browser": "browser mcp", "desktop": "desktop mcp",
         "chrome-devtools": "chrome-devtools", "mcpmagic": "mcpmagic"}


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "mcpshare:", *parts, file=sys.stderr, flush=True)


# ---------- ścieżki (czytane przy każdym wywołaniu, żeby testy mogły je podmienić) ----------

def state_dir():
    return os.environ.get("CLAUDE_ACC_MCPSHARE_DIR") or os.path.expanduser("~/.local/share/claude-acc/mcpshare")


def claude_json():
    return os.environ.get("CLAUDE_ACC_CLAUDE_JSON") or os.path.expanduser("~/.claude.json")


def agents_dir():
    return os.environ.get("CLAUDE_ACC_LAUNCH_AGENTS") or os.path.expanduser("~/Library/LaunchAgents")


def launchctl():
    return os.environ.get("CLAUDE_ACC_LAUNCHCTL") or "launchctl"


def label(name):
    return f"com.filip.claude-acc.mcpshare.{name}"


def write_private(path, data):
    """Zapis atomowy z prawami 0600 (w stanie leżą tokeny i zmienne środowiska serwerów)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_state():
    try:
        with open(os.path.join(state_dir(), "servers.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    write_private(os.path.join(state_dir(), "servers.json"), json.dumps(state, indent=1, ensure_ascii=False) + "\n")


# ---------- dziecko stdio ----------

class Call:
    """Jedno żądanie klienta w drodze do dziecka: kolejka z postępem i odpowiedzią."""

    def __init__(self, orig_id, token=None):
        self.orig_id = orig_id
        self.token = token  # progressToken klienta
        self.events = queue.Queue()


class Child:
    """Jedno dziecko stdio: start, initialize, odczyt odpowiedzi i restart, gdy padnie."""

    def __init__(self, name, spec, on_notify=None):
        self.name = name
        self.spec = spec
        self.on_notify = on_notify or (lambda msg: None)
        self.write_lock = threading.Lock()
        self.pending = {}  # id u dziecka -> Call
        self.tokens = {}  # progressToken u dziecka -> Call
        self.ids = itertools.count(1)
        self.ready = threading.Event()
        self.init_result = None
        self.proc = None
        self.restarts = 0
        self.stopping = False
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name=f"child-{self.name}", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run(self):
        delay = 1
        while not self.stopping:
            started = time.time()
            try:
                self._spawn()
                self._read()
            except Exception as exc:  # zły start dziecka nie może zabić serwera HTTP
                log(self.name, f"dziecko: {type(exc).__name__}: {exc}")
            self.ready.clear()
            code = self.proc.poll() if self.proc else None
            self._fail_pending("wspólny serwer MCP uruchamia się ponownie")
            if self.stopping:
                return
            self.restarts += 1
            delay = 1 if time.time() - started > 60 else min(delay * 2, 30)
            log(self.name, f"dziecko zakończyło się (kod {code}), restart za {delay} s")
            time.sleep(delay)

    def _spawn(self):
        env = dict(os.environ)
        env.update(self.spec.get("env") or {})
        self.proc = subprocess.Popen(
            [self.spec["command"]] + list(self.spec.get("args") or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env,
            cwd=self.spec.get("cwd") or os.path.expanduser("~"),
        )
        self._init_id = f"mcpshare-init-{next(self.ids)}"
        self._send({
            "jsonrpc": "2.0", "id": self._init_id, "method": "initialize",
            "params": {
                "protocolVersion": mcpbase.PROTOCOL, "capabilities": {},
                "clientInfo": {"name": f"claude-acc-mcpshare/{self.name}", "version": VERSION},
            },
        })
        proc = self.proc
        # dziecko, które nie odpowie na initialize, zatrzymałoby wszystkie sesje bez końca
        threading.Timer(INIT_TIMEOUT, lambda: (not self.ready.is_set() and proc.poll() is None
                                               and proc is self.proc and proc.kill())).start()

    def _send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        with self.write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read(self):
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                log(self.name, "dziecko wypisało coś, co nie jest JSON-em; pomijam")
                continue
            for item in msg if isinstance(msg, list) else [msg]:
                if isinstance(item, dict):
                    self._dispatch(item)
        self.proc.wait()

    def _dispatch(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            if mid == getattr(self, "_init_id", None):
                if "error" in msg:
                    raise RuntimeError(f"initialize: {msg['error'].get('message')}")
                self.init_result = msg.get("result") or {}
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.ready.set()
                if self.restarts and ((self.init_result.get("capabilities") or {}).get("tools") or {}).get("listChanged"):
                    # nowe dziecko może mieć inne narzędzia; otwarte strumienie GET dowiedzą się od razu
                    self.on_notify({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                info = self.init_result.get("serverInfo") or {}
                log(self.name, f"dziecko {info.get('name', '?')} {info.get('version', '?')} gotowe")
                return
            call = self.pending.pop(mid, None)
            if call is not None:
                if call.token is not None:
                    self.tokens = {k: v for k, v in self.tokens.items() if v is not call}
                call.events.put(("done", msg))
            return
        if mid is not None:
            # pytanie dziecka do klienta: odpowiadamy sami, bo nie wiadomo, do której sesji należy
            if method == "ping":
                self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                self._send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601,
                    "message": f"{method} not available: this server is shared by claude-acc mcpshare "
                               "and cannot route requests to one client session"}})
            return
        if method == "notifications/progress":
            params = msg.get("params") or {}
            call = self.tokens.get(params.get("progressToken"))
            if call is not None:
                note = dict(msg, params=dict(params, progressToken=call.token))
                call.events.put(("progress", note))
            return
        if method in ("notifications/cancelled", "notifications/initialized"):
            return
        self.on_notify(msg)  # list_changed, resources/updated, logging: do wszystkich strumieni

    def _fail_pending(self, why):
        pending, self.pending, self.tokens = self.pending, {}, {}
        for call in pending.values():
            call.events.put(("done", {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32603, "message": why}}))

    def request(self, msg):
        """Wyślij żądanie klienta z własnym id i progressToken; zwraca (id u dziecka, Call)."""
        params = msg.get("params")
        token = None
        if isinstance(params, dict) and isinstance(params.get("_meta"), dict) and "progressToken" in params["_meta"]:
            token = params["_meta"]["progressToken"]
        call = Call(msg.get("id"), token)
        cid = f"c{next(self.ids)}"
        out = dict(msg, id=cid)
        if token is not None:
            ctoken = f"p{next(self.ids)}"
            out["params"] = dict(params, _meta=dict(params["_meta"], progressToken=ctoken))
            self.tokens[ctoken] = call
        self.pending[cid] = call
        try:
            self._send(out)
        except (OSError, ValueError, AttributeError):
            self.pending.pop(cid, None)
            call.events.put(("done", {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32603, "message": "wspólny serwer MCP jest niedostępny"}}))
        return cid, call

    def cancel(self, cid, reason="cancelled by client"):
        if cid in self.pending:
            try:
                self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": cid, "reason": reason}})
            except (OSError, ValueError, AttributeError):
                pass


# ---------- sesje i HTTP ----------

class Session:
    def __init__(self):
        self.last_seen = time.time()
        self.streams = []  # kolejki otwartych GET /mcp
        self.inflight = {}  # id klienta -> id u dziecka


class Hub:
    def __init__(self, name, child, token, port=0):
        self.name = name
        self.child = child
        self.token = token
        self.port = port
        self.sessions = {}
        self.listeners = []  # (kolejka, flagi) otwartych subscriptions/listen ery 2026
        self.lock = threading.Lock()
        self.started = time.time()
        child.on_notify = self.broadcast

    # -- odpowiedzi z pamięci zamiast wołania dziecka --

    def offered(self):
        caps = (self.child.init_result or {}).get("capabilities") or {}
        out = {}
        for key in ("tools", "prompts", "resources", "completions"):
            if key in caps:
                value = dict(caps[key] or {})
                value.pop("subscribe", None)  # subskrypcja jest per klient, a dziecko ma jednego
                out[key] = value
        return out

    def initialize(self, params):
        init = self.child.init_result or {}
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in mcpbase.SUPPORTED else mcpbase.PROTOCOL,
            "capabilities": self.offered(),
            "serverInfo": init.get("serverInfo") or {"name": self.name, "version": "0"},
        }
        if init.get("instructions"):
            result["instructions"] = init["instructions"]
        return result

    def discover(self):
        init = self.child.init_result or {}
        return {
            "supportedVersions": list(mcpbase.MODERN),
            "capabilities": self.offered(),
            "_meta": {"io.modelcontextprotocol/serverInfo": init.get("serverInfo") or {"name": self.name}},
            "instructions": init.get("instructions") or "",
            "ttlMs": 3600000,
            "cacheScope": "private",
        }

    def broadcast(self, msg):
        with self.lock:
            streams = [q for s in self.sessions.values() for q in s.streams]
            listeners = list(self.listeners)
        for q in streams:
            q.put(msg)
        flag = LISTEN_FLAGS.get(msg.get("method"))
        for q, flags in listeners:
            if flag and flags.get(flag):
                q.put(msg)

    def honored(self, asked):
        """Które powiadomienia z subscriptions/listen most dostarczy: list_changed tam, gdzie dziecko
        je ogłasza; subskrypcji pojedynczych zasobów nie (dziecko ma jednego klienta, nas)."""
        caps = self.offered()
        can = {"toolsListChanged": (caps.get("tools") or {}).get("listChanged"),
               "promptsListChanged": (caps.get("prompts") or {}).get("listChanged"),
               "resourcesListChanged": (caps.get("resources") or {}).get("listChanged")}
        return {k: True for k, v in (asked or {}).items() if v is True and can.get(k)}

    def new_session(self):
        sid = secrets.token_hex(16)
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions, key=lambda k: self.sessions[k].last_seen)
                del self.sessions[oldest]
            self.sessions[sid] = Session()
        return sid

    def reap(self):
        cut = time.time() - IDLE
        with self.lock:
            for sid in [k for k, s in self.sessions.items() if s.last_seen < cut and not s.streams]:
                del self.sessions[sid]

    def status(self):
        proc = self.child.proc
        return {
            "name": self.name, "pid": os.getpid(), "port": self.port,
            "child_pid": proc.pid if proc and proc.poll() is None else None,
            "ready": self.child.ready.is_set(), "restarts": self.child.restarts,
            "sessions": len(self.sessions), "listeners": len(self.listeners),
            "streams": sum(len(s.streams) for s in self.sessions.values()),
            "inflight": len(self.child.pending), "uptime": int(time.time() - self.started),
        }


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "claude-acc-mcpshare"

        def log_message(self, fmt, *args):  # treść żądań nie trafia do logu
            pass

        # -- strażnicy --

        def guard(self):
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{hub.port}", f"localhost:{hub.port}"):
                return self.error(403, -32000, "Forbidden host")  # DNS rebinding: obca strona z 127.0.0.1
            origin = self.headers.get("Origin")
            if origin and not any(origin == f"{s}://{h}" or origin.startswith(f"{s}://{h}:")
                                  for s in ("http", "https") for h in ("127.0.0.1", "localhost")):
                return self.error(403, -32000, "Forbidden origin")
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {hub.token}".encode()):
                return self.error(401, -32000, "Unauthorized")
            path = self.path.split("?", 1)[0]
            if path not in ("/mcp", "/status"):
                return self.error(404, -32000, "Not found")
            return path

        def error(self, status, code, message, rid=None):
            self.send_json(status, {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})
            return None

        def send_json(self, status, payload, headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def accepted(self):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def session(self):
            sid = self.headers.get("Mcp-Session-Id")
            with hub.lock:
                sess = hub.sessions.get(sid) if sid else None
                if sess:
                    sess.last_seen = time.time()
            return sid, sess

        # -- metody HTTP --

        def do_GET(self):
            path = self.guard()
            if path is None:
                return
            if path == "/status":
                return self.send_json(200, hub.status())
            if "text/event-stream" not in self.headers.get("Accept", ""):
                return self.error(405, -32000, "Method not allowed")
            sid, sess = self.session()
            if not sid:
                return self.error(400, -32000, "Session ID required")
            if not sess:
                return self.error(404, -32001, "Session not found")
            q = queue.Queue()
            with hub.lock:
                sess.streams.append(q)
            self.start_sse()
            try:
                while True:
                    try:
                        msg = q.get(timeout=KEEPALIVE)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        self.event(msg)
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                with hub.lock:
                    if q in sess.streams:
                        sess.streams.remove(q)

        def do_DELETE(self):
            if self.guard() is None:
                return
            sid = self.headers.get("Mcp-Session-Id")
            with hub.lock:
                gone = hub.sessions.pop(sid, None) if sid else None
            if not gone:
                return self.error(404, -32001, "Session not found")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            path = self.guard()
            if path is None:
                return
            if path != "/mcp":
                return self.error(405, -32000, "Method not allowed")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self.error(400, -32700, "Parse error")
            if DEBUG:  # same nazwy metod, bez treści: do diagnozy klienta, który czegoś nie widzi
                log(hub.name, "POST", [i.get("method") or "response" for i in (body if isinstance(body, list) else [body])
                                       if isinstance(i, dict)], "session" if self.headers.get("Mcp-Session-Id") else "-",
                    "sse" if "text/event-stream" in self.headers.get("Accept", "") else "json")
            items = body if isinstance(body, list) else [body]
            if not items or not all(isinstance(i, dict) for i in items):
                return self.error(400, -32600, "Invalid request")
            sid, sess = self.session()
            modern = any(self.is_modern(i) for i in items)
            first = items[0]
            if first.get("method") == "initialize" and not sid:
                if not hub.child.ready.wait(INIT_TIMEOUT):
                    return self.error(503, -32603, "shared MCP server is not ready", first.get("id"))
                new = hub.new_session()
                result = {"jsonrpc": "2.0", "id": first.get("id"),
                          "result": hub.initialize(first.get("params") or {})}
                return self.send_json(200, result, {"Mcp-Session-Id": new})
            if not modern:
                if not sid:
                    return self.error(400, -32000, "Session ID required")
                if not sess:
                    return self.error(404, -32001, "Session not found")
            requests = []
            for item in items:
                if item.get("method") is None:
                    continue  # odpowiedź klienta; wspólny serwer o nic klientów nie pyta
                if item.get("id") is None:
                    self.notification(item, sess)
                    continue
                requests.append(item)
            if not requests:
                return self.accepted()
            stream = len(requests) == 1 and "text/event-stream" in self.headers.get("Accept", "")
            if stream and requests[0].get("method") == "subscriptions/listen" and self.is_modern(requests[0]):
                return self.listen(requests[0])
            if stream:
                return self.answer_sse(requests[0], sess)
            replies = [self.answer(item, sess) for item in requests]
            self.send_json(200, replies if isinstance(body, list) else replies[0])

        # -- JSON-RPC --

        @staticmethod
        def is_modern(item):
            meta = (item.get("params") or {}).get("_meta") if isinstance(item.get("params"), dict) else None
            return item.get("method") == "server/discover" or (isinstance(meta, dict) and mcpbase.MODERN_KEY in meta)

        def notification(self, item, sess):
            if item.get("method") == "notifications/cancelled" and sess:
                rid = (item.get("params") or {}).get("requestId")
                cid = sess.inflight.get(json.dumps(rid))
                if cid:
                    hub.child.cancel(cid, (item.get("params") or {}).get("reason") or "cancelled by client")

        def local(self, item):
            """Odpowiedź bez dziecka albo None, gdy żądanie idzie do dziecka."""
            method, rid = item.get("method"), item.get("id")
            modern = self.is_modern(item)
            if modern:
                asked = (item.get("params") or {}).get("_meta", {}).get(mcpbase.MODERN_KEY)
                if asked not in mcpbase.MODERN:
                    return {"jsonrpc": "2.0", "id": rid, "error": {
                        "code": -32022, "message": "Unsupported protocol version",
                        "data": {"supported": list(mcpbase.MODERN) + list(mcpbase.SUPPORTED), "requested": asked}}}
            if method == "server/discover":
                return {"jsonrpc": "2.0", "id": rid, "result": dict(hub.discover(), resultType="complete")}
            if method == "initialize":
                return {"jsonrpc": "2.0", "id": rid, "result": hub.initialize(item.get("params") or {})}
            if method in ("ping", "logging/setLevel"):
                result = {"resultType": "complete"} if modern else {}
                return {"jsonrpc": "2.0", "id": rid, "result": result}
            return None

        def forward(self, item, sess):
            """Wyślij żądanie do dziecka; zwraca (Call, id u dziecka) albo odpowiedź błędu."""
            if not hub.child.ready.wait(INIT_TIMEOUT):
                return None, {"jsonrpc": "2.0", "id": item.get("id"),
                              "error": {"code": -32603, "message": "shared MCP server is not ready"}}
            out = item
            if self.is_modern(item):
                # dziecko mówi starszą wersją protokołu; pola ery 2026 zostają po stronie mostu
                params = dict(item.get("params") or {})
                params["_meta"] = {k: v for k, v in params.get("_meta", {}).items()
                                   if not k.startswith("io.modelcontextprotocol/")}
                if not params["_meta"]:
                    params.pop("_meta")
                out = dict(item, params=params)
            cid, call = hub.child.request(out)
            if sess is not None:
                sess.inflight[json.dumps(item.get("id"))] = cid
            return (call, cid), None

        def complete(self, item, sess, msg):
            if sess is not None:
                sess.inflight.pop(json.dumps(item.get("id")), None)
            reply = dict(msg, id=item.get("id"))
            if self.is_modern(item) and isinstance(reply.get("result"), dict):
                result = dict(reply["result"])
                result.setdefault("resultType", "complete")
                ttl = CACHE_TTL.get(item.get("method"))
                if ttl is not None:
                    result.setdefault("ttlMs", ttl)
                    result.setdefault("cacheScope", "private")
                reply["result"] = result
            return reply

        def wait(self, call, cid, on_progress=None, on_idle=None):
            deadline = time.time() + CALL_TIMEOUT
            while True:
                left = deadline - time.time()
                if left <= 0:
                    hub.child.cancel(cid, "timed out in claude-acc mcpshare")
                    hub.child.pending.pop(cid, None)
                    return {"jsonrpc": "2.0", "error": {"code": -32001, "message": "request timed out"}}
                try:
                    kind, msg = call.events.get(timeout=min(left, KEEPALIVE))
                except queue.Empty:
                    if on_idle:
                        on_idle()
                    continue
                if kind == "done":
                    return msg
                deadline = time.time() + CALL_TIMEOUT  # postęp przedłuża czas, jak resetTimeoutOnProgress
                if on_progress:
                    on_progress(msg)

        def answer(self, item, sess):
            reply = self.local(item)
            if reply is not None:
                return reply
            pending, err = self.forward(item, sess)
            if err:
                return err
            call, cid = pending
            return self.complete(item, sess, self.wait(call, cid))

        def start_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def event(self, msg):
            self.wfile.write(b"event: message\ndata: " + json.dumps(msg, ensure_ascii=False).encode() + b"\n\n")

        def listen(self, item):
            """subscriptions/listen (era 2026): strumień SSE z potwierdzeniem, potem list_changed."""
            refused = self.local(item)  # zła wersja protokołu
            if refused is not None and "error" in refused:
                return self.send_json(200, refused)
            flags = hub.honored(((item.get("params") or {}).get("notifications")))
            sub = secrets.token_hex(8)
            q = queue.Queue()
            with hub.lock:
                hub.listeners.append((q, flags))
            self.start_sse()
            try:
                self.event({"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged",
                            "params": {"_meta": {SUBSCRIPTION_KEY: sub}, "notifications": flags}})
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=KEEPALIVE)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        params = dict(msg.get("params") or {})
                        params["_meta"] = dict(params.get("_meta") or {}, **{SUBSCRIPTION_KEY: sub})
                        self.event(dict(msg, params=params))
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                with hub.lock:
                    hub.listeners = [entry for entry in hub.listeners if entry[0] is not q]

        def answer_sse(self, item, sess):
            reply = self.local(item)
            if reply is not None:
                return self.send_json(200, reply)
            pending, err = self.forward(item, sess)
            if err:
                return self.send_json(200, err)
            call, cid = pending
            self.start_sse()
            alive = [True]

            def write(msg=None):
                if not alive[0]:
                    return
                try:
                    if msg is None:
                        self.wfile.write(b": keepalive\n\n")
                    else:
                        self.event(msg)
                    self.wfile.flush()
                except (OSError, ValueError):
                    alive[0] = False
                    hub.child.cancel(cid, "client went away")

            done = self.wait(call, cid, on_progress=write, on_idle=write)
            write(self.complete(item, sess, done))

    return Handler


def start(name, spec, token, port=0):
    """Dziecko i serwer HTTP w wątkach; zwraca (httpd, hub). Używa tego `serve` i testy."""
    child = Child(name, spec)
    hub = Hub(name, child, token)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(hub))
    httpd.daemon_threads = True
    hub.port = httpd.server_address[1]
    child.start()
    threading.Thread(target=httpd.serve_forever, name=f"http-{name}", daemon=True).start()

    def reaper():
        while not child.stopping:
            time.sleep(3600)
            hub.reap()

    threading.Thread(target=reaper, daemon=True).start()
    return httpd, hub


def cmd_serve(name):
    entry = load_state().get(name)
    if not entry:
        log(name, "brak w servers.json; najpierw claude-acc mcp share", name)
        return 2
    with open(entry["token_file"]) as f:
        token = f.read().strip()
    httpd, hub = start(name, entry["spec"], token, entry["port"])
    log(name, f"słucha na http://127.0.0.1:{hub.port}/mcp")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(1):  # krótkie czekanie: sygnał obsługuje się tylko między nimi
        pass
    hub.child.stop()
    httpd.shutdown()
    return 0


# ---------- ~/.claude.json ----------

def read_config():
    with open(claude_json()) as f:
        return json.load(f)


def backend():
    """`cli`: przez `claude mcp`, jak robi to człowiek (Claude Code sam pilnuje zapisu pliku,
    który piszą też działające sesje); `file`: bezpośrednia, atomowa edycja (testy, brak claude)."""
    choice = os.environ.get("CLAUDE_ACC_MCPSHARE_BACKEND")
    if choice in ("cli", "file"):
        return choice
    return "cli" if shutil.which(os.environ.get("CLAUDE_ACC_CLAUDE_BIN") or "claude") else "file"


def set_entry(name, entry):
    path = claude_json()
    if backend() == "cli":
        claude = os.environ.get("CLAUDE_ACC_CLAUDE_BIN") or "claude"
        subprocess.run([claude, "mcp", "remove", "-s", "user", name], capture_output=True, text=True)
        if entry is not None:
            r = subprocess.run([claude, "mcp", "add-json", "-s", "user", name, json.dumps(entry)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"claude mcp add-json {name}: {(r.stderr or r.stdout).strip()[:300]}")
    else:
        cfg = read_config()
        servers = cfg.setdefault("mcpServers", {})
        if entry is None:
            servers.pop(name, None)
        else:
            servers[name] = entry
        write_private(path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)  # w pliku leży teraz token wspólnego serwera
    got = (read_config().get("mcpServers") or {}).get(name)
    if not matches(got, entry):
        raise RuntimeError(f"wpis {name} w {path} po zmianie nie zgadza się z oczekiwanym")


def matches(got, want):
    """Czy wpis w pliku to ten zapisany (claude mcp może dopisać własne pola, np. type)."""
    if want is None:
        return got is None
    return isinstance(got, dict) and all(got.get(k) == v for k, v in want.items() if k != "type") \
        and got.get("type", "stdio") == want.get("type", "stdio")


def stateful_reason(name, entry):
    text = " ".join([name, str(entry.get("command", ""))] + [str(a) for a in entry.get("args") or []]).lower()
    key = NAMED.get(name.lower())
    for needle, why in STATEFUL:
        if needle == key or needle in text:
            return why
    return None


# ---------- launchd ----------

def python():
    explicit = os.environ.get("CLAUDE_ACC_PYTHON")
    if explicit:
        return explicit
    linked = os.path.expanduser("~/.local/share/claude-acc/python")
    return linked if os.access(linked, os.X_OK) else sys.executable


def plist_path(name):
    return os.path.join(agents_dir(), label(name) + ".plist")


def write_agent(name):
    os.makedirs(agents_dir(), exist_ok=True)
    logfile = os.path.join(state_dir(), f"{name}.log")
    data = {
        "Label": label(name),
        "ProgramArguments": [python(), os.path.realpath(__file__), "serve", "--name", name],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        # most jest na ścieżce każdego wywołania narzędzia agenta: bez ProcessType (albo z Background)
        # launchd dławi mu CPU i I/O właśnie wtedy, gdy Mac jest zajęty (man launchd.plist)
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": load_state()[name]["path"]},
        "StandardOutPath": logfile,
        "StandardErrorPath": logfile,
    }
    for key in ("CLAUDE_ACC_MCPSHARE_DIR",):  # serwer musi czytać ten sam stan co komenda
        if os.environ.get(key):
            data["EnvironmentVariables"][key] = os.environ[key]
    with open(plist_path(name), "wb") as f:
        plistlib.dump(data, f)
    return plist_path(name)


def cmd_refresh(restart=False):
    """Plisty wspólnych serwerów od nowa, z ustawieniami tej wersji (setup.sh przy każdej instalacji).
    Działający most zostaje przy starych do następnego załadowania (logowanie, share), bo restart zrywa
    sesje MCP otwartych rozmów; --restart ładuje od razu te, których plista się zmieniła."""
    changed = []
    for name in sorted(load_state()):
        path = plist_path(name)
        try:
            with open(path, "rb") as f:
                before = f.read()
        except OSError:
            continue  # nieudostępniony do końca albo zdjęty ręcznie: share/unshare to naprawi
        write_agent(name)
        with open(path, "rb") as f:
            if f.read() == before:
                continue
        changed.append(name)
        if restart:
            agent_up(name)
    if changed:
        when = "załadowane od nowa" if restart else "działają do następnego logowania (--restart: od razu)"
        print(f"plisty mostów odświeżone: {', '.join(changed)}; {when}")
    return 0


def domain():
    return f"gui/{os.getuid()}"


def agent_up(name):
    subprocess.run([launchctl(), "bootout", f"{domain()}/{label(name)}"], capture_output=True)
    r = subprocess.run([launchctl(), "bootstrap", domain(), plist_path(name)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap: {(r.stderr or r.stdout).strip()[:300]}")


def agent_down(name):
    subprocess.run([launchctl(), "bootout", f"{domain()}/{label(name)}"], capture_output=True)
    try:
        os.remove(plist_path(name))
    except OSError:
        pass


def probe(entry, timeout=2):
    with open(entry["token_file"]) as f:
        token = f.read().strip()
    req = urllib.request.Request(f"http://127.0.0.1:{entry['port']}/status",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_ready(entry, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if probe(entry).get("ready"):
                return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- komendy ----------

def cmd_share(name, force=False, port=None, wait=60):
    try:
        cfg = read_config()
    except (OSError, ValueError) as exc:
        print(f"nie czytam {claude_json()}: {exc}", file=sys.stderr)
        return 1
    entry = (cfg.get("mcpServers") or {}).get(name)
    state = load_state()
    if name in state:
        print(f"{name} jest już wspólny (port {state[name]['port']}); najpierw claude-acc mcp unshare {name}",
              file=sys.stderr)
        return 1
    if not entry:
        known = ", ".join(sorted(cfg.get("mcpServers") or {})) or "brak"
        print(f"nie ma serwera {name} w zasięgu user ({claude_json()}); są: {known}", file=sys.stderr)
        return 1
    if entry.get("type", "stdio") != "stdio" or not entry.get("command"):
        print(f"{name} nie jest serwerem stdio (type={entry.get('type')}); wspólny może być tylko stdio",
              file=sys.stderr)
        return 1
    why = stateful_reason(name, entry)
    if why and not force:
        print(f"{name} zostaje osobno w każdej sesji: {why}. Wymuś: claude-acc mcp share {name} --force",
              file=sys.stderr)
        return 1

    os.makedirs(state_dir(), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{claude_json()}.bak-mcpshare-{stamp}"
    with open(claude_json()) as f:
        write_private(backup, f.read())
    token_file = os.path.join(state_dir(), f"{name}.token")
    token = secrets.token_urlsafe(32)
    write_private(token_file, token + "\n")
    spec = {k: entry[k] for k in ("command", "args", "env", "cwd") if k in entry}
    record = {"port": port or free_port(), "token_file": token_file, "spec": spec, "original": entry,
              "path": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "backup": backup,
              "shared_at": stamp}
    state[name] = record
    save_state(state)
    try:
        write_agent(name)
        agent_up(name)
        if not wait_ready(record, wait):
            raise RuntimeError(f"serwer nie wstał w {wait} s; log: {os.path.join(state_dir(), name + '.log')}")
        url = f"http://127.0.0.1:{record['port']}/mcp"
        set_entry(name, {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}})
    except Exception as exc:
        try:  # wpis mógł się zmienić, zanim padła weryfikacja: wraca oryginał stdio
            if not matches((read_config().get("mcpServers") or {}).get(name), entry):
                set_entry(name, entry)
        except Exception as again:
            print(f"uwaga: przywróć wpis ręcznie z {backup}: {again}", file=sys.stderr)
        agent_down(name)
        state.pop(name, None)
        save_state(state)
        try:
            os.remove(token_file)
        except OSError:
            pass
        print(f"nie udało się, nic nie zmieniam: {exc}", file=sys.stderr)
        return 1
    print(f"{name}: wspólny na http://127.0.0.1:{record['port']}/mcp (launchd {label(name)}).")
    print("Nowe sesje Claude Code łączą się z nim; działające trzymają swoje kopie do restartu.")
    print(f"Kopia ~/.claude.json: {backup}. Cofnięcie: claude-acc mcp unshare {name}")
    return 0


def cmd_unshare(names, force=False):
    state = load_state()
    if not names:
        names = sorted(state)
    rc = 0
    for name in names:
        record = state.get(name)
        if not record:
            print(f"{name} nie jest wspólny", file=sys.stderr)
            rc = 1
            continue
        try:
            current = (read_config().get("mcpServers") or {}).get(name)
        except (OSError, ValueError):
            current = None
        ours = isinstance(current, dict) and current.get("url") == f"http://127.0.0.1:{record['port']}/mcp"
        if ours or current is None or force:
            try:
                set_entry(name, record["original"])
            except Exception as exc:
                print(f"{name}: nie przywróciłem wpisu stdio: {exc}", file=sys.stderr)
                rc = 1
                continue
        else:
            print(f"{name}: wpis w ~/.claude.json zmienił ktoś inny, zostawiam go (wymuś: --force)", file=sys.stderr)
        agent_down(name)
        try:
            os.remove(record["token_file"])
        except OSError:
            pass
        state.pop(name, None)
        save_state(state)
        print(f"{name}: z powrotem osobno w każdej sesji (stdio)")
    return rc


def rss_mb(pid):
    if not pid:
        return None
    r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    try:
        return round(int(r.stdout.strip()) / 1024)
    except ValueError:
        return None


def cmd_status(as_json=False):
    rows = []
    for name, record in sorted(load_state().items()):
        row = {"name": name, "port": record["port"], "label": label(name), "up": False}
        try:
            info = probe(record)
            row.update(up=True, ready=info.get("ready"), sessions=info.get("sessions"),
                       restarts=info.get("restarts"), pid=info.get("pid"), child_pid=info.get("child_pid"),
                       rss_mb=(rss_mb(info.get("pid")) or 0) + (rss_mb(info.get("child_pid")) or 0))
        except (OSError, ValueError, urllib.error.URLError):
            pass
        rows.append(row)
    if as_json:
        print(json.dumps({"servers": rows}, ensure_ascii=False))
        return 0
    if not rows:
        print("brak wspólnych serwerów MCP; dodaj: claude-acc mcp share <nazwa>")
        return 0
    for row in rows:
        if row["up"]:
            print(f"{row['name']}: działa na :{row['port']}, sesje {row['sessions']}, restarty {row['restarts']}, "
                  f"{row['rss_mb']} MB (most + dziecko)")
        else:
            print(f"{row['name']}: nie odpowiada na :{row['port']} (launchctl print {domain()}/{row['label']})")
    return 0


USAGE = "\n\n".join(__doc__.split("\n\n")[:2])


def main(argv):
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    flags = {a for a in rest if a.startswith("--")}
    args = [a for a in rest if not a.startswith("--")]
    if cmd == "serve" and "--name" in rest:
        return cmd_serve(rest[rest.index("--name") + 1])
    if cmd == "share" and args:
        port = None
        if "--port" in rest:
            port = int(rest[rest.index("--port") + 1])
            args = [a for a in args if a != str(port)]
        return cmd_share(args[0], force="--force" in flags, port=port)
    if cmd == "unshare" and (args or "--all" in flags):
        return cmd_unshare([] if "--all" in flags else args, force="--force" in flags)
    if cmd == "status":
        return cmd_status("--json" in flags)
    if cmd == "refresh":
        return cmd_refresh(restart="--restart" in flags)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
