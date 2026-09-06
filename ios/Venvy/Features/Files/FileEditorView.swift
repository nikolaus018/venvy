import SwiftUI

/// Text editor for a single sandboxed file.
///
/// Saving goes through `POST /files/op` with `operation: "save"`, which
/// overwrites the whole file — so the editor loads the current contents first
/// and refuses to save if it never managed to load them.
struct FileEditorView: View {
    let appID: Int
    let node: FileNode

    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var text = ""
    @State private var originalText = ""
    @State private var isLoading = true
    @State private var loadError: String?
    @State private var isSaving = false
    @State private var lastSaved: Date?
    @State private var confirmDiscard = false
    @State private var wrapLines = true
    @FocusState private var editorFocused: Bool

    private var isDirty: Bool { text != originalText }
    private var isEditable: Bool { session.canOperate && loadError == nil && !isLoading }

    var body: some View {
        Group {
            if isLoading {
                ProgressView("Loading \(node.name)…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError {
                EmptyStateView(icon: "doc.questionmark", title: "Can't open this file",
                               message: loadError, actionTitle: "Try again") {
                    Task { await load() }
                }
            } else {
                editor
            }
        }
        .navigationTitle(node.name)
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(isDirty)
        .toolbar {
            if isDirty {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Back") { confirmDiscard = true }
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                if isSaving {
                    ProgressView().controlSize(.small)
                } else if isEditable {
                    Button("Save") { Task { await save() } }
                        .fontWeight(.semibold)
                        .disabled(!isDirty)
                }
            }
            ToolbarItem(placement: .bottomBar) {
                statusBar
            }
        }
        .task { await load() }
        .alert("Discard changes?", isPresented: $confirmDiscard) {
            Button("Discard", role: .destructive) {
                text = originalText
                dismiss()
            }
            Button("Save and close") {
                Task {
                    await save()
                    if !isDirty { dismiss() }
                }
            }
            Button("Keep editing", role: .cancel) {}
        } message: {
            Text("\(node.name) has unsaved changes.")
        }
    }

    private var editor: some View {
        VStack(spacing: 0) {
            if !session.canOperate {
                HStack(spacing: 6) {
                    Image(systemName: "eye")
                    Text("Read-only — your role can't modify files.")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color(.secondarySystemBackground))
            }

            if wrapLines {
                TextEditor(text: $text)
                    .font(.system(.callout, design: .monospaced))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .scrollContentBackground(.hidden)
                    .background(Color(.systemBackground))
                    .focused($editorFocused)
                    .disabled(!isEditable)
            } else {
                // Horizontal scrolling keeps long lines readable for code that
                // relies on alignment; TextEditor itself always wraps.
                ScrollView(.horizontal) {
                    TextEditor(text: $text)
                        .font(.system(.callout, design: .monospaced))
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .scrollContentBackground(.hidden)
                        .frame(width: 1200)
                        .focused($editorFocused)
                        .disabled(!isEditable)
                }
                .background(Color(.systemBackground))
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Button("Tab") { insert("    ") }
                Button("(") { insert("(") }
                Button("[") { insert("[") }
                Button("{") { insert("{") }
                Button("\"") { insert("\"") }
                Button(":") { insert(":") }
                Spacer()
                Button("Done") { editorFocused = false }
                    .fontWeight(.semibold)
            }
        }
    }

    private var statusBar: some View {
        HStack(spacing: 10) {
            Menu {
                Toggle("Wrap lines", isOn: $wrapLines)
            } label: {
                Image(systemName: "textformat.size")
            }
            Text("\(lineCount) lines · \(text.count) chars")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Spacer()
            if isDirty {
                Label("Unsaved", systemImage: "circle.fill")
                    .font(.caption2)
                    .foregroundStyle(VenvyColor.warning)
                    .labelStyle(.titleAndIcon)
                    .imageScale(.small)
            } else if let lastSaved {
                Label("Saved \(Fmt.relative(lastSaved))", systemImage: "checkmark.circle.fill")
                    .font(.caption2)
                    .foregroundStyle(VenvyColor.success)
                    .imageScale(.small)
            }
        }
    }

    private var lineCount: Int {
        text.isEmpty ? 0 : text.reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
    }

    private func insert(_ s: String) {
        text.append(s)
    }

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        loadError = nil
        do {
            let contents = try await client.fileContents(appID: appID, path: node.path)
            text = contents
            originalText = contents
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure() }
            loadError = error.localizedDescription
        } catch {
            loadError = error.localizedDescription
        }
        isLoading = false
    }

    private func save() async {
        guard let client = session.client, isEditable, isDirty, !isSaving else { return }
        isSaving = true
        defer { isSaving = false }
        do {
            try await client.fileOp(appID: appID, operation: .save,
                                    path: node.path, content: text)
            originalText = text
            lastSaved = Date()
            session.toasts.success("Saved \(node.name).")
        } catch {
            session.report(error, context: "Couldn't save \(node.name)")
        }
    }
}
