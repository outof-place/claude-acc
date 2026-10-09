"""The IDE that hosts claude-acc: Orca, or Pod (outofplace's downstream of Orca), in one place.

Everything claude-acc knows about the host app lives here, so the rest asks this module instead of
spelling Orca's paths: the userData folder (claude-accounts, orca-data.json, orca-runtime.json), the app
bundle and its main process, its CLI, the Keychain service of its Claude accounts and the folder of its
agent hooks. The env keys the host sets in its terminals (ORCA_PANE_KEY, ORCA_TERMINAL_HANDLE, ...) are
constants: Pod keeps Orca's names, its CLI and hooks speak them.

Which host, first match wins:
1. explicit: CLAUDE_ACC_HOST=orca|pod pins one; CLAUDE_ACC_HOST_APP (or POD_APP_PATH) names the app
   bundle, CLAUDE_ACC_HOST_DATA its userData folder;
2. Pod (Pod.app in /Applications or ~/Applications) when it runs, or when Orca doesn't run and Pod
   already has a userData folder (or Orca has none);
3. Orca's defaults: exactly the paths claude-acc used before this module.
"Runs" is Electron's SingletonLock in the userData folder (a link to "<host>-<pid>") with a live pid,
so the check spawns nothing.

ORCA_USER_DATA_PATH, which Orca and Pod set in their agent sessions, names the instance a session
belongs to (a dev build has its own). Only the runtime socket follows it, as devguard did before:
accounts and settings stay with the host the launchd jobs see, so an agent in a dev instance and the
tick never disagree about them.

Pod's identity comes from its bundle: Info.plist (CFBundleName, CFBundleExecutable,
CFBundleIdentifier) and, when the app isn't packed into app.asar, Resources/app/package.json
(Electron's userData folder is Application Support/<productName or name>; packed, the lower-case
bundle name, which is how Orca's "orca" comes about). What a bundle can't tell, Pod may declare in
Info.plist under `ClaudeAccHost` (electron-builder `extendInfo`): userData, cli, keychainService,
hooksDir, dataFile, runtimeFile. Without it Pod gets Orca's values, which a fork keeps unless it
renames them.

    orcahost.py [field]     the resolved host as JSON, or one field (perf-root.sh asks for `app`)
"""

import json
import os
import re
import sys
import time
from collections import namedtuple

# what the host sets in its terminals and agent sessions; Pod keeps Orca's names
PANE_ENV = "ORCA_PANE_KEY"
TERMINAL_ENV = "ORCA_TERMINAL_HANDLE"
USER_DATA_ENV = "ORCA_USER_DATA_PATH"
# a remote host (pairing, environment): its CLI knows the way, the local socket doesn't
REMOTE_ENV = ("ORCA_PAIRING_CODE", "ORCA_REMOTE_PAIRING", "ORCA_ENVIRONMENT")
ENV_PREFIX = "ORCA_"

PIN_ENV = "CLAUDE_ACC_HOST"
APP_ENV = ("CLAUDE_ACC_HOST_APP", "POD_APP_PATH")
DATA_ENV = "CLAUDE_ACC_HOST_DATA"

ORCA_BUNDLE_ID = "com.stablyai.orca"
# Pod's app name until the naming kit lands; a bundle elsewhere comes in through CLAUDE_ACC_HOST_APP
POD_NAME = "Pod"
# the hosts' own CLIs, for code that must recognise them without reading a bundle
CLI_NAMES = ("orca", "pod")

Host = namedtuple(
    "Host",
    "kind name app executable bundle_id user_data cli keychain_service hooks data_file runtime_file",
)
Host.__doc__ = """One host app. `hooks` is its agent-hooks folder relative to HOME (".orca/agent-hooks"),
which is how its hook commands show up in ~/.claude/settings.json."""


def _home(home=None):
    return home or os.path.expanduser("~")


def _app_support(home, folder):
    return os.path.join(_home(home), "Library", "Application Support", folder)


def _expand(path, home=None):
    return os.path.join(_home(home), path[2:]) if path.startswith("~/") else path


def bundle_name(app):
    """"Orca" for /Applications/Orca.app: how ps, Gatekeeper and TCC name the app."""
    name = os.path.basename(app.rstrip("/"))
    return name[: -len(".app")] if name.endswith(".app") else name


