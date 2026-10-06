#!/usr/bin/env python3
"""Aktualizacje narzędzi na Macu: Homebrew, globalne paczki npm i programy Go co 3 dni.

launchd uruchamia `updates.py run` codziennie o 4:30 (uśpiony Mac nadrabia po obudzeniu);
przebieg rusza, gdy od poprzedniego minęły 3 dni, a po nieudanym próbuje znowu następnej
nocy. Przycisk Update w panelu i `claude-acc update` uruchamiają go od razu.

Co aktualizuje, do najnowszej wersji:
- Homebrew: `brew update`, potem formuły i aplikacje (casks). Bez --greedy: aplikacje
  z własnym aktualizatorem (Chrome, Claude) robią to same. Przypięte (`brew pin`) zostają;
- npm: globalne paczki, także o wersję główną wyżej. `npm_pins` w updates.json trzyma
  paczkę w jednej wersji głównej, np. {"pnpm": "11"};
- Go: programy postawione przez `go install` w GOBIN.
Python tylko sprawdza: globalne paczki pip dzielą zależności, a hurtowe podbicie potrafi po
cichu zepsuć inne narzędzia, więc panel pokazuje, co jest do zrobienia, a robi to człowiek.

Każda paczka idzie osobno albo jest sprawdzana po fakcie, więc jedna nieudana (np. aplikacja,
która chce hasła administratora) nie zatrzymuje reszty i w panelu widać, która to była.
Stan dla panelu: updates-state.json; pełne wyjście poleceń: updates.log.

Komendy:
  run [--force] [--dry-run] [--only brew,npm,go,pip]
        --force pomija odstęp 3 dni (przycisk w panelu), --dry-run tylko wypisuje plan,
        --only zawęża przebieg i nie przesuwa terminu następnego
  status [--json]
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
CONFIG_PATH = os.path.join(STATE_DIR, "updates.json")
STATE_PATH = os.path.join(STATE_DIR, "updates-state.json")
LOG_PATH = os.path.join(STATE_DIR, "updates.log")
LOCK_PATH = os.path.join(STATE_DIR, "updates.lock")
LOG_MAX_BYTES = 1024 * 1024

HOUR = 3600
DAY = 24 * HOUR
# godzina z launchd/com.filip.claude-acc.updates.plist.template; stąd panel zna termin następnego
RUN_AT = (4, 30)

DEFAULT_CONFIG = {
    "every_days": 3,
    # po nieudanym przebiegu: następnej nocy, nie za 3 dni
    "retry_hours": 20,
    # kroki do pominięcia: brew, npm, go, pip
    "skip": [],
    # paczka npm -> zakres, w którym ma zostać ("11" to każda 11.x)
    "npm_pins": {},
    # Python, którego paczki sprawdzać; domyślnie python.org, potem Homebrew
    "python": None,
}


def tool_path():
    """PATH dla narzędzi: launchd daje procesowi tylko /usr/bin:/bin:/usr/sbin:/sbin."""
    if os.environ.get("CLAUDE_ACC_TOOL_PATH"):  # testy: atrapy brew, npm, go
        return os.environ["CLAUDE_ACC_TOOL_PATH"]
    dirs = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(d for d in dirs if os.path.isdir(d))


ENV = dict(
    os.environ,
    PATH=tool_path(),
    HOMEBREW_NO_AUTO_UPDATE="1",
    HOMEBREW_NO_ENV_HINTS="1",
    HOMEBREW_NO_ANALYTICS="1",
    npm_config_update_notifier="false",
    npm_config_fund="false",
    npm_config_audit="false",
)


# ---------- drobne narzędzia ----------


def log(line):
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
        with open(LOG_PATH) as f:
            tail = f.readlines()[-3000:]
        with open(LOG_PATH, "w") as f:
            f.writelines(tail)
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


def notify(title, text):
    subprocess.run(
        ["osascript", "-e", f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
        capture_output=True,
        env=ENV,
    )


def write_json(path, data):
    """Zapis przez plik tymczasowy: przerwany proces nie zostawi uciętego JSON-a."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def which(name):
    return shutil.which(name, path=ENV["PATH"])


