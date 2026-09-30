# claude-acc

Keeps Claude Code working when you have more than one Claude subscription. A menu bar app shows the 5-hour and weekly usage of every account, and a background job moves your running Claude Code sessions to the account with the most headroom once the current one runs out. No restart, no `/login`, no terminal.

<img src="docs/panel.png" width="350" alt="Claude Acc panel: active account with 5-hour and weekly usage bars, the other accounts with their reset times, and a login button for an expired account">

The UI text and the code comments are in Polish.

## What it does

- **Menu bar ring** with the usage of the active account, for whichever window (5 hours or weekly) runs out first. It turns orange at 75% and red at 90%. An orange dot means some account needs to log in again.
- **Panel** with every account: 5-hour and weekly bars, when each window resets ("za 2h 5min", "za 4d 4h"), which account goes next, and when the subscription renews. For the active account it also shows when, at the current pace, the watcher will switch away from it.
- **Automatic switching** when the active account is down to 5% of the session or 3% of the week. It picks the account with the most weekly headroom and keeps accounts you mark as last resort (a company seat, say) for the end. An account whose subscription is canceled is skipped, and it comes back on its own once the subscription is renewed.
- **One-click switch** to any account. Running sessions keep working because Claude Code reads its credentials from the Keychain on the fly.
- **Log in again** for an account whose refresh token died. The button runs `claude auth login` in the background and opens the browser with the email pre-filled. Before saving anything, it asks the API which account you actually signed into, so a browser logged into the wrong account can't overwrite anything.

## How it works

