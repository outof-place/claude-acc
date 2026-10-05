import AppKit
import SwiftUI

// MARK: - Model (`sched/state.json`, written by sched.py; contract in docs/sched.md)

/// The Go scheduler's state: what builds and tests run, who waits and why, where memory goes.
struct SchedState: Decodable {
    struct Host: Decodable {
        let ramGb: Double?
    }

    struct Config: Decodable {
        let headroomGb: Double?
    }

    struct Memory: Decodable {
        let levelPct: Double?
        let availableGb: Double?
        let headroomGb: Double?
        let devserverReserveGb: Double?
        let jobsNowGb: Double?
        let reservedGb: Double?
        let othersGb: Double?
        let freeForAdmissionGb: Double?
        let idleMaxGb: Double?
        let swapUsedGb: Double?
        let pressure: String?
    }

    struct Agent: Decodable {
        let name: String?
        let worktree: String?
    }

    struct Route: Decodable {
        let choice: String?
        let why: String?
        let text: String?
        let costUsd: Double?
    }

    struct Depot: Decodable {
        let target: String?
        let job: String?
        let cores: Int?
        let runId: String?
        let url: String?
        let costUsd: Double?
    }

    struct Reason: Decodable {
        let code: String?
        let needGb: Double?
        let freeGb: Double?
        let text: String?
    }

    struct Job: Decodable, Identifiable {
        let id: String
        let kind: String?
        let label: String
        let repo: String?
        let agent: Agent?
        let `where`: String?
        let route: Route?
        let p: Int?
        let memPredictedGb: Double?
        let count1Dropped: Bool?
        let elapsedS: Double?
        let etaS: Double?
        let progress: Double?
        let memNowGb: Double?
        let paused: Bool?
        let pauseReason: String?
        let depot: Depot?
        let position: Int?
        let waitedS: Double?
        let etaStartS: Double?
        let reason: Reason?

        var onDepot: Bool { self.where == "depot" }
    }

    struct Recent: Decodable, Identifiable {
        let id: String
        let label: String
        let `where`: String?
        let rc: Int?
        let finishedAt: Double?
        let wallS: Double?
        let peakGb: Double?
        let costUsd: Double?
        let routeText: String?
    }

    struct Today: Decodable {
        let jobsLocal: Int?
        let jobsDepot: Int?
        let waitSavedS: Double?
        let depotCostUsd: Double?
        let localKeptUsd: Double?
        let overtakes: Int?
    }

    let updatedAt: Double?
    let idleSince: Double?
    let host: Host?
    let config: Config?
    let memory: Memory?
    let running: [Job]
    let queue: [Job]
    let recent: [Recent]
    let today: Today?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        updatedAt = try c.decodeIfPresent(Double.self, forKey: .updatedAt)
        idleSince = try c.decodeIfPresent(Double.self, forKey: .idleSince)
        host = try c.decodeIfPresent(Host.self, forKey: .host)
        config = try c.decodeIfPresent(Config.self, forKey: .config)
        memory = try c.decodeIfPresent(Memory.self, forKey: .memory)
        running = try c.decodeIfPresent([Job].self, forKey: .running) ?? []
        queue = try c.decodeIfPresent([Job].self, forKey: .queue) ?? []
        recent = try c.decodeIfPresent([Recent].self, forKey: .recent) ?? []
        today = try c.decodeIfPresent(Today.self, forKey: .today)
    }

    private enum CodingKeys: String, CodingKey {
        case updatedAt, idleSince, host, config, memory, running, queue, recent, today
    }

    var busy: Bool { !running.isEmpty || !queue.isEmpty }
}

// MARK: - Card

