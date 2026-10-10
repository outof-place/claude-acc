import Foundation
import Testing
@testable import ClaudeAcc

// Pod Menu: the same app run by Pod from Contents/Library/LoginItems (setup.sh --pod-agents).

@Test("Pod Menu is the copy inside a host bundle's LoginItems, not the one in ~/Applications")
func podMenuLocation() {
    #expect(PodMenu.isLoginItem(URL(fileURLWithPath: "/Applications/Pod.app/Contents/Library/LoginItems/Pod Menu.app")))
    #expect(!PodMenu.isLoginItem(URL(fileURLWithPath: "/Users/x/Applications/Claude Acc.app")))
    #expect(!PodMenu.isLoginItem(nil))
}

@Test("a test run is not Pod Menu: the panel names the legacy launchd job")
func legacyAgentLabel() {
    #expect(!PodMenu.active)
    #expect(PodMenu.agentLabel("devguard") == "com.filip.claude-acc.devguard")
}
