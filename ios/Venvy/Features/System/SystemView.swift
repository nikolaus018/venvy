import SwiftUI

/// Admin-only host overview: metrics, users, backups, ports and Cloudflare.
struct SystemView: View {
    @EnvironmentObject private var session: VenvySession

    @State private var metrics: SystemMetrics?
    @State private var metricsError: String?
    @State private var ports: PortsInfo?
    @State private var isLoading = true
    @State private var refreshTask: Task<Void, Never>?

    var body: some View {
        NavigationStack {
            List {
                metricsSection
                portsSection

                Section("Administration") {
                    NavigationLink {
                        UsersView()
                    } label: {
                        Label("Users", systemImage: "person.2")
                    }
                    NavigationLink {
                        BackupsView()
                    } label: {
                        Label("Backups", systemImage: "externaldrive.badge.timemachine")
                    }
                    NavigationLink {
                        CloudflareSettingsView()
                    } label: {
                        Label("Cloudflare", systemImage: "cloud")
                    }
                }

                Section("Server") {
                    if let server = session.currentServer {
                        DetailRow(label: "Address", value: server.displayHost,
                                  monospaced: true, copyable: true)
                        DetailRow(label: "Transport", value: server.isSecure ? "HTTPS" : "HTTP")
                    }
                    if let ipv4 = session.serverIPv4 {
                        DetailRow(label: "LAN address", value: ipv4, monospaced: true, copyable: true)
                    }
                    DetailRow(label: "Signed in as", value: session.username)
                    DetailRow(label: "Role", value: session.role.label)
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("System")
            .refreshable { await load() }
            .task { await startRefreshing() }
            .onDisappear { refreshTask?.cancel() }
        }
    }

    private var metricsSection: some View {
        Section {
            if isLoading && metrics == nil {
                ProgressView().frame(maxWidth: .infinity)
            } else if let error = metricsError {
                ErrorBanner(message: error) { Task { await load() } }
                    .listRowBackground(Color.clear)
            } else if let metrics {
                if let psutilError = metrics.error {
                    Label(psutilError, systemImage: "info.circle")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                } else {
                    HStack(spacing: 10) {
                        StatTile(title: "CPU",
                                 value: metrics.cpuPercent.map { Fmt.percent($0) } ?? "—",
                                 systemImage: "cpu")
                        StatTile(title: "Memory",
                                 value: metrics.memoryPercent.map { Fmt.percent($0) } ?? "—",
                                 systemImage: "memorychip", tint: VenvyColor.warning)
                        StatTile(title: "Disk",
                                 value: metrics.diskPercent.map { Fmt.percent($0) } ?? "—",
                                 systemImage: "internaldrive", tint: VenvyColor.success)
                    }
                    .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))

                    if let used = metrics.memoryUsedBytes, let total = metrics.memoryTotalBytes, total > 0 {
                        DetailRow(label: "Memory used",
                                  value: "\(Fmt.bytes(used)) of \(Fmt.bytes(total))")
                    }
                    if let used = metrics.diskUsedBytes, let total = metrics.diskTotalBytes, total > 0 {
                        DetailRow(label: "Disk used",
                                  value: "\(Fmt.bytes(used)) of \(Fmt.bytes(total))")
                    }
                    if let free = metrics.diskFreeBytes, free > 0 {
                        DetailRow(label: "Disk free", value: Fmt.bytes(free))
                    }
                    if let load = metrics.loadAverage, !load.isEmpty {
                        DetailRow(label: "Load average",
                                  value: load.map { String(format: "%.2f", $0) }.joined(separator: "  "),
                                  monospaced: true)
                    }
                    if let cores = metrics.cpuCores {
                        DetailRow(label: "CPU cores", value: String(cores))
                    }
                    if let uptime = metrics.uptimeSeconds, uptime > 0 {
                        DetailRow(label: "Host uptime", value: Fmt.uptime(uptime))
                    }
                }
            }
        } header: {
            Text("Host")
        }
    }

