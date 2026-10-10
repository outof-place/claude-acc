// devguard_core's and lastresort's patterns, character for character, compiled once (PyRegex gives
// them Python's meaning on ICU). The host apps (Orca, Pod) come from orcahost.app_pattern.

public enum GuardPatterns {
    /// orcahost.APPS for the given app names ("Orca", "Pod")
    public static func apps(_ names: [String]) -> String {
        names.map { #"(?<![\w-])"# + PyRegex.escape($0) + #"\.app"# }.joined(separator: "|")
    }

    public static let serverKinds: [(String, PyRegex)] = [
        ("next", PyRegex(#"/next(/dist/bin/next)?\s+dev\b"#, anyOf: ["/next"])),
        ("vite", PyRegex(#"/vite(/bin/vite\.js)?(\s+(dev|serve)\b|\s+--|\s*$)"#, anyOf: ["/vite"])),
        ("expo", PyRegex(#"/expo(/bin/cli)?\s+start\b"#, anyOf: ["/expo"])),
        ("webpack", PyRegex(#"/webpack(-cli)?(/bin/cli\.js)?\s+serve\b"#, anyOf: ["/webpack"])),
        ("astro", PyRegex(#"/astro(\.js)?\s+dev\b"#, anyOf: ["/astro"])),
        ("storybook", PyRegex(#"/storybook(/bin/index\.c?js)?\s+dev\b"#, anyOf: ["/storybook"])),
        ("nuxt", PyRegex(#"/nuxi?(\.mjs)?\s+dev\b"#, anyOf: ["/nux"])),
    ]
    public static let launcher = PyRegex(
        #"^(\S*/)?(pnpm|npm|npx|yarn|bun|bunx|rtk|turbo|corepack|nohup)(\s|$)"#
            + #"|^(\S*/)?node\s+\S*/(pnpm|npm|npx|yarn|turbo)(\.c?js)?(\s|$)"#
            + #"|^(/bin/)?(ba|z)?sh\s+-c\s"#,
        anyOf: ["pnpm", "npm", "npx", "yarn", "bun", "rtk", "turbo", "corepack", "nohup", "sh"])
    public static let shell = PyRegex(#"^-?(\S*/)?(zsh|bash|fish|sh)(\s+-[a-z]+)*\s*$"#, anyOf: ["sh", "fish"])
    public static let agent = PyRegex(#"(^|/)(claude|codex)(\s|$)"#, anyOf: ["claude", "codex"])
    public static let browser = PyRegex(
        #"Brave Browser|Google Chrome(?! for Testing)|Safari|com\.apple\.WebKit|firefox|Arc\.app|Microsoft Edge|Chromium|Vivaldi|Opera"#,
        anyOf: ["Brave Browser", "Google Chrome", "Safari", "com.apple.WebKit", "firefox", "Arc.app", "Microsoft Edge", "Chromium", "Vivaldi", "Opera"])
    public static let headless = PyRegex(
        #"Chrome for Testing|HeadlessChrome|headless_shell|chrome-headless-shell|ms-playwright|puppeteer"#,
        anyOf: ["Chrome for Testing", "HeadlessChrome", "headless_shell", "chrome-headless-shell", "ms-playwright", "puppeteer"])
    public static let udid = #"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}"#
    public static let udidRx = PyRegex(udid)
    public static let simDevice = PyRegex("/CoreSimulator/Devices/(" + udid + ")/", anyOf: ["/CoreSimulator/Devices/"])
    public static let launchdSim = PyRegex(#"(^|/)launchd_sim(\s|$)"#, anyOf: ["launchd_sim"])
    public static let serveSim = PyRegex(#"(^|/)serve-sim(\s|$)"#, anyOf: ["serve-sim"])
    public static let simctlBooted = PyRegex(#"(^|/|\s)simctl\s.*\bbooted\b"#, anyOf: ["simctl"])

    public static let families: [(String, PyRegex)] = [
        ("headless", headless),
        ("simulators", PyRegex(#"launchd_sim|CoreSimulator|Simulator\.app|SimulatorTrampoline|/Developer/CoreSimulator/"#, anyOf: ["launchd_sim", "CoreSimulator", "Simulator.app", "SimulatorTrampoline"])),
        ("metro", PyRegex(#"/expo(/bin/cli)?\s+start\b|/metro\b|react-native\s+start\b"#, anyOf: ["/expo", "/metro", "react-native"])),
        ("watchers", PyRegex(#"--watch(All)?\b|(^|/)nodemon(\s|$)|(^|\s)-w(\s|$)|/vitest(\.mjs)?\s+(watch|dev)\b"#, anyOf: ["--watch", "nodemon", "-w", "/vitest"])),
        ("lsp", PyRegex(#"(^|/)gopls(\s|$)|tsserver\.js|typescript-language-server|rust-analyzer|sourcekit-lsp|clangd|pyright-langserver"#, anyOf: ["gopls", "tsserver.js", "typescript-language-server", "rust-analyzer", "sourcekit-lsp", "clangd", "pyright-langserver"])),
        ("docker", PyRegex(#"com\.docker|Docker\.app|com\.apple\.Virtualization"#, anyOf: ["com.docker", "Docker.app", "com.apple.Virtualization"])),
        ("git", PyRegex(#"^(\S*/)?git(\s|$)"#, anyOf: ["git"])),
        ("agents", agent),
        ("browsers", browser),
    ]
    public static let longLived: Set<String> = ["dev", "metro", "watchers", "simulators", "headless", "lsp", "docker"]

    /// The patterns that depend on the host apps' names.
    public struct Host: Sendable {
        public let apps: PyRegex
        public let sacred: PyRegex

        public init(appNames: [String]) {
            let fragment = GuardPatterns.apps(appNames)
            apps = PyRegex(fragment, anyOf: [".app"])
            sacred = PyRegex(
                #"(^|/)(claude|codex|login|launchd)(\s|$)|"# + fragment + #"|^-?(\S*/)?(zsh|bash|fish)(\s|$)"#,
                anyOf: ["claude", "codex", "login", "launchd", ".app", "zsh", "bash", "fish"])
        }
    }
}

/// What the guard's patterns need to know about the hosts, set once at start.
public enum GuardContext {
    nonisolated(unsafe) public static var host = GuardPatterns.Host(appNames: ["Orca", "Pod"])
}