/// Agents' Go builds and tests, admitted by memory instead of one lock: what runs where, who
/// waits for what, and what that saved today.
struct BuildsCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Builds", symbol: "square.stack.3d.up.fill") {
            if let state = store.sched {
                VStack(alignment: .leading, spacing: 12) {
                    MemoryLane(lane: MemoryLane.Lane(state: state, liveLevel: busy(state) ? nil : store.memoryLevel))
                    if busy(state) {
                        CardScroll {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(state.running) { job in
                                    RunningRow(job: job).transition(.blurReplace)
                                }
                                ForEach(state.queue) { job in
                                    QueuedRow(job: job).transition(.blurReplace)
                                }
                            }
                        }
                    } else {
                        RecentRuns(recent: Array(state.recent.prefix(4)), now: now)
                    }
                    if let today = state.today {
                        TodayLine(today: today)
                    }
                }
                .animation(.smooth, value: state.running.map(\.id) + state.queue.map(\.id))
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Label("The build scheduler hasn't run yet", systemImage: "square.stack.3d.up")
                        .font(.callout)
                    Text("Agents' go build, vet, test and lint show up here once sched.py admits them.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        } accessory: {
            if let state = store.sched {
                status(state)
            }
        }
    }

    /// Busy, but the file stopped moving: the scheduler died mid-run.
    private func busy(_ state: SchedState) -> Bool {
        state.busy && now.timeIntervalSince1970 - (state.updatedAt ?? 0) < 30
    }

    @ViewBuilder private func status(_ state: SchedState) -> some View {
        if busy(state) {
            HStack(spacing: 4) {
                Chip("\(state.running.count) running", tint: Format.violet)
                if !state.queue.isEmpty {
                    Chip("\(state.queue.count) queued", tint: .orange)
                }
            }
        } else {
            Text("Idle").font(.caption).foregroundStyle(.secondary)
        }
    }
}

// MARK: - Memory lane

/// Where the Mac's memory goes, left to right: other apps, builds now, what they'll still grow
/// into, room kept for a dev server, free for the next build, and the headroom nobody touches.
private struct MemoryLane: View {
    struct Lane {
        var others = 0.0, builds = 0.0, growing = 0.0, devServer = 0.0, free = 0.0, headroom = 0.0

        /// Busy: the scheduler's own split. Idle: nobody writes the file, so the split comes
        /// from the kernel's current level and the last known reserves.
        init(state: SchedState, liveLevel: Double?) {
            let m = state.memory
            let ram = state.host?.ramGb ?? Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824
            headroom = m?.headroomGb ?? state.config?.headroomGb ?? 4
            devServer = m?.devserverReserveGb ?? 0
            if let level = liveLevel {
                let available = level / 100 * ram
                others = ram - available
                free = max(0, available - headroom - devServer)
            } else {
                builds = m?.jobsNowGb ?? 0
                growing = m?.reservedGb ?? 0
                others = m?.othersGb ?? 0
                free = max(0, m?.freeForAdmissionGb ?? 0)
            }
        }

        var segments: [(value: Double, color: Color, name: String)] {
            [
                (others, Color.secondary.opacity(0.35), "Other apps"),
                (builds, Format.violet, "Builds now"),
                (growing, Format.violet.opacity(0.35), "Builds will still grow"),
                (devServer, Color.orange.opacity(0.6), "Kept for a dev server"),
                (free, Color.green.opacity(0.6), "Free for the next build"),
                (headroom, Color.red.opacity(0.3), "Headroom"),
            ]
        }
    }

    let lane: Lane

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            GeometryReader { geo in
                let total = max(lane.segments.reduce(0) { $0 + $1.value }, 1)
                HStack(spacing: 2) {
                    ForEach(Array(lane.segments.enumerated()), id: \.offset) { _, segment in
                        if segment.value > 0.05 {
                            RoundedRectangle(cornerRadius: 3, style: .continuous)
                                .fill(segment.color)
                                .frame(width: max(3, (geo.size.width - 10) * segment.value / total))
                                .help("\(segment.name): \(String(format: "%.1f", segment.value)) GB")
                        }
                    }
                }
            }
            .frame(height: 10)
            .animation(.smooth, value: lane.free)
            HStack(spacing: 4) {
                Text("\(String(format: "%.1f", lane.free)) GB free for builds")
                    .foregroundStyle(.primary)
                if lane.builds > 0 {
                    Text("· \(String(format: "%.1f", lane.builds)) in use")
                }
                Text("· \(String(format: "%.0f", lane.headroom)) GB headroom")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            .monospacedDigit()
            .contentTransition(.numericText())
        }
    }
}

// MARK: - Rows

private struct KindIcon: View {
    let kind: String?

    var body: some View {
        Image(systemName: Self.symbol(kind))
            .font(.caption.weight(.semibold))
            .foregroundStyle(Format.violet)
            .frame(width: 24, height: 24)
            .background(Format.violet.opacity(0.14), in: .circle)
    }

    static func symbol(_ kind: String?) -> String {
        switch kind {
        case "build": "hammer"
        case "vet": "checkmark.seal"
        case "test": "testtube.2"
        case "lint": "text.magnifyingglass"
        case "make": "wrench.and.screwdriver"
        case "generate": "gearshape.2"
        case "run": "play"
        default: "terminal"
        }
    }
}

private struct WhereChip: View {
    let job: SchedState.Job

