import AppKit
import ApplicationServices
import DictationCore

/// Puts a transcript where the person types. A native text field takes it straight at the
/// cursor through Accessibility, with spaces at the seam and a lower-case start mid-sentence.
/// A terminal (Claude Code in Orca, Ghostty) gets ⌘V, in pieces under the 800 characters at
/// which Claude Code folds a paste into "[Pasted text #N]". The clipboard is put back after.
enum Inserter {
    enum Outcome: Equatable {
        /// Straight into the field through Accessibility.
        case typed
        case pasted(pieces: Int)
        /// Left on the clipboard: no Accessibility, or another app came to the front.
        case clipboard
    }

    /// Marks the ⌘V this app posts, so the right ⌘ tap doesn't take it for typing.
    nonisolated static let marker: Int64 = 0x4443_5441 // "DCTA"

    /// Terminals: their text areas take no Accessibility writes, so they go straight to ⌘V. The host app
    /// (`HostApp.current`, Pod as well as Orca) counts as one too.
    static let terminals: Set<String> = [
        "com.stablyai.orca", "com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty",
        "dev.warp.Warp-Stable", "net.kovidgoyal.kitty", "com.github.wez.wezterm", "io.alacritty", "co.zeit.hyper",
        "com.microsoft.VSCode", "com.todesktop.230313mzl4w4u92", "dev.zed.Zed",
    ]

    static func insert(_ text: String, terms: [String], expectedPID: pid_t?, gapMs: Int) async -> Outcome {
        let front = NSWorkspace.shared.frontmostApplication
        let trusted = AXIsProcessTrusted()
        guard trusted, expectedPID == nil || front?.processIdentifier == expectedPID else {
            copy(text)
            return .clipboard
        }
        let isTerminal = front?.bundleIdentifier.map { terminals.contains($0) || $0 == HostApp.current.bundleId } ?? false
        if !isTerminal, await typeIntoFocused(text, terms: terms) { return .typed }
        // a terminal shows no draft to Accessibility: the text goes in as at an empty prompt
        let piece = TextRules.joinInsert(before: "", after: "", raw: text, terms: terms)
        let pieces = Chunker.chunks(piece)
        await paste(pieces, gapMs: gapMs)
        return .pasted(pieces: pieces.count)
    }

    /// The plain clipboard, for ⌘V by hand.
    static func copy(_ text: String) {
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(text, forType: .string)
    }

    // MARK: Accessibility

    /// Off the main actor: every Accessibility call is a round trip to the other app.
    @concurrent
    static func typeIntoFocused(_ text: String, terms: [String]) async -> Bool {
        let system = AXUIElementCreateSystemWide()
        // the default timeout is 6 s: a hung app would hold the transcript that long
        AXUIElementSetMessagingTimeout(system, 0.3)
        var focused: CFTypeRef?
        guard AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let focused, CFGetTypeID(focused) == AXUIElementGetTypeID()
        else { return false }
        let element = focused as! AXUIElement
        AXUIElementSetMessagingTimeout(element, 0.3)
        var settable = DarwinBoolean(false)
        guard AXUIElementIsAttributeSettable(element, kAXSelectedTextAttribute as CFString, &settable) == .success,
              settable.boolValue, let selection = selectedRange(element)
        else { return false }
        let total = numberOfCharacters(element)
        let start = max(0, selection.location - 200)
        let before = string(element, CFRange(location: start, length: selection.location - start)) ?? ""
        let end = selection.location + selection.length
        let after = total.map { string(element, CFRange(location: end, length: min(2, max(0, $0 - end)))) ?? "" } ?? ""
        let piece = TextRules.joinInsert(before: before, after: after, raw: text, terms: terms)
        guard !piece.isEmpty else { return true }
        guard AXUIElementSetAttributeValue(element, kAXSelectedTextAttribute as CFString, piece as CFString) == .success
        else { return false }
        // Electron and Chrome take the write and do nothing: believe only what reads back
        let written = string(element, CFRange(location: selection.location, length: piece.utf16.count))
        return written == piece
    }

    nonisolated private static func selectedRange(_ element: AXUIElement) -> CFRange? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXValueGetTypeID()
        else { return nil }
        var range = CFRange()
        return AXValueGetValue(value as! AXValue, .cfRange, &range) ? range : nil
    }

    nonisolated private static func numberOfCharacters(_ element: AXUIElement) -> Int? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXNumberOfCharactersAttribute as CFString, &value) == .success
        else { return nil }
        return (value as? NSNumber)?.intValue
    }

    /// A slice of the field's text, without reading the whole of it; whole value as a fallback.
    nonisolated private static func string(_ element: AXUIElement, _ range: CFRange) -> String? {
        guard range.length > 0 else { return "" }
        var cfRange = range
        if let param = AXValueCreate(.cfRange, &cfRange) {
            var value: CFTypeRef?
            if AXUIElementCopyParameterizedAttributeValue(
                element, kAXStringForRangeParameterizedAttribute as CFString, param, &value) == .success,
                let text = value as? String {
                return text
            }
        }
        var whole: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &whole) == .success,
              let text = whole as? NSString, range.location + range.length <= text.length
        else { return nil }
        return text.substring(with: NSRange(location: range.location, length: range.length))
    }

    // MARK: Paste

    /// ⌘V piece by piece, then the clipboard as it was. The pieces skip clipboard managers and
    /// Universal Clipboard: they're marked transient and stay on this Mac.
    static func paste(_ pieces: [String], gapMs: Int) async {
        let board = NSPasteboard.general
        let saved = board.pasteboardItems?.map { item in
            item.types.compactMap { type in item.data(forType: type).map { (type, $0) } }
        } ?? []
        await waitForModifiersUp()
        var ours = board.changeCount
        for (index, piece) in pieces.enumerated() {
            if index > 0 { try? await Task.sleep(for: .milliseconds(gapMs)) }
            board.prepareForNewContents(with: .currentHostOnly)
            let item = NSPasteboardItem()
            item.setString(piece, forType: .string)
            item.setData(Data(), forType: NSPasteboard.PasteboardType("org.nspasteboard.TransientType"))
            item.setData(Data(), forType: NSPasteboard.PasteboardType("org.nspasteboard.AutoGeneratedType"))
            board.writeObjects([item])
            ours = board.changeCount
            try? await Task.sleep(for: .milliseconds(50))
            commandV()
        }
        try? await Task.sleep(for: .milliseconds(500))
        // someone copied in the meantime: theirs stays
        guard board.changeCount == ours else { return }
        board.clearContents()
        guard !saved.isEmpty else { return }
        board.writeObjects(saved.map { pairs in
            let item = NSPasteboardItem()
            for (type, data) in pairs { item.setData(data, forType: type) }
            return item
        })
    }

    /// A held modifier (the ⌘ that just stopped dictation) would turn ⌘V into something else.
    private static func waitForModifiersUp() async {
        let held: CGEventFlags = [.maskCommand, .maskAlternate, .maskControl, .maskShift]
        for _ in 0..<50 {
            if CGEventSource.flagsState(.hidSystemState).intersection(held).isEmpty { return }
            try? await Task.sleep(for: .milliseconds(20))
        }
    }

    private static func commandV() {
        let source = CGEventSource(stateID: .combinedSessionState)
        source?.userData = marker
        let v: CGKeyCode = 9
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: down)
            event?.flags = .maskCommand
            event?.post(tap: .cghidEventTap)
        }
    }
}
