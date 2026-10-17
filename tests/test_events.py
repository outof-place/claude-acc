"""Rejestr decyzji (events.py): wiersz z zegarem ściennym, uptime bez snu i rozruchem, zapis z dwóch
interpreterów naraz bez porwanych linii, własna rotacja i miejsce w $STATE, poza ~/Library/Logs.

Pomyłki, które łapie: zegar z time.monotonic() (na /usr/bin/python3 3.9.6 liczy od startu procesu,
więc plock i hak zapisałyby niezgodne czasy), dwa procesy, które przeplatają sobie linie, rejestr
obcinany w miejscu jak devguard.log (janitor zostawia ostatnie 1000 linii) i log hamulca w
~/Library/Logs, który mole kasuje (clean/user.sh:309; tak zniknął log memory-guard 2026-10-13 i 10-17).

Uruchomienie: python3 -m unittest tests.test_events
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import events  # noqa: E402

SYSTEM_PYTHON = "/usr/bin/python3"


def kern_boottime():
    """Sekunda rozruchu z `sysctl -n kern.boottime` (niezależnie od ctypes w events.py)."""
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True).stdout
    return int(re.search(r"sec = (\d+)", out).group(1))


def rows(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


# pisarz w osobnym interpreterze: 500 wierszy z numerem i balastem, start na wspólny sygnał (plik)
WRITER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
import events
state, name, go = sys.argv[2], sys.argv[3], sys.argv[4]
while not os.path.exists(go):
    time.sleep(0.001)
for i in range(500):
    events.append(state, "test", writer=name, i=i, pad="x" * 300, python=sys.version.split()[0])
"""


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="events-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_row_has_wall_uptime_and_boot(self):
        before_up = time.clock_gettime(time.CLOCK_UPTIME_RAW)
        self.assertTrue(events.append(self.dir, "admit", job="j-1", how="fits"))
        (row,) = rows(os.path.join(self.dir, "events.jsonl"))
        self.assertEqual(list(row)[:4], ["ts", "up", "boot", "ev"])
        self.assertEqual((row["ev"], row["job"], row["how"]), ("admit", "j-1", "fits"))
        self.assertLess(abs(row["ts"] - time.time()), 5)
        # uptime bez snu, a nie czas od startu tego procesu
        self.assertGreaterEqual(row["up"], before_up)
        self.assertLess(row["up"] - before_up, 5)
        self.assertEqual(row["boot"], kern_boottime())
        # pole wołającego nie nadpisze zegara
        events.append(self.dir, "x", up=1, ts=2)
        last = rows(os.path.join(self.dir, "events.jsonl"))[-1]
        self.assertGreater(last["up"], 100)
        self.assertGreater(last["ts"], 1e9)
        # błąd zapisu nie przerywa wołającego: katalog, którego nie da się założyć
        blocked = os.path.join(self.dir, "file")
        open(blocked, "w").close()
        self.assertFalse(events.append(os.path.join(blocked, "state"), "x"))

    def test_two_interpreters_append_whole_lines(self):
        if not os.access(SYSTEM_PYTHON, os.X_OK):
            self.skipTest("brak /usr/bin/python3")
        go = os.path.join(self.dir, "go")
        procs = [
            subprocess.Popen([python, "-I", "-c", WRITER, ROOT, self.dir, name, go])
            for python, name in ((sys.executable, "hook"), (SYSTEM_PYTHON, "plock"))
        ]
        time.sleep(0.5)
        open(go, "w").close()
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        with open(os.path.join(self.dir, "events.jsonl")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1000)
        got = [json.loads(line) for line in lines]  # każda linia to cały JSON
        for name in ("hook", "plock"):
            self.assertEqual([r["i"] for r in got if r["writer"] == name], list(range(500)), name)
        self.assertEqual({r["python"][:3] for r in got if r["writer"] == "plock"}, {"3.9"})
        # oba interpretery liczą ten sam uptime (systemowy), więc wiersze idą po nim w kolejności zapisu
        ups = [r["up"] for r in got]
        self.assertEqual(ups, sorted(ups))

    def test_rotates_at_size_keeps_one_previous(self):
        path = os.path.join(self.dir, "events.jsonl")
        with mock.patch.object(events, "ROTATE_BYTES", 2000):
            for i in range(30):
                events.append(self.dir, "test", i=i, pad="y" * 100)
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertFalse(os.path.exists(path + ".2"))
        current, previous = rows(path), rows(path + ".1")
        self.assertLess(os.path.getsize(path), 2000 + 200)
        # najnowszy wiersz w bieżącym pliku, poprzedni plik tuż przed nim, najstarsze odpadły
        self.assertEqual(current[-1]["i"], 29)
        self.assertEqual(previous[-1]["i"] + 1, current[0]["i"])
        self.assertGreater(previous[0]["i"], 0)
        self.assertEqual(events.ROTATE_BYTES, 32 * 1024 * 1024)

    def test_registry_lives_in_state_not_library_logs(self):
        home = os.path.join(self.dir, "home")
        os.makedirs(home)
        code = "import sys; sys.path.insert(0, sys.argv[1]); import events; print(events.EVENTS_PATH); events.append(events.STATE_DIR, 'x')"
        out = subprocess.run([SYSTEM_PYTHON, "-I", "-c", code, ROOT], env={"HOME": home, "PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True, check=True).stdout.strip()
        state = os.path.join(home, ".local/share/claude-acc")
        self.assertEqual(out, os.path.join(state, "events.jsonl"))
        self.assertEqual([r["ev"] for r in rows(out)], ["x"])
        self.assertFalse(os.path.exists(os.path.join(home, "Library")))
        self.assertTrue(os.path.exists(os.path.join(state, "events.lock")))

    def test_brake_logs_live_outside_library_logs(self):
        """Logi hamulców i rejestr leżą w $STATE: mole czyści każdy wpis pierwszego poziomu w
        ~/Library/Logs (clean/user.sh:309), a ~/.local/share/claude-acc nie rusza."""
        import devguard_core

        home = os.path.expanduser("~")
        state = os.path.join(home, ".local/share/claude-acc") + "/"
        paths = {"events": events.EVENTS_PATH, "devguard.log": devguard_core.LOG_PATH}
        for name in ("devguard", "jobs"):
            template = os.path.join(ROOT, "launchd", f"com.filip.claude-acc.{name}.plist.template")
            with open(template) as f:
                plist = plistlib.loads(f.read().replace("__HOME__", home).encode())
            paths[f"{name} stdout"] = plist["StandardOutPath"]
            paths[f"{name} stderr"] = plist["StandardErrorPath"]
        for name, path in paths.items():
            self.assertTrue(path.startswith(state), (name, path))
            self.assertNotIn("/Library/Logs", path, name)


if __name__ == "__main__":
    unittest.main()
