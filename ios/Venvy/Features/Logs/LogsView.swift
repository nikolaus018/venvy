import SwiftUI

struct LogsView: View {
    let appID: Int
    let appName: String

    @EnvironmentObject private var session: VenvySession

    @State private var lines: [LogLine] = []
    @State private var truncated = false
    @State private var totalLines = 0
    @State private var isLoading = true
    @State private var errorText: String?
    @State private var follow = true
    @State private var filter = ""
    @State private var lineLimit = 2000
    @State private var confirmClear = false
    @State private var pollTask: Task<Void, Never>?
    @State private var shareFile: ShareableFile?

    struct LogLine: Identifiable, Equatable {
        let id: Int
        /// Styled runs parsed from the log's own ANSI escapes. App output is
        /// frequently colourised (Flask, pip, pytest), and the endpoint hands
        /// those codes back verbatim — rendering them as literal text would
        /// show "[33mGET" to the user.
        let spans: [ANSIParser.Span]
        let text: String
        /// Rough severity, inferred from the text, used only for runs that
        /// carry no colour of their own.
        let level: Level

        enum Level { case plain, warning, error }

        var color: Color {
            switch level {
            case .plain: return .primary
            case .warning: return VenvyColor.warning
            case .error: return VenvyColor.danger
            }
        }
    }

    var body: some View {
        Group {
            if isLoading && lines.isEmpty {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText, lines.isEmpty {
                EmptyStateView(icon: "exclamationmark.triangle", title: "Couldn't load logs",
                               message: errorText, actionTitle: "Try again") {
                    Task { await load() }
                }
            } else if lines.isEmpty {
                EmptyStateView(icon: "text.alignleft", title: "No log output yet",
                               message: "Output appears here once the app writes to stdout or stderr.")
            } else {
                logScroll
            }
        }
        .navigationTitle("Logs")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $filter, prompt: "Filter lines")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Toggle(isOn: $follow) {
                        Label("Follow output", systemImage: "arrow.down.to.line")
                    }
                    Picker("Tail length", selection: $lineLimit) {
                        Text("500 lines").tag(500)
                        Text("2,000 lines").tag(2000)
                        Text("10,000 lines").tag(10000)
                    }
                    Divider()
                    Button {
                        Task { await load() }
                    } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                    Button {
                        copyAll()
                    } label: { Label("Copy all", systemImage: "doc.on.doc") }
                    Button {
                        exportLogs()
                    } label: { Label("Export…", systemImage: "square.and.arrow.up") }
                    if session.canOperate {
                        Divider()
                        Button(role: .destructive) {
                            confirmClear = true
                        } label: { Label("Clear log", systemImage: "trash") }
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .accessibilityLabel("Log options")
            }
        }
        .task { await startPolling() }
        .onDisappear { pollTask?.cancel() }
        .onChange(of: lineLimit) { _, _ in Task { await load() } }
        .onChange(of: follow) { _, on in
            Task { on ? await startPolling() : pollTask?.cancel() }
        }
        .sheet(item: $shareFile) { ShareSheet(items: [$0.url]) }
        .alert("Clear this log?", isPresented: $confirmClear) {
            Button("Clear", role: .destructive) { clear() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The log file for \(appName) will be emptied on the server. This can't be undone.")
        }
    }

    private var logScroll: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    if truncated {
                        Label("Showing the tail of a larger log (\(totalLines) lines total).",
                              systemImage: "scissors")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.bottom, 6)
                    }
                    ForEach(visibleLines) { line in
                        line.spans
                            .reduce(Text("")) { $0 + $1.text(defaultColor: line.color) }
                            .font(.system(size: 11.5, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .id(line.id)
                    }
                    Color.clear.frame(height: 1).id(bottomAnchor)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
            }
            .background(Color(.systemBackground))
            .onChange(of: lines) { _, _ in
                guard follow else { return }
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo(bottomAnchor, anchor: .bottom)
                }
            }
            .onAppear {
                proxy.scrollTo(bottomAnchor, anchor: .bottom)
            }
            .safeAreaInset(edge: .bottom) {
                HStack(spacing: 8) {
                    Circle()
                        .fill(follow ? VenvyColor.success : Color.secondary)
                        .frame(width: 6, height: 6)
                    Text(follow ? "Following" : "Paused")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text(countLabel)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Button {
                        withAnimation { proxy.scrollTo(bottomAnchor, anchor: .bottom) }
                    } label: {
                        Image(systemName: "arrow.down.to.line")
                            .font(.caption)
                    }
                    .accessibilityLabel("Scroll to latest")
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(.bar)
            }
        }
    }

    private let bottomAnchor = "log-bottom"

    private var visibleLines: [LogLine] {
        let term = filter.trimmingCharacters(in: .whitespaces).lowercased()
        guard !term.isEmpty else { return lines }
        return lines.filter { $0.text.lowercased().contains(term) }
    }

    private var countLabel: String {
        let shown = visibleLines.count
        if shown == lines.count { return "\(lines.count) lines" }
        return "\(shown) of \(lines.count)"
    }

    // MARK: - Data

    private func startPolling() async {
        await load()
        pollTask?.cancel()
        guard follow else { return }
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                if Task.isCancelled { return }
                await load(silent: true)
            }
        }
    }

    private func load(silent: Bool = false) async {
        guard let client = session.client else { return }
        if !silent { isLoading = true }
        do {
            let res = try await client.logs(appID: appID, lines: lineLimit)
            // Run the whole blob through the ANSI parser so escape codes become
            // styling rather than literal text.
            var parser = ANSIParser(maxLines: max(lineLimit, 1000))
            var parsed = parser.feed(res.content)
            // Drop the trailing empty line the final newline produces.
            if parsed.last?.isEmpty == true { parsed.removeLast() }
            lines = parsed.enumerated().map { index, line in
                let plain = line.plainText
                return LogLine(id: index, spans: line.spans, text: plain,
                               level: Self.level(of: plain))
            }
            truncated = res.truncated
            totalLines = res.totalLines
            errorText = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure(); return }
            errorText = error.localizedDescription
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    /// Best-effort severity colouring. The endpoint returns an undifferentiated
    /// blob, so this is a display nicety, not a parsed log level.
    private static func level(of line: String) -> LogLine.Level {
        let lower = line.lowercased()
        if lower.contains("traceback") || lower.contains("error") || lower.contains("critical")
            || lower.contains("exception") || lower.contains("fatal") {
            return .error
        }
        if lower.contains("warn") { return .warning }
        return .plain
    }

    private func clear() {
        guard let client = session.client else { return }
        Task {
            do {
                try await client.clearLogs(appID: appID)
                lines = []
                totalLines = 0
                truncated = false
                session.toasts.success("Log cleared.")
            } catch {
                session.report(error, context: "Couldn't clear the log")
            }
        }
    }

    private func copyAll() {
        UIPasteboard.general.string = visibleLines.map(\.text).joined(separator: "\n")
        session.toasts.success("Copied \(visibleLines.count) lines.")
    }

    private func exportLogs() {
        let content = visibleLines.map(\.text).joined(separator: "\n")
        let safeName = appName.replacingOccurrences(of: "/", with: "-")
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(safeName)-log.txt")
        do {
            try content.write(to: url, atomically: true, encoding: .utf8)
            shareFile = ShareableFile(url: url)
        } catch {
            session.toasts.error("Couldn't prepare the export.")
        }
    }
}
