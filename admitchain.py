#!/usr/bin/env python3
"""Hook admit (claude-acc-hook) w settings.json obok łańcucha fasthooks (kontrakt 1 z cmd-proxy).

pod-hook-client z fasthooks przepuszcza każdą komendę Bash przez rtk-enforce, a tę ze słowem z
hook-words.json oddaje claude-acc-hook z tym samym zdarzeniem. Nasz osobny wpis PreToolUse byłby
wtedy drugim admit tej samej komendy. Łańcuch działa tylko wtedy, gdy zgadzają się trzy rzeczy naraz:

- znacznik ~/.claude/hooks/fasthooks/chains-claude-acc, zwykły plik z treścią "1\\n" (wersja kontraktu);
- w settings.json wpis PreToolUse dla Basha z programem ~/.claude/hooks/fasthooks/pod-hook-client,
  którego `command` i `args` dają ".../.claude/hooks/fasthooks/pod-hook-client pre-bash ...";
- `pod-hook-client --chains-claude-acc` wypisuje "1" i kończy się kodem 0: działa i zna kontrakt 1.

heal (setup.sh i launchd co 5 minut, `perf.py keep`):
- łańcuch działa: nasz wpis admit znika z settings.json i ląduje w admit-chain.json, stan zapisuje
  chain_seen;
- łańcuch przestał działać po chain_seen, a naszego wpisu nie ma: wpis wraca, ten zapamiętany albo
  claude-acc-hook w exec form. Usunięty albo zepsuty fasthooks nie zostawia agentów bez strażnika
  dev serwerów i schedulera;
- bez chain_seen niczego nie dodajemy: hook admit wpisuje sam użytkownik (README), więc wpis
  usunięty ręcznie zostaje usunięty.

Wpisów pod-hook-client i rtk-enforce nie ruszamy nigdy, a uruchamiamy tylko pod-hook-client z jego
stałej ścieżki, nigdy program wzięty z settings.json.

    admitchain.py status|heal [ścieżka settings.json]
"""

import json
import os
import re
import subprocess
import sys

import hook  # read_settings / write_settings: zapis atomowy, ponowna próba po cudzym zapisie

CONTRACT = "1"
CHAIN_ARGS = "/.claude/hooks/fasthooks/pod-hook-client pre-bash"
CHECK_TIMEOUT = 2
# devguard.py admit, także przez acc.py, jak DEVGUARD_ADMIT w perf.py
ADMIT = re.compile(r"(?:^|\s)\S*(?:devguard\.py|acc\.py devguard) admit$")
NATIVE_MARKER = "claude-acc/claude-acc-hook"


def paths(home=None):
    """Ścieżki z HOME w chwili wywołania: testy i launchd mają każde swoje."""
    home = home or os.path.expanduser("~")
    state = os.path.join(home, ".local", "share", "claude-acc")
    fasthooks = os.path.join(home, ".claude", "hooks", "fasthooks")
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    return {
        "settings": os.path.join(config, "settings.json"),
        "state": os.path.join(state, "admit-chain.json"),
        "marker": os.path.join(fasthooks, "chains-claude-acc"),
        "client": os.path.join(fasthooks, "pod-hook-client"),
        "native": os.path.join(state, "claude-acc-hook"),
        "python": os.path.join(state, "python"),
        "acc": os.path.join(state, "acc.py"),
    }


def for_bash(matcher):
    """Czy grupa z tym matcherem dostaje komendy Bash: bez matchera, "*" i wzorce jak w Claude Code."""
    if not matcher or matcher == "*":
        return True
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return matcher == "Bash"


def line(entry):
    """Komenda wpisu z argumentami exec form, jak ją widzi kontrakt."""
    args = entry.get("args")
    words = [entry.get("command") or ""]
    if isinstance(args, list):
        words += [a for a in args if isinstance(a, str)]
    return " ".join(words)


def is_admit(entry):
    """Nasz wpis admit: claude-acc-hook bez `pause` i `codex` albo devguard.py admit."""
    command = (entry.get("command") or "").strip()
    args = entry.get("args")
    if command.endswith(NATIVE_MARKER):
        return not (isinstance(args, list) and args[:1] in (["pause"], ["codex"]))
    return bool(ADMIT.search(line(entry)))


def bash_groups(settings):
    groups = ((settings.get("hooks") or {}).get("PreToolUse")) or []
    return [g for g in groups if isinstance(g, dict) and for_bash(g.get("matcher"))]


def admit_entries(settings):
    return [(g, h) for g in bash_groups(settings) for h in g.get("hooks") or [] if isinstance(h, dict) and is_admit(h)]


def chain_entry(settings, p):
    for group in bash_groups(settings):
        for entry in group.get("hooks") or []:
            if isinstance(entry, dict) and entry.get("command") == p["client"] and CHAIN_ARGS in line(entry):
                return entry
    return None


