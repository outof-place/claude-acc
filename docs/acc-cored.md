# acc-cored: claude-acc's loops in one native daemon

acc-cored is a Swift 6 daemon that takes over claude-acc's resident and periodic Python loops. It
reads the kernel directly (sysctl, `proc_pid_rusage`, `proc_pidinfo`, kqueue, Dispatch sources)
instead of starting Python every few seconds. Every action stays in Python. When a tick would act,
acc-cored hands that tick to the script, which reads the Mac again and acts exactly as before.

Status: the measuring tool, the readers, the dev-server guard's tick and the scheduler's per-second
pass (these stacked branches). Nothing is registered on a Mac by the build or the tests. The switch from `devguard run` to
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
  It then runs `acc.py devguard once` (killed after 300 s) and reloads the state Python saved. It
  takes the lock back without waiting: if another guard took it meanwhile (launchd's `devguard
  run`, a manual one), native neither saves nor runs the caps, and waits as after Python's loop
  (below), so neither the queue nor a stop hangs on the lock. The tick's own log lines and
  notifications are dropped then, since Python's tick writes its own. The
  caps run through `acc.py devguard caps` every `caps_minutes` while enforcing, as before.
- **Waits after a handover:** Python's loop runs every tick without starting a process, so two kinds
  of handover wait (`HandoverPolicy`):
  - Some plans Python can decline on its own reading: a pool simulator's shutdown, or a server whose
    app is open in a Simulator that is in front. If Python declined one, it waits 60 s. The plans
    under it and the brake still go in the meantime.
  - After a brake handover, whatever its outcome, the brake waits its own gap:
    `emergency_cooldown_seconds` at stage 3, `brake_cooldown_seconds` below. Without that wait, a
    reap that finds no victim would start Python every 2 s. A stage above the last brake's goes at
    once: stage 3 picks victims stage 2 can't, and when stage 2 did reap, `brake_at` already holds
    the brake back, as in Python.
  - Every other plan is tried again on the next tick. Python's reading may disagree for a moment
    (pressure flickering), and it acts as soon as it agrees.
- **Caps, only when they can do something:** a caps run walks every entry of the capped folders with
  `du -sk`: 17 processes and about 1.8 s of CPU per run here, every `caps_minutes`. Its outcome
  changes only in these cases, so at each interval `CapsWatch` lets a run through only then:
  - something under a capped folder changed, or `janitor.json` did.
    - FSEvents watch the real paths of the globs' static prefixes and of the folders the last run
      matched (a folder under the glob may be a link to elsewhere), or their nearest existing
      parent. A link anywhere along a glob that leaves its static prefix is reported too: the
      link's real target plus the glob's fixed part after it (`wt1 -> repo` with `caps/*/out/builds`
      gives `repo/out/builds`), even before those folders exist or while the link dangles, and not
      the whole linked folder, so an edit elsewhere in it doesn't wake the caps.
    - A link right before another wildcard (`caps/*/*/builds`, `l1 -> a worktree`) has no fixed
      part: its target is watched shallow, and each folder found under it is reported as above.
      FSEvents (without per-file events) names a folder on every file written in it, so an event
      naming a shallow folder counts only when its subfolders and links changed since the run
      started: one made, removed or renamed, or a link in it retargeted or swapped for a folder.
      A file such as `t1/README` doesn't count.
    - Above a watched folder (the stream sees those events under a shallow root, or at the nearest
      existing parent of a folder not made yet), an event counts only when the next folder on the
      way came or went since the run started: one stat. A file written in `t1/s0` with
      `t1/s0/builds` there, a file in `t1/s1` before `t1/s1/builds` exists, or a write in a
      `~/.cache` that has no `perf` folder yet, doesn't count; `mkdir t1/s1/builds` does.
    - A link to a file is skipped: nothing can be made under it. A dangling link still counts, since
      it may become a folder.
    - A folder under another watched path adds no root. Once the watch is due, further events
      cost a flag check.
    - The paths and their roots are computed again at every interval, so a folder made later (in
      any letter case) re-arms the stream. A root replaced by a rename is reported (`WatchRoot`).
    - A lost-events flag counts as a change.
  - free space on a watched volume dropped since the last run by at least a threshold: a file
    growing behind an open descriptor gets no event until it's closed.
    - The threshold is the tightest folder's headroom from the last run's report, compared as a
      Double, and never less than a floor: a tenth of the smallest cap, the floor itself at most
      1 GiB.
    - A folder's headroom is its limit minus the running total at the last entry the run keeps by
      size (an index past `keep`, the total still within the limit): growth of what stays changes
      the next run only past that. With no entry kept by size (all within `keep`, or the rest
      removed), it's the limit itself, a finite stand-in for "no growth matters".
    - Free space is sampled on every guard tick (a statfs per root), and the drop counts from the
      highest free space seen since the run, so space freed meanwhile doesn't hide it;
  - the time the last run reported passed: an entry skipped as fresh (a write in progress) may go;
  - the last run skipped an entry in use or one it couldn't move, or `du` failed on one (no total;
    exit 1 with a total, an unreadable subfolder, is a size): no event says that changed, so the
    next interval runs again. Protected entries and ones in git stay so,
    and don't count.
  - A cap whose path only expanduser or the working directory can place (`~user/`, `$HOME/`, a
    relative path), or a stream that didn't start, runs at every interval, as in Python.

  Six hours bound the rest. `devguard caps` prints its report as one JSON line (`busy`, `retry_at`,
  `folders`, `headroom`). The stream holds its watch, and the guard stops it when it stops.
- **Config types:** a `devguard.json` value whose type differs from the default's is read
  differently by the two sides. `"runtimes": "node"` still finds servers in Python, where `in`
  works on a string, and finds none here. The brake's own keys count too. While such a value is
  set, Python's own loop (`devguard run`) guards, as a child of acc-cored with the lock let go,
  and the log says so once.
  - What native pinned to the E cores goes back before the loop starts, and its flags are
    forgotten when the loop ends: the two would disagree otherwise.
  - A loop that ends within a minute (Python can't run that config either, e.g. a string
    `interval_seconds`) starts again after 10 s, doubling to 5 min, with one log line each time.
  - When the config reads natively again, or acc-cored stops, the loop gets SIGTERM. After 10 s it
    is killed, with what it started: each process is stopped (SIGSTOP) before its children are
    read, and the whole tree is collected before the kill, so nothing it starts meanwhile escapes.
  - Native ticks again only after taking the lock back without waiting, also when the loop had
    already ended and sat in its backoff. While another guard holds the lock (a `devguard run`
    agent, a manual `devguard once`), native doesn't tick. Then it starts from the state Python
    saved last (`brake_at`, `warned`).
  - `interval_seconds` is capped at an hour (a timer's leeway would overflow) and floored at 0.5 s
    (the waiting paths would spin).
  - A caps run in flight finishes before the loop starts.
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

## The scheduler

Today every `sched run` wrapper runs its own loops. A queued wrapper reloads `state.json` every
0.5 s. A running one samples its job's process tree at 4 Hz and writes a heartbeat once a second.
Each of them runs `reap`, `refresh_memory`, `safety`, `plan` and `update_queue_view` on every pass,
so ten wrappers do the same housekeeping ten times.

`acc-cored run --sched` does that work once for all jobs:
- **One pass a second, under `sched/lock`:** the heartbeats of the jobs it watches, then `reap`,
  `refresh_memory`, `safety`, `plan` and `update_queue_view`, ported line by line (`Sched.tick`).
  The pass reads memory as `probe_memory` does, the guard's snapshot from the guard in the same
  process when there is one, and native builds as `native_scan` does.
- **Job trees:** sampled on the wrapper's own cadence (10 ms, then 50 ms, then 250 ms), for the peak,
  the CPU, the class limit and the stall clock.
- **Wake-ups:** when `plan` admits a queued job, acc-cored sends that wrapper SIGUSR1. The wrapper
  then admits itself, as before.
- **Idle:** with no job left the timers stop, and acc-cored only watches the `sched` directory.

The wrapper (`sched.py`) cooperates only when acc-cored marks the state (`"cored": {"pid", "at",
"proto"}`), that pid is a live `acc-cored` of the same `proto`, and its socket answers a ping
(asked outside `sched/lock`). `SCHED_NO_CORED=1` keeps the old loops. While attached, a wrapper
treats a mark older than 3 s as acc-cored gone. That covers a stopped or wedged daemon, whose socket
may still answer: acc-cored refreshes the mark on every pass.
- **Queued:** the wrapper marks its entry `"wake": "usr1"` and waits in a kqueue for the signal
  (`EVFILT_SIGNAL`, with a no-op handler so a late signal can't kill it and the job's disposition
  stays default). It still wakes every 5 s for its own checks (timeout, reroute).
- **Running:** the wrapper marks its entry `"monitor": "cored"` and blocks on the child's exit
  (kqueue `NOTE_EXIT`, `wait4`). Every 5 s it writes the stall log from acc-cored's `stalled_s` and
  enforces the class limit. At exit it asks for the job's final peak and CPU over the control socket
  (`sched.final`).
- **Fallback:** if acc-cored disappears, a queued wrapper drops its mark and polls again. A running
  one goes back to its own loop, starting from the last peak acc-cored saved. When acc-cored stops,
  it removes its mark. A restarted acc-cored takes over with the peak the entry holds.
- **When acc-cored stands back:** it doesn't run a pass, and writes nothing, in three cases. A state
  of another version or an unreadable one (sched.py may be newer than the daemon; a fresh state
  would wipe the queue) is one. A `config.json` value of another type than its default is another
  (`"stall_s": "600"` is 600 to Python's `float()`); then the mark is taken away. In both cases
  the wrappers watch their own jobs again, so acc-cored drops its watches and stops sampling. The
  third is `sched/lock` held by Python: that pass is tried again in 100 ms, and the samples go on.
- **One daemon:** `run` takes `$STATE/acc-cored.lock` first. A second instance exits instead of
  taking the socket. With `--wait-lock` (launchd's job, whose KeepAlive would restart an exiting
  one every 10 s) it waits for the lock instead.
- **Signals:** SIGUSR1 goes only to a pid above 1 and within `pid_t` that is still the job's
  wrapper (`is_wrapper`). A `child_pgid` is a process group only when it's an int above 1 and
  below 2**31 (`group_id`, on both sides): `killpg(-N)` is `kill(N)`, `killpg(0)` the sender's own
  group, 1 is launchd, and past `pid_t` Python's `os.kill` raises OverflowError, not OSError. That
  applies to the orphan's SIGTERM in `reap`, `safety`'s SIGSTOP and SIGCONT, and `kill_child`.
- **After a reboot:** a wrapper writes this boot's `kern.bootsessionuuid` into its entry
  (`boot_session`). `reap` drops an entry from another session with no signal: nothing of it
  lives, and its pid and group belong to other processes now. `p_uniqueid` counts again from each
  boot (early on it equals the pid: launchd is 1, a pid 351 has uid 351), so the same (pid, uid)
  pair can come back. No clock decides: every `settimeofday` moves `kern.boottime`, and a step
  forward by Δ would have dropped the entries of a boot's first Δ seconds while their jobs ran on
  unaccounted. A signal needs the entry's session to be this one (`same_boot`), so an entry without
  one (written before it existed) gets none, and an unknown session (the kernel didn't answer)
  makes nothing stale and signals nobody. `reap` never signals its own process group either.
- **pids reused within one boot** (they wrap here every ~25 minutes, ~4000 a minute). A process is
  told by its `p_uniqueid` (`PROC_PIDUNIQIDENTIFIERINFO`): the kernel gives it at fork and not again
  until a reboot, so a reused pid has another one and a clock change doesn't move it. Unlike
  `proc_bsdinfo`, it's readable for other users' processes too (a wrapper started with sudo, root's
  launchd).
  - A wrapper writes its own uid into its entry (`pid_uid`), and counts as alive only while its pid
    still has that uid (`owned`). Otherwise its entry goes, as for a dead wrapper. An entry without
    one (written before it existed, or not an int of Int64 at 0 or above) only needs its pid alive,
    as before.
  - A wrapper also writes its child's uid (`child_uid`) when it starts the job. A job's group is
    signalled (reap's SIGCONT + SIGTERM, safety's SIGSTOP and SIGCONT) only while its leader still
    has that uid (`group_of`). An entry without it, or without a numeric `started_at` (an int of
    Int64, as acc-cored reads it), gets no signal.
  - So a group's identity is (session, group, uid), with no clock in it.
  - acc-cored puts no watch on an entry from another boot or on a group that isn't the job's.
  - Cost: one `proc_pidinfo` per entry and pass, microseconds. Earlier versions compared the
    kernel's start in seconds with `enqueued_at` (the wall clock): off by 1-2 s, wrong after a clock
    step back, and reap then SIGTERMed a live job. The start stamp in µs, compared for equality,
    fixed that but gave EPERM for root's processes, so a wrapper under sudo looked dead.

The control socket is `$STATE/acc-cored.sock`: mode 0600, JSON lines, and every peer's uid is
checked (`LOCAL_PEERCRED`). A peer that closes before its answer (a wrapper whose call timed out)
costs nothing: `run` ignores SIGPIPE (the commands that print keep the default, so a `| head`
ends them), sends use `MSG_NOSIGNAL`, and a client XNU refuses
`SO_NOSIGPIPE` for (EINVAL: the peer closed before `accept`) is closed without a write. Unchecked,
~60 such clients killed the daemon (exit 141), and with it every engine in it.

### Parity

- `tests/acc_cored/sched_replay.py fuzz` builds synthetic states. They cover queues and running jobs
  (local and Depot, small, native, paused, stalled, passed heads, starving heads, simulators), plus
  memory readings, guard snapshots and configs. Each one runs through sched.py's own five functions.
  `record` reads the live state and readings without writing anything. The fixtures carry the boot
  session (now and then unknown; entries from another boot with a pid and uid the kernel gives
  again, from before `boot_session`, or with an odd one), the reaper's own group and the processes' `p_uniqueid` (some a pid or group reused since,
  some unreadable, some entries from before `pid_uid`, some uids no Int64 holds or of another type), and `child_pgid` takes odd values too: negative, 0, 1, `true`,
  2**40, a string, the reaper's own group.
- `acc-cored sched-replay` runs the native pass on every fixture. The state, the admissions and the
  signals sent must match. A file with no fixture fails, and so does `fuzz` when Python dropped every
  one, or when `<src>` holds no sched.py (the stdlib has a `sched` too, and every fixture would
  raise).
- `tests/test_acc_cored.py` runs 300 fuzzed fixtures. Their Python side runs on 3.12 or newer (the
  suite's own interpreter, else the first `python3.1x` or `python3` on PATH that is): from 3.12
  `sum()` of floats is compensated, and acc-cored mirrors that, as the live sched.py runs on 3.14.
  On 3.9 a sum such as 0.17 + 4.49 + 2.395 rounds to 7.05, not 7.06.

Results on 2026-10-10:
- fuzz: 10,500 of 10,500 fixtures identical (again after the review fixes, 2026-10-11), and
  12,000 of 12,000 with the reboot and own-group rules (seeds 7 and 11, Python 3.14), and again
  12,000 of 12,000 with the `p_uniqueid` identity (seed 7: 3,463 live wrappers whose pid is another
  process now, 3,098 odd uids, 645 reused groups, 685 expected signals);
- live: 10 of 10 recorded passes identical (6 on 2026-10-10, 4 on 2026-10-11, one job running).

### Cost

End to end with a scratch `HOME`, `SCHED_FAKE_MEMORY` and 20 s jobs started at once. The wrapper
numbers come from `proc_pid_rusage` on each wrapper process:

| Jobs | | wrapper CPU (median) | wrapper wakeups (median) | acc-cored |
|---|---|---|---|---|
| 4 | wrappers' own loops | 1.40 ms/s | 4.86/s | |
| 4 | with acc-cored | 0.05 ms/s | 0.21/s | 51.2 ms in total, 3.1 MB footprint |
| 8 | wrappers' own loops | 1.28 ms/s | 5.00/s | |
| 8 | with acc-cored | 0.05 ms/s | 0.21/s | 106.2 ms in total (about 4.4 ms/s), 15 wakeups/s, 3.6 MB |

With eight jobs that is about 10 ms/s and 40 wakeups/s for the wrappers' own loops, against about
4.8 ms/s and 17 wakeups/s with acc-cored. Every job finished with rc 0 and a history row in all
runs.

The queue frees faster too. Three jobs waited on memory that freed 10 s in. The jobs started
0.24 to 0.26 s after the change with the wrappers' own polling, and 0.06 to 0.09 s with acc-cored.

The test jobs are sleeping processes. A real build's tree is larger, so a wrapper's own sampling
costs more there (2.5 to 5 ms/s per job, see [What the loops cost](#what-the-loops-cost)).

Two timings change:
- `safety` and `refresh_memory` now run once a second in total, not once a second per wrapper. A
  cascade of pauses under swap growth is therefore slower, one job a second at most.
- A running wrapper enforces `class_timeout_s` when it wakes, every 5 s. Its SIGKILL can come up to
  about 5 s later than with the wrapper's own 4 Hz loop.

## The account switcher's tick

launchd runs `acc.py accswitch tick` every 120 s. A tick reads every account's Keychain item (about 27
`security` processes), often asks the usage API, and costs about 2.4 to 2.9 ms/s. Most ticks change
nothing: while the active account carries, the tick reads the same cached usage and returns.

`acc-cored run --accswitch` keeps launchd's 120 s slots, but a slot runs the tick only when that
tick could do something the last one didn't:
- **An input changed** since the last tick ended. The inputs are the keychain files in
  `~/Library/Keychains` (their stamps, never their contents), accswitch's config, pause, usage
  cache and state, the jobs' held accounts, the host app's account list and its account selection
  (`orca-data.json`). Without any keychain file every slot runs.
  - A change during a tick isn't in its result: a change to an input other than `state.json` and
    `usage-cache.json` (which the tick rewrites itself) runs the next slot too.
  - The baseline moves only after a tick that ran, which `last_tick` tells: a tick that didn't get
    the lock leaves what was due for the next slot.
  - The host app is asked again (`orcahost.py`) when a file its choice depends on changes (each
    host's `SingletonLock`, the Pod bundles, `owner.json`), and every slot while it fails.
- **The last tick wasn't quiet, or its deadline passed.** The tick computes that deadline itself,
  with its own functions, and writes it to `state.json` as `tick_due`:
  - The tick is quiet when the active account carries, or when the switcher stands aside (an
    account selected in the host app, foreign data in the runtime).
  - Its deadline is the earliest of three times: when the active account's cached reading stops
    being enough (`active_max_age`), when the active token expires (the tick then waits for a
    session, and refreshes it itself after 15 minutes), and the deadline of Depot's fast path.
  - Every other tick leaves the next slot, as today. So does a tick that ended early: its first
    write (`last_tick`) clears `tick_due`, and one that doesn't exit with 0 or 1 runs the next slot.
  - A tick isn't quiet when the profile API didn't answer it (no network, 429): foreign data in
    the runtime is quiet only when the API named the account.
- **Eight minutes passed** since the last tick. The menu bar calls the switcher silent after ten.
- **`last_tick` is ahead of the clock** (the clock was set back): the tick writes a true one.

### The tick is never killed

A tick killed between a token refresh and its Keychain write leaves a used refresh token behind,
and the server answers its reuse by revoking the account. acc-cored never signals a tick, and
launchd sends SIGKILL only after the job's `ExitTimeOut` (90 s in the tick's plist: curl's 20 s and
the writes fit in it):
- The tick leads a process group of its own. acc-cored stops without waiting for it, and a tick
  outlives acc-cored (launchd reaps it then). The next slot is scheduled when the tick ends.
- Its timeout is cooperative. `ACC_TICK_DEADLINE` (100 s after its start) is when the tick stops
  starting work: it reads no further account and starts a refresh only with 30 s left to finish
  it. A tick past the deadline ends cleanly with 1 and leaves the rest to the next slot.
- From the refresh request to its last Keychain write, SIGTERM, SIGINT and SIGHUP wait until the
  block ends (`uninterrupted`). Terminating signals are delivered first, then SIGINT. The `curl`
  and `security` children started in the block get a session of their own, so a signal to the
  whole group (Ctrl-C, a closed terminal or pane, an agent's timeout) doesn't kill them halfway.
- `tests/test_accswitch_uninterrupted.py` sends SIGTERM at every call of the fake `curl` and
  `security`, at its start and just before it answers, then runs a normal tick. The server never
  sees a used token, and every copy holds a live one. SIGINT and SIGHUP run inside the refresh.
  The same runs again with the signal sent to the tick's whole process group. SIGKILL right
  after the server's answer does leave a used token (the control).
- A tick past the deadline in its status snapshot (`survey`) skips the snapshot and ends not
  quiet: an older `status.json` must not be vouched for.

### Slots on record

Every slot writes `accswitch-slot.json`: `at`, whether it `ran` the tick and why (`reasons`), the
tick's exit code (`rc`) and time (`tick_s`), the counts, and `checked_at`: the last slot that was a
check, i.e. a skip or a tick that got the lock (a new `last_tick`) and ended with 0. A tick that
didn't start (rc 127, with one line in `launchd.log` until a tick starts again), crashed, or found
the lock held (a hung `claude-acc status`, say: the tick returns 0 then) is tried, not checked.
The tick's output goes to `launchd.log`, as with launchd. `ping` returns the counts, the last slot and how long a running tick has run, without
waiting for the engine's queue (an `orcahost.py` lookup may hold it).

A skipped slot also names the `status.json` it vouches for (`status_at`, its `generated_at`):
nothing a tick reads changed since that snapshot. It vouches only for the latest tick's snapshot
(`generated_at` at least `last_tick`). The menu bar counts a vouched snapshot fresh from the slot's
time, so with the panel closed it doesn't run `status --json` itself while ticks are skipped. The
panel's "Auto-switch checked" counts from `checked_at`, so a dead switcher still goes silent.

acc-cored never reads, refreshes or copies credentials. The tick stays Python's.

Some refreshes are the panel's: other accounts' readings after 30 minutes, and an idle account's
token near its expiry. They can come up to eight minutes later than today. They change what the
panel shows, not which account the switcher picks: when the switcher looks for another account it
reads again anyway.

### Shadow

`acc-cored run --accswitch-shadow FILE` runs nothing. It watches launchd's own ticks: a tick starts
with its `last_tick` write and ends with `status.json`. For each tick it records whether this
schedule would have run it, and what the tick changed:
- hard: its own log lines, the Keychain file, the pause file, the active account's reading, or
  state other than the panel's caches;
- soft: other accounts' readings, the profile cache and the fingerprints.

A claude-acc without `tick_due` gets an estimate from the files: the active reading's expiry, and
Depot's. A pause is quiet only with the headroom the switcher takes an account with. The active
token's expiry is not in any file, and the estimate doesn't check that Depot's CLI exists. The gap
counts from the last tick the shadow schedule would have run.

## The janitor's sweep

launchd starts `acc.py janitor sweep` at login and every 3 hours, whatever the Mac is doing. The
sweep includes `compress` (APFS compression), which is CPU-heavy.

`acc-cored run --janitor` keeps the 3-hour cadence, but a due sweep waits until the Mac is idle:
- no keyboard or mouse for 5 minutes (IOKit's `HIDIdleTime`);
- no local build in the scheduler (`sched/state.json`);
- not on a battery under `min_battery_percent`;
- no sweep holding `janitor.lock`, and at least five minutes since boot.

Six hours past due it runs anyway, though still not on a low battery. While the sweep isn't due
nothing polls: one timer waits for the due time, then one waits for each idle check (the idle time
it still lacks). The sweep is Python's and keeps its own gates. It runs as launchd ran it, in the
background band (`taskpolicy -b`: E cores, throttled IO) at nice 10, without a time limit (launchd
had none, and a kill would stop `compress` mid-file).

"Due" is 3 hours after the later of the last recorded sweep and the engine's last start. So a sweep
that ends without recording itself is tried again 3 hours later, as launchd would, not in a loop.
That covers a missing `$STATE/python`, an exception, and Python's own gates. `min_hours_between_sweeps`
from `janitor.json` holds it as well. Idle is keyboard and mouse only: a video or a call without
input counts as idle. A user who comes back during a sweep doesn't stop it. `acc-cored janitor-check`
prints the live reading and what the engine would do.

## Perf keep

launchd ran `perf.py keep` every 300 s: every check of every applied tweak, whether anything had
changed or not. `acc-cored run --perf` brings a tweak back when one of its files changes or the
network does, and runs the full pass only at start and every 30 minutes. Python keeps the knowledge
and the writes:
- `perf.py keep-spec` prints what to watch: each applied tweak with its class and files, plus
  `admit-chain` (the files `admitchain.broken()` checks: settings.json under `CLAUDE_CONFIG_DIR`,
  the `chains-claude-acc` marker and `pod-hook-client`) and `link` (the network).
- `perf.py keep --only a,b` repairs just those: no deferred undos, no Ultra, and the link and the
  admit chain only when named. An unknown name or an empty list exits 2.
- The classes, as decided with the lead (a 30-minute repair delay is fine only for perf or cosmetic
  tweaks, never for safety or privacy):
  - **safety:** `claude-hooks-native`, `devguard-budget`, `devguard-max-server`, `security-slim`
    (also its generated hooks: `hooks.json` points at them by absolute path), `admit-chain`. They
    come back on an event, every time.
  - **perf:** the settings.json ones (`node-compile-cache`, `claude-limits`, `subagent-cache-1h`,
    `fast-npx-hooks`, `rg-threads`, `workflow-size`, `claude-ui`, `tether-profile`) and `git-speed`
    (`~/.gitconfig`). They come back on the same events.
  - **periodic**, no cheap event: `bg-helpers` (a helper restarted with a new pid), `docker-vm`
    (its file lies in Docker's group container, where an `open()` from acc-cored means a TCC
    prompt or EPERM), git maintenance in the repositories, Ultra's results, and
    `claude-hooks-async`. That last one stays out of the events until `pod/claude-async-hooks`
    ships (v6.1). They come back within 30 minutes. `docker-idle` runs alone every 5 minutes, as
    under launchd: it counts connection samples up to its 2-hour threshold.
- **A fight:** a tweak repaired 3 times in 10 minutes has someone reverting it (another tool, a
  hand edit). A perf tweak then waits an hour instead of fighting, in both keeps. A safety tweak
  comes back every time and says so in perf.log at most once an hour. Only tweaks repaired from
  their files count: a network change switches `tether-profile`, and a new pid isn't someone
  undoing `bg-helpers`.
- **The link for updates.py:** acc-cored refreshes `link` only on a network change, so on a tether
  that lasts the entry ages past updates.py's 15 minutes. updates.py then asks `perf.py link
  --json` live before a scheduled run.
- **Its own writes:** a repair writes files it watches. Those events land while the run is on, and
  one more run confirms them: it finds nothing to change and writes nothing.
- **Events:**
  - files: a kqueue source per file for writes in place, one per folder for atomic replacements;
  - a missing folder: waited for from its nearest parent;
  - a file counts only when its stamp (inode, mtime, size) moved, so a busy folder costs a stat per
    watched file;
  - the network: `notify_register_dispatch` on `com.apple.system.config.network_change`, settled
    for 2 s;
  - a change to perf.json or perf-state.json (apply, undo, a run) reloads the spec without a run.
- **Runs:** events within 0.5 s go to one run, and a stream of events doesn't push it back. A run
  is `taskpolicy -c utility` (a safety repair shouldn't wait behind background throttling), and
  writes to perf-launchd.log.
- **One writer:** keep, apply, undo, ultra and record take `perf.lock`, so launchd's keep beside
  acc-cored's (after a setup.sh run) takes turns.
- **Fallback:** without a spec (a perf.py from before `keep-spec`), the full pass runs every 300 s,
  as launchd ran it, so nothing comes back later than before.
- **Not live:** the switch adds `--perf` and replaces `com.filip.claude-acc.perf`, and switching on
  is a separate decision.

## Events for hosts

Hosts that show claude-acc's state (AccKit's `AccStore` in PodNative, Pod Menu) watch the state
directory and read files again on every rename in it. `acc-cored run` serves the same files as a
stream on its control socket, so a host can subscribe instead:
- The line `{"op":"events","v":1}` turns the connection into frames: a u32 little-endian length
  of what follows, a u8 kind, then the payload.
  - 1 hello (server): `{"v": 1, "topics": [...]}`.
  - 2 subscribe (client): `{"topics": [...], "have": {topic: seq}, "maxHz": {topic: hz}}`. It is
    the whole set and replaces the last one, and `have` says what the client holds now.
  - 3 snapshot (server): a u16 little-endian header length, the header `{"topic", "seq"}`, then
    the file's exact bytes. A host decodes them with the same `Decodable` it uses on the file.
  - 4 gone (server): `{"topic"}`, the file isn't there, or isn't a state file: a link, a FIFO
    (opened without blocking), a device, or a file over 1 MiB.
  - 5 error (server): `{"code", "msg"}`. An unknown topic is reported and the rest still flows.
    A frame that isn't a subscribe ends the stream.
- `seq` is the file's `ino:mtime_ns:size`, the stamp a host computes when it reads the file itself
  (from `st_mtimespec` in ns, not a `Date`), so `have` holds across a fallback to the files.
  `pressure`'s seq is devguard-state.json's.
- Topics:
  - `accounts` (status.json), `accountsSlot` (accswitch-slot.json), `devguard`, `sched`
    (sched/state.json), `depot` (sched/depot.json), `awake`, `services` (pod-services.json),
    `owner`, `menuBar` (menubar.json), `janitor`, `fans` and `hotspot` (their state files);
  - `pressure`, derived: devguard-state.json's `snapshot.pressure.level` as `{"level": N}`, sent
    only when the level changes.
- Latest-wins per topic and client: while a client hasn't taken what was sent, newer versions
  replace the waiting ones instead of queueing (one flush stays well under the 16 MiB a client may
  leave unsent). `maxHz` spaces a topic's frames: it's clamped to 0.001-1000, and each client's
  one timer goes to the earliest topic's turn, so a slow topic doesn't hold back a fast one.
- A client is dropped when it hasn't subscribed 10 s after the events line, or when its socket
  took nothing for 30 s while frames waited (it stopped reading; a slow reader that keeps taking
  stays). The reaper looks every 5 s, so that's 10-15 s and 30-35 s. 16 event clients at most;
  the 17th gets `busy`.
- A snapshot's frame is built once per version and shared by every client it goes to: 16 clients
  that don't read, on 12 topics of 1 MiB and the same version, took the daemon to 31 MB, not
  225 MB. A slow reader holds the version it's on, though: with each of 16 clients joining after
  another change of all 12 files, it's 220-252 MB again, the worst case `maxOutbox` bounds (16 ×
  16 MiB). The hub also holds each watched file twice (its frame and its last data), ~50 KB in
  practice.
- A client with a small `SO_RCVBUF` (1024) still gets everything: the hub sets `SO_SNDLOWAT` to 1,
  since after a drain such a client never leaves the default 2048 bytes of room.
- `acc-cored events` restores SIGPIPE's default (a parent may pass on SIG_IGN), and checks once a
  second whether stdout's reader went (POLLHUP), so `| head -1` ends it even while no frame comes.
- The files stay the contract, written atomically (a rename, as every writer does today: an
  in-place write can be sent half done). Python writes them, and acc-cored only watches them, and
  only while someone subscribes. It watches their folders for renames and each subscribed file
  for writes in place, and stats a file before reading it, so writes to other files in a folder
  cost a stat each. A folder that's missing is waited for from its nearest parent; a folder made,
  removed or moved re-arms its watches and re-stamps its topics.
- The daemon ignores SIGPIPE and sends with `MSG_NOSIGNAL`, so a client gone mid-frame costs
  nothing. A connection is handed from the control socket to the hub only once its read source's
  cancel has run, so no source watches a descriptor the hub may close.
- `acc-cored events [--for SECONDS] TOPIC...` prints one line per frame of a running daemon (line
  buffered, and it ends when the daemon closes the stream), and `ping` counts the event clients
  without waiting for the hub.
- A host should give the hello 1 s at most (a hung daemon accepts connections it never answers)
  and read the files when the socket is missing or drops.

Cost, measured 2026-10-11 with a release build: a scratch HOME, `run` with no engine, one client
on every topic for 60 s. The files were written at the live rates counted that day (sched 32/min,
hotspot 30, devguard 11 at 30 KB, fans 10). The client got exactly one frame per write (83), plus
10 pressure frames. The daemon used about 0.8 ms/s (the whole process, start included), the client
under 0.17 ms/s (below `time`'s 10 ms resolution).

After the review fixes (stat before read, a file's own event re-stamps only its topic), 30 s runs
on the same Mac, the hub before and after: 1.24 and 0.61 ms/s at those rates; with 5 writes/s of
files no topic names added to the state directory, 2.03 and 1.14 ms/s.

## Switching

`scripts/acc-cored-switch.sh` swaps four launchd jobs for one acc-cored job, and back: devguard,
the accswitch tick (`com.filip.claude-acc`), the janitor and perf keep (`com.filip.claude-acc.perf`).
Standalone installs only: Pod registers its own agents, so where `owner.json` says Pod owns
claude-acc, `on` refuses with exit 3 (`owner.py check`, as setup.sh does), and it exits 1 when none
of the jobs is loaded. A
tombstone (handed back to Homebrew, uninstalled) is a standalone install again.

    scripts/acc-cored-switch.sh status
    scripts/acc-cored-switch.sh on  [--apply] [--binary PATH]
    scripts/acc-cored-switch.sh off [--apply]

- **Check-only by default:** without `--apply` it prints what it would do.
- **`on`:**
  - It installs the binary as `$STATE/acc-cored` (a temp file, then a rename: overwriting a
    running binary gets its next launch killed) and writes `com.filip.claude-acc.cored.plist`. The
    job runs `acc-cored run --guard --sched --accswitch --janitor --perf` as `ProcessType Interactive`,
    with `KeepAlive`.
  - It writes the plist and records the jobs it stops in `$STATE/acc-cored-switch.json` first, so
    a Ctrl-C later still leaves `off` what to bring back.
  - The tick's job goes first, under the switcher's own lock (`lockf -k -t 150 $STATE/lock`), so
    no tick is between a token refresh and its Keychain write when launchd stops it. Whether it
    went is what launchd says afterwards, not bootout's exit code. If it's still loaded (the lock
    held past 150 s by a hung `claude-acc status`, say), the script removes the record and the
    plist again and exits 1: no other job goes and no acc-cored starts (the new binary is in
    place).
  - Then it boots out the others and bootstraps acc-cored (one launchd doesn't have loaded
    afterwards exits 1 and points at `off`). The job runs with `--wait-lock` and `ExitTimeOut` 30 s (stopping ends
    Python's guard loop first, up to 10 s).
  - setup.sh boots the tick's job out under the same lock too (past 150 s, without it, as before),
    and only when launchd has it loaded. `setup.sh --uninstall` and `--pod-agents` take acc-cored
    down as well, with its plist and the record: under KeepAlive it would go on ticking accswitch
    beside Pod's agents or after an uninstall.
  - The replaced jobs' plists stay where setup.sh wrote them.
- **`off`** is the undo. It boots acc-cored out, bootstraps the recorded jobs from their plists
  again, and removes the plist and the record. Each step is judged by what launchd says afterwards,
  as in `on`. If any of them didn't take (or a job's plist is missing), the record and the plist
  stay and it exits 1, so it can run again.
- **After a setup.sh run**, the jobs come back beside acc-cored. The two guards then share
  `devguard.lock`, the ticks the switcher's lock, the sweeps `janitor.lock`, and the keeps
  `perf.lock`, so they take turns instead of acting twice. Run `off` and `on` again after it.
- **With main's Python** (no `tick_due`, no sched cooperation) acc-cored still works:
  - the tick runs in every slot, as launchd ran it;
  - wrappers keep their own loops, and the sched pass stays idle;
  - perf keep, without `keep-spec`, runs the full pass every 300 s, as launchd did;
  - the guard and the janitor gain fully.

Before the first live switch: measure the stage-3 handover latency (a cold `devguard once` under
memory pressure).

`acc-cored guard --shadow FILE` runs the native guard beside the Python one, observe-only, and
writes its state to FILE. `run --accswitch-shadow FILE` does the same for the tick.
