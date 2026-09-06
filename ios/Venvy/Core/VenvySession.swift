import Foundation
import SwiftUI

/// Where the app should be right now.
enum SessionPhase: Equatable {
    /// No server saved yet — show onboarding.
    case needsServer
    /// Contacting the server to find out whether anyone is signed in.
    case connecting
    /// Server reachable, nobody signed in.
    case needsLogin
    /// Server reachable and has no users at all — offer first-run setup.
    case needsSetup
    /// Signed in.
    case ready
    /// Couldn't reach the server at all.
    case unreachable(String)
}

/// Owns the connection to the selected server: auth state, the app list, and
/// the polling that keeps statuses fresh.
@MainActor
final class VenvySession: ObservableObject {
    @Published private(set) var phase: SessionPhase = .needsServer
    @Published private(set) var client: APIClient?
    @Published private(set) var username: String = ""
    @Published private(set) var role: Role = .viewer
    @Published private(set) var totpEnabled: Bool = false

    @Published private(set) var apps: [HostedApp] = []
    @Published private(set) var appsError: String?
    @Published private(set) var isLoadingApps = false
    @Published private(set) var hasLoadedAppsOnce = false

    @Published var serverIPv4: String?

    let toasts = ToastCenter()
    private let servers: ServerStore
    private var pollTask: Task<Void, Never>?
    /// App ids with a control action in flight, so the UI can show a spinner
    /// while the server settles asynchronously.
    @Published private(set) var pendingControl: Set<Int> = []

    init(servers: ServerStore) {
        self.servers = servers
        if let s = servers.selected {
            connect(to: s)
        } else if let first = servers.servers.first {
            servers.select(first)
            connect(to: first)
        } else {
            phase = .needsServer
        }
    }

    var currentServer: ServerConfig? { servers.selected }
    var canOperate: Bool { role.canOperate }
    var isAdmin: Bool { role.isAdmin }

    // MARK: - Connection lifecycle

    func connect(to server: ServerConfig) {
        stopPolling()
        servers.select(server)
        guard let url = server.url else {
            phase = .unreachable("That server's address is invalid.")
            return
        }
        client = APIClient(baseURL: url)
        apps = []
        hasLoadedAppsOnce = false
        Task { await refreshAuthStatus() }
    }

    /// Asks the server who we are. Also mints the csrf_token cookie.
    func refreshAuthStatus() async {
        guard let client else { phase = .needsServer; return }
        phase = .connecting
        do {
            let status = try await client.authStatus()
            totpEnabled = status.totpEnabled ?? false
            if status.authenticated {
                username = status.username ?? ""
                role = status.role ?? .viewer
                phase = .ready
                if let id = currentServer?.id { servers.markConnected(id) }
                await loadApps()
                startPolling()
            } else if !status.userExists {
                phase = .needsSetup
            } else {
                phase = .needsLogin
            }
        } catch let error as APIError {
            phase = error.isConnectivity
                ? .unreachable(error.localizedDescription)
                : .needsLogin
        } catch {
            phase = .unreachable(error.localizedDescription)
        }
    }

    func login(username u: String, password: String, totpCode: String?) async throws {
        guard let client else { throw APIError.transport("No server selected.") }
        let res = try await client.login(username: u, password: password, totpCode: totpCode)
        username = res.username
        role = res.role
        phase = .ready
        if var server = currentServer {
            server.username = u
            servers.upsert(server)
            servers.markConnected(server.id)
        }
        let status = try? await client.authStatus()
        totpEnabled = status?.totpEnabled ?? false
        await loadApps()
        startPolling()
    }

    func firstRunSetup(username u: String, password: String) async throws {
        guard let client else { throw APIError.transport("No server selected.") }
        _ = try await client.setup(username: u, password: password)
        // Setup logs the account in and returns the session cookie.
        username = u
        role = .admin
        phase = .ready
        if var server = currentServer {
            server.username = u
            servers.upsert(server)
        }
        await loadApps()
        startPolling()
    }

    func logout() async {
        stopPolling()
        try? await client?.logout()
        client?.clearCookies()
        apps = []
        hasLoadedAppsOnce = false
        username = ""
        role = .viewer
        phase = .needsLogin
    }