# npm pisze kilka linii "npm error"; kod i ścieżka do logu nic nie mówią człowiekowi
NOISE = re.compile(r"npm (error|ERR!) (code|errno|syscall|A complete log|Log files)|npm (error|ERR!)\s*$")
ERROR = re.compile(r"^(Error|npm error|npm ERR!|go: |fatal|ERROR)")


def error_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if ERROR.match(line) and not NOISE.match(line):
            return line[:300]
    return (lines[-1] if lines else "unknown error")[:300]


def call(cmd, timeout=600, codes=(0,), quiet=False):
    """(udało się, stdout, opis błędu). Polecenia, które coś zmieniają, trafiają do logu w całości."""
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=ENV, cwd=HOME, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        log(f"$ {' '.join(cmd)}: przekroczony czas {timeout} s")
        return False, "", f"timed out after {timeout // 60} min"
    except OSError as err:
        return False, "", str(err)
    ok = done.returncode in codes
    if not quiet or not ok:
        output = (done.stdout + done.stderr).strip()
        log(f"$ {' '.join(cmd)}  (kod {done.returncode})" + (f"\n{output}" if output else ""))
    return ok, done.stdout, "" if ok else error_line(done.stderr + "\n" + done.stdout)


def version_key(version):
    """1.10.2 > 1.9.9; v-przedrostek i część po myślniku nie liczą się."""
    return [int(n) for n in re.findall(r"\d+", version.lstrip("v").split("-")[0])[:3]]


class StepError(Exception):
    pass


class Step:
    def __init__(self, name, label):
        self.name, self.label = name, label
        self.updated, self.failed, self.held, self.outdated = [], [], [], []
        self.error = None
        self.report_only = False

    def to_json(self):
        data = {
            "name": self.name,
            "label": self.label,
            "ok": self.error is None and not self.failed,
            "updated": self.updated,
            "failed": self.failed,
            "held": self.held,
        }
        if self.report_only:
            data["report_only"] = True
            data["outdated"] = self.outdated
        if self.error:
            data["error"] = self.error
        return data


def pkg(name, old, new, **extra):
    return {"name": name, "from": old, "to": new, **extra}


# ---------- Homebrew ----------


def brew_outdated():
    ok, out, err = call(["brew", "outdated", "--json=v2"], timeout=300, quiet=True)
    if not ok:
        raise StepError(f"brew outdated: {err}")
    try:
        data = json.loads(out[out.find("{"):])
    except ValueError:
        raise StepError("brew outdated: unreadable output")
    found = {}
    for kind in ("formulae", "casks"):
        for item in data.get(kind, []):
            found[item["name"]] = {
                "kind": kind,
                "from": (item.get("installed_versions") or ["?"])[-1],
                "to": item.get("current_version") or "?",
                "pinned": bool(item.get("pinned")),
            }
    return found


def step_brew(st, cfg, dry):
    if not which("brew"):
        return False
    ok, _, err = call(["brew", "update", "--quiet"], timeout=900)
    if not ok:
        st.error = f"brew update: {err}"
        return True
    before = brew_outdated()
    short = lambda name: name.rsplit("/", 1)[-1]  # facebook/fb/idb-companion
    st.held = [pkg(short(n), i["from"], i["to"]) for n, i in before.items() if i["pinned"]]
    todo = {n: i for n, i in before.items() if not i["pinned"]}
    if dry:
        st.updated = [pkg(short(n), i["from"], i["to"]) for n, i in todo.items()]
        return True
    errors = {}
    formulae = [n for n, i in todo.items() if i["kind"] == "formulae"]
    if formulae:
        ok, _, err = call(["brew", "upgrade", "--formula", *formulae], timeout=2 * HOUR)
        errors.update({n: err for n in formulae if not ok})
    # aplikacje po jednej: ta, która chce hasła administratora, nie blokuje reszty
    for name in [n for n, i in todo.items() if i["kind"] == "casks"]:
        ok, _, err = call(["brew", "upgrade", "--cask", name], timeout=HOUR)
        if not ok:
            errors[name] = err
    try:
        after = brew_outdated()
    except StepError:
        after = errors  # bez drugiego odczytu wierzymy kodom wyjścia
    for name, item in todo.items():
        if name in after:
            error = errors.get(name) or "still outdated after brew upgrade"
            entry = pkg(short(name), item["from"], item["to"], error=error)
            # instalator aplikacji chce hasła administratora, a w tle nie ma kogo zapytać
            if "sudo" in error:
                kind = "cask" if item["kind"] == "casks" else "formula"
                entry.update(admin=True, retry=f"brew upgrade --{kind} {name}")
            st.failed.append(entry)
        else:
            st.updated.append(pkg(short(name), item["from"], item["to"]))
    return True