    var body: some View {
        if job.onDepot {
            let chip = Chip("Depot \(job.depot?.cores.map { "\($0)c" } ?? "")", symbol: "cloud", tint: .blue)
            if let link = job.depot?.url, let url = URL(string: link) {
                Button { NSWorkspace.shared.open(url) } label: { chip }
                    .buttonStyle(.plain)
                    .help("Open the run on Depot")
            } else {
                chip
            }
        } else {
            Chip("Local", symbol: "laptopcomputer", tint: .secondary)
        }
    }
}

private struct RunningRow: View {
    let job: SchedState.Job

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            KindIcon(kind: job.kind)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(job.label)
                        .font(.callout.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 4)
                    WhereChip(job: job)
                }
                HStack(spacing: 8) {
                    UsageBar(fraction: job.progress ?? 0, tint: job.paused == true ? .orange : Format.violet, height: 5)
                    Text(remaining)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .contentTransition(.numericText())
                        .frame(minWidth: 46, alignment: .trailing)
                }
                Text(detail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .monospacedDigit()
                if job.paused == true {
                    Label(job.pauseReason.map { "Paused: \($0)" } ?? "Paused", systemImage: "pause.circle.fill")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                } else if let route = job.route?.text, job.onDepot || job.route?.why != "fits" {
                    Text(route)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
        }
    }

    private var remaining: String {
        guard let eta = job.etaS else { return "" }
        return eta < 1 ? "finishing" : "\(Format.age(Int(eta.rounded()))) left"
    }

    private var detail: String {
        var parts: [String] = []
        if let name = job.agent?.name { parts.append(name) }
        if let repo = job.repo { parts.append(repo) }
        if job.onDepot {
            if let cost = job.depot?.costUsd { parts.append(String(format: "$%.2f so far", cost)) }
        } else {
            if let p = job.p { parts.append("-p\(p)") }
            if let now = job.memNowGb, let predicted = job.memPredictedGb {
                parts.append(String(format: "%.1f of %.1f GB", now, predicted))
            }
            if job.count1Dropped == true { parts.append("test cache on") }
        }
        return parts.joined(separator: " · ")
    }
}

private struct QueuedRow: View {
    let job: SchedState.Job

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text("\(job.position ?? 0)")
                .font(.caption.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.orange)
                .frame(width: 24, height: 24)
                .background(Color.orange.opacity(0.14), in: .circle)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(job.label)
                        .font(.callout.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 4)
                    if let start = job.etaStartS {
                        Text("starts in \(Format.age(Int(start.rounded())))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
                if let reason = job.reason?.text {
                    Text(reason)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .lineLimit(2)
                }
                if let route = job.route?.text {
                    Text(route)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
        }
    }
}

private struct RecentRuns: View {
    let recent: [SchedState.Recent]
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if recent.isEmpty {
                Text("Nothing built yet today").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(recent) { run in
                HStack(spacing: 8) {
                    StatusDot(color: run.rc == 0 ? .green : .red, size: 6)
                    Text(run.label)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 4)
                    Text(summary(run))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .lineLimit(1)
                }
                .help(run.routeText ?? "")
            }
        }
    }

    private func summary(_ run: SchedState.Recent) -> String {
        var parts: [String] = []
        if let wall = run.wallS { parts.append(Format.age(Int(wall.rounded()))) }
        if run.where == "depot" {
            parts.append(run.costUsd.map { String(format: "Depot $%.2f", $0) } ?? "Depot")
        }
        if let at = run.finishedAt { parts.append(Format.ago(at, now: now)) }
        return parts.joined(separator: " · ")
    }
}

/// Today in one line: builds, waiting saved against the old one-at-a-time lock, Depot spend.
private struct TodayLine: View {
    let today: SchedState.Today

    var body: some View {
        let jobs = (today.jobsLocal ?? 0) + (today.jobsDepot ?? 0)
        HStack(spacing: 4) {
            Image(systemName: "clock.arrow.circlepath")
            Text(line(jobs: jobs))
                .lineLimit(1)
                .minimumScaleFactor(0.85)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .monospacedDigit()
    }

    private func line(jobs: Int) -> String {
        var parts = ["\(jobs) today"]
        if let saved = today.waitSavedS, saved >= 60 {
            parts.append("\(Format.age(Int(saved))) less waiting")
        }
        if let depot = today.depotCostUsd, depot > 0 {
            parts.append(String(format: "Depot $%.2f", depot))
        }
        if let kept = today.localKeptUsd, kept > 0 {
            parts.append(String(format: "$%.2f kept local", kept))
        }
        return parts.joined(separator: " · ")
    }
}
