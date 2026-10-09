import AppKit
import Carbon.HIToolbox
import DictationCore
import Synchronization

/// The dictation key, the right ⌘, seen system wide through an event tap on a thread of its
/// own. (The right ⌥ types ą ę ł on a Polish layout, and holding it turns Orca's pointer into a
/// column-select crosshair; the right ⌘ types nothing alone.) The tap wakes the main actor only
/// around a press of the key: typing and clicking elsewhere cost a check of one atomic flag on
/// that thread. Escape while dictation records cancels it and doesn't reach the app (in Claude
/// Code it would interrupt the running turn). Decisions live in `TriggerMachine`.
final class TriggerKey {
    struct Event: Sendable {
        enum Kind: Sendable { case triggerDown, triggerUp, key, modifier, pointer, escape }
        let kind: Kind
        let time: Double
        let otherModifiers: Bool
    }

    private var machine = TriggerMachine()
    private let context: TapContext
    private var holdTimer: DispatchWorkItem?
    private var keyPoll: Timer?
    private(set) var isInstalled = false

    var onStartHold: (() -> Void)?
    var onStartHandsFree: (() -> Void)?
    var onStop: (() -> Void)?
    var onDiscard: (() -> Void)?
    var onEscape: (() -> Void)?

    init() {
        context = TapContext()
        context.handler = { [weak self] event in self?.handle(event) }
    }

    /// Needs Accessibility; false until it is granted.
    @discardableResult
    func install() -> Bool {
        guard !isInstalled else { return true }
        isInstalled = context.start()
        return isInstalled
    }

    /// Recording, so Escape is ours.
    func setRecording(_ on: Bool) {
        context.setRecording(on)
        if !on {
            machine.reset()
            stopKeyPoll()
            syncArmed()
        }
    }

    /// The tap thread forwards other input only while the machine wants it.
    private func syncArmed() {
        context.armed.store(machine.wantsOtherInput, ordering: .relaxed)
    }

    /// Recording started from the widget: a tap of the right ⌘ stops it.
    func enterHandsFree() {
        machine.enterHandsFree()
        setRecording(true)
    }

    private func handle(_ event: Event) {
        if event.kind == .triggerDown || event.kind == .triggerUp {
            dictationLog.info("right ⌘ \(event.kind == .triggerDown ? "down" : "up")")
        }
        let action: TriggerMachine.Action?
        switch event.kind {
        case .triggerDown:
            action = machine.keyDown(at: event.time, otherModifiers: event.otherModifiers)
            armHoldTimer()
        case .triggerUp:
            holdTimer?.cancel()
            action = machine.keyUp(at: event.time)
        case .key, .modifier:
            action = machine.otherInput(at: event.time, isKey: true)
        case .pointer:
            action = machine.otherInput(at: event.time, isKey: false)
        case .escape:
            onEscape?()
            return
        }
        perform(action)
        syncArmed()
    }

    private func perform(_ action: TriggerMachine.Action?) {
        if let action { dictationLog.notice("trigger: \(String(describing: action), privacy: .public)") }
        switch action {
        case .startHold:
            startKeyPoll()
            onStartHold?()
        case .startHandsFree:
            onStartHandsFree?()
        case .stop:
            stopKeyPoll()
            context.setRecording(false)
            onStop?()
        case .discard:
            stopKeyPoll()
            context.setRecording(false)
            onDiscard?()
        case nil:
            break
        }
    }

    private func armHoldTimer() {
        holdTimer?.cancel()
        guard let deadline = machine.holdDeadline else { return }
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.perform(self.machine.tick(at: TapContext.now()))
                self.syncArmed()
            }
        }
        holdTimer = work
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, deadline - TapContext.now()), execute: work)
    }

    /// A key-up lost to a busy moment or secure input would record until the length limit:
    /// while held, the key's real state is checked ten times a second.
    private func startKeyPoll() {
        stopKeyPoll()
        keyPoll = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.machine.isHolding else { return }
                // keyState(…, keyCode) of a modifier reads false while it is held (it stopped every
                // hold 100 ms in); the ⌘ flag of the session's modifier state is the reliable one
                if !CGEventSource.flagsState(.combinedSessionState).contains(.maskCommand) {
                    dictationLog.notice("right ⌘ up missed, caught by the poll")
                    self.perform(self.machine.keyUp(at: TapContext.now()))
                    self.syncArmed()
                }
            }
        }
    }

    private func stopKeyPoll() {
        keyPoll?.invalidate()
        keyPoll = nil
    }

    /// Secure keyboard entry (a password field, Terminal's "Secure Keyboard Entry") hides keys
    /// from every tap.
    static var secureInput: Bool { IsSecureEventInputEnabled() }
}

