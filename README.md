<div align="center">

<img src="docs/panel.png" width="900" alt="Claude Acc panel in four columns: the active Claude account with session and weekly usage and the other accounts; dev servers with a memory chart against the budget and what the guard will do, plus free disk space; Stay Awake, then CPU and GPU load, fans and temperatures with a 20-minute chart; Ultra with each tweak measured before and after">

# claude-acc

**A menu bar control room for a Mac that runs Claude Code agents all day.**

It rotates your Claude subscriptions before one hits the wall, keeps the agents' dev servers from eating your RAM, cleans up what they leave on disk, holds the Mac awake on a hotspot, and spins the fans up before the chip cooks.

[Install](#install) · [What it does](#what-it-does) · [How it works](#how-it-works) · [Numbers](#numbers) · [Command line](#command-line)

![macOS 26+](https://img.shields.io/badge/macOS-26%2B-111?logo=apple) ![Swift 6.2](https://img.shields.io/badge/Swift-6.2-F05138?logo=swift&logoColor=white) ![Python](https://img.shields.io/badge/Python-stdlib%20only-3776AB?logo=python&logoColor=white) ![License MIT](https://img.shields.io/badge/license-MIT-2ea44f)

</div>

## Install

```sh
brew install outof-place/tap/claude-acc
claude-acc-setup          # scripts, launchd jobs and the menu bar app, into your account
claude-acc fans install   # optional: fan control, a small root helper (asks for Touch ID)
claude-acc hotspot on     # optional: Hotspot turbo, a small root helper (asks for Touch ID)
```

Or from source:

```sh
git clone https://github.com/outof-place/claude-acc.git
cd claude-acc
./install.sh              # builds the app and fanctl, then runs setup.sh
./install-fans.sh         # optional, root
./install-fsguard.sh      # optional, root: restarts a bloated fseventsd
```

Setup copies the scripts to `~/.local/share/claude-acc`, adds a `claude-acc` command to `~/.local/bin`, loads its launchd jobs (the account watcher, the janitor, the dev server guard, Ultra's keeper and the updater) and opens `~/Applications/Claude Acc.app`, which adds itself to your login items and puts itself back after every reinstall, unless you turn **Open at Login** off in the panel. It also adds its [limit hooks](#how-the-pause-works) to `~/.claude/settings.json`, next to your own hooks (the alarm for sessions that hit a limit, and the pause hooks when the optional pause is on), keeping a copy of the file from before the first change in `settings.json.bak-claude-acc`; run setup with `CLAUDE_ACC_NO_HOOKS=1` to leave them out. To stop agents from starting a second dev server of the same app, add the [Claude Code hook](#dev-server-guard).

After `brew upgrade claude-acc`, run `claude-acc-setup` again to put the new version in place. `claude-acc uninstall` removes the launchd jobs, the app, the command and the limit pause hooks and keeps your settings in `~/.local/share/claude-acc`; `claude-acc fans uninstall` gives the fans back to macOS first.

## What it does

| | |
| --- | --- |
| **Claude accounts** | Session and weekly usage of every subscription in the menu bar. Moves your running Claude Code sessions to the account with the most headroom once the active one is down to 5% of the session or 3% of the week, with no restart and no `/login`, skipping accounts whose subscription was canceled. When no account is left, it can [use up the last few percent of every account](#using-up-every-account) (`claude-acc drain on`), then tells you once; with the optional [limit pause](#how-the-pause-works) (`claude-acc pause on`) it also stops your sessions at a checkpoint instead of letting agents run into the wall, and wakes them when limits come back. Keeps [`depot claude`](#depot-sandboxes) sandboxes on another account than the laptop. Click any account for its plan, subscription start, renewal and both resets to the minute. |
| **Dev server guard** | Agents in [Orca](https://github.com/stablyai/orca) each run their own `next dev` with a preview tab, and Turbopack grows to 6-9 GB per server under their edits. The guard watches every dev server's real memory (the number macOS kills by), knows who is looking at it, restarts a bloated one in its own Orca terminal in seconds, stops duplicates, orphans and loops, and turns away an agent about to start a second server of the same app. |
| **Janitor** | Removes what a build or an install brings back (`.next`, `.turbo`, stale `node_modules`, Go and npm caches, Docker leftovers) when nobody is using it, at login and every 3 hours, and keeps folders that agents fill without end under a size cap. |
| **Stay Awake** | Like Amphetamine: awake until you say so or for 1-8 hours, optionally with the display on, even with the lid closed. Turns on by itself on any hotspot (iPhone over Wi-Fi or USB, Android, cellular) and keeps the hotspot from dozing off. With [Hotspot turbo](#hotspot-turbo) on, an iPhone hotspot stops queueing every session's requests behind one session's upload. |
| **Load & heat** | CPU load split into performance and efficiency cores, GPU load, and P-core, E-core, GPU, SSD and battery temperatures with a 20-minute chart. Fans on Auto, 50%, 75% or Max, going full speed whenever a chip passes 95 °C, and never fighting another fan app. |
| **Updates** | Everything on the Mac brought to its newest version every 3 days: Homebrew formulae and casks, global npm packages, Go programs, Python packages (rolled back if anything conflicts or stops importing), Python itself, Claude Code with its plugins and skills. Pins are respected and hand-edited skills are left alone. The panel shows when it last worked, what each part did and why something failed, and an Update button runs it now. |
| **Mail gateway** | Your agents read and answer the company mail without a password or key on disk: several mailboxes, Google Workspace through domain-wide delegation and any IMAP/SMTP server, behind one [MCP server](#mail-gateway) every Claude Code session gets. Each mailbox has its own level (read, modify, draft) and sending is off unless you allow it, so an agent leaves a draft for you. What an email says is treated as untrusted data, and every call lands in an audit log. |
| **Browser gateway** | Your agents use your own Chrome or Brave, with your logins, in tabs that never take focus: hidden tabs with no window or tab strip entry, one connection the browser approves once per start, shared by every Claude Code session through one [MCP server](#browser-gateway). The tools are Anthropic's browser use toolset, the one Claude models are trained on (`navigate`, `read_page`, `find`, `left_click`, `type`...), and the same toolset drives your browser from the Python and TypeScript SDKs. Agents see only their own tabs, hand a tab to you when a login, captcha or payment needs a human, and in the default guarded mode can't open browser settings, keep banks read-only and ask before running JavaScript or uploading a file. |
| **API credits** | Max plans come with monthly API credits ($200 on Max 20x, $100 on Max 5x), one Console organization per plan, and they expire every billing cycle. [`claude-acc credits`](#api-credits) keeps the keys of all those organizations in the Keychain as one pool and runs your scripts and `claude -p` jobs on the credit that expires first, so less of it goes to waste. The panel shows what is left in total and what expires next. |
| **Desktop gateway** | Your agents drive the whole macOS desktop with Anthropic's computer use toolset, the one Claude models are trained on (`screenshot`, `zoom`, `left_click`, `type`, `key`, `scroll`...), behind one [MCP server](#desktop-gateway) every Claude Code session gets. A native helper captures the screen (ScreenCaptureKit) and posts real mouse and keyboard events, with the two permissions it needs pinned to a stable code signature so they survive rebuilds. Full mode by default, with a deny list of secret apps (Keychain, Passwords, 1Password, the Settings Privacy panes) that stays read-only even then, focus never taken except by a click, and every call in an audit log without the typed text. |
| **Dictation** | Tap the right ⌘, talk, tap it again and the text is at the cursor: in Claude Code in any terminal, or in any text field. Recorded in the app itself, transcribed by MAI-Transcribe 2 through Vercel AI Gateway with zero data retention and a dictionary of the repo's terms, then a correction pass for the spelling of names. A small pill appears only while you dictate. See [Dictation](#dictation). |
| **Ultra** | One switch that tunes the Mac for agent work. Helpers no agent waits on move to the efficiency cores, memory hooks stop holding up every tool call, Node starts warm for everything sessions spawn, and the guard frees dev server memory sooner. Each change is measured before and after, and Off puts back exactly what was there. |

<img src="docs/panel-details.png" width="900" alt="The same panel with one account opened: plan, subscription status and start, renewal date with a countdown, the 5-hour and weekly windows with their exact resets, place in the switching order, and buttons to switch or sign in again">

The UI is in English; the scripts' messages and the code comments are in Polish.

## How it works

```mermaid
flowchart LR
    app["Claude Acc.app<br/>menu bar"]
    subgraph user["launchd, your account"]
        tick["accswitch.py<br/>every 2 min"]
        guard["devguard.py<br/>every 5 s"]
        janitor["janitor.py<br/>login + every 3 h"]
        perf["perf.py keep<br/>every 5 min"]
        updates["updates.py<br/>04:30, every 3 days"]
    end
    subgraph root["launchd, root"]
        fans["fanctl<br/>every 2 s"]
    end
    hook["Claude Code<br/>PreToolUse hook"]
    pausehooks["Claude Code<br/>pause hooks"]
    pause[("pause.json")]
    keychain[("Keychain<br/>Claude + Orca entries")]
    api[("Anthropic API<br/>usage, profile")]
    orca["Orca<br/>tabs, terminals, agents"]
    disk[("build caches<br/>node_modules")]
    tuned[("helpers, settings.json<br/>Docker, devguard.json")]
    smc[("SMC<br/>fans, sensors")]
    pkgs[("Homebrew, npm, Go,<br/>Python, Claude plugins")]
    app -.-> tick & guard & janitor & perf & fans & updates
    tick --> keychain & api & pause
    pausehooks --> pause
    guard --> orca
    janitor --> disk
    perf --> tuned
    updates --> pkgs
    fans --> smc
    hook --> guard
```

- Every piece runs without the app. Each job writes its state to `~/.local/share/claude-acc`, and the app (dotted lines) only reads those files, calls the scripts and writes the fan mode. CPU and GPU load it reads from the kernel itself. Close it and the switching, cleanup, guard and fans keep working.
- The scripts are standard library only and still run on Python 3.9. Setup links uv's CPython 3.14 as `~/.local/share/claude-acc/python` when [uv](https://docs.astral.sh/uv/) is installed (it starts in 26 ms where the command line tools' 3.9 takes 37), the system `/usr/bin/python3` otherwise. Everything starts its script through `acc.py`, which runs it from cached bytecode: Python compiles the file it is given on every start, which was a third of a short run. Kernel numbers come through `ctypes`: `proc_pid_rusage` for memory, `sysctl` for swap and pressure, `KERN_PROCARGS2` for exact command lines.
- The app is Swift 6 with main-actor default isolation (SE-0466) and `@concurrent` for process work, Swift Charts for the charts, and SF Rounded throughout. It stays near idle with the panel open (1-2% CPU, from 40%): a state file that didn't change costs one `stat` and wakes no view, live readings (load, heat, fans, memory, build progress) change in place, since while any animation runs SwiftUI updates the whole panel on every frame, and the spinning fan is a Core Animation layer the render server turns.
- `fanctl` talks to the SMC through IOKit's `AppleSMC` user client. Reading needs no root; the daemon that writes runs as root, reads only a mode from your folder, and writes its readings back atomically.

### Accounts under the hood

A Claude Code account is the `claudeAiOauth` object inside a Keychain entry. Claude Code reads `Claude Code-credentials`, plus `Claude Code-credentials-<first 8 hex chars of sha256(config dir)>` when `CLAUDE_CONFIG_DIR` is set. [Orca](https://github.com/stablyai/orca) keeps a copy of every account you add to it under `Orca Claude Code Managed Credentials`. Switching accounts means copying one account's `claudeAiOauth` into the entries the live sessions read.

- `accswitch.py` holds all the logic. launchd runs `accswitch.py tick` every 2 minutes, with or without the app.
- Usage comes from `GET https://api.anthropic.com/api/oauth/usage`, and the account behind a token from `/api/oauth/profile`.
- It asks for usage only when the answer could have changed. Usage in a window only grows until the window resets, so an account with a full window isn't read again before that reset, apart from a check every 3 to 4 hours in case Anthropic resets limits early. An account nobody works on keeps its numbers for 30 minutes and is read once more right before the watcher switches to it. The active account is read as often as the fastest burn seen so far (5% of the 5-hour window a minute) could bring it to the switch threshold: every 2 minutes close to it, every 15 minutes far from it.

## Numbers

Measured on a MacBook Pro 16" M4 Max with 48 GB, on 2026-10-04, with several agents working in Orca.

<img src="docs/turbopack-ab.svg" width="760" alt="Line chart: memory of a Turbopack next dev server over 30 cycles of an agent editing a CSS file and a component and requesting two pages, then a minute idle. Both arms start at 1.2 GB, reach about 6 GB by the 30th edit and settle at about 4.6 GB idle; the auto and full memory eviction settings give the same curve">

A Turbopack dev server grows fivefold under an agent's edits, and `turbopackMemoryEviction: "full"` changes nothing: 6.01 GB against 5.97 GB after 30 edits. Next.js restarts a dev server only when its V8 heap fills, and Turbopack's memory lives outside it. What does work is a restart: the server comes back from its disk cache in seconds at a third of the size, which is what the guard does once a server passes 5 GB and goes quiet.

<img src="docs/guard-timeline.svg" width="760" alt="Area chart of the memory of all dev servers over about an hour, peaking near 15 GB below a 16.8 GB budget line, with dots where the guard restarted or stopped a server and the total dropped right after">

With agents compiling, macOS on Auto kept the fans at about 2,000 rpm and let the hottest CPU sensor reach **115&nbsp;°C**. The fixed settings, as the SMC reports them on the same Mac:

| Setting | Fan speed |
| --- | --- |
| macOS Auto, idle | ~1,400 rpm |
| 50% | ~3,560 rpm |
| 75% | ~4,670 rpm |
| Max | 5,777 rpm |

On any fixed setting the daemon goes to full speed when a sensor passes 95 °C and comes back below 85 °C.

Ultra, measured on the same Mac with Orca and nine Claude Code sessions running:

| Tweak | Before | After |
| --- | --- | --- |
| A memory helper that scanned its 1.9 GB database all day, moved to the efficiency cores | 32% of a P-core, ~1 W | 0.1%, ~0.1 W |
| Node compile cache for what sessions spawn: `require('typescript')` | 96 ms | 49 ms |
| `git status` in an agent worktree repo, with `untrackedCache` and `fsmonitor` (opt-in) | 71 ms | 26 ms |
| Hook wait on every agent tool call, with a synchronous memory hook made async | 53 ms p50, 73 ms p90 | 24 ms p50, 30 ms p90 |
| Orca's status hook, which every tool call waited for, made async | 36-39 ms p50 per call | 0 |
| The guard's hook before every Bash command, native instead of Python | 25-57 ms | 5-8 ms |
| rtk's hook before every Bash command, `rtk hook claude` instead of its shell script | 57-80 ms | 12-14 ms |

## How it avoids logging you out

Each of these rules comes from an account that actually lost its login while the tool was being built:

- Refreshing a token rotates it, and presenting a refresh token that was already used got the whole account logged out. Claude Code sessions refresh the active account on their own, about 5 minutes before the token expires, so the watcher never refreshes the active account while a session could. It copies the pair the session wrote instead, from whichever Keychain entry the sessions use (`Claude Code-credentials` without `CLAUDE_CONFIG_DIR`, the hashed one with it). It refreshes the active account itself only once the token has been expired for 15 minutes, when no session is running.
- An inactive account whose token no session holds is refreshed by whichever process reads it first (the watcher, a click, or the panel), one process at a time behind a file lock. The panel never refreshes a token that a session in any config directory may hold.
- It never writes a token it hasn't checked against the API first. A future expiry date doesn't prove the token still works.
- It replaces only `claudeAiOauth`. The same Keychain entry holds `mcpOAuth`, the tokens of your MCP servers, which belong to the config directory and survive every switch.
- A 429 from the usage endpoint means "unknown", never "dead". It backs off that one account for 2 minutes, then 4, 8 and 15 while the endpoint keeps refusing it, starts over after that account's first good answer, and doesn't refresh or flag anything in the meantime. The endpoint throttles one account at a time: other accounts kept answering while the active one got 429 for an hour and a half. Claude Code and Orca poll the same endpoint too, so it can throttle even while this tool is quiet. Whether a token works is checked against the profile endpoint, so switching still works while the usage endpoint is throttled. Accounts with a canceled subscription get a 403 there, so they aren't asked at all.
- Before overwriting the live entry it copies the token there back to its Orca copy, because the running session may have rotated it since the last switch.

## Using up every account

An account normally leaves the rotation at the switch threshold (`hard_session_left` / `hard_weekly_left`) and comes back once it has `min_session_left` / `min_weekly_left` again, so a few percent of each account go unused. With **Use up every account** in the panel's Claude Code card, or `claude-acc drain on`, the watcher spends them once no account has headroom:

- The active account keeps working to its last percent instead of being given up at the switch threshold.
- Once it is empty, the watcher switches to the account with the most left in its weaker window (at least 1% of both the 5-hour window and the week), then to the next one, and so on. Accounts in `last_resort` come last, `never` accounts are never used. Sessions that hit the wall in between wake on the switch through the `StopFailure` alarm.
- An account with real headroom (after a reset) takes over from the scraps as soon as it shows up, like any switch.
- One notification when this starts; the switches go to the switch log. When nothing is left anywhere, the usual warning or the limit pause follows. A pause running when the mode starts is lifted, because there is still something to work with.

Depot sandboxes only ever get accounts with real headroom, so they never land on a scrap. The mode is off by default.

## How the pause works

The pause is optional and off by default. **Pause at the limit** in the panel's Claude Code card switches it, and so do `claude-acc pause on` and `claude-acc pause off`: both write `limit_pause` to `config.json` and update the hooks in `settings.json` right away, and turning it off during a pause wakes the paused sessions. Open sessions keep the hooks they started with; new and resumed ones pick up the change. Without the pause, sessions keep working until the limit, Claude Code resumes them after the reset, the `StopFailure` alarm below wakes them earlier when the watcher switches to an account with headroom, and the watcher sends one notification per episode when no account has headroom.

Claude Code already waits at a usage limit and continues on its own after the reset (`Continue automatically at usage limit` in `/config`, on by default). What it lacks is a warning early enough for subagents to stop cleanly, any warning before the weekly limit, and a way to know that capacity came back on another account. Agents that hit the wall stop halfway through an edit and have to be started over. The pause fills those gaps with Claude Code hooks, which setup adds to `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) next to your own hooks.

- The watcher writes `~/.local/share/claude-acc/pause.json` when the active account is down to its switch threshold and no other account qualifies. It removes the file once the active account or another one is back at `min_session_left` and `min_weekly_left` (15% and 8%), not just above the switch threshold, so sessions don't wake up for one minute of work. A switch to an account with headroom ends it too. A throttled usage read leaves the pause as it is. With an account selected in Orca, or Claude Code signed in to an account outside Orca, there is no pause, because the watcher isn't in charge then.
- `hook.py` only reads that file. Outside a pause each hook costs two file checks and never starts Python. Setup writes them in exec form (`args`), which Claude Code starts without a shell, as `claude-acc-pause <mode>`: a few lines of C that answer in 3.1 ms, where `claude-acc-hook pause` (Swift, 5.0 ms, used when the C program is missing) loads its runtime first and the shell guard takes 7.6 ms. Without either program, the hooks are a short shell command with the same checks.
- `PostToolUse` delivers the checkpoint once per session and once per subagent: finish the current small step, write the state to `TASKS.md`, start nothing new, end the turn. Subagents return with a report of where they stopped. `PreToolUse` on `Agent` refuses new subagents. A message you type in a paused session lifts the pause for that session; subagent reports and Claude Code's own resume messages don't count.
- `Stop` starts a background alarm (`asyncRewake`) in sessions that were told to stop. It checks the file every 20 seconds, exits quietly if the session got going some other way, and wakes the session with an instruction to resume from `TASKS.md` and continue its subagents through `SendMessage` instead of starting them over. `StopFailure` with `rate_limit` starts the same alarm for a session that hit the wall: Claude Code resumes it by itself after that account's reset, and the alarm wakes it earlier if the watcher switches to an account with headroom. Both alarms carry a `timeout` of eight days and a minute: Claude Code cancels an `asyncRewake` hook at its timeout, 600 seconds when unset, so without it no session woke from a pause longer than ten minutes.
- `claude-acc resume`, or **Resume Now** in the panel, lifts the pause by hand. It stays lifted until limits recover and run out again.
- Ultra's `claude-hooks-async` never makes these hooks async: in the background their instructions would never reach the session.
- Setup keeps a copy of `settings.json` from before its first change in `settings.json.bak-claude-acc`. `CLAUDE_ACC_NO_HOOKS=1` leaves the hooks out (and removes the ones an earlier setup added), and `claude-acc uninstall` removes them.

To try the pause in one real session without pausing the others, start that session with its own pause file, `CLAUDE_ACC_PAUSE_FILE=/tmp/pause.json claude`, then create the file with `echo '{"episode": "1"}' > /tmp/pause.json` and delete it to end the pause. `CLAUDE_ACC_HOOK_LOG=/tmp/hook.log` logs every hook call.

## Requirements

- macOS 26 or newer on Apple silicon, with Swift 6.2 or newer (Xcode or the command line tools) to build the app. Homebrew builds it for you.
- `/usr/bin/python3` (ships with the command line tools). With [uv](https://docs.astral.sh/uv/) installed, setup uses its CPython 3.14 instead.
- Claude Code. Tested with 2.1.284.
- Orca with your Claude accounts added as managed accounts, and **System default** selected as the active Claude account in Orca. With a managed account selected, Orca puts its own account back whenever a terminal starts and every 15 minutes, undoing every switch, and it refreshes that account's token itself. claude-acc reads Orca's settings, and while an account is selected there the watcher stands down, switching is blocked and the panel tells you to pick System default.

## Command line

| Command | What it does |
| --- | --- |
| `claude-acc status` | Usage of every account and the switching order |
| `claude-acc who` | The active account, its burn rate and when it will hit the wall |
| `claude-acc who --json` | Which account's token is in the Claude Code Keychain entries right now, as `{"email", "real_email", "id", "source"}` (or `"email": null` with a `reason`): no network, no lock, about 50 ms, for tools that check the account before every model call |
| `claude-acc switch <email>` | Switch to that account |
| `claude-acc switch --auto` | Switch to the next account in line |
| `claude-acc login <email>` | Log the account in again in the browser |
| `claude-acc heal [--deep]` | Recover accounts whose Orca copy died after a rotation elsewhere |
| `claude-acc tick` | One watcher pass (what launchd runs) |
| `claude-acc resume` | Lift the limit pause until limits recover (the panel's Resume Now) |
| `claude-acc pause [on\|off]` | Show, turn on or turn off the optional limit pause |
| `claude-acc drain [on\|off]` | Show, turn on or turn off using up every account |
| `claude-acc depot [--force]` | Which account the Depot sandboxes run on; `--force` sends its token again |
| `claude-acc depot --fallback` | Store a long-lived `claude setup-token` token for when no account has headroom |
| `claude-acc clean [--dry-run]` | Clean up now: every janitor task, whatever its schedule |
| `claude-acc mac status` | Free space, the last cleanup and warnings |
| `claude-acc update` | Update everything now: Homebrew, npm, Go, Python, Claude Code (the panel's Update) |
| `claude-acc updates [status\|run --dry-run]` | The last update run per package manager, or what a run would update |
| `claude-acc mac report` | What slows the Mac down: top processes, Spotlight, orphaned dev servers, data of uninstalled apps, broken launchd entries |
| `claude-acc mac spotlight` | Projects whose `node_modules` Spotlight indexes, and the settings pane to exclude them |
| `claude-acc mac optimize [--dry-run\|--undo]` | Faster Dock, Mission Control, window, Quick Look and Finder animations, faster key repeat (15 ms, from the next login), and disabling launch agents whose app is gone. Reversible |
| `claude-acc mac compress-apps [--dry-run] [--apps A,B]` | Transparent APFS compression of root-owned apps (Office, Adobe, `.pkg` installs) through sudo, signature checked before and after, rolled back if it breaks |
| `claude-acc guard status [--json]` | Dev servers, their memory, who watches them and what the guard is about to do |
| `claude-acc guard once [--dry-run]` | One guard pass, at most one action |
| `claude-acc guard stop <pid\|:port>` | Stop a dev server the way the guard does |
| `claude-acc guard recycle <pid\|:port>` | Restart a dev server in its own Orca terminal |
| `claude-acc guard pin <:port\|dir> [--for 12h \| --forever] [--reason TEXT] [--no-restart]` | An exception for a while: the guard never stops that server |
| `claude-acc guard unpin <:port\|dir\|all>` / `claude-acc guard pins` | Drop a pin / list pins with their reasons and expiry |
| `claude-acc guard room [dir]` | Exit 0 when memory admits a new dev server (of the app in `dir`), 1 with the reason: what a refused agent waits on |
| `claude-acc guard brake [--stage N] [--within PID]` | The memory brake's stage now and whom it would stop, without a signal |
| `claude-acc run -- <command>` | Run a heavy command through the memory scheduler from a terminal, a script or an Orca automation |
| `claude-acc sched codex install` / `uninstall` / `status` | The scheduler's hook for Codex in `~/.codex/hooks.json` (trust it once in Codex's `/hooks`) |
| `claude-acc fans [read\|keys]` | Fan speeds, CPU and GPU temperature, or every SMC key |
| `claude-acc fans set auto\|<30-100>` | Set the fans by hand (root) |
| `claude-acc fans install\|uninstall` | Install the fan daemon, or remove it and give the fans back to macOS |
| `claude-acc hotspot on\|off` | Hotspot turbo on or off (the first `on` installs the root helper) |
| `claude-acc hotspot status [--json]` | Whether it shapes now, the upload cap, and the queue delay it sees |
| `claude-acc hotspot install\|uninstall` | Install the hotspot daemon, or remove it and lift the cap |
| `claude-acc perf ultra on\|off\|status` | Turn Ultra on or off, or show what it changed and the numbers |
| `claude-acc perf bench network\|cpu\|gpu\|fs` | Measure: queueing in the network vs inside the connection, P-core share and wake-up latency, GPU time per app, the file cache |
| `claude-acc perf list` / `apply` / `undo <name>` | Every tweak on its own, with what it changes and how it was measured |
| `claude-acc perf-root vnodes trial [--keep]` | Root: a bigger vnode cache, measured before and after; `vnodes apply --persist` keeps it across reboots |
| `claude-acc perf-root spotlight apps-only\|undo` | Root: Spotlight indexes apps only; undo restores the previous privacy list |
| `claude-acc perf-root iogpu set [MB]\|undo\|status` | Root: more memory for the GPU (`iogpu.wired_limit_mb`), so local models stay on Metal; kept across reboots, `status` without sudo |
| `claude-acc perf-root devtools add\|undo\|status` | Opens Developer Tools in System Settings and waits until Orca is on the list, so fresh Go test binaries skip Gatekeeper |
| `claude-acc perf bench agents [--hours N]` | Where agents spend their time, from Claude Code transcripts (last day by default): model latency by context size, the floor of a Bash call, cache re-writes after idle gaps, tool turnaround and the hooks that cost the most, among those that print something ([why](#silent-hooks)) |
| `claude-acc perf bench gatekeeper` | How long the first run of a freshly built binary waits for Gatekeeper from this terminal |
| `claude-acc mail add <address> gmail\|imap [read\|modify\|draft] [--send] [...]` | Add a mailbox; for IMAP the password goes into the Keychain, `--token-command` gives a Gmail mailbox without delegation |
| `claude-acc mail google --service-account SA [--aws-audience A --aws-profile P]` | The Google service account and the identity that signs for it |
| `claude-acc mail doctor` | Check the identity and every mailbox |
| `claude-acc mail install` / `uninstall` | Register the `mail` MCP server, the `mail` skill and the prompt hint for every Claude Code session |
| `claude-acc mail wait <mailbox> '<query>' [--timeout 6h]` | Wait for a new matching message; agents run it in the background and get woken when it arrives |
| `claude-acc mail search\|read\|thread\|attachment\|modify\|draft\|send ...` | The same tools from the shell |
| `claude-acc browser install` / `uninstall` | Register the `browser` MCP server, the `browser` skill and the prompt hint for every Claude Code session |
| `claude-acc browser doctor` / `status [--json]` | What to click in Chrome and Brave, which one is connected, agent tabs |
| `claude-acc browser setup chrome\|brave` | Open the browser's remote debugging page in a new tab of that browser, where you tick the checkbox once |
| `claude-acc browser use chrome\|brave` / `tabs-mode hidden\|background` / `site <domain> act\|read\|deny\|-` / `mode guarded\|full` | The default browser, where agent tabs live, per-site levels, and whether agents ask before risky calls |
| `claude-acc browser disconnect` | Close the agents' tabs and the connection (the panel's Disconnect) |
| `claude-acc browser <member> ['<json input>']` | Any toolset member from the shell, such as `navigate '{"url": "example.com"}'` or `read_page '{"filter": "interactive"}'`; calls from one agent session share its tabs |
| `claude-acc browser run "<task>" [--model M] [--browser chrome\|brave]` | One task in the SDK tool runner on the native toolset, in your browser (default model `claude-opus-5-5`) |
| `claude-acc browser api-key` | Store the Anthropic API key for `run` in the Keychain |
| `claude-acc credits status [--json]` | API credits left per Console organization and in total, with the next cycle reset |
| `claude-acc credits add <email> --scope own\|<client> [--org ID] [--from-keychain SERVICE/ACCOUNT]` | Register the organization linked to that plan; the API key goes from a hidden dialog (or another Keychain item) straight to the Keychain |
| `claude-acc credits pending <email>` / `remove <email>` | Mark a plan whose API credits button hasn't appeared yet / forget an account and its key |
| `claude-acc credits balance <email> --remaining-usd N [--expires-at DATE] [--plan-ends-at DATE\|none]` | Record what Console shows under Promotional credits, and the date a cancelled plan ends |
| `claude-acc credits exec --purpose NAME [--scope] [--org ID] [--no-env] -- <cmd>` | Run a command on the credit that expires first; 75 when none has headroom, 76 when it ran out mid-run |
| `claude-acc credits helper --purpose NAME` | `apiKeyHelper` for Claude Code and the Agent SDK under `exec --no-env` |
| `claude-acc credits run --purpose NAME --budget-usd N [--mode auto\|credits\|subscription] [--summary FILE] -- <cmd>` | Run unattended `claude -p` work on credit or a spare subscription account, metered and isolated; 75 when nothing can pay |
| `claude-acc credits canary [--version X.Y.Z]` | Check a Claude Code version with one metered Haiku call and pin it for `run` |
| `claude-acc credits record --org ID --usd N --purpose NAME` | Report what a call cost, so the balance stays current between Console readings |
| `claude-acc credits key --purpose NAME --json` | Which Keychain entry to use, without the key |
| `claude-acc jobs run NAME [--slot S] [--json]` | One unattended attempt of a blog job and its record ([Jobs](#jobs)); `jobs list`, `jobs log NAME`, `jobs add/set/remove`, `jobs hold/release` |
| `claude-acc desktop install` / `uninstall` | Register the `desktop` MCP server, the `desktop` skill and the prompt hint for every Claude Code session |
| `claude-acc desktop doctor [--open]` / `status [--json]` | Whether the helper has Accessibility and Screen Recording (and which binary to tick), the displays, the mode |
| `claude-acc desktop mode full\|guarded` | Whether the agent acts freely (full) or is asked before `type`, `key` and `hold_key` (guarded) |
| `claude-acc desktop <member> ['<json input>']` | Any toolset member from the shell, such as `screenshot --out shot.png` or `left_click '{"coordinate": [640, 300]}'` |
| `claude-acc mcp share <name> [--force] [--port N]` | Run that user-scope stdio MCP server once for every Claude Code session instead of once per session |
| `claude-acc mcp unshare <name>\|--all` / `status [--json]` | Put it back to one copy per session / the shared servers, their sessions, restarts and memory |
| `claude-acc uninstall` | Remove the launchd jobs, the app, this command and the limit pause hooks; settings stay |

## Configuration

`~/.local/share/claude-acc/config.json`. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `hard_session_left` | `5` | Switch when the active account has this % of the 5-hour window left |
| `hard_weekly_left` | `3` | Switch when it has this % of the week left |
| `min_session_left` / `min_weekly_left` | `15` / `8` | An account needs at least this much to be switched to, and the limit pause ends once an account has it again |
| `limit_pause` | `false` | [Pause sessions at a checkpoint](#how-the-pause-works) when no account has headroom; **Pause at the limit** in the panel and `claude-acc pause on\|off` set it and update the hooks |
| `drain` | `false` | [Use up the last few percent of every account](#using-up-every-account) once none has headroom; **Use up every account** in the panel and `claude-acc drain on\|off` set it |
| `last_resort` | `[]` | Emails used only when nothing else has headroom |
| `never` | `[]` | Emails never switched to |
| `config_dir` | `~/.claude` | The config directory whose sessions get switched |
| `other_config_dirs` | `[]` | Other config directories whose entries get the new token when an account refreshes, but are never switched |
| `depot_sync` | `true` | Keep the `CLAUDE_CODE_OAUTH_TOKEN` secret of `depot claude` sandboxes on an account with headroom |
| `depot_min_valid_hours` | `4` | A token sent to Depot must stay valid at least this long; a shorter one is refreshed first |
| `depot_bin` | `""` | Path of the Depot CLI; empty means `PATH`, then Homebrew |

## Depot sandboxes

[`depot claude`](https://depot.dev/docs/agents/claude-code/quickstart) starts Claude Code in a remote sandbox with the token stored in the organization secret `CLAUDE_CODE_OAUTH_TOKEN`. Every tick keeps that secret on the account first in the switching order **other than the local one**, so the laptop and the sandboxes never burn the same account. The token is sent again only when that account runs out of headroom, becomes the local account, or has less than `depot_min_valid_hours` of validity left; an idle account's token is refreshed before it is sent, the active account's never (its sessions own that refresh). With no other account left, the long-lived token from `claude-acc depot --fallback` goes out instead. A Depot failure is logged and never stops the switching. Requires the Depot CLI logged in to the organization (`depot login`).

## Mac janitor

Agents build, install and test all day, and every run leaves something on disk: `.next` caches of a few GB per worktree, `node_modules` of projects nobody touched in a month, `go-build` temp dirs from interrupted test runs, npx and uv caches. `janitor.py` removes what a build or an install brings back, and only what nobody is using.

launchd runs `janitor.py sweep` at login and every 3 hours as a background process: macOS keeps it on the efficiency cores and throttles its disk access, so a cleanup never competes with your work. Right after boot it waits until the system has been up for 5 minutes, and on battery below 30% it waits for the charger.

Before removing anything it checks, with one `lsof` over your processes, that no process has a file open in it and that no program has its working directory there. A terminal sitting at a prompt in the project doesn't count, a dev server does. The directory is renamed first and deleted after, so a dev server starting at that moment builds from scratch instead of reading half-deleted chunks. An interrupted delete is finished on the next run.

| Task | What goes | When |
| --- | --- | --- |
| `next` | `.next` and `.next-*` next to a `package.json`, unchanged for 24 hours; Turbopack's persistent cache in `.next/cache` and `.next/dev/cache` (what new worktrees seed from) stays until it is unchanged for 7 days | every run |
| `caches` | `.turbo`, `node_modules/.cache`, `node_modules/.vite`, unchanged for 7 days | daily |
| `node_modules` | every `node_modules` of a project where no file changed and git didn't move for 30 days | daily |
| `tmp` | `go-build*` in `$TMPDIR` older than 6 hours | every run |
| `caps` | the oldest entries of folders listed in `caps` once a folder is over its limit (the guard also runs it every 10 minutes) | every run |
| `go` | the least recently used entries of the Go build cache once it's over 20 GB, down to 12 GB, so agents keep their warm builds (Go trims entries unused for 5 days on its own) | daily |
| `npm` | `npm cache verify`, npx packages unused for 30 days (not the ones a running process uses, like MCP servers), npm logs older than a week | daily |
| `pnpm` | `pnpm store prune`, and after every run that removed a `node_modules` | weekly |
| `docker` | dangling images and build cache older than a week, only when the engine is already running | daily |
| `xcode` | DerivedData unchanged for 14 days except the shared `ModuleCache.noindex` and `CompilationCache.noindex`, unavailable simulators | daily |
| `brew` | `brew cleanup --prune=14` | weekly |
| `uv` | `uv cache prune` | weekly |
| `logs` | files in `~/Library/Logs` older than 30 days | daily |
| `compress` | transparent APFS compression (`afsctool`, LZFSE) of files under `compress.paths` that haven't changed for an hour; off until you list paths | every run |

`compress` keeps files on disk compressed the way macOS stores its own system files: reads are transparent, the kernel decompresses on the fly. Measured on 2026-10-08: Claude Code transcripts in `~/.claude/projects` shrink by 75%, Microsoft Word.app by 39%. Appending to a compressed file makes APFS write it back uncompressed, so each run picks up the files changed since the last one once they have been quiet for `min_age_minutes`. It skips files a process has open, the whole bundle of an app that is running (compressing in place swaps out a mapped executable), files you can't write that you don't own (the janitor has no root), and protected paths. Nothing it skips is lost: `~/.local/share/claude-acc/janitor-compress.json` remembers an app that was running and goes through the whole bundle once it has quit, retries files that were open, and keeps a list of files afsctool could not shrink (media, binaries that are compressed already) so they are not read again until they change. Install the tool with `brew install afsctool`; without it the task logs a warning and does nothing. APFS clones share blocks and each compressed copy gets its own, so leave clone-heavy folders such as a pnpm store on APFS out of `paths`.

Apps installed by a `.pkg` (Microsoft Office, Adobe, App Store apps, most installers) belong to root, so the `compress` task can't write them. `claude-acc mac compress-apps` does them through sudo (Touch ID), one app at a time:

```bash
claude-acc mac compress-apps --dry-run          # root-owned apps in /Applications (and one folder down), size, current savings
claude-acc mac compress-apps                    # compress every root-owned app that isn't running or already done
claude-acc mac compress-apps --apps Word,Premiere --threads 8
```

For each app it checks the signature with `codesign --verify --deep --strict`, and an app whose signature is already broken is left alone. Then it compresses with `afsctool -c -T LZFSE` and checks the signature again. If the check passed before and fails after, it decompresses the app (`afsctool -d`) and says so in the result table. It skips running apps and apps where nearly every file is compressed already. Measured on 2026-10-09, every signature stayed valid: Word 12.6% → 38.8% savings, Excel 45.6%, Outlook 42.6%, PowerPoint 43.6%, Premiere Pro 61.1%, Photoshop 48.7%, Lightroom 26.6%. Office (Microsoft AutoUpdate) and Creative Cloud updates install fresh uncompressed bundles, so run it again after big updates; a second run only does what changed. The code is in `compressapps.py`. It imports nothing from the repo and runs as `sudo /usr/bin/python3 -I`, with the full path to afsctool passed in because sudo's `secure_path` doesn't include `/opt/homebrew/bin`. A weekly root job for this isn't part of claude-acc.

Each run also checks which projects' `node_modules` end up in the Spotlight index. Spotlight skips directories whose name starts with a dot or ends with `.noindex`, so pnpm's `.pnpm` store is never indexed, but hoisted `node_modules` (Expo, npm, yarn) are, and every install makes Spotlight chew through tens of thousands of files. Neither a `.metadata_never_index` file nor `chflags hidden` stops it on current macOS. The fix is System Settings > Spotlight > Search Privacy; `claude-acc mac spotlight` lists the projects and opens that pane.

`janitor-root.sh` does what needs root, run it by hand with `sudo`: it parks system launchd entries whose app is gone in `/Library/launchd-disabled-<date>` and removes crash reports older than a month. With `--high-power` it also switches a Mac that supports it to High Power mode on the charger (battery stays on automatic). `--dry-run` shows what it would do. The iOS simulator dyld cache in `/Library/Developer/CoreSimulator/Caches` is protected by SIP even from root, so it stays.

Configuration lives in `~/.local/share/claude-acc/janitor.json`. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `roots` | `~/Documents`, `~/Developer`, `~/Projects`, `~/code`, `~/src` | Where projects live |
| `protect` | `[]` | Paths the janitor never touches (footage, experiment results) |
| `next_idle_hours` | `24` | Age of a `.next` build before it goes |
| `cache_idle_days` | `7` | Age of `.turbo`, `node_modules/.cache` and Turbopack's cache in `.next` |
| `node_modules_idle_days` | `30` | Project inactivity before its `node_modules` goes, `0` turns it off |
| `npx_idle_days` | `30` | Age of an npx package |
| `go_cache_max_gb` / `go_cache_keep_percent` | `20` / `60` | Go build cache size that triggers a trim, and how much of it the trim keeps (the most recently used entries) |
| `derived_data_idle_days` | `14` | Age of Xcode DerivedData |
| `simulator_pool_prefix` / `simulator_leases` | `Portivo-` / `~/.cache/portivo-mobile/leases` | Xcode task: an unavailable simulator is deleted one by one, only if it was already unavailable on the previous run, its name does not start with the prefix and no `portivo-mobile` lease file exists for it (a runtime that vanishes for a moment during an Xcode switch must not take the agents' simulators along) |
| `log_days` | `30` | Age of logs in `~/Library/Logs` |
| `min_battery_percent` | `30` | On battery below this, the run waits for the charger |
| `notify_min_gb` | `2` | A run that frees at least this much sends a notification |
| `low_disk_gb` | `40` | Below this much free space, a warning (at most every 12 hours) |
| `skip` | `[]` | Task names to leave out, like `["docker", "brew"]` |
| `caps` | `[]` | Folders agents fill without end, like `[{"path": "~/.cache/portivo-perf/*/builds", "max_gb": 10, "keep": 1}]`. Entries over `max_gb` go oldest first (by creation date, since `rsync -a` copies mtimes); the `keep` newest always stay, and so does anything changed in the last `fresh_minutes` (10) |
| `compress` | `{}` | Transparent APFS compression, like `{"paths": ["~/.claude/projects"]}`. Other keys: `min_age_minutes` (60), `compressor` (`LZFSE`), `threads` (4), `max_file_mb` (1024), `exclude_running_apps` (`true`) |

## Dev server guard

With a few worktrees open in Orca, every agent starts its own `next dev` and opens a preview tab. Measured on a 48 GB Mac on 2026-10-04:

- a Turbopack dev server holds its module graph in native Rust memory and reaches 7-8 GB of footprint after an hour of agent edits. A restarted one was back from 2.3 to 7.8 GB within 10 minutes. Next.js has its own restart watchdog, but it only watches the V8 heap, and Turbopack's `turbopackMemoryEviction: 'auto'` waits for memory pressure feedback from the OS;
- the OS never gives it. With 12.7 of 13.3 GB of swap used, `kern.memorystatus_vm_pressure_level` still said normal, right until jetsam started killing processes with reason `low-swap`;
- an open preview keeps an HMR websocket, so every file an agent saves is a recompile and a page reload: one `tokens.css` edit cost the server with a preview 10 s of CPU, the six servers without one 0.1 s.

`devguard.py` runs from launchd all the time (`KeepAlive`, standard priority, so it gets the CPU exactly when the Mac is choking) and looks every 5 seconds without starting a single process: the process table from `sysctl` (`KERN_PROC_ALL`, `KERN_PROCARGS2`), sockets from `proc_pidfdinfo`, and Orca over its own unix socket. A pass used to spawn `ps`, `lsof` and four `orca` CLI calls, 3.9 s of CPU a minute in child processes; now it takes about 0.2 s. It reads:

- memory the way jetsam counts it: `phys_footprint` from `proc_pid_rusage`, plus CPU time and disk writes, read through `ctypes` without forking anything per process;
- who watches each server: TCP clients of its ports (an Orca tab, a browser, a headless Chrome), Orca's preview tabs, and whether the tab is the one you are looking at (active tab of the worktree selected in Orca);
- memory pressure from swap growth and the compressor's `vm.compressor.compactor.swapouts_queued_pressure` counter. Swap that stays full after memory was freed is only a warning; full and still growing is critical;
- Orca's worktrees, agents and terminals, so it knows which terminal a server runs in and whether an agent is working there. Reads go over Orca's runtime socket with the request its CLI sends, and fall back to the `orca` CLI when that fails; actions (restarting a server in its terminal) always use the CLI. It never starts Orca.

What it does, gentlest first:

| Action | When |
| --- | --- |
| background QoS (`PRIO_DARWIN_BG`, efficiency cores and throttled disk) | a server you are not looking at; back to normal priority once you switch to its preview |
| restart in its own Orca terminal | the server is over `max_server_gb` and has been quiet for `quiet_seconds`. Turbopack comes back from its disk cache in seconds and the preview reconnects by itself. The exact argv comes from `KERN_PROCARGS2` and goes back in with `orca terminal send`, only into a plain shell that is back at its prompt |
| stop | a second server of the same app nobody watches, a server whose agent or terminal is gone, a server nobody watched or used for `idle_minutes` (twice that while an agent works in its worktree), and a server restarted `max_recycles_per_hour` times within an hour, which is the HMR loop again |
| free the biggest | dev servers over `budget_percent` of RAM, or memory pressure. With a warning only servers without a preview or bloated ones; when critical, also ones agents watch. The server you watch is never stopped, at most restarted |

**Memory brake.** On 2026-10-08 a 48 GB Mac froze at 18:56 with 18 GB of swap and the compressor at 34 GB; the kernel's last lines were `memorystatus: killing due to "vm-compressor-space-shortage"`. No dev server was left to stop: the memory went to what the guard did not see, 55 node processes (MCP servers, vitest, tsc), four gopls, agents' headless Chromes and a `git grep` at 10 GB. So every pass also computes a brake stage from the signals that announced that freeze: the compressor's size against RAM (`vm.compressor_bytes_used`), its segments against the kernel's limit (`vm.compressor.segment.total` / `.limit`, the limit behind "compressor space shortage"), swap and its growth over 2 minutes, and the kernel's pressure level. Replayed on that day's readings, the stages fire at 17:26 (tight), 17:30 (brake) and 18:49 (emergency), seven minutes before the freeze; an hour of normal work before it stays calm, and swap that only stands still never counts.

| Stage | When (48 GB Mac) | What happens |
| --- | --- | --- |
| tight | compressor 40% of RAM, or swap over 12% of RAM growing 0.5 GB in 2 min | nobody is stopped; the build scheduler starts only what fits, without its "alone on the Mac" overcommit |
| brake | compressor 50%, segments 60% of the limit, swap over 20% of RAM growing 1 GB in 2 min, kernel critical, or the guard's own critical | one tree every 20 s, when the guard did nothing this pass; the scheduler admits only what fits |
| emergency | compressor 60%, segments 80%, swap over 30% of RAM growing 2 GB in 2 min | one tree every 5 s, whatever the guard did, 3 s between SIGTERM and SIGKILL, no minimum age; the scheduler admits nothing |

Victims, least painful first, biggest within a class: a single process over half the RAM (any stage; the rule a separate memory fuse used to cover), an orphan (a node, bun, deno or gopls process adopted by launchd whose owner is gone), a headless browser, a bloated `git` or language server (gopls, tsserver, rust-analyzer, sourcekit-lsp), a job of the build scheduler (the agent gets the exit code and the log says how to resume it), a test or compile run of an agent, and in an emergency any other tree an agent started with a Bash command over 1 GB. Parent 1 alone does not make an orphan: an agent that starts a job with `&` or `nohup` keeps waiting for it after its shell exits, so the owner comes from `CLAUDE_PID` in the process environment (Claude Code sets it for every command) and, without it, from the process group leader. Agents (Claude Code, Codex) and their MCP servers, shells, Orca, your browser, apps under `/Applications`, Docker Desktop, WindowServer, system processes and claude-acc itself are never touched; dev servers stay with the guard's own rules above. A stop sends SIGCONT first (a job the scheduler paused can't handle SIGTERM while stopped), then SIGTERM, then SIGKILL after the grace, and waits until the processes are really gone: the kernel frees a killed process's compressed and swapped pages in that process's own context, which takes seconds on a choking Mac, and checking right after SIGKILL is what used to log "nie chcą zginąć" for processes that were already dying. Every stop goes to the log as `ostatnia linia (stage): ... | wznowienie: <command>` and to a notification that never takes focus. While the stage is tight or worse the guard looks every 2 seconds, and in an emergency it skips Orca (its CLI is a node process that starts slowly on a choking Mac). `claude-acc guard brake [--stage N] [--within PID]` shows the stage and whom the brake would pick, without sending a signal; `"last_resort": false` turns it off and `"brake": {...}` overrides any threshold.

**Long-lived processes.** The guard also sums memory by family every 30 seconds (every pass when tight): dev servers, scheduler jobs, headless browsers, simulators, expo/metro, watchers, language servers, Docker, git, agents and browsers. `guard status` and `claude-acc sched status` show the long-lived ones (`Długo żyjące: ...`); the scheduler gets the numbers in `state.json` (`memory.long_lived_gb`, `memory.long_lived`, `memory.brake`). They already count in what the kernel reports as available, so admission sees them as less free memory; the line says what that memory is.

One action at a time, then `cooldown_seconds` to let memory settle, and the swap growth window starts over so an old trend can't trigger the next one. A server younger than `grace_minutes` is left alone. A server that ignores SIGTERM gets SIGKILL after 10 seconds and up to 10 more to exit: under swap the kernel takes seconds to free a killed process's pages, so only one still there after that is logged as refusing to die. After a stop the guard closes the server's background preview tabs in Orca (they would only keep reloading), writes a `devguard: ...` comment on the worktree card when the card has no comment of someone else's, sends a notification, and logs the command to bring the server back.

### Pins: exceptions for a while

Some servers have to stay up even when nobody watches them: a server a film pipeline captures from every few minutes, a demo you are about to show. `protect` in the config is permanent; a pin is the same promise for a while, set from the terminal (by you or by an agent) without editing JSON:

```sh
claude-acc guard pin :3747 --for 24h --reason "hero film captures"
claude-acc guard pin ~/code/site/apps/web          # a directory: every server in or below it
claude-acc guard pins
claude-acc guard unpin :3747
```

A pinned server is never stopped: not as idle, a duplicate, an orphan, over budget or under pressure. When it bloats over `max_server_gb` it is still restarted in its terminal (it is back in seconds), and a restart loop only warns. `--no-restart` holds even that, until memory is critical: then the biggest pinned server is restarted, never stopped, and only when nothing unpinned is left to free. Pins last 12 hours unless you say `--for 90m`, `--for 2d` or `--forever`, live in `~/.local/share/claude-acc/devguard-pins.json`, take effect on the next pass without restarting the guard, and show up in `guard status` with their reason and expiry. Expired ones are ignored.

### iOS simulators and Metro

A booted iOS simulator holds 3-4 GB (measured 2026-10-08: 3.3-4.2 GB and 150-180 processes for one with a React Native app), and agents leave them booted after their session ends. The guard sees each one by its `launchd_sim` and the UDID in its arguments, and counts it in use while a live session holds its `portivo-mobile` lease (`~/.cache/portivo-mobile/leases`, the session's pid and start time), while a process outside it names its UDID (maestro's driver, a build with `-destination id=UDID`, `serve-sim`), when it isn't from the agents' `Portivo-*` pool, which makes it yours, or when it matches `simulator_protect` (by default `Portivo-Perf-*`, performance sessions). A pool simulator nobody uses is shut down with `xcrun simctl shutdown` after `simulator_idle_minutes` of quiet (an idle simulator uses 0.01 of a core, one maestro drives 0.4-0.7, so quiet means under `simulator_busy_cores`), and after `simulator_quiet_minutes` when more than `max_booted_simulators` are booted or memory is tight. Never while Simulator is the frontmost app or the frontmost app can't be read, never while an app other than the leased one (and maestro's driver) runs in it, never within `grace_minutes` of boot, and the lease is read again under `portivo-mobile`'s own lock right before, so a session taking the device wins. A shutdown held back this way doesn't block the guard's other actions in the same tick. Over the cap with nothing safe to shut down, it warns once an hour. `portivo-mobile up` from a session without a simulator waits in the build scheduler while the agents' pool simulators in use are at the cap; yours count toward memory but not toward that cap.

An app in a simulator keeps a connection to its Metro, which made a Metro whose session died look watched forever. A client in a simulator nobody uses no longer counts as a viewer, so that Metro goes as an orphan or idle server like any other; it isn't stopped while Simulator is the frontmost app.

`claude-acc-hook` is a `PreToolUse` hook for Claude Code. When an agent is about to start a dev server (also through `orca terminal create --command`, `cd`, `pnpm -C`, `--filter`, a path to the CLI such as `./node_modules/.bin/expo start`, or `node …/expo/bin/cli start` as the guard's own restart line has it), it refuses a second server of an app that already runs and gives the agent its URL instead, and refuses a new one when memory is critical, even when no dev server is left, or the servers are over budget. Metro counts in every form: `expo start`, `react-native start` and `expo run:ios|android` without `--no-bundler` (which reuses a Metro already serving the app, so it is only checked for memory). A server the guard stopped for lack of memory stays stopped for `restart_hold_minutes` unless pressure is back to normal and swap is below the warning level: right after a stop the swap growth is measured from scratch, so pressure looks lower than it is, and on 2026-10-08 an agent restarted a Metro the guard had just stopped, and the Mac froze five minutes later. A memory refusal gives the agent the exact command to wait with, `claude-acc sched wait -- 'claude-acc guard room <app dir>'`. It denies even under `--dangerously-skip-permissions`. `DEVGUARD_ALLOW=1` in front of the command lets it through. Add it to `~/.claude/settings.json`:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
  { "type": "command", "command": "$HOME/.local/share/claude-acc/claude-acc-hook", "timeout": 10 }
] } ] } }
```

Native builds and simulators wait in the build scheduler's memory queue (`sched.py`, [`docs/sched.md`](docs/sched.md)), like heavy Go and JS work: `xcodebuild` builds, tests and archives, `expo run:ios|android`, `expo prebuild`, `pod install`, `eas build --local`, Gradle assemble, bundle, install and build tasks, `react-native run-*`, `xcrun simctl boot`, `open -a Simulator` and `portivo-mobile up`. An Expo app's `xcodebuild` takes 6-12 GB at full parallelism, and nothing gated it: on 2026-10-08 a Release build and then a dev client build started while swap was growing. A native job starts only in memory that is free after the reserves of running jobs (alone on the Mac: what is available minus the 4 GB headroom), never over it after a wait as a lone Go job may, and never while the guard sees critical pressure, since the kernel's own level can say normal with full swap. The agent's command waits and then runs by itself, with its output and exit code. Version, list, settings and clean commands are left alone. Only one iOS build runs at a time on the Mac (Android builds run beside it, still gated by memory), and only for its compile phase, so an `expo run:ios` that keeps Metro afterwards frees the slot: a build started outside the scheduler (portivo-mobile's detached builder, Xcode while a compiler runs under it) holds that slot too, and its growth to a build's peak is reserved. `xcrun simctl boot`, `open -a Simulator` and `portivo-mobile up` from a session without a simulator also wait while the agents' pool simulators in use are at `max_booted_simulators`.

The hook runs before every Bash command of every agent, and most commands neither start a dev server nor bring Go, JS or native work for the scheduler. `claude-acc-hook` is a small native binary that answers those in about 5 ms; a command where one of the words that matter stands as a whole word goes on to `devguard.py admit` with the same input, which decides everything. The words and the pattern come from `devguard.py words`, written to `hook-words.json` at setup. The scheduler knows a program by its whole token and a dev server start always follows whitespace, so `2>/dev/null`, `export`, `main_test.go` or `tsconfig.json` no longer start Python: on a day of real commands, 16% went to Python instead of 45%, and none of the commands Python acts on was lost. Before, Python started for every command: 44 ms each, and hundreds when the Mac is loaded. `python3 devguard.py admit` still works as the hook on its own.

**Heavy work goes through the build scheduler.** The same hook hands every heavy command of an agent to `sched.py` ([docs/sched.md](docs/sched.md)), which starts it when its predicted memory peak fits and makes it wait otherwise; it never refuses one. That covers Go and JS builds and tests and anything else with a heavy shape: `cargo`, `swift`, `xcodebuild`, `docker build`, `pytest` and `mypy`, Playwright, Cypress and Lighthouse, bundlers, `just`, `make` and `task` recipes, scripts run by path (`./scripts/e2e.sh`, `bin/verify`), through an interpreter (`python x.py`, `node x.js`, `tsx`, `bun`, `uv run`) or as a `package.json` script (`pnpm sm capture`). Servers, watchers, REPLs and quick one-liners (`python -c`, `node -e`, `git`, `ls`, `rg`) stay out. A command the scheduler has never seen gets a conservative estimate (4 GB for a script, 6 GB for a build); then it learns the p90 peak of that command's signature from its history, and a new script in a family it knows (the sixth Python script of a repo) starts from the family's numbers. A script that calls `claude-acc sched run` itself inside an admitted job runs at once (`CLAUDE_ACC_SCHED_JOB`), so it never waits for memory its own job already holds. The hook also adds `-I` to an agent's `git grep`: a regex over committed binaries once took git to 10 GB in two seconds. Setup keeps rtk's `exclude_commands` in step with what the scheduler wraps, since two hooks rewriting one command race.

Codex has the same `PreToolUse` hooks: `claude-acc sched codex install` adds this hook to `~/.codex/hooks.json` next to yours, and Codex runs it once you trust it in `/hooks`. Codex takes a rewritten command only together with `permissionDecision: "allow"`, so in a Codex that asks before commands, wrapped ones stop asking (Orca starts Codex with `--dangerously-bypass-approvals-and-sandbox`, where nothing changes). Terminal scripts and Orca automations that run commands themselves use `claude-acc run -- <command>`. Whatever cooperates with nothing is left to the memory brake.

Configuration lives in `~/.local/share/claude-acc/devguard.json`. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `enforce` | `observe` only reports and logs |
| `budget_percent` | `35` | All dev servers together, as % of RAM |
| `max_server_gb` | `5` | A single server above this is bloated |
| `swap_warn_percent` / `swap_critical_percent` | `12` / `20` | Swap as % of RAM for a warning and, while it grows, for critical |
| `available_critical_percent` | `10` | `kern.memorystatus_level` at or below this is critical |
| `kernel_pressure` | `true` | Take the kernel's pressure level (`kern.memorystatus_vm_pressure_level`) into account; `false` relies on swap and `memorystatus_level` alone |
| `quiet_seconds` | `30` | No CPU and no terminal output for this long before a watched server is restarted (10 times that for the one you watch) |
| `grace_minutes` | `3` | A new server is left alone this long |
| `duplicate_minutes` / `orphan_minutes` / `idle_minutes` | `5` / `10` / `45` | When a duplicate, an orphan and an idle server go |
| `cooldown_seconds` | `45` | Pause after every action |
| `max_recycles_per_hour` | `2` | More restarts of one app than this is a loop: stop instead |
| `background_unattended` | `true` | Background QoS for servers you don't watch |
| `close_tabs` / `orca_comment` / `notify` | `true` | What happens around a stop |
| `protect` | `[]` | Server paths (or anything above them) and ports like `":3000"` the guard never touches; for a while, use `guard pin` |
| `scope` | `[]` | When set, the guard only sees servers under these paths |
| `runtimes` | `node`, `bun`, `deno` | Interpreters dev servers run under |
| `caps_minutes` | `10` | How often the guard applies the janitor's `caps`, `0` turns it off |
| `restart_hold_minutes` | `10` | How long the hook keeps a server the guard stopped for lack of memory from starting again, unless pressure is back to normal and swap is below the warning level |
| `max_booted_simulators` | `2` | Booted iOS simulators at once; over it, unused pool ones go after `simulator_quiet_minutes` and `portivo-mobile up` waits; `0` turns the cap off |
| `simulator_idle_minutes` / `simulator_quiet_minutes` | `30` / `5` | When an unused pool simulator goes, under and over the cap |
| `simulator_busy_cores` | `0.15` | CPU of the whole simulator that counts as use |
| `simulator_pool_prefix` | `Portivo-` | Names of the agents' simulators; others are yours and never shut down |
| `simulator_leases` | `~/.cache/portivo-mobile/leases` | Where `portivo-mobile` keeps its leases |
| `simulator_protect` | `["Portivo-Perf-*"]` | Simulator names (with `*` and `?` patterns) or UDIDs the guard never shuts down. `Portivo-Perf-*` are the simulators of sessions measuring the iOS app's performance, which hold one for long without a lease. Your own list replaces this one, so repeat the pattern in it |
| `last_resort` | `true` | The memory brake (stages, victims and timing above); `false` turns it off |
| `brake` | `{}` | Brake thresholds: `compressor_tight_percent` / `_brake_` / `_emergency_` (40 / 50 / 60), `segments_brake_percent` / `_emergency_` (60 / 80), `swap_tight_percent` / `_brake_` / `_emergency_` (12 / 20 / 30, only while growing), `swap_growth_tight_gb` / `_brake_` / `_emergency_` (0.5 / 1 / 2 in 2 min), `runaway_percent` (50), `brake_cooldown_seconds` / `emergency_cooldown_seconds` (20 / 5), `brake_grace_seconds` / `emergency_grace_seconds` (8 / 3) |

## fseventsd guard

`fseventsd` hands file events to every watcher on the Mac: Spotlight, git fsmonitor, dev servers, tsserver, editors. It normally takes 5 to 50 MB. On 2026-10-06 it grew to 41.7 GB on a 48 GB Mac, swap reached 48 GB, the system stopped answering and the kernel restarted it (`watchdog timeout: no checkins from watchdogd`). It had been growing for a day: 7.2 GB seven hours before the crash, then about 5 GB an hour. The memory guard for your own processes can't see it, because it runs as root.

`./install-fsguard.sh` installs `fsguard.py` as a root LaunchDaemon that runs every minute. When two readings in a row are over 4 GB, it stops `fseventsd` (SIGTERM, then SIGKILL after 10 seconds) and launchd starts it again at once. Watchers registered before the restart go deaf: a Node `fs.watch` gets nothing afterwards. So the guard then stops the `git fsmonitor--daemon` processes, and the next git command starts a fresh one with a full scan; the [dev server guard](#dev-server-guard) restarts, in their Orca terminal, the dev servers started before the restart, except protected ones; editors and language servers (tsserver, gopls) need a restart by hand, and a notification says so. That cost is why the limit sits at 4 GB, a hundred times the usual size and a tenth of the crash. Restarts are at least five minutes apart. The log at `/Library/Logs/claude-acc-fsguard.log` keeps the daemon's size every hour (every ten minutes above 512 MB) and notes any other process above 8 GB. `./install-fsguard.sh --uninstall` removes it.

## Updates

`updates.py` keeps the tools on the Mac at their newest version:

- **Homebrew**: `brew update`, then every outdated formula and cask. Casks that update themselves (Chrome, Slack) are left to their own updater, as `brew upgrade` does without `--greedy`. Pinned formulae (`brew pin`) stay where they are.
- **npm**: every global package to its latest version, major versions included (for global packages `npm outdated` reports `latest` as the wanted version). `npm_pins` keeps a package within one major version. npm 12 doesn't run the install scripts of packages outside `allowScripts`, so a package that downloads its binary in one installs fine and then doesn't run. Each command a package puts on the `PATH` is run with `--version` before and after its upgrade; one that stopped answering brings the old version back, and the panel names the blocked scripts with the `--allow-scripts` command to allow them on purpose.
- **Go**: every program `go install` put in `GOBIN` (or `GOPATH/bin`), read with `go version -m` and installed again at the module's latest version.
- **Python**: the pip packages of every Python people install into: the default `python3` from uv (`uv python install --default`), python.org and Homebrew. The packages share their dependencies, so they go up together: one `pip install -U --upgrade-strategy eager` over the packages nothing else requires, so the resolver sees every constraint at once, from wheels only (a package whose new release is source only, like `llama-cpp-python`, stays where it is). Packages in the user site get their own run with `--user`; packages installed from a folder or git are left alone, because the PyPI package of the same name is someone else's. `pip check` runs before and after, and so does an import of every package nothing else requires: a new conflict, or a package that stopped importing (a native library `pip check` can't see), puts that Python back to the versions it had. A package the resolver kept lower because another needs it is shown as held back, not failed. `pip_pins` keeps a package at a specifier, even when it is only a dependency of another. A new Playwright gets its browsers.
- **Python itself**: a newer patch release of the python.org Python is downloaded and its signature checked (Developer ID Installer: Python Software Foundation); it needs an admin password, so the panel offers an **Install** button that opens it. A new minor version (3.14 to 3.15) is never offered, since every package would need installing again. Pythons managed by `uv` go up with `uv python upgrade`, except one whose packages this step upgrades (uv's default `python3`): a new patch is a new folder, and the packages would stay behind in the old one.
- **Claude Code**: `claude update`, then the plugin marketplaces and every installed plugin in its own scope (user, project and local, each run from its project; installs whose project is gone are skipped). Without an SSH key for GitHub the plugins clone over HTTPS, because `claude plugin update` would try SSH only, and every git clone gets 10 minutes instead of Claude Code's 120 seconds, so a slow link to GitHub doesn't fail the run. A plugin whose update wants to run a command from its marketplace is never confirmed by the script: the panel gives the command to review in Terminal. Skills installed with `npx skills add -g` are updated too, except one whose folder no longer matches the hash in `~/.agents/.skill-lock.json`: you edited it, and an update would overwrite it.

launchd starts `updates.py run` every day at 04:30 (a sleeping Mac catches up when it wakes), and the script goes ahead once 3 days have passed since the last run, or the next night after a run that failed. Each cask and npm package is upgraded on its own and every result is checked afterwards, so one failure doesn't stop the rest and the panel names it. A cask whose installer asks for an admin password can't be upgraded in the background, so the panel gives the command to run in Terminal. A notification comes for a new problem, not every night for the same one. The **Update** button and `claude-acc update` run it now; the full output of every command goes to `~/.local/share/claude-acc/updates.log`.

Configuration lives in `~/.local/share/claude-acc/updates.json`. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `every_days` | `3` | Days between runs |
| `retry_hours` | `20` | After a failed run, try again once this many hours have passed (the next night) |
| `skip` | `[]` | Steps to leave out: `brew`, `npm`, `go`, `pip`, `claude` |
| `npm_pins` | `{}` | Global npm packages held in a version range, e.g. `{"pnpm": "11"}` keeps pnpm on the newest 11.x |
| `pip_pins` | `{}` | Python packages held at a specifier, e.g. `{"fb-idb": "==1.1.7"}` |
| `python` | uv's default, python.org, Homebrew | The Python (a path or a list) whose packages are upgraded |

## Mail gateway

`claude-acc mail mcp` is a stdio [MCP](https://modelcontextprotocol.io) server (protocol 2026-07-28 without a handshake and 2025-11-25 with one, no SDK, Python standard library only) with eight tools: `mail_mailboxes`, `mail_search`, `mail_read`, `mail_thread`, `mail_attachment`, `mail_modify`, `mail_draft` and `mail_send`. `claude-acc mail install` registers it as `mail` in your user scope, so every session on the Mac has it, together with the `mail` skill and a prompt hint (below); scripts and other agents get the same tools from `claude-acc mail <tool>`. Configuration lives in `~/.local/share/claude-acc/mail.json` (0600).

**Mailboxes.** Each one has a provider, a level and a send switch:

```json
{
  "google": {
    "service_account": "claude-mail@your-project.iam.gserviceaccount.com",
    "identity": {"type": "aws", "audience": "//iam.googleapis.com/projects/N/locations/global/workloadIdentityPools/POOL/providers/PROVIDER", "aws_profile": "mail-gateway", "region": "eu-central-1"}
  },
  "mailboxes": {
    "contact@example.com": {"provider": "gmail", "access": "draft", "send": "ask"},
    "ceo@example.com": {"provider": "gmail", "access": "read"},
    "info@other.example": {"provider": "imap", "access": "modify", "host": "imap.other.example", "smtp_host": "smtp.other.example"}
  }
}
```

`read` searches and reads, `modify` also labels, archives and marks read, `draft` also writes drafts, which never need approval. `send` is `off` (the default: the agent leaves the draft and you send it), `ask` or `auto`. With `auto` the draft goes out at once: while no mailbox is `ask`, `mail_send` carries no approval flag, so a session in `bypassPermissions` sends without stopping. With `ask` a person approves every send: `mail_send` carries Claude Code's `anthropic/requiresUserInteraction` (the flag is per tool, so once any mailbox is `ask`, Claude Code prompts for every send), so Claude Code shows its permission prompt on every call, even in `bypassPermissions`, with the recipients and subject the agent must copy from the draft (the gateway refuses if they differ). Other clients get the gateway's own confirmation through MCP elicitation, or the agent has to ask you and pass `user_confirmed`. The **Mail** card in the panel shows every mailbox, whether it answers, its level and send mode, today's calls and the last few calls agents made; **Check** signs in to each one.

**Google Workspace.** A service account with [domain-wide delegation](https://support.google.com/a/answer/162106) for `gmail.readonly`, `gmail.modify` and `gmail.compose` acts as each mailbox. Nothing secret sits on disk:

- `identity.type: "key"` keeps the service account key only in the Keychain (service `claude-acc-mail`, account `google-service-account`). `claude-acc mail key-create --service-account SA` asks the IAM API for a key with your gcloud sign-in and puts the answer straight into the Keychain, and each delegation JWT is signed by `/usr/bin/openssl` reading the key from a pipe, so it never touches a file. It never expires and needs no sign-in, at the price of a long-lived secret; organizations that block key creation (`iam.disableServiceAccountKeyCreation`) need a one-off exception.

- `identity.type: "aws"` signs an STS `GetCallerIdentity` with your AWS credentials (`aws configure export-credentials`, any profile, typically a role you assume), Google STS exchanges it in a [Workload Identity pool](https://cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds) for a federated token, and IAM Credentials `signJwt` signs the delegation JWT with `sub` set to the mailbox. No browser session, so no reauthentication every few hours.
- `identity.type: "gcloud"` uses your Application Default Credentials instead; your Google account needs `roles/iam.serviceAccountTokenCreator` on the service account.

Tokens stay in memory, one per mailbox and scope, and each call asks for the narrowest scope it needs.

**A Gmail mailbox without delegation.** When you are not the admin of the domain (an employer's or a client's Workspace, a personal Gmail), `--token-command` takes a command that prints an OAuth access token from the mailbox owner's own consent, for example one built on `gws`. Such a mailbox needs no service account. The gateway reads the token's scopes from Google's tokeninfo once per token and refuses an operation the consent does not cover, naming the missing scope, so a read-only consent fits the `read` level.

**IMAP and SMTP.** Any server over TLS. The password, or an app password, sits in the Keychain under the service `claude-acc-mail` (`claude-acc mail add` asks for it in a system dialog with a hidden field and stores it through `security`, so it never passes through the agent, a process list or your shell history); `--xoauth2-command` takes a command that prints an OAuth token instead, for Microsoft 365 and the like. Search takes the useful part of Gmail's syntax (`from: to: cc: subject: newer_than:7d after: before: is:unread is:starred in:FOLDER` and words) and turns it into IMAP `SEARCH`; threads are rebuilt from `Message-ID` and `References`; `UNREAD` and `STARRED` are flags, removing `INBOX` moves to the archive folder and `TRASH` to the trash; drafts are appended to the drafts folder, and a sent draft goes out over SMTP and lands in the sent folder. A connection waits two minutes for the next call and is checked with `NOOP` before reuse, so a run of calls signs in once; on a distant server the TLS handshake and `LOGIN` are most of a call. Folder names default to `INBOX`, `Archive`, `Trash`, `Drafts` and `Sent` and can be set per mailbox (`archive_folder`, `trash_folder`, `drafts_folder`, `sent_folder`).

**How agents find it.** `claude-acc mail install` puts three things in place, and `claude-acc-setup` refreshes them whenever mailboxes are configured:

- the `mail` skill (`~/.claude/skills/mail/SKILL.md`): a description of a few lines that Claude Code keeps in context, and a short recipe it loads only when a task needs an email (find, read, attachment, answer, send, file, wait);
- a prompt hint: a `UserPromptSubmit` hook (`hint.py`, shared with the browser gateway, about 10 ms, exec form) that gives the session one line with your mailboxes and their levels when your message talks about mail, a reply from someone, a verification code or one of the configured addresses, at most once per session;
- the guard: the dev server guard's Bash hook refuses commands that read the gateway's secrets out of the Keychain (`security find-generic-password ... claude-acc-mail`, `dump-keychain`), so the key and passwords stay with the gateway.

`claude-acc mail wait <mailbox> '<query>'` waits for a new message matching the query (messages that already matched when it started don't count). An agent runs it in the background and is woken by Claude Code when it exits: code 0 with the message, 3 after `--timeout`.

**Untrusted content.** An email is data from a stranger. The gateway strips control, zero-width and bidirectional characters, turns HTML into text without scripts and styles, cuts long bodies, lists links without opening them, and hands bodies to the agent inside `<untrusted-email id="...">` with a random id per call, so a message can't close the envelope early; a fake tag inside a message is removed. Attachments go to `~/.local/share/claude-acc/mail/attachments` with owner-only permissions. The server's instructions tell the agent never to act on what an email asks. Every call, successful or not, is appended to `~/.local/share/claude-acc/mail/audit.jsonl` with the client, tool, mailbox and ids, never the content.

## Browser gateway

`claude-acc browser mcp` is a stdio MCP server with Anthropic's browser use toolset (`browser_toolset_20260801`), the tools Claude models are trained on, under their own names and inputs: `navigate`, `screenshot`, `zoom`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `hover`, `left_click_drag`, `left_mouse_down`, `left_mouse_up`, `mouse_move`, `scroll`, `scroll_to`, `type`, `key`, `hold_key`, `wait`, `read_page`, `find`, `get_page_text`, `form_input`, `file_upload`, `read_console`, `read_network`, `javascript_exec`, `new_tab`, `list_tabs`, `switch_tab` and `close_tab`. Three more belong to the gateway: `show_tab`, `user_tabs` and `borrow_tab`. A target is a ref from `read_page` or `find` (`{"type": "ref", "ref": "ref_3"}`) or a point on the last screenshot (`{"type": "coordinate", "x": 640, "y": 300}`). Results read like the API's own: `Navigated to <url> - <title> (HTTP 200)`, page content in an envelope, and a Tab Context with the agent's tabs and what changed (tabs opened, downloads, dialogs, refused navigations) whenever that changed. `claude-acc browser install` registers it as `browser` in your user scope with the `browser` skill and the shared prompt hint; scripts call any member with `claude-acc browser <member> '<json>'`. Python standard library only: its own WebSocket client and the Chrome DevTools Protocol, no Puppeteer, no Node.

**From the SDKs.** `sdk/python/claude_acc_browser.py` (`ClaudeAccBrowser`, `AsyncClaudeAccBrowser`) and `sdk/typescript/claude-acc-browser.ts` (`ClaudeAccBrowser`) subclass the SDKs' abstract browser toolsets (`anthropic>=1.12`, `@anthropic-ai/sdk>=0.132`). Your own agent passes one to `tool_runner` / `toolRunner` and keeps the SDK's URL and file policies and confirmations, while every call runs in your browser through the same daemon, gate and audit log. `claude-acc browser run "<task>"` is that loop ready made: `uv run --with anthropic`, streaming, with each tool call started before the response ends, and the key from `ANTHROPIC_API_KEY` or the Keychain (`claude-acc browser api-key`).

**Getting in, once.** Since Chrome 136 a browser on its default profile ignores `--remote-debugging-port`. Since 144 the way in is a checkbox: tick *Allow remote debugging for this browser instance* at `chrome://inspect/#remote-debugging` (or `brave://inspect/...`). A link or `open` can't reach these pages, so `claude-acc browser setup` and the panel's **Turn on** open the page in a new tab of that browser through AppleScript. It survives restarts. The browser then listens on localhost only and asks *Allow remote debugging?* for every new connection, bringing its window forward. So the connection is held by one daemon (`claude-acc browser serve`, started by the first tool call, gone after ten quiet minutes) and every session talks to the daemon over a `0600` unix socket. You click **Allow** once per browser start, not once per session. While it is connected the browser shows its *controlled by automated test software* bar. After `idle_minutes` (20) without a call, or on **Disconnect** in the panel, the daemon closes the agents' tabs and the connection, so the bar goes away. While a running agent session still has tabs open, it waits six times as long (two hours by default), so an agent that stops for a build or a test run comes back to its pages.

**No focus taken.** Agent tabs are hidden targets by default (`tabs-mode hidden`): pages with your cookies and logins that have no window and no place in the tab strip. Focus emulation keeps them rendering at full rate, and they get a 1280x860 viewport for layout and screenshots. With `tabs-mode background` they open as background tabs in your last window instead. No tool brings a page forward, and `switch_tab` only changes which tab the agent works on. A page that opens a window (`target=_blank`, `window.open`) gets it as another agent tab, reported in the Tab Context. File choosers are intercepted, so no native dialog appears over your work. `show_tab` turns a hidden tab into a normal background tab and sends a notification, for the login, captcha, 2FA or payment only you should do. `borrow_tab` lets an agent borrow one of your own open tabs (listed by `user_tabs`), and `close_tab` gives it back without closing it.

**What agents see.** Not screenshots by default: `read_page` gives the page's accessibility tree in the toolset's format, `role "name" [ref_N] [flags]: value` indented by depth, with layout containers removed, text runs joined, table rows as cells, `<select>` options inline, and frames from other sites (a payment field, a captcha) included through their own sessions. Without a filter it shows what is in the viewport, `interactive` only what can be acted on, `all` the whole page. A ref stays the same for the same element until the page loads a new document. `find` matches a description ("search field", "Download invoice link") against roles and names, and `get_page_text` gives the text, up to 50 000 characters. Screenshots are JPEG at one CSS pixel per pixel, so coordinates are used as they are. Clicks and keys are real input events (`Input.dispatchMouseEvent`, `insertText`), `form_input` sets fields, selects and checkboxes, and uploads go straight into the file input. The daemon keeps each tab's console and network requests for `read_console` and `read_network`. Alerts are accepted and confirms dismissed (accepted in full mode), and both show up in the Tab Context.

**The gate.** Each session sees and touches only the tabs it opened. When it ends, its tabs close two minutes later, except ones handed to you. From the shell every call is its own process, so the session is the Claude Code session the command runs in (`CLAUDE_CODE_SESSION_ID`, and its tabs close when that session's process is gone), else the Orca terminal or the terminal window; `CLAUDE_ACC_BROWSER_OWNER` names one yourself. Two agents calling `claude-acc browser` at once never navigate, read or close each other's tabs. In the default `guarded` mode sites have levels in `~/.local/share/claude-acc/browser.json`. `act` (the default) allows everything, `read` allows opening, reading and screenshots, `deny` allows nothing, and the longest matching pattern wins:

```json
{"default": "brave", "tabs": "hidden", "idle_minutes": 20,
 "sites": {"dashboard.stripe.com": "read", "*.internal.example": "deny"}}
```

Polish banks, Revolut, Wise and PayPal are `read` out of the box, because a payment is one click. Browser pages (`chrome://`, `brave://`, extensions, `file:`, `data:`, `javascript:`) are closed. A navigation to a closed or denied address is stopped before the request leaves (`Fetch`), so the page stays where it was and the Tab Context reports the refusal. `javascript_exec`, `file_upload`, `show_tab` and `borrow_tab` ask you first (`anthropic/requiresUserInteraction`), and uploads refuse files from `~/.ssh`, `~/.aws`, the Keychains, browser profiles and claude-acc's own state. The gateway never calls the cookie methods. Page content reaches the agent inside `<untrusted-page id="...">` with a random id, and fake closing tags are stripped from it. Every call lands in `~/.local/share/claude-acc/browser/audit.jsonl` with the session, tool, tab and address without its query string. Typed text and scripts are never logged, only their length and, for a script, a short hash.

**Full mode.** `claude-acc browser mode full` is for when you let the agent do anything. Nothing asks, every address is `act` except the levels you set yourself with `site`, browser pages and `file:` open, uploads take any file, and confirms are accepted. Sessions still see only their own tabs and the audit log still records every call. `claude-acc browser mode guarded` goes back.

**How agents find it.** The `browser` skill (when to use it and the recipe: navigate, read before you look, act on refs, waiting and popups, the human, done). The shared prompt hint names your default browser when a message talks about a browser, clicking, signing in or a console, and tells the agent what to ask you when debugging is off. The guard's Bash hook refuses commands that read the Chrome or Brave `Safe Storage` key or the profile's `Cookies`, `Login Data` and `Web Data` files. The **Browser** card in the panel shows each browser (connected, asking for Allow, ready, not running, debugging off with **Turn on**), the agents' tabs and the last call, refreshed every 5 seconds while the panel is open.

**Answering Allow for you.** Every new connection makes the browser ask *Allow remote debugging?*. With `"auto_allow": true` in `browser.json` (off by default) the daemon answers it for you: while it waits for approval of its own handshake, and only then, it presses **Allow** with the desktop gateway's native helper, and only when exactly one such dialog belongs to the real browser (the process that owns the debugging port from `DevToolsActivePort`). It never presses *Turn off in settings* or *Cancel*, and does nothing when more than one dialog is open, so you still decide in any case it does not recognise. The helper needs Accessibility (see the Desktop gateway below).

## API credits

Every Max plan gets monthly [API credits](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers): $200 on Max 20x, $100 on Max 5x. They land in one Claude Console organization you link from claude.ai, show up there as **Promotional credits**, and pay for the Messages API, Message Batches, the Agent SDK and `claude -p` run with that organization's API key. They don't pay for interactive Claude Code, even with a key. They expire at the end of each billing cycle and never touch your plan: once they run out, requests fail with *credit balance is too low* until the next cycle. With several plans you have several organizations and several keys, and `claude-acc credits` turns them into one pool.

**Setting up an account.** Each plan can be linked to exactly one organization, and only support can change it, so give each plan a new organization of its own.

1. In Console (platform.claude.com), create an organization, for example `oop-credits-work`.
2. On claude.ai, signed in with that plan: **Settings > Billing > API credits > Link organization**, pick the new organization and accept the terms. No card is needed. The button is being rolled out over a few days, so if it isn't there yet, run `claude-acc credits pending work@example.com` and try again tomorrow.
3. In Console, in the new organization: **Settings > API keys > Create key**, linked to yourself and scoped to the **Default** workspace. A key that isn't scoped to a workspace needs an `anthropic-workspace-id` header on every request, which `claude -p` and plain scripts don't send.
4. `claude-acc credits add work@example.com --scope own` opens a dialog with a hidden field. Paste the key there. It goes straight to the Keychain (service `claude-acc-credits`, account = the email), and claude-acc asks the API which organization it belongs to, so a key pasted into the wrong account is refused. `--scope acme` (any lowercase name) marks a plan whose credit may only pay for that client's work. Console now issues user-linked keys (`sk-ant-usr-...`) next to the classic ones (`sk-ant-api...`); both work, Admin API keys (`sk-ant-admin...`) are refused.

   A key that already sits in another Keychain item (say a program created it there) can skip the dialog: `claude-acc credits add work@example.com --scope own --from-keychain SERVICE/ACCOUNT` reads it with `security` (the key only travels through a pipe, never an argument), checks it against the API like a pasted one, and stores a copy under `claude-acc-credits`. The source item stays as it was. If that item was created by another app, macOS may ask once to allow access; `security` gives up after 30 seconds.

**Running on credits.** `claude-acc credits exec --purpose nightly-digest -- python3 digest.py` picks the organization whose credit expires first and still has `--min-remaining-usd` (5) left, puts its key in `ANTHROPIC_API_KEY` for that command only, and passes stdin, stdout, stderr and the exit code through. It drops `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the Bedrock, Vertex and Foundry switches from the command's environment, because Claude Code would prefer them to the key. `--org ID` pins one organization with no fallback, and scopes never mix: `--scope acme` uses only organizations marked `acme` and exits 75 when they have no headroom, even if your own credit does, and the default `own` never touches a client's credit. Exit codes: the command's own; **75** when no organization had headroom before starting (wait, or run on the subscription); **76** when the API answered *credit balance is too low* while the command ran. A 76 means the run stopped halfway and may have left side effects, so don't retry it blindly. That organization then counts as empty until its next cycle or Console reading. claude-acc notices it by watching the command's output for that sentence (`claude -p` prints it to stdout and exits 1), together with a non-zero exit, and stores nothing of the output.

A key in the environment is readable by anything the command starts, including an agent's Bash tool. For agents, use `--no-env`, which puts no key anywhere and lets Claude Code fetch it through `apiKeyHelper`:

```sh
claude-acc credits exec --no-env --purpose nightly-digest -- \
  claude -p "Summarize today's notes" \
  --settings '{"apiKeyHelper": "claude-acc credits helper --purpose nightly-digest"}'
```

`credits helper` prints the key of the organization `exec` picked (`CLAUDE_ACC_CREDITS_ORG`) to Claude Code alone: Claude Code runs it at startup, after an hour (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`) and after a 401, with a 5-second timeout. It refuses to print to a terminal, and the dev server guard refuses an agent's Bash command that calls it or reads `claude-acc-credits` from the Keychain. If the helper was never called during an `exec --no-env` run, `exec` warns you: that command paid with something else, usually the subscription. `claude-acc credits key --purpose NAME --json` gives programs that read the Keychain themselves the entry to read (`service`, `account`, `org_id`), never the key.

**What is left.** No API returns the balance of promotional credits, and the Admin API (usage and cost reports) is [not available to individual accounts](https://platform.claude.com/docs/en/manage-claude/admin-api), so Console's **Settings > Billing > Promotional credits** is the truth. `claude-acc credits balance work@example.com --remaining-usd 143.20 --expires-at 2026-11-15` records what it shows. Between readings, callers report each call's cost (`credits record --org ID --usd 0.42 --purpose NAME`, for example the `total_cost_usd` that `claude -p --output-format json` returns), and the remaining amount is the last reading minus what was reported after it. Without a reading in the current cycle it is the plan's grant minus what was reported since the cycle started. Spend nobody reported (the Playground, a script outside `exec`) isn't counted until the next reading. The cycle is the monthly anniversary of `--resets-at`/`--expires-at`, or else of the subscription start that `claude-acc status` reads. `claude-acc credits status --json` gives:

```json
{"total_remaining_usd": 287.5, "accounts": [{"email": "work@example.com", "org_id": "...", "scope": "own",
  "granted_usd": 200.0, "spent_usd": 12.5, "remaining_usd": 187.5, "cycle_resets_at": "2026-11-15T00:00:00+01:00",
  "checked_at": null, "state": "linked"}]}
```

`state` is `linked`, `pending` (no button yet) or `error` (the key is gone from the Keychain). The total counts linked organizations only. `claude-acc status` ends with the same total, and the **API Credits** card in the panel shows what is left of this cycle's grant, what expires next and how old the Console reading is. Files: `~/.local/share/claude-acc/credits/` (`accounts.json`, the monthly spend ledgers `ledger-YYYY-MM.jsonl` and `credits.log`, all `0600`, with no keys in them).

**When a plan ends.** A cancelled plan keeps its credit until the plan ends and gets no new grant after that, but the cycle arithmetic alone would hand it a fresh $200 at the next anniversary. `claude-acc credits balance work@example.com --plan-ends-at 2026-11-01` records the end: from that date the organization has 0 left (and `exec` exits 75 with its usual message when it was the only one) until a Console reading taken after the end, which counts as usual. While the plan still runs, its credit counts as expiring on the end date if that comes before the cycle reset, so it is spent first. `--plan-ends-at none` lifts the end, for example after you resume the plan. `credits status` shows the end next to the organization.

### Unattended runs: `credits run`

`exec` hands a key to one command. `claude-acc credits run` is for unattended agent work that starts its own `claude -p` sessions, nested ones in an agent's Bash tool included, and has to know before it starts who pays, and afterwards what it cost:

```sh
claude-acc credits run --purpose blog-outofplace --budget-usd 5 --summary /tmp/run.json -- pnpm autopilot
```

**Who pays** is settled once, before the command starts, for the whole run:

1. Credit, when an organization `exec` would pick has at least the budget × 1.5 (where the run is stopped) plus a reserve left, $20 by default (`--reserve-usd`), so runs never eat the credit that interactive callers count on. Runs still in progress hold their own budget × 1.5 (or what they spent, if more) on their organization, and the choice and the run's registration happen under one lock, so two $25 runs can't both start on $60.
2. Otherwise a subscription account from `claude-acc token`: only a token from the rotation (never a fallback), never the active account and never one listed in `last_resort` (the work account), preferring the tail of the headroom queue (the head is what your own sessions switch to next), with at least 40% of the session and 15% of the week left and a token valid for `--awake-minutes` (120) plus 30 minutes. An access token lives about 8 hours, so a run that would need more than 450 minutes is refused before `claude-acc token` is even asked (asking would refresh every candidate account for nothing). An account that got a 429 in a run is skipped by later runs until its session resets.
3. Otherwise the run is refused: exit **75**, one line saying why, and nothing starts.

A run is also refused before the start when the project in its working directory has its own `apiKeyHelper` or login variables in `.claude/settings.json` or `.claude/settings.local.json`: project settings rank above the run's own, so they would pay instead of the chosen payer.

`--mode credits` or `--mode subscription` forces one of the first two. The first line on stderr names the payer and the reason.

**What the command gets.** Each run has its own directory, `~/.local/share/claude-acc/runenv/runs/<id>/`. Its `config/` is `CLAUDE_CONFIG_DIR` for every `claude` in the run, with a `settings.json` written by claude-acc: on credit an `apiKeyHelper` that names the organization and the run in its own command (so a child that lost its environment still pays with that organization), OpenTelemetry pointed at the run's own receiver, `bypassPermissions` with `PushNotification`, `RemoteTrigger` and `Monitor` denied, auto memory off, a one hour prompt cache, and one `PreToolUse` hook for `Bash` and `Monitor` that stops an agent from reading the credit keys, the run's login or `claude-acc token`, and from running any `claude-acc credits` command other than `status` (`credits exec -- printenv ANTHROPIC_API_KEY` would hand it the key). If the hook itself can't run, it blocks the command instead of letting it through. A short `CLAUDE.md` tells the agent nobody is there to answer. On a subscription the chosen account's access token, without its refresh token, goes to a Keychain entry of the run's own (`Claude Code-credentials-<hash of the directory>`) through the stdin of `security`, never an argument or a file, so the run can't refresh the account and log its other sessions out. `bin/claude` comes first on the command's `PATH`: it starts the pinned Claude Code version, always with the run's directory and without the variables that would override the login, so a nested `claude -p` pays the same way. Pipelines that start `claude` with `--restricted` or without user settings merge the file in `$CLAUDE_ACC_JOB_SETTINGS` into their own `--settings` to keep payment, metering and the guard.

**Metering.** The receiver (OTLP/HTTP JSON on 127.0.0.1) counts the `cost_usd` of every API request once, per session. The run is stopped at 1.5 × the budget, when the payer check fails mid-run (also after an earlier 429 or 401), when a `claude` is found running without the run's directory, or when the credit runs out: SIGTERM to the command, and 30 seconds later, if it is still there, the whole process tree of the run. At the end it prints a summary (cost, requests, sessions, payer check, metering, errors; `--summary FILE` writes it as JSON), books credit spend in the spend ledger with one entry per session, dated at the session's last request, and appends it to `runenv/history.jsonl`. A `claude -p` or Agent SDK session written under `~/.claude/projects/<working directory>` while the run lasted counts as a leak; your own interactive sessions there don't. The payer check passes on credit when the requests came through the helper of the chosen organization with no account email attached, and on a subscription when they carry the chosen account's email and not the work organization. A session that sent no request, or a request without a cost, makes the metering incomplete and the payer unverified, never a silent $0. Exit codes: the command's own; **75** refused before the start; **76** the credit ran out mid-run (the organization then counts as empty; don't retry blindly); **78** something other than the chosen payer paid.

**Cleanup.** The run's processes are stopped, the Keychain entry and the directory removed, except `bin/claude`, which refuses to start for another day so a straggler fails instead of falling back to your normal login. A run whose owner was killed is cleaned up by the next `run` (or `claude-acc credits run` from any caller): its processes stopped, its cost booked once. From the moment the owner dies, the run's credit helper hands out no more keys, so a `claude` that outlived it can't keep spending. A session found in the working directory of such a run makes its payer unverified rather than mismatched, since the run's time window can no longer be pinned down.

**Pinned version.** `run` uses only the Claude Code version recorded in `runenv/pin.json` and refuses, naming it, when that version is gone. After an update, `claude-acc credits canary` runs one Haiku call (about half a cent) through a full run and pins the installed version only if the call was metered with its cost and the payer check passed.

Programs use the same thing from Python: `runenv.prepare(purpose, budget_usd, ...)` returns the run (its `env`, payer, live `spent()`, `alarm()` and `alarms()`), `runenv.finish(run, exit_code)` returns the summary and `runenv.exit_code_for(summary, code)` the exit code; the docstring at the top of `runenv.py` has the details.

## Jobs

`claude-acc jobs` runs a blog pipeline (one repo command that follows the blog pipeline contract) unattended and leaves one record per attempt that says what happened, what it cost, who paid, the links and the log.

```sh
claude-acc jobs add outofplace --cwd ~/orca/workspaces/outofplace/blog-autopilot \
  --precheck 'sh scripts/autopilot/precheck.sh' --entry 'pnpm autopilot' --budget-usd 25
claude-acc jobs run outofplace     # one attempt; exits with the contract's code, 75 when it could not start
claude-acc jobs list               # every job, its last outcome, a run in progress, a hold
claude-acc jobs log outofplace     # the last attempts and the tail of the latest log
claude-acc jobs set outofplace budget_usd=30 run_min=240
claude-acc jobs set outofplace live_urls=https://example.com/blog   # checked after an unclean publish
claude-acc jobs hold --hours 4 --reason 'iOS build'   # until `jobs release`
```

An attempt picks a payer through `credits run`'s run environment (credit from the pool, otherwise a subscription account, never the work account), runs the precheck and then the entry command in their own process groups with the pinned `claude` first on `PATH` (built like the updater's, so `pnpm`, `node`, `vercel` and `gh` resolve under launchd), and holds `caffeinate -i -s` for as long as the runner lives. Limits are in awake minutes and count from the moment a command starts: time the Mac sleeps, time spent waiting for memory and time a heavy step waits in the build scheduler's queue are not counted. Before the start the run waits for memory (no brake, no critical pressure, `footprint_gb` free); heavy steps inside the pipeline go through `$CLAUDE_ACC_JOB_HEAVY`, one at a time through the scheduler. One attempt per job at a time (a second one exits 73 without a record) and one heavy job at a time across jobs.

The outcome follows the contract's exit code unless the runner saw something itself before the pipeline wrote its side-effect marker: a credit that ran out, a 401 or 429, a sleep, the memory brake, a signal, the time limit. Those make it BŁĄD, retryable, with a hint to pay differently next time where it fits. The brake kills first and logs its victim at the end of its pass, after the pipeline has already exited, so on a failing end the runner waits up to 12 seconds for the guard's state file to change before it decides. The side-effect phase starts with the marker, with a final result that says OPUBLIKOWANO, ZAPARKOWANO or PILNE, or with exit code 0, 2 or 5, and the runner writes the exit code to `current.json` the moment the pipeline ends. After that nothing is retried: any unclean end, a runner error included, is PILNE, and the runner opens the result's URLs itself, or the job's `live_urls` when the result has none (the marker carries no URLs). A stop at 1.5 × the budget, a payer mismatch or a leaked login is final and carries a banner. An attempt that could not start is POMINIĘTO with a `skip` cause (`heavy`, `hold`, `memory`, `payer`, `version`, `interrupt`) for the scheduler to time its retry. A runner killed outright leaves its attempt in `current.json`, and the next `jobs` command turns it into a record once the run environment has settled the run's bill. Records live in `~/.local/share/claude-acc/jobs/<job>/history.jsonl`, logs next to them in `logs/` (60 per job, skipped attempts' logs pruned first); the field list is in the docstring at the top of `jobs.py`.

## Desktop gateway

`claude-acc desktop mcp` is a stdio MCP server with Anthropic's computer use toolset (`computer_toolset_20260801`), the tools Claude models are trained on, under their own names and inputs: `screenshot`, `zoom`, `cursor_position`, `mouse_move`, `left_mouse_down`, `left_mouse_up`, `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`, `scroll`, `type`, `key`, `hold_key` and `wait`. It drives the whole macOS desktop the way the browser gateway drives your Chrome: the agent takes a `screenshot` of the active display, then clicks and types at coordinates in that screenshot's pixel space. `claude-acc desktop install` registers it as `desktop` in your user scope with the `desktop` skill and the shared prompt hint; scripts call any member with `claude-acc desktop <member> '<json>'`.

**The native helper.** Python can't post real input events or capture the screen on its own, so a small Swift program, `claude-acc-desktop`, does that part: screenshots through ScreenCaptureKit, and mouse and keyboard as global `CGEvent`s. `desktop.py` holds one copy of it running, maps the model's coordinates to display points, keeps the gate and the audit log, and speaks the MCP protocol. Clicks and keys go to the active display; the toolset is coordinate-based by design, exactly as Anthropic documents it.

**Coordinates and screenshots.** Each display is captured at its logical size (a Retina screen's 2x pixels scaled back to points), capped at 1600 px on the long edge, so one screenshot pixel is one point and the model's coordinates map straight through; a larger display is scaled down and the mapping carries the ratio. `zoom` crops a region at full resolution for small text, and coordinates stay in the full screenshot's space, zoom included, as the spec requires. Screenshots are PNG.

**Permissions.** The two macOS permissions it needs, Accessibility (to post input) and Screen Recording (to capture), attach to the helper binary's code signature. The helper is signed with a stable designated requirement by identifier (`com.filip.claude-acc.desktop`), so the grant survives rebuilds even though ad hoc signing changes the hash each time. `claude-acc desktop doctor` says whether each permission is on and, with `--open`, opens the right System Settings pane so you can tick the one binary; it never changes a security setting itself. Keys use xdotool-style names (`Return`, `Tab`, `ctrl+shift+Escape`); on macOS write `cmd` for shortcuts (`cmd+a`, `cmd+c`), not `ctrl`.

**The gate.** Full mode (the default) lets the agent do anything on the desktop, with one floor that holds even in full mode: a short deny list of secret apps stays read-only, and when one of them is frontmost every member is refused, screenshot included, so an agent can neither read nor change secrets or permissions through the GUI. The list is Keychain Access, the Passwords app, 1Password, and the System Settings Passwords and Privacy panes (matched by the frontmost window's title); add your own with `deny_bundles` in `desktop.json`. Guarded mode additionally asks you before `type`, `key` and `hold_key` (`anthropic/requiresUserInteraction`), because keyboard input goes to whatever has focus and what the screen shows can steer it. Focus is never taken except as the natural result of a click. Every call lands in `~/.local/share/claude-acc/desktop/audit.jsonl` with the session, member and frontmost app; typed text is never logged, only its length.

**How agents find it.** The `desktop` skill (look first with a screenshot, act on coordinates, keys on macOS, read the result, how to open an app). The shared prompt hint points at the `desktop` tools when a message talks about a native app, the Finder, System Settings, a window or a screenshot, and sends web work to the `browser` gateway instead. The **Desktop** card in the panel shows whether the helper has its two permissions, the active displays, the mode and the last call, with **Open Settings** when a permission is off.

There is no desktop daemon and no "open app" member: an agent opens an app through Spotlight (`cmd+space`, type its name, Return) or the Dock. Prefer the `browser` gateway for anything inside a web page; it works in a background tab and never takes focus.

## Shared MCP servers

Every Claude Code session starts its own copy of every stdio MCP server in `~/.claude.json`, so with
eight agents running you have eight copies of each, and a server that loads a model loads it eight
times. `claude-acc mcp share <name>` runs one copy for all of them: a launchd agent
(`com.filip.claude-acc.mcpshare.<name>`) keeps the stdio server alive and serves it as Streamable HTTP
on `127.0.0.1`, and the user-scope entry becomes an `http` entry with a bearer token. Sessions started
after that connect to the shared copy; running ones keep theirs until they restart.

Measured on 2026-10-09 with cavemem (it loads a MiniLM embedder) and nine sessions: 490 MB of copies
became 165 MB for the bridge and the one server, and a session connects in 8 ms instead of 83 ms. A
new session adds nothing.

How it works: the server initializes once and every client gets that answer from memory. Each client
has its own `Mcp-Session-Id`, and its JSON-RPC ids and progress tokens are rewritten to unique ones on
the way in and back on the way out, so two sessions both sending id 1 never get each other's result.
Progress streams back over SSE to the session that asked, cancellations reach the server under its
own id, and a crashed server restarts with backoff while the HTTP side stays up. Both protocol eras
work: the classic `initialize` handshake and 2026-07-28 (`server/discover`, `subscriptions/listen`,
and the `ttlMs`/`cacheScope` that list results need there, which an older server doesn't send). It
listens on `127.0.0.1` only, answers 401 without the token (a 0600 file under
`~/.local/share/claude-acc/mcpshare/`), and 403 for a foreign `Host` or `Origin`, so a web page can't
reach it through DNS rebinding. `~/.claude.json` is backed up before the change and kept at 0600,
because the token is in it.

Only stateless servers can be shared. A server that asks the client something (elicitation, sampling,
roots) can't be told which of the sessions asked, so the bridge answers it with an error, and the
server still runs in your home directory, not each session's project. That is why `share` refuses,
unless you pass `--force`:

- `mail`: it collects send approval through MCP elicitation.
- `browser`: it remembers which tabs each session opened or borrowed.
- `desktop`: it asks for approval through elicitation and maps coordinates from the session's last screenshot.
- `chrome-devtools`: one browser and page per client.
- MCP Magic: every session joins its own Figma channel.

`claude-acc mcp unshare <name>` puts the original stdio entry back exactly as it was and removes the
agent; `claude-acc uninstall` unshares everything first. `CLAUDE_ACC_MCPSHARE_DEBUG=1` in the agent's
environment logs each request's method names (never their content) to `<name>.log`.

## Stay Awake

The **Stay Awake** card holds an `IOPMAssertion`, the same thing `caffeinate` does: the Mac doesn't sleep while it's on, and with **Keep the display on** neither does the screen. It runs until you turn it off or for 1, 2, 4 or 8 hours.

An assertion only stops idle sleep: closing the lid of a MacBook on battery sleeps it anyway. With **Awake with the lid closed** (on by default) a session you started yourself also asks the fan daemon (root, `claude-acc fans install`) to turn on `SleepDisabled`, the switch behind `pmset disablesleep`, through `~/.local/share/claude-acc/awake.json`. The daemon holds it only while the session lasts and the app that asked still runs, lets go on battery at 10% or less, at a serious thermal state (then waits 15 minutes before holding it again), 24 hours after the request even for a session without an end, and when it stops, and never turns off a `SleepDisabled` it didn't turn on (Amphetamine, `sudo pmset disablesleep 1`). A session that switched itself on for a hotspot doesn't ask: a Mac awake in a bag has to be your choice.

With **Auto on any hotspot** it switches itself on whenever the Mac joins a network that macOS marks as expensive: an iPhone or Android hotspot over Wi-Fi or USB, or a cellular modem. It turns off again when you leave that network, unless you turned it on yourself. **Keep the hotspot alive** sends one small request every 25 seconds, so a phone doesn't drop a hotspot it thinks nobody uses. The settings live in the app's preferences.

## Dictation

Tap the **right ⌘** to start and tap it again to put the text in where the cursor is, or hold it while you talk and let go. Escape cancels. The right ⌘ types nothing on its own (the right ⌥ would type ą ę ł on a Polish layout, and holding it turns Orca's pointer into a column-select crosshair), but it is half of every shortcut, so nothing starts on the press itself: a tap is down and up within 250 ms with no other key, click or scroll in between, a hold counts after 300 ms of the same, and a key pressed within the first second of a hold throws the recording away. ⌘C, ⌘V and the rest never light the microphone up.

The recording stays in the app (AVAudioEngine, 16 kHz mono, in memory) and goes to [Vercel AI Gateway](https://vercel.com/ai-gateway) as one request: MAI-Transcribe 2 with the Polish locale pinned and the dictionary as its phrase list, with zero data retention. Over 30 seconds it goes up as FLAC, half the bytes. MAI usually answers a few seconds of speech in 1.5-2.5 s but now and then takes 6-25 s, so after about 3.5 s the fallback model (Grok STT, another provider) starts alongside it and the first answer wins. A correction pass (Gemini 3.8 Flash) then fixes punctuation and the spelling of terms ("dev guard" becomes "devguard"); a fuse throws the correction away if it adds a word the transcript doesn't have or changes too much, so a model answering the dictated command can't slip in. Known hallucinations on silence ("Napisy stworzone przez społeczność Amara.org") are dropped.

Where the text goes:

- A native text field gets it straight at the cursor through Accessibility, with a space at the seam and a lower-case start mid-sentence. The clipboard isn't touched.
- A terminal (Claude Code in Orca, Ghostty, Terminal, iTerm) gets ⌘V. Claude Code folds a paste of more than 800 characters or 3 lines into `[Pasted text #N]`, so a long dictation goes in as pieces of up to 780 characters, 300 ms apart, and line breaks become spaces. The clipboard is put back afterwards and the pieces are marked transient, so clipboard managers skip them.
- If another app came to the front meanwhile, or Accessibility is off, the text waits on the clipboard and the pill says ⌘V.

The pill appears only while you dictate, over every window and Space, and never takes the keyboard; drag it anywhere. A failed transcription keeps the recording for **Retry**. The panel's **Dictation** card picks the model, the correction, the microphone (with **Built-in mic over Bluetooth**, so AirPods keep their music profile), and runs a **Test** on a shipped recording without the microphone.

The key comes from the Keychain (service `AI_GATEWAY_API_KEY`) through `security`, so there is no prompt. Your own terms go in `~/.local/share/claude-acc/dictation/slownik-user.txt`, one per line; the shipped dictionary is `slownik.txt` next to it. `timing.log` there has one line per dictation: time to the first sound, stop to text, the transcription and the correction.

It needs the Microphone and Accessibility permissions (macOS also asks for Input Monitoring). `install.sh` signs the app with your first code signing certificate, or ad hoc with a stable designated requirement, so the permissions survive rebuilds. Every program Claude Acc starts (its Python scripts, `claude-acc` commands) runs with the app's permissions.

`ClaudeAcc --dictate-file <wav>` runs the whole path on a file and prints JSON; `ClaudeAcc --widget-demo` shows the pill through its states.

## Hotspot turbo

An iPhone hotspot is fast downstream and narrow upstream: measured on an iPhone 16 Pro Max over USB on 5G, about 280 Mb/s down and 40 Mb/s up, with the phone's modem holding up to 0.8 s of upload in its queue (`networkQuality` gives 74 RPM while uploading). Claude Code sends the whole conversation on every turn, 1-2 MB in a long session, so with a few sessions open one session's upload sits in front of every other session's new request, of every ACK for a streaming answer and of every DNS lookup.

Turning **Hotspot turbo** on (the switch in the Stay Awake card, or `claude-acc hotspot on`) starts a small root daemon that wakes up only while the default route goes through an iPhone hotspot, over USB or Wi-Fi. It caps the Mac's upload with `ifconfig <interface> tbr` just under what the uplink carries right now, so the queue forms on the Mac, where macOS's FQ-CoDel sends small flows ahead of bulk uploads, instead of in the modem's FIFO. 5G capacity moves from minute to minute, so the cap follows round-trip probes the way [cake-autorate](https://github.com/lynxthecat/cake-autorate) does on OpenWrt routers: 20 small pings a second across six public resolvers. While the upload is busy and the probes stay clean, the cap rises by about a third per second. When every probe for 200 ms sits 30 ms above its baseline and the Mac's own upload is the cause, the cap drops by 10%, at most once a second. Single latency spikes from the radio don't count. If no probe comes back for 5 seconds, the cap is lifted rather than steered blind. The probes also keep the phone's radio connected, so the first request after a pause doesn't wait for the modem to wake.

Measured with three 40-second rounds off and three on, alternating, with other sessions' traffic going on as usual: ping p90 fell from 95 to 53 ms and p99 from 367 to 274 ms, a small request to api.anthropic.com went from 608 to 550 ms at p90, and a 2 MB upload got no slower (1.01 to 0.90 s at p50). It can't help the download direction, and the ~200 ms an API request spends in Anthropic's backend isn't the link's to win.

The daemon is a copy of `hotspot.py` in `/usr/local/libexec/claude-acc-hotspot`, owned by root. It reads the switch from `~/.local/share/claude-acc/hotspot.json`, where `min_mbps` and `max_mbps` can bound the cap (6 and 150 by default), and writes what it does to `hotspot-state.json` next to it every 2 seconds; its log is `/var/log/claude-acc-hotspot.log`. After an upgrade that changes it, `claude-acc hotspot status` says so and `claude-acc hotspot install` refreshes it.

## Load & heat

`fanctl` reads the SMC: every fan's speed, minimum, maximum and target, and the temperature sensors grouped by what they measure: P-cores (`Tp*`), E-cores (`Te*`), GPU (`Tg*`), SSD (`TH*`) and battery (`TB*`). The panel shows the hottest of each group, the fan speeds and a 20-minute chart of CPU and GPU temperature. Reading needs no root, so the card works without the daemon.

Above that sits the load, the way Activity Monitor counts it: CPU from the kernel's per-core tick counters (`host_processor_info`), split into performance and efficiency cores (Apple silicon numbers the efficiency cores first), and GPU from the `Device Utilization %` the GPU driver publishes in the I/O Registry. The app reads both every 3 seconds while the panel is open and once a minute otherwise, with nothing spawned and no root.

Setting the fans needs root, so `claude-acc fans install` puts `fanctl` in `/usr/local/libexec` (owned by root) and loads it as a LaunchDaemon. Every 2 seconds it follows the mode the panel writes to `~/.local/share/claude-acc/fans.json`:

- `{"mode": "auto"}` gives the fans back to macOS, `{"mode": "fixed", "percent": 50}` holds them at that share of the range between their minimum and maximum. Until you pick a mode, the daemon only reads.
- Above 95 °C on any CPU or GPU sensor it goes to full speed, and back to your setting below 85 °C.
- Another fan app (Mole, Macs Fan Control, TG Pro) wins: when the fans move to a speed the daemon didn't set, the panel says so and the daemon leaves them alone. It only takes the fans back after sleep, when macOS reset them to auto under a fixed setting.
- When the daemon stops, it gives the fans back to macOS, unless another app had them.
- It also holds `SleepDisabled` for **Stay Awake** with the lid closed (see Stay Awake).

## Ultra

A Mac running a dozen agents spends a surprising amount of its time on work nobody waits for. `perf.py` measured where it went on an M4 Max with Orca and nine Claude Code sessions, and Ultra turns on the fixes that paid off. Every tweak records what was there before, measures before and after, and `ultra off` restores it byte for byte, leaving alone anything you changed by hand in the meantime. Running `ultra on` twice is safe. launchd runs `perf.py keep` every 5 minutes, which reapplies tweaks to processes that restarted with a new pid and to `settings.json` when something rewrote it, fills in numbers that arrive later, and turns on what a newer version added to Ultra. A tweak you undid by hand with `perf undo` stays off until the next `ultra on`.

| Tweak | What it changes |
| --- | --- |
| `bg-helpers` | Background QoS (`PRIO_DARWIN_BG`: efficiency cores, throttled disk) for always-on helpers matched by `background` in `perf.json` |
| `claude-hooks-async` | `"async": true` on the hooks listed in `async_hooks`, in `~/.claude/settings.json`: a memory plugin's hooks and Orca's status hook on tool, prompt, stop and subagent events, which only report and print `{}`. Every tool call of every session waited for them. Orca's `SessionStart`, `SessionEnd` and `PermissionRequest` stay synchronous |
| `claude-hooks-native` | Hooks on native programs and without a shell: the guard's `devguard.py admit` becomes `claude-acc-hook`, which answers plain commands itself and hands the ones with a dev server or scheduler word to Python, and rtk's `rtk-rewrite.sh` becomes `rtk hook claude`. Both run in exec form (`command` is the program, `args` its arguments), and so does any other simple hook whose program is given by a path and starts by itself (`#!` or a binary), like your own Go hook. A program found on `PATH`, a variable in an argument, shell syntax and hooks another tweak changes stay in the shell. Only when the program is installed. The rtk script stays untouched, because rtk checks its hash |
| `node-compile-cache` | `NODE_COMPILE_CACHE` in the `env` of `~/.claude/settings.json`, so tsc, eslint, MCP servers and hooks start from a warm V8 cache. The `claude` binary itself runs on Bun and doesn't need it |
| `devguard-budget`, `devguard-max-server` | The guard's `budget_percent` from 35 to 25 and `max_server_gb` from 5 to 4 |
| `git-speed` | `core.untrackedCache`, `core.fsmonitor` and `git maintenance` (launchd: hourly prefetch, commit-graph and loose objects, daily incremental repack) in the repos listed in `git_repos` (empty by default), plus `git_global` in `~/.gitconfig`: parallel checkout (`checkout.workers=0`, `git worktree add` in an 18,000-file repo 2.0 s to 1.55 s) and `fetch.writeCommitGraph`. Set `git_maintenance` to `false` to leave maintenance out. A repo already in maintenance stays there after `ultra off` |
| `claude-ui` | `prefersReducedMotion: true` and `spinnerTipsEnabled: false` in `~/.claude/settings.json` (keys from `claude_ui`): fewer spinner, shimmer and flash frames to repaint when many sessions share one terminal window |
| `subagent-cache-1h` | `subagentPromptCacheTtl: "1h"` in `~/.claude/settings.json`. With the 5-minute default, 47% of subagent cache writes re-wrote a 300-900k context after a 5-60 minute pause |
| `tether-profile` | While the default route goes through a phone (iPhone USB, Bluetooth PAN, Personal Hotspot), `tether_env` in the `env` of `~/.claude/settings.json`: no auto-updater download (~236 MB a release) and no background prompt suggestions or away summaries. Removed again on a cable or regular Wi-Fi; scheduled `updates` runs wait for a regular link too |
| `rg-threads` | A `ripgreprc` with `--threads=4` and `RIPGREP_CONFIG_PATH` for Claude Code sessions: on macOS 16 threads fight over kernel locks walking a tree (428 → 179 ms in a 17.7k-file repo). An explicit `-j` still wins |

Claude Code runs every hook of an event in parallel and waits for the slowest, and a shell script with `jq` pays 50-80 ms in process starts on every tool call of every session. `claude-acc perf bench agents` lists the hooks that cost the most. For a hook of your own that shows up there, a small compiled program (Go, Swift) answers in about 5 ms, and `"async": true` removes the wait for a hook whose output nobody reads. `sh -c` itself costs 3-4 ms per hook on a loaded Mac; exec form (Claude Code 2.1.139 and later) skips it, and `claude-hooks-native` moves simple hooks there. Running sessions keep the hook commands they started with: the change shows in new and resumed sessions.

<a id="silent-hooks"></a>**The ranking misses silent hooks.** Claude Code writes a hook's run time to the transcript (`durationMs` in a `hook_success` entry) only when the hook printed something, so a hook that stays quiet never shows up, however slow. On 2026-10-09 that hid a plugin's Python hooks that cost about 51 ms before and 53 ms after every tool call, and security-guidance. Claude Code 2.1.294 keeps no other local record of each hook's time: `stop_hook_summary` lists every `Stop` hook but times only those with output, `lastSessionMetrics` in `~/.claude.json` holds one figure for all hooks of a project's last session, debug logs are off unless a session starts with `--debug` and give no time per command hook, and OpenTelemetry's `hook_execution_complete` times all matching hooks of one event together (`PostToolUse:Read`), needs telemetry on in every session plus an OTLP receiver (there is no file exporter), and names the hooks only under detailed beta tracing. To check a quiet hook, time it by hand: start its command with a sample event on stdin, the way Claude Code does.

Docker's VM is left out of Ultra because the cap applies only after a Docker restart: `claude-acc perf apply docker-vm` writes `MemoryMiB` 6144 while Docker is closed (the VM held 8 GB for 3.7 GB of containers), and every container with a restart policy comes back on its own.

`claude-acc perf apply docker-idle` (also outside Ultra) stops a Docker project, a compose project or a lone container, when nothing on the Mac has connected to its published ports or run `docker exec` in it (healthchecks don't count) for `docker_idle_hours` (2 by default, `docker_idle_keep` lists projects it never touches). Docker's Resource Saver then pauses the VM once nothing runs. The idle clock starts when the tweak first sees a project, `docker compose up -d` or `docker start` brings it back, and `perf undo docker-idle` starts exactly what it stopped.

Root tweaks sit next to Ultra in the panel, each with the command to copy:

- **A bigger vnode cache.** The kernel's file cache held 263,168 vnodes and recycled 28 million in 5 hours, so the metadata of a large `node_modules` (358,000 entries in a pnpm store) never stayed cached. `claude-acc perf-root vnodes trial` measures a second `lstat` pass before and after raising `kern.maxvnodes` to 786,432: 3.59 s to 2.51 s here, with 253,000 vnodes recycled per pass dropping to 4,500. `vnodes apply --persist` keeps it across reboots with a small LaunchDaemon. It costs about 1.2 KB of wired kernel memory per vnode, 0.63 GB in all, and it helps only metadata (`lstat`, `open`, lookups): file contents still leave the cache under memory pressure. The kernel never frees vnodes, so an undo stops the growth but gives the memory back only at the next reboot.
- **Spotlight for apps only.** `claude-acc perf-root spotlight apps-only` puts every home folder except `Applications`, plus `/Library`, `/opt`, `/usr/local` and `/Users/Shared`, on Spotlight's privacy list and rebuilds the index, so Spotlight stops chewing through package stores (half a million files under `~/Library/pnpm` and `~/go` here) and keeps finding apps. System Settings has no command line for that list and `mds` keeps it in memory, so the script writes `VolumeConfiguration.plist`, kills `mds` before it can write its old copy back, and lets launchd start it on the new list. `undo` restores the list you had.
- **More memory for the GPU.** macOS lets Metal wire about two thirds of RAM (37.4 GiB of 48 GB here), so a local model that would fit spills off the GPU. `claude-acc perf-root iogpu set` raises `iogpu.wired_limit_mb` to RAM minus 8 GB for the system, at most 85% (40,960 MB on 48 GB; MLX then saw 42.9 GB instead of 40.2 GB and llama.cpp 40,960 MiB), and keeps it across reboots with a small LaunchDaemon. On 24 GB or less it changes nothing, because that would not beat the default. `iogpu status` shows the value and the proposal, `undo` goes back to the default 0. The sysctl is documented in mlx-lm's README (macOS 15 and later).

What was measured and left alone, in [`docs/perf-research.md`](docs/perf-research.md) (Polish): open-file and process limits (5% used), App Nap for Orca (it never naps), forced L4S (halved the upload here), an upload shaper (the router adds only 1-4 ms under load), MCP servers duplicated per session (2.3 GB across nine sessions, with no shared mode to switch to), and the Claude API connections (already reused).

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests
```

The tests run the real script end to end against fake `security`, `curl` and `claude` binaries put first on `PATH`, so they never touch your Keychain or your accounts. They cover logging in, switching during a 429, keeping MCP tokens, cancelling a login halfway, leaving an account whose token died, and when the limit pause starts and ends. `tests/test_hook.py` runs each pause hook the way `settings.json` gets it, through the shell command and, with `app/.build/release/claude-acc-hook` and `claude-acc-pause` built, through each native program in exec form, with JSON on stdin as Claude Code sends it, on a temporary `$HOME`, and installs and removes the hooks in a temporary `settings.json`. The memory brake tests (`tests/test_lastresort.py`) replay the readings of the 2026-10-08 freeze through the stage rules (tight by 17:26, brake by 17:31, emergency at least five minutes before the freeze, the calm minutes before it calm), pick victims from fake process tables (orphans, headless browsers, git, language servers, scheduler jobs with their resume command, agent trees only in an emergency, a runaway on any stage, never an agent, its MCP servers or a shell), and stop a real tree: a fake agent, its shell and a process with 200 MB of ballast that ignores SIGTERM, which dies after the grace while the agent and the shell are left alone. The scheduler tests classify heavy commands outside Go and JS and leave servers, REPLs and one-liners alone, learn from a command's signature and its family, run a script that calls the scheduler inside its own job without waiting twice, add `-I` to `git grep`, install the Codex hook next to others, and ask the real rtk that it leaves alone exactly what the scheduler wraps. The guard tests check the native front's gate against the real `devguard.py admit`: every dev server start and every command the scheduler takes passes it, words inside paths and other words stay quiet, and the Swift binary reads the pattern the way Python does.

The dictation tests are Swift (`cd app && swift test`): the text rules ported from the dyktuj mod with the same cases (the hallucination filter, the correction fuse, joining text at a UTF-16 cursor), speech detection, WAV and FLAC, pieces under Claude Code's paste fold, the right ⌘ on a Polish layout, and the race against a fallback model, a refused key and a server error against a scripted local Gateway. `DICTATION_LIVE=1 swift test` also sends the shipped recording through the real AI Gateway.

The janitor tests run the real script on a temporary `$HOME` with the real `lsof`: caches that go, caches kept because a file is open or a dev server works in the app, a shell prompt that doesn't block, protected paths, stale and active projects, an interrupted delete, and `caps` dropping the oldest snapshots. The `compress` tests run the real `afsctool`: old files get compressed, an appended file comes back on the next run, and open files, protected paths and the bundle of a running app stay as they are.

The performance tests run `perf.py` on a temporary `$HOME`: every tweak applies, records and undoes exactly, Ultra on and off restore `settings.json` and `devguard.json` byte for byte, a second `ultra on` changes nothing, a value someone changed after Ultra survives `ultra off`, a hook command someone changed after `claude-hooks-native` survives its undo, hooks it moved out of the shell in 1.7 move to exec form and still come back to their originals, a script without `#!`, a program from `PATH` or a variable in an argument stay in the shell, the pause hooks in exec form are never made async or wrapped, a tweak a newer version adds to Ultra is turned on by `keep` while one undone by hand is not, and Ultra and the pause hooks come off in either order without taking each other's entries along.

The update tests run `updates.py` on a temporary `$HOME` against fake `brew`, `npm`, `go`, `pip`, `uv`, `claude`, `npx` and `pkgutil` that keep the installed and newest versions in a JSON file: every package manager brought to the newest version, a pinned formula and an npm major pin held (with versions in registry order, which sorts wrong as text), a failing cask and npm package that don't stop the rest and get one notification, an npm package rolled back when its command stops working after npm blocked its install scripts, the 3-day interval and the next-night retry, Python packages upgraded together from wheels (with the user site on its own, a package installed from a folder left alone, a source-only release and one held lower by another package reported as held back), an upgrade that breaks `pip check` rolled back while a conflict from before the run is not, an upgrade that stops a package importing rolled back, a pinned package and a pinned dependency held, a new Playwright given its browsers, a failed `pip install`, every Python upgraded once and named, a newer python.org patch offered (and not a new minor, nor Homebrew's Python) and an unsigned installer thrown away, a uv Python that holds pip packages left on its patch, Claude Code and its plugins updated, plugins updated from their own project and never auto-confirmed, a hand-edited skill left alone, the native `claude` first on the `PATH`, a step from an older version dropped from the state, a dry run that changes nothing, a failed `brew update`, a second run waiting for the first, and `--only` leaving the schedule alone.

The guard tests check its decisions on a made-up picture of the Mac (bloated, busy, duplicate, orphaned, watched and loop-restarted servers, warning and critical pressure, sticky and growing swap) and the hook's reading of agent commands, Metro in every form among them, and that a killed process slow to exit is not reported as surviving. Then they start a fake `next dev` (Python with 48 MB of ballast, listening on a port) on a temporary `$HOME`, with `scope` limited to it so the real dev servers on the Mac stay invisible, and check that the guard sees its port and size, stops it, leaves it alone in `--dry-run` and `observe`, that the hook sends a second start to the running one, refuses a Metro under critical pressure with no server left and one the guard just stopped for memory, and that `guard room` lets it through once memory is back.

The shared MCP tests (`tests/test_mcpshare.py`) run the bridge on a random port with a fake stdio server: the token, `Host` and `Origin` guards, one `initialize` for two clients, two sessions sending the same id at once, progress over SSE under the client's own token, a server request refused instead of routed, a crash and restart, sessions and DELETE, the 2026-07-28 era with its list cache fields and `subscriptions/listen`, and `share`/`unshare` on a temporary `.claude.json` through fake `launchctl` (which starts the real `serve` from the plist) and `claude mcp`, including a server that never starts and an entry someone else changed in the meantime.

The mail gateway tests (`tests/test_mail.py`) need no network: the MCP handshake, version negotiation, tool calls and errors over stdio; levels, the send switch and the audit log; the envelope, invisible characters and HTML; Gmail's MIME tree and threaded drafts; Gmail queries turned into IMAP `SEARCH`; the IMAP provider against a fake `imaplib` (search, read, attachment to quarantine, archive, draft, the connection pool and a sign-in that times out); a Gmail mailbox on a token command (scopes from tokeninfo, a failing command); and the approval flag on `mail_send` following the send modes. The SigV4 signer is checked against the example in AWS's documentation.

The API credits tests (`tests/test_credits.py`) run `credits.py` on a temporary `$HOME` against fake `security`, `osascript` (the hidden dialog) and `curl` (the models endpoint with its organization header). They check that a pasted key, classic or user-linked, reaches only the Keychain, never a process argument, a file or the output; that `--from-keychain` copies a key from another item without a dialog and with the key in no argument, stores nothing when the source is missing, not a key, an admin key or refused by the API, and replaces the key of an account already stored; that a key from another organization, an admin key, a key without a workspace and a cancelled dialog store nothing; that the balance counts only the current cycle and starts again from a Console reading; that 30 parallel `record` calls all land; which organization `exec` picks, that `--org` and a client scope never fall back, and what the command gets on stdin, in its environment and as its exit code; 75 before a start without headroom and 76 when *credit balance is too low* arrives mid-run, split across two writes; the `--no-env` run whose key comes only from `credits helper`; and that the guard stops an agent from reading the keys. They also check `helper --run`, which records the paying organization for a run without changing the log line and gives no key once the run is over or its owner died, and a plan end: the anniversary grants as before without one, nothing is left after it (and `exec --no-env` exits 75 with its usual message), a Console reading after the end counts, `none` lifts it, and a plan ending before its cycle reset is spent first.

The run environment tests (`tests/test_runenv.py`) start `credits run` and `runenv.prepare` on a temporary `$HOME` with fake `security` (which logs every argument and stdin), `claude-acc` (`status --json` and `token --json`) and a fake Claude Code installed as the pinned version, which pays the way the real one does (the helper from the config directory's settings, that directory's Keychain entry, or the base one, here the work account) and reports each request over OpenTelemetry. They check who pays (credit only with the budget × 1.5 plus the reserve, the subscription tail, never the work account, a `last_resort` or the active account, never a fallback token, 75 with a reason and no request when nothing fits); the settings, the hook and the headless `CLAUDE.md`; a child that dropped or swapped `CLAUDE_CONFIG_DIR` paying with the run anyway, a `claude` started by absolute path outside the run and a session written outside it failing the payer check with 78; nested sessions metered and booked per session; incomplete metering never verifying a payer; a billing error ending in 76 with the organization marked empty, a 429 making later runs avoid the account and a 401 changing nothing; a token that never reaches an argument, a file or the output and has no refresh token; the restricted pipeline that pays and is metered only with the job settings merged; two runs of one purpose staying apart; a resent batch and a second `finish` counting once; leftover processes stopped; a sweep after the owner was killed booking once and leaving a live run alone; the pinned version and the canary; and the guard denying key and login reads while the helper still pays. They also check the stops mid-run (1.5 × the budget before the next session starts, a wrong payer after an earlier 429, a command deaf to SIGTERM stopped as a tree, an exception that still removes the login), parallel credit runs holding their stop threshold, a long `--awake-minutes` refused without asking for a token, project settings with their own login refused, a failing login check leaving no Keychain entry, `Monitor` denied and guarded, a guard that blocks when it can't load, every `credits` command but `status` denied with no pointer to `exec`, your interactive session in the run's directory not counting as a leak, a swept run's session left unverified, and a swept session booked at its last request.

The browser gateway tests (`tests/test_browser.py`) check site levels in both modes and URL handling, `read_page` built from a recorded accessibility tree (refs that stay, visibility, `interactive`, depth, `ref`, frames, table rows, `find`), the result text (navigation line, Tab Context once, state changes, the envelope), the WebSocket client against a local server (handshake without `Origin`, ping, fragmented and 64-bit frames), the MCP toolset and its approval flags in both modes, the prompt hint and the guard's browser rules. Then they run a real Chrome without a window at device scale 2, like a Retina screen, on a temporary profile, with a local page that has a frame from another origin, a popup link, a confirm and a file input. Through the daemon's socket they type, pick an option, click inside both frames, take a screenshot, open the popup as a new tab, answer the confirm, stop a link to a denied host without leaving the page, refuse an upload from `~/.ssh`, and check that typed text never reaches the audit log. They also check that a button in the far corner of the viewport is in `read_page`, that the daemon still accepts sessions after a quiet second, that two agent sessions calling the command line at once (with the daemon started by the first call) keep their own tabs, that a finished session's tabs close, and that the daemon reconnects after the browser restarts. Keys come out as real keyboard events, with a chord's modifiers pressed first and `hold_key "shift"` holding Shift, and a live session keeps its tabs through a long pause while tabs nobody holds go after the usual idle time. With `anthropic` installed, the Python SDK driver runs the same tab through the SDK's own `tool_result`.

The desktop gateway tests (`tests/test_desktop.py`) need no real screen or input: a fake helper stands in for the native binary and records what it was sent, so they check the config and both modes, the 17-member tool list with the approval flag on `type`, `key` and `hold_key` in guarded mode only, the model-to-point coordinate mapping after a screenshot (clicks, drags, zoom regions, and the pixel a cursor position renders back to), the acknowledgement text each member returns, the scroll direction vectors, a protected app refusing both a screenshot and input while an ordinary app does not, the audit line that carries a typed string's length but never the string, the MCP handshake and a tool call over stdio, the prompt hint firing on desktop phrases and not on web ones, and the browser auto-allow (off by default, pressing only a single real dialog, the worker stopping on its event). A gated live smoke test (`CLAUDE_ACC_DESKTOP_LIVE=1`, with the signed helper built and its permissions granted) opens a new untitled TextEdit document, types into it, reads the text back, selects all, reads the frontmost app, takes a screenshot, and closes the document it opened.

To see the panel without clicking the menu bar, render it to a PNG, from live data or from a JSON file:

```sh
"$HOME/Applications/Claude Acc.app/Contents/MacOS/ClaudeAcc" --render panel.png --snapshot docs/demo-snapshot.json
```

With `--snapshot` the clock stops at the moment the snapshot was taken, and `demo-guard.json`, `demo-janitor.json`, `demo-fans.json`, `demo-sched.json`, `demo-depot.json`, `demo-updates.json` and `demo-desktop.json` next to it stand in for the guard, cleanup, fan, build scheduler, Depot CI, update and desktop gateway state. `docs/demo-snapshot-pause.json` is the same panel during a limit pause. `--open <account id>` renders that account opened. `--hover <account id>` renders that row as if the pointer were over it. Lists that scroll in the panel come out in full.

## Caveats

- It relies on undocumented endpoints and on Claude Code's own OAuth client, so any Claude Code update can break it.
- This project is not affiliated with Anthropic. Section 3.7 of Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) prohibits accessing the services "through automated or non-human means, whether through a bot, script, or otherwise" outside the API, and this tool polls those endpoints from a script. Read the terms and decide for yourself.
- The renewal date is the monthly anniversary of the subscription start. The API doesn't expose the billing date, so it's wrong for yearly plans.

## License

MIT
