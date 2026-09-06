import SwiftUI
import UniformTypeIdentifiers

struct CreateAppView: View {
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var runtime: Runtime = .python
    @State private var entrypoint = "main.py"
    @State private var startOnBoot = false
    @State private var restartPolicy: RestartPolicy = .onFailure

    @State private var files: [PickedFile] = []
    @State private var requirements: PickedFile?
    @State private var pickingFiles = false
    @State private var pickingRequirements = false

    @State private var isUploading = false
    @State private var errorText: String?

    struct PickedFile: Identifiable, Equatable {
        let id = UUID()
        var name: String
        var data: Data
        var size: Int { data.count }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("my-flask-app", text: $name)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Name")
                } footer: {
                    Text("Up to 50 characters.")
                }

                Section("Runtime") {
                    Picker("Runtime", selection: $runtime) {
                        ForEach(Runtime.allCases) { Text($0.label).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .onChange(of: runtime) { _, new in
                        // Rust apps are always built from main.rs.
                        entrypoint = new == .rust ? "main.rs" : "main.py"
                    }

                    LabeledContent("Entrypoint") {
                        TextField("main.py", text: $entrypoint)
                            .multilineTextAlignment(.trailing)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .font(.body.monospaced())
                            .disabled(runtime == .rust)
                    }
                }

                Section {
                    if files.isEmpty {
                        Text("No files selected.")
                            .foregroundStyle(.secondary)
                            .font(.subheadline)
                    } else {
                        ForEach(files) { file in
                            HStack {
                                Image(systemName: "doc")
                                    .foregroundStyle(.secondary)
                                Text(file.name).lineLimit(1)
                                Spacer()
                                Text(Fmt.bytes(file.size))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .onDelete { files.remove(atOffsets: $0) }
                    }
                    Button {
                        pickingFiles = true
                    } label: {
                        Label("Choose source files", systemImage: "doc.badge.plus")
                    }
                } header: {
                    Text("Source files")
                } footer: {
                    Text("Everything your app needs to run. 50 MB per file.")
                }

                Section {
                    if let requirements {
                        HStack {
                            Image(systemName: "list.bullet.rectangle")
                                .foregroundStyle(.secondary)
                            Text(requirements.name).lineLimit(1)
                            Spacer()
                            Button("Remove") { self.requirements = nil }
                                .font(.caption)
                        }
                    }
                    Button {
                        pickingRequirements = true
                    } label: {
                        Label(requirements == nil
                              ? "Choose \(runtime.requirementsFilename)"
                              : "Replace \(runtime.requirementsFilename)",
                              systemImage: "shippingbox")
                    }
                } header: {
                    Text("Dependencies")
                } footer: {
                    Text("Optional. Dependencies install during upload, which can take several minutes — keep this screen open until it finishes.")
                }

                Section("Lifecycle") {
                    Toggle("Start on boot", isOn: $startOnBoot)
                    Picker("Restart policy", selection: $restartPolicy) {
                        ForEach(RestartPolicy.allCases) { Text($0.label).tag($0) }
                    }
                }

                if let errorText {
                    Section {
                        Text(errorText)
                            .font(.subheadline)
                            .foregroundStyle(VenvyColor.danger)
                    }
                }
            }
            .navigationTitle("New app")
            .navigationBarTitleDisplayMode(.inline)
            .interactiveDismissDisabled(isUploading)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(isUploading)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isUploading {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Create") { create() }
                            .fontWeight(.semibold)
                            .disabled(!canCreate)
                    }
                }
            }
            .overlay {
                if isUploading {
                    BusyOverlay(message: requirements == nil
                                ? "Creating \(name)…"
                                : "Creating \(name) and installing dependencies…\nThis can take a few minutes.")
                }
            }
            .fileImporter(isPresented: $pickingFiles, allowedContentTypes: [.item],
                          allowsMultipleSelection: true) { result in
                handlePick(result) { picked in
                    files.append(contentsOf: picked)
                }
            }
            .fileImporter(isPresented: $pickingRequirements, allowedContentTypes: [.item],
                          allowsMultipleSelection: false) { result in
                handlePick(result) { picked in
                    requirements = picked.first
                }
            }
        }
    }

    private var canCreate: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty && !files.isEmpty
    }

    private func handlePick(_ result: Result<[URL], Error>, assign: ([PickedFile]) -> Void) {
        switch result {
        case .failure(let error):
            errorText = error.localizedDescription
        case .success(let urls):
            var picked: [PickedFile] = []
            for url in urls {
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                guard let data = try? Data(contentsOf: url) else {
                    errorText = "Couldn't read \(url.lastPathComponent)."
                    continue
                }
                guard data.count <= 50 * 1024 * 1024 else {
                    errorText = "\(url.lastPathComponent) is over the 50 MB limit."
                    continue
                }
                picked.append(PickedFile(name: url.lastPathComponent, data: data))
            }
            assign(picked)
        }
    }

    private func create() {
        guard let client = session.client, canCreate else { return }
        errorText = nil
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard trimmed.count <= 50 else {
            errorText = "Names are limited to 50 characters."
            return
        }
        isUploading = true
        Task {
            defer { isUploading = false }
            do {
                let created = try await client.createApp(
                    name: trimmed,
                    runtime: runtime,
                    entrypoint: entrypoint.trimmingCharacters(in: .whitespaces),
                    startOnBoot: startOnBoot,
                    restartPolicy: restartPolicy,
                    files: files.map { ($0.name, $0.data) },
                    requirements: requirements.map { ($0.name, $0.data) })
                session.toasts.success("Created \(created.name).")
                await session.loadApps(showSpinner: false)
                dismiss()
            } catch let error as APIError {
                if error.isAuthFailure { session.handleAuthFailure(); dismiss(); return }
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}
