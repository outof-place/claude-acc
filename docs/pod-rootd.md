# pod-rootd: one root helper for Pod

pod-rootd replaces claude-acc's five root LaunchDaemons and its `sudo` scripts with one
privileged helper. Pod.app ships it inside a signed installer package that puts it in root-only
paths as a launch daemon, and clients reach it over XPC. The helper accepts a fixed set of typed verbs. It runs no shell, takes no free
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
| `helper.uninstall` | none | removing the five daemons by hand | `restoreDefaults`, then the package's files in `/Library`, `pkgutil --forget`, its own `launchctl bootout` | `helperVersion` |
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
| A: harmless, rate-limited | `status`, `fans.set`, `lid.hold`, `lid.release`; `shaper.set` scoped to the session at 6 Mb/s or more; `shaper.clear` of the caller's own session limit | yes | yes, no prompt | yes, except `lid.hold` |
| B: changes the system | `power.mode`, `sysctl.*`, `shaper.*`, `spotlight.*`, `fsguard.set`, `launchd.parkOrphans`, `logs.pruneDiagnostics`, `legacy.*`, `restoreDefaults` | yes, no prompt: the click is the consent | only approved | never |

- Tier A can't do lasting harm. A fan setting keeps the 95 °C rule. A lid hold ends with its
  session (a minute later, see [Restart safety](#restart-safety)), at 24 h, at 10 % battery and
  when the Mac gets hot. A session's upload limit ends with
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
  `codes.pod.app`. Electron Pod reaches tier B only through Pod Menu or `pod-rootctl`. For the
  same reason a lid hold from `codes.pod.app` is tier B: a closed-lid hold keeps a Mac awake in a
  bag, and Stay Awake is Pod Menu's anyway.
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

- SMAppService: `statusForLegacyPlist(at:)` for "apps that don't use the new bundle structure",
  `openSystemSettingsLoginItems()`, `Status.requiresApproval` (also returned when the user switches
  the helper off); `AssociatedBundleIdentifiers` ("If an app installs a legacy property list, the
  property list needs to include the AssociatedBundleIdentifiers key with a value of the app's
  bundle identifier", and the `Program`'s team must match the app's);
  developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos.
- `NSUpdateSecurityPolicy` ("For signed apps, macOS allows apps from the same developer — those
  sharing the same Team ID — to modify the app's bundle");
  developer.apple.com/documentation/bundleresources/information-property-list/nsupdatesecuritypolicy.
- `SecStaticCodeCreateWithPath`, `SecStaticCodeCheckValidityWithErrors` (TN3127),
  `kSecCSStrictValidate`, `kSecCSCheckAllArchitectures`, `kSecCodeInfoPList` ("the secured Info.plist
  as seen by code signing") in the SDK's `SecStaticCode.h` and `SecCode.h`.
- Developer ID Installer: "Sign and distribute a Mac Installer Package, containing your signed app,
  outside the Mac App Store"; developer.apple.com/help/account/certificates/certificates-overview.
  `stapler` takes flat installer packages; `notarytool submit --wait`.
- Homebrew's Cask Cookbook (`pkg` "must always be accompanied by `uninstall`", the uninstall keys and
  their order, `auto_updates`) and the FAQ on self-updating casks; docs.brew.sh. The `pkg` artifact
  runs `/usr/sbin/installer -pkg … -target /` through `sudo` (Homebrew's `cask/artifact/pkg.rb`).
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

Not found in the docs, so not relied on: an IOPMAssertion page (power assertions stay in Pod Menu,
no root needed), a public API for `SleepDisabled` and `powermode` (`IOPMSetSystemPowerSetting` and
`IOPMSetPMPreferences` are exported by IOKit.tbd but declared in no public header, so the helper runs
`/usr/bin/pmset` with fixed arguments instead of calling SPI), and the `ifconfig tbr` ioctl
(`SIOCSIFLINKPARAMS` is private; the helper runs `/sbin/ifconfig` with a validated interface and rate).
`SpawnConstraint` was found later, see [Why a package](#why-a-package-a-tampered-helper).

The helper executes only these Apple binaries, each with an argument vector built from enums and
validated numbers through `posix_spawn` (no shell): `/usr/bin/pmset`, `/sbin/ifconfig`,
`/usr/bin/mdutil`, `/bin/launchctl`, `/usr/sbin/pkgutil` (`--forget` at uninstall), all on the sealed
system volume. It never runs
`/usr/bin/python3` (as root that is the xcrun shim, which can lead into a user-owned Xcode), Homebrew,
anything under `$STATE` or in `/Applications`. Everything else is a syscall or a framework call:
`sysctlbyname`, IOKit (SMC, `IOPMrootDomain`, IOPS), `proc_listallpids`/`proc_pid_rusage`, `kill`,
`PropertyListSerialization`, `rename`/`unlink` with symlink checks.

## Install layout

Pod.app stays a drag-installed bundle that belongs to the user and updates itself (ShipIt). The
helper doesn't run from it: Pod's signed package installs it outside the bundle, in directories that
only root can write.

| Path | Owner, mode | What |
|---|---|---|
| `/Library/PrivilegedHelperTools/codes.pod.app.rootd` | root:wheel 0755 | the helper, signing identifier `codes.pod.rootd` |
| `/Library/LaunchDaemons/codes.pod.app.rootd.plist` | root:wheel 0644 | the job |
| `/var/db/codes.pod.app.rootd/` | root 0700 | state, the old daemons' plists after `legacy.migrate`, the Pod.app the package came from (`app`) |
| receipt `codes.pod.rootd.pkg` | | for `pkgutil --forget` and the cask's `uninstall` |
| Pod.app `Contents/Resources/claude-acc/` | the user's | `pod-rootd` (the update source, never started by launchd), `pod-rootctl`, `pod-rootd.pkg` |

Both system directories are `root:wheel` and `drwxr-xr-x` (`PrivilegedHelperTools` also has the
sticky bit), so a process of an admin user can't write them. The program and the job are named after
the service (`<Pod's bundle id>.rootd`), so another Pod build gets its own.

```xml
<key>Label</key><string>codes.pod.app.rootd</string>
<key>Program</key><string>/Library/PrivilegedHelperTools/codes.pod.app.rootd</string>
<key>ProgramArguments</key><array><string>codes.pod.app.rootd</string></array>
<key>AssociatedBundleIdentifiers</key><array><string>codes.pod.app</string></array>
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

- `AssociatedBundleIdentifiers`: Login Items lists the helper under Pod, with its switch.
- `SpawnConstraint`: launchd kills anything at that path that isn't `codes.pod.rootd` of Pod's team
  before it runs. With the program in a root-only directory this is defence in depth.
- `RunAtLoad`: at boot the helper puts back what the kernel forgot (persisted sysctls, a fixed fan
  setting) and starts fsguard if it is on. That is what the vnodes and iogpu one-shots did.
- `KeepAlive` on a non-zero exit only: a crash under a fixed fan setting must not leave the 95 °C
  rule unattended, and a self-update exits with `EX_TEMPFAIL` to get the new binary started. With
  nothing to watch (fans on auto, no lease, fsguard off, no sessions for 60 s) the helper exits 0 and
  launchd starts it again on the next connection.
- SIGTERM (reboot, bootout, the Login Items switch) hands the fans back to macOS, drops the lid hold
  and session-scoped shaper limits, and keeps the persisted state for the next start.
- The helper takes its service name from its own file name (`<app id>.rootd`); both binaries embed an
  `__info_plist` with `CFBundleIdentifier` and `CFBundleVersion`, so `codesign` keeps the identifier
  and binds the version whoever signs them.

### The package

`scripts/rootd-pkg.sh --binary pod-rootd --version N --out pod-rootd.pkg [--sign IDENTITY]` (the
payload carries it in `rootd/`, next to the job's plist):

- `pkgbuild --root <dir> --identifier codes.pod.rootd.pkg --version N --scripts <dir> --ownership
  recommended --install-location /`, then `productbuild --package`, with `--sign` when given. The
  PackageInfo says `relocatable="false"`, `auth="root"` and `overwrite-permissions="true"`, so the
  payload's directories carry macOS's own modes (0755, and 1755 for `PrivilegedHelperTools`).
- `preinstall`: `launchctl bootout system/codes.pod.app.rootd`, a failure ignored.
- `postinstall`: when the package was opened from inside a Pod.app (`$1` ends in
  `.app/Contents/Resources/claude-acc/pod-rootd.pkg`), that app's path goes to
  `/var/db/codes.pod.app.rootd/app`; then `launchctl bootstrap system
  /Library/LaunchDaemons/codes.pod.app.rootd.plist`.
- Pod's `release.sh` builds it with the release's `pod-rootd`, signs it with
  `productbuild --sign "Developer ID Installer: OUTOFPLACE POLAND SP. Z O.O (75Y2KR6P5W)"`, submits
  it with `notarytool submit --wait`, staples it, and ships it as
  `Contents/Resources/claude-acc/pod-rootd.pkg` and as a release asset for the cask.
- The tests build it unsigned and read it back (`pkgutil --expand-full`, `lsbom`); nothing installs
  it. The files' `com.apple.provenance` shows up in the Bom as `._` entries, which Installer folds
  back into extended attributes (the expanded payload has no `._` files).

## Restart safety

The state file holds what was asked and what was there before:

- `fans`: the mode; at start a fixed mode is applied again (the SMC forgets it over a restart).
- `sysctls`: per key the original value (read before the first change, refreshed at each boot from
  the kernel default) and the persisted value; at start the persisted value is applied again.
- `lid`: `heldByUs`. A lease never survives the helper or its session, but the hold does for a
  minute (`Engine.lidRehold`): after a restart, or a session that ended without `lid.release`, a
  `SleepDisabled` that we set stays on until Pod Menu holds it again or the minute passes. A Mac with
  the lid closed doesn't sleep through a Pod update or a helper crash. The minute only keeps a hold
  of ours, it never takes one. A release, SIGTERM, 10 % battery and heat end it at once.
- `shaper`: interface, our rate, the rate before, the scope. Session scope is dropped at start;
  `untilReboot` is dropped when the boot time differs from the one recorded.
- `spotlight`: the list before apps-only (only from the first apply), applied or not.
- `power`: the original mode per source before the first change.
- `fsguard`: enabled, limit, over-since, last restart, restart count, generation.
- `legacy`: which old plists were moved, where to.

The file is written atomically (temporary file, `fsync`, `rename`) in a root-only directory.

## Uninstall and restore

`restoreDefaults` (`pod-rootctl restore`) puts every section back: fans to auto, `SleepDisabled` off
if we set it, each sysctl back to its saved original, the shaper back to the rate before (or none),
the Spotlight list back to the saved one, the power mode back to its original, fsguard off. It clears
the persisted state, so a later start changes nothing. A sysctl the kernel resets at boot anyway is
still set back, so the change does not wait for a reboot.

`helper.uninstall` (tier B: Pod Menu on a click, `pod-rootctl uninstall` with Touch ID, the cask's
`early_script`) runs `restoreDefaults` first; if any part of it fails, nothing is removed. Then it
removes the job's plist, the program, `state.json` and `app`, runs `pkgutil --forget
codes.pod.rootd.pkg`, replies, and at the next tick runs `launchctl bootout` of its own job, which ends
it. The old daemons' plists stay in `/var/db/codes.pod.app.rootd/legacy` for a rollback by hand.

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
   before apps-only (so `spotlight.restore` still has it). That file is then renamed to
   `spotlight-exclusions.json.migrated` in its folder, every folder from the home opened without
   following a link (`UserFiles.rename`), so after a restore neither the root copy nor `rootroute.py`
   takes it for the live list;
2. `launchctl bootout system/<label>` for each (the fans daemon hands the fans back on SIGTERM and
   drops its lid hold);
3. moves each plist to `/var/db/codes.pod.app.rootd/legacy/<label>.plist`; the binaries in
   `/usr/local/libexec` stay, for rollback;
4. applies the same settings through its own verbs and persists them (fans mode, sysctls, fsguard
   on).

The labels are a fixed list, nothing else in `/Library/LaunchDaemons` is touched. Running it again
changes nothing.

`legacy.rollback` undoes it: helper features the migration turned on go off (fans auto, persisted
sysctls cleared, fsguard off, the helper's copy of the Spotlight list dropped and the file renamed
back), each saved plist goes back to `/Library/LaunchDaemons` as root:wheel 0644 and `launchctl
bootstrap system` starts it. A later cleanup (after a few releases) can remove
`/usr/local/libexec/claude-acc-{fanctl,fsguard,hotspot}` and the backups.

## Installing and removing it

Installing is an explicit user gesture in Pod's own UI (an "Enable root helper" button next to the
root features), never automatic and never from a URL scheme. Pod Menu and AccKit only say where to
turn it on when the helper doesn't answer.

1. Pod reads the status: `SMAppService.statusForLegacyPlist(at: /Library/LaunchDaemons/
   codes.pod.app.rootd.plist)` (`PodRootdService.step`, `pod-rootctl service status`) and whether
   the helper answers.
2. `.notRegistered` or `.notFound` (step `install`): on that click Pod opens the bundled
   `pod-rootd.pkg` (`shell.openPath`, `NSWorkspace.open`, `pod-rootctl service install`). Installer
   checks the signature and the notarization and asks for an administrator. Pod registers nothing.
3. `.requiresApproval` (step `approve`): the helper is installed but switched off in Login Items. A
   card says what it does, with one button: `SMAppService.openSystemSettingsLoginItems()`.
4. `.enabled` (step `ready`): Pod connects, and if any of the five old plists exist, offers
   `legacy.migrate`.

The docs I found don't say whether macOS also asks to approve an admin-installed legacy daemon
before it first runs; the first real install shows it, and step 3 covers it either way.

Through Homebrew, an optional cask next to `pod`, in outof-place/homebrew-tap:

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

- `brew install --cask pod-rootd` runs `/usr/sbin/installer -pkg … -target /` through `sudo`, so it
  asks for the password in the terminal.
- The version is Pod's release version, because the package is built with each release.
  `auto_updates true` keeps `brew upgrade` away: the helper updates itself, and for a package
  Homebrew can't compare versions, which per its FAQ means it "normally skips the self-updating cask".
- `uninstall` runs `early_script` before `launchctl`. That order matters, because SIGTERM keeps the
  persisted settings. `launchctl` and `pkgutil` then clean up when the helper is already gone.

## Updates

- **Pod.app**: unchanged. electron-updater and ShipIt swap the user-owned bundle with no prompt. The
  `pod` cask keeps `app "Pod.app"` and `auto_updates true`.
- **The helper** updates itself from Pod.app, with no prompt and no package per release. After a
  message from Pod or Pod Menu, at most once an hour per process (the CLI's messages don't count):
  1. The candidates are `Contents/Resources/claude-acc/pod-rootd` in the app the package was opened
     from (`/var/db/codes.pod.app.rootd/app`), then in `/Applications/Pod.app`. Nothing in a message
     names a path, and only a helper running as its installed program updates.
  2. The helper copies a candidate, opened with `O_NOFOLLOW`, a regular file of at most 64 MB, into
     `/Library/PrivilegedHelperTools/.codes.pod.app.rootd.new` (`HelperFiles.stage`). Nothing but
     root can change that copy after the check.
  3. It checks the copy with `SecStaticCodeCheckValidityWithErrors` (`kSecCSStrictValidate`,
     `kSecCSCheckAllArchitectures`) against `anchor apple generic and certificate leaf[subject.OU] =
     "75Y2KR6P5W" and identifier "codes.pod.rootd" and notarized`. The `notarized` clause holds for a
     binary nested in a notarized app (checked with `codesign --verify -R` on a nested Mach-O of a
     notarized app; an ad hoc build fails it).
  4. It reads the copy's `CFBundleVersion` from the Info.plist its signature binds
     (`kSecCodeInfoPList`). Only a higher version than its own goes on, so a genuine older build
     can't be swapped back in. The version is the app's build number (`app/Info.plist`); a test keeps
     the two equal, so every release bump moves both.
  5. It renames the copy over its program, persists state and exits with `EX_TEMPFAIL`; `KeepAlive`
     starts the new binary. A lid hold stays on through the restart (see Restart safety).

  The job's plist changes only with a new package. A release that needs a different plist has Pod
  offer "Update root helper", which opens the bundled package: one administrator prompt.

## Signing

Both binaries: Developer ID Application, team 75Y2KR6P5W, hardened runtime with library validation
(`--options runtime,library`), secure timestamp, no entitlements (the helper is not sandboxed;
IOKit's AppleSMC user client, sysctl, `posix_spawn` and the files it writes need none). Identifiers
`codes.pod.rootd` and `codes.pod.rootctl`; pass `--identifier` explicitly when re-signing. The app is
notarized with them inside, which is what the self-update's `notarized` clause checks.

The package: Developer ID Installer of the same team ("Sign and distribute a Mac Installer Package,
containing your signed app, outside the Mac App Store"), then notarized and stapled. The release
keychain holds only the Developer ID Application identity today; the Account Holder creates the
Installer one.

The helper's peer requirement (`PeerPolicy.production`) checks more than team and identifier:

| Caller | Required of the running process |
|---|---|
| all | team 75Y2KR6P5W, validation category Developer ID, `isHardenedRuntimeEnforced` |
| Pod Menu, `pod-rootctl` | also `isLibraryValidationRequired` |

So Pod signs Pod Menu and `pod-rootctl` with `--options runtime,library`, and without the
`allow-dyld-environment-variables`, `disable-library-validation` and `get-task-allow`
entitlements. An Apple Development copy (claude-acc's own `~/Applications/Claude Acc.app`) or a
non-hardened build with the right team and identifier is turned away, so `DYLD_INSERT_LIBRARIES`
into a tier B caller gets nothing. The flags are the process's own at run time. On macOS 27 a
hardened process carries `CS_REQUIRE_LV` only when it is signed with `library` (measured: a
team-signed `--options runtime` binary reports `0x62011311`, with `runtime,library` it reports
`0x62013301`). Pod's Electron process loads native modules and gets tier A only, so it needs the
hardened runtime, not library validation.

A debuggable caller is refused too, whatever it is signed as: a build with
`com.apple.security.get-task-allow` (`CS_GET_TASK_ALLOW`) or a process a debugger is attached to
(`CS_DEBUGGED`). Pod's terminals have Developer Tools access (Ultra's `devtools` tweak), so anything
they run could take over such a process with `task_for_pid`. Lightweight code requirements have no
"not", so each message is tested against `isDebuggable` and `isDebugged` (`PeerPolicy.refused`) and
refused when it matches. Measured: a binary with the entitlement runs with csflags `0x22011315`, one
without with `0x22011311`, and only the first launches under a launch requirement of `isDebuggable`.
SwiftPM's test runner is debuggable, and the PeerTests see it refused.

Pod's Electron binary keeps RunAsNode on (its terminal daemon needs it), which is why
`codes.pod.app` gets tier A only (see [Tiers](#tiers)).

## Why a package: a tampered helper

`/Applications/Pod.app` belongs to the user when it was dragged there, so a helper started from inside
it could be swapped without root. That is why launchd runs the package's copy in
`/Library/PrivilegedHelperTools` instead.

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

So the kernel does not stop a swapped binary that carries a valid signature of its own, ad hoc
included. A launch constraint does stop it, which is why the job carries `SpawnConstraint`
(developer.apple.com/documentation/security/applying-launch-environment-and-library-constraints:
"Spawn constraints for launchd daemons and agents are not embedded in the code signature ... add the
constraint to the `SpawnConstraint` key in the launchd property list"; `LAUNCH_JOBKEY_SPAWNCONSTRAINT`
in the SDK's `launch.h`). With a job in the user's bundle, an edited plist could drop the constraint,
and whether launchd/BTM would notice was never proven. With the job and the program in root-only
directories the question doesn't come up. `scripts/rootd-tamper-kit.sh` builds the throwaway apps for
that test; it is informative now, not a blocker.

Two cheaper ideas don't close the hole:

- A root-owned Pod.app: `/Applications` is `root:admin` `drwxrwxr-x` without the sticky bit, so any
  process of an admin user can rename the bundle aside and put another one at the same path.
- App Management: macOS lets apps of the same team modify the bundle (`NSUpdateSecurityPolicy`), and
  agents run as Pod's children.

The whole app as a package would also break Pod's updates. ShipIt can't write a root-owned bundle, so
Squirrel.Mac takes its privileged path (`launchPrivileged:` with `AuthorizationCreate` and
`SMJobSubmit`, both in Electron 43.7.5's `Squirrel.framework`; the SDK marks `SMJobSubmit` deprecated
since 10.10, "will be removed in a future release"): an administrator prompt at every update, if it
works on macOS 27 at all, for every user, including those who never turn the helper on.

pod-rootd also checks its own signature at start (`anchor apple generic`, team 75Y2KR6P5W,
identifier `codes.pod.rootd`) and exits without touching anything otherwise. That catches an ad hoc
or unsigned build installed by mistake.

## Cutover

Nothing in this branch installs anything. The first install is a manual step with the user present
(Installer's administrator prompt, Touch ID for the CLI); the command list goes to the lead with the
PR.

What has to land before it, or the migration leaves a feature without its daemon:

- Pod Menu talks to the helper: fan mode and the lid hold through `PodRootdClient`, fan readings from
  `SMCKit` (#110).
- `perf-root.sh` and `janitor-root.sh` call `pod-rootctl` when the helper is there: `rootroute.py`
  (the wrapper's `perf-root` and `mac root-clean` in Pod), ahead of 1.31.2's root copy
  (`root-run.sh`), which stays the path when the helper doesn't answer or an old daemon still owns
  the tweak (#113).
- `hotspot.py` as the user with `pod-rootctl shaper follow`, and `hotspot` in `Engine.migrating`
  (#116).
- Pod: the "Enable root helper" button opens the package (b2); `release.sh` signs `pod-rootd` and
  `pod-rootctl` with `--options runtime,library`, builds the package with `rootd/rootd-pkg.sh`, signs
  it with the Developer ID Installer identity, notarizes and staples it, and ships it in
  `Contents/Resources/claude-acc/` and as a release asset (nt-packaging). With `--pod-agents`,
  `setup.sh` links `$STATE/pod-rootctl` when the owner app carries it: `claude-acc rootd ...` runs it,
  and `claude-acc fans install` points there.
- The Developer ID Installer certificate exists (the Account Holder's step).

## Code

- `app/Sources/PodRootdProtocol`: verbs, validated parameter types, replies, status, names and the
  install layout.
- `app/Sources/PodRootdClient`: `PodRootdClient` (XPC session, async calls, leases; letting go of it
  cancels the session) and `PodRootdService` (`statusForLegacyPlist`, the package's place in Pod.app,
  Login Items). Also exported from the repository's root `Package.swift`, so Pod can depend on it by
  URL.
- `app/Sources/PodRootdCore`: the engine (policy, rate limits, state, leases, fan safety, fsguard
  logic, migration, restore, self-update, uninstall) against a `Backend` protocol; `FakeBackend` for
  tests and `--fake`; `HelperFiles` for the self-update's copy.
- `app/Sources/SMCKit`: the SMC and fan code, shared by `fanctl`, the helper and Pod Menu.
- `app/Sources/pod-rootd`: the listener and the real backend.
- `app/Sources/pod-rootctl`: the CLI for scripts (`status`, every verb, `shaper follow`, `uninstall`,
  `service status|install|open-settings`).
- `app/Tests/PodRootdTests`: validation, peer rejection, the tier table and the CLI's approval
  grace, idempotency, rate limits, leases, restart, migration, restore, self-update and uninstall
  against the fake backend, the staging copy against real files.
- `scripts/rootd-pkg.sh` and `tests/test_rootd_pkg.py`: the package and its layout.
- `scripts/rootd-tamper-kit.sh`: the throwaway apps for the tampered-helper test.
