"""PostToolUse hook on Edit/Write/MultiEdit for the Ultra tweak `security-slim`: the pattern
warnings of security-guidance@claude-plugins-official, without the rest of that plugin.

claude-acc ships only this runner. `perf.py` copies `patterns.py` (the rules and the warning
texts) from the security-guidance the user has installed and writes `source.py` next to it
(its version and provenance tag), so the warnings match that installed version.

Same rules, same warning text, same JSON on stdout, same once-per-session-per-file-and-rule
dedup: it shares the state files in ~/.claude/security with security-guidance, so a session
never gets the same warning from both. Left out: the LLM reviews (Stop, commit, push), the
UserPromptSubmit git baseline, the seven Bash hooks, SDK bootstrap, debug log, custom
security-patterns files.

One deliberate difference: when a Write matches, security-guidance drops rules the file
already matched at the git baseline it took on the last user prompt (`git stash create`).
This hook compares with HEAD instead, so a pattern that sat in uncommitted changes before
the prompt warns once.

Runs as `python -I -S`: no site packages and no script dir on sys.path, hence the explicit
path insert below. Python 3.9 and later.
"""

import os
import sys

# bytecode only in $STATE: next to a script in Pod's bundle (Pod.app/Contents/Resources/claude-acc) a
# __pycache__ breaks the app's seal, whatever starts this file and with whatever flags (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patterns import SECURITY_PATTERNS, _RULE_NAME_TO_ID, rule_names_to_mask  # noqa: E402
from source import PROVENANCE_TAG, PV  # noqa: E402

try:
    import fcntl
except ImportError:
    fcntl = None

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
GIT_CMD = [
    "git",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.quotePath=false",
    "-c", "diff.autoRefreshIndex=false",
]


# state shared with security-guidance

def state_dir():
    explicit = os.environ.get("SECURITY_WARNINGS_STATE_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    cc_config = os.environ.get("CLAUDE_CONFIG_DIR")
    if cc_config:
        return os.path.expanduser(os.path.join(cc_config, "security"))
    return os.path.expanduser("~/.claude/security")


def state_key(session_id):
    import re
    key = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID") or session_id
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(key))[:128]


def load_state(path):
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"shown_warnings": data}
        if isinstance(data, dict):
            data.setdefault("shown_warnings", [])
            return data
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return {"shown_warnings": []}


def first_time(session_id, warning_key):
    """True the first time this session sees warning_key, and marks it as shown.
    Fails open (True) when the state file can't be locked."""
    base = os.path.join(state_dir(), f"security_warnings_state_{state_key(session_id)}")
    try:
        os.makedirs(os.path.dirname(base), exist_ok=True)
    except OSError:
        pass
    lock_fd = None
    try:
        if fcntl is not None:
            lock_fd = os.open(base + ".lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = load_state(base + ".json")
        shown = state["shown_warnings"]
        if warning_key in shown:
            return False
        shown.append(warning_key)
        try:
            with open(base + ".json", "w") as f:
                json.dump(state, f)
        except OSError:
            pass
        return True
    except OSError:
        return True
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def cleanup_old_state_files():
    """Per-session state files older than 30 days go (security-guidance did this itself)."""
    import time
    cutoff = time.time() - 30 * 24 * 60 * 60
    d = state_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if name.startswith("security_warnings_state_") and name.endswith((".json", ".lock")):
            p = os.path.join(d, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass


# matching

def check_patterns(file_path, content):
    """[(rule name, warning)] of the rules that match this file and content, in rule order."""
    import re
    normalized_path = file_path.lstrip("/")
    matches = []
    for pattern in SECURITY_PATTERNS:
        if "path_filter" in pattern:
            try:
                if not pattern["path_filter"](normalized_path):
                    continue
            except Exception:
                continue
        matched = False
        if "path_check" in pattern:
            try:
                matched = bool(pattern["path_check"](normalized_path))
            except Exception:
                pass
        if not matched and content and "substrings" in pattern:
            matched = any(s in content for s in pattern["substrings"])
        if not matched and content and "regex" in pattern:
            try:
                matched = bool(re.search(pattern["regex"], content))
            except Exception:
                pass
        if matched:
            matches.append((pattern["ruleName"], pattern["reminder"]))
    return matches


def new_text(tool_name, tool_input):
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        return tool_input.get("new_string", "")
    if tool_name == "MultiEdit":
        return " ".join(edit.get("new_string", "") for edit in tool_input.get("edits", []))
    return ""


def head_text(file_path, cwd):
    """The file at HEAD, or None (not a repo, untracked, outside the repo)."""
    import subprocess
    try:
        rel_path = os.path.relpath(os.path.abspath(file_path), os.path.abspath(cwd) if cwd else os.getcwd())
    except ValueError:
        return None
    env = dict(os.environ)
    try:
        n = max(0, int(env.get("GIT_CONFIG_COUNT") or 0))
    except (TypeError, ValueError):
        n = 0
    for i, (k, v) in enumerate((("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null")), start=n):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    env["GIT_CONFIG_COUNT"] = str(n + 2)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run([*GIT_CMD, "show", f"HEAD:{rel_path}"],
                                cwd=cwd, env=env, capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode == 0:
        return (result.stdout or b"").decode("utf-8", errors="replace")
    return None


def emit(obj):
    print(json.dumps(obj), flush=True)


def metrics(**fields):
    """Metrics as security-guidance reports them, with its version as M*10000 + m*100 + p
    when known: after the fields on a match, first on a skip (same bytes as upstream)."""
    if PV:
        fields["pv"] = PV
    return fields


def skipped(reason):
    return dict({"pv": PV} if PV else {}, skipped=True, skip_reason=reason)


def main():
    disable = os.environ.get("SECURITY_GUIDANCE_DISABLE", "").strip().lower()
    if disable in ("1", "true", "yes", "on") or os.environ.get("ENABLE_SECURITY_REMINDER", "1") == "0":
        emit({"metrics": skipped(-1)})
        return

    if os.urandom(1)[0] < 26:  # about one run in ten
        cleanup_old_state_files()

    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "surrogateescape"))
    except ValueError:
        emit({"metrics": skipped(-2)})
        return

    tool_name = event.get("tool_name", "")
    if tool_name not in EDIT_TOOLS or event.get("hook_event_name", "") != "PostToolUse":
        return
    if os.environ.get("ENABLE_PATTERN_RULES", "1") == "0":
        return
    tool_input = event.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path or file_path.startswith(os.path.expanduser("~/.claude/plans")):
        return

    session_id = event.get("session_id", "default")
    raw = check_patterns(file_path, new_text(tool_name, tool_input))
    if not raw:
        return

    shown = raw
    if tool_name == "Write":
        before = head_text(file_path, os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
        if before is not None:
            had = {rule for rule, _ in check_patterns(file_path, before)}
            shown = [(rule, text) for rule, text in raw if rule not in had]

    guidance = [text for rule, text in shown if first_time(session_id, f"{file_path}-{rule}")]

    names = [rule for rule, _ in raw]
    output = {"metrics": metrics(
        pattern_hits=len(guidance),
        rule_id=int(_RULE_NAME_TO_ID.get(names[0], -1)),
        rule_mask=rule_names_to_mask(names),
    )}
    if guidance:
        output["hookSpecificOutput"] = {
            "hookEventName": "PostToolUse",
            "additionalContext": PROVENANCE_TAG + "\n\n" + "\n\n".join(guidance),
        }
    emit(output)


if __name__ == "__main__":
    main()
