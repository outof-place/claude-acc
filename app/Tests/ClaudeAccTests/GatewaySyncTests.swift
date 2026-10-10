import Foundation
import Testing
@testable import ClaudeAcc

// How often the open panel asks the browser and desktop gateways for `status` (each run is 50 to
// 75 ms of Python). It used to be every 5 s for both, about 25 ms/s with the panel open.

private func browserPanel(_ states: [(installed: Bool, state: String)]) throws -> BrowserPanel {
    let browsers = states.enumerated().map { index, entry in
        #"{"name": "b\#(index)", "title": "B\#(index)", "installed": \#(entry.installed), "state": "\#(entry.state)", "tabs": 0, "inspect": "chrome://inspect"}"#
    }
    let json = #"{"installed": true, "mcp_registered": true, "hub": false, "tabs_mode": "background", "idle_minutes": 10, "browsers": [\#(browsers.joined(separator: ","))], "tabs": [], "clients": 0, "recent": []}"#
    return try #require(Store.decode(BrowserPanel.self, from: Data(json.utf8)))
}

@Test("the open panel asks a gateway every minute, and every 5 s only while it waits for the user")
func statusIntervals() {
    #expect(Store.statusMaxAge(waiting: false) == 60)
    #expect(Store.statusMaxAge(waiting: true) == 5)
}

@Test("a browser asking for Allow is waited for; one that is ready, closed, disabled or missing is not")
func browserWaitsOnlyForAllow() throws {
    #expect(Store.browserWaits(try browserPanel([(true, "connecting")])))
    #expect(Store.browserWaits(try browserPanel([(true, "ready"), (true, "connecting")])))
    for state in ["connected", "ready", "closed", "disabled", "missing", "error"] {
        #expect(!Store.browserWaits(try browserPanel([(true, state)])), "\(state)")
    }
    // an uninstalled browser can't be asking for anything
    #expect(!Store.browserWaits(try browserPanel([(false, "connecting")])))
    #expect(!Store.browserWaits(nil))
}

@Test("launches and quits of the browsers browser.py drives wake the panel, matched lowercase like its config")
func browserBundles() {
    #expect(Store.browserBundles == ["com.google.chrome", "com.brave.browser"])
    #expect(Store.browserBundles.contains("com.google.Chrome".lowercased()))
}
