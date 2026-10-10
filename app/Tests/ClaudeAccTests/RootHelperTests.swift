import PodRootdClient
import Testing
@testable import ClaudeAcc

// Pod Menu and Pod's root helper (docs/pod-rootd.md): who drives the fans and the lid.

@Test("the helper takes the fans and the lid only once the old fans daemon is migrated")
func rootHelperOwnsAfterMigration() {
    var status = Status()
    status.legacy = [LegacyStatus(daemon: .fans, installed: true, migrated: false)]
    #expect(!RootHelper.owns(status))
    status.legacy = [LegacyStatus(daemon: .fans, installed: false, migrated: true)]
    #expect(RootHelper.owns(status))
    // a Mac that never had the old daemon
    status.legacy = [LegacyStatus(daemon: .fans, installed: false, migrated: false)]
    #expect(RootHelper.owns(status))
}

@Test("the standalone Claude Acc.app has no helper: nothing connects, the files stay in charge")
func rootHelperOutsidePod() {
    let helper = RootHelper(appIdentifier: nil)
    #expect(!helper.owns)
    #expect(helper.fanState == nil)
    #expect(RootHelper.hostAppIdentifier() == nil)
}
