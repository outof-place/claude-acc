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

@Test("what Swift can't hold comes back unchanged: big ints and lone surrogates")
func rawValues() throws {
    let text = #"{"n": 123456789012345678901234567890, "c": "make \udcff", "k": -5}"#
    let value = try PyJSON.loads(text)
    #expect(value["n"] == .raw("123456789012345678901234567890"))
    #expect(value["c"] == .raw(#""make \udcff""#))
    #expect(value["k"] == .int(-5))
    #expect(value.dumps() == text)
    #expect(value["c"]?.string == "make \u{FFFD}")
    #expect(value["n"]?.double == 1.2345678901234568e29)
    #expect(try PyJSON.loads(#""\udcff""#, exact: false) == .string("\u{FFFD}"))
    #expect(throws: PyJSON.ParseError.self) { try PyJSON.loads("--5") }
}

@Test("strings and keys compare by code point, as Python's str")
func codePointEquality() {
    let nfc = "caf\u{E9}", nfd = "cafe\u{301}"
    #expect(PyJSON.string(nfc) != PyJSON.string(nfd))
    #expect(PyJSON.string("\u{212A}") != PyJSON.string("K"))  // KELVIN SIGN is canonically K
    var o = PyObject()
    o[nfc] = 1
    o[nfd] = 2
    #expect(o.count == 2)
    #expect(o[nfc] == .int(1) && o[nfd] == .int(2))
    o[nfd] = nil
    #expect(o.keys == [nfc])
}

@Test("os.path.basename and dirname split at the last / code point")
func paths() {
    #expect(basename("/a/b\u{301}/c") == "c")
    #expect(basename("/a//\u{301}x") == "\u{301}x")  // "/" + a combining mark is one Character in Swift
    #expect(dirname("/a//\u{301}x") == "/a")
    #expect(dirname("//x") == "//")
    #expect(dirname("///") == "///")
    #expect(dirname("/x") == "/")
    #expect(dirname("a//b") == "a")
    #expect(dirname("x") == "")
    #expect(basename("a/") == "")
}

@Test("translate: Python's \\B, \\v, \\Z and class punctuation; flags and (?P are refused")
func regexTranslate() {
    #expect(PyRegex(#"a\Bb"#).search("ab"))
    #expect(!PyRegex(#"a\B b"#).search("a b"))
    #expect(!PyRegex(#"\v"#).search("\n"))  // ICU's \v is any vertical space
    #expect(!PyRegex(#"x\Z"#).search("x\n"))
    #expect(PyRegex(#"[[]"#).search("["))
    #expect(PyRegex(#"[a&&b]"#).search("&"))
    #expect(PyRegex(#"[\S]"#).search("x") && !PyRegex(#"[\S]"#).search(" "))
    #expect(PyRegex.translate(#"(?i)pod"#) == nil)
    #expect(PyRegex.translate(#"(?P<x>a)"#) == nil)
    #expect(PyRegex.translate(#"[+--]"#) == nil)
    #expect(PyRegex.translate(#"(?<!x)(?<=y)(?:z)(?=w)(?!v)(?>u)"#) != nil)
}
