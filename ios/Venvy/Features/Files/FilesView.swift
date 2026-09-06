import SwiftUI
import UniformTypeIdentifiers

struct FilesView: View {
    let appID: Int
    let appName: String
    /// Relative path inside the app sandbox; "" is the app root.
    var path: String = ""

    @EnvironmentObject private var session: VenvySession

    @State private var nodes: [FileNode] = []
    @State private var isLoading = true
    @State private var errorText: String?

    @State private var newItem: NewItemKind?
    @State private var newItemName = ""
    @State private var renaming: FileNode?
    @State private var renameText = ""
    @State private var pendingDelete: FileNode?
    @State private var importing = false
    @State private var exportURL: ShareableFile?
    @State private var busyMessage: String?

    private enum NewItemKind: String, Identifiable {
        case file, folder
        var id: String { rawValue }
        var title: String { self == .file ? "New file" : "New folder" }
    }

    var body: some View {
        Group {
            if isLoading && nodes.isEmpty {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText, nodes.isEmpty {
                EmptyStateView(icon: "exclamationmark.triangle", title: "Couldn't load files",
                               message: errorText, actionTitle: "Try again") {
                    Task { await load() }
                }
            } else if nodes.isEmpty {
                EmptyStateView(icon: "folder", title: "This folder is empty",
                               message: session.canOperate
                                 ? "Add a file or upload from your device."
                                 : nil)
            } else {
                list
            }
        }
        .navigationTitle(path.isEmpty ? "Files" : (path as NSString).lastPathComponent)
        .navigationBarTitleDisplayMode(.inline)
        .background(Color(.systemGroupedBackground))
        .toolbar {
            if session.canOperate {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button {
                            newItemName = ""
                            newItem = .file
                        } label: { Label("New file", systemImage: "doc.badge.plus") }
                        Button {
                            newItemName = ""
                            newItem = .folder
                        } label: { Label("New folder", systemImage: "folder.badge.plus") }
                        Divider()
                        Button {
                            importing = true
                        } label: { Label("Upload files", systemImage: "arrow.up.doc") }
                    } label: {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("Add")
                }
            }
        }
        .overlay {
            if let busyMessage {
                BusyOverlay(message: busyMessage)
            }
        }
        .task { await load() }
        .refreshable { await load() }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.item],
                      allowsMultipleSelection: true) { result in
            handleImport(result)
        }
        .sheet(item: $exportURL) { file in
            ShareSheet(items: [file.url])
        }
        .alert(newItem?.title ?? "", isPresented: .init(
            get: { newItem != nil }, set: { if !$0 { newItem = nil } }
        )) {
            TextField(newItem == .folder ? "folder name" : "name.py", text: $newItemName)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("Create") { createItem() }
                .disabled(newItemName.trimmingCharacters(in: .whitespaces).isEmpty)
            Button("Cancel", role: .cancel) { newItem = nil }
        }
        .alert("Rename", isPresented: .init(
            get: { renaming != nil }, set: { if !$0 { renaming = nil } }
        )) {
            TextField("new name", text: $renameText)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            Button("Rename") { performRename() }
            Button("Cancel", role: .cancel) { renaming = nil }
        }
        .alert("Delete \(pendingDelete?.name ?? "")?", isPresented: .init(
            get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }
        ), presenting: pendingDelete) { node in
            Button("Delete", role: .destructive) { performDelete(node) }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: { node in
            Text(node.isDir
                 ? "This permanently deletes the folder and everything inside it."
                 : "This permanently deletes the file.")
        }
    }

    private var list: some View {
        List {
            Section {
                ForEach(nodes) { node in
                    row(node)
                }
            } footer: {
                Text("Hidden files and internal directories (venv, __pycache__, logs, .git) aren't listed.")
            }
        }
        .listStyle(.insetGrouped)
    }

    @ViewBuilder
    private func row(_ node: FileNode) -> some View {
        Group {
            if node.isDir {
                NavigationLink {
                    FilesView(appID: appID, appName: appName, path: node.path)
                } label: {
                    fileLabel(node)
                }
            } else {
                NavigationLink {
                    FileEditorView(appID: appID, node: node)
                } label: {
                    fileLabel(node)
                }
            }
        }
        .swipeActions(edge: .trailing) {
            if session.canOperate {
                Button(role: .destructive) {
                    pendingDelete = node
                } label: { Label("Delete", systemImage: "trash") }
                Button {
                    renameText = node.name
                    renaming = node
                } label: { Label("Rename", systemImage: "pencil") }
                .tint(.gray)
            }
        }
        .contextMenu {
            if !node.isDir {
                Button {
                    download(node)
                } label: { Label("Download", systemImage: "arrow.down.circle") }
            }
            Button {
                UIPasteboard.general.string = node.path
                session.toasts.success("Path copied.")
            } label: { Label("Copy path", systemImage: "doc.on.doc") }
            if session.canOperate {
                Button {
                    renameText = node.name
                    renaming = node
                } label: { Label("Rename", systemImage: "pencil") }
                Button {
                    duplicate(node)
                } label: { Label("Duplicate", systemImage: "plus.square.on.square") }
                Divider()
                Button(role: .destructive) {
                    pendingDelete = node
                } label: { Label("Delete", systemImage: "trash") }
            }
        }
    }

    private func fileLabel(_ node: FileNode) -> some View {
        HStack(spacing: 11) {
            Image(systemName: node.isDir ? "folder.fill" : icon(for: node.name))
                .foregroundStyle(node.isDir ? Color.accentColor : Color.secondary)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(node.name).lineLimit(1)
                if !node.isDir {
                    Text(Fmt.bytes(node.size))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func icon(for name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "py": return "chevron.left.forwardslash.chevron.right"
        case "rs", "toml": return "gearshape.2"
        case "json", "yaml", "yml", "ini", "cfg", "env": return "curlybraces"
        case "md", "txt", "log": return "doc.text"
        case "png", "jpg", "jpeg", "gif", "svg", "webp": return "photo"
        case "zip", "gz", "tar", "whl": return "archivebox"
        case "sh", "bash": return "terminal"
        default: return "doc"
        }
    }

    // MARK: - Data

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        do {
            nodes = try await client.fileTree(appID: appID, path: path)
            errorText = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure() }
            errorText = error.localizedDescription
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    private func joined(_ name: String) -> String {
        path.isEmpty ? name : "\(path)/\(name)"
    }

    private func createItem() {
        guard let client = session.client, let kind = newItem else { return }
        let name = newItemName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        newItem = nil
        Task {
            do {
                try await client.fileOp(appID: appID,
                                        operation: kind == .file ? .createFile : .createFolder,
                                        path: joined(name))
                session.toasts.success("Created \(name).")
                await load()
            } catch {
                session.report(error, context: "Couldn't create \(name)")
            }
        }
    }

    private func performRename() {
        guard let client = session.client, let node = renaming else { return }
        let name = renameText.trimmingCharacters(in: .whitespaces)
        renaming = nil
        guard !name.isEmpty, name != node.name else { return }
        Task {
            do {
                try await client.fileOp(appID: appID, operation: .rename,
                                        path: node.path, newPath: joined(name))
                session.toasts.success("Renamed to \(name).")
                await load()
            } catch {
                session.report(error, context: "Couldn't rename \(node.name)")
            }
        }
    }

    private func duplicate(_ node: FileNode) {
        guard let client = session.client else { return }
        let ext = (node.name as NSString).pathExtension
        let base = (node.name as NSString).deletingPathExtension
        let copyName = ext.isEmpty ? "\(base) copy" : "\(base) copy.\(ext)"
        Task {
            do {
                try await client.fileOp(appID: appID, operation: .copy,
                                        path: node.path, newPath: joined(copyName))
                session.toasts.success("Duplicated.")
                await load()
            } catch {
                session.report(error, context: "Couldn't duplicate \(node.name)")
            }
        }
    }

    private func performDelete(_ node: FileNode) {
        guard let client = session.client else { return }
        pendingDelete = nil
        Task {
            do {
                try await client.fileOp(appID: appID, operation: .delete, path: node.path)
                session.toasts.success("Deleted \(node.name).")
                await load()
            } catch {
                session.report(error, context: "Couldn't delete \(node.name)")
            }
        }
    }

    private func download(_ node: FileNode) {
        guard let client = session.client else { return }
        busyMessage = "Downloading…"
        Task {
            defer { busyMessage = nil }
            do {
                let url = try await client.downloadFile(appID: appID, path: node.path)
                exportURL = ShareableFile(url: url)
            } catch {
                session.report(error, context: "Couldn't download \(node.name)")
            }
        }
    }

    private func handleImport(_ result: Result<[URL], Error>) {
        guard let client = session.client else { return }
        switch result {
        case .failure(let error):
            session.report(error, context: "Import failed")
        case .success(let urls):
            guard !urls.isEmpty else { return }
            busyMessage = "Uploading \(urls.count) file\(urls.count == 1 ? "" : "s")…"
            Task {
                defer { busyMessage = nil }
                var payload: [(name: String, data: Data)] = []
                for url in urls {
                    // Files chosen through the picker live outside our sandbox.
                    let scoped = url.startAccessingSecurityScopedResource()
                    defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                    guard let data = try? Data(contentsOf: url) else { continue }
                    guard data.count <= 50 * 1024 * 1024 else {
                        session.toasts.error("\(url.lastPathComponent) is over the 50 MB limit.")
                        continue
                    }
                    payload.append((url.lastPathComponent, data))
                }
                guard !payload.isEmpty else { return }
                do {
                    try await client.uploadFiles(appID: appID, path: path, files: payload)
                    session.toasts.success("Uploaded \(payload.count) file\(payload.count == 1 ? "" : "s").")
                    await load()
                } catch {
                    session.report(error, context: "Upload failed")
                }
            }
        }
    }
}

// MARK: - Support types

struct ShareableFile: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

struct BusyOverlay: View {
    let message: String

    var body: some View {
        ZStack {
            Color.black.opacity(0.18).ignoresSafeArea()
            VStack(spacing: 12) {
                ProgressView().controlSize(.large)
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .padding(24)
            .venvyGlass(in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .transition(.opacity)
    }
}