# ---------- npm ----------


def npm_outdated():
    # npm outdated kończy się kodem 1, gdy jest co aktualizować
    ok, out, err = call(["npm", "outdated", "-g", "--json"], timeout=300, codes=(0, 1), quiet=True)
    try:
        data = json.loads(out or "{}")
    except ValueError:
        raise StepError(f"npm outdated: {err or 'unreadable output'}")
    if not ok or isinstance(data.get("error"), dict):
        detail = data.get("error", {}).get("summary") if isinstance(data.get("error"), dict) else err
        raise StepError(f"npm outdated: {detail}")
    return {n: i for n, i in data.items() if isinstance(i, dict) and i.get("current") and i.get("latest")}


def npm_newest(name, pin):
    """Najwyższa wersja w zakresie przypięcia albo None."""
    ok, out, _ = call(["npm", "view", f"{name}@{pin}", "version", "--json"], timeout=120, quiet=True)
    try:
        versions = json.loads(out) if ok and out.strip() else []
    except ValueError:
        return None
    versions = [versions] if isinstance(versions, str) else versions
    return max(versions, key=version_key) if versions else None


def step_npm(st, cfg, dry):
    if not which("npm"):
        return False
    before = npm_outdated()
    pins = cfg.get("npm_pins") or {}
    todo = []
    # npm na końcu: podmienia samego siebie
    for name, info in sorted(before.items(), key=lambda kv: (kv[0] == "npm", kv[0])):
        target = info["latest"]
        if name in pins:
            target = npm_newest(name, pins[name]) or info["current"]
            if target != info["latest"]:
                st.held.append(pkg(name, target, info["latest"], pin=pins[name]))
        if target != info["current"]:
            todo.append((name, info["current"], target))
    if dry:
        st.updated = [pkg(n, old, new) for n, old, new in todo]
        return True
    errors = {}
    for name, old, new in todo:
        ok, _, err = call(["npm", "install", "-g", f"{name}@{new}"], timeout=1200)
        if not ok:
            errors[name] = err
    try:
        after = npm_outdated() if todo else {}
    except StepError:
        after = None
    for name, old, new in todo:
        if after is None:
            now = old if name in errors else new
        else:  # nieobecna na liście = najnowsza
            now = after[name]["current"] if name in after else before[name]["latest"]
        if now == new:
            st.updated.append(pkg(name, old, new))
        else:
            st.failed.append(pkg(name, old, new, error=errors.get(name) or f"still {now} after npm install"))
    return True


# ---------- Go ----------


def go_bin_dir():
    ok, out, _ = call(["go", "env", "GOBIN", "GOPATH"], quiet=True)
    if not ok:
        return None
    gobin, gopath = (out.splitlines() + ["", ""])[:2]
    return gobin.strip() or os.path.join(gopath.strip().split(":")[0], "bin")


def go_binary(path):
    """Ścieżka pakietu, moduł i wersja z `go version -m`; None dla plików spoza `go install`."""
    ok, out, _ = call(["go", "version", "-m", path], quiet=True)
    if not ok:
        return None
    fields = {}
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if parts[0] in ("path", "mod") and len(parts) > 1:
            fields.setdefault(parts[0], parts[1:])
    mod = fields.get("mod", [])
    if "path" not in fields or len(mod) < 2 or mod[1] == "(devel)":
        return None
    return {"path": fields["path"][0], "module": mod[0], "version": mod[1]}


