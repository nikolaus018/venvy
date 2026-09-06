import SwiftUI

@main
struct VenvyApp: App {
    @StateObject private var servers = ServerStore()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(servers: servers)
        }
    }
}

struct RootView: View {
    @ObservedObject var servers: ServerStore
    @StateObject private var session: VenvySession
    @Environment(\.scenePhase) private var scenePhase

    init(servers: ServerStore) {
        self.servers = servers
        _session = StateObject(wrappedValue: VenvySession(servers: servers))
    }

    var body: some View {
        ZStack {
            content
            ToastOverlay(center: session.toasts)
        }
        .environmentObject(session)
        .environmentObject(servers)
        .onChange(of: scenePhase) { _, newPhase in
            session.handleScenePhase(newPhase)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch session.phase {
        case .needsServer:
            ServerSetupView(mode: .firstRun)
        case .connecting:
            ConnectingView(server: session.currentServer)
        case .needsLogin:
            LoginView(mode: .login)
        case .needsSetup:
            LoginView(mode: .firstRunSetup)
        case .ready:
            MainTabView()
        case .unreachable(let message):
            UnreachableView(message: message)
        }
    }
}

struct ConnectingView: View {
    let server: ServerConfig?

    var body: some View {
        VStack(spacing: 14) {
            ProgressView()
                .controlSize(.large)
            if let server {
                Text("Connecting to \(server.displayHost)…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemGroupedBackground))
    }
}

struct UnreachableView: View {
    let message: String
    @EnvironmentObject private var session: VenvySession
    @EnvironmentObject private var servers: ServerStore
    @State private var showingServers = false

    var body: some View {
        NavigationStack {
            EmptyStateView(
                icon: "wifi.exclamationmark",
                title: "Can't reach that server",
                message: message,
                actionTitle: "Try again",
                action: { Task { await session.refreshAuthStatus() } }
            )
            .background(Color(.systemGroupedBackground))
            .navigationTitle(session.currentServer?.name ?? "Venvy")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Servers") { showingServers = true }
                }
            }
            .sheet(isPresented: $showingServers) {
                ServerListView()
            }
        }
    }
}
