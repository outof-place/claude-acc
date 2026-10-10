// The scheduler's live readings for acc-cored: probe_memory, the guard's snapshot, native_scan,
// job_pids/pids_usage and the wrapper's output size, read as sched.py reads them.
import Darwin

public final class LiveSchedProbes: SchedProbes {
    public let now: Double
    let state: String
    /// the guard's snapshot from the guard running in this process, when it does
    let snapshot: PyObject?
    /// native_scan's answer, read at most once per pass
    lazy var scan: (gb: Double, active: Bool, pids: Set<pid_t>) = SchedNative.scan()

    public init(stateDir: String, snapshot: PyObject? = nil) {
        now = Kernel.wall()
        state = stateDir
        self.snapshot = snapshot
    }

    /// fake_memory(): SCHED_FAKE_MEMORY's file in tests, as sched.py reads it
    static func fake() -> PyObject? {
        guard let path = ProcessEnv.get("SCHED_FAKE_MEMORY") else { return nil }
        return Files.json(path, fallback: .null).object
    }

    /// probe_memory(): kern.memorystatus_level, RAM, swap and the kernel's pressure word
    public func memory() -> PyObject {
        if let data = Self.fake() {
            var out = PyObject([
                ("level", .double(data["level"]?.double ?? 60)), ("ram_gb", .double(data["ram_gb"]?.double ?? 48)),
                ("swap_gb", .double(data["swap_gb"]?.double ?? 0)), ("pressure", data["pressure"] ?? .string("normal")),
            ])
            if let native = data["native"] { out["native"] = native.truthy ? native : .object(PyObject()) }
            return out
        }
        let ram = Double(Kernel.int("hw.memsize") ?? UInt64(16 * Sched.GB)) / Sched.GB
        let level = Kernel.int("kern.memorystatus_level").map { Double($0) } ?? 50
        let k = Kernel.int("kern.memorystatus_vm_pressure_level").flatMap { $0 == 0 ? nil : Int($0) } ?? 1
        let pressure = k >= 4 ? "critical" : k >= 2 ? "warn" : "normal"
        return PyObject([
            ("level", .double(level)), ("ram_gb", .double(ram)), ("swap_gb", .double(Double(Kernel.swap().used) / Sched.GB)),
            ("pressure", .string(pressure)),
        ])
    }

    public func guardSnapshot() -> PyObject {
        if let snapshot { return snapshot }
        let s = Files.json(state + "/devguard-state.json", fallback: .null)["snapshot"] ?? .null
        return s.object ?? PyObject()
    }

    public func guardMaxServerGB() -> PyJSON? {
        Files.json(state + "/devguard.json", fallback: .null).object?["max_server_gb"]
    }

    public func nativeScan() -> (gb: Double, active: Bool, pids: Set<pid_t>) {
        if let data = Self.fake(), let native = data["native"] {
            let n = native.object ?? PyObject()
            return (n["gb"]?.double ?? 0, n["active"]?.truthy ?? false, [])
        }
        return scan
    }

    /// alive(pid): kill(pid, 0) answers (EPERM counts as alive)
    public func alive(_ pid: PyJSON?) -> Bool {
        guard let pid, pid.truthy, let p = pid.int else { return false }
        if kill(pid_t(clamping: p), 0) == 0 { return true }
        return errno == EPERM
    }

    public func descendants(_ pid: pid_t) -> Set<pid_t> { SchedNative.descendants(pid) }

    public func killpg(_ pgid: pid_t, _ signal: Int32) -> Bool { Darwin.killpg(pgid, signal) == 0 }

    public func day() -> String {
        var t = time_t(now)
        var tm = tm()
        localtime_r(&t, &tm)
        var buffer = [CChar](repeating: 0, count: 16)
        strftime(&buffer, buffer.count, "%Y-%m-%d", &tm)
        return cText(buffer)
    }
}

/// sched.py's process readers without ctypes.
public enum SchedNative {
    /// proc_listpids(kind, arg)
    static func listpids(_ kind: Int32, _ arg: UInt32) -> [pid_t] {
        var buffer = [pid_t](repeating: 0, count: 2048)
        let n = buffer.withUnsafeMutableBytes { proc_listpids(UInt32(kind), arg, $0.baseAddress, Int32($0.count)) }
        guard n > 0 else { return [] }
        return buffer.prefix(Int(n) / MemoryLayout<pid_t>.size).filter { $0 > 0 }
    }

    /// descendants(root) through PROC_PPID_ONLY
    public static func descendants(_ root: pid_t) -> Set<pid_t> {
        var seen: Set<pid_t> = []
        var todo = [root]
        while let pid = todo.popLast() {
            if seen.insert(pid).inserted { todo += listpids(PROC_PPID_ONLY, UInt32(pid)) }
        }
        return seen
    }

    /// job_pids(root): the tree under root and its whole process group
    public static func jobPids(_ root: pid_t) -> Set<pid_t> {
        descendants(root).union(listpids(PROC_PGRP_ONLY, UInt32(root)))
    }

