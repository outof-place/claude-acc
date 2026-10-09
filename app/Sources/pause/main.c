// claude-acc-pause: the limit pause hooks of hook.py, which Claude Code runs without a shell
// (exec form) after every tool call of every session.
//
// Outside a pause the answer is "nothing": two file checks and the event drained. During one,
// hook.py gets the event untouched on stdin, through acc.py on the managed interpreter setup.sh
// links (20 ms a call against 30 ms through /usr/bin/python3, 8.10, loaded Mac), or without
// them through the same /usr/bin/python3 the shell guard uses. It is plain C because the Swift front (`claude-acc-hook pause`) spends about 2 ms
// loading its runtime and Foundation before the first check: 5.0 ms against 3.1 ms here, and
// 2.8 ms for /usr/bin/true (6.10, loaded Mac). The checks match the shell guard hook.py
// installs when no native program is there: CLAUDE_ACC_PAUSE_FILE wins, like in the session.
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 2) return 0;
    // HOME first, like the Python scripts' expanduser: tests point it at a scratch folder
    const char *home = getenv("HOME");
    if (!home || !*home) {
        struct passwd *pw = getpwuid(getuid());
        home = pw ? pw->pw_dir : "";
    }
    char pause[4096], script[4096], python[4096], launcher[4096];
    const char *override = getenv("CLAUDE_ACC_PAUSE_FILE");
    if (override && *override)
        snprintf(pause, sizeof pause, "%s", override);
    else
        snprintf(pause, sizeof pause, "%s/.local/share/claude-acc/pause.json", home);
    snprintf(script, sizeof script, "%s/.local/share/claude-acc/hook.py", home);
    if (access(pause, F_OK) == 0 && access(script, F_OK) == 0) {
        snprintf(python, sizeof python, "%s/.local/share/claude-acc/python", home);
        snprintf(launcher, sizeof launcher, "%s/.local/share/claude-acc/acc.py", home);
        if (access(python, X_OK) == 0 && access(launcher, F_OK) == 0)
            execl(python, python, launcher, "hook", argv[1], (char *)NULL);
        execl("/usr/bin/python3", "/usr/bin/python3", script, argv[1], (char *)NULL);
    }
    // the shell's `cat >/dev/null`: Claude Code's write of the event never meets a closed pipe
    char buf[65536];
    while (read(STDIN_FILENO, buf, sizeof buf) > 0) {
    }
    return 0;
}
