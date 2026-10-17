"""Testy scripts/pod_agents.py: automaty launchd Pod (SMAppService) z szablonów setup.sh.

Każdy job z JOBS w setup.sh ma swój agent `codes.pod.app.acc.<job>`: te same klucze i harmonogram,
start przez pod-acc-run z pakietu zamiast ścieżek w HOME i ten sam log w $STATE.
"""

import importlib.util
import os
import plistlib
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location("pod_agents", os.path.join(ROOT, "scripts", "pod_agents.py"))
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)


def legacy(label):
    with open(os.path.join(ROOT, "launchd", label + ".plist.template"), encoding="utf-8") as f:
        return plistlib.loads(f.read().replace("__HOME__", "/Users/x").encode("utf-8"))


class PodAgentsTest(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="pod-agents-")
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_every_setup_job_becomes_a_bundled_agent(self):
        paths = A.write(self.out)
        labels = A.jobs()
        self.assertEqual(len(paths), len(labels))
        self.assertIn("com.filip.claude-acc.devguard", labels)
        for label, path in zip(labels, paths):
            with self.subTest(label=label):
                with open(path, "rb") as f:
                    agent = plistlib.load(f)
                old = legacy(label)
                name = "tick" if label == "com.filip.claude-acc" else label.rsplit(".", 1)[1]
                self.assertEqual(os.path.basename(path), f"codes.pod.app.acc.{name}.plist")
                self.assertEqual(agent["Label"], f"codes.pod.app.acc.{name}")
                self.assertEqual(agent["BundleProgram"], "Contents/Resources/claude-acc/pod-acc-run")
                log = os.path.basename(old["StandardOutPath"])
                self.assertEqual(agent["ProgramArguments"], ["pod-acc-run", "--log", log, *old["ProgramArguments"][2:]])
                # reszta kluczy (harmonogram, klasa procesu, KeepAlive...) bez zmian, bez ścieżek w HOME
                rest = {k: v for k, v in old.items() if k not in ("Label", "ProgramArguments", "StandardOutPath", "StandardErrorPath")}
                self.assertEqual({k: v for k, v in agent.items() if k not in ("Label", "ProgramArguments", "BundleProgram")}, rest)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                self.assertNotIn("__HOME__", text)
                self.assertNotIn("/Users/", text)

    def test_pod_only_jobs_are_bundled_but_never_installed_by_setup(self):
        """admitd tylko jako agent Pod: setup.sh bootstrapuje wyłącznie JOBS."""
        A.write(self.out)
        self.assertTrue(os.path.exists(os.path.join(self.out, "codes.pod.app.acc.admit.plist")))
        with open(os.path.join(ROOT, "setup.sh"), encoding="utf-8") as f:
            setup = f.read()
        jobs_line = next(x for x in setup.splitlines() if x.startswith("JOBS="))
        self.assertNotIn("com.filip.claude-acc.admit", jobs_line)
        self.assertNotIn("$POD_JOBS", setup.replace('POD_JOBS="', ""))
        with open(os.path.join(self.out, "codes.pod.app.acc.admit.plist"), "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["ProgramArguments"], ["pod-acc-run", "--log", "admit-launchd.log", "devguard", "admitd"])
        self.assertEqual(agent["ProcessType"], "Interactive")

    def test_the_tick_keeps_its_launchd_log(self):
        A.write(self.out)
        with open(os.path.join(self.out, "codes.pod.app.acc.tick.plist"), "rb") as f:
            args = plistlib.load(f)["ProgramArguments"]
        self.assertEqual(args, ["pod-acc-run", "--log", "launchd.log", "accswitch", "tick"])

    def test_other_app_id_and_program(self):
        paths = A.write(self.out, app_id="codes.pod.canary", program="Contents/Helpers/pod-acc-run")
        with open(paths[0], "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["Label"], "codes.pod.canary.acc.tick")
        self.assertEqual(agent["BundleProgram"], "Contents/Helpers/pod-acc-run")

    def test_a_template_that_starts_something_else_is_refused(self):
        with open(os.path.join(ROOT, "launchd", "com.filip.claude-acc.plist.template"), encoding="utf-8") as f:
            text = f.read()
        with self.assertRaises(ValueError):
            A.convert(text.replace("__HOME__/.local/share/claude-acc/python", "/usr/bin/python3"), "com.filip.claude-acc")
        with self.assertRaises(ValueError):
            A.convert(text, "com.filip.claude-acc.janitor")  # etykieta z innego szablonu
        broken = text.replace("<key>RunAtLoad</key>", "<key>WorkingDirectory</key><string>__HOME__</string><key>RunAtLoad</key>")
        with self.assertRaises(ValueError):
            A.convert(broken, "com.filip.claude-acc")  # ścieżka w HOME została


if __name__ == "__main__":
    unittest.main()
