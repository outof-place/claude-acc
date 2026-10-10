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

@Test("claude-acc://panel opens the panel; a path names the section to show")
func panelRoute() throws {
    #expect(PanelRoute.section(of: try #require(URL(string: "claude-acc://panel"))) == nil)
    #expect(PanelRoute.section(of: try #require(URL(string: "claude-acc://panel/"))) == nil)
    #expect(PanelRoute.section(of: try #require(URL(string: "claude-acc://panel/services"))) == "services")
    #expect(PanelRoute.section(of: try #require(URL(string: "claude-acc://awake/on"))) == nil)
}

@Test("Pod Menu shows Pod's mark only without quota data; the legacy app always draws the ring")
func podMarkOnlyWithoutQuota() throws {
    #expect(MenuLabelState(used: nil).showsPodMark(in: true))
    #expect(!MenuLabelState(used: 42).showsPodMark(in: true))
    #expect(!MenuLabelState(used: nil).showsPodMark(in: false))
    let mark = try #require(PodMark.template)
    #expect(mark.isTemplate)
    #expect(mark.size == NSSize(width: 16, height: 16))
    let badged = try #require(PodMark.make(badge: .systemOrange))
    #expect(!badged.isTemplate)
}
