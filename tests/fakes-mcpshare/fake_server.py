#!/usr/bin/env python3
"""Atrapa serwera MCP na stdio dla testów mcpshare.

Narzędzia: echo (zwraca argumenty), slow (śpi i zwraca tag), progress (dwa powiadomienia postępu
z tokenem żądania), die (proces kończy się bez odpowiedzi), elicit (pyta klienta i zwraca to, co
dostał), stats (pid i liczba initialize w tym procesie).
"""
import json
import os
import sys
import threading
import time

lock = threading.Lock()
inits = 0
waiting = {}


def send(msg):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def text(rid, value):
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": value}]}})


def call(rid, params):
    name, args = params.get("name"), params.get("arguments") or {}
    token = (params.get("_meta") or {}).get("progressToken")
    if name == "echo":
        return text(rid, json.dumps(args, sort_keys=True))
    if name == "slow":
        time.sleep(float(args.get("seconds", 0.3)))
        return text(rid, str(args.get("tag")))
    if name == "progress":
        for n in (1, 2):
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": token, "progress": n, "total": 2}})
            time.sleep(0.05)
        return text(rid, "done")
    if name == "die":
        os._exit(3)
    if name == "elicit":
        event = threading.Event()
        waiting["e1"] = (event, [])
        send({"jsonrpc": "2.0", "id": "e1", "method": "elicitation/create",
              "params": {"message": "ok?", "requestedSchema": {"type": "object", "properties": {}}}})
        event.wait(5)
        return text(rid, json.dumps(waiting["e1"][1][0] if waiting["e1"][1] else None))
    if name == "stats":
        return text(rid, json.dumps({"pid": os.getpid(), "inits": inits}))
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}})


TOOLS = [{"name": n, "description": n, "inputSchema": {"type": "object"}}
         for n in ("echo", "slow", "progress", "die", "elicit", "stats")]

for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method is None:
        if rid in waiting:
            waiting[rid][1].append(msg)
            waiting[rid][0].set()
        continue
    if method == "initialize":
        inits += 1
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {"listChanged": True}, "resources": {"subscribe": True}},
            "serverInfo": {"name": "fake", "version": "1.2.3"}, "instructions": "fake instructions"}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=call, args=(rid, msg.get("params") or {}), daemon=True).start()
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
