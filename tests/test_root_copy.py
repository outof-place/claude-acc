"""Testy kopii roota (root-install.sh, root-run.sh) i tego, że nic nie woła sudo na plikach spoza niej.

Testy nie biegną jako root, więc root-run.sh sprawdzamy po kawałku: odmowę bez roota i poza katalogiem
kopii, funkcję łańcucha właścicieli na prawdziwych ścieżkach systemu i walidację opcji kompresji
wyciętą z pliku. janitor dostaje atrapę kopii roota w katalogu testu.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUN = os.path.join(ROOT, "root-run.sh")


def functions():
    """Blok funkcji root-run.sh (root_chain, compress_args), bez sprawdzeń roota na początku pliku."""
    with open(RUN, encoding="utf-8") as f:
        src = f.read()
    start = src.index("# --- funkcje")
    return src[start : src.index("# --- koniec funkcji ---", start)]


def bash(script, *args):
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            "set -euo pipefail\n" + functions() + script,
            "test",
            *args,
        ],
        capture_output=True,
        text=True,
    )


class RootRunTest(unittest.TestCase):
    def test_refuses_without_root(self):
        if os.geteuid() == 0:
            self.skipTest("test jako root")
        done = subprocess.run(
            ["/bin/bash", RUN, "perf-root", "status"], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("tylko przez sudo", done.stderr)

    def test_root_chain_on_real_paths(self):
        self.assertEqual(bash('root_chain "$1"', "/usr/bin/true").returncode, 0)
        # /Applications należy do roota, ale grupa admin może w nim pisać
        self.assertNotEqual(bash('root_chain "$1"', "/Applications").returncode, 0)
        self.assertNotEqual(
            bash('root_chain "$1"', tempfile.gettempdir()).returncode, 0
        )
        self.assertNotEqual(
            bash('root_chain "$1"', "/nie/ma/takiej/sciezki").returncode, 0
        )

    def test_compress_options_are_a_closed_list(self):
        ok = bash(
            'compress_args "$@"; printf "%s\\n" "${ARGS[@]}"',
            "--apps",
            "Microsoft Word,Premiere Pro",
            "--threads",
            "8",
            "--done-ratio",
            "0.9",
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(
            ok.stdout.splitlines(),
            [
                "--apps",
                "Microsoft Word,Premiere Pro",
                "--threads",
                "8",
                "--done-ratio",
                "0.9",
            ],
        )
        for args in (
            ["--afsctool", "/tmp/x"],
            ["--json-out", "/etc/x"],
            ["--apps", "a;b"],
            ["--apps", "$(id)"],
            ["--threads", "-1"],
            ["--threads", "8x"],
            ["--done-ratio", "2"],
            ["--apps"],
            ["run"],
        ):
            with self.subTest(args=args):
                self.assertEqual(bash('compress_args "$@"', *args).returncode, 2)


class RootInstallStatusTest(unittest.TestCase):
    def test_status_without_a_copy(self):
        if os.path.exists("/usr/local/libexec/claude-acc-root/pins.sha256"):
            self.skipTest("kopia roota jest zainstalowana na tym Macu")
        done = subprocess.run(
            ["/bin/bash", os.path.join(ROOT, "root-install.sh"), "--status"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("nie zainstalowana", done.stdout)

    def test_the_copy_carries_everything_root_runs(self):
        with open(os.path.join(ROOT, "root-install.sh"), encoding="utf-8") as f:
            src = f.read()
        for name in (
            "root-run.sh",
            "perf-root.sh",
            "janitor-root.sh",
            "compressapps.py",
            "rootpy.py",
        ):
            self.assertIn(name, src)
        # payload i formuła wożą oba nowe skrypty obok setup.sh
        with open(os.path.join(ROOT, "scripts", "payload.sh"), encoding="utf-8") as f:
            payload = f.read()
        self.assertIn("root-install.sh", payload)
        self.assertIn("root-run.sh", payload)


class NoSudoOnUserFilesTest(unittest.TestCase):
    """Żaden plik claude-acc nie woła sudo na skrypcie z $STATE, `source` albo /usr/bin/python3."""

    def test_sources(self):
        offenders = []
        for name in sorted(os.listdir(ROOT)):
            if not name.endswith((".py", ".sh")):
                continue
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    code = line.split("#", 1)[0]
                    if (
                        '"sudo", "/usr/bin/python3"' in code
                        or "sudo /usr/bin/python3" in code
                        or 'exec sudo "$(cat "$STATE/source")' in code
                    ):
                        offenders.append(f"{name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [])


class JanitorCompressTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "acc_janitor_rc", os.path.join(ROOT, "janitor.py")
        )
        self.janitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.janitor)
        self.dir = tempfile.mkdtemp(prefix="root-copy-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        sys.path.insert(0, ROOT)
        import compressapps

        self.compressapps = compressapps

    def run_compress(self, root_run, *args):
        out = io.StringIO()
        with (
            mock.patch.object(self.janitor, "ROOT_RUN", root_run),
            mock.patch.object(
                self.compressapps, "find_afsctool", return_value="/usr/bin/true"
            ),
            mock.patch.object(self.compressapps, "main", return_value=0),
            mock.patch.object(self.janitor, "log"),
            redirect_stdout(out),
        ):
            rc = self.janitor.cmd_compress_apps({}, list(args))
        return rc, out.getvalue()

    def test_without_the_root_copy_only_the_plan(self):
        with mock.patch.object(self.janitor.subprocess, "Popen") as popen:
            rc, out = self.run_compress(os.path.join(self.dir, "nie-ma", "root-run.sh"))
        popen.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertIn("claude-acc root install", out)

    def test_with_the_root_copy_sudo_runs_it_and_reads_the_results(self):
        fake = os.path.join(self.dir, "root-run.sh")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake, 0o755)
        results = [{"app": "/Applications/Word.app", "freed": 2048}]
        lines = [
            "kompresuję Word.app...\n",
            "CLAUDE-ACC-RESULTS " + json.dumps(results) + "\n",
        ]
        proc = mock.Mock(stdout=iter(lines), wait=mock.Mock(return_value=0))
        with mock.patch.object(
            self.janitor.subprocess, "Popen", return_value=proc
        ) as popen:
            rc, out = self.run_compress(fake, "--apps", "Word")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], ["sudo", fake, "compress-apps"])
        self.assertEqual(argv[-2:], ["--apps", "Word"])
        self.assertNotIn(
            "--afsctool", argv
        )  # afsctool bierze kopia roota, nie Homebrew
        self.assertEqual(rc, 0)
        self.assertIn("kompresuję Word.app", out)
        self.assertNotIn("CLAUDE-ACC-RESULTS", out)


if __name__ == "__main__":
    unittest.main()
