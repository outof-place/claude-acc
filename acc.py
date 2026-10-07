"""Runs one of the claude-acc scripts as __main__ from cached bytecode.

    acc.py <script> [args...]     e.g. acc.py accswitch status --json

Python compiles the script it is given on every start and caches only what it imports: for
accswitch, sched, perf and janitor that is 7-15 ms of every run (a quarter to a third of a
`status --json` the menu bar app asks for every minute). Loaded through an import loader,
the script's bytecode lands in __pycache__ once and every later start reads it from there.
The script still runs as __main__ with its own path in __file__ and sys.argv[0], so it
finds the files next to it exactly as when started directly.
"""

import os
import sys
from importlib.machinery import SourceFileLoader

SCRIPTS = ("accswitch", "browser", "devguard", "hint", "janitor", "mail", "mailhint", "perf", "sched", "updates")
# dawna nazwa skryptu, która może jeszcze stać we wpisie hooka w settings.json
ALIASES = {"mailhint": "hint"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in SCRIPTS:
        print("usage: acc.py {" + ",".join(SCRIPTS) + "} [args...]", file=sys.stderr)
        return 2
    path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), ALIASES.get(sys.argv[1], sys.argv[1]) + ".py"
    )
    # the loader caches by file path, so the name the module runs under doesn't matter;
    # importlib.util would cost 4 ms more on Python 3.9 (it pulls in typing)
    loader = SourceFileLoader("__main__", path)
    module = type(sys)("__main__")
    module.__file__ = path
    module.__loader__ = loader
    sys.argv = [path] + sys.argv[2:]
    sys.modules["__main__"] = module
    loader.exec_module(module)
    return 0


if __name__ == "__main__":
    sys.exit(main())
