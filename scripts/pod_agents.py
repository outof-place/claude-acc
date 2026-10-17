#!/usr/bin/env python3
"""Agenty launchd claude-acc dla Pod (SMAppService) z tych samych szablonów, które setup.sh kładzie
w ~/Library/LaunchAgents.

  scripts/pod_agents.py --out DIR [--app-id codes.pod.app] [--program Contents/Resources/claude-acc/pod-acc-run]

Agent z pakietu aplikacji nie zna ścieżek w HOME: zamiast `$STATE/python $STATE/acc.py <job>` startuje
BundleProgram (pod-acc-run w Pod.app), który bierze HOME z bazy kont, dopisuje wyjście do tego samego
logu w $STATE i uruchamia acc.py. Etykieta `<app-id>.acc.<job>` (tick, janitor, devguard, perf, updates,
jobs, admit), harmonogram, klasa procesu i reszta kluczy bez zmian; bez StandardOutPath/StandardErrorPath.
Lista jobów to JOBS z setup.sh, więc nowy automat trafia do Pod sam, i POD_JOBS: automaty, które ma
tylko Pod (setup.sh nie kładzie ich w ~/Library/LaunchAgents).
"""

import os
import plistlib
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY = "com.filip.claude-acc"
APP_ID = "codes.pod.app"
PROGRAM = "Contents/Resources/claude-acc/pod-acc-run"
HOME_MARK = "/__HOME__"
STATE_MARK = HOME_MARK + "/.local/share/claude-acc/"


def jobs(setup=None):
    """Etykiety z linii JOBS= i potem POD_JOBS= w setup.sh, w ich kolejności."""
    with open(setup or os.path.join(ROOT, "setup.sh"), encoding="utf-8") as f:
        lines = f.readlines()
    labels = []
    for name in ("JOBS=", "POD_JOBS="):
        line = next((x for x in lines if x.startswith(name)), None)
        if line is None and name == "JOBS=":
            raise ValueError("setup.sh: brak linii JOBS=")
        if line is not None:
            labels += line.split("=", 1)[1].strip().strip('"').split()
    return labels


def job_name(label):
    """com.filip.claude-acc to tick (accswitch tick), reszta to swój przyrostek."""
    return "tick" if label == LEGACY else label[len(LEGACY) + 1 :]


def convert(template_text, label, app_id=APP_ID, program=PROGRAM):
    """Szablon setup.sh (z __HOME__) na słownik plisty agenta Pod."""
    data = plistlib.loads(template_text.replace("__HOME__", HOME_MARK).encode("utf-8"))
    if data.get("Label") != label:
        raise ValueError(f"{label}: szablon ma etykietę {data.get('Label')!r}")
    args = data.pop("ProgramArguments")
    if args[:2] != [STATE_MARK + "python", STATE_MARK + "acc.py"]:
        raise ValueError(f"{label}: ProgramArguments nie startują $STATE/python $STATE/acc.py: {args[:2]}")
    logs = {data.pop("StandardOutPath", None), data.pop("StandardErrorPath", None)} - {None}
    if len(logs) != 1 or not next(iter(logs)).startswith(STATE_MARK):
        raise ValueError(f"{label}: wyjście ma iść do jednego logu w $STATE, jest {sorted(logs)}")
    log = next(iter(logs))[len(STATE_MARK) :]
    data["Label"] = f"{app_id}.acc.{job_name(label)}"
    data["BundleProgram"] = program
    data["ProgramArguments"] = [os.path.basename(program), "--log", log, *args[2:]]
    left = [k for k, v in data.items() if HOME_MARK in repr(v)]
    if left:
        raise ValueError(f"{label}: ścieżki w HOME zostały w {left}")
    return data


def write(out, app_id=APP_ID, program=PROGRAM, root=ROOT):
    os.makedirs(out, exist_ok=True)
    written = []
    for label in jobs(os.path.join(root, "setup.sh")):
        with open(os.path.join(root, "launchd", label + ".plist.template"), encoding="utf-8") as f:
            data = convert(f.read(), label, app_id, program)
        path = os.path.join(out, data["Label"] + ".plist")
        with open(path, "wb") as f:
            plistlib.dump(data, f, sort_keys=True)
        written.append(path)
    return written


def option(args, name, default):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def main(argv):
    out = option(argv, "--out", None)
    if not out:
        print(__doc__, file=sys.stderr)
        return 2
    for path in write(out, option(argv, "--app-id", APP_ID), option(argv, "--program", PROGRAM)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
