import SwiftUI

struct TerminalView: View {
    let appID: Int
    let appName: String

    @EnvironmentObject private var session: VenvySession
    @StateObject private var terminal = TerminalClient()

    @State private var input = ""
    @State private var fontSize: Double = 11.5
    @State private var autoScroll = true
    /// Locally kept command history, navigated with the ↑/↓ keys. The shell
    /// keeps its own history, but we send whole lines rather than keystrokes,
    /// so its line editor never sees our text field — this keeps recall
    /// predictable on the client side instead.
    @State private var history: [String] = []
    @State private var historyIndex: Int?
    /// Size of the output area, used to tell the PTY how wide it really is.
    @State private var viewport: CGSize = .zero
    @FocusState private var inputFocused: Bool

    private let bottomAnchor = "term-bottom"

    var body: some View {
        VStack(spacing: 0) {
            statusStrip
            Divider()
            output
            Divider()
            inputBar
        }
        .background(Color(.systemBackground))
        .navigationTitle("Terminal")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.hidden, for: .tabBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button { reconnect() } label: {
                        Label("Reconnect", systemImage: "arrow.clockwise")
                    }
                    Button { terminal.clearScrollback() } label: {
                        Label("Clear screen", systemImage: "eraser")
                    }
                    Button { copyOutput() } label: {
                        Label("Copy output", systemImage: "doc.on.doc")
                    }
                    Divider()
                    Toggle(isOn: $autoScroll) {
                        Label("Auto-scroll", systemImage: "arrow.down.to.line")
                    }
                    Picker("Text size", selection: $fontSize) {
                        Text("Small").tag(10.0)
                        Text("Medium").tag(11.5)
                        Text("Large").tag(13.5)
                    }
                    if !history.isEmpty {
                        Divider()
                        Button(role: .destructive) {
                            history.removeAll()
                            historyIndex = nil
                        } label: {
                            Label("Clear command history", systemImage: "clock.arrow.circlepath")
                        }
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .accessibilityLabel("Terminal options")
            }
        }
        .onAppear {
            terminal.configure(appID: appID, session: session)
            connectIfNeeded()
        }
        .onDisappear { terminal.disconnect() }
    }

    // MARK: - Status

    private var statusStrip: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(statusColor)
                .frame(width: 7, height: 7)
            Text(statusText)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Spacer(minLength: 0)
            switch state {
            case .failed:
                Button("Retry") { reconnect() }
                    .font(.caption.weight(.semibold))
            case .closed:
                Button("Reconnect") { reconnect() }
                    .font(.caption.weight(.semibold))
            default:
                EmptyView()
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.bar)
    }

    // MARK: - Output

    private var output: some View {
        GeometryReader { geo in
            ScrollViewReader { proxy in
                ScrollView(.vertical) {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(lines) { line in
                            renderLine(line).id(line.id)
                        }
                        Color.clear.frame(height: 1).id(bottomAnchor)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    // Pin short output to the top instead of letting the
                    // scroll view centre it.
                    .frame(minWidth: geo.size.width,
                           minHeight: geo.size.height,
                           alignment: .topLeading)
                }
                // Scrolling back through output shouldn't drop the keyboard —
                // it's needed again the moment you finish reading.
                .scrollDismissesKeyboard(.never)
                .background(Color(.systemBackground))
                .overlay {
                    if lines.isEmpty { idleOverlay }
                }
                .onChange(of: lines.count) { _, _ in
                    guard autoScroll else { return }
                    proxy.scrollTo(bottomAnchor, anchor: .bottom)
                }
                // Tapping the transcript puts the cursor back in the prompt.
                .contentShape(Rectangle())
                .onTapGesture { inputFocused = true }
            }
            .onAppear { updateViewport(geo.size) }
            .onChange(of: geo.size) { _, size in updateViewport(size) }
            .onChange(of: fontSize) { _, _ in updateViewport(geo.size) }
        }
    }

    /// Reports the real terminal size to the PTY, so the shell wraps its own
    /// output to the screen rather than to the 220-column default.
    private func updateViewport(_ size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        viewport = size
        let cell = characterSize
        let cols = max(20, Int((size.width - 20) / cell.width))
        let rows = max(5, Int((size.height - 16) / cell.height))
        terminal.resize(cols: cols, rows: rows)
    }

    /// Measured cell size for the current monospaced font.
    private var characterSize: CGSize {
        let font = UIFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
        let width = ("M" as NSString).size(withAttributes: [.font: font]).width
        return CGSize(width: max(width, 1), height: font.lineHeight)
    }

    @ViewBuilder
    private var idleOverlay: some View {
        switch state {
        case .connecting:
            VStack(spacing: 10) {
                ProgressView()
                Text("Opening a shell…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        case .connected:
            Text("Connected. Type a command below.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        case .failed(let message):
            EmptyStateView(icon: "exclamationmark.triangle", title: "Terminal unavailable",
                           message: message, actionTitle: "Try again") { reconnect() }
        case .closed:
            EmptyStateView(icon: "powerplug", title: "Disconnected",
                           message: "The shell session ended.",
                           actionTitle: "Reconnect") { reconnect() }
        case .idle:
            EmptyView()
        }
    }

    /// Joins a line's styled spans into one Text so it wraps as a unit.
    private func renderLine(_ line: ANSIParser.Line) -> some View {
        line.spans.reduce(Text("")) { $0 + styled($1) }
            .font(.system(size: fontSize, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
            .lineLimit(nil)
    }

    private func styled(_ span: ANSIParser.Span) -> Text {
        var text = Text(span.text.isEmpty ? " " : span.text)
        let style = span.style
        // `inverse` swaps foreground and background; Text has no per-run
        // background, so approximate by taking the accent colour.
        if style.inverse {
            text = text.foregroundColor(style.background ?? Color.accentColor)
        } else if style.dim {
            text = text.foregroundColor(.secondary)
        } else {
            text = text.foregroundColor(style.foreground ?? .primary)
        }
        if style.bold { text = text.bold() }
        if style.italic { text = text.italic() }
        if style.underline { text = text.underline() }
        return text
    }

    // MARK: - Input

    private var inputBar: some View {
        VStack(spacing: 0) {
            keyStrip
            HStack(spacing: 8) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .foregroundStyle(isLive ? VenvyColor.success : .secondary)
                TextField("command", text: $input, axis: .vertical)
                    .lineLimit(1...4)
                    .font(.system(size: fontSize, design: .monospaced))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textContentType(.none)
                    .keyboardType(.asciiCapable)
                    .submitLabel(.send)
                    .focused($inputFocused)
                    .onSubmit(sendInput)
                    .disabled(!isLive)
                Button(action: sendInput) {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 22))
                        .symbolRenderingMode(.hierarchical)
                }
                .disabled(!isLive || input.isEmpty)
                .accessibilityLabel("Run command")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
        }
        .background(.bar)
    }

    /// Keys a software keyboard can't produce but a shell needs constantly.
    private var keyStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 7) {
                keyButton("↑", wide: false) { recallPrevious() }
                    .disabled(history.isEmpty)
                keyButton("↓", wide: false) { recallNext() }
                    .disabled(historyIndex == nil)

                Divider().frame(height: 18)

                keyButton("tab") { terminal.send("\t") }
                keyButton("esc") { terminal.send("\u{1B}") }
                keyButton("^C") { sendControl("\u{03}", note: "^C") }
                keyButton("^D") { sendControl("\u{04}", note: "^D") }
                keyButton("^Z") { sendControl("\u{1A}", note: "^Z") }
                keyButton("^L") { terminal.send("\u{0C}") }

                Divider().frame(height: 18)

                ForEach(["|", "~", "/", "-", "_", "$", "*", "\"", "'"], id: \.self) { symbol in
                    keyButton(symbol) { insert(symbol) }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
        }
        .scrollDismissesKeyboard(.never)
    }

    private func keyButton(_ label: String, wide: Bool = true,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .frame(minWidth: wide ? 0 : 22)
                .padding(.horizontal, wide ? 10 : 6)
                .padding(.vertical, 6)
        }
        .venvyGlassButton()
        .buttonBorderShape(.capsule)
        .tint(.secondary)
        .disabled(!isLive)
    }

    // MARK: - State

    private var state: TerminalClient.State { terminal.state ?? .idle }
    private var lines: [ANSIParser.Line] { terminal.lines ?? [] }
    private var isLive: Bool { terminal.isLive ?? false }

    private var statusColor: Color {
        switch state {
        case .connected: return VenvyColor.success
        case .connecting: return VenvyColor.warning
        case .failed: return VenvyColor.danger
        case .closed, .idle: return .secondary
        }
    }

    private var statusText: String {
        switch state {
        case .idle: return "Not connected"
        case .connecting: return "Opening a shell in \(appName)…"
        case .connected: return "Connected — \(appName)"
        case .closed(let reason): return reason.map { "Disconnected: \($0)" } ?? "Disconnected"
        case .failed(let message): return message
        }
    }

    // MARK: - Actions

    private func connectIfNeeded() {
        if case .idle = terminal.state {
            let (cols, rows) = measuredGrid()
            terminal.connect(cols: cols, rows: rows)
        }
    }

    private func reconnect() {
        let (cols, rows) = measuredGrid()
        terminal.reconnect(cols: cols, rows: rows)
        inputFocused = true
    }

    private func measuredGrid() -> (cols: Int, rows: Int) {
        guard viewport.width > 0 else { return (80, 24) }
        let cell = characterSize
        return (max(20, Int((viewport.width - 20) / cell.width)),
                max(5, Int((viewport.height - 16) / cell.height)))
    }

    private func sendInput() {
        guard isLive else { return }
        let command = input
        terminal.sendLine(command)

        let trimmed = command.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty && history.last != command {
            history.append(command)
            if history.count > 200 { history.removeFirst(history.count - 200) }
        }
        historyIndex = nil
        input = ""
        // Keep the prompt focused so the keyboard stays put between commands —
        // submitting would otherwise resign first responder.
        inputFocused = true
    }

    private func sendControl(_ sequence: String, note: String) {
        terminal.send(sequence)
        // Control keys act on the running process, not the draft line, so
        // clear whatever was half-typed to match what the shell now sees.
        input = ""
        historyIndex = nil
        inputFocused = true
        _ = note
    }

    private func recallPrevious() {
        guard !history.isEmpty else { return }
        let index = historyIndex.map { max(0, $0 - 1) } ?? history.count - 1
        historyIndex = index
        input = history[index]
        inputFocused = true
    }

    private func recallNext() {
        guard let current = historyIndex else { return }
        let next = current + 1
        if next >= history.count {
            historyIndex = nil
            input = ""
        } else {
            historyIndex = next
            input = history[next]
        }
        inputFocused = true
    }

    private func insert(_ s: String) {
        input += s
        inputFocused = true
    }

    private func copyOutput() {
        UIPasteboard.general.string = lines.map(\.plainText).joined(separator: "\n")
        session.toasts.success("Terminal output copied.")
    }
}
