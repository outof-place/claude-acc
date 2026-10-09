import Foundation
import Testing
@testable import ClaudeAcc

// What the panel makes of a state file or a script's answer, with the inputs taken from the
// files and commands as they were on 2026-10-09.

private func alert(_ json: String) throws -> JanitorState.Alert {
    try #require(Store.decode(JanitorState.Alert.self, from: Data(json.utf8)))
}

private let gib = 1024.0 * 1024 * 1024

@Test("low disk: a sweep's old reading is not shown once today's reading is back above the limit")
func lowDiskHidesWhenSpaceCameBack() throws {
    // the 13:45 sweep saw 9.3 GB; by 18:00 Finder and the card above said 44 GB
    let sweep = try alert(#"{"kind": "low_disk", "free": \#(9.3 * gib), "limit": \#(40 * gib)}"#)
    #expect(sweep.lowDiskFree(now: DiskSpace(free: 44 * gib, total: 460 * gib)) == nil)
}

@Test("low disk: still low, the alert says today's number, the one the card shows")
func lowDiskSaysTodaysNumber() throws {
    let sweep = try alert(#"{"kind": "low_disk", "free": \#(23.8 * gib), "limit": \#(40 * gib)}"#)
    #expect(sweep.lowDiskFree(now: DiskSpace(free: 14.9 * gib, total: 460 * gib)) == 14.9 * gib)
    // no reading of its own yet: the sweep's number is all there is
    #expect(sweep.lowDiskFree(now: nil) == 23.8 * gib)
}

@Test("low disk: an alert from before the limit came with it uses janitor.py's default of 40 GB")
func lowDiskOldStateFile() throws {
    let sweep = try alert(#"{"kind": "low_disk", "free": \#(9.3 * gib)}"#)
    #expect(sweep.lowDiskFree(now: DiskSpace(free: 44 * gib, total: 460 * gib)) == nil)
    #expect(sweep.lowDiskFree(now: DiskSpace(free: 30 * gib, total: 460 * gib)) == 30 * gib)
    #expect(try alert(#"{"kind": "spotlight", "projects": ["web"]}"#).lowDiskFree(now: nil) == nil)
}

@Test("hotspot: `perf link --json` over the iPhone's USB reads as a hotspot via iPhone USB")
func tetherOverUSB() throws {
    // this Mac, 21:45: route via en8, gateway 172.20.10.1
    let out = #"{"tethered": true, "port": "iPhone USB", "iface": "en8", "gateway": "172.20.10.1", "at": 1791575116.06}"#
    let link = try #require(Store.decode(TetherLink.self, from: Data(out.utf8)))
    #expect(link.tethered)
    #expect(link.via == "iPhone USB")
}

@Test("hotspot: the iPhone's hotspot over Wi-Fi counts, a plain Wi-Fi does not")
func tetherOverWiFi() throws {
    let hotspot = #"{"tethered": true, "port": "Wi-Fi", "iface": "en0", "gateway": "172.20.10.1", "at": 1}"#
    let home = #"{"tethered": false, "port": "Wi-Fi", "iface": "en0", "gateway": "192.168.0.1", "at": 1}"#
    #expect(try #require(Store.decode(TetherLink.self, from: Data(hotspot.utf8))).via == "Wi-Fi")
    #expect(try #require(Store.decode(TetherLink.self, from: Data(home.utf8))).tethered == false)
}

@Test("hotspot: a render reads the route `perf keep` saved and Stay Awake shows it")
func renderShowsSavedRoute() throws {
    let state = #"{"ultra": null, "applied": {}, "link": {"tethered": true, "port": "iPhone USB", "iface": "en8", "gateway": "172.20.10.1", "at": 1}}"#
    let link = try #require(Store.decode(PerfFile.self, from: Data(state.utf8))?.link)
    let awake = Awake(preview: true, link: link)
    #expect(awake.onHotspot)
    #expect(awake.hotspotVia == "iPhone USB")
    #expect(!Awake(preview: true).onHotspot)
}