def orca(home=None):
    """Orca's defaults."""
    return Host(
        kind="orca",
        name="Orca",
        app="/Applications/Orca.app",
        executable="Orca",
        bundle_id=ORCA_BUNDLE_ID,
        user_data=_app_support(home, "orca"),
        cli="orca",
        keychain_service="Orca Claude Code Managed Credentials",
        hooks=".orca/agent-hooks",
        data_file="orca-data.json",
        runtime_file="orca-runtime.json",
    )


def pod_apps(home=None):
    """Where an installed Pod would be, in the order Launch Services prefers."""
    return ["/Applications/%s.app" % POD_NAME, os.path.join(_home(home), "Applications", POD_NAME + ".app")]


_PLIST_KEYS = ("CFBundleName", "CFBundleExecutable", "CFBundleIdentifier")
# the tags that matter in an XML plist: containers (for the depth) and a key with its string, if any;
# compiled on first use, since the scheduler's hook imports this module for constants only
_PLIST_TOKEN = r"<(/?)(dict|array)\s*>|<key>([^<]*)</key>\s*(?:<string>([^<]*)</string>)?"
_XML_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&"))


def _unescape(text):
    for entity, char in _XML_ENTITIES:
        text = text.replace(entity, char)
    return text


def read_info(app):
    """The Info.plist keys from_bundle uses. An XML plist (what electron-builder writes) goes through
    a small tag scanner: plistlib with expat costs 6-12 ms per start, and every claude-acc process may
    ask. A binary plist goes through plistlib."""
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    if raw.lstrip().startswith(b"<"):
        return _scan_plist(raw.decode("utf-8", "replace"))
    import plistlib

    try:
        info = plistlib.loads(raw)
    except Exception:  # a broken plist means no identity, not a crash
        return {}
    return info if isinstance(info, dict) else {}


def _scan_plist(text):
    """Top-level strings from _PLIST_KEYS and the ClaudeAccHost dict, by depth: the root dict is 1."""
    info, declared, depth, inside, previous = {}, {}, 0, False, None
    for match in re.finditer(_PLIST_TOKEN, text):
        close, container, key, value = match.groups()
        if container:
            depth += -1 if close else 1
            if not close and depth == 2 and container == "dict" and previous == "ClaudeAccHost":
                inside = True
            elif close and depth < 2:
                inside = False
            previous = None
            continue
        previous = key if value is None else None
        if value is None:
            continue
        if depth == 1 and key in _PLIST_KEYS:
            info[key] = _unescape(value)
        elif inside and depth == 2:
            declared[key] = _unescape(value)
    if declared:
        info["ClaudeAccHost"] = declared
    return info


def from_bundle(app, home=None):
    """The host an app bundle describes. Orca's own bundle gives exactly orca()."""
    base = orca(home)
    app = os.path.abspath(os.path.expanduser(app)).rstrip("/")
    info = read_info(app)
    declared = info.get("ClaudeAccHost")
    declared = declared if isinstance(declared, dict) else {}
    name = info.get("CFBundleName") or bundle_name(app) or POD_NAME
    bundle_id = info.get("CFBundleIdentifier") or ""
    try:
        with open(os.path.join(app, "Contents", "Resources", "app", "package.json")) as f:
            package = json.load(f)
    except (OSError, ValueError):
        package = {}
    folder = package.get("productName") or package.get("name") if isinstance(package, dict) else None
    user_data = declared.get("userData")
    user_data = _expand(user_data, home) if user_data else _app_support(home, folder or name.lower())
    hooks = declared.get("hooksDir") or base.hooks
    if hooks.startswith("~/"):
        hooks = hooks[2:]
    return Host(
        kind="orca" if bundle_id == ORCA_BUNDLE_ID else "pod",
        name=name,
        app=app,
        executable=info.get("CFBundleExecutable") or name,
        bundle_id=bundle_id,
        user_data=user_data,
        cli=declared.get("cli") or _bundled_cli(app) or base.cli,
        keychain_service=declared.get("keychainService") or base.keychain_service,
        hooks=hooks.rstrip("/"),
        data_file=declared.get("dataFile") or base.data_file,
        runtime_file=declared.get("runtimeFile") or base.runtime_file,
    )


def _bundled_cli(app):
    """The CLI the bundle carries in Resources/bin (Orca's is `orca`), by name."""
    folder = os.path.join(app, "Contents", "Resources", "bin")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    return next((n for n in names if not n.startswith(".") and os.access(os.path.join(folder, n), os.X_OK)), None)