    /// Called when any request comes back 401 mid-session.
    func handleAuthFailure() {
        stopPolling()
        client?.clearCookies()
        apps = []
        hasLoadedAppsOnce = false
        phase = .needsLogin
        toasts.error("Your session expired. Sign in again.")
    }

    /// Central error funnel: routes 401s to re-auth and surfaces the rest.
    func report(_ error: Error, context: String? = nil) {
        if let api = error as? APIError {
            if api.isAuthFailure {
                handleAuthFailure()
                return
            }
            let msg = context.map { "\($0): \(api.localizedDescription)" } ?? api.localizedDescription
            toasts.error(msg)
        } else {
            toasts.error(context.map { "\($0): \(error.localizedDescription)" } ?? error.localizedDescription)
        }
    }

    // MARK: - Apps

    func loadApps(showSpinner: Bool = true) async {
        guard let client, phase == .ready else { return }
        if showSpinner && !hasLoadedAppsOnce { isLoadingApps = true }
        do {
            let fetched = try await client.apps()
            apps = fetched.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            appsError = nil
            hasLoadedAppsOnce = true
            // A settled status means any queued control action has landed.
            pendingControl = pendingControl.filter { id in
                guard let app = apps.first(where: { $0.id == id }) else { return false }
                return app.status.isTransitional
            }
        } catch let error as APIError {
            if error.isAuthFailure { handleAuthFailure(); return }
            appsError = error.localizedDescription
        } catch {
            appsError = error.localizedDescription
        }
        isLoadingApps = false
    }

    func app(id: Int) -> HostedApp? { apps.first { $0.id == id } }

    func control(app: HostedApp, action: String) async {
        guard let client else { return }
        pendingControl.insert(app.id)
        do {
            try await client.control(id: app.id, action: action)
            toasts.info("\(action.capitalized)ing \(app.name)…")
            // Poll a few times so the settled state shows up promptly.
            await pollUntilSettled(appID: app.id)
        } catch {
            pendingControl.remove(app.id)
            report(error, context: "Couldn't \(action) \(app.name)")
        }
    }

    private func pollUntilSettled(appID: Int) async {
        for _ in 0..<12 {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if Task.isCancelled { return }
            await loadApps(showSpinner: false)
            if let app = app(id: appID), !app.status.isTransitional {
                pendingControl.remove(appID)
                return
            }
        }
        pendingControl.remove(appID)
    }

    func deleteApp(_ app: HostedApp) async {
        guard let client else { return }
        do {
            try await client.deleteApp(id: app.id)
            apps.removeAll { $0.id == app.id }
            toasts.success("Deleted \(app.name).")
        } catch {
            report(error, context: "Couldn't delete \(app.name)")
        }
    }

    @discardableResult
    func updateApp(id: Int, _ update: AppUpdateRequest) async -> HostedApp? {
        guard let client else { return nil }
        do {
            let updated = try await client.updateApp(id: id, update)
            if let idx = apps.firstIndex(where: { $0.id == id }) {
                apps[idx] = updated
            }
            return updated
        } catch {
            report(error, context: "Couldn't save changes")
            return nil
        }
    }

    func loadServerIPv4() async {
        guard let client, phase == .ready, serverIPv4 == nil else { return }
        serverIPv4 = try? await client.systemInfo().ipv4
    }

    // MARK: - Polling

    /// Refreshes the app list on a slow cadence while the app is foregrounded,
    /// so statuses and metrics stay roughly live without hammering the server.
    func startPolling(interval: TimeInterval = 6) {
        stopPolling()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                if Task.isCancelled { return }
                guard let self else { return }
                if self.phase == .ready {
                    await self.loadApps(showSpinner: false)
                } else {
                    return
                }
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func handleScenePhase(_ scene: ScenePhase) {
        switch scene {
        case .active:
            if phase == .ready {
                Task {
                    await loadApps(showSpinner: false)
                    startPolling()
                }
            }
        case .background, .inactive:
            stopPolling()
        @unknown default:
            break
        }
    }
}
