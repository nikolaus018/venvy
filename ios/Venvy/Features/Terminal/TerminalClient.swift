import Foundation
import SwiftUI

/// Drives the PTY WebSocket at `WS /api/apps/{id}/terminal`.
///
/// Wire protocol (all text frames):
///   • server → client: raw PTY output, ANSI included, no framing
///   • client → server: raw keystrokes
///   • client → server: "\0" + JSON for control messages (resize only)
///
/// The server sends nothing on connect — an open socket is the only success
/// signal, and a 1008 close is the failure signal.
@MainActor
final class TerminalClient: ObservableObject {

    enum State: Equatable {
        case idle
        case connecting
        case connected
        case closed(reason: String?)
        case failed(String)
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var lines: [ANSIParser.Line] = []

    private var task: URLSessionWebSocketTask?
    private var parser = ANSIParser()
    private var receiveLoop: Task<Void, Never>?
    private var lastSize: (cols: Int, rows: Int)?

    private var appID: Int?
    private weak var session: VenvySession?

    /// Configured after init so the view can own this as a `@StateObject`
    /// (a StateObject initialiser can't read the environment). Idempotent —
    /// only the first call takes effect.
    func configure(appID: Int, session: VenvySession) {
        guard self.appID == nil else { return }
        self.appID = appID
        self.session = session
    }

    var isLive: Bool { state == .connected }

    // MARK: - Lifecycle

    func connect(cols: Int, rows: Int) {
        guard state != .connecting, state != .connected else { return }
        guard let appID, let client = session?.client,
              let socket = client.makeWebSocketTask(appID: appID) else {
            state = .failed("No server connection.")
            return
        }
        parser.reset()
        lines = []
        state = .connecting
        task = socket
        socket.resume()

        // There's no "connected" message, so treat a successful resize write —
        // the first frame we send — as confirmation the socket is live.
        lastSize = (cols, rows)
        Task {
            do {
                try await sendControl(["cols": cols, "rows": rows])
                if case .connecting = state { state = .connected }
            } catch {
                state = .failed(friendly(error))
            }
        }
        startReceiving()
    }

    func disconnect() {
        receiveLoop?.cancel()
        receiveLoop = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        if case .connected = state { state = .closed(reason: nil) }
    }

    func reconnect(cols: Int, rows: Int) {
        disconnect()
        state = .idle
        connect(cols: cols, rows: rows)
    }

    // MARK: - Receiving

    private func startReceiving() {
        receiveLoop?.cancel()
        receiveLoop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, let task = self.task else { return }
                do {
                    let message = try await task.receive()
                    self.handle(message)
                } catch {
                    self.handleReceiveFailure(error)
                    return
                }
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            lines = parser.feed(text)
        case .data(let data):
            // The server documents text frames only, but decode defensively.
            if let text = String(data: data, encoding: .utf8) {
                lines = parser.feed(text)
            }
        @unknown default:
            break
        }
    }

    private func handleReceiveFailure(_ error: Error) {
        guard state == .connected || state == .connecting else { return }
        let closeCode = task?.closeCode ?? .invalid
        let reason = task?.closeReason.flatMap { String(data: $0, encoding: .utf8) }

        // 1008 is the server's "policy violation" close: unauthenticated,
        // expired session, missing app, or a viewer-role user.
        if closeCode == .policyViolation {
            state = .failed(reason?.isEmpty == false
                            ? reason!
                            : "The server refused the terminal session. Operator or admin role is required.")
            if reason?.localizedCaseInsensitiveContains("session") == true {
                session?.handleAuthFailure()
            }
            return
        }
        if closeCode == .goingAway || closeCode == .normalClosure {
            state = .closed(reason: reason)
            return
        }
        state = .failed(friendly(error))
    }

    private func friendly(_ error: Error) -> String {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            switch ns.code {
            case NSURLErrorCannotConnectToHost:
                return "Couldn't open a terminal connection to the server."
            case NSURLErrorNetworkConnectionLost:
                return "The terminal connection dropped."
            case NSURLErrorTimedOut:
                return "The terminal connection timed out."
            default:
                break
            }
        }
        return ns.localizedDescription
    }

    // MARK: - Sending

    func send(_ text: String) {
        guard let task, isLive else { return }
        // A real keystroke never starts with NUL — that byte is reserved as the
        // control-message marker, so drop it defensively.
        let safe = text.replacingOccurrences(of: "\0", with: "")
        guard !safe.isEmpty else { return }
        Task {
            do {
                try await task.send(.string(safe))
            } catch {
                state = .failed(friendly(error))
            }
        }
    }

    func sendLine(_ text: String) {
        send(text + "\n")
    }

    func resize(cols: Int, rows: Int) {
        guard isLive else { return }
        // Don't spam the server with identical sizes on every layout pass.
        if let last = lastSize, last.cols == cols, last.rows == rows { return }
        lastSize = (cols, rows)
        Task { try? await sendControl(["cols": cols, "rows": rows]) }
    }

    private func sendControl(_ payload: [String: Int]) async throws {
        guard let task else { throw APIError.transport("Terminal not connected.") }
        let json = try JSONSerialization.data(withJSONObject: payload)
        guard let body = String(data: json, encoding: .utf8) else { return }
        // Control frames are a leading NUL followed by JSON, max 4096 bytes.
        try await task.send(.string("\0" + body))
    }

    func clearScrollback() {
        parser.reset()
        lines = []
    }
}
