# acc-cored: claude-acc's loops in one native daemon

acc-cored is a Swift 6 daemon that takes over claude-acc's resident and periodic Python loops. It
reads the kernel directly (sysctl, `proc_pid_rusage`, `proc_pidinfo`, kqueue, Dispatch sources)
instead of starting Python every few seconds. Every action stays in Python. When a tick would act,
acc-cored hands that tick to the script, which reads the Mac again and acts exactly as before.

Status: the measuring tool and the readers the guard's loop needs (these branches); the loops
follow in stacked branches. Nothing is registered on a Mac by the build or the tests.

## What the loops cost

Measured on the author's Mac on 2026-10-10 over 15 and 20 minutes, at a 1-minute load of 12 to 20,
with `acc-cored measure`. Each launchd job runs in its own resource coalition. The kernel keeps the
CPU time, wakeups, spawns and writes of every process that ever ran in it, so two reads a window
apart cover a periodic job's runs and their children. No root is needed.

| Loop | CPU | Wakeups | Notes |
|---|---|---|---|
| `sched run` wrappers (all running jobs) | 7.4 ms/s | 8.3/s | per job 2.5 to 5 ms/s and about 5 wakeups/s: a 4 Hz tree sample and a 1 Hz `state.json` heartbeat; about 36 MB each |
| `devguard run` | 7.0 to 7.5 ms/s | 0.3/s | a tick is about 30 ms of CPU every 5 s; caps run `du -sk` 17 times every 10 min (1.76 s of CPU) |
| `accswitch tick` (every 120 s) | 2.4 ms/s | 0.03/s | about 27 processes per tick (Keychain reads) |
| `perf keep` (every 300 s) | 1.7 ms/s | 0.5/s | about 114 processes per run |
| `jobs tick` (every 120 s) | 0.7 ms/s | 0.04/s | |

`acc-cored measure [--seconds N] [--json] [LABEL...]` takes every loaded job whose label starts
with `com.filip.claude-acc`, `codes.pod.app.acc.` or the menu bar app's, unless labels are given.

## Readers

What devguard_core reads through ctypes, read the same way in Swift, so the two agree line by line:

- `Proc.all()` is `processes()`: one `KERN_PROC_ALL` sysctl, `KERN_PROCARGS2` for the user's own
  processes, the line `ps -axo command=` prints (control characters as ps shows them, invalid UTF-8
  replaced as `bytes.decode(errors="replace")` does), sorted by terminal, then pid.
- `SocketTable.read()` is `sockets()`: `PROC_PIDLISTFDS` and `PROC_PIDFDSOCKETINFO`, the answer
  `lsof -nP -a -u $UID -iTCP` gave.
- `PyJSON` reads and writes JSON as the scripts do: key order kept, ints stay ints, `json.dumps`
  bytes (`ensure_ascii`, `", "` and `": "`, float repr).
- `PyRegex` runs Python patterns on ICU with Python's meaning of `\s`, `\w`, `\b`, `.` and `$`.
  NSRegularExpression costs about 2.7 µs a call, so each pattern carries literals one of which every
  match contains, and a line without any is a miss without ICU.
- `ProcCache` keeps process lines between reads. It reads a process's arguments again only when it
  is new, after an exec (kqueue `NOTE_EXEC`), when its name, uid or zombie state changes, while it
  is younger than 60 s (node rewrites its own argv), and on a full read every 60 s. Each line keeps
  its pattern answers (`CommandFacts`).

Checks, all with the built binary:
- `tests/acc_cored/parity_probes.py` compares the process and socket tables with devguard_core's,
  read Python, native, Python; a row counts only when both Python reads agree.
- `tests/acc_cored/parity_regex.py` compares every pattern with `re` on this Mac's command lines and
  on fuzzed lines with the characters where ICU and Python disagree (`\v`, `\x1c`-`\x1f`, `\x85`,
  NBSP, U+2028, combining marks, non-ASCII digits).
- `acc-cored cache-check` compares `ProcCache` with fresh reads taken on both sides of it.
- `tests/test_acc_cored.py` runs the first two.

Results on 2026-10-10:
- tables: 5,403 of 5,403 rows and 120 of 120 sockets identical;
- patterns: 112,840 checks over 28 patterns, 0 different;
- cache: 37,023 lines over 3 minutes, 0 wrong, with 8.5% of them read.