    private var portsSection: some View {
        Section {
            if let ports {
                if !ports.listening.isEmpty {
                    DetailRow(label: "Listening",
                              value: ports.listening.map(String.init).joined(separator: ", "),
                              monospaced: true, copyable: true)
                }
                if !ports.recommended.isEmpty {
                    DetailRow(label: "Free to use",
                              value: ports.recommended.prefix(8).map(String.init).joined(separator: ", "),
                              monospaced: true, copyable: true)
                }
                if ports.listening.isEmpty && ports.recommended.isEmpty {
                    Text("No port information reported.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            } else {
                Text("Loading…").foregroundStyle(.secondary).font(.subheadline)
            }
        } header: {
            Text("Ports")
        }
    }

    private func startRefreshing() async {
        await load()
        refreshTask?.cancel()
        refreshTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                if Task.isCancelled { return }
                await load(silent: true)
            }
        }
    }

    private func load(silent: Bool = false) async {
        guard let client = session.client else { return }
        if !silent { isLoading = true }
        do {
            metrics = try await client.systemMetrics()
            metricsError = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure(); return }
            metricsError = error.localizedDescription
        } catch {
            metricsError = error.localizedDescription
        }
        ports = try? await client.systemPorts()
        isLoading = false
    }
}

// MARK: - Users

struct UsersView: View {
    @EnvironmentObject private var session: VenvySession

    @State private var users: [VenvyUser] = []
    @State private var isLoading = true
    @State private var errorText: String?
    @State private var adding = false
    @State private var pendingDelete: VenvyUser?

    var body: some View {
        List {
            if let errorText {
                Section {
                    ErrorBanner(message: errorText) { Task { await load() } }
                        .listRowBackground(Color.clear)
                }
            }
            Section {
                ForEach(users) { user in
                    HStack(spacing: 12) {
                        Image(systemName: icon(for: user.role))
                            .foregroundStyle(color(for: user.role))
                            .frame(width: 24)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(user.username)
                                .font(.body.weight(.medium))
                            Text(user.role.label)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if user.username == session.username {
                            Text("You")
                                .font(.caption2.weight(.semibold))
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(Color.accentColor.opacity(0.14), in: Capsule())
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                    .swipeActions(edge: .trailing) {
                        // The server rejects self-deletion; hide the action too.
                        if user.username != session.username {
                            Button(role: .destructive) {
                                pendingDelete = user
                            } label: { Label("Delete", systemImage: "trash") }
                        }
                    }
                }
            } header: {
                Text("\(users.count) account\(users.count == 1 ? "" : "s")")
            } footer: {
                Text("Apps belong to the instance, not to individual accounts — deleting a user leaves their apps untouched.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Users")
        .navigationBarTitleDisplayMode(.inline)
        .overlay {
            if isLoading && users.isEmpty { ProgressView() }
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { adding = true } label: { Image(systemName: "plus") }
                    .accessibilityLabel("Add user")
            }
        }
        .task { await load() }
        .refreshable { await load() }
        .sheet(isPresented: $adding) {
            AddUserView { await load() }
        }
        .alert("Delete \(pendingDelete?.username ?? "")?", isPresented: .init(
            get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }
        ), presenting: pendingDelete) { user in
            Button("Delete", role: .destructive) { delete(user) }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: { _ in
            Text("The account and its active sessions are removed. Their apps stay on the server.")
        }
    }

    private func icon(for role: Role) -> String {
        switch role {
        case .admin: return "crown.fill"
        case .operator_: return "wrench.and.screwdriver.fill"
        case .viewer: return "eye.fill"
        }
    }

    private func color(for role: Role) -> Color {
        switch role {
        case .admin: return VenvyColor.warning
        case .operator_: return Color.accentColor
        case .viewer: return .secondary
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        do {
            users = try await client.users()
            errorText = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure(); return }
            errorText = error.localizedDescription
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    private func delete(_ user: VenvyUser) {
        guard let client = session.client else { return }
        pendingDelete = nil
        Task {
            do {
                try await client.deleteUser(id: user.id)
                session.toasts.success("Deleted \(user.username).")
                await load()
            } catch {
                session.report(error, context: "Couldn't delete \(user.username)")
            }
        }
    }
}

struct AddUserView: View {
    var onDone: () async -> Void

    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var username = ""
    @State private var password = ""
    @State private var role: Role = .operator_
    @State private var isSaving = false
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Account") {
                    TextField("username", text: $username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("password", text: $password)
                        .textContentType(.newPassword)
                }

                Section {
                    Picker("Role", selection: $role) {
                        ForEach(Role.allCases, id: \.self) { r in
                            Text(r.label).tag(r)
                        }
                    }
                } header: {
                    Text("Role")
                } footer: {
                    Text(roleDescription)
                }

                if let errorText {
                    Section {
                        Text(errorText)
                            .font(.subheadline)
                            .foregroundStyle(VenvyColor.danger)
                    }
                }
            }
            .navigationTitle("New user")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isSaving {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Create") { create() }
                            .fontWeight(.semibold)
                            .disabled(username.trimmingCharacters(in: .whitespaces).isEmpty || password.isEmpty)
                    }
                }
            }
        }
    }