    /// pids_usage(pids): (GB, CPU s) of the ones still alive (rusage v0's footprint and times)
    public static func usage(_ pids: Set<pid_t>) -> (gb: Double, cpu: Double) {
        var footprint: UInt64 = 0
        var cpu = 0.0
        for pid in pids {
            var info = rusage_info_v0()
            let ok = withUnsafeMutablePointer(to: &info) {
                $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V0, $0) }
            }
            guard ok == 0 else { continue }
            footprint &+= info.ri_phys_footprint
            cpu += Double(info.ri_user_time &+ info.ri_system_time) * Kernel.nsPerTick / 1e9
        }
        return (Double(footprint) / Sched.GB, cpu)
    }

    /// proc_name(pid)
    static func name(_ pid: pid_t) -> String {
        var buffer = [CChar](repeating: 0, count: 64)
        let n = proc_name(pid, &buffer, 64)
        return n > 0 ? cText(buffer) : ""
    }

    static func bsd(_ pid: pid_t) -> proc_bsdinfo? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        return proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size ? info : nil
    }

    static let drivers: Set<String> = ["xcodebuild", "swift-build"]
    static let services: Set<String> = ["XCBBuildService", "SWBBuildService"]
    static let compilers: Set<String> = [
        "swift-frontend", "swiftc", "swift-driver", "clang", "clang++", "ld", "libtool", "ibtool", "actool", "dsymutil", "codesign",
    ]
    static let xcodeInfo: Set<String> = [
        "-checkFirstLaunchStatus", "-create-xcframework", "-deleteComponent", "-downloadAllPlatforms", "-downloadComponent",
        "-downloadPlatform", "-exportArchive", "-exportLocalizations", "-exportNotarizedApp", "-find-executable", "-find-library",
        "-help", "-importComponent", "-importLocalizations", "-importPlatform", "-license", "-list", "-resolvePackageDependencies",
        "-runFirstLaunch", "-showBuildSettings", "-showBuildSettingsForIndex", "-showComponent", "-showTestPlans",
        "-showdestinations", "-showsdks", "-usage", "-version",
    ]
    static let xcodeActions: Set<String> = [
        "build", "test", "archive", "analyze", "build-for-testing", "test-without-building", "docbuild", "install", "installsrc", "clean",
    ]
    /// sched.py binds HELP_FLAGS twice; parse_native reads the module global at call time, so the
    /// second (the classifier's) is the one that counts
    static let helpFlags: Set<String> = ["--help", "-h", "--version", "-v", "-V", "help"]

    /// xcode_compiles(argv): parse_native's xcodebuild detail is not test-without-building/installsrc
    static func xcodeCompiles(_ argv: [String]?) -> Bool {
        var words = argv.flatMap { $0.isEmpty ? nil : $0 } ?? ["xcodebuild"]
        while true {
            let prog = basename(words[0])
            let args = Array(words.dropFirst())
            if ["arch", "npx", "bunx"].contains(prog) || (prog == "bundle" && args.first == "exec") {
                var rest = prog == "bundle" ? Array(args.dropFirst()) : args
                while let first = rest.first, first.pyStarts("-") {
                    rest = (first == "-p" || first == "--package") ? Array(rest.dropFirst(2)) : Array(rest.dropFirst())
                }
                guard !rest.isEmpty else { return false }
                words = rest
                continue
            }
            if args.contains(where: helpFlags.contains) { return false }
            if prog == "xcrun" {
                guard args.first == "xcodebuild" else { return false }
                words = args
                continue
            }
            guard prog == "xcodebuild" else { return true }
            let actions = args.filter(xcodeActions.contains)
            if !xcodeInfo.isDisjoint(with: args) || (!actions.isEmpty && Set(actions) == ["clean"]) { return false }
            let detail = actions.first { $0 != "clean" } ?? "build"
            return detail != "test-without-building" && detail != "installsrc"
        }
    }

    /// native_scan(sims_since, builds)
    public static func scan(simsSince: Double? = nil, builds: Bool = true) -> (gb: Double, active: Bool, pids: Set<pid_t>) {
        var roots: [pid_t] = []
        var serviceList: [pid_t] = []
        var idle: Set<pid_t> = []
        var sims: [pid_t] = []
        for pid in listpids(1, 0) {  // PROC_ALL_PIDS
            let n = name(pid)
            if !builds && n != "launchd_sim" { continue }
            if n == "xcodebuild" {
                if xcodeCompiles(Args.read(pid).map { $0.argv.map(Text.decode) }) { roots.append(pid) } else { idle.insert(pid) }
            } else if drivers.contains(n) {
                roots.append(pid)
            } else if services.contains(n), !listpids(PROC_PPID_ONLY, UInt32(pid)).isEmpty {
                serviceList.append(pid)
            } else if let since = simsSince, n == "launchd_sim", let info = bsd(pid), info.pbi_start_tvsec != 0,
                      Double(info.pbi_start_tvsec) >= since - 2
            {
                sims.append(pid)
            }
        }
        roots += serviceList.filter { p in
            guard !idle.contains(pid_t(bitPattern: bsd(p)?.pbi_ppid ?? 0)) else { return false }
            return descendants(p).subtracting([p]).contains { compilers.contains(name($0)) }
        }
        var tree: Set<pid_t> = []
        for root in roots + sims { tree.formUnion(descendants(root)) }
        return (usage(tree).gb, !roots.isEmpty, tree)
    }

    /// StallWatch.output_size(): the sizes of the wrapper's stdout and stderr (files or pipes)
    public static func outputSize(_ pid: pid_t) -> Int64 {
        var size: Int64 = 0
        for fd: Int32 in [1, 2] {
            var info = vnode_fdinfowithpath()
            let n = proc_pidfdinfo(pid, fd, PROC_PIDFDVNODEPATHINFO, &info, Int32(MemoryLayout<vnode_fdinfowithpath>.size))
            if n == Int32(MemoryLayout<vnode_fdinfowithpath>.size) {
                size += info.pvip.vip_vi.vi_stat.vst_size
                continue
            }
            var pipe = pipe_fdinfo()
            if proc_pidfdinfo(pid, fd, PROC_PIDFDPIPEINFO, &pipe, Int32(MemoryLayout<pipe_fdinfo>.size)) == Int32(MemoryLayout<pipe_fdinfo>.size) {
                size += pipe.pipeinfo.pipe_stat.vst_size
            }
        }
        return size
    }
}
