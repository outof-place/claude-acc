import Testing
@testable import PodAccRunCore

// pod-acc-run's arguments as the payload's agent plists give them (scripts/pod_agents.py).

private let home = "/Users/x"
private let state = "/Users/x/.local/share/claude-acc"
private let installed: Set<String> = [state + "/python", state + "/acc.py"]

@Test("the tick agent: acc.py accswitch tick on the managed interpreter, output in launchd.log")
func tickAgent() throws {
    let plan = try PodAccRun.plan(home: home, args: ["--log", "launchd.log", "accswitch", "tick"], exists: installed.contains).get()
    #expect(plan.argv == [state + "/python", state + "/acc.py", "accswitch", "tick"])
    #expect(plan.log == state + "/launchd.log")
    #expect(plan.state == state)
}

@Test("without --log the output goes to pod-acc-run.log; a trailing slash in HOME changes nothing")
func defaultLog() throws {
    let plan = try PodAccRun.plan(home: home + "/", args: ["devguard", "run"], exists: installed.contains).get()
    #expect(plan.log == state + "/pod-acc-run.log")
    #expect(plan.argv.suffix(2) == ["devguard", "run"])
}

@Test("before setup.sh ran: EX_CONFIG, so launchd retries on the agent's schedule")
func notInstalled() {
    let result = PodAccRun.plan(home: home, args: ["perf", "keep"], exists: { $0 == state + "/python" })
    guard case .failure(let failure) = result else {
        Issue.record("expected a failure")
        return
    }
    #expect(failure.exitCode == 78)
    #expect(failure.message.contains(state))
}

@Test("no job, a flag for a job or a log name that leaves $STATE: EX_USAGE")
func usage() {
    for args in [[], ["--log"], ["--log", "launchd.log"], ["--log", "../x.log", "jobs", "tick"],
                 ["--log", "a/b.log", "jobs", "tick"], ["--log", "..", "jobs"], ["-v"]] {
        guard case .failure(let failure) = PodAccRun.plan(home: home, args: args, exists: installed.contains) else {
            Issue.record("expected a usage failure for \(args)")
            continue
        }
        #expect(failure.exitCode == 64, "\(args)")
    }
}
