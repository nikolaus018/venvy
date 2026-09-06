import SwiftUI

struct AppSettingsView: View {
    let appID: Int
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var entrypoint = ""
    @State private var runtime: Runtime = .python
    @State private var restartPolicy: RestartPolicy = .never
    @State private var startOnBoot = false
    @State private var urlSourcePref: URLSourcePref = .local
    @State private var portsText = ""
    @State private var memoryUnlimited = false
    @State private var memoryLimit = 512.0
    @State private var cpuUnlimited = false
    @State private var cpuLimit = 50.0
    @State private var envVars: [EnvVar] = []
    @State private var revealSecrets = false

    @State private var loaded = false
    @State private var isSaving = false
    @State private var validationError: String?

    struct EnvVar: Identifiable, Equatable {
        let id = UUID()
        var key: String
        var value: String
    }

    private var app: HostedApp? { session.app(id: appID) }
    private var canEdit: Bool { session.canOperate }

    var body: some View {
        Form {
            if !canEdit {
                Section {
                    Label("Read-only — your role can't change app settings.",
                          systemImage: "eye")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            Section("General") {
                LabeledContent("Name") {
                    TextField("app name", text: $name)
                        .multilineTextAlignment(.trailing)
                        .disabled(!canEdit)
                }
                Picker("Runtime", selection: $runtime) {
                    ForEach(Runtime.allCases) { Text($0.label).tag($0) }
                }
                .disabled(!canEdit)

                LabeledContent("Entrypoint") {
                    TextField(runtime == .rust ? "main.rs" : "main.py", text: $entrypoint)
                        .multilineTextAlignment(.trailing)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                        .disabled(!canEdit || runtime == .rust)
                }
            }
            .listRowSeparator(.automatic)

            Section {
                Text(runtime == .rust
                     ? "Rust apps always use main.rs as their entrypoint."
                     : "A plain filename only — letters, digits, dots, dashes and underscores.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .listRowBackground(Color.clear)

            Section("Lifecycle") {
                Toggle("Start on boot", isOn: $startOnBoot)
                    .disabled(!canEdit)
                Picker("Restart policy", selection: $restartPolicy) {
                    ForEach(RestartPolicy.allCases) { Text($0.label).tag($0) }
                }
                .disabled(!canEdit)
            }

            Section {
                TextField("8001, 8002", text: $portsText)
                    .keyboardType(.numbersAndPunctuation)
                    .font(.body.monospaced())
                    .disabled(!canEdit)
                Picker("Preferred URL", selection: $urlSourcePref) {
                    ForEach(URLSourcePref.allCases) { Text($0.label).tag($0) }
                }
                .disabled(!canEdit)
            } header: {
                Text("Ports")
            } footer: {
                Text("Comma-separated. The first port becomes the app's primary. Leave empty to clear all manual ports and fall back to auto-detection.")
            }

            Section {
                Toggle("Unlimited memory", isOn: $memoryUnlimited)
                    .disabled(!canEdit)
                if !memoryUnlimited {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text("Memory limit")
                            Spacer()
                            Text("\(Int(memoryLimit)) MB")
                                .foregroundStyle(.secondary)
                                .font(.subheadline.monospacedDigit())
                        }
                        Slider(value: $memoryLimit, in: 16...8192, step: 16)
                            .disabled(!canEdit)
                    }
                }
                Toggle("Unlimited CPU", isOn: $cpuUnlimited)
                    .disabled(!canEdit)
                if !cpuUnlimited {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text("CPU limit")
                            Spacer()
                            Text("\(Int(cpuLimit))%")
                                .foregroundStyle(.secondary)
                                .font(.subheadline.monospacedDigit())
                        }
                        Slider(value: $cpuLimit, in: 1...400, step: 1)
                            .disabled(!canEdit)
                    }
                }
            } header: {
                Text("Resource limits")
            } footer: {
                Text("Applied as hard caps via systemd-run when the host supports it. Over 100% CPU means more than one core.")
            }

            envSection

            if let validationError {
                Section {
                    Text(validationError)
                        .font(.subheadline)
                        .foregroundStyle(VenvyColor.danger)
                }
            }
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if isSaving {
                    ProgressView().controlSize(.small)
                } else if canEdit {
                    Button("Save") { save() }
                        .fontWeight(.semibold)
                        .disabled(!hasChanges)
                }
            }
        }
        .onAppear(perform: loadIfNeeded)
    }

    // MARK: - Env vars

    private var envSection: some View {
        Section {
            if let app, app.envRedacted && envVars.isEmpty {
                Label("This app has environment variables, but your role can't view them.",
                      systemImage: "eye.slash")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            ForEach($envVars) { $item in
                VStack(spacing: 6) {
                    TextField("KEY", text: $item.key)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.subheadline.monospaced())
                        .disabled(!canEdit)
                    HStack {
                        if revealSecrets {
                            TextField("value", text: $item.value)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .font(.subheadline.monospaced())
                                .disabled(!canEdit)
                        } else {
                            SecureField("value", text: $item.value)
                                .font(.subheadline.monospaced())
                                .disabled(!canEdit)
                        }
                    }
                }
                .padding(.vertical, 2)
            }
            .onDelete { indexSet in
                guard canEdit else { return }
                envVars.remove(atOffsets: indexSet)
            }

            if canEdit {
                Button {
                    envVars.append(EnvVar(key: "", value: ""))
                } label: {
                    Label("Add variable", systemImage: "plus")
                }
            }
        } header: {
            HStack {
                Text("Environment")
                Spacer()
                if !envVars.isEmpty {
                    Button(revealSecrets ? "Hide" : "Reveal") {
                        revealSecrets.toggle()
                    }
                    .font(.caption)
                    .textCase(nil)
                }
            }
        } footer: {
            Text("Stored encrypted on the server and injected into the app process and its terminal sessions. Changes take effect on the next restart.")
        }
    }

    // MARK: - Load / save

    private func loadIfNeeded() {
        guard !loaded, let app else { return }
        loaded = true
        name = app.name
        entrypoint = app.entrypoint ?? ""
        runtime = app.runtime
        restartPolicy = app.restartPolicy
        startOnBoot = app.startOnBoot
        urlSourcePref = app.urlSourcePref
        portsText = app.manualPorts.map(String.init).joined(separator: ", ")
        memoryUnlimited = app.memoryLimitMB == nil
        memoryLimit = Double(app.memoryLimitMB ?? 512)
        cpuUnlimited = app.cpuLimitPercent == nil
        cpuLimit = Double(app.cpuLimitPercent ?? 50)
        envVars = app.envVars
            .sorted { $0.key < $1.key }
            .map { EnvVar(key: $0.key, value: $0.value) }
    }

    private var hasChanges: Bool {
        guard let app else { return false }
        if name != app.name { return true }
        if entrypoint != (app.entrypoint ?? "") { return true }
        if runtime != app.runtime { return true }
        if restartPolicy != app.restartPolicy { return true }
        if startOnBoot != app.startOnBoot { return true }
        if urlSourcePref != app.urlSourcePref { return true }
        if parsedPorts ?? [] != app.manualPorts { return true }
        if memoryUnlimited != (app.memoryLimitMB == nil) { return true }
        if !memoryUnlimited && Int(memoryLimit) != (app.memoryLimitMB ?? -1) { return true }
        if cpuUnlimited != (app.cpuLimitPercent == nil) { return true }
        if !cpuUnlimited && Int(cpuLimit) != (app.cpuLimitPercent ?? -1) { return true }
        if currentEnvDict != app.envVars { return true }
        return false
    }

    private var currentEnvDict: [String: String] {
        var out: [String: String] = [:]
        for item in envVars {
            let key = item.key.trimmingCharacters(in: .whitespaces)
            guard !key.isEmpty else { continue }
            out[key] = item.value
        }
        return out
    }

    /// nil means the field is malformed.
    private var parsedPorts: [Int]? {
        let trimmed = portsText.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return [] }
        var out: [Int] = []
        for piece in trimmed.split(separator: ",") {
            let s = piece.trimmingCharacters(in: .whitespaces)
            guard let n = Int(s), (1...65535).contains(n) else { return nil }
            out.append(n)
        }
        return out
    }

    private func save() {
        validationError = nil
        guard let app else { return }

        let trimmedName = name.trimmingCharacters(in: .whitespaces)
        guard !trimmedName.isEmpty else {
            validationError = "The app needs a name."
            return
        }
        guard trimmedName.count <= 50 else {
            validationError = "Names are limited to 50 characters."
            return
        }
        guard let ports = parsedPorts else {
            validationError = "Ports must be numbers between 1 and 65535, separated by commas."
            return
        }
        let trimmedEntry = entrypoint.trimmingCharacters(in: .whitespaces)
        if runtime == .python, !trimmedEntry.isEmpty {
            let allowed = CharacterSet(charactersIn:
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
            guard trimmedEntry.rangeOfCharacter(from: allowed.inverted) == nil else {
                validationError = "The entrypoint must be a plain filename, e.g. main.py."
                return
            }
        }
        // Duplicate keys would silently collapse when we build the dictionary.
        let keys = envVars.map { $0.key.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard Set(keys).count == keys.count else {
            validationError = "Two environment variables share the same name."
            return
        }

        var update = AppUpdateRequest()
        if trimmedName != app.name { update.name = trimmedName }
        if startOnBoot != app.startOnBoot { update.start_on_boot = startOnBoot }
        if restartPolicy != app.restartPolicy { update.restart_policy = restartPolicy.rawValue }
        if runtime != app.runtime { update.runtime = runtime.rawValue }
        if urlSourcePref != app.urlSourcePref { update.url_source_pref = urlSourcePref.rawValue }
        if trimmedEntry != (app.entrypoint ?? "") { update.entrypoint = trimmedEntry }
        if ports != app.manualPorts {
            update.manual_ports = ports
            // An empty list clears the primary port too; the server reads
            // `port: 0` as "clear".
            if ports.isEmpty { update.port = 0 }
        }
        if currentEnvDict != app.envVars { update.env_vars = currentEnvDict }
        // 0 is the server's sentinel for "unlimited" — omitting the field would
        // mean "leave unchanged" instead.
        if memoryUnlimited != (app.memoryLimitMB == nil) || (!memoryUnlimited && Int(memoryLimit) != app.memoryLimitMB) {
            update.memory_limit_mb = memoryUnlimited ? 0 : Int(memoryLimit)
        }
        if cpuUnlimited != (app.cpuLimitPercent == nil) || (!cpuUnlimited && Int(cpuLimit) != app.cpuLimitPercent) {
            update.cpu_limit_percent = cpuUnlimited ? 0 : Int(cpuLimit)
        }

        isSaving = true
        Task {
            defer { isSaving = false }
            if let updated = await session.updateApp(id: appID, update) {
                session.toasts.success("Saved \(updated.name).")
                loaded = false
                loadIfNeeded()
                if app.status == .running {
                    session.toasts.info("Restart the app for changes to take effect.")
                }
            }
        }
    }
}
