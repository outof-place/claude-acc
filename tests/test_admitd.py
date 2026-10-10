"""Testy admitd.py: ciepły `devguard.py admit` na gnieździe daje to samo co exec.

Prawdziwy serwer startuje z kopii skryptów w katalogu stanu tymczasowego HOME (jak po setup.sh),
z DEVGUARD_ORCA=/usr/bin/false, więc nic nie rusza Twojej Orki ani Twojego $STATE. Dziecko po
fork() sprawdzamy też wprost, z atrapą devguard: środowisko, katalog, stdin, kod i wyjątki.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_admitd
"""

import glob
import importlib.util
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILT_HOOK = os.path.join(ROOT, "app/.build/release/claude-acc-hook")

_spec = importlib.util.spec_from_file_location(
    "admitd", os.path.join(ROOT, "admitd.py")
)
admitd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admitd)

# komendy, przy których admit coś mówi (scheduler, odczyt sekretu) albo milczy
COMMANDS = (
    "go test -count=1 ./... 2>&1 | tail -5",
    "pnpm test 2>&1 | tail -5",
    "npx vitest run",
    "make test",
    "cat ~/.ssh/id_ed25519",
    "ls -la 2>/dev/null",
    "echo observe the server",
    "git grep -a needle",
)


def frame(obj):
    data = json.dumps(obj).encode()
    return struct.pack(">I", len(data)) + data


