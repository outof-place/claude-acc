// The user's TCP sockets as devguard_core.sockets() reads them: each process's descriptor list and
// proc_pidfdinfo for its sockets, the answer `lsof -nP -a -u $UID -iTCP` gave, without lsof.
import Darwin

public struct SocketTable: Equatable, Sendable {
    /// listening port -> the pids listening on it
    public var listen: [Int: Set<pid_t>] = [:]
    /// (client pid, server port) for every established loopback connection, in lsof's order
    public var links: [Link] = []

    public struct Link: Equatable, Sendable {
        public var pid: pid_t
        public var port: Int
    }

    public init() {}

    public static let loopback: Set<String> = ["127.0.0.1", "[::1]", "::1", "localhost", "0.0.0.0", "*"]

    /// Every TCP socket of `pids` (all the user's processes when nil), ascending pid as lsof lists them.
    public static func read(pids: [pid_t]? = nil) -> SocketTable {
        var table = SocketTable()
        let list = (pids ?? userPids()).filter { $0 > 0 }.sorted()
        var fds = [proc_fdinfo](repeating: proc_fdinfo(), count: 4096)
        var info = socket_fdinfo()
        let infoSize = Int32(MemoryLayout<socket_fdinfo>.size)
        for pid in list {
            var size = fds.withUnsafeMutableBytes { proc_pidinfo(pid, PROC_PIDLISTFDS, 0, $0.baseAddress, Int32($0.count)) }
            if Int(size) == fds.count * MemoryLayout<proc_fdinfo>.stride {
                // a full buffer: the process has more descriptors than fit
                let more = Int(proc_pidinfo(pid, PROC_PIDLISTFDS, 0, nil, 0))
                fds = [proc_fdinfo](repeating: proc_fdinfo(), count: max(more, Int(size)) / MemoryLayout<proc_fdinfo>.stride + 64)
                size = fds.withUnsafeMutableBytes { proc_pidinfo(pid, PROC_PIDLISTFDS, 0, $0.baseAddress, Int32($0.count)) }
            }
            guard size > 0 else { continue }
            for fd in fds.prefix(Int(size) / MemoryLayout<proc_fdinfo>.stride) where fd.proc_fdtype == UInt32(PROX_FDTYPE_SOCKET) {
                guard proc_pidfdinfo(pid, fd.proc_fd, PROC_PIDFDSOCKETINFO, &info, infoSize) == infoSize else { continue }
                guard info.psi.soi_kind == Int32(SOCKINFO_TCP) else { continue }
                let tcp = info.psi.soi_proto.pri_tcp
                let ini = tcp.tcpsi_ini
                if tcp.tcpsi_state == Int32(TSI_S_LISTEN) {
                    table.listen[Int(UInt16(truncatingIfNeeded: ini.insi_lport).byteSwapped), default: []].insert(pid)
                } else if tcp.tcpsi_state == Int32(TSI_S_ESTABLISHED) {
                    let ipv4 = ini.insi_vflag & UInt8(INI_IPV4) != 0
                    let local = host(ini.insi_laddr, ipv4: ipv4)
                    let remote = host(ini.insi_faddr, ipv4: ipv4)
                    if loopback.contains(remote) || remote == local {
                        table.links.append(Link(pid: pid, port: Int(UInt16(truncatingIfNeeded: ini.insi_fport).byteSwapped)))
                    }
                }
            }
        }
        return table
    }

    /// proc_listpids(PROC_UID_ONLY, uid)
    public static func userPids() -> [pid_t] {
        let need = proc_listpids(UInt32(PROC_UID_ONLY), UInt32(getuid()), nil, 0)
        guard need > 0 else { return [] }
        var pids = [pid_t](repeating: 0, count: Int(need) / 4 + 64)
        let got = pids.withUnsafeMutableBytes { proc_listpids(UInt32(PROC_UID_ONLY), UInt32(getuid()), $0.baseAddress, Int32($0.count)) }
        return Array(pids.prefix(max(Int(got), 0) / 4))
    }

    /// An address from in_sockinfo as `lsof -n` writes it: 127.0.0.1, [::1], * for any.
    static func host(_ addr: in_sockinfo.__Unnamed_union_insi_faddr, ipv4: Bool) -> String {
        var addr = addr
        var text = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN))
        if ipv4 {
            var v4 = addr.ina_46.i46a_addr4
            inet_ntop(AF_INET, &v4, &text, socklen_t(text.count))
            let host = cText(text)
            return host == "0.0.0.0" ? "*" : host
        }
        inet_ntop(AF_INET6, &addr.ina_6, &text, socklen_t(text.count))
        let host = cText(text)
        return host == "::" ? "*" : "[\(host)]"
    }

    static func host(_ addr: in_sockinfo.__Unnamed_union_insi_laddr, ipv4: Bool) -> String {
        withUnsafeBytes(of: addr) { raw in
            host(raw.load(as: in_sockinfo.__Unnamed_union_insi_faddr.self), ipv4: ipv4)
        }
    }
}
