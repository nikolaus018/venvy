import Foundation

/// A saved Venvy server. The password is never stored here — it lives in the
/// Keychain keyed by `id` (see `Keychain`).
struct ServerConfig: Codable, Identifiable, Hashable {
    var id: UUID
    var name: String
    /// Normalised absolute origin, e.g. `http://192.168.1.20:54783`.
    var baseURL: String
    var username: String
    /// Whether the user opted to remember the password in the Keychain.
    var savePassword: Bool
    var lastConnected: Date?

    init(id: UUID = UUID(), name: String, baseURL: String, username: String = "",
         savePassword: Bool = false, lastConnected: Date? = nil) {
        self.id = id
        self.name = name
        self.baseURL = baseURL
        self.username = username
        self.savePassword = savePassword
        self.lastConnected = lastConnected
    }

    var url: URL? { URL(string: baseURL) }
    var host: String { url?.host ?? baseURL }

    /// A display string like `192.168.1.20:54783`.
    var displayHost: String {
        guard let u = url, let h = u.host else { return baseURL }
        if let p = u.port { return "\(h):\(p)" }
        return h
    }

    var isSecure: Bool { url?.scheme?.lowercased() == "https" }
}

enum ServerURLError: LocalizedError {
    case empty
    case invalid
    case unsupportedScheme(String)

    var errorDescription: String? {
        switch self {
        case .empty: return "Enter your server's address."
        case .invalid: return "That doesn't look like a valid address."
        case .unsupportedScheme(let s): return "Unsupported scheme \"\(s)\" — use http or https."
        }
    }
}

enum ServerURL {
    /// Turns whatever the user typed into a canonical origin string.
    ///
    /// Accepts `192.168.1.20`, `192.168.1.20:54783`, `venvy.local`,
    /// `venvy.example.com`, `http://host:port`, `https://host/`, trimming
    /// stray paths and whitespace.
    ///
    /// When no scheme is given the guess depends on what kind of host it is:
    /// a bare LAN target (IP literal, single-label name, or `.local`) gets
    /// `http` on Venvy's default port 54783, while a public-looking domain
    /// gets `https` on 443. Guessing http+54783 for a real domain produces a
    /// connection that just hangs, so this distinction matters.
    static func normalize(_ input: String) throws -> String {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw ServerURLError.empty }
        guard let first = candidates(for: trimmed).first else { throw ServerURLError.invalid }
        return first
    }

    /// Ordered origins worth trying for `input`, best guess first.
    ///
    /// Used by the connection test so the user can type just a hostname and
    /// have the app work out the scheme and port for them.
    static func candidates(for input: String) -> [String] {
        let raw = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return [] }

        // An explicit scheme is a decision, not a guess — honour it exactly.
        if raw.contains("://") {
            guard let origin = try? origin(from: raw) else { return [] }
            return [origin]
        }

        guard var comps = URLComponents(string: "http://" + raw),
              let host = comps.host, !host.isEmpty else { return [] }
        let explicitPort = comps.port

        func build(_ scheme: String, _ port: Int?) -> String? {
            comps.scheme = scheme
            comps.port = port
            comps.path = ""
            comps.query = nil
            comps.fragment = nil
            comps.user = nil
            comps.password = nil
            return comps.url?.absoluteString
        }

        var out: [String] = []
        if let port = explicitPort {
            // A port was given, so only the scheme is in question. Plain HTTP
            // is overwhelmingly the norm for a self-hosted box on a LAN.
            out = [build("http", port), build("https", port)].compactMap { $0 }
        } else if isLocalStyle(host) {
            out = [build("http", 54783), build("http", nil), build("https", nil)].compactMap { $0 }
        } else {
            // A public-looking domain is almost certainly fronted by TLS.
            out = [build("https", nil), build("http", 54783), build("http", nil)].compactMap { $0 }
        }

        var seen = Set<String>()
        return out.filter { seen.insert($0).inserted }
    }

    private static func origin(from raw: String) throws -> String {
        guard var comps = URLComponents(string: raw) else { throw ServerURLError.invalid }
        let scheme = (comps.scheme ?? "http").lowercased()
        guard scheme == "http" || scheme == "https" else {
            throw ServerURLError.unsupportedScheme(scheme)
        }
        comps.scheme = scheme
        guard let host = comps.host, !host.isEmpty else { throw ServerURLError.invalid }
        _ = host
        comps.path = ""
        comps.query = nil
        comps.fragment = nil
        comps.user = nil
        comps.password = nil
        guard let url = comps.url else { throw ServerURLError.invalid }
        return url.absoluteString
    }

    /// True for hosts that look like something on the local network rather
    /// than a registered public domain.
    static func isLocalStyle(_ host: String) -> Bool {
        let h = host.lowercased()
        if h == "localhost" || h.hasSuffix(".local") || h.hasSuffix(".lan")
            || h.hasSuffix(".home") || h.hasSuffix(".internal") { return true }
        if h.contains(":") { return true }                 // IPv6 literal
        if !h.contains(".") { return true }                // single-label name
        // Dotted-quad IPv4 literals are LAN-style; a TLD is not.
        let parts = h.split(separator: ".")
        if parts.count == 4, parts.allSatisfy({ Int($0).map { (0...255).contains($0) } == true }) {
            return true
        }
        return false
    }
}

/// Persists the list of servers plus which one was last used.
@MainActor
final class ServerStore: ObservableObject {
    @Published private(set) var servers: [ServerConfig] = []
    @Published var selectedID: UUID?

    private let serversKey = "venvy.servers.v1"
    private let selectedKey = "venvy.selectedServer.v1"
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        load()
    }

    var selected: ServerConfig? {
        guard let id = selectedID else { return nil }
        return servers.first { $0.id == id }
    }

    private func load() {
        if let data = defaults.data(forKey: serversKey),
           let decoded = try? JSONDecoder().decode([ServerConfig].self, from: data) {
            servers = decoded
        }
        if let s = defaults.string(forKey: selectedKey), let id = UUID(uuidString: s),
           servers.contains(where: { $0.id == id }) {
            selectedID = id
        }
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(servers) {
            defaults.set(data, forKey: serversKey)
        }
        if let id = selectedID {
            defaults.set(id.uuidString, forKey: selectedKey)
        } else {
            defaults.removeObject(forKey: selectedKey)
        }
    }

    func upsert(_ server: ServerConfig) {
        if let idx = servers.firstIndex(where: { $0.id == server.id }) {
            servers[idx] = server
        } else {
            servers.append(server)
        }
        persist()
    }

    func remove(_ server: ServerConfig) {
        servers.removeAll { $0.id == server.id }
        Keychain.deletePassword(for: server.id)
        if selectedID == server.id { selectedID = nil }
        persist()
    }

    func select(_ server: ServerConfig?) {
        selectedID = server?.id
        persist()
    }

    func markConnected(_ id: UUID) {
        guard let idx = servers.firstIndex(where: { $0.id == id }) else { return }
        servers[idx].lastConnected = Date()
        persist()
    }
}
