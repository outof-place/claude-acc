# acc-cored: claude-acc's loops in one native daemon

acc-cored is a Swift 6 daemon that takes over claude-acc's resident and periodic Python loops. It
reads the kernel directly (sysctl, `proc_pid_rusage`, `proc_pidinfo`, kqueue, Dispatch sources)
instead of starting Python every few seconds. Every action stays in Python. When a tick would act,
acc-cored hands that tick to the script, which reads the Mac again and acts exactly as before.

Status: the measuring tool, the readers and the dev-server guard's tick (these stacked branches).
Nothing is registered on a Mac by the build or the tests. The switch from `devguard run` to
`acc-cored guard` is a separate step with its own undo (see [Switching](#switching)).

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
  bytes (`ensure_ascii`, `", "` and `": "`, float repr). Strings and keys compare by code point, as
  Python's do; Swift's `String ==` would merge "é" and "e" + U+0301. An int beyond Int64 or a string
  with a lone surrogate (from `os.fsdecode`) stays as its JSON text and is written back unchanged.
- `PyRegex` runs Python patterns on ICU with Python's meaning of `\s`, `\S`, `\w`, `\W`, `\b`, `\B`,
  `\v`, `\Z`, `.` and `$`, and escapes the punctuation ICU reads as set syntax inside a class.
  Inline flags, `(?P…)` and `--` in a class aren't translated: such a pattern stops at its build.
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

## The guard

`acc-cored guard` runs the loop of `acc.py devguard run`: the same tick on the same cadence
(`interval_seconds`, or 2 s while memory is tight). It also ticks as soon as the kernel reports
memory pressure (a Dispatch memory-pressure source).

- **Ported line by line:** `World` (`discover`, `Server`, `Unit`, simulators, the socket table,
  Orca's tabs, worktrees and terminals), `Pressure`, `lastresort.stage`, `decide`,
  `simulator_plans`, `check_pending`, the inventory, the history and the snapshot. List orders, float
  sums (CPython 3.12+ compensates `sum()` of floats), `round()`, `%.Nf`, `shlex.join`, `json.dumps`
  bytes and the regular expressions all follow Python. Python's `re` classes (`\s`, `\w`, `\b`, `.`,
  `$`) are spelled out for ICU in `PyRegex`.
- **Handed to Python:** a tick with a plan Python's loop would hand to `execute()`, or whose memory
  brake is due. acc-cored saves the state as it was before that tick and releases `devguard.lock`.
  It then runs `acc.py devguard once` (killed after 120 s) and reloads the state Python saved. The
  tick's own log lines and notifications are dropped then, since Python's tick writes its own. The
  caps run through `acc.py devguard caps` every `caps_minutes` while enforcing, as before.
- **Waits after a handover:** Python's loop runs every tick without starting a process, so two kinds
  of handover wait (`HandoverPolicy`):
  - Some plans Python can decline on its own reading: a pool simulator's shutdown, or a server whose
    app is open in a Simulator that is in front. If Python declined one, it waits 60 s. The plans
    under it and the brake still go in the meantime.
  - After a brake handover, whatever its outcome, the brake waits its own gap:
    `emergency_cooldown_seconds` at stage 3, `brake_cooldown_seconds` below. Without that wait, a
    reap that finds no victim would start Python every 2 s.
  - Every other plan is tried again on the next tick. Python's reading may disagree for a moment
    (pressure flickering), and it acts as soon as it agrees.
- **Config types:** a `devguard.json` value whose type differs from the default's is read
  differently by the two sides. `"runtimes": "node"` still finds servers in Python, where `in`
  works on a string, and finds none here. While such a value is set, every tick goes to Python, and
  the log says so once.
- **Kept between ticks:** process lines and their pattern answers (`ProcCache`, see
  [Readers](#readers)).

### Parity

- `tests/acc_cored/devguard_replay.py record` records live ticks. Each fixture holds every reading
  the tick made (processes, sockets, rusage sequences per pid, cwd, argv, sysctls, files, Orca's
  answers) plus the state, plans and verdict Python produced. Actions are replaced by recorders.
- `devguard_replay.py fuzz` builds synthetic Macs and runs them through the same tick. They include
  dev servers under launchers, shells and agents, duplicates, orphans, protected and pinned servers,
  simulators with leases, host tabs and terminals, memory pressure and histories.
- `acc-cored guard-replay` runs the native tick on every fixture. The state, the Orca view, the plans,
  every plan Python's loop hands to `execute()` (in order) and the brake must match. Python's
  recorder declines every plan, so its loop tries them all.
- Fuzzed configs sometimes carry a value of the wrong type. The replay checks that the native side
  flags each of them; those ticks would go to Python.
- A quarter of the fuzzed Macs pin a bloated server in a plain Orca terminal, for the `recycle
  bloated` and `warn loop_watched` paths.
- `tests/test_acc_cored.py` runs a short fuzz replay (200 fixtures) besides the readers' checks.
- `GuardEngineTests` drives the engine over recorded readings with a stubbed `once`. It checks the
  holds, the brake's wait, the caps' mode gate and the config handover.

Results:
- fuzz, 2026-10-10: 10,000 of 10,000 fixtures identical (first plan and brake only);
- fuzz, 2026-10-11, every plan and the brake: 9,659 of 9,659 identical, and 165 configs of the wrong
  type flagged (176 more fixtures were dropped because Python's tick raised on them);
- live, 2026-10-10: 26 of 26 recorded ticks identical, with two dev servers and real Orca reads.

### Cost

Native tick, from `acc-cored guard --profile` with the first tick excluded:
- no dev servers: about 2.2 ms of CPU per 5 s tick, about 0.44 ms/s;
- two servers: about 7 ms per tick;
- footprint: 5 MB, against 16 MB for the Python guard.

The Python guard spends 6 to 7.5 ms/s.

Pod's runtime answers `browser.tabList` in about 8 s. While dev servers run, the guard's Orca
refresh (every `orca_seconds`) blocks the tick for that long, in Python as well as here.

## Switching

Not done by this branch. The plan:
- Pod registers `codes.pod.app.acc.cored` in place of the devguard agent.
- Standalone installs swap `com.filip.claude-acc.devguard` for a `cored` plist.
- Undo: bootout the cored job and bootstrap the devguard plist again.

Both loops hold `devguard.lock`, so they never run together. `acc-cored guard --shadow FILE` runs
the native loop beside the Python one, observe-only, and writes its state to FILE.