def ask(path, obj=None, raw=None, timeout=10):
    """Odpowiedź serwera jako dict albo None, gdy zamknął połączenie bez niej."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(raw if raw is not None else frame(obj))
        head = admitd.read_exact(s, 4)
        if head is None:
            return None
        return json.loads(admitd.read_exact(s, struct.unpack(">I", head)[0]))


class ChildTest(unittest.TestCase):
    """Jedno zdarzenie w dziecku po fork(), z atrapą devguard."""

    def run_child(self, req, main, sigchld=signal.SIG_IGN):
        """Fork tak jak w serwerze (admitd.fork_child), z SIGCHLD rodzica ustawionym na `sigchld`:
        SIG_IGN to najgorszy przypadek dla podprocesów dziecka."""
        fake = types.SimpleNamespace(main=main)
        a, b = socket.socketpair()
        old = signal.signal(signal.SIGCHLD, sigchld)
        try:
            pid = admitd.fork_child(b, fake, a)
        finally:
            signal.signal(signal.SIGCHLD, old)
        b.close()
        with a:
            a.sendall(req if isinstance(req, bytes) else frame(req))
            a.settimeout(10)
            head = admitd.read_exact(a, 4)
            answer = (
                None
                if head is None
                else json.loads(admitd.read_exact(a, struct.unpack(">I", head)[0]))
            )
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass  # już posprzątane (SIG_IGN albo admitd.reap)
        return answer

    def request(self, **extra):
        cwd = os.path.realpath(tempfile.mkdtemp(prefix="admitd-cwd-"))
        self.addCleanup(shutil.rmtree, cwd, True)
        return dict(
            {
                "v": 1,
                "argv": ["admit"],
                "event": '{"tool_name": "Bash"}',
                "cwd": cwd,
                "env": {"HOME": "/h", "ONLY_HERE": "1"},
            },
            **extra,
        )

    def test_the_child_runs_with_the_hooks_env_cwd_and_stdin(self):
        def main(argv):
            print(
                json.dumps(
                    {
                        "argv": argv,
                        "env": dict(os.environ),
                        "cwd": os.getcwd(),
                        "stdin": sys.stdin.read(),
                        "argv0": os.path.basename(sys.argv[0]),
                    }
                )
            )
            os.write(2, b"raw stderr\n")
            return 0

        req = self.request()
        answer = self.run_child(req, main)
        seen = json.loads(answer["stdout"])
        self.assertEqual(answer["v"], 1)
        self.assertEqual(answer["code"], 0)
        self.assertEqual(
            seen["env"], req["env"]
        )  # dokładnie środowisko hooka, nic z serwera
        self.assertEqual(seen["cwd"], req["cwd"])
        self.assertEqual(seen["stdin"], req["event"])
        self.assertEqual(seen["argv"], ["admit"])
        self.assertEqual(seen["argv0"], "devguard.py")
        self.assertEqual(answer["stderr"], "raw stderr\n")
        self.assertNotIn("ONLY_HERE", os.environ)  # rodzic (ten test) bez zmian

    def test_subprocesses_of_the_child_keep_their_exit_status(self):
        """Pod SIG_IGN (albo handlerem rodzica) waitpid podprocesu dawałby ECHILD, a subprocess 0."""

        def main(argv):
            print(subprocess.run(["/usr/bin/false"]).returncode)
            return 0

        for disposition in (signal.SIG_IGN, admitd.reap):
            with self.subTest(disposition=disposition):
                self.assertEqual(
                    self.run_child(self.request(), main, disposition)["stdout"], "1\n"
                )

    def test_fd_0_umask_like_the_exec(self):
        def main(argv):
            inherited = subprocess.run(
                ["/bin/cat"], capture_output=True, text=True
            ).stdout
            print(json.dumps({"fd0": inherited, "umask": oct(os.umask(0o022))}))
            return 0

        req = self.request()
        seen = json.loads(self.run_child(req, main)["stdout"])
        self.assertEqual(
            seen["fd0"], req["event"]
        )  # podproces czyta zdarzenie z odziedziczonego fd 0
        self.assertEqual(seen["umask"], "0o22")

    def test_codex_argv_exit_codes_and_exceptions(self):
        self.assertEqual(
            self.run_child(
                self.request(argv=["admit", "--codex"]),
                lambda argv: print(argv[-1]) or 0,
            )["stdout"],
            "--codex\n",
        )
        self.assertEqual(self.run_child(self.request(), lambda argv: 2)["code"], 2)

        def exits(argv):
            sys.exit(3)

        self.assertEqual(self.run_child(self.request(), exits)["code"], 3)

        def crashes(argv):
            raise KeyboardInterrupt

        answer = self.run_child(self.request(), crashes)
        self.assertEqual(answer["code"], 1)
        self.assertIn("KeyboardInterrupt", answer["stderr"])

    def test_anything_but_a_v1_request_gets_no_answer(self):
        ok = lambda argv: print("ran") or 0
        for bad in (
            self.request(v=2),
            self.request(argv=["status"]),
            self.request(env={"A": 1}),
            self.request(env={"A=B": "1"}),
            self.request(event=None),
            self.request(cwd="/nonexistent/dir"),
            self.request(cwd=""),
            self.request(cwd="relative/dir"),
            self.request(env={}),
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(self.run_child(bad, ok))
        self.assertIsNone(self.run_child(struct.pack(">I", admitd.MAX_FRAME + 1), ok))
        self.assertIsNone(self.run_child(struct.pack(">I", 5) + b"{not}", ok))

    def test_peer_uid_is_ours(self):
        a, b = socket.socketpair()
        with a, b:
            self.assertEqual(admitd.peer_uid(a), os.getuid())


class ServerTest(unittest.TestCase):
    """Prawdziwy admitd z kopii skryptów: ta sama odpowiedź co exec, dla każdej komendy."""

    def setUp(self):
        # krótka ścieżka: gniazdo musi się zmieścić w 104 bajtach
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="admitd-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state = os.path.join(self.home, ".local/share/claude-acc")
        os.makedirs(self.state)
        for path in glob.glob(os.path.join(ROOT, "*.py")):
            shutil.copy(path, self.state)
        self.project = os.path.join(self.home, "shop")
        os.makedirs(os.path.join(self.project, ".git"))
        with open(os.path.join(self.project, "go.mod"), "w") as f:
            f.write("module shop\n")
        with open(os.path.join(self.project, "package.json"), "w") as f:
            f.write('{"name": "shop"}')
        self.env = {
            "HOME": self.home,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DEVGUARD_ORCA": "/usr/bin/false",
        }
        self.sock = os.path.join(self.state, "admit.sock")
        self.server = self.start()

    def start(self):
        server = subprocess.Popen(
            [sys.executable, os.path.join(self.state, "acc.py"), "devguard", "admitd"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(server.stderr.close)
        self.addCleanup(server.wait)
        self.addCleanup(server.kill)
        for _ in range(200):
            if os.path.exists(self.sock):
                return server
            time.sleep(0.05)
        server.kill()
        self.fail("admitd nie wystartował: " + server.stderr.read().decode())

    def event(self, command):
        return json.dumps(
            {
                "session_id": "t",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command, "description": "d"},
                "cwd": self.project,
            }
        )

    def exec_admit(self, event, argv=("admit",)):
        done = subprocess.run(
            [sys.executable, os.path.join(self.state, "acc.py"), "devguard", *argv],
            input=event,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.project,
            timeout=60,
        )
        return {"stdout": done.stdout, "stderr": done.stderr, "code": done.returncode}

    def warm(self, event, argv=("admit",)):
        answer = ask(
            self.sock,
            {
                "v": 1,
                "argv": list(argv),
                "event": event,
                "cwd": self.project,
                "env": self.env,
            },
        )
        return answer and {k: answer[k] for k in ("stdout", "stderr", "code")}

    def test_the_same_answer_as_the_exec(self):
        answered = 0
        for command in COMMANDS:
            with self.subTest(command=command):
                event = self.event(command)
                cold = self.exec_admit(event)
                self.assertEqual(self.warm(event), cold)
                answered += bool(cold["stdout"])
        self.assertGreaterEqual(
            answered, 4
        )  # scheduler i sekret: przypadki, w których admit coś mówi

    def test_concurrent_requests_keep_their_own_env(self):
        event = self.event("pnpm test 2>&1 | tail -5")
        cold = self.exec_admit(event)
        results = []

        def one(i):
            env = dict(self.env, ONLY_FOR=str(i))
            results.append(
                ask(
                    self.sock,
                    {
                        "v": 1,
                        "argv": ["admit"],
                        "event": event,
                        "cwd": self.project,
                        "env": env,
                    },
                )
            )

        threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 12)
        for answer in results:
            self.assertEqual(answer["stdout"], cold["stdout"])

    def test_new_code_on_disk_restarts_the_server(self):
        event = self.event("pnpm test 2>&1 | tail -5")
        self.assertIsNotNone(self.warm(event))
        sched = os.path.join(self.state, "sched.py")
        st = os.stat(sched)
        os.utime(sched, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertIsNone(self.warm(event))  # to połączenie: exec u klienta
        for _ in range(100):
            try:
                answer = self.warm(event)
                if answer is not None:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        self.assertEqual(answer, self.exec_admit(event))
        self.assertIsNone(self.server.poll())  # ten sam proces po execv

    def test_bad_frames_get_no_answer(self):
        self.assertIsNone(ask(self.sock, raw=struct.pack(">I", admitd.MAX_FRAME + 1)))
        self.assertIsNone(
            ask(
                self.sock,
                {
                    "v": 2,
                    "argv": ["admit"],
                    "event": "{}",
                    "cwd": self.project,
                    "env": {},
                },
            )
        )
        self.assertEqual(oct(os.stat(self.sock).st_mode & 0o777), "0o600")

    @unittest.skipUnless(
        os.access(BUILT_HOOK, os.X_OK), "brak app/.build/release/claude-acc-hook"
    )
    def test_native_front_asks_the_daemon_first(self):
        """claude-acc-hook z działającym admitd: odpowiedź z gniazda, bez startu Pythona (atrapa
        $STATE/python mówi "python"); z CLAUDE_ACC_ADMIT_SOCK=0 albo bez serwera: exec jak dotąd."""
        with open(os.path.join(self.state, "hook-words.json"), "w") as f:
            f.write(
                subprocess.run(
                    [sys.executable, os.path.join(self.state, "devguard.py"), "words"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
        python = os.path.join(self.state, "python")
        with open(python, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\necho python\n")
        os.chmod(python, 0o755)

        def front(command, **extra):
            return subprocess.run(
                [BUILT_HOOK],
                input=self.event(command),
                capture_output=True,
                text=True,
                env=dict(self.env, **extra),
                cwd=self.project,
                timeout=30,
            ).stdout

        for command in COMMANDS:
            with self.subTest(command=command):
                cold = self.exec_admit(self.event(command))["stdout"]
                if cold:
                    self.assertEqual(front(command), cold)
        self.assertEqual(
            front("pnpm test", CLAUDE_ACC_ADMIT_SOCK="0").strip(), "python"
        )
        self.server.kill()
        self.server.wait()
        self.assertEqual(front("pnpm test").strip(), "python")  # martwe gniazdo: exec


if __name__ == "__main__":
    unittest.main()
