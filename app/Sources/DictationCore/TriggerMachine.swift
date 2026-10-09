import Foundation

/// The dictation key (the right ⌘) as a button: tap it to start, tap it again to stop and
/// insert. Or hold it while you talk and let go to insert.
///
/// The key is pressed all the time as a modifier (⌘C, ⌘V), so nothing starts on the press
/// itself: a tap is down and up within 250 ms with no other key, click or scroll in between,
/// and a hold counts only after 300 ms of the same, so a shortcut never lights the microphone
/// up (or switches AirPods to their call profile). A key pressed within the first second of a
/// hold still means a shortcut typed slowly: that recording goes away without a trace.
///
/// Times come in from the caller, so the tests drive it without a clock.
public struct TriggerMachine: Sendable {
    public static let holdDelay = 0.3
    public static let tapMax = 0.25
    /// A key pressed this soon after a hold started recording still means a shortcut typed
    /// slowly: the recording goes away without a trace.
    public static let typingGrace = 1.0

    public enum Action: Sendable, Equatable {
        case startHold
        case startHandsFree
        case stop
        /// Throw the recording away without a sound or an error.
        case discard
    }

    enum State: Sendable, Equatable {
        case idle
        /// Down, not yet a hold; `clean` while nothing else happened.
        case pressed(at: Double, clean: Bool)
        case holding(since: Double)
        case handsFree
        /// Down during hands free: a clean tap stops it.
        case pressedInHandsFree(at: Double, clean: Bool)
    }

    private(set) var state = State.idle

    public init() {}

    public var isHolding: Bool { if case .holding = state { true } else { false } }
    /// Whether other keys, clicks and modifiers matter right now: around a press of the key,
    /// not while idle or while hands free just listens.
    public var wantsOtherInput: Bool {
        switch state {
        case .idle, .handsFree: false
        default: true
        }
    }
    public var isRecording: Bool {
        switch state {
        case .holding, .handsFree, .pressedInHandsFree: true
        default: false
        }
    }

    /// The key went down. `otherModifiers`: another modifier (⌃, ⇧, ⌥) is held.
    public mutating func keyDown(at t: Double, otherModifiers: Bool) -> Action? {
        switch state {
        case .idle:
            if !otherModifiers { state = .pressed(at: t, clean: true) }
        case .handsFree:
            state = .pressedInHandsFree(at: t, clean: !otherModifiers)
        default:
            break
        }
        return nil
    }

    public mutating func keyUp(at t: Double) -> Action? {
        switch state {
        case .pressed(let at, let clean):
            if clean && t - at < Self.tapMax {
                state = .handsFree
                return .startHandsFree
            }
            state = .idle
        case .holding:
            state = .idle
            return .stop
        case .pressedInHandsFree(let at, let clean):
            if clean && t - at < Self.tapMax {
                state = .idle
                return .stop
            }
            state = .handsFree
        default:
            break
        }
        return nil
    }

    /// Any other key, a click or a scroll, or another modifier.
    public mutating func otherInput(at t: Double, isKey: Bool) -> Action? {
        switch state {
        case .pressed(let at, _):
            state = .pressed(at: at, clean: false)
        case .holding(let since):
            if isKey && t - since < Self.typingGrace {
                state = .idle
                return .discard
            }
        case .pressedInHandsFree(let at, _):
            state = .pressedInHandsFree(at: at, clean: false)
        default:
            break
        }
        return nil
    }

    /// The hold timer: the caller asks `holdDeadline` and calls this when it passes.
    public mutating func tick(at t: Double) -> Action? {
        if case .pressed(let at, let clean) = state, clean, t - at >= Self.holdDelay {
            state = .holding(since: t)
            return .startHold
        }
        return nil
    }

    /// When `tick` may start a hold, if the key is down and nothing else happened.
    public var holdDeadline: Double? {
        if case .pressed(let at, true) = state { return at + Self.holdDelay }
        return nil
    }

    /// Recording started some other way (a click on the widget): the next clean tap stops it.
    public mutating func enterHandsFree() {
        state = .handsFree
    }

    /// Recording ended some other way (Escape, the widget, an error).
    public mutating func reset() {
        state = .idle
    }
}
