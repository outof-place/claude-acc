"""admitchain.py: hook admit claude-acc obok łańcucha fasthooks (kontrakt 1 z cmd-proxy).

Każdy test stawia osobny $HOME z settings.json, katalogiem fasthooks (znacznik i atrapa
pod-hook-client) i atrapą claude-acc-hook, uruchamia `admitchain.py heal` jako proces i patrzy
na settings.json i stan. Prawdziwe ~/.claude nie jest dotykane.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_admitchain
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "admitchain.py")


class Home:
    def __init__(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="admitchain-"))
        self.state = os.path.join(self.home, ".local", "share", "claude-acc")
        self.fasthooks = os.path.join(self.home, ".claude", "hooks", "fasthooks")
        os.makedirs(self.state)
        os.makedirs(self.fasthooks)
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.native = os.path.join(self.state, "claude-acc-hook")
        self.client = os.path.join(self.fasthooks, "pod-hook-client")
        self.script(self.native, "exit 0")
        self.rtk = {"type": "command", "command": os.path.join(self.fasthooks, "fasthooks"),
                    "args": ["rtk-enforce", os.path.join(self.home, ".claude/hooks/rtk-enforce.sh")]}
        self.admit = {"type": "command", "command": self.native, "args": [], "timeout": 10}
        self.pause = {"type": "command", "command": self.native, "args": ["pause", "post"], "timeout": 10}
        self.chain = {"type": "command", "command": self.client,
                      "args": ["pre-bash", os.path.join(self.home, ".claude/hooks/rtk-enforce.sh")], "timeout": 10}

    @staticmethod
    def script(path, body):
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def chained(self, answer="1", marker="1\n"):
        """fasthooks z łańcuchem: znacznik, klient, który odpowiada na --chains-claude-acc."""
        if marker is not None:
            with open(os.path.join(self.fasthooks, "chains-claude-acc"), "w") as f:
                f.write(marker)
        self.script(self.client, f'[ "$1" = --chains-claude-acc ] && {{ printf "{answer}\\n"; exit 0; }}; exit 0')

    def write(self, pre, **other):
        data = {"env": {"KEEP": "1"}, "hooks": {"PreToolUse": pre, **other}}
        with open(self.settings, "w") as f:
            json.dump(data, f, indent=2)

    def read(self):
        with open(self.settings) as f:
            return json.load(f)

    def pre(self):
        return self.read()["hooks"].get("PreToolUse", [])

    def entries(self):
        return [h for g in self.pre() for h in g["hooks"]]

    def run(self, *args):
        env = dict(os.environ, HOME=self.home)
        env.pop("CLAUDE_CONFIG_DIR", None)
        return subprocess.run(["/usr/bin/python3", SCRIPT, *args], env=env, capture_output=True, text=True,
                              timeout=60)

    def heal(self):
        done = self.run("heal")
        assert done.returncode == 0, done.stderr
        return done.stdout

    def state_file(self):
        path = os.path.join(self.state, "admit-chain.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)


class AdmitChainTest(unittest.TestCase):
    def setUp(self):
        self.h = Home()
        self.addCleanup(shutil.rmtree, self.h.home, True)

    def test_valid_chain_takes_our_entry_out_and_nothing_else(self):
        h = self.h
        h.chained()
        h.write([{"matcher": "Bash", "hooks": [h.rtk, h.chain, h.admit]},
                 {"matcher": "Agent|Task", "hooks": [h.pause]}])

        out = h.heal()

        self.assertIn("zdjęty", out)
        self.assertEqual(h.entries(), [h.rtk, h.chain, h.pause])
        self.assertEqual(h.read()["env"], {"KEEP": "1"})
        self.assertTrue(h.state_file()["chain_seen"])
        self.assertEqual(h.state_file()["removed"], [{"matcher": "Bash", "hook": h.admit}])
        # drugie przejście niczego nie zmienia
        before = h.read()
        self.assertEqual(h.heal(), "")
        self.assertEqual(h.read(), before)

    def test_broken_chain_puts_the_same_entry_back(self):
        h = self.h
        h.chained()
        h.write([{"matcher": "Bash", "hooks": [h.chain]}, {"matcher": "Bash", "hooks": [h.admit]}])
        h.heal()
        self.assertEqual(h.entries(), [h.chain])
        os.remove(os.path.join(h.fasthooks, "chains-claude-acc"))

        out = h.heal()

        self.assertIn("wrócił", out)
        self.assertIn(h.admit, h.entries())
        self.assertIn(h.chain, h.entries())  # wpisu fasthooks nie ruszamy
        self.assertEqual(h.state_file(), {})

    def test_installer_swapped_entries_then_fasthooks_went_away(self):
        # `pod-hooks native on` sam zdjął oba wpisy: my widzimy już tylko łańcuch
        h = self.h
        h.chained()
        h.write([{"matcher": "Bash", "hooks": [h.chain]}])
        self.assertEqual(h.heal(), "")
        self.assertTrue(h.state_file()["chain_seen"])
        os.remove(h.client)

        h.heal()

        self.assertIn({"type": "command", "command": h.native, "args": [], "timeout": 10}, h.entries())

    def test_each_part_of_the_contract_is_required(self):
        cases = {
            "wrong marker": dict(marker="2\n"),
            "no marker": dict(marker=None),
            "client speaks contract 2": dict(answer="2"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                h = Home()
                self.addCleanup(shutil.rmtree, h.home, True)
                h.chained(**kwargs)
                h.write([{"matcher": "Bash", "hooks": [h.chain, h.admit]}])
                h.heal()
                self.assertIn(h.admit, h.entries())
        with self.subTest("no settings entry"):
            h = Home()
            self.addCleanup(shutil.rmtree, h.home, True)
            h.chained()
            h.write([{"matcher": "Bash", "hooks": [h.rtk, h.admit]}])
            h.heal()
            self.assertIn(h.admit, h.entries())

    def test_a_program_from_settings_is_never_run(self):
        # wpis wygląda na łańcuch, ale program leży gdzie indziej: nie uruchamiamy go i nie zdejmujemy admit
        h = self.h
        h.chained()
        ran = os.path.join(h.home, "ran")
        elsewhere = os.path.join(h.home, "x", ".claude", "hooks", "fasthooks", "pod-hook-client")
        os.makedirs(os.path.dirname(elsewhere))
        h.script(elsewhere, f'touch "{ran}"; printf "1\\n"')
        fake = dict(h.chain, command=elsewhere)
        h.write([{"matcher": "Bash", "hooks": [fake, h.admit]}])

        h.heal()

        self.assertFalse(os.path.exists(ran))
        self.assertIn(h.admit, h.entries())

    def test_without_chain_seen_a_removed_entry_stays_removed(self):
        h = self.h
        h.write([{"matcher": "Bash", "hooks": [h.rtk]}])

        h.heal()

        self.assertEqual(h.entries(), [h.rtk])
        self.assertEqual(h.state_file(), {})

    def test_our_entry_back_by_hand_resets_the_state(self):
        h = self.h
        h.chained()
        h.write([{"matcher": "Bash", "hooks": [h.chain, h.admit]}])
        h.heal()
        os.remove(os.path.join(h.fasthooks, "chains-claude-acc"))
        h.write([{"matcher": "Bash", "hooks": [h.chain, h.admit]}])  # `pod-hooks native off` oddał oba

        h.heal()

        self.assertEqual(h.state_file(), {})
        self.assertEqual(h.entries(), [h.chain, h.admit])

    def test_devguard_admit_in_the_shell_counts_as_ours(self):
        h = self.h
        h.chained()
        shell = {"type": "command", "command": f"/usr/bin/python3 {h.state}/devguard.py admit"}
        h.write([{"matcher": "Bash", "hooks": [h.chain, shell]}])

        h.heal()

        self.assertEqual(h.entries(), [h.chain])

    def test_broken_settings_change_nothing(self):
        h = self.h
        h.chained()
        with open(h.settings, "w") as f:
            f.write("{not json")

        self.assertEqual(h.heal(), "")
        with open(h.settings) as f:
            self.assertEqual(f.read(), "{not json")

    def test_perf_keep_heals_every_five_minutes(self):
        # launchd: `acc.py perf keep` (com.filip.claude-acc.perf, codes.pod.app.acc.perf w Pod)
        h = self.h
        h.chained()
        h.write([{"matcher": "Bash", "hooks": [h.chain, h.admit]}])
        env = dict(os.environ, HOME=h.home, SCHED_OFF="1")
        env.pop("CLAUDE_CONFIG_DIR", None)

        done = subprocess.run(["/usr/bin/python3", os.path.join(ROOT, "acc.py"), "perf", "keep"], env=env,
                              capture_output=True, text=True, timeout=120)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(h.entries(), [h.chain])

    def test_status(self):
        h = self.h
        h.chained()
        h.write([{"matcher": "Bash", "hooks": [h.chain]}])

        out = h.run("status").stdout

        self.assertIn("łańcuch fasthooks: działa", out)
        self.assertIn("wpis admit claude-acc: nie ma", out)


if __name__ == "__main__":
    unittest.main()
