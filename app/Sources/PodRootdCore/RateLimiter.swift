import PodRootdProtocol

/// Token buckets per caller and verb kind (docs/pod-rootd.md, "Rate limits").
public struct RateLimiter {
    public struct Limit: Sendable {
        public var burst: Double
        public var perSecond: Double

        public init(burst: Double, perSecond: Double) {
            self.burst = burst
            self.perSecond = perSecond
        }
    }

    public static let standard: [VerbKind: Limit] = [
        .read: Limit(burst: 20, perSecond: 10),
        .fans: Limit(burst: 3, perSecond: 0.5),
        .power: Limit(burst: 3, perSecond: 0.5),
        // the hotspot controller retunes up to ~10 times a second
        .shaper: Limit(burst: 40, perSecond: 20),
        .config: Limit(burst: 3, perSecond: 0.1),
        .system: Limit(burst: 2, perSecond: 0.1),
    ]

    private struct Bucket {
        var tokens: Double
        var at: Double
    }

    private let limits: [VerbKind: Limit]
    private var buckets: [String: Bucket] = [:]

    public init(limits: [VerbKind: Limit] = RateLimiter.standard) { self.limits = limits }

    /// Takes a token; nil when there was one, otherwise the seconds until there is.
    public mutating func take(_ kind: VerbKind, for caller: Caller, now: Double) -> Double? {
        guard let limit = limits[kind] else { return nil }
        let key = "\(caller.rawValue)/\(kind.rawValue)"
        var bucket = buckets[key] ?? Bucket(tokens: limit.burst, at: now)
        bucket.tokens = min(limit.burst, bucket.tokens + max(0, now - bucket.at) * limit.perSecond)
        bucket.at = now
        defer { buckets[key] = bucket }
        guard bucket.tokens >= 1 else { return (1 - bucket.tokens) / limit.perSecond }
        bucket.tokens -= 1
        return nil
    }
}
