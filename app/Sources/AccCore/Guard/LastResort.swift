// lastresort.stage and lastresort.settings: the memory brake's stage from the kernel's signals. The
// brake's victims (lastresort.reap) stay in Python: when the stage calls for one, the native guard
// hands the tick to `acc.py devguard once`, which picks and kills exactly as before.

public enum LastResort {
    public static let defaults = PyObject([
        ("compressor_tight_percent", 40),
        ("compressor_brake_percent", 50),
        ("compressor_emergency_percent", 60),
        ("segments_brake_percent", 60),
        ("segments_emergency_percent", 80),
        ("swap_tight_percent", 12),
        ("swap_brake_percent", 20),
        ("swap_emergency_percent", 30),
        ("swap_growth_tight_gb", 0.5),
        ("swap_growth_brake_gb", 1.0),
        ("swap_growth_emergency_gb", 2.0),
        ("runaway_percent", 50),
        ("brake_cooldown_seconds", 20),
        ("emergency_cooldown_seconds", 5),
        ("brake_grace_seconds", 8),
        ("emergency_grace_seconds", 3),
    ])

    /// settings(cfg): the defaults with cfg["brake"]'s known keys over them
    public static func settings(_ cfg: GuardConfig) -> PyObject {
        var out = defaults
        if let brake = cfg["brake"].object {
            for (k, v) in brake where defaults[k] != nil { out[k] = v }
        }
        return out
    }

    public static func stage(
        ram: Int, compressed: Int, segments: Int?, segmentsLimit: Int?, swapUsed: Int, swapGrowth: Int, kernel: Int,
        available: Int?, guardLevel: Int, cfg: GuardConfig
    ) -> (Int, [String]) {
        let s = settings(cfg)
        func v(_ k: String) -> Double { s[k]?.double ?? 0 }
        let ramD = Double(ram == 0 ? 16 * GuardText.GB : ram)
        let comp = Double(compressed) / ramD * 100
        let limit = segmentsLimit ?? 0
        let segs = limit != 0 ? Double(segments ?? 0) / Double(limit) * 100 : 0
        let swap = Double(swapUsed) / ramD * 100
        let growth = Double(swapGrowth) / Double(GuardText.GB)
        let k = kernel == 0 ? 1 : kernel
        func gb(_ pct: Double) -> String { GuardText.fixed(pct / 100 * ramD / Double(GuardText.GB), 1) + " GB" }
        let f0 = { (x: Double) in GuardText.fixed(x, 0) }
        let f1 = { (x: Double) in GuardText.fixed(x, 1) }
        let rules: [(Int, Bool, () -> String)] = [
            (3, comp >= v("compressor_emergency_percent"), { "kompresor \(gb(comp)) (\(f0(comp))% RAM)" }),
            (3, segs >= v("segments_emergency_percent"), { "segmenty kompresora \(f0(segs))% limitu" }),
            (3, swap >= v("swap_emergency_percent") && growth >= v("swap_growth_emergency_gb"), { "swap \(gb(swap)), +\(f1(growth)) GB w 2 min" }),
            (3, k >= 4 && growth >= v("swap_growth_emergency_gb"), { "jądro: presja krytyczna, swap +\(f1(growth)) GB w 2 min" }),
            (2, comp >= v("compressor_brake_percent"), { "kompresor \(gb(comp)) (\(f0(comp))% RAM)" }),
            (2, segs >= v("segments_brake_percent"), { "segmenty kompresora \(f0(segs))% limitu" }),
            (2, swap >= v("swap_brake_percent") && growth >= v("swap_growth_brake_gb"), { "swap \(gb(swap)), +\(f1(growth)) GB w 2 min" }),
            (2, k >= 4, { "jądro: presja krytyczna" }),
            (2, guardLevel >= 2, { "strażnik: presja krytyczna" }),
            (1, comp >= v("compressor_tight_percent"), { "kompresor \(gb(comp)) (\(f0(comp))% RAM)" }),
            (1, swap >= v("swap_tight_percent") && growth >= v("swap_growth_tight_gb"), { "swap \(gb(swap)), +\(f1(growth)) GB w 2 min" }),
            (1, k >= 2 && growth >= v("swap_growth_tight_gb"), { "jądro: ostrzeżenie, swap +\(f1(growth)) GB w 2 min" }),
        ]
        for level in [3, 2, 1] {
            let reasons = rules.filter { $0.0 == level && $0.1 }.map { $0.2() }
            if !reasons.isEmpty { return (level, reasons) }
        }
        return (0, [])
    }
}
