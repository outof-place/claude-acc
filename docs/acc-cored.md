# acc-cored: claude-acc's loops in one native daemon

acc-cored is a Swift 6 daemon that takes over claude-acc's resident and periodic Python loops. It
reads the kernel directly (sysctl, `proc_pid_rusage`, `proc_pidinfo`, kqueue, Dispatch sources)
instead of starting Python every few seconds. Every action stays in Python. When a tick would act,
acc-cored hands that tick to the script, which reads the Mac again and acts exactly as before.

Status: the measuring tool (this branch); the loops follow in stacked branches. Nothing is
registered on a Mac by the build or the tests.

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
