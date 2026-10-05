#!/usr/bin/env python3
"""Hook sesji Claude Code dla pauzy limitów claude-acc.

Gdy żadne konto nie ma zapasu, automat (accswitch.py tick) zapisuje plik pauzy.
Ten skrypt go tylko czyta i robi z niego punkt kontrolny w każdej sesji:

  post         PostToolUse: sesja i każdy subagent raz na epizod dostają polecenie,
               żeby dokończyć krok, zapisać stan i się zatrzymać
  agent        PreToolUse (Agent): nowi subagenci są wstrzymani do końca pauzy
  prompt       UserPromptSubmit: Twoja wiadomość zdejmuje pauzę w tej jednej sesji
  watch        Stop (asyncRewake): budzik, który wznawia wstrzymaną sesję, gdy pauza znika
  watch-wall   StopFailure rate_limit: budzik dla sesji, która uderzyła w limit, gdy
               zapas wraca przez przełączenie konta (reset tego samego konta wznawia
               już sam Claude Code)
  install / uninstall [settings.json]   dopisuje albo usuwa te hooki

Poza pauzą hook kosztuje dwa sprawdzenia plików w powłoce: Python startuje
tylko w trakcie pauzy. Ścieżkę pliku pauzy można nadpisać zmienną
CLAUDE_ACC_PAUSE_FILE (sesja testowa nie wstrzymuje wtedy pozostałych).
"""

import fcntl
import json
import os
import shutil
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
PAUSE_PATH = os.environ.get("CLAUDE_ACC_PAUSE_FILE") or os.path.join(HOME, ".local/share/claude-acc/pause.json")
BASE_DIR = os.path.dirname(PAUSE_PATH)
STATE_PATH = os.path.join(BASE_DIR, "state.json")
POLL = float(os.environ.get("CLAUDE_ACC_HOOK_POLL", "20"))
MAX_WAIT = 8 * 86400  # pauza tygodniowa trwa najwyżej kilka dni
# Claude Code ubija hook z asyncRewake po jego timeout (bez niego po 600 s), więc budzik
# dostaje limit dłuższy niż własne czekanie. Bez tego pauza dłuższa niż 10 min nikogo nie budziła.
WATCH_TIMEOUT = MAX_WAIT + 60
MARKER = "claude-acc/hook.py"  # po tym poznajemy własne wpisy w settings.json (perf.py też)
BACKUP_SUFFIX = ".bak-claude-acc"


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clock(epoch):
    moment = datetime.fromtimestamp(epoch)
    return f"{moment:%H:%M}" if moment.date() == datetime.now().date() else f"{moment.day}.{moment:%m %H:%M}"


def until(pause):
    return f" Zapas wróci ok. {clock(pause['resume_at'])}." if pause.get("resume_at") else ""


# ---------- znaczniki epizodu ----------
#
# Plik na znacznik, tworzony z O_EXCL: kilka hooków naraz nie potrzebuje blokady,
# a pierwszy, któremu się uda, wysyła polecenie. Automat kasuje katalog po pauzie.

def marks_dir(pause):
    return os.path.join(BASE_DIR, "pause-marks", str(pause["episode"]))