def running(host):
    """The host runs: Electron's SingletonLock points at a live pid."""
    try:
        target = os.readlink(os.path.join(host.user_data, "SingletonLock"))
    except OSError:
        return False
    pid = target.rpartition("-")[2]
    if not pid.isdigit() or int(pid) <= 1:
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def installed_pod(home=None):
    """The first Pod bundle on disk, or None."""
    return next((from_bundle(p, home) for p in pod_apps(home) if os.path.isdir(p)), None)


def resolve(env=None, home=None):
    """The host by the rules in the module docstring."""
    env = os.environ if env is None else env
    base = orca(home)
    pin = env.get(PIN_ENV, "").strip().lower()
    app = next((env[k] for k in APP_ENV if env.get(k)), None)
    if pin == "orca":
        chosen = base
    elif app:
        chosen = from_bundle(app, home)
    else:
        pod = installed_pod(home)
        if pod is None:
            chosen = base
        elif pin == "pod" or running(pod):
            chosen = pod
        elif running(base):
            chosen = base
        elif os.path.isdir(pod.user_data) or not os.path.isdir(base.user_data):
            chosen = pod
        else:
            chosen = base
    data = env.get(DATA_ENV)
    if data:
        chosen = chosen._replace(user_data=_expand(data, home))
    return chosen


_cache = [0.0, None]


def host(ttl=30.0):
    """The resolved host, cached for `ttl` seconds: long-running loops follow a switch to Pod."""
    now = time.monotonic()
    if _cache[1] is None or now - _cache[0] > ttl:
        _cache[:] = [now, resolve()]
    return _cache[1]


def known(env=None, home=None):
    """Every host that may be on this Mac: the resolved one first, then Orca and an installed Pod.
    What protects (processes never killed, hooks, Keychain entries agents may not read) covers all."""
    hosts = [resolve(env, home), orca(home)]
    pod = installed_pod(home)
    if pod:
        hosts.append(pod)
    unique = []
    for h in hosts:
        if h not in unique:
            unique.append(h)
    return unique


def app_names(env=None):
    """Bundle names of the hosts, without touching the disk: Orca, Pod and an app named in env."""
    env = os.environ if env is None else env
    names = ["Orca", POD_NAME]
    for key in APP_ENV:
        if env.get(key):
            name = bundle_name(env[key])
            if name and name not in names:
                names.append(name)
    return names


def app_pattern(env=None):
    """Regex fragment for a command inside a host bundle (main process, helpers, CLI)."""
    return "|".join(r"(?<![\w-])%s\.app" % re.escape(n) for n in app_names(env))


# built at import from constants and env only: hooks and the guard compile it without disk reads
APPS = app_pattern()


def main_path(h=None):
    h = h or host()
    return os.path.join(h.app, "Contents", "MacOS", h.executable)


def main_marker(h=None):
    """What the host's main process has in its command: "Orca.app/Contents/MacOS/Orca"."""
    h = h or host()
    return "%s/Contents/MacOS/%s" % (os.path.basename(h.app), h.executable)


def runtime_path(h=None, env=None):
    """orca-runtime.json of the instance this process belongs to (ORCA_USER_DATA_PATH), else the host's."""
    h = h or host()
    env = os.environ if env is None else env
    return os.path.join(env.get(USER_DATA_ENV) or h.user_data, h.runtime_file)


def cli_path(h=None, which=None):
    """The host's CLI: Orca's from PATH, as before; Pod's from its bundle, then PATH."""
    import shutil

    h = h or host()
    which = which or shutil.which
    if h.kind != "orca":
        bundled = os.path.join(h.app, "Contents", "Resources", "bin", h.cli)
        if os.access(bundled, os.X_OK):
            return bundled
    return which(h.cli)


def keychain_services(env=None, home=None):
    return list(dict.fromkeys(h.keychain_service for h in known(env, home)))


def hook_dirs(env=None, home=None):
    return list(dict.fromkeys(h.hooks for h in known(env, home)))


def main(argv):
    h = resolve()
    fields = dict(h._asdict(), main=main_path(h), runtime=runtime_path(h), running=running(h))
    if argv:
        if argv[0] not in fields:
            print("orcahost.py [%s]" % "|".join(fields), file=sys.stderr)
            return 2
        print(fields[argv[0]])
        return 0
    print(json.dumps(fields, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
