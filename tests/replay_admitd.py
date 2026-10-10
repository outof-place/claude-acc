#!/usr/bin/env python3
"""Bramka bajt w bajt dla admitd: prawdziwe komendy agentów przez exec i przez gniazdo, para po parze.

  python3 -I tests/replay_admitd.py KORPUS.jsonl [--every N]

KORPUS to linie JSON z polami "cmd" i "cwd" (komendy z transkryptów; zostaje na Twoim dysku, ten
skrypt niczego z niego nie zapisuje). Liczą się komendy, które przepuszcza bramka z hook-words.json,
co N-ta (domyślnie 5), z istniejącym katalogiem. HOME to katalog tymczasowy z kopią $STATE (rsync,
bez pobrań i przeglądarki) i skryptami z tego repo na wierzchu; DEVGUARD_ORCA=/usr/bin/false trzyma
strażnika z dala od Twojej Orki. Każde zdarzenie idzie najpierw przez `acc.py devguard admit`
(exec, jak claude-acc-hook), zaraz potem przez admit.sock, bo odpowiedź strażnika zależy od pamięci
i działających serwerów; różnica dostaje jedno powtórzenie pary, a ta, która zostaje, oblewa bramkę.
Wynik: liczby, czasy obu ścieżek i kod 1 przy różnicy.
"""

import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REAL_STATE = os.path.expanduser("~/.local/share/claude-acc")


def option(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def read_exact(conn, n):
    chunks = []
    while n:
        chunk = conn.recv(n)
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def warm(sock, event, cwd, env):
    data = json.dumps(
        {"v": 1, "argv": ["admit"], "event": event, "cwd": cwd, "env": env}
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(30)
        s.connect(sock)
        s.sendall(struct.pack(">I", len(data)) + data)
        head = read_exact(s, 4)
        if head is None:
            return None
        answer = json.loads(read_exact(s, struct.unpack(">I", head)[0]))
    return {k: answer[k] for k in ("stdout", "stderr", "code")}


def cold(state, event, cwd, env):
    done = subprocess.run(
        [
            os.path.join(state, "python"),
            os.path.join(state, "acc.py"),
            "devguard",
            "admit",
        ],
        input=event,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=60,
    )
    return {"stdout": done.stdout, "stderr": done.stderr, "code": done.returncode}


def quantiles(values):
    values = sorted(values)
    if not values:
        return "-"
    pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]
    return f"p50 {pick(0.5):.2f} p90 {pick(0.9):.2f} p99 {pick(0.99):.2f} ms"


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    corpus, every = sys.argv[1], int(option("--every", "5"))
    home = os.path.realpath(tempfile.mkdtemp(prefix="admitd-replay-", dir="/tmp"))
    try:
        state = os.path.join(home, ".local/share/claude-acc")
        os.makedirs(os.path.dirname(state))
        subprocess.run(
            [
                "rsync",
                "-a",
                "--exclude",
                "downloads/",
                "--exclude",
                "mcp-share/",
                "--exclude",
                "browser/",
                "--exclude",
                "dictation/",
                "--exclude",
                "admit.sock",
                REAL_STATE + "/",
                state + "/",
            ],
            check=True,
        )
        for path in glob.glob(os.path.join(ROOT, "*.py")):
            shutil.copy(path, state)
        with open(os.path.join(state, "hook-words.json")) as f:
            gate = re.compile(json.load(f)["gate"][0])
        env = {
            "HOME": home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DEVGUARD_ORCA": "/usr/bin/false",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }
        sock = os.path.join(state, "admit.sock")
        server = subprocess.Popen(
            [
                os.path.join(state, "python"),
                os.path.join(state, "acc.py"),
                "devguard",
                "admitd",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(200):
                if os.path.exists(sock):
                    break
                time.sleep(0.05)
            gated = replayed = same = answered = diff = reruns = 0
            t_cold, t_warm = [], []
            with open(corpus) as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    cmd, cwd = row.get("cmd") or "", row.get("cwd") or ""
                    if not cmd or not gate.search(cmd):
                        continue
                    gated += 1
                    if gated % every or not os.path.isdir(cwd):
                        continue
                    replayed += 1
                    event = json.dumps(
                        {
                            "session_id": "replay",
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Bash",
                            "tool_input": {"command": cmd, "description": "d"},
                            "cwd": cwd,
                        }
                    )
                    for attempt in range(2):
                        t0 = time.monotonic()
                        a = cold(state, event, cwd, env)
                        t1 = time.monotonic()
                        b = warm(sock, event, cwd, env)
                        t2 = time.monotonic()
                        if a == b:
                            break
                        if attempt == 0:
                            reruns += 1
                    t_cold.append((t1 - t0) * 1000)
                    t_warm.append((t2 - t1) * 1000)
                    if a == b:
                        same += 1
                        answered += bool(a["stdout"])
                    else:
                        diff += 1
                        if diff <= 10:
                            print(f"DIFF {cmd[:120]!r}\n  exec {a}\n  sock {b}")
        finally:
            server.kill()
            server.wait()
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print(
        f"gated {gated}, replayed {replayed}: identical {same} (with an answer {answered}), different {diff}, "
        f"pairs re-run after a first difference {reruns}"
    )
    print(f"exec   {quantiles(t_cold)}")
    print(f"socket {quantiles(t_warm)}")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
