import SwiftUI

struct PackagesView: View {
    let appID: Int
    let runtime: Runtime

    @EnvironmentObject private var session: VenvySession

    @State private var packages: [Package] = []
    @State private var isLoading = true
    @State private var errorText: String?
    @State private var search = ""
    @State private var addingPackage = false
    @State private var newPackage = ""
    @State private var busyMessage: String?
    @State private var scan: ImportScan?
    @State private var isScanning = false
    @State private var pendingRemove: Package?

    var body: some View {
        Group {
            if isLoading && packages.isEmpty {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                list
            }
        }
        .navigationTitle(runtime == .python ? "Packages" : "Crates")
        .navigationBarTitleDisplayMode(.inline)
        .background(Color(.systemGroupedBackground))
        .searchable(text: $search, prompt: runtime == .python ? "Search packages" : "Search crates")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    if session.canOperate {
                        Button {
                            newPackage = ""
                            addingPackage = true
                        } label: {
                            Label(runtime == .python ? "Install package" : "Add crate",
                                  systemImage: "plus")
                        }
                    }
                    if runtime == .python {
                        Button {
                            runScan()
                        } label: {
                            Label("Scan imports", systemImage: "magnifyingglass.circle")
                        }
                    }
                    Button {
                        Task { await load() }
                    } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .accessibilityLabel("Package options")
            }
        }
        .overlay { if let busyMessage { BusyOverlay(message: busyMessage) } }
        .task { await load() }
        .refreshable { await load() }
        .alert(runtime == .python ? "Install package" : "Add crate", isPresented: $addingPackage) {
            TextField(runtime == .python ? "requests==2.31.0" : "serde@1.0", text: $newPackage)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("Install") { install(newPackage) }
                .disabled(newPackage.trimmingCharacters(in: .whitespaces).isEmpty)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(runtime == .python
                 ? "Name, optionally with extras and a pinned version — e.g. httpx[http2]==0.27.0."
                 : "Crate name, optionally with a version — e.g. serde@1.0.")
        }
        .alert("Remove \(pendingRemove?.name ?? "")?", isPresented: .init(
            get: { pendingRemove != nil }, set: { if !$0 { pendingRemove = nil } }
        ), presenting: pendingRemove) { pkg in
            Button("Remove", role: .destructive) { remove(pkg) }
            Button("Cancel", role: .cancel) { pendingRemove = nil }
        } message: { _ in
            Text(runtime == .python
                 ? "This runs pip uninstall inside the app's virtual environment."
                 : "This runs cargo remove for this app.")
        }
    }

    private var list: some View {
        List {
            if let errorText {
                Section {
                    ErrorBanner(message: errorText) { Task { await load() } }
                        .listRowBackground(Color.clear)
                }
            }

            if let scan, runtime == .python {
                Section {
                    if scan.missing.isEmpty {
                        Label("Every import is satisfied.", systemImage: "checkmark.seal.fill")
                            .foregroundStyle(VenvyColor.success)
                            .font(.subheadline)
                    } else {
                        ForEach(scan.missing, id: \.self) { name in
                            HStack {
                                Label(name, systemImage: "exclamationmark.triangle.fill")
                                    .foregroundStyle(VenvyColor.warning)
                                Spacer()
                                if session.canOperate {
                                    Button("Install") { install(name) }
                                        .font(.caption.weight(.semibold))
                                        .buttonStyle(.bordered)
                                }
                            }
                        }
                        if session.canOperate && scan.missing.count > 1 {
                            Button {
                                installAll(scan.missing)
                            } label: {
                                Label("Install all \(scan.missing.count)", systemImage: "square.and.arrow.down.on.square")
                            }
                        }
                    }
                } header: {
                    Text("Imports scan")
                } footer: {
                    Text("Static analysis of this app's .py files against the standard library and the packages installed in its venv.")
                }
            }

            Section {
                if filtered.isEmpty {
                    Text(packages.isEmpty
                         ? "No packages reported."
                         : "No matches for \"\(search)\".")
                        .foregroundStyle(.secondary)
                        .font(.subheadline)
                } else {
                    ForEach(filtered) { pkg in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(pkg.name)
                                    .font(.body.monospaced())
                                if let v = pkg.version, !v.isEmpty {
                                    Text(v)
                                        .font(.caption2.monospaced())
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                        }
                        .swipeActions(edge: .trailing) {
                            if session.canOperate {
                                Button(role: .destructive) {
                                    pendingRemove = pkg
                                } label: { Label("Remove", systemImage: "trash") }
                            }
                        }
                    }
                }
            } header: {
                Text(headerText)
            } footer: {
                // The endpoint swallows internal failures and returns [], so an
                // empty list genuinely can mean "something went wrong".
                if packages.isEmpty && errorText == nil && !isLoading {
                    Text("An empty list can also mean the server couldn't read the environment — check that the app's \(runtime == .python ? "virtual environment" : "Cargo.toml") is intact.")
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    private var headerText: String {
        let n = filtered.count
        if n == packages.count { return "\(packages.count) installed" }
        return "\(n) of \(packages.count)"
    }

    private var filtered: [Package] {
        let term = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !term.isEmpty else { return packages }
        return packages.filter { $0.name.lowercased().contains(term) }
    }

    // MARK: - Actions

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        do {
            let fetched = try await client.packages(appID: appID)
            packages = fetched.sorted {
                $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
            }
            errorText = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure(); return }
            errorText = error.localizedDescription
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    private func install(_ spec: String) {
        guard let client = session.client else { return }
        let cleaned = spec.trimmingCharacters(in: .whitespaces)
        guard !cleaned.isEmpty else { return }
        busyMessage = "Installing \(cleaned)…"
        Task {
            defer { busyMessage = nil }
            do {
                try await client.installPackage(appID: appID, spec: cleaned)
                session.toasts.success("Installed \(cleaned).")
                await load()
                if scan != nil { runScan() }
            } catch {
                session.report(error, context: "Couldn't install \(cleaned)")
            }
        }
    }

    private func installAll(_ names: [String]) {
        guard let client = session.client else { return }
        Task {
            var installed = 0
            for (index, name) in names.enumerated() {
                busyMessage = "Installing \(name) (\(index + 1) of \(names.count))…"
                do {
                    try await client.installPackage(appID: appID, spec: name)
                    installed += 1
                } catch {
                    // Keep going — one bad name shouldn't abort the batch.
                    session.toasts.error("\(name): \((error as? APIError)?.localizedDescription ?? error.localizedDescription)")
                }
            }
            busyMessage = nil
            if installed > 0 {
                session.toasts.success("Installed \(installed) of \(names.count).")
            }
            await load()
            runScan()
        }
    }

    private func remove(_ pkg: Package) {
        guard let client = session.client else { return }
        pendingRemove = nil
        busyMessage = "Removing \(pkg.name)…"
        Task {
            defer { busyMessage = nil }
            do {
                try await client.uninstallPackage(appID: appID, name: pkg.name)
                session.toasts.success("Removed \(pkg.name).")
                await load()
            } catch {
                session.report(error, context: "Couldn't remove \(pkg.name)")
            }
        }
    }

    private func runScan() {
        guard let client = session.client, runtime == .python, !isScanning else { return }
        isScanning = true
        Task {
            defer { isScanning = false }
            do {
                scan = try await client.scanImports(appID: appID)
            } catch {
                session.report(error, context: "Import scan failed")
            }
        }
    }
}
