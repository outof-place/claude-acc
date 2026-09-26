import AppKit
import Observation
import ServiceManagement

/// Stan aplikacji: ostatni odczyt kont i akcja, która właśnie trwa.
@MainActor
@Observable
final class Store {
    enum Busy: Equatable {
        case switching(String)
        case loggingIn(String)

        var email: String {
            switch self {
            case .switching(let email), .loggingIn(let email): return email
            }
        }
    }

    struct Notice: Equatable {
        let text: String
        let isError: Bool
    }

    private(set) var snapshot: Snapshot?
    private(set) var refreshing = false
    private(set) var problem: String?
    private(set) var busy: Busy?
    var notice: Notice?
    private(set) var launchAtLogin = false

    @ObservationIgnored private var timer: Timer?
    @ObservationIgnored private var loginProcess: Process?
    @ObservationIgnored private var loginCancelled = false
    @ObservationIgnored private var refreshAgain = false

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    /// Podgląd do renderowania panelu do pliku: bez timera i bez rejestracji.
    init(preview: Snapshot) {
        snapshot = preview
    }

    init() {
        // zamknięcie aplikacji w trakcie logowania nie może zostawić skryptu z `claude` w tle
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.cancelLogin() }
        }
        registerLoginItemOnFirstRun()
        launchAtLogin = SMAppService.mainApp.status == .enabled
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    // MARK: odczyt

    func refresh() async {
        if refreshing {
            refreshAgain = true  // np. po przełączeniu konta: odczyt w toku ma już stare dane
            return
        }
        refreshing = true
        defer { refreshing = false }
        repeat {
            refreshAgain = false
            let result = await CLI.run(["status", "--json"])
            guard result.status == 0 else {
                problem = result.message.isEmpty ? "Nie udało się odczytać limitów" : result.message
                continue
            }
            do {
                snapshot = try Self.decoder.decode(Snapshot.self, from: Data(result.stdout.utf8))
                problem = nil
            } catch {
                problem = "Skrypt zwrócił nieczytelne dane: \(error.localizedDescription)"
            }
        } while refreshAgain
    }

    /// Otwarcie panelu odświeża dane, gdy są starsze niż pół minuty.
    func refreshIfStale() {
        let age = Date().timeIntervalSince1970 - (snapshot?.generatedAt ?? 0)
        if age > 30 { Task { await refresh() } }
    }

    // MARK: akcje

    func switchTo(_ account: Account) async {
        guard busy == nil else { return }
        busy = .switching(account.email)
        notice = nil
        let result = await CLI.run(["switch", account.email])
        busy = nil
        notice = result.status == 0
            ? Notice(text: "Przełączono na \(account.email)", isError: false)
            : Notice(text: result.message, isError: true)
        await refresh()
    }

    func login(_ account: Account) async {
        guard busy == nil else { return }
        let process = CLI.process(["login", account.email])
        loginProcess = process
        loginCancelled = false
        busy = .loggingIn(account.email)
        notice = nil
        let result = await CLI.run(process)
        loginProcess = nil
        busy = nil
        // po powrocie z przeglądarki skrypt ignoruje Anuluj i kończy zapis, więc liczy się wynik
        if result.status == 0 {
            notice = Notice(text: "Zalogowano \(account.email)", isError: false)
        } else if loginCancelled {
            notice = Notice(text: "Logowanie anulowane", isError: false)
        } else {
            notice = Notice(text: result.message.isEmpty ? "Logowanie nieudane" : result.message, isError: true)
        }
        await refresh()
    }

    func cancelLogin() {
        guard let process = loginProcess, process.isRunning else { return }
        loginCancelled = true
        process.terminate()  // skrypt na SIGTERM zamyka też `claude auth login`
    }

    // MARK: start przy logowaniu

    func setLaunchAtLogin(_ on: Bool) {
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            notice = Notice(text: "Nie udało się zmienić startu przy logowaniu: \(error.localizedDescription)", isError: true)
        }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    /// Aplikacja ma działać bez pamiętania o niej, więc przy pierwszym
    /// uruchomieniu sama dopisuje się do rzeczy otwieranych przy logowaniu.
    private func registerLoginItemOnFirstRun() {
        let key = "didRegisterLoginItem"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        try? SMAppService.mainApp.register()
        UserDefaults.standard.set(true, forKey: key)
    }
}
