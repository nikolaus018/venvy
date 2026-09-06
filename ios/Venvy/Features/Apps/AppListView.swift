import SwiftUI

struct AppListView: View {
    @EnvironmentObject private var session: VenvySession
    @State private var search = ""
    @State private var statusFilter: StatusFilter = .all
    @State private var creating = false
    @State private var showingServers = false

    enum StatusFilter: String, CaseIterable, Identifiable {
        case all, running, stopped, problems
        var id: String { rawValue }
        var label: String {
            switch self {
            case .all: return "All"
            case .running: return "Running"
            case .stopped: return "Stopped"
            case .problems: return "Errors"
            }
        }
    }

    var body: some View {
        NavigationStack {
            Group {
                if session.isLoadingApps && !session.hasLoadedAppsOnce {
                    loadingList
                } else if let error = session.appsError, session.apps.isEmpty {
                    EmptyStateView(
                        icon: "exclamationmark.triangle",
                        title: "Couldn't load your apps",
                        message: error,
                        actionTitle: "Try again",
                        action: { Task { await session.loadApps() } }
                    )
                } else if session.apps.isEmpty {
                    EmptyStateView(
                        icon: "square.stack.3d.up.slash",
                        title: "No apps yet",
                        message: session.canOperate
                            ? "Upload your first app to get started."
                            : "Nothing has been deployed to this server yet.",
                        actionTitle: session.canOperate ? "Upload an app" : nil,
                        action: session.canOperate ? { creating = true } : nil
                    )
                } else if filtered.isEmpty {
                    EmptyStateView(
                        icon: "magnifyingglass",
                        title: "No matches",
                        message: "No apps match \"\(search)\"\(statusFilter == .all ? "" : " in \(statusFilter.label)")."
                    )
                } else {
                    list
                }
            }
            .navigationTitle("Apps")
            .background(Color(.systemGroupedBackground))
            .searchable(text: $search, placement: .navigationBarDrawer(displayMode: .automatic),
                        prompt: "Search apps")
            .refreshable { await session.loadApps(showSpinner: false) }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Picker("Filter", selection: $statusFilter) {
                            ForEach(StatusFilter.allCases) { f in
                                Text(f.label).tag(f)
                            }
                        }
                        Divider()
                        Button {
                            showingServers = true
                        } label: {
                            Label("Switch server", systemImage: "server.rack")
                        }
                    } label: {
                        Image(systemName: statusFilter == .all
                              ? "line.3.horizontal.decrease.circle"
                              : "line.3.horizontal.decrease.circle.fill")
                    }
                    .accessibilityLabel("Filter and servers")
                }
                if session.canOperate {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            creating = true
                        } label: {
                            Image(systemName: "plus")
                        }
                        .accessibilityLabel("Upload an app")
                    }
                }
            }
            .sheet(isPresented: $creating) { CreateAppView() }
            .sheet(isPresented: $showingServers) { ServerListView() }
        }
    }

    private var list: some View {
        List {
            if let error = session.appsError {
                Section {
                    ErrorBanner(message: error) {
                        Task { await session.loadApps() }
                    }
                    .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                    .listRowBackground(Color.clear)
                }
            }
            Section {
                ForEach(filtered) { app in
                    NavigationLink(value: app.id) {
                        AppRow(app: app, isBusy: session.pendingControl.contains(app.id))
                    }
                    .swipeActions(edge: .leading) {
                        if session.canOperate {
                            if app.status == .running {
                                Button {
                                    Task { await session.control(app: app, action: "restart") }
                                } label: {
                                    Label("Restart", systemImage: "arrow.clockwise")
                                }
                                .tint(VenvyColor.warning)
                            } else {
                                Button {
                                    Task { await session.control(app: app, action: "start") }
                                } label: {
                                    Label("Start", systemImage: "play.fill")
                                }
                                .tint(VenvyColor.success)
                            }
                        }
                    }
                    .swipeActions(edge: .trailing) {
                        if session.canOperate && app.status != .stopped {
                            Button {
                                Task { await session.control(app: app, action: "stop") }
                            } label: {
                                Label("Stop", systemImage: "stop.fill")
                            }
                            .tint(VenvyColor.danger)
                        }
                    }
                }
            } header: {
                Text(headerText)
            }
        }
        .listStyle(.insetGrouped)
        .navigationDestination(for: Int.self) { id in
            AppDetailView(appID: id)
        }
    }

    private var loadingList: some View {
        List {
            ForEach(0..<5, id: \.self) { _ in
                HStack(spacing: 12) {
                    RoundedRectangle(cornerRadius: 8).fill(.quaternary).frame(width: 34, height: 34)
                    VStack(alignment: .leading, spacing: 6) {
                        RoundedRectangle(cornerRadius: 4).fill(.quaternary).frame(width: 140, height: 12)
                        RoundedRectangle(cornerRadius: 4).fill(.quaternary).frame(width: 90, height: 9)
                    }
                    Spacer()
                }
                .padding(.vertical, 4)
                .redacted(reason: .placeholder)
            }
        }
        .listStyle(.insetGrouped)
        .allowsHitTesting(false)
    }

    private var headerText: String {
        let n = filtered.count
        let total = session.apps.count
        let running = session.apps.filter { $0.status == .running }.count
        if n == total { return "\(total) app\(total == 1 ? "" : "s") · \(running) running" }
        return "\(n) of \(total) app\(total == 1 ? "" : "s")"
    }

    private var filtered: [HostedApp] {
        let term = search.trimmingCharacters(in: .whitespaces).lowercased()
        return session.apps.filter { app in
            let matchesSearch = term.isEmpty
                || app.name.lowercased().contains(term)
                || (app.slug ?? "").lowercased().contains(term)
                || (app.entrypoint ?? "").lowercased().contains(term)
                || app.allPorts.contains { String($0).contains(term) }
            let matchesStatus: Bool
            switch statusFilter {
            case .all: matchesStatus = true
            case .running: matchesStatus = app.status == .running
            case .stopped: matchesStatus = app.status == .stopped
            case .problems: matchesStatus = app.status == .error
            }
            return matchesSearch && matchesStatus
        }
    }
}

struct AppRow: View {
    let app: HostedApp
    var isBusy: Bool

    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(VenvyColor.status(app.status).opacity(0.14))
                    .frame(width: 36, height: 36)
                if isBusy || app.status.isTransitional {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: app.runtime.icon)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(VenvyColor.status(app.status))
                }
            }

            VStack(alignment: .leading, spacing: 3) {
                Text(app.name)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                HStack(spacing: 6) {
                    StatusBadge(status: app.status, compact: true)
                    if let port = app.port {
                        Text(":\(String(port))")
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                    }
                    if app.status == .running, app.metrics.memoryBytes > 0 {
                        Text(Fmt.bytes(app.metrics.memoryBytes))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if app.externalURL != nil {
                        Image(systemName: "globe")
                            .font(.caption2)
                            .foregroundStyle(Color.accentColor)
                            .accessibilityLabel("Published publicly")
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 3)
    }
}
