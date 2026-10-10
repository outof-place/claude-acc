// pod-acc-run: the program of every claude-acc agent Pod registers with SMAppService
// (codes.pod.app.acc.*). launchd gives a bundled agent no templated paths, so this front:
//
// - takes HOME from the account database (getpwuid), never from the environment: an app started
//   with an isolated HOME must not point the account's agents at someone else's state;
// - appends stdout and stderr to $STATE/<--log NAME>, the file the legacy agent wrote;
// - execs $STATE/python $STATE/acc.py <job words>, the interpreter and launcher setup.sh installed.
//
// Before setup.sh ran there is nothing to start: exit 78 (EX_CONFIG), and launchd tries again on
// the agent's own schedule.
import Darwin
import PodAccRunCore

guard let entry = getpwuid(getuid()), let dir = entry.pointee.pw_dir else {
    fputs("pod-acc-run: no home directory for uid \(getuid())\n", stderr)
    exit(78)
}
let home = String(cString: dir)
let plan: PodAccRun.Plan
switch PodAccRun.plan(home: home, args: Array(CommandLine.arguments.dropFirst()), exists: { access($0, F_OK) == 0 }) {
case .success(let found):
    plan = found
case .failure(let failure):
    fputs(failure.message + "\n", stderr)
    exit(failure.exitCode)
}

let fd = open(plan.log, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o644)
if fd >= 0 {
    dup2(fd, STDOUT_FILENO)
    dup2(fd, STDERR_FILENO)
    close(fd)
}
setenv("HOME", home, 1)
var argv = plan.argv.map { strdup($0) } + [nil]
execv(plan.argv[0], &argv)
fputs("pod-acc-run: cannot start \(plan.argv[0]): \(String(cString: strerror(errno)))\n", stderr)
exit(71)
