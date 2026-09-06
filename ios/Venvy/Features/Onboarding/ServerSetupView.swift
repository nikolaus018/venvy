import SwiftUI

/// Add or edit a server. `firstRun` styles it as a full-screen welcome;
/// `sheet` is the version presented from the server list.
struct ServerSetupView: View {
    enum Mode { case firstRun, sheet }
    var mode: Mode = .sheet
    var editing: ServerConfig?

    @EnvironmentObject private var servers: ServerStore
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var address = ""
    @State private var testState: TestState = .idle
    @State private var errorText: String?
    /// The address the probe itself wrote, so rewriting the field on success
    /// doesn't look like a user edit and wipe the result banner.
    @State private var probeRewrote: String?
    @FocusState private var addressFocused: Bool

    private enum TestState: Equatable {
        case idle, testing
        case ok(String)
        case failed(String)
    }

    var body: some View {
        Group {
            if mode == .firstRun {
                NavigationStack { form.navigationTitle("Add your server") }
            } else {
                NavigationStack {
                    form
                        .navigationTitle(editing == nil ? "Add server" : "Edit server")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Cancel") { dismiss() }
                            }
                            ToolbarItem(placement: .confirmationAction) {
                                Button("Save") { save() }
                                    .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                            }
                        }
                }
            }
        }
        .onAppear(perform: prefill)
    }

    private var form: some View {
        Form {
            if mode == .firstRun {
                Section {
                    VStack(spacing: 10) {
                        Image(systemName: "server.rack")
                            .font(.system(size: 40, weight: .light))
                            .foregroundStyle(Color.accentColor)
                        Text("Venvy")
                            .font(.system(.largeTitle, design: .rounded).weight(.bold))
                        Text("Manage your self-hosted apps from your phone. Start by pointing this app at your Venvy server.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                }
                .listRowBackground(Color.clear)
            }

            Section {
                TextField("192.168.1.20:54783", text: $address)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .submitLabel(.go)
                    .focused($addressFocused)
                    .onSubmit(test)
                    .onChange(of: address) { _, newValue in
                        guard newValue != probeRewrote else { return }
                        testState = .idle
                        errorText = nil
                    }
            } header: {
                Text("Server address")
            } footer: {
                Text("Enter your Venvy server's IP or hostname. A bare address gets Venvy's default port 54783 over http — type a full `https://…` URL if your server is behind TLS.")
            }

            Section {
                TextField("Home server", text: $name)
                    .textInputAutocapitalization(.words)
            } header: {
                Text("Name")
            } footer: {
                Text("Optional. Shown when you have more than one server.")
            }

            Section {
                Button(action: test) {
                    HStack {
                        if testState == .testing {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "bolt.horizontal.circle")
                        }
                        Text(testState == .testing ? "Testing…" : "Test connection")
                    }
                }
                .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty || testState == .testing)

                switch testState {
                case .ok(let detail):
                    Label(detail, systemImage: "checkmark.circle.fill")
                        .foregroundStyle(VenvyColor.success)
                        .font(.subheadline)
                case .failed(let detail):
                    Label {
                        Text(detail).fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .foregroundStyle(VenvyColor.danger)
                    .font(.subheadline)
                default:
                    EmptyView()
                }
            }

            if let errorText {
                Section {
                    Text(errorText)
                        .font(.subheadline)
                        .foregroundStyle(VenvyColor.danger)
                }
            }

            if mode == .firstRun {
                Section {
                    Button(action: save) {
                        Text("Continue")
                            .frame(maxWidth: .infinity)
                    }
                    .venvyProminentButton()
                    .controlSize(.large)
                    .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                .listRowBackground(Color.clear)
            }
        }
    }

    private func prefill() {
        guard let editing else {
            if address.isEmpty { addressFocused = mode == .sheet }
            return
        }
        name = editing.name
        address = editing.baseURL
    }

    /// Probes each plausible origin for the typed address and adopts the
    /// first that answers `/api/health`, so the user can type a bare hostname
    /// without knowing the scheme or port.
    private func test() {
        errorText = nil
        let options = ServerURL.candidates(for: address)
        guard !options.isEmpty else {
            testState = .failed(ServerURLError.invalid.localizedDescription)
            return
        }
        testState = .testing
        Task {
            var lastError: String?
            for candidate in options {
                guard let url = URL(string: candidate) else { continue }
                // Keep the per-candidate timeout short so walking a few dead
                // guesses stays quick.
                let timeout: TimeInterval = options.count > 1 ? 8 : 15
                let client = APIClient(baseURL: url, timeout: timeout)
                do {
                    let health = try await client.health()
                    // /api/health stays reachable even when the rest of the API
                    // is locked down, so also read auth status for a fuller
                    // picture of what we just connected to.
                    let status = try? await client.authStatus()
                    var detail = "Connected — server is \(health.status)"
                    if let status {
                        detail += status.userExists
                            ? ", ready to sign in"
                            : ", awaiting first-run setup"
                    }
                    if candidate != address {
                        detail += "\nUsing \(candidate)"
                    }
                    probeRewrote = candidate
                    address = candidate
                    testState = .ok(detail)
                    return
                } catch let error as APIError {
                    lastError = error.localizedDescription
                } catch {
                    lastError = error.localizedDescription
                }
            }
            let tried = options.count > 1
                ? "\nTried: " + options.joined(separator: ", ")
                : ""
            testState = .failed((lastError ?? "Couldn't reach that server.") + tried)
        }
    }

    private func save() {
        errorText = nil
        let normalized: String
        do {
            normalized = try ServerURL.normalize(address)
        } catch {
            errorText = error.localizedDescription
            return
        }
        let displayName = name.trimmingCharacters(in: .whitespaces).isEmpty
            ? (URL(string: normalized)?.host ?? "Venvy server")
            : name.trimmingCharacters(in: .whitespaces)

        var config = editing ?? ServerConfig(name: displayName, baseURL: normalized)
        config.name = displayName
        config.baseURL = normalized
        servers.upsert(config)
        session.connect(to: config)
        if mode == .sheet { dismiss() }
    }
}

/// Manage the list of saved servers and switch between them.
struct ServerListView: View {
    @EnvironmentObject private var servers: ServerStore
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var adding = false
    @State private var editingServer: ServerConfig?
    @State private var pendingDelete: ServerConfig?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(servers.servers) { server in
                        Button {
                            session.connect(to: server)
                            dismiss()
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: server.isSecure ? "lock.fill" : "network")
                                    .foregroundStyle(server.isSecure ? VenvyColor.success : Color.accentColor)
                                    .frame(width: 22)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(server.name)
                                        .font(.body.weight(.medium))
                                        .foregroundStyle(.primary)
                                    Text(server.displayHost)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if servers.selectedID == server.id {
                                    Image(systemName: "checkmark")
                                        .font(.subheadline.weight(.semibold))
                                        .foregroundStyle(Color.accentColor)
                                }
                            }
                        }
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) {
                                pendingDelete = server
                            } label: {
                                Label("Remove", systemImage: "trash")
                            }
                            Button {
                                editingServer = server
                            } label: {
                                Label("Edit", systemImage: "pencil")
                            }
                            .tint(.gray)
                        }
                    }
                } footer: {
                    if servers.servers.count > 1 {
                        Text("Switching servers signs you out of the current one on this device only.")
                    }
                }

                Section {
                    Button {
                        adding = true
                    } label: {
                        Label("Add server", systemImage: "plus.circle.fill")
                    }
                }
            }
            .navigationTitle("Servers")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .sheet(isPresented: $adding) { ServerSetupView() }
            .sheet(item: $editingServer) { ServerSetupView(editing: $0) }
            .alert("Remove server?", isPresented: .init(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } }
            ), presenting: pendingDelete) { server in
                Button("Remove", role: .destructive) {
                    servers.remove(server)
                    pendingDelete = nil
                }
                Button("Cancel", role: .cancel) { pendingDelete = nil }
            } message: { server in
                Text("\(server.name) will be removed from this device, along with any saved password. Nothing on the server changes.")
            }
        }
    }
}
