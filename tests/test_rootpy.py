"""Testy rootpy.py: który interpreter wolno uruchomić jako root.

Atrapy interpreterów to skrypty w katalogu testu, które mówią o sobie to, co podamy, bo właśnie o to
rootpy pyta kandydata: zaślepka `/usr/bin/python3` należy do roota i nie jest dowiązaniem, a mimo to
uruchamia interpreter z Xcode'a użytkownika.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location(
    "acc_rootpy", os.path.join(ROOT, "rootpy.py")
)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


class FindTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="rootpy-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def fake(self, name, reports=None):
        """Atrapa interpretera: `-I -S -c ...` wypisuje swoje ścieżki (domyślnie siebie i stdlib)."""
        path = os.path.join(self.dir, name)
        stdlib = os.path.join(self.dir, name + "-stdlib")
        os.makedirs(stdlib, exist_ok=True)
        said = reports if reports is not None else [path, stdlib]
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + "".join(f'echo "{line}"\n' for line in said))
        os.chmod(path, 0o755)
        return path

    def probe(self, answers):
        """Podstawiona sonda: {ścieżka: ([ścieżki], powód)}."""
        return lambda path: answers.get(path, ([], f"{path}: brak w atrapie"))

    def test_a_root_owned_interpreter_passes(self):
        """Na tym Macu narzędzia wiersza poleceń Apple: pakiet .pkg, więc wszystko należy do roota."""
        clt = "/Library/Developer/CommandLineTools/usr/bin/python3"
        if not os.access(clt, os.X_OK):
            self.skipTest("brak narzędzi wiersza poleceń")
        self.assertIsNone(R.unsafe_reason(clt))
        self.assertEqual(R.find(env={})[0], clt)

    def test_the_xcrun_shim_is_refused_even_though_it_is_root_owned(self):
        """/usr/bin/python3 należy do roota, ale uruchamia interpreter z wybranego Xcode'a."""
        shim = "/usr/bin/python3"
        if not os.access(shim, os.X_OK):
            self.skipTest("brak /usr/bin/python3")
        self.assertNotIn(shim, R.CANDIDATES)
        xcode = R.unsafe_reason(shim)
        if (
            os.lstat("/Applications").st_uid == 0
            and os.lstat(os.path.realpath(shim)).st_uid == 0
        ):
            self.skipTest(
                "Xcode spod instalatora: interpreter zaślepki należy do roota"
            )
        self.assertIsNotNone(xcode)
        self.assertIn("/Applications", xcode)

    def test_a_user_owned_interpreter_or_stdlib_is_refused(self):
        mine = self.fake("python3")
        reason = R.unsafe_reason(mine)
        self.assertIsNotNone(reason)
        self.assertIn(str(os.getuid()), reason)
        # plik roota, ale biblioteka standardowa użytkownika: tak wygląda Xcode z DMG
        answers = {"/root/bin/python3": (["/root/bin/python3", mine + "-stdlib"], None)}
        reason = R.unsafe_reason("/usr/bin/true", probe=self.probe(answers).__call__)
        self.assertIsNotNone(reason)

    def test_what_the_interpreter_says_about_itself_decides(self):
        answers = {"/usr/bin/true": (["/usr/bin/true", "/usr/lib"], None)}
        self.assertIsNone(R.unsafe_reason("/usr/bin/true", probe=self.probe(answers)))
        answers = {
            "/usr/bin/true": (["/usr/bin/true", os.path.join(self.dir, "mine")], None)
        }
        self.assertIsNotNone(
            R.unsafe_reason("/usr/bin/true", probe=self.probe(answers))
        )
        # kandydat, którego nie da się o to spytać, nie przechodzi
        self.assertIsNotNone(
            R.unsafe_reason("/usr/bin/true", probe=lambda p: ([], "nie odpowiada"))
        )

    def test_nothing_safe_reports_every_candidate(self):
        one, two = self.fake("one"), self.fake("two")
        path, why = R.find(candidates=(one, two), env={})
        self.assertIsNone(path)
        self.assertIn(one, why)
        self.assertIn(two, why)
        self.assertIn(
            "nie da się uruchomić", R.find(candidates=("/nie/ma/python3",), env={})[1]
        )

    def test_the_override_goes_through_the_same_checks(self):
        mine = self.fake("python3-mine")
        clt = "/Library/Developer/CommandLineTools/usr/bin/python3"
        self.assertIsNone(R.find(env={R.OVERRIDE_ENV: mine})[0])
        if os.access(clt, os.X_OK):
            self.assertEqual(R.find(candidates=(), env={R.OVERRIDE_ENV: clt})[0], clt)

    def test_relative_paths_are_refused(self):
        self.assertIn("bezwzględna", R.unsafe_reason("python3"))


if __name__ == "__main__":
    unittest.main()
