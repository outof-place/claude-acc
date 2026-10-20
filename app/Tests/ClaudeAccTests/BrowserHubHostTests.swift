import Darwin
import Testing
@testable import ClaudeAcc

// `ClaudeAcc --browser-hub` (BrowserHubHost): the disclaiming re-exec replaces the process, so it is proven
// end to end in tests/test_browser.py (HubHost) against the built binary; these cover what runs in-process.

@Test("the host runs the installed browser.py with the app's interpreter, never a program from its command line")
func hubHostCommand() {
    #expect(BrowserHubHost.command(directory: "/s", python: "/s/python") == ["/s/python", "/s/browser.py", "serve"])
    #expect(BrowserHubHost.requested(["ClaudeAcc", "--browser-hub"]))
    #expect(!BrowserHubHost.requested(["ClaudeAcc", "--render", "--browser-hub"]))
    #expect(!BrowserHubHost.requested(["ClaudeAcc"]))
}

@Test("the host ends with the daemon's exit code, or 128 plus the signal that ended it")
func hubHostExitCode() {
    #expect(BrowserHubHost.runChild(["/bin/sh", "-c", "exit 7"]) == 7)
    #expect(BrowserHubHost.runChild(["/bin/sh", "-c", "kill -TERM $$"]) == 128 + SIGTERM)
    #expect(BrowserHubHost.runChild(["/nonexistent/python", "browser.py", "serve"]) == 127)
}
