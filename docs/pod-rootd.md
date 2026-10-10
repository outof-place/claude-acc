# pod-rootd: one root helper for Pod

pod-rootd replaces claude-acc's five root LaunchDaemons and its `sudo` scripts with one
privileged helper. Pod.app ships it, `SMAppService` registers it as a launch daemon, and clients
reach it over XPC. The helper accepts a fixed set of typed verbs. It runs no shell, takes no free
paths and never executes a binary it was handed.

Status: design plus the first implementation (this branch). Nothing is installed by the build or
the tests; the cutover on a real Mac is a separate, manual step (see [Cutover](#cutover)).

## What it replaces

The root code today, read from the sources:

| Today | Runs as | What needs root |
|---|---|---|
| `com.filip.claude-acc.fans` (`fanctl daemon`, `install-fans.sh`) | root LaunchDaemon, KeepAlive, 2 s tick | SMC writes `Ftst`, `F<n>Md`, `F<n>Tg`; `pmset disablesleep` for Stay Awake with the lid closed (`Lid.swift`) |
| `com.filip.claude-acc.fsguard` (`fsguard.py`, `install-fsguard.sh`) | root LaunchDaemon, every 60 s | reading the footprint of root's `fseventsd`, signalling it |
| `com.filip.claude-acc.hotspot` (`hotspot.py daemon`) | root LaunchDaemon, KeepAlive, 50 ms probe loop | `ifconfig <if> tbr` only |
| `com.filip.claude-acc.vnodes` (`perf-root.sh vnodes --persist`) | root one-shot at boot | `sysctl -w kern.maxvnodes` |
| `com.filip.claude-acc.iogpu` (`perf-root.sh iogpu set`) | root one-shot at boot | `sysctl -w iogpu.wired_limit_mb` |
| `perf-root.sh shaper` | `sudo` | `ifconfig <if> tbr` |
| `perf-root.sh spotlight` | `sudo` | writing `.Spotlight-V100/VolumeConfiguration.plist`, killing `mds`, `mdutil -E` |
| `janitor-root.sh` | `sudo` | parking `/Library/Launch{Daemons,Agents}` plists with a missing program, pruning `/Library/Logs/DiagnosticReports`, `pmset -c powermode 2` |
| `compressapps.py run` | `sudo` | rewriting root-owned app bundles with `afsctool` |
| `setup.sh` | user | none itself; it points at `install-fans.sh` and `hotspot.py install` |

`perf-root.sh devtools` never needed root (it opens System Settings and waits for the click), and
`perf.py`/`devguard` only lower the priority of the user's own processes.

## Verbs

Every verb is idempotent: it names a wanted state, the helper compares it with the current one and
reports `changed: false` when nothing had to move. Every verb has a read-only counterpart in
`status`. Parameters are Swift types that refuse bad values when they are decoded, so a verb that
reaches the engine is already valid.

| Verb | Parameters | Replaces | Why root | Status |
|---|---|---|---|---|
| `fans.set` | `auto` or `fixed(percent)`, percent 30-100 | `fanctl daemon` + `fans.json`, `fanctl set` | SMC writes | `fans`: mode, applied, boosting, conflict, error |
| `lid.hold` | `seconds` 60-86400 | `Lid` + `awake.json` + the pid/signature check | `pmset -a disablesleep 1` | `lid`: held by us, lease end, last release reason, `SleepDisabled` now |
| `lid.release` | none | same | `pmset -a disablesleep 0` | same |
| `power.mode` | `source` ac/battery, `mode` automatic/low/high | `janitor-root.sh --high-power` | `pmset -c/-b powermode N` | `power`: current mode per source, the original, high-power capability |
| `sysctl.set` | `key` maxvnodes/gpuWiredLimitMB, `value` in the key's range, `persist` | `perf-root.sh vnodes`, `iogpu set`, the vnodes and iogpu daemons | `sysctlbyname` write | `sysctls`: current, original, persisted value |
| `sysctl.reset` | `key` | `vnodes undo`, `iogpu undo` | same | same |
| `shaper.set` | `interface` (`en<N>`, must exist), `kbps` 1000-10000000, `scope` session/untilReboot | `perf-root.sh shaper apply`, hotspot's `set_tbr` | `ifconfig <if> tbr` | `shaper`: interface, rate set by us, rate before, scope |
| `shaper.clear` | `interface` | `shaper undo`, hotspot release | same | same |
| `spotlight.appsOnly` | none | `perf-root.sh spotlight apps-only` | Spotlight's volume config, `mds`, `mdutil` | `spotlight`: applied, saved list size |
| `spotlight.restore` | none | `perf-root.sh spotlight undo` | same | same |
| `fsguard.set` | `enabled`, `limitMB` 1024-65536 | `fsguard.py` daemon | footprint of root's `fseventsd`, SIGTERM/SIGKILL | `fsguard`: footprint, limit, restarts, last restart, generation |
| `launchd.parkOrphans` | `dryRun` | `janitor-root.sh` (launchd part) | `/Library/Launch*` writes, `launchctl bootout system/...` | the orphans found, parked |
| `logs.pruneDiagnostics` | `olderThanDays` 30-365, `dryRun` | `janitor-root.sh` (reports part) | root-owned reports | count, bytes |
| `legacy.migrate` | none | the five `com.filip.claude-acc.*` daemons | bootout system jobs, move their plists | `legacy`: per label present/migrated |
| `legacy.rollback` | none | same | same | same |
| `restoreDefaults` | none | `install-fans.sh --uninstall`, `hotspot.py uninstall`, every `undo` | all of the above | every section back to its default |
| `status` | none | `fanctl read`, `hotspot.py status`, `perf-root.sh * status` | none (one place to read) | everything above |

### Ranges

- `fans.set fixed`: 30-100 %, the same range `fanctl set` accepts. A fixed setting still never
  lets a chip cook: at 95 °C on the hottest CPU/GPU sensor the fans go to 100 %, back below 85 °C.
- `lid.hold`: 60 s to 24 h, `Lid.maxHold`. The helper also lets go at 10 % battery on battery power
  and at a serious thermal state (then not again for 15 minutes), as `Lid` does.
- `sysctl.set maxvnodes`: 263168 (the kernel default on the M4 Max) to 2097152 (about 2.5 GB of kernel memory at 1.2 KB a vnode). `gpuWiredLimitMB`:
  0 (macOS default) or 4096 up to the RAM size minus 4096 MB, so the system always keeps 4 GB.
- `shaper.set`: 1-10000 Mb/s, interfaces `en0`-`en999` that exist right now.
- `fsguard.set`: 1-64 GB; the default stays 4 GB.

### Rate limits

Token buckets per caller and verb class; a refusal says when to retry.

| Class | Verbs | Burst | Refill |
|---|---|---|---|
| fans | `fans.set` | 3 | 1 per 2 s |
| power | `lid.*`, `power.mode` | 3 | 1 per 2 s |
| shaper | `shaper.*` | 40 | 20 per s (the hotspot controller retunes up to ~10 per s) |
| system | `sysctl.*`, `spotlight.*`, `launchd.*`, `logs.*`, `legacy.*`, `restoreDefaults` | 2 | 1 per 10 s |
| read | `status` | 20 | 10 per s |

## Tiers

Every verb has a tier, and every caller a set of tiers it may send (`Verb.tier`, `VerbPolicy`):

| Tier | Verbs | Pod Menu | `pod-rootctl` | Pod (Electron, `codes.pod.app`) |
|---|---|---|---|---|
| A: harmless, rate-limited | `status`, `fans.set`, `lid.hold`, `lid.release`; `shaper.set` scoped to the session at 6 Mb/s or more; `shaper.clear` of the caller's own session limit | yes | yes, no prompt | yes |
| B: changes the system | `power.mode`, `sysctl.*`, `shaper.*`, `spotlight.*`, `fsguard.set`, `launchd.parkOrphans`, `logs.pruneDiagnostics`, `legacy.*`, `restoreDefaults` | yes, no prompt: the click is the consent | only approved | never |

- Tier A can't do lasting harm. A fan setting keeps the 95 °C rule. A lid hold ends with its
  session, at 24 h, at 10 % battery and when the Mac gets hot. A session's upload limit ends with
  its session and can't go below 6 Mb/s (hotspot.py's floor), so the worst a forged call does is a
  temporary slowdown. That keeps the hotspot controller (`pod-rootctl shaper follow`, a background
  agent) free of prompts. Two cases stay tier B: a session limit on an interface that has a limit
  until reboot (its end would not bring that one back), and clearing a limit some other session or
  a user set.
- `pod-rootctl` is a confused deputy: anything running as the user can exec it, and it passes the
  peer requirement. For tier B it sends an `Approval`, and the helper refuses it (`needsApproval`)
  unless the approval is fresh. Fresh means the CLI's own LocalAuthentication prompt (Touch ID or the
  password, `deviceOwnerAuthentication`) succeeded in this run, or the same parent did that less than
  5 minutes ago. The parent is the CLI's session id, its parent pid with that process's start time,
  and its terminal; a script started from the shell is another parent. The grace runs from the
  approval, not from the last use. Only the CLI's signed code builds an `Approval`, and a modified CLI
  fails the peer requirement, so the helper can trust what it says.