def step_go(st, cfg, dry):
    if not which("go"):
        return False
    root = go_bin_dir()
    if not root or not os.path.isdir(root):
        return False
    todo = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        info = go_binary(path)
        if not info:
            continue
        ok, out, err = call(["go", "list", "-m", "-json", f"{info['module']}@latest"], timeout=120, quiet=True)
        try:
            latest = json.loads(out)["Version"] if ok else None
        except (ValueError, KeyError):
            latest, err = None, "unreadable go list output"
        if not latest:
            st.failed.append(pkg(name, info["version"], "?", error=f"couldn't check: {err}"))
        elif latest != info["version"]:
            todo.append((name, path, info, latest))
    for name, path, info, latest in todo:
        if dry:
            st.updated.append(pkg(name, info["version"], latest))
            continue
        ok, _, err = call(["go", "install", f"{info['path']}@{latest}"], timeout=1800)
        now = (go_binary(path) or {}).get("version")
        if now == latest:
            st.updated.append(pkg(name, info["version"], latest))
        else:
            st.failed.append(pkg(name, info["version"], latest, error=err or f"still {now} after go install"))
    return True


# ---------- Python (tylko raport) ----------


def python_for_pip(cfg):
    if cfg.get("python"):
        return cfg["python"]
    # nie /usr/bin/python3: bez narzędzi Xcode wyskakuje okno instalatora
    for path in ("/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "/opt/homebrew/bin/python3"):
        if os.access(path, os.X_OK):
            return path
    return None


def step_pip(st, cfg, dry):
    python = python_for_pip(cfg)
    if not python:
        return False
    st.report_only = True
    ok, out, err = call(
        [python, "-m", "pip", "list", "--outdated", "--format=json", "--disable-pip-version-check"],
        timeout=600,
        quiet=True,
    )
    try:
        items = json.loads(out) if ok else None
    except ValueError:
        items, err = None, "unreadable pip output"
    if items is None:
        st.error = f"pip list: {err}"
        return True
    st.outdated = [pkg(i["name"], i.get("version"), i.get("latest_version")) for i in items]
    return True


STEPS = [
    ("brew", "Homebrew", step_brew),
    ("npm", "npm", step_npm),
    ("go", "Go", step_go),
    ("pip", "Python", step_pip),
]


# ---------- przebieg ----------


def take_lock():
    """Blokada na cały proces: zwolni ją system przy wyjściu, także po awarii."""
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def next_due(cfg, state):
    last = state.get("last_run")
    if not last:
        return 0
    wait = cfg["every_days"] * DAY - HOUR if last.get("ok") else cfg["retry_hours"] * HOUR
    return last["at"] + wait


def next_run(cfg, state):
    """Pierwsza 4:30, kiedy przebieg będzie już należny: tak go uruchomi launchd."""
    when = datetime.fromtimestamp(max(next_due(cfg, state), time.time()))
    slot = when.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
    if slot < when:
        slot += timedelta(days=1)
    return slot.timestamp()


def run_steps(cfg, only, dry):
    results = []
    for name, label, func in STEPS:
        if name in cfg["skip"] or (only and name not in only):
            continue
        st = Step(name, label)
        try:
            present = func(st, cfg, dry)
        except StepError as err:
            st.error, present = str(err), True
        except Exception as err:  # jeden menedżer paczek nie może zatrzymać reszty
            log(f"{name}: błąd {err!r}")
            st.error, present = f"{name}: {err}", True
        if present:
            results.append(st)
    return results


def cmd_run(cfg, args):
    force = "--force" in args
    dry = "--dry-run" in args
    only = set(args[args.index("--only") + 1].split(",")) if "--only" in args else None
    lock = take_lock()
    if lock is None:
        print("aktualizacja już trwa")
        return 0
    state = load_json(STATE_PATH, {})
    if not (force or dry or only) and time.time() < next_due(cfg, state):
        return 0

    started = time.time()
    if dry:
        print_plan(run_steps(cfg, only, dry=True))
        return 0
    # znacznik dla panelu; znika także wtedy, gdy przebieg padnie
    state["running_since"] = started
    write_json(STATE_PATH, state)
    log(f"=== aktualizacja ({'ręczna' if force else 'automatyczna'}{', tylko ' + ','.join(sorted(only)) if only else ''})")
    try:
        steps = [st.to_json() for st in run_steps(cfg, only, dry=False)]
    finally:
        current = load_json(STATE_PATH, {})
        if current.pop("running_since", None) is not None:
            write_json(STATE_PATH, current)

    state = load_json(STATE_PATH, {})
    done = {s["name"] for s in steps}
    state["steps"] = steps + [s for s in state.get("steps", []) if s["name"] not in done]
    state["steps"].sort(key=lambda s: [n for n, _, _ in STEPS].index(s["name"]))
    counted = [s for s in steps if not s.get("report_only")]
    updated = sum(len(s["updated"]) for s in counted)
    failures = [f"{f['name']} ({s['label']})" for s in counted for f in s["failed"]]
    failures += [s["label"] for s in counted if s.get("error")]
    ok = not failures
    if not only:  # przebieg zawężony nie przesuwa terminu dla reszty
        state["last_run"] = {
            "at": time.time(),
            "duration": round(time.time() - started),
            "trigger": "manual" if force else "auto",
            "ok": ok,
            "updated": updated,
            "failed": len(failures),
        }
        if ok:
            state["last_success"] = state["last_run"]["at"]
        state["next_run"] = next_run(cfg, state)
    # powiadomienie tylko o nowym problemie, nie co noc o tym samym; przy kliknięciu mówi panel
    if failures and sorted(failures) != state.get("notified") and not force:
        notify("Aktualizacje", f"Nie udało się: {', '.join(failures[:4])}. Szczegóły w panelu claude-acc.")
    state["notified"] = sorted(failures)
    write_json(STATE_PATH, state)
    summary = f"zaktualizowano {updated}" + (f", nie udało się {len(failures)}: {', '.join(failures)}" if failures else "")
    log(f"=== koniec: {summary} w {round(time.time() - started)} s")
    print(summary if updated or failures else "wszystko aktualne")
    return 0


def print_plan(steps):
    for st in steps:
        line = f"{st.label}: "
        if st.error:
            line += f"błąd: {st.error}"
        elif st.report_only:
            line += f"{len(st.outdated)} do aktualizacji ręcznie" if st.outdated else "aktualne"
        else:
            line += ", ".join(f"{p['name']} {p['from']} → {p['to']}" for p in st.updated) or "aktualne"
        if st.held:
            line += "; przytrzymane: " + ", ".join(f"{p['name']} {p['from']} (jest {p['to']})" for p in st.held)
        if st.failed:
            line += "; nie do sprawdzenia: " + ", ".join(f"{p['name']}: {p['error']}" for p in st.failed)
        print(line)


def cmd_status(cfg, args):
    state = load_json(STATE_PATH, {})
    if "--json" in args:
        print(json.dumps(state))
        return 0
    last = state.get("last_run")
    if not last:
        print("Aktualizacje jeszcze nie ruszyły")
    else:
        when = datetime.fromtimestamp(last["at"]).strftime("%Y-%m-%d %H:%M")
        print(f"Ostatnio: {when}, {'w porządku' if last['ok'] else 'z błędami'}, zaktualizowano {last['updated']}")
        if state.get("next_run"):
            print(f"Następnie: {datetime.fromtimestamp(state['next_run']):%Y-%m-%d %H:%M}")
    if state.get("running_since"):
        print("Aktualizacja trwa teraz")
    for s in state.get("steps", []):
        if s.get("report_only"):
            detail = f"{len(s.get('outdated', []))} do aktualizacji ręcznie"
        else:
            detail = f"zaktualizowano {len(s['updated'])}" + (f", nie udało się {len(s['failed'])}" if s["failed"] else "")
        print(f"  {s['label']}: {detail}" + (f", błąd: {s['error']}" if s.get("error") else ""))
        for f in s["failed"]:
            print(f"    ! {f['name']}: {f['error']}")
    return 0


COMMANDS = {"run": cmd_run, "status": cmd_status}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd not in COMMANDS:
        print(__doc__)
        return 2
    try:
        return COMMANDS[cmd](load_config(), argv[1:])
    except Exception as err:
        log(f"{cmd}: błąd {err!r}")
        print(f"błąd: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
