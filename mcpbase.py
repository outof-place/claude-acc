"""Serwer MCP na stdio bez SDK, wspólny dla bramek claude-acc (mail.py, browser.py).

Obie ery protokołu naraz: klasyczny handshake `initialize` (2025-11-25 i starsze) i 2026-07-28,
gdzie klient nie robi handshake'u, tylko w każdym żądaniu niesie w `_meta` wersję i swoje
możliwości (`server/discover` zamiast `initialize`, `resultType` w każdej odpowiedzi, -32022
dla nieznanej wersji). Klasa pochodna podaje nazwę, narzędzia i `call_tool`; tools/call idzie
do puli wątków, więc długie wywołanie (czekanie na zgodę, ładowanie strony) nie blokuje odczytu.
"""

# Python 3.15 (PEP 810) ładuje go dopiero przy pierwszym serwerze, a starsze pomijają tę nazwę:
# `--help` i status bramek nie płacą za pulę wątków
__lazy_modules__ = ["concurrent.futures"]

import json
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor

PROTOCOL = "2025-11-25"
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN = ("2026-07-28",)
MODERN_KEY = "io.modelcontextprotocol/protocolVersion"


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class McpServer:
    name = "claude-acc"
    title = "claude-acc"
    version = "1.0.0"
    instructions = ""
    tools = ()
    workers = 4

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.client = "mcp"
        self.client_caps = {}
        self.write_lock = threading.Lock()
        self.pending = {}
        self.next_id = 0

    # ---- do nadpisania ----

    def call_tool(self, name, args):
        """Wynik tools/call (content, structuredContent, isError)."""
        raise NotImplementedError

    def on_client(self):
        """Klient się przedstawił (initialize albo pierwsze żądanie 2026-07-28)."""

    # ---- protokół ----

    def send(self, msg):
        with self.write_lock:
            self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.out.flush()

    def request(self, method, params, timeout=600):
        """Zapytanie serwera do klienta (np. elicitation/create); odpowiedź przychodzi przez serve()."""
        with self.write_lock:
            self.next_id += 1
            rid = f"srv-{self.next_id}"
        future = Future()
        self.pending[rid] = future
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return future.result(timeout=timeout)
        finally:
            self.pending.pop(rid, None)

    def server_info(self):
        return {"name": self.name, "title": self.title, "version": self.version}

    def handle(self, msg):
        if not isinstance(msg, dict):
            return None
        if msg.get("method") is None:
            future = self.pending.get(msg.get("id"))
            if future is not None and not future.done():
                future.set_result(msg)  # odpowiedź klienta na nasze zapytanie
            return None
        if msg.get("id") is None:
            return None  # notyfikacja (initialized, cancelled)
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        modern = MODERN_KEY in meta or msg["method"] == "server/discover"
        if modern:
            asked = meta.get(MODERN_KEY)
            if asked not in MODERN:
                return {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                        "data": {
                            "supported": list(MODERN) + list(SUPPORTED),
                            "requested": asked,
                        },
                    },
                }
            info = meta.get("io.modelcontextprotocol/clientInfo") or {}
            self.client = f"mcp:{info.get('name', '?')}/{info.get('version', '?')}"
            self.client_caps = (
                meta.get("io.modelcontextprotocol/clientCapabilities") or {}
            )
            self.on_client()
        try:
            result = self.dispatch(msg["method"], params)
            if modern and isinstance(result, dict):
                result.setdefault("resultType", "complete")
        except RpcError as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": exc.code, "message": str(exc)},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

    def dispatch(self, method, params):
        if method == "server/discover":
            return {
                "supportedVersions": list(MODERN),
                "capabilities": {"tools": {}},
                "_meta": {"io.modelcontextprotocol/serverInfo": self.server_info()},
                "instructions": self.instructions,
                "ttlMs": 3600000,
                "cacheScope": "private",
            }
        if method == "initialize":
            info = params.get("clientInfo") or {}
            self.client = f"mcp:{info.get('name', '?')}/{info.get('version', '?')}"
            self.client_caps = params.get("capabilities") or {}
            self.on_client()
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in SUPPORTED else PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self.server_info(),
                "instructions": self.instructions,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list(self.tools), "ttlMs": 300000, "cacheScope": "private"}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in self.tools}:
                raise RpcError(-32602, f"unknown tool: {name}")
            try:
                return self.call_tool(name, args)
            except Exception as exc:  # błąd narzędzia widzi agent, serwer żyje dalej
                return {
                    "content": [
                        {"type": "text", "text": f"error: {type(exc).__name__}: {exc}"}
                    ],
                    "isError": True,
                }
        raise RpcError(-32601, f"method not found: {method}")

    def serve(self, stream=None):
        stream = stream or sys.stdin
        pool = ThreadPoolExecutor(max_workers=self.workers)
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    }
                )
                continue
            for item in msg if isinstance(msg, list) else [msg]:
                if isinstance(item, dict) and item.get("method") == "tools/call":
                    pool.submit(self._answer, item)
                else:
                    self._answer(item)
        pool.shutdown(wait=True)

    def _answer(self, item):
        reply = self.handle(item)
        if reply is not None:
            self.send(reply)
