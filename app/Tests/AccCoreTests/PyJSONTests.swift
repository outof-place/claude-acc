import Testing
@testable import AccCore

// json.dumps / json.loads / float.__repr__ / sum() as the scripts that share the state files do them.

@Test("float repr: shortest round-trip digits, fixed between 1e-4 and 1e16, else d.de±XX")
func floatRepr() {
    let cases: [(Double, String)] = [
        (1.0, "1.0"), (0.1, "0.1"), (1e16, "1e+16"), (1e15, "1000000000000000.0"), (1.5e-7, "1.5e-07"),
        (0.0001, "0.0001"), (0.00001, "1e-05"), (-2.5, "-2.5"), (123456789.125, "123456789.125"),
        (1791659299.0441125, "1791659299.0441124"), (-0.0, "-0.0"), (2.675, "2.675"), (5e-324, "5e-324"),
    ]
    for (value, text) in cases { #expect(PyJSON.repr(value) == text, "\(value)") }
}

@Test("dumps: ensure_ascii, ', ' and ': ', key order kept, ints stay ints")
func dumps() throws {
    let text = #"{"b": 1, "a": [1.0, null, true, "zażółć \"q\"\n"], "c": {}}"#
    let value = try PyJSON.loads(text)
    #expect(value.dumps() == #"{"b": 1, "a": [1.0, null, true, "za\u017c\u00f3\u0142\u0107 \"q\"\n"], "c": {}}"#)
    #expect(value["b"] == .int(1))
    #expect(value["a"]?.array?.first == .double(1.0))
    #expect(try PyJSON.loads("\"\\ud83d\\ude00\"") == .string("😀"))
    #expect(PyJSON.string("😀").dumps() == "\"\\ud83d\\ude00\"")
}

@Test("Python's \\s, \\b, . and $ on ICU")
func regexSemantics() {
    #expect(PyRegex(#"a\sb"#).search("a\u{0B}b"))  // ICU's \s leaves out \v
    #expect(PyRegex(#"dev\b"#).search("dev\u{0301}"))  // a combining mark is no word character in Python
    #expect(!PyRegex(#"a.b"#).search("a\nb"))
    #expect(PyRegex(#"a.b"#).search("a\u{2028}b"))  // ICU's . stops at U+2028, Python's doesn't
    #expect(PyRegex(#"x$"#).search("x\n"))
    #expect(!PyRegex(#"x$"#).search("x\n\n"))
    #expect(PyRegex(#"(?<![\w-])Pod\.app"#).search("/Applications/Pod.app/x"))
    #expect(!PyRegex(#"(?<![\w-])Pod\.app"#).search("/Applications/MyPod.app/x"))
}
