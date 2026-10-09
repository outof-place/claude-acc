#!/usr/bin/env python3
"""Stay Awake aplikacji w pasku menu z zewnątrz: z terminala, skryptu albo wtyczki Orki.

  awake.py status [--json]          stan z awake-state.json, który aplikacja pisze przy każdej zmianie
  awake.py on [--for 2h|90m|3600]   trzymaj Maca na nogach (bez --for: do wyłączenia)
  awake.py off                      wyłącz (włączone samo na hotspocie wraca przy następnym hotspocie)
  awake.py toggle [--for ...]

on, off i toggle otwierają w tle claude-acc://awake/<on|off|toggle>[?for=sekundy] (`open -g`, jak
`claude-acc dictate`); aplikacja wstaje, jeśli nie biegnie. Asercje zasilania trzyma tylko jej
proces, więc stan bez żywego pid aplikacji to "wyłączone". Komenda czeka do 4 s na potwierdzenie
w pliku stanu.
"""

import json
import os
import re
import subprocess
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local/share/claude-acc")
STATE_PATH = os.path.join(STATE_DIR, "awake-state.json")
CONFIRM_S = 4.0
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    """Sekundy z "3600", "90m", "2h", "1h30m"; None dla złego zapisu albo zera."""
    text = (text or "").strip().lower()
    if re.fullmatch(r"\d+", text):
        seconds = int(text)
    else:
        parts = re.findall(r"(\d+)([smhd])", text)
        if not parts or "".join(n + u for n, u in parts) != text:
            return None
        seconds = sum(int(n) * UNITS[u] for n, u in parts)
    return seconds or None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state():
    """Stan z pliku aplikacji; `running` mówi, czy jej proces żyje (bez niego nic nie trzyma Maca)."""
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"on": False, "running": False, "known": False}
    state["known"] = True
    state["running"] = alive(state.get("pid"))
    if not state["running"]:
        state.update(on=False, manual=False, hotspot=False)
    return state


def describe(state):
    if not state["known"]:
        return "Stay Awake: brak stanu (aplikacja Claude Acc jeszcze go nie zapisała)"
    if not state["running"]:
        return "Stay Awake: wyłączone (aplikacja Claude Acc nie działa)"
    if state.get("manual"):
        if state.get("forever"):
            text = "włączone do wyłączenia"
        else:
            left = max(0, int((state.get("until") or 0) - time.time()))
            text = f"włączone jeszcze {left // 3600} h {left % 3600 // 60} min"
        if state.get("lid_closed"):
            text += ", także z zamkniętą klapą"
    elif state.get("hotspot"):
        text = "włączone samo: Mac jest na hotspocie" + (f" ({state['via']})" if state.get("via") else "")
    else:
        text = "wyłączone" + (", włączy się samo na hotspocie" if state.get("auto_on_hotspot") else "")
    return "Stay Awake: " + text


def send(verb, seconds=None):
    url = f"claude-acc://awake/{verb}" + (f"?for={seconds}" if seconds else "")
    return subprocess.run(["open", "-g", url], capture_output=True, text=True)


def confirmed(verb, before, want):
    """Czy plik stanu pokazał zmianę: nowy zapis albo już taki stan, o jaki chodzi."""
    end = time.time() + CONFIRM_S
    while True:
        state = read_state()
        fresh = state.get("updated_at") != before.get("updated_at") or verb != "toggle"
        if state["running"] and fresh and (want is None or state["on"] == want):
            return state
        if time.time() >= end:
            return None
        time.sleep(0.1)


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    verb, rest = argv[0], argv[1:]
    if verb == "status":
        state = read_state()
        print(json.dumps(state, ensure_ascii=False) if "--json" in rest else describe(state))
        return 0
    if verb not in ("on", "off", "toggle"):
        print(__doc__, file=sys.stderr)
        return 2
    seconds = None
    if "--for" in rest:
        i = rest.index("--for")
        seconds = parse_duration(rest[i + 1] if i + 1 < len(rest) else "")
        if seconds is None:
            print("--for: liczba sekund albo np. 90m, 2h, 1h30m", file=sys.stderr)
            return 2
    before = read_state()
    want = {"on": True, "off": False}.get(verb, not before["on"])
    done = send(verb, seconds if verb != "off" else None)
    if done.returncode != 0:
        print(f"open claude-acc://awake/{verb}: {(done.stderr or done.stdout).strip()}", file=sys.stderr)
        return 1
    state = confirmed(verb, before, want)
    if state is None:
        print("aplikacja Claude Acc nie potwierdziła zmiany w 4 s (działa? claude-acc awake status)", file=sys.stderr)
        return 1
    print(describe(state))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
