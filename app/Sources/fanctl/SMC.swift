import Foundation
import IOKit

/// The System Management Controller through `AppleSMC`, the way smcFanControl and Stats talk to it.
///
/// Every call passes an 80-byte `SMCKeyData_t` (C layout, filled by offset here so Swift's
/// struct layout can't drift) to selector 2 (`kSMCHandleYPCEvent`). Reading needs no root;
/// writing fan keys does.
final class SMC {
    enum Failure: Error, CustomStringConvertible {
        case noService, open(kern_return_t), call(String, kern_return_t), result(String, UInt8), badType(String, String)

        var description: String {
            switch self {
            case .noService: "no AppleSMC service"
            case .open(let code): "IOServiceOpen failed (\(code))"
            case .call(let key, let code): "\(key): IOConnectCallStructMethod failed (\(code))"
            case .result(let key, let code): "\(key): SMC refused (result \(code))"
            case .badType(let key, let type): "\(key): unexpected type \(type)"
            }
        }
    }

    struct Info {
        let size: Int
        let type: String
    }

    private let connection: io_connect_t
    private static let size = 80
    private enum Command: UInt8 { case read = 5, write = 6, keyAtIndex = 8, info = 9 }

    init() throws {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"))
        guard service != 0 else { throw Failure.noService }
        defer { IOObjectRelease(service) }
        var connection: io_connect_t = 0
        let status = IOServiceOpen(service, mach_task_self_, 0, &connection)
        guard status == KERN_SUCCESS else { throw Failure.open(status) }
        self.connection = connection
    }

    deinit { IOServiceClose(connection) }

    // MARK: Keys

    static func code(_ text: String) -> UInt32 {
        text.utf8.prefix(4).reduce(0) { $0 << 8 | UInt32($1) }
    }

    static func name(_ code: UInt32) -> String {
        String(bytes: [24, 16, 8, 0].map { UInt8(truncatingIfNeeded: code >> $0) }, encoding: .ascii) ?? "????"
    }

    func info(_ key: String) throws -> Info {
        var input = [UInt8](repeating: 0, count: Self.size)
        Self.put(Self.code(key), at: 0, in: &input)
        input[42] = Command.info.rawValue
        let output = try call(key, input)
        return Info(size: Int(Self.get(output, at: 28)), type: Self.name(Self.get(output, at: 32)))
    }

    func read(_ key: String) throws -> (info: Info, bytes: [UInt8]) {
        let info = try info(key)
        var input = [UInt8](repeating: 0, count: Self.size)
        Self.put(Self.code(key), at: 0, in: &input)
        Self.put(UInt32(info.size), at: 28, in: &input)
        input[42] = Command.read.rawValue
        let output = try call(key, input)
        return (info, Array(output[48..<(48 + min(info.size, 32))]))
    }

    func write(_ key: String, _ bytes: [UInt8]) throws {
        let info = try info(key)
        var input = [UInt8](repeating: 0, count: Self.size)
        Self.put(Self.code(key), at: 0, in: &input)
        Self.put(UInt32(info.size), at: 28, in: &input)
        input[42] = Command.write.rawValue
        for (i, byte) in bytes.prefix(min(info.size, 32)).enumerated() { input[48 + i] = byte }
        _ = try call(key, input)
    }

    /// Every key the SMC knows, by index; `#KEY` holds the count.
    func allKeys() throws -> [String] {
        let count = try read("#KEY").bytes.prefix(4).reduce(0) { $0 << 8 | UInt32($1) }
        return (0..<count).compactMap { index in
            var input = [UInt8](repeating: 0, count: Self.size)
            input[42] = Command.keyAtIndex.rawValue
            Self.put(index, at: 44, in: &input)
            guard let output = try? call("#\(index)", input) else { return nil }
            return Self.name(Self.get(output, at: 0))
        }
    }

    // MARK: Values

    /// A number in whatever type the key uses: `flt ` on Apple Silicon, `fpe2`/`sp78` on older Macs.
    func number(_ key: String) throws -> Double {
        let (info, bytes) = try read(key)
        switch info.type {
        case "flt ": return Double(Float(bitPattern: bytes.prefix(4).reversed().reduce(0) { $0 << 8 | UInt32($1) }))
        case "ui8 ": return Double(bytes[0])
        case "ui16": return Double(UInt16(bytes[0]) << 8 | UInt16(bytes[1]))
        case "ui32": return Double(bytes.prefix(4).reduce(0) { $0 << 8 | UInt32($1) })
        case "fpe2": return Double(UInt16(bytes[0]) << 8 | UInt16(bytes[1])) / 4
        case "sp78": return Double(Int16(bitPattern: UInt16(bytes[0]) << 8 | UInt16(bytes[1]))) / 256
        default: throw Failure.badType(key, info.type)
        }
    }

    func setNumber(_ key: String, _ value: Double) throws {
        let info = try info(key)
        switch info.type {
        case "flt ":
            let bits = Float(value).bitPattern
            try write(key, [0, 8, 16, 24].map { UInt8(truncatingIfNeeded: bits >> $0) })
        case "ui8 ":
            try write(key, [UInt8(clamping: Int(value))])
        case "fpe2":
            let raw = UInt16(clamping: Int(value * 4))
            try write(key, [UInt8(raw >> 8), UInt8(raw & 0xff)])
        default:
            throw Failure.badType(key, info.type)
        }
    }

    // MARK: Plumbing

    private func call(_ key: String, _ input: [UInt8]) throws -> [UInt8] {
        var output = [UInt8](repeating: 0, count: Self.size)
        var outputSize = Self.size
        let status = input.withUnsafeBytes { inBytes in
            output.withUnsafeMutableBytes { outBytes in
                IOConnectCallStructMethod(
                    connection, 2, inBytes.baseAddress, Self.size, outBytes.baseAddress, &outputSize)
            }
        }
        guard status == KERN_SUCCESS else { throw Failure.call(key, status) }
        guard output[40] == 0 else { throw Failure.result(key, output[40]) }
        return output
    }

    private static func put(_ value: UInt32, at offset: Int, in buffer: inout [UInt8]) {
        withUnsafeBytes(of: value.littleEndian) { for (i, byte) in $0.enumerated() { buffer[offset + i] = byte } }
    }

    private static func get(_ buffer: [UInt8], at offset: Int) -> UInt32 {
        buffer[offset..<(offset + 4)].reversed().reduce(0) { $0 << 8 | UInt32($1) }
    }
}