def marker_ok(p):
    try:
        if os.path.islink(p["marker"]) or not os.path.isfile(p["marker"]):
            return False
        with open(p["marker"]) as f:
            return f.read() == CONTRACT + "\n"
    except OSError:
        return False


def client_ok(p):
    """pod-hook-client ze stałej ścieżki potwierdza kontrakt: działa i mówi tą samą wersją."""
    if not os.access(p["client"], os.X_OK):
        return False
    try:
        done = subprocess.run([p["client"], "--chains-claude-acc"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout in (CONTRACT.encode(), CONTRACT.encode() + b"\n")


def broken(settings, p):
    """Dlaczego łańcuch nie działa, albo None, gdy działa (kolejność: najtańsze sprawdzenie pierwsze)."""
    if not marker_ok(p):
        return "brak znacznika chains-claude-acc z kontraktem 1"
    if chain_entry(settings, p) is None:
        return "brak wpisu pod-hook-client pre-bash w settings.json"
    if not client_ok(p):
        return "pod-hook-client nie potwierdza kontraktu 1"
    return None


def load_state(p):
    try:
        with open(p["state"]) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(p, data):
    os.makedirs(os.path.dirname(p["state"]), exist_ok=True)
    tmp = p["state"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p["state"])


def default_entry(p):
    """Wpis admit, gdy żaden nie został zapamiętany: natywny front w exec form, bez niego Python."""
    if os.access(p["native"], os.X_OK):
        entry = {"type": "command", "command": p["native"], "args": [], "timeout": 10}
    else:
        entry = {"type": "command", "command": p["python"], "args": [p["acc"], "devguard", "admit"], "timeout": 10}
    return {"matcher": "Bash", "hook": entry}


def take_out(settings, found):
    """Zdejmuje wpisy admit (grupa, która opustoszeje, znika); zwraca je z matcherem grupy."""
    removed = []
    for group, entry in found:
        group["hooks"] = [h for h in group["hooks"] if h is not entry]
        removed.append({"matcher": group.get("matcher"), "hook": entry})
    pre = [g for g in settings["hooks"]["PreToolUse"] if not (isinstance(g, dict) and g.get("hooks") == [])]
    if pre:
        settings["hooks"]["PreToolUse"] = pre
    else:
        del settings["hooks"]["PreToolUse"]
    return removed


def put_back(settings, removed):
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for item in removed:
        group = next((g for g in pre if isinstance(g, dict) and g.get("matcher") == item["matcher"]), None)
        if group is None:
            group = {"matcher": item["matcher"], "hooks": []} if item["matcher"] is not None else {"hooks": []}
            pre.append(group)
        group.setdefault("hooks", []).append(item["hook"])


def heal(path=None, home=None):
    """Jedno przejście: opis zmiany albo None. settings.json pisze tylko wtedy, gdy trzeba."""
    p = paths(home)
    path = path or p["settings"]
    for _ in range(5):
        settings, stamp = hook.read_settings(path)
        if not settings or not isinstance(settings.get("hooks", {}), dict):
            return None  # brak pliku albo zepsuty JSON: nic tu nie naprawimy
        state = load_state(p)
        why = broken(settings, p)
        found = admit_entries(settings)
        if why is None:
            if not found:
                if not state.get("chain_seen"):
                    save_state(p, dict(state, chain_seen=True))
                return None
            removed = take_out(settings, found)
            if not hook.write_settings(path, settings, stamp):
                continue
            save_state(p, {"chain_seen": True, "removed": state.get("removed", []) + removed})
            return "admit idzie przez łańcuch fasthooks: wpis claude-acc zdjęty z settings.json"
        if found:
            if state:
                save_state(p, {})  # nasz wpis jest na miejscu: zwykły stan
            return None
        if not state.get("chain_seen"):
            return None
        put_back(settings, state.get("removed") or [default_entry(p)])
        if not hook.write_settings(path, settings, stamp):
            continue
        save_state(p, {})
        return f"łańcuch fasthooks nie działa ({why}): wpis admit claude-acc wrócił do settings.json"
    return None


def status(path=None, home=None):
    p = paths(home)
    path = path or p["settings"]
    settings, _ = hook.read_settings(path)
    if settings is None:
        print(f"{path} nie jest poprawnym JSON-em")
        return 1
    why = broken(settings, p)
    print(f"łańcuch fasthooks: {'działa' if why is None else why}")
    print(f"wpis admit claude-acc: {'jest' if admit_entries(settings) else 'nie ma'}")
    state = load_state(p)
    if state.get("chain_seen"):
        print("łańcuch był widziany: gdy przestanie działać, wpis admit wróci")
    return 0


def main(argv):
    cmd = argv[0] if argv else "status"
    path = argv[1] if len(argv) > 1 else None
    if cmd == "heal":
        change = heal(path)
        if change:
            print(change)
        return 0
    if cmd == "status":
        return status(path)
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