A Claude Code account is the `claudeAiOauth` object inside a Keychain entry. Claude Code reads `Claude Code-credentials`, plus `Claude Code-credentials-<first 8 hex chars of sha256(config dir)>` when `CLAUDE_CONFIG_DIR` is set. [Orca](https://github.com/stablyai/orca) keeps a copy of every account you add to it under `Orca Claude Code Managed Credentials`. Switching accounts means copying one account's `claudeAiOauth` into the entries the live sessions read.

- `accswitch.py` holds all the logic. launchd runs `accswitch.py tick` every 2 minutes, with or without the app.
- The menu bar app (SwiftUI) is a thin UI. It runs `accswitch.py status --json` every minute and `switch` or `login` when you click.
- Usage comes from `GET https://api.anthropic.com/api/oauth/usage`, and the account behind a token from `/api/oauth/profile`.

## How it avoids logging you out

Each of these rules comes from an account that actually lost its login while the tool was being built:

- Refreshing a token rotates it, and presenting a refresh token that was already used got the whole account logged out. Claude Code sessions refresh the active account on their own, about 5 minutes before the token expires, so the watcher never refreshes the active account while a session could. It copies the pair the session wrote instead, from whichever Keychain entry the sessions use (`Claude Code-credentials` without `CLAUDE_CONFIG_DIR`, the hashed one with it). It refreshes the active account itself only once the token has been expired for 15 minutes, when no session is running.
- An inactive account whose token no session holds is refreshed by whichever process reads it first (the watcher, a click, or the panel), one process at a time behind a file lock. The panel never refreshes a token that a session in any config directory may hold.
- It never writes a token it hasn't checked against the API first. A future expiry date doesn't prove the token still works.
- It replaces only `claudeAiOauth`. The same Keychain entry holds `mcpOAuth`, the tokens of your MCP servers, which belong to the config directory and survive every switch.
- A 429 from the usage endpoint means "unknown", never "dead". It backs off for 2 minutes, then 4, 8 and 15 while the endpoint keeps refusing, starts over after the first good answer, and doesn't refresh or flag anything in the meantime. Claude Code and Orca poll the same endpoint too, so it can throttle even while this tool is quiet. Whether a token works is checked against the profile endpoint, so switching still works while the usage endpoint is throttled. Accounts with a canceled subscription get a 403 there, so they aren't asked at all.
- Before overwriting the live entry it copies the token there back to its Orca copy, because the running session may have rotated it since the last switch.

## Requirements

- macOS 14 or newer, with Swift 6 (Xcode or the command line tools) to build the app.
- `/usr/bin/python3` (ships with the command line tools).
- Claude Code. Tested with 2.1.284.
- Orca with your Claude accounts added as managed accounts, and **System default** selected as the active Claude account in Orca. With a managed account selected, Orca puts its own account back whenever a terminal starts and every 15 minutes, undoing every switch, and it refreshes that account's token itself. claude-acc reads Orca's settings, and while an account is selected there the watcher stands down, switching is blocked and the panel tells you to pick System default.

## Install

```sh
git clone https://github.com/outofplace-space/claude-acc.git
cd claude-acc
./install.sh
```

The installer copies the script to `~/.local/share/claude-acc`, adds a `claude-acc` command to `~/.local/bin`, loads the launchd job, then builds and opens `~/Applications/Claude Acc.app`. The app adds itself to your login items on first run. You can turn that off in the panel.

## Command line

| Command | What it does |
| --- | --- |
| `claude-acc status` | Usage of every account and the switching order |
| `claude-acc who` | The active account, its burn rate and when it will hit the wall |
| `claude-acc switch <email>` | Switch to that account |
| `claude-acc switch --auto` | Switch to the next account in line |
| `claude-acc login <email>` | Log the account in again in the browser |
| `claude-acc heal [--deep]` | Recover accounts whose Orca copy died after a rotation elsewhere |
| `claude-acc tick` | One watcher pass (what launchd runs) |
| `claude-acc depot [--force]` | Which account the Depot sandboxes run on; `--force` sends its token again |
| `claude-acc depot --fallback` | Store a long-lived `claude setup-token` token for when no account has headroom |

## Configuration

`~/.local/share/claude-acc/config.json`. Every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `hard_session_left` | `5` | Switch when the active account has this % of the 5-hour window left |
| `hard_weekly_left` | `3` | Switch when it has this % of the week left |
| `min_session_left` / `min_weekly_left` | `15` / `8` | An account needs at least this much to be switched to |
| `last_resort` | `[]` | Emails used only when nothing else has headroom |
| `never` | `[]` | Emails never switched to |
| `config_dir` | `~/.claude` | The config directory whose sessions get switched |
| `other_config_dirs` | `[]` | Other config directories whose entries get the new token when an account refreshes, but are never switched |
| `depot_sync` | `true` | Keep the `CLAUDE_CODE_OAUTH_TOKEN` secret of `depot claude` sandboxes on an account with headroom |
| `depot_min_valid_hours` | `4` | A token sent to Depot must stay valid at least this long; a shorter one is refreshed first |
| `depot_bin` | `""` | Path of the Depot CLI; empty means `PATH`, then Homebrew |

## Depot sandboxes

[`depot claude`](https://depot.dev/docs/agents/claude-code/quickstart) starts Claude Code in a remote sandbox with the token stored in the organization secret `CLAUDE_CODE_OAUTH_TOKEN`. Every tick keeps that secret on the account first in the switching order **other than the local one**, so the laptop and the sandboxes never burn the same account. The token is sent again only when that account runs out of headroom, becomes the local account, or has less than `depot_min_valid_hours` of validity left; an idle account's token is refreshed before it is sent, the active account's never (its sessions own that refresh). With no other account left, the long-lived token from `claude-acc depot --fallback` goes out instead. A Depot failure is logged and never stops the switching. Requires the Depot CLI logged in to the organization (`depot login`).

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests
```

The tests run the real script end to end against fake `security`, `curl` and `claude` binaries put first on `PATH`, so they never touch your Keychain or your accounts. They cover logging in, switching during a 429, keeping MCP tokens, cancelling a login halfway, and leaving an account whose token died.

To see the panel without clicking the menu bar, render it to a PNG, from live data or from a JSON file:

```sh
"$HOME/Applications/Claude Acc.app/Contents/MacOS/ClaudeAcc" --render panel.png --snapshot docs/demo-snapshot.json
```

## Caveats

- It relies on undocumented endpoints and on Claude Code's own OAuth client, so any Claude Code update can break it.
- This project is not affiliated with Anthropic. Section 3.7 of Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) prohibits accessing the services "through automated or non-human means, whether through a bot, script, or otherwise" outside the API, and this tool polls those endpoints from a script. Read the terms and decide for yourself.
- The renewal date is the monthly anniversary of the subscription start. The API doesn't expose the billing date, so it's wrong for yearly plans.

## License

MIT
