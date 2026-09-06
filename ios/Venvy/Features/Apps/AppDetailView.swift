import SwiftUI

struct AppDetailView: View {
    let appID: Int
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    @State private var liveStats: AppStats?
    @State private var statsTask: Task<Void, Never>?
    @State private var confirmDelete = false
    @State private var deleteConfirmText = ""

    private var app: HostedApp? { session.app(id: appID) }

    var body: some View {
        Group {
            if let app {
                content(app)
            } else {
                // The app vanished from the list — usually because it was
                // deleted here or from another client.
                EmptyStateView(icon: "questionmark.folder",
                               title: "This app is no longer on the server")
            }
        }
        .navigationTitle(app?.name ?? "App")
        .navigationBarTitleDisplayMode(.inline)
        .background(Color(.systemGroupedBackground))
        .toolbar {
            if let app, session.canOperate {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        if let urlString = app.accessURL, let url = URL(string: urlString) {
                            Button {
                                openURL(url)
                            } label: {
                                Label("Open app", systemImage: "safari")
                            }
                            Button {
                                UIPasteboard.general.string = urlString
                                session.toasts.success("URL copied.")
                            } label: {
                                Label("Copy URL", systemImage: "doc.on.doc")
                            }
                            Divider()
                        }
                        Button(role: .destructive) {
                            deleteConfirmText = ""
                            confirmDelete = true
                        } label: {
                            Label("Delete app", systemImage: "trash")
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                    .accessibilityLabel("More actions")
                }
            }
        }
        .task(id: appID) { await pollStats() }
        .onDisappear { statsTask?.cancel() }
        .alert("Delete \(app?.name ?? "app")?", isPresented: $confirmDelete) {
            TextField("Type the app name", text: $deleteConfirmText)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("Delete", role: .destructive) {
                guard let app, deleteConfirmText == app.name else { return }
                Task {
                    await session.deleteApp(app)
                    dismiss()
                }
            }
            .disabled(deleteConfirmText != (app?.name ?? ""))
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops the process and permanently removes its files, virtual environment and configuration. Type the app's name to confirm.")
        }
    }

    // MARK: - Content