/// What the tap thread shares with the main actor: two flags it reads on every event, and the
/// handler it sends events to.
nonisolated final class TapContext: @unchecked Sendable {
    let recording = Atomic<Bool>(false)
    /// Set by the tap thread itself the moment the right ⌘ goes down, so a key typed right
    /// after it is never missed; cleared by the main actor when the machine is idle again.
    let armed = Atomic<Bool>(false)
    /// Set once before the tap starts.
    var handler: (@MainActor (TriggerKey.Event) -> Void)?
    fileprivate var port: CFMachPort?
    private var thread: Thread?

    /// Seconds on the clock the hold timer uses too.
    static func now() -> Double { Double(clock_gettime_nsec_np(CLOCK_UPTIME_RAW)) / 1e9 }

    /// One active tap: it needs Accessibility only. A listen-only tap needs Input Monitoring
    /// too, whose grant macOS ties to the exact build that asked for it; after an update it
    /// silently delivered no keys while the Settings switch still showed on (2026-10-08).
    /// The callback runs on this thread, not the main one, and passes everything through at
    /// once, so a busy main thread never holds anyone's typing.
    func start() -> Bool {
        let watch = [CGEventType.flagsChanged, .keyDown, .leftMouseDown, .rightMouseDown, .otherMouseDown]
            .reduce(CGEventMask(0)) { $0 | (1 << $1.rawValue) }
        guard let port = CGEvent.tapCreate(
            tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap, eventsOfInterest: watch,
            callback: tapCallback, userInfo: Unmanaged.passUnretained(self).toOpaque())
        else { return false }
        self.port = port
        // handed to the tap thread once and used only there
        let parts = Unchecked(port)
        let thread = Thread {
            let port = parts.value
            CFRunLoopAddSource(CFRunLoopGetCurrent(), CFMachPortCreateRunLoopSource(nil, port, 0), .commonModes)
            CGEvent.tapEnable(tap: port, enable: true)
            CFRunLoopRun()
        }
        thread.name = "claude-acc.dictation.tap"
        thread.qualityOfService = .userInteractive
        thread.start()
        self.thread = thread
        return true
    }

    func setRecording(_ on: Bool) {
        recording.store(on, ordering: .relaxed)
    }

    fileprivate func send(_ event: TriggerKey.Event) {
        guard let handler else { return }
        DispatchQueue.main.async {
            MainActor.assumeIsolated { handler(event) }
        }
    }
}

nonisolated private struct Unchecked<Value>: @unchecked Sendable {
    let value: Value
    init(_ value: Value) { self.value = value }
}

/// The tap: right ⌘ presses always, other input only while armed, Escape kept from the app
/// while recording. Plain C, no captures; everything else passes through untouched.
nonisolated private func tapCallback(
    proxy: CGEventTapProxy, type: CGEventType, event: CGEvent, refcon: UnsafeMutableRawPointer?
) -> Unmanaged<CGEvent>? {
    guard let refcon else { return Unmanaged.passUnretained(event) }
    let context = Unmanaged<TapContext>.fromOpaque(refcon).takeUnretainedValue()
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let port = context.port { CGEvent.tapEnable(tap: port, enable: true) }
        return Unmanaged.passUnretained(event)
    }
    let code = event.getIntegerValueField(.keyboardEventKeycode)
    if type == .flagsChanged, code == Int64(kVK_RightCommand) {
        // NX_DEVICERCMDKEYMASK: the right ⌘ itself, not just "some ⌘"
        let down = event.flags.rawValue & 0x10 != 0
        if down { context.armed.store(true, ordering: .relaxed) }
        let others = !event.flags.intersection([.maskControl, .maskShift, .maskAlternate]).isEmpty
        context.send(.init(kind: down ? .triggerDown : .triggerUp, time: TapContext.now(), otherModifiers: others))
        return Unmanaged.passUnretained(event)
    }
    if type == .keyDown, code == Int64(kVK_Escape), context.recording.load(ordering: .relaxed) {
        // cancels dictation and doesn't reach the app (in Claude Code it would interrupt the turn)
        context.send(.init(kind: .escape, time: TapContext.now(), otherModifiers: false))
        return nil
    }
    guard context.armed.load(ordering: .relaxed),
          event.getIntegerValueField(.eventSourceUserData) != Inserter.marker  // our own ⌘V
    else { return Unmanaged.passUnretained(event) }
    let kind: TriggerKey.Event.Kind = switch type {
    case .flagsChanged: .modifier
    case .keyDown: .key
    default: .pointer
    }
    context.send(.init(kind: kind, time: TapContext.now(), otherModifiers: false))
    return Unmanaged.passUnretained(event)
}
