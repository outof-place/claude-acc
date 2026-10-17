"""Tests for scripts/measure_panel.py: the replay that feeds the measured panel its state files.

The cards judge freshness by the Unix times inside the files, so a replayed version must carry
times moved to now, and only times: a byte count or a percent stays as it was.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location("measure_panel", os.path.join(ROOT, "scripts", "measure_panel.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


class MovedTest(unittest.TestCase):
    def test_moves_seconds_and_milliseconds_only(self):
        state = {"at": 1791670859.5, "updated_ms": 1791670859000, "rpm": 2400.0, "used": 0.4,
                 "busy": True, "history": [[1791670800.0, 61.5], [1791670805.0, 62.0]], "name": "fans"}
        moved = M.moved(state, 100)
        self.assertEqual(moved["at"], 1791670959.5)
        self.assertEqual(moved["updated_ms"], 1791670959000)
        self.assertEqual(moved["history"], [[1791670900.0, 61.5], [1791670905.0, 62.0]])
        self.assertEqual((moved["rpm"], moved["used"], moved["busy"], moved["name"]), (2400.0, 0.4, True, "fans"))

    def test_keeps_the_type_of_each_number(self):
        self.assertIsInstance(M.moved(1791670859, 1.5), int)
        self.assertIsInstance(M.moved(1791670859.0, 1), float)


class RecordingStartTest(unittest.TestCase):
    def setUp(self):
        self.rec = tempfile.mkdtemp(prefix="measure-panel-")
        self.addCleanup(shutil.rmtree, self.rec, True)

    def version(self, name, data):
        with open(os.path.join(self.rec, name), "w") as f:
            json.dump(data, f)

    def test_saved_start_wins(self):
        self.assertEqual(M.recording_start(self.rec, {"start": 1791670000.0, "versions": []}), 1791670000.0)

    def test_older_recording_is_dated_by_its_writers(self):
        # each writer stamps the moment it wrote; the version came t seconds into the recording
        self.version("a", {"at": 1791670010.0, "since": 1791600000.0})
        self.version("b", {"at": 1791670020.5})
        self.version("c", {"at": 1791670031.0})
        log = {"versions": [{"t": 10.0, "copy": "a"}, {"t": 20.0, "copy": "b"}, {"t": 30.0, "copy": "c"}]}
        self.assertEqual(M.recording_start(self.rec, log), 1791670000.5)


if __name__ == "__main__":
    unittest.main()