- Pod's Electron process gets tier A only. Pod's terminal daemon runs `Pod Helper` with
  `ELECTRON_RUN_AS_NODE=1`, so the RunAsNode fuse has to stay on, and fuses are app-wide: any process
  can run Pod's signed binary as Node with its own JavaScript (`ELECTRON_RUN_AS_NODE=1
  /Applications/Pod.app/Contents/MacOS/Pod -e ...`), and that passes the requirement for
  `codes.pod.app`. Electron Pod reaches tier B only through Pod Menu or `pod-rootctl`.
- PodNative (nt-lean's Swift shell, `codes.pod.native` while it is a second window) is not
  admitted. Once it ships as `codes.pod.app` without Electron, it gets tier B like Pod Menu.

## What moves out of root

- **Fan readings.** Reading the SMC needs no root (`SMC.swift` says so, and `fanctl read` works as
  the user). The rpm, temperature and history Pod Menu shows come from Pod Menu itself through
  `SMCKit` (the SMC code moved there from `fanctl`). The helper reads the sensors only while a fixed
  setting below 100 % needs the 95 °C rule.
- **The hotspot controller.** The 50 ms ICMP probe loop, the interface counters and the
  cake-autorate controller in `hotspot.py` run as the user: ICMP datagram sockets and
  `getifaddrs` need no root. Parsing network replies as root is exactly what should not be in the
  helper. The controller calls `shaper.set ... scope: session` through `pod-rootctl shaper follow`,
  one session for the whole run: when the controller dies, the helper drops the limit.
- **The Lid's requester check.** `awake.json` plus a pid plus a signature check of that pid becomes
  a lease on an XPC session: the hold ends when Pod Menu's session ends (quit, crash), and the peer
  requirement already proved who holds it.
- **fsguard's side effects.** Stopping the user's `git fsmonitor--daemon` processes, the
  notification and devguard's dev server restart run as the user when `fsguard.generation` goes up.
  The helper only measures and restarts `fseventsd`, and logs other processes over 8 GB.
- **`perf.py record` bookkeeping** for root tweaks: the helper's `status` is the truth; `perf.py`
  reads it instead of keeping its own copy.
- **devtools** stays a System Settings click, no root.
- **compressapps** stays out of the helper for now. Running a user-writable `afsctool` from
  Homebrew as root (what `sudo compressapps.py` does today) is not something a root helper may do.
  The plan is a native decmpfs/LZFSE writer behind an `apps.compress(bundleID)` verb that resolves
  the bundle itself under `/Applications`; until then the feature keeps its `sudo` path.

## XPC and the peer requirement

The helper listens on the Mach service named in its plist with the Swift XPC API:

```swift
XPCListener(service: "codes.pod.app.rootd", targetQueue: .main,
            requirement: .codeRequirement(PeerPolicy.requirement)) { request in ... }