def mark(pause, name):
    """True, gdy znacznik powstał teraz (pierwszy raz w tym epizodzie)."""
    os.makedirs(marks_dir(pause), exist_ok=True)
    try:
        os.close(os.open(os.path.join(marks_dir(pause), name), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def marked(pause, name):
    return os.path.exists(os.path.join(marks_dir(pause), name))


def notified_anyone(pause, session):
    """Czy ktoś w tej sesji (ona sama albo jej subagent) dostał polecenie zatrzymania."""
    try:
        names = os.listdir(marks_dir(pause))
    except OSError:
        return False
    return any(n.startswith(f"{session}__") and n.endswith(".notified") for n in names)


def safe(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(text))[:120]


# ---------- polecenia dla modelu ----------

def checkpoint_main(pause):
    return (
        "[claude-acc] Pauza limitów: wszystkie konta Claude są prawie wyczerpane i nie ma "
        f"konta, na które da się przełączyć.{until(pause)} Doprowadź pracę do punktu "
        "kontrolnego i zatrzymaj się, zamiast pracować do ściany:\n"
        "1. Dokończ tylko bieżący mały krok. Nie zaczynaj nowych kroków.\n"
        "2. Nie odpalaj nowych subagentów ani workflow (są wstrzymane). Działający subagenci "
        "dostali to samo polecenie i wrócą z raportem, poczekaj na nich.\n"
        "3. Zapisz stan w TASKS.md w katalogu głównym repo: co zrobione, co w toku, dokładny "
        "następny krok i ID przerwanych subagentów, żeby wznowić ich przez SendMessage.\n"
        "4. Zakończ turę krótkim podsumowaniem. Gdy limity wrócą, sesja zostanie obudzona "
        "automatycznie i dostanie polecenie wznowienia."
    )


def checkpoint_agent(pause):
    return (
        "[claude-acc] Pauza limitów: konta Claude są prawie wyczerpane. Zatrzymaj pracę w "
        "spójnym miejscu: dokończ bieżący mały krok, nic nowego nie zaczynaj i nie zostawiaj "
        "pliku w połowie edycji. Zakończ teraz z raportem: co zrobione, co zostało i dokładny "
        "następny krok. Agent nadrzędny wznowi Cię przez SendMessage z zachowanym kontekstem."
    )


DENY = (
    "[claude-acc] Pauza limitów: nowi subagenci są wstrzymani, bo żadne konto nie ma zapasu.{until} "
    "Doprowadź pracę do punktu kontrolnego (TASKS.md) i zakończ turę. Sesja zostanie obudzona "
    "automatycznie, gdy limity wrócą."
)

WAKE = (
    "[claude-acc] Limity wróciły, pauza się skończyła. Wznów przerwaną pracę od punktu "
    "kontrolnego: sprawdź TASKS.md, a przerwanych subagentów kontynuuj przez SendMessage z ich "
    "ID zamiast odpalać nowych. Jeśli praca była już skończona, krótko to potwierdź."
)

WAKE_SWITCH = (
    "[claude-acc] Zapas wrócił na innym koncie, na które automat właśnie przełączył. Kontynuuj "
    "zadanie przerwane przez limit od miejsca, w którym stanęło."
)


# ---------- tryby ----------

def emit(event, **fields):
    print(json.dumps({"hookSpecificOutput": dict(hookEventName=event, **fields)}, ensure_ascii=False))


def run_post(data, pause):
    session = safe(data.get("session_id"))
    agent = data.get("agent_id")
    if marked(pause, f"{session}.override"):
        return 0
    if not mark(pause, f"{session}__{safe(agent) if agent else 'main'}.notified"):
        return 0
    emit("PostToolUse", additionalContext=checkpoint_agent(pause) if agent else checkpoint_main(pause))
    return 0


def run_agent(data, pause):
    if marked(pause, f"{safe(data.get('session_id'))}.override"):
        return 0
    emit("PreToolUse", permissionDecision="deny", permissionDecisionReason=DENY.format(until=until(pause)))
    return 0


# Wiadomości, które Claude Code sam wkłada do sesji i przepuszcza przez
# UserPromptSubmit: raport skończonego subagenta, wznowienie po limicie, nasz budzik.
# To nie Ty piszesz, więc nie zdejmują pauzy.
SYNTHETIC = ("<task-notification>", "<system-reminder>", "Your claude.ai usage limit", "[claude-acc]")


def run_prompt(data, pause):
    if (data.get("prompt") or "").lstrip().startswith(SYNTHETIC):
        return 0
    session = safe(data.get("session_id"))
    if mark(pause, f"{session}.override") and marked(pause, f"{session}__main.notified"):
        # wcześniej sesja dostała polecenie zatrzymania: Twoja wiadomość je odwołuje
        print("[claude-acc] Użytkownik pisze w trakcie pauzy limitów, więc pauza nie dotyczy już "
              "tej sesji: pracuj normalnie i możesz znowu odpalać subagentów.")
    return 0


def resumed(transcript, offset):
    """Czy sesja ruszyła sama (Twoja wiadomość, wznowienie przez Claude Code)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(offset)
            return b'"type":"user"' in f.read()
    except OSError:
        return False


def run_watch(data, wall):
    """Budzik w tle. Kod 2 i tekst na stderr budzą sesję (asyncRewake)."""
    if data.get("agent_id"):
        return 0
    session = safe(data.get("session_id"))
    pause = load(PAUSE_PATH)
    if not wall and (not pause or not notified_anyone(pause, session)
                     or marked(pause, f"{session}.override")):
        return 0  # sesja nie była w trakcie pracy wstrzymanej przez pauzę
    os.makedirs(os.path.join(BASE_DIR, "pause-watchers"), exist_ok=True)
    lock = open(os.path.join(BASE_DIR, "pause-watchers", f"{session}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # ta sesja ma już budzik
    transcript = data.get("transcript_path") or ""
    offset = os.path.getsize(transcript) if os.path.exists(transcript) else 0
    started = time.time()
    episode = pause and pause.get("episode")
    switched_at = (load(STATE_PATH) or {}).get("switched_at", 0)
    while time.time() - started < MAX_WAIT:
        time.sleep(POLL)
        if os.getppid() == 1:
            return 0  # sesja się zamknęła
        if resumed(transcript, offset):
            return 0
        current = load(PAUSE_PATH)
        if current and not episode:
            episode = current.get("episode")  # limit trafił, zanim automat ogłosił pauzę
        if current and current.get("episode") == episode:
            if marked(current, f"{session}.override"):
                return 0
            continue
        if episode:  # pauza się skończyła (plik zniknął albo zaczął się nowy epizod)
            print(WAKE, file=sys.stderr)
            return 2
        if wall and (load(STATE_PATH) or {}).get("switched_at", 0) > switched_at:
            print(WAKE_SWITCH, file=sys.stderr)
            return 2
    return 0


# ---------- instalacja ----------

SCRIPT = f"$HOME/.local/share/{MARKER}"


def guard(mode):
    """Polecenie hooka: poza pauzą tylko testy plików w powłoce, bez startu Pythona.
    Bez skryptu (usunięty katalog stanu) też nic: python3 z brakującym plikiem kończy
    się kodem 2, a to dla Claude Code blokada narzędzia albo fałszywa pobudka."""
    return (f'f="${{CLAUDE_ACC_PAUSE_FILE:-$HOME/.local/share/claude-acc/pause.json}}"; h="{SCRIPT}"; '
            f'if [ -e "$f" ] && [ -e "$h" ]; then exec /usr/bin/python3 "$h" {mode}; fi; '
            'cat >/dev/null')


def entries():
    always = f'h="{SCRIPT}"; if [ -e "$h" ]; then exec /usr/bin/python3 "$h" watch-wall; fi; cat >/dev/null'
    return {
        "PostToolUse": {"matcher": "*", "hooks": [{"type": "command", "command": guard("post"), "timeout": 10}]},
        "PreToolUse": {"matcher": "Agent|Task", "hooks": [{"type": "command", "command": guard("agent"), "timeout": 10}]},
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": guard("prompt"), "timeout": 10}]},
        "Stop": {"hooks": [{"type": "command", "command": guard("watch"), "asyncRewake": True,
                            "timeout": WATCH_TIMEOUT}]},
        "StopFailure": {"matcher": "rate_limit", "hooks": [{"type": "command", "command": always, "asyncRewake": True,
                                                            "timeout": WATCH_TIMEOUT}]},
    }


def ours(hook):
    return MARKER in (hook.get("command") or "")


def strip(settings):
    """Usuwa nasze hooki i tylko te: cudzy hook w tej samej grupie zostaje, a zdarzenie
    albo całe "hooks" znika tylko wtedy, gdy opróżniło się przez nas."""
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = hooks[event] or []
        kept = []
        for group in groups:
            rest = [h for h in group.get("hooks") or [] if not ours(h)]
            if len(rest) == len(group.get("hooks") or []):
                kept.append(group)
            elif rest:
                kept.append(dict(group, hooks=rest))
        if len(kept) == len(groups) and all(a is b for a, b in zip(kept, groups)):
            continue
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
            if not hooks:
                del settings["hooks"]


def read_settings(path):
    """(ustawienia, znacznik czasu zapisu); brak pliku to ({}, None), zepsuty JSON to (None, ...)."""
    try:
        stamp = os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return {}, None
    settings = load(path)
    return (settings if isinstance(settings, dict) else None), stamp


def write_settings(path, settings, stamp):
    """Zapis atomowy z tym samym trybem pliku i końcem linii co przedtem. False, gdy
    ktoś zapisał plik w międzyczasie (Claude Code, perf.py): wtedy zaczynamy od nowa."""
    tmp = f"{path}.{os.getpid()}.tmp"
    newline = True
    if stamp is not None:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                newline = f.read(1) == b"\n"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        if newline:
            f.write("\n")
    if stamp is not None:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    current = os.stat(path).st_mtime_ns if os.path.exists(path) else None
    if current != stamp:
        os.remove(tmp)
        return False
    os.replace(tmp, path)
    return True


def run_install(args, add):
    path = args[0] if args else os.path.join(HOME, ".claude/settings.json")
    for _ in range(5):
        settings, stamp = read_settings(path)
        if settings is None or not isinstance(settings.get("hooks", {}), dict):
            print(f"{path} nie jest poprawnym JSON-em ustawień, nic nie zmieniam", file=sys.stderr)
            return 1
        before = json.dumps(settings, sort_keys=False)
        strip(settings)
        if add:
            hooks = settings.setdefault("hooks", {})
            for event, group in entries().items():
                hooks.setdefault(event, []).append(group)
        if json.dumps(settings, sort_keys=False) == before:
            # także uninstall bez pliku: nie zakładamy pustego settings.json
            print(f"hooki pauzy limitów w {path} bez zmian")
            return 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # kopia sprzed pierwszej zmiany: kolejne instalacje jej nie nadpisują
        if stamp is not None and not os.path.exists(path + BACKUP_SUFFIX):
            shutil.copy2(path, path + BACKUP_SUFFIX)
        if write_settings(path, settings, stamp):
            print(f"{'dopisano' if add else 'usunięto'} hooki pauzy limitów w {path}")
            return 0
    print(f"{path} zmienia się bez przerwy, spróbuj później", file=sys.stderr)
    return 1


def main(argv):
    mode = argv[0] if argv else ""
    if mode in ("install", "uninstall"):
        return run_install(argv[1:], mode == "install")
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if os.environ.get("CLAUDE_ACC_HOOK_LOG"):  # diagnostyka: co i kiedy odpala Claude Code
        with open(os.environ["CLAUDE_ACC_HOOK_LOG"], "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "mode": mode, "agent": data.get("agent_id"),
                                "prompt": (data.get("prompt") or "")[:120]}, ensure_ascii=False) + "\n")
    if mode in ("watch", "watch-wall"):
        return run_watch(data, wall=mode == "watch-wall")
    pause = load(PAUSE_PATH)
    if not pause or "episode" not in pause:
        return 0
    handler = {"post": run_post, "agent": run_agent, "prompt": run_prompt}.get(mode)
    return handler(data, pause) if handler else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as err:  # hook nigdy nie może zablokować sesji własnym błędem
        print(f"claude-acc hook: {err}", file=sys.stderr)
        sys.exit(0)
