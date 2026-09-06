import SwiftUI

struct TerminalView: View {
    let appID: Int
    let appName: String

    @EnvironmentObject private var session: VenvySession
    @StateObject private var terminal = TerminalClient()

    @State private var input = ""
    @State private var fontSize: Double = 11.5
    @State private var autoScroll = true
    @FocusState private var inputFocused: Bool

    private let bottomAnchor = "term-bottom"

    var body: some View {
        output
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    statusStrip
                    Divider()
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(spacing: 0) {
                    Divider()
                    inputBar
                }
            }
            .background(Color(.systemBackground))
        .navigationTitle("Terminal")
        .navigationBarTitleDisplayMode(.inline)
        // A terminal is a focused, keyboard-driven full-screen tool: hide the
        // tab bar so it can't overlap the command input and the shell gets the
        // whole screen.
        .toolbar(.hidden, for: .tabBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        reconnect()
                    } label: { Label("Reconnect", systemImage: "arrow.clockwise") }
                    Button {
                        terminal.clearScrollback()
                    } label: { Label("Clear screen", systemImage: "eraser") }
                    Button {
                        copyOutput()
                    } label: { Label("Copy output", systemImage: "doc.on.doc") }
                    Divider()
                    Toggle(isOn: $autoScroll) {
                        Label("Auto-scroll", systemImage: "arrow.down.to.line")
                    }
                    Picker("Text size", selection: $fontSize) {
                        Text("Small").tag(10.0)
                        Text("Medium").tag(11.5)
                        Text("Large").tag(13.5)
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

    // MARK: - Pieces

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
            if case .failed = state {
                Button("Retry") { reconnect() }
                    .font(.caption.weight(.semibold))
            } else if case .closed = state {
                Button("Reconnect") { reconnect() }
                    .font(.caption.weight(.semibold))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private var output: some View {
        ScrollViewReader { proxy in
            GeometryReader { geo in
                ScrollView([.vertical, .horizontal]) {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(lines) { line in
                            renderLine(line)
                                .id(line.id)
                        }
                        Color.clear.frame(height: 1).id(bottomAnchor)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    // Fill the viewport so a nearly-empty buffer stays pinned
                    // top-left rather than floating in the middle.
                    .frame(minWidth: geo.size.width,
                           minHeight: geo.size.height,
                           alignment: .topLeading)
                }
                .background(Color(.systemBackground))
                .onChange(of: lines.count) { _, _ in
                    guard autoScroll else { return }
                    proxy.scrollTo(bottomAnchor, anchor: .bottom)
                }
                .onTapGesture { inputFocused = true }
            }
        }
    }

    /// Joins the line's styled spans into one Text so it wraps as a unit.
    private func renderLine(_ line: ANSIParser.Line) -> some View {
        line.spans.reduce(Text("")) { acc, span in
            acc + span.text()
        }
        .font(.system(size: fontSize, design: .monospaced))
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
    }

    private var inputBar: some View {
        VStack(spacing: 0) {
            keyStrip
            Divider()
            HStack(spacing: 8) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .foregroundStyle(VenvyColor.success)
                TextField("command", text: $input)
                    .font(.system(size: fontSize, design: .monospaced))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.send)
                    .focused($inputFocused)
                    .onSubmit(sendInput)
                    .disabled(!isLive)
                Button(action: sendInput) {
                    Image(systemName: "return")
                        .font(.system(size: 13, weight: .semibold))
                }
                .disabled(!isLive || input.isEmpty)
                .accessibilityLabel("Send command")
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
                keyButton("esc") { terminal.send("\u{1B}") }
                keyButton("tab") { terminal.send("\t") }
                keyButton("^C") { terminal.send("\u{03}") }
                keyButton("^D") { terminal.send("\u{04}") }
                keyButton("^Z") { terminal.send("\u{1A}") }
                keyButton("^L") { terminal.send("\u{0C}") }
                keyButton("↑") { terminal.send("\u{1B}[A") }
                keyButton("↓") { terminal.send("\u{1B}[B") }
                keyButton("←") { terminal.send("\u{1B}[D") }
                keyButton("→") { terminal.send("\u{1B}[C") }
                keyButton("|") { insert("|") }
                keyButton("~") { insert("~") }
                keyButton("/") { insert("/") }
                keyButton("-") { insert("-") }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
        }
        .disabled(!isLive)
    }

    private func keyButton(_ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
        }
        .venvyGlassButton()
        .buttonBorderShape(.capsule)
        .tint(.secondary)
    }

    // MARK: - State plumbing

    private var state: TerminalClient.State { terminal.state }
    private var lines: [ANSIParser.Line] { terminal.lines }
    private var isLive: Bool { terminal.isLive }

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
            terminal.connect(cols: 100, rows: 30)
        }
    }

    private func reconnect() {
        terminal.reconnect(cols: 100, rows: 30)
    }

    private func sendInput() {
        guard isLive else { return }
        terminal.sendLine(input)
        input = ""
    }

    private func insert(_ s: String) {
        input += s
    }

    private func copyOutput() {
        UIPasteboard.general.string = lines.map(\.plainText).joined(separator: "\n")
        session.toasts.success("Terminal output copied.")
    }
}