```

`PeerPolicy.requirement` is a lightweight code requirement (`LightweightCodeRequirements`):

```swift
try ProcessCodeRequirement.allOf {
    TeamIdentifier("75Y2KR6P5W")
    SigningIdentifier.in("codes.pod.app", "com.filip.claude-acc.menubar", "codes.pod.rootctl")
}
```

- The system checks it for every message before the handler sees it (the listener requirement),
  so a process signed by anyone else, ad hoc or unsigned never reaches a verb. No pid is trusted.
- Inside the handler each message is classified with `XPCReceivedMessage.senderSatisfies(_:)`
  against one requirement per identifier: the caller (`app`, `menu`, `cli`) goes into the log line
  and into the per-verb policy, and a message no class matches is refused (defense in depth).
- What each caller may send is in [Tiers](#tiers).
- Every verb is logged with `os_log` (subsystem `codes.pod.rootd`, category `verb`): caller class,
  verb with its parameters, the outcome.
- Clients connect with `XPCSession(machService:options: .privileged, requirement:)`: `.privileged`
  looks the name up in the system bootstrap only, and the requirement checks the helper is ours
  (`codes.pod.rootd`, same team).

Docs used (ctx7, `/websites/developer_apple_servicemanagement`, `/websites/developer_apple_de`,
`/websites/developer_apple_kernel`, `/websites/developer_apple_iokit`, `/electron/electron`), plus
the Xcode 27 SDK interfaces for exact Swift signatures where the docs only had the C side:

- SMAppService: `daemon(plistName:)` ("must correspond to a property list in the calling app's
  Contents/Library/LaunchDaemons directory"), `register()` ("the system bootstraps it after admin
  approval in System Preferences and on each subsequent boot"), `unregister()` (terminates a running
  daemon), `Status.requiresApproval` (also returned when the user revokes consent),
  `openSystemSettingsLoginItems()`;
  developer.apple.com/documentation/servicemanagement/smappservice.
- `BundleProgram` "relative to the bundle, such as Contents/Resources/mydaemon";
  developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos.
- `XPCListener.init(service:targetQueue:options:requirement:incomingSessionHandler:)` and
  `XPCSession.init(machService:targetQueue:options:requirement:...)`;
  developer.apple.com/documentation/xpc/xpclistener, .../xpc/xpcsession.
- Lightweight requirement semantics (checked on every message; a failing peer's message is never
  delivered): developer.apple.com/documentation/xpc/xpc_connection_set_peer_lightweight_code_requirement(_:_:).
- SDK (`XPC.swiftinterface`, `LightweightCodeRequirements.swiftinterface`, `xpc/session.h`):
  `XPCPeerRequirement.codeRequirement(_:)`, `.isFromSameTeam(andMatchesSigningIdentifier:)`,
  `XPCReceivedMessage.senderSatisfies(_:)` (macOS 26), `SigningIdentifier.in(_:)`,
  `XPCSession.InitializationOptions.privileged` (`XPC_SESSION_CREATE_MACH_PRIVILEGED`: the job is in
  the privileged bootstrap, "typically ... /Library/LaunchDaemons").
- `sysctlbyname`: "newp ... Requires root-level privileges"; developer.apple.com/documentation/kernel/1387446-sysctlbyname.
- `IOServiceOpen`, `IOConnectCallStructMethod`; developer.apple.com/documentation/iokit. The SMC
  selector and key layout are not Apple API; they stay as `fanctl` has used them.
- Electron `app.setLoginItemSettings`/`getLoginItemSettings` with `type: 'daemonService'`, `serviceName`
  the plist name, status `not-registered`/`enabled`/`requires-approval`/`not-found`.

Not found in the docs, so not relied on: an IOPMAssertion page (power assertions stay in Pod Menu,
no root needed), a public API for `SleepDisabled` and `powermode` (`IOPMSetSystemPowerSetting` and
`IOPMSetPMPreferences` are exported by IOKit.tbd but declared in no public header, so the helper runs
`/usr/bin/pmset` with fixed arguments instead of calling SPI), and the `ifconfig tbr` ioctl
(`SIOCSIFLINKPARAMS` is private; the helper runs `/sbin/ifconfig` with a validated interface and rate).
`SpawnConstraint` was found later, see [A tampered helper](#a-tampered-helper).

The helper executes only these Apple binaries, each with an argument vector built from enums and
validated numbers through `posix_spawn` (no shell): `/usr/bin/pmset`, `/sbin/ifconfig`,
`/usr/bin/mdutil`, `/bin/launchctl`, all on the sealed system volume. It never runs
`/usr/bin/python3` (as root that is the xcrun shim, which can lead into a user-owned Xcode), Homebrew,
anything under `$STATE` or in `/Applications`. Everything else is a syscall or a framework call:
`sysctlbyname`, IOKit (SMC, `IOPMrootDomain`, IOPS), `proc_listallpids`/`proc_pid_rusage`, `kill`,
`PropertyListSerialization`, `rename`/`unlink` with symlink checks.

## Bundle and plist layout

| Item | Name | Where in Pod.app |
|---|---|---|
| helper | `pod-rootd`, signing identifier `codes.pod.rootd` | `Contents/Resources/claude-acc/pod-rootd` (next to `pod-acc-run`) |
| CLI | `pod-rootctl`, signing identifier `codes.pod.rootctl` | `Contents/Resources/claude-acc/pod-rootctl` |
| plist | `codes.pod.app.rootd.plist` | `Contents/Library/LaunchDaemons/codes.pod.app.rootd.plist` |
| Mach service and label | `codes.pod.app.rootd` | |
| state | `/var/db/codes.pod.app.rootd/state.json` (root, 0600) | outside the bundle |

The payload carries the plist as `LaunchDaemons/codes.pod.app.rootd.plist` (from
`launchd/codes.pod.app.rootd.plist`); Pod's `extraFiles` put it in `Contents/Library/LaunchDaemons`.
Both binaries embed an `__info_plist` section with their `CFBundleIdentifier`, so `codesign` keeps the
identifier whoever signs them.

```xml
<key>Label</key><string>codes.pod.app.rootd</string>
<key>BundleProgram</key><string>Contents/Resources/claude-acc/pod-rootd</string>
<key>ProgramArguments</key><array><string>pod-rootd</string></array>
<key>MachServices</key><dict><key>codes.pod.app.rootd</key><true/></dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>ProcessType</key><string>Adaptive</string>
<key>SpawnConstraint</key>
<dict>
  <key>team-identifier</key><string>75Y2KR6P5W</string>
  <key>signing-identifier</key><string>codes.pod.rootd</string>