    private var roleDescription: String {
        switch role {
        case .admin: return "Full access, including users, system settings, metrics and backups."
        case .operator_: return "Full app lifecycle, files, packages, logs and tunnels. No user or system administration."
        case .viewer: return "Read-only. Can list apps and view logs, files and stats, but can't change anything."
        }
    }

    private func create() {
        guard let client = session.client else { return }
        errorText = nil
        guard password.count >= 8,
              password.rangeOfCharacter(from: .letters) != nil,
              password.rangeOfCharacter(from: .decimalDigits) != nil else {
            errorText = "Use at least 8 characters, with a letter and a digit."
            return
        }
        isSaving = true
        Task {
            defer { isSaving = false }
            do {
                try await client.createUser(username: username.trimmingCharacters(in: .whitespaces),
                                            password: password, role: role)
                session.toasts.success("Created \(username).")
                await onDone()
                dismiss()
            } catch let error as APIError {
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

// MARK: - Backups

struct BackupsView: View {
    @EnvironmentObject private var session: VenvySession

    @State private var backups: [Backup] = []
    @State private var isLoading = true
    @State private var errorText: String?
    @State private var isCreating = false

    var body: some View {
        List {
            if let errorText {
                Section {
                    ErrorBanner(message: errorText) { Task { await load() } }
                        .listRowBackground(Color.clear)
                }
            }

            Section {
                Button {
                    createBackup()
                } label: {
                    HStack {
                        if isCreating { ProgressView().controlSize(.small) }
                        Label("Create snapshot", systemImage: "plus.circle")
                    }
                }
                .disabled(isCreating)
            } footer: {
                Text("Takes a consistent SQLite snapshot with VACUUM INTO — safe to run while Venvy is live.")
            }

            Section {
                if backups.isEmpty && !isLoading {
                    Text("No snapshots yet.")
                        .foregroundStyle(.secondary)
                        .font(.subheadline)
                } else {
                    ForEach(backups) { backup in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(backup.filename)
                                .font(.subheadline.monospaced())
                                .lineLimit(1)
                                .truncationMode(.middle)
                            HStack(spacing: 8) {
                                Text(Fmt.bytes(backup.sizeBytes))
                                if let created = backup.createdAt {
                                    Text("·")
                                    Text(formatted(created))
                                }
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                    }
                }
            } header: {
                Text("Snapshots")
            } footer: {
                Text("A full disaster-recovery backup also needs the apps/ directory and the .venvy_key file — without the key, encrypted secrets in a restored database can't be read. Snapshots stay on the server; there's no restore endpoint.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Backups")
        .navigationBarTitleDisplayMode(.inline)
        .overlay { if isLoading && backups.isEmpty { ProgressView() } }
        .task { await load() }
        .refreshable { await load() }
    }

    private func formatted(_ raw: String) -> String {
        guard let date = ISO8601.parse(raw) else { return raw }
        return date.formatted(date: .abbreviated, time: .shortened)
    }

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        do {
            backups = try await client.backups()
            errorText = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure(); return }
            errorText = error.localizedDescription
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    private func createBackup() {
        guard let client = session.client else { return }
        isCreating = true
        Task {
            defer { isCreating = false }
            do {
                let result = try await client.createBackup()
                session.toasts.success(result.sizeBytes.map {
                    "Snapshot created (\(Fmt.bytes($0)))."
                } ?? "Snapshot created.")
                await load()
            } catch {
                session.report(error, context: "Backup failed")
            }
        }
    }
}