    @ViewBuilder
    private func content(_ app: HostedApp) -> some View {
        List {
            Section {
                DashboardHeader(app: app,
                                isBusy: session.pendingControl.contains(app.id),
                                stats: liveStats)
                    .listRowInsets(EdgeInsets(top: 10, leading: 16, bottom: 10, trailing: 16))
            }

            if session.canOperate {
                Section {
                    ControlButtons(app: app,
                                   isBusy: session.pendingControl.contains(app.id)) { action in
                        Task { await session.control(app: app, action: action) }
                    }
                    .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                }
            }

            if let urlString = app.accessURL, let url = URL(string: urlString) {
                Section {
                    Link(destination: url) {
                        HStack {
                            Label {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("Open app").foregroundStyle(.primary)
                                    Text(urlString)
                                        .font(.caption.monospaced())
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                        .truncationMode(.middle)
                                }
                            } icon: {
                                Image(systemName: app.urlSource == "cloudflare" ? "globe" : "network")
                            }
                            Spacer()
                            Image(systemName: "arrow.up.right.square")
                                .foregroundStyle(.secondary)
                        }
                    }
                } header: {
                    Text(app.urlSource == "cloudflare" ? "Public URL" : "Local URL")
                } footer: {
                    if app.status != .running {
                        Text("The app isn't running, so this URL won't respond yet.")
                    }
                }
            }

            Section("Manage") {
                NavigationLink {
                    FilesView(appID: app.id, appName: app.name)
                } label: {
                    Label("Files", systemImage: "folder")
                }
                NavigationLink {
                    LogsView(appID: app.id, appName: app.name)
                } label: {
                    Label("Logs", systemImage: "text.alignleft")
                }
                NavigationLink {
                    PackagesView(appID: app.id, runtime: app.runtime)
                } label: {
                    Label(app.runtime == .python ? "Packages (pip)" : "Crates (cargo)",
                          systemImage: "shippingbox")
                }
                if session.canOperate {
                    NavigationLink {
                        TerminalView(appID: app.id, appName: app.name)
                    } label: {
                        Label("Terminal", systemImage: "terminal")
                    }
                }
                NavigationLink {
                    TunnelView(appID: app.id, app: app)
                } label: {
                    Label("Public access", systemImage: "globe.badge.chevron.backward")
                }
                NavigationLink {
                    AppSettingsView(appID: app.id)
                } label: {
                    Label("Settings", systemImage: "gearshape")
                }
            }

            Section("Details") {
                DetailRow(label: "Runtime", value: app.runtime.label)
                if let entry = app.entrypoint, !entry.isEmpty {
                    DetailRow(label: "Entrypoint", value: entry, monospaced: true, copyable: true)
                }
                if let pid = app.pid, app.status == .running {
                    DetailRow(label: "PID", value: String(pid), monospaced: true)
                }
                if !app.allPorts.isEmpty {
                    DetailRow(label: app.allPorts.count > 1 ? "Ports" : "Port",
                              value: app.allPorts.map(String.init).joined(separator: ", "),
                              monospaced: true, copyable: true)
                }
                DetailRow(label: "Restart policy", value: app.restartPolicy.label)
                DetailRow(label: "Start on boot", value: app.startOnBoot ? "Yes" : "No")
                if let code = app.lastExitCode {
                    DetailRow(label: "Last exit code", value: String(code), monospaced: true)
                }
                if let started = app.lastStartTime {
                    DetailRow(label: "Last started", value: Fmt.relative(started))
                }
                if let dir = app.appDir {
                    DetailRow(label: "Directory", value: dir, monospaced: true, copyable: true)
                }
            }

            Section("Limits") {
                DetailRow(label: "Memory",
                          value: app.memoryLimitMB.map { "\($0) MB" } ?? "Unlimited")
                DetailRow(label: "CPU",
                          value: app.cpuLimitPercent.map { "\($0)%" } ?? "Unlimited")
            }
        }
        .listStyle(.insetGrouped)
        .refreshable {
            await session.loadApps(showSpinner: false)
            await refreshStatsOnce()
        }
    }

    // MARK: - Stats polling

    /// `/stats` is sampled fresh per call. psutil's first CPU reading after a
    /// process starts has no prior sample to diff against and reads 0.0, so
    /// poll on a timer rather than showing a single cold value.
    private func pollStats() async {
        statsTask?.cancel()
        statsTask = Task {
            while !Task.isCancelled {
                await refreshStatsOnce()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
        await statsTask?.value
    }

    private func refreshStatsOnce() async {
        guard let client = session.client, let app else { return }
        guard app.status == .running else {
            liveStats = nil
            return
        }
        liveStats = try? await client.stats(id: app.id)
    }
}

// MARK: - Header

struct DashboardHeader: View {
    let app: HostedApp
    let isBusy: Bool
    let stats: AppStats?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(VenvyColor.status(app.status).opacity(0.14))
                        .frame(width: 48, height: 48)
                    if isBusy || app.status.isTransitional {
                        ProgressView()
                    } else {
                        Image(systemName: app.runtime.icon)
                            .font(.system(size: 20, weight: .semibold))
                            .foregroundStyle(VenvyColor.status(app.status))
                    }
                }
                VStack(alignment: .leading, spacing: 5) {
                    Text(app.name)
                        .font(.title3.weight(.semibold))
                        .lineLimit(2)
                    StatusBadge(status: app.status)
                }
                Spacer(minLength: 0)
            }

            HStack(spacing: 10) {
                StatTile(title: "CPU",
                         value: statsAvailable ? Fmt.percent(stats?.cpu ?? app.metrics.cpu) : "—",
                         systemImage: "cpu", tint: .accentColor)
                StatTile(title: "Memory",
                         value: memoryText,
                         systemImage: "memorychip", tint: VenvyColor.warning)
                StatTile(title: "Uptime",
                         value: stats.map { Fmt.uptime($0.uptime) } ?? "—",
                         systemImage: "clock", tint: VenvyColor.success)
            }
        }
    }

    private var statsAvailable: Bool { app.status == .running }

    /// The list endpoint reports memory in bytes; `/stats` reports megabytes.
    private var memoryText: String {
        guard statsAvailable else { return "—" }
        if let stats { return Fmt.megabytes(stats.memoryMB) }
        return app.metrics.memoryBytes > 0 ? Fmt.bytes(app.metrics.memoryBytes) : "—"
    }
}

// MARK: - Controls

struct ControlButtons: View {
    let app: HostedApp
    let isBusy: Bool
    let action: (String) -> Void

    var body: some View {
        // Grouped so the three glass capsules blend into one cluster and morph
        // together as they enable/disable.
        GlassGroup(spacing: 10) {
            HStack(spacing: 10) {
                controlButton(title: "Start", icon: "play.fill", tint: VenvyColor.success,
                              enabled: app.status == .stopped || app.status == .error) {
                    action("start")
                }
                controlButton(title: "Restart", icon: "arrow.clockwise", tint: VenvyColor.warning,
                              enabled: app.status == .running) {
                    action("restart")
                }
                controlButton(title: "Stop", icon: "stop.fill", tint: VenvyColor.danger,
                              enabled: app.status == .running || app.status == .starting) {
                    action("stop")
                }
            }
        }
        .disabled(isBusy || app.status.isTransitional)
    }

    private func controlButton(title: String, icon: String, tint: Color,
                               enabled: Bool, perform: @escaping () -> Void) -> some View {
        Button(action: perform) {
            VStack(spacing: 5) {
                Image(systemName: icon).font(.system(size: 15, weight: .semibold))
                Text(title).font(.caption.weight(.medium))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 11)
            .foregroundStyle(enabled ? tint : Color.secondary.opacity(0.5))
        }
        .buttonStyle(.plain)
        .venvyGlass(in: RoundedRectangle(cornerRadius: 14, style: .continuous),
                    tint: enabled ? tint.opacity(0.16) : .clear,
                    interactive: enabled)
        .disabled(!enabled)
        .accessibilityLabel("\(title) app")
    }
}