</dict>
```

- `RunAtLoad`: at boot the helper puts back what the kernel forgot (persisted sysctls, a fixed fan
  setting) and starts fsguard if it is on. That is what the vnodes and iogpu one-shots did.
- `KeepAlive` on a crash only: a crash under a fixed fan setting must not leave the 95 °C rule
  unattended. With nothing to watch (fans on auto, no lease, fsguard off, no sessions for 60 s) the
  helper exits 0 and launchd starts it again on the next connection.
- SIGTERM (reboot, unregister, Login Items switch) hands the fans back to macOS, drops the lid hold
  and session-scoped shaper limits, and keeps the persisted state for the next start.

## Restart safety

The state file holds what was asked and what was there before:

- `fans`: the mode; at start a fixed mode is applied again (the SMC forgets it over a restart).
- `sysctls`: per key the original value (read before the first change, refreshed at each boot from
  the kernel default) and the persisted value; at start the persisted value is applied again.
- `lid`: `heldByUs`. A lease never survives the helper: at start a `SleepDisabled` that we set is
  turned off, and Pod Menu asks again when it reconnects.
- `shaper`: interface, our rate, the rate before, the scope. Session scope is dropped at start;
  `untilReboot` is dropped when the boot time differs from the one recorded.
- `spotlight`: the list before apps-only (only from the first apply), applied or not.
- `power`: the original mode per source before the first change.
- `fsguard`: enabled, limit, over-since, last restart, restart count, generation.
- `legacy`: which old plists were moved, where to.

The file is written atomically (temporary file, `fsync`, `rename`) in a root-only directory.

## Uninstall and restore

`restoreDefaults` (Pod calls it before `SMAppService.unregister()`, `pod-rootctl restore` does it
by hand) puts every section back: fans to auto, `SleepDisabled` off if we set it, each sysctl back to
its saved original, the shaper back to the rate before (or none), the Spotlight list back to the
saved one, the power mode back to its original, fsguard off. It clears the persisted state, so a
later start changes nothing. A sysctl the kernel resets at boot anyway is still set back, so the
change does not wait for a reboot.

Switching the helper off in Login Items stops it with SIGTERM: fans, lid and session limits go back
at once and the sysctls at the next boot, but the Spotlight list and the power mode stay. Pod shows
that state (`requiresApproval` while settings are still applied) and offers the restore once the
helper runs again.

## Migration from the five root daemons

`legacy.migrate`, offered by Pod once the helper is enabled and any of the old plists exist. It
takes over fans, fsguard, iogpu and vnodes; the hotspot daemon stays until `hotspot.py` runs its
controller as the user and sets the limit through `shaper.set` (a follow-up), so hotspot turbo keeps
working in between.

1. reads what they hold: `kern.maxvnodes=N` and `iogpu.wired_limit_mb=N` from their
   `ProgramArguments`, the fan mode from the `fans.json` named by the fans plist (opened with
   `O_NOFOLLOW`, 4 KB at most), fsguard present or not, and the Spotlight list `perf-root.sh` saved
   before apps-only (so `spotlight.restore` still has it);
2. `launchctl bootout system/<label>` for each (the fans daemon hands the fans back on SIGTERM and
   drops its lid hold);
3. moves each plist to `/var/db/codes.pod.app.rootd/legacy/<label>.plist`; the binaries in
   `/usr/local/libexec` stay, for rollback;
4. applies the same settings through its own verbs and persists them (fans mode, sysctls, fsguard
   on).

The labels are a fixed list, nothing else in `/Library/LaunchDaemons` is touched. Running it again
changes nothing.

`legacy.rollback` undoes it: helper features the migration turned on go off (fans auto, persisted
sysctls cleared, fsguard off), each saved plist goes back to `/Library/LaunchDaemons` as root:wheel
0644 and `launchctl bootstrap system` starts it. A later cleanup (after a few releases) can remove
`/usr/local/libexec/claude-acc-{fanctl,fsguard,hotspot}` and the backups.

## Approval UX

Registering is an explicit user gesture in Pod's own UI (an "Enable root helper" button next to
the root features), never automatic and never from a URL scheme. Pod Menu and AccKit only say where
to turn it on when the helper doesn't answer.

1. Pod reads the status: `SMAppService.daemon(plistName: "codes.pod.app.rootd.plist").status`
   (native shell) or `app.getLoginItemSettings({type: 'daemonService', serviceName:
   'codes.pod.app.rootd.plist'})` (Electron).
2. `.notRegistered`: on that click Pod calls `register()`. A daemon is not started until an admin
   approves it, so the status becomes `.requiresApproval`.
3. `.requiresApproval`: a card says what the helper does and that Pod lists it under Login Items >
   Allow in the Background, with one button: `SMAppService.openSystemSettingsLoginItems()`. While
   the card shows (and when Pod becomes active) Pod reads the status again every 2 s.
4. `.enabled`: Pod connects, and if any of the five old plists exist, offers `legacy.migrate`.
5. `.notFound`: the bundle lacks the plist; Pod reports a damaged install.

The same status a user who revoked consent sees (`requiresApproval`) brings back the same card.
`PodRootdService` in `PodRootdClient` wraps these calls for the native shell; Electron uses the API
above (the register call must come from Pod.app's main process: `daemon(plistName:)` resolves the
plist in the calling app's bundle, so Pod Menu and `pod-rootctl` cannot register it).

## Signing

Both binaries: Developer ID Application, team 75Y2KR6P5W, hardened runtime (`--options runtime`),
secure timestamp, no entitlements (the helper is not sandboxed; IOKit's AppleSMC user client,
sysctl, `posix_spawn` and the files it writes need none). Identifiers `codes.pod.rootd` and
`codes.pod.rootctl`; pass `--identifier` explicitly when re-signing.

Pod's Electron binary keeps RunAsNode on (its terminal daemon needs it), which is why
`codes.pod.app` gets tier A only (see [Tiers](#tiers)).

## A tampered helper

`/Applications/Pod.app` belongs to the user when it was dragged there, so
`Contents/Resources/claude-acc/pod-rootd` is writable without root. The question is whether launchd
runs a swapped BundleProgram as root.

Tested on macOS 27 in a scratch folder as the user (nothing registered, `scripts/rootd-tamper-kit.sh`
and the commands in this section). Binaries were signed with an Apple Development certificate of team
75Y2KR6P5W:

| Case | Result |
|---|---|
| the signed binary | runs |
| one byte changed in place | killed at exec, exit 137 (AMFI rejects the invalid page) |
| replaced with another binary, ad hoc signed with the same identifier | runs as the user; `codesign --verify --deep --strict` on the app reports "a sealed resource is missing or invalid" |
| the replaced binary spawned with a launch requirement (`Process.launchRequirement`, team 75Y2KR6P5W + identifier) | killed at spawn, signal 9; the genuine binary runs |
| a binary signed with `--launch-constraint-self` holding `{team-identifier, signing-identifier}` that matches | runs; one that doesn't match is killed, exit 137 |

So the kernel does not stop a swapped binary that carries a valid signature of its own, ad hoc
included. A launch constraint does stop it. Apple documents the one for launchd jobs at
developer.apple.com/documentation/security/applying-launch-environment-and-library-constraints:
"Spawn constraints for launchd daemons and agents are not embedded in the code signature ... add
the constraint to the `SpawnConstraint` key in the launchd property list". The macOS 27 SDK defines
the key (`LAUNCH_JOBKEY_SPAWNCONSTRAINT` in `launch.h`), and `codesign` takes the same dictionary
format. The helper's plist therefore carries `SpawnConstraint` = `{team-identifier: 75Y2KR6P5W,
signing-identifier: codes.pod.rootd}`: a replaced pod-rootd is killed before it runs as root.

The plist lives in the same writable bundle, and editing it breaks the app's seal. What isn't
proven yet is whether launchd/BTM validates the bundle, or keeps its own copy of the plist from
registration, before it honours `SpawnConstraint`. Proving it takes a registered daemon, which is
the user's system. `scripts/rootd-tamper-kit.sh DIR IDENTITY` builds two throwaway apps, one
daemon with the constraint and one without, and prints the steps (register, approve, kickstart,
swap, kickstart, read the log, drop the constraint, reboot). Run them on a scratch Mac or VM. Until
they pass, this is a cutover blocker. If the edited plist wins, the fallback is to install the
helper itself from a `.pkg` into root-only paths, see
[Option: install the helper with a .pkg](#option-install-the-helper-with-a-pkg).

pod-rootd also checks its own signature at start (`anchor apple generic`, team 75Y2KR6P5W,
identifier `codes.pod.rootd`) and exits without touching anything otherwise. That catches an ad hoc
or unsigned build registered by mistake. It can't stop a replacement, which simply won't contain
the check.

## Option: install the helper with a .pkg

Not built yet; written up so the choice between this and a VM run of the tamper kit is concrete.

Making the whole Pod.app root-owned does not close the hole. `/Applications` is `root:admin`
`drwxrwxr-x` without the sticky bit, so any process of an admin user can rename a root-owned
`Pod.app` aside and put another one at the same path. Whether BTM then runs the new bundle's
`BundleProgram` is the same unproven question. App Management doesn't help either: "For signed
apps, macOS allows apps from the same developer — those sharing the same Team ID — to modify the
app's bundle" (`NSUpdateSecurityPolicy`), and agents run as Pod's children.

So the package installs only the helper, outside the bundle, in directories that only root can
write. Pod.app stays a drag-installed, user-owned bundle that updates itself as it does today.

### Install layout

| Path | Owner, mode | What |
|---|---|---|
| `/Library/PrivilegedHelperTools/codes.pod.rootd` | root:wheel 0755 | the helper, the same binary and signature as now |
| `/Library/LaunchDaemons/codes.pod.app.rootd.plist` | root:wheel 0644 | the job |
| `/var/db/codes.pod.app.rootd/` | root 0700 | state, unchanged |
| receipt `codes.pod.rootd.pkg` | | for `pkgutil` and the cask's `uninstall` |

Both directories are `root:wheel drwxr-xr-x`, so an admin user's process can't write them. The
plist differs from the bundle one in two keys: `Program` =
`/Library/PrivilegedHelperTools/codes.pod.rootd` replaces `BundleProgram`, and
`AssociatedBundleIdentifiers` = `[codes.pod.app]` is added. Apple: "If an app installs a legacy
property list, the property list needs to include the AssociatedBundleIdentifiers key with a
value of the app's bundle identifier", and the `Program`'s team must match the app's. Label,
`MachServices`, `RunAtLoad`, `KeepAlive`, `ProcessType` and `SpawnConstraint` stay. The constraint
now guards a root-owned path, so it is defence in depth rather than the only barrier.

`pod-rootctl` stays in Pod.app: it is an unprivileged client and the helper checks its signature.
The `pod-rootd` copy inside Pod.app stays too. launchd never starts it; it is the update source.

### The package

- `pkgbuild --root <dir> --identifier codes.pod.rootd.pkg --version <helper version> --scripts
  <dir>` with the two files above.
- `preinstall`: `launchctl bootout system/codes.pod.app.rootd`, a failure ignored.
- `postinstall`: `launchctl bootstrap system /Library/LaunchDaemons/codes.pod.app.rootd.plist`.
- `productbuild --sign "Developer ID Installer: OUTOFPLACE POLAND SP. Z O.O (75Y2KR6P5W)"`, then
  `notarytool submit --wait` and `stapler staple` (stapler takes flat installer packages).
- Pod's `release.sh` builds it, because the signature needs the release Mac's keychain. It ships
  as `Contents/Resources/claude-acc/pod-rootd.pkg` and as a release asset for the cask.

Prerequisite: a **Developer ID Installer** certificate. Apple's certificate list says it is for
"Sign and distribute a Mac Installer Package, containing your signed app, outside the Mac App
Store". The release keychain holds only the Developer ID Application identity. As with that one,
the Account Holder has to create it.

### Installing and removing it

- **In Pod**: "Enable root helper" opens the bundled package in Installer.app (`shell.openPath`, or
  `NSWorkspace.open` in the native shell). Installer checks the signature and the notarization and
  asks for an administrator. There is no `register()`. The status comes from
  `SMAppService.statusForLegacyPlist(at:)` on the plist path, which Apple documents for helpers
  outside the bundle, and from `pod-rootctl status`. Login Items lists the helper under Pod with
  the usual switch. The docs I found don't say whether macOS also asks to approve an admin-installed
  legacy daemon; check that at the first real install.
- **Through brew**: an optional cask next to `pod`, in outof-place/homebrew-tap.
  `brew install --cask pod-rootd` runs `/usr/sbin/installer -pkg … -target /` through `sudo`, so it
  asks for the password in the terminal (Homebrew's pkg artifact has `requires_sudo? = true`).

  ```ruby
  cask "pod-rootd" do
    version "0.1.0"
    sha256 "<sha256 of pod-rootd-#{version}.pkg>"
    url "https://github.com/outof-place/pod/releases/download/v#{version}/pod-rootd-#{version}.pkg"
    name "Pod root helper"
    desc "Privileged helper for Pod: fans, Stay Awake with the lid closed, sysctls, uplink shaper"
    homepage "https://pod.codes"
    auto_updates true
    depends_on cask: "pod"
    pkg "pod-rootd-#{version}.pkg"
    uninstall early_script: {
                executable: "/Applications/Pod.app/Contents/Resources/claude-acc/pod-rootctl",
                args:       ["uninstall"],
              },
              launchctl:    "codes.pod.app.rootd",
              pkgutil:      "codes.pod.rootd.pkg"
  end
  ```

  The version is Pod's release version, because the package is built with each release.
  `auto_updates true` keeps `brew upgrade` away: the helper updates itself (below), and for a
  package Homebrew can't compare versions, which per its FAQ means it "normally skips the
  self-updating cask". `uninstall` runs `early_script` before `launchctl`. That order matters,
  because SIGTERM keeps persisted settings. `launchctl` and `pkgutil` then clean up when the helper
  is already gone.
- **Removing**: a new tier B verb, `helper.uninstall`. It runs `restoreDefaults`, removes the plist,
  the binary and the state directory, runs `/usr/sbin/pkgutil --forget codes.pod.rootd.pkg`, then
  `launchctl bootout system/codes.pod.app.rootd`, which ends the helper. Pod Menu, `pod-rootctl
  uninstall` and the cask call it. Pod's Electron process can't, being tier A only.

### Updates

- **Pod.app**: unchanged. electron-updater and ShipIt swap the user-owned bundle with no prompt.
  The `pod` cask keeps `app "Pod.app"` and `auto_updates true`.
- **The helper** updates itself from the Pod.app next to it, with no prompt and no package per
  release. After a session from `codes.pod.app` or Pod Menu opens (at most once an hour):
  1. The candidate is `Contents/Resources/claude-acc/pod-rootd` in `/Applications/Pod.app`, or in
     the app the package was opened from (`postinstall` records it). Nothing in a message names a
     path.
  2. The helper copies the candidate with `O_NOFOLLOW`, 64 MB at most, into
     `/Library/PrivilegedHelperTools/.codes.pod.rootd.new`. That copy can't change after the check.
  3. It checks the copy with `SecStaticCodeCheckValidityWithErrors` (`kSecCSStrictValidate`,
     `kSecCSCheckAllArchitectures`) against `anchor apple generic and certificate leaf[subject.OU]
     = "75Y2KR6P5W" and identifier "codes.pod.rootd" and notarized`. The `notarized` clause holds
     for a binary nested in a notarized app (checked with `codesign --verify -R` on a nested Mach-O
     of a notarized app; an ad hoc build fails it).
  4. It reads the copy's `CFBundleVersion` from its `__info_plist`. Only a higher version than its
     own goes on, so a genuine older build can't be swapped back in.
  5. It renames the copy over `codes.pod.rootd`, persists state and exits with a non-zero status.
     `KeepAlive` = `{SuccessfulExit: false}` then starts the new binary.

  The plist changes only with a new package. A release that needs a different plist has Pod offer
  "Update root helper", which opens the bundled package: one administrator prompt.

### The whole app as a .pkg (not recommended)

- It doesn't close the hole (the rename above).
- ShipIt can't write a root-owned bundle. Squirrel.Mac then takes its privileged path:
  `launchPrivileged:` with `AuthorizationCreate` and `SMJobSubmit`, both in Electron 43.7.5's
  `Squirrel.framework`. The SDK marks `SMJobSubmit` deprecated since 10.10, "will be removed in a
  future release". So that means an administrator prompt on every update, if the path still works
  on macOS 27, which is unproven. Pod's serve-update handoff is built on the unprivileged ShipIt
  swap.
- Every user pays an administrator prompt at install and at each update, including those who
  never turn the helper on. There is no drag install, and the cask moves from `app` to `pkg` with
  `sudo` on every upgrade.

### What changes in this branch if the package is chosen

- `launchd/codes.pod.app.rootd.plist`: `Program` and `AssociatedBundleIdentifiers` instead of
  `BundleProgram`. The bundle variant goes.
- `PodRootdService`: status from `statusForLegacyPlist(at:)`, install by opening the package, no
  `register()`/`unregister()`.
- The engine: the self-update steps and `helper.uninstall`, tested against the fake backend
  (version order, symlinked candidate, copy before check, check failure leaves the live binary).
- The Pod side (b2): the button opens the package instead of `setLoginItemSettings`.
- Packaging (nt-packaging): the Installer identity and the `pkgbuild`/`productbuild`/notarize step.
- The tamper kit stops being a blocker, because no launchd job points into a writable path.
  Verbs, tiers, the peer requirement, the migration from the five root daemons and the restore
  stay as they are.

## Cutover

Nothing in this branch registers or installs anything. The real cutover is a manual step with the
user present (Login Items approval, Touch ID); the command list goes to the lead with the PR.

What has to land before it, or the migration leaves a feature without its daemon:

- Pod Menu talks to the helper: fan mode and the lid hold through `PodRootdClient` (one client
  kept while Stay Awake with the lid closed is on), fan readings from `SMCKit` instead of
  `fans-state.json`. Until then the old fans daemon is what follows `fans.json` and `awake.json`.
- `perf-root.sh` and `janitor-root.sh` call `pod-rootctl` when the helper is there, `sudo` otherwise.
- `hotspot.py` as the user with `pod-rootctl shaper follow`, then `hotspot` joins `Engine.migrating`.
- Pod registers the daemon as `daemonService` and signs both binaries (see Signing). The payload
  carries `pod-rootd`, `pod-rootctl` and `LaunchDaemons/codes.pod.app.rootd.plist`
  (`scripts/payload.sh`). With `--pod-agents`, `setup.sh` links `$STATE/pod-rootctl` when the owner
  app has the helper: `claude-acc rootd ...` runs it, and `claude-acc fans install` points there.
- The tampered-helper steps above pass, or the helper ships as a `.pkg` (see the option above).

## Code

- `app/Sources/PodRootdProtocol`: verbs, validated parameter types, replies, status, names.
- `app/Sources/PodRootdClient`: `PodRootdClient` (XPC session, async calls, leases) and
  `PodRootdService` (SMAppService status, register, open Login Items). Also exported from the
  repository's root `Package.swift`, so Pod can depend on it by URL.
- `app/Sources/PodRootdCore`: the engine (policy, rate limits, state, leases, fan safety, fsguard
  logic, migration, restore) against a `Backend` protocol; `FakeBackend` for tests and `--fake`.
- `app/Sources/SMCKit`: the SMC and fan code, shared by `fanctl`, the helper and Pod Menu.
- `app/Sources/pod-rootd`: the listener and the real backend.
- `app/Sources/pod-rootctl`: the CLI for scripts (`status`, every verb, `shaper follow`).
- `app/Tests/PodRootdTests`: validation, peer rejection, the tier table and the CLI's approval
  grace, idempotency, rate limits, leases, restart, migration and restore against the fake backend.
- `scripts/rootd-tamper-kit.sh`: the throwaway apps for the tampered-helper test.
