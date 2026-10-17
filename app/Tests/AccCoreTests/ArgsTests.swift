import Testing
@testable import AccCore

// KERN_PROCARGS2 blocks as devguard_core.procargs/proc_argv/proc_env parse them.

private func block(argc: UInt32, _ body: [UInt8]) -> [UInt8] {
    [UInt8(argc & 0xff), UInt8(argc >> 8 & 0xff), UInt8(argc >> 16 & 0xff), UInt8(argc >> 24)] + body
}

private func bytes(_ text: String) -> [UInt8] { Array(text.utf8) }

@Test("argv after the path and its padding, then the environment up to the empty string")
func argvAndEnvironment() {
    let raw = block(argc: 2, bytes("/bin/node\0\0\0node\0dev\0A=1\0B=x=y\0\0apple=1\0"))
    let args = Args.parse(raw)
    #expect(args.exact == ["node", "dev"])
    #expect(args.environment == ["A": "1", "B": "x=y"])
    #expect(Text.psCommand(args.argv) == "node dev")
}

@Test("a cut block: fewer strings than argc means no exact argv and no environment")
func cutBlock() {
    let args = Args.parse(block(argc: 3, bytes("/bin/sh\0sh\0-c")))
    #expect(args.argv.count == 2)
    #expect(args.exact == nil)
    #expect(args.environment.isEmpty)
    #expect(Text.psCommand(args.argv) == "sh -c")
}

@Test("no NUL after the path: one empty argument, as Python's partition and split give")
func noNul() {
    let args = Args.parse(block(argc: 1, bytes("/bin/x")))
    #expect(args.argv == [[]])
    #expect(Text.psCommand(args.argv) == "")
}

@Test("control characters as ps shows them, tab and newline in octal")
func controlCharacters() {
    #expect(Text.psCommand([bytes("a\tb\nc\u{1}d\u{7f}")]) == "a\\011b\\012c^Ad^?")
}
