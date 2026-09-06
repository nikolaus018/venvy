import Foundation

// MARK: - Roles

enum Role: String, Codable, CaseIterable {
    case admin, operator_ = "operator", viewer

    var label: String {
        switch self {
        case .admin: return "Admin"
        case .operator_: return "Operator"
        case .viewer: return "Viewer"
        }
    }

    /// Can mutate apps, files, packages, logs and tunnels.
    var canOperate: Bool { self == .admin || self == .operator_ }
    var isAdmin: Bool { self == .admin }
}

// MARK: - Auth

struct AuthStatus: Decodable {
    var userExists: Bool
    var authenticated: Bool
    var username: String?
    var role: Role?
    var totpEnabled: Bool?

    enum CodingKeys: String, CodingKey {
        case userExists = "user_exists"
        case authenticated
        case username
        case role
        case totpEnabled = "totp_enabled"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        userExists = (try? c.decode(Bool.self, forKey: .userExists)) ?? false
        authenticated = (try? c.decode(Bool.self, forKey: .authenticated)) ?? false
        username = try? c.decodeIfPresent(String.self, forKey: .username)
        role = try? c.decodeIfPresent(Role.self, forKey: .role)
        totpEnabled = try? c.decodeIfPresent(Bool.self, forKey: .totpEnabled)
    }
}

struct LoginResponse: Decodable {
    var ok: Bool
    var username: String
    var role: Role
}

struct TOTPSetup: Decodable {
    var ok: Bool?
    var secret: String
    var uri: String
}

struct HealthResponse: Decodable {
    var ok: Bool?
    var status: String
    var uptimeSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case ok, status
        case uptimeSeconds = "uptime_seconds"
    }
}

// MARK: - Users

struct VenvyUser: Decodable, Identifiable, Hashable {
    var id: Int
    var username: String
    var role: Role
}

// MARK: - Apps

enum AppStatus: String, Codable {
    case stopped, starting, running, stopping, error

    /// Unknown values from a newer server degrade to `.error` rather than
    /// failing the decode of the whole app list.
    init(fromServer raw: String) {
        self = AppStatus(rawValue: raw) ?? .error
    }

    var isTransitional: Bool { self == .starting || self == .stopping }
}

enum RestartPolicy: String, Codable, CaseIterable, Identifiable {
    case never
    case onFailure = "on-failure"
    case always

    var id: String { rawValue }
    var label: String {
        switch self {
        case .never: return "Never"
        case .onFailure: return "On failure"
        case .always: return "Always"
        }
    }
}

enum Runtime: String, Codable, CaseIterable, Identifiable {
    case python, rust
    var id: String { rawValue }
    var label: String { self == .python ? "Python" : "Rust" }
    var requirementsFilename: String { self == .python ? "requirements.txt" : "Cargo.toml" }
}

enum URLSourcePref: String, Codable, CaseIterable, Identifiable {
    case local, publicURL = "public"
    var id: String { rawValue }
    var label: String { self == .local ? "Local" : "Public" }
}

/// Cached metrics attached to the App object by the server's background
/// collector. NOTE: `memory` is **bytes** here, unlike `AppStats.memoryMB`.
struct AppMetrics: Decodable, Hashable {
    var cpu: Double
    var memoryBytes: Int

    enum CodingKeys: String, CodingKey { case cpu, memory }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cpu = (try? c.decode(Double.self, forKey: .cpu)) ?? 0
        memoryBytes = (try? c.decode(Int.self, forKey: .memory)) ?? 0
    }

    init(cpu: Double = 0, memoryBytes: Int = 0) {
        self.cpu = cpu
        self.memoryBytes = memoryBytes
    }
}

/// Freshly sampled stats from `GET /api/apps/{id}/stats`.
/// NOTE: `memory` is **megabytes** here.
struct AppStats: Decodable, Hashable {
    var cpu: Double
    var memoryMB: Double
    var uptime: Double

    enum CodingKeys: String, CodingKey { case cpu, memory, uptime }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cpu = (try? c.decode(Double.self, forKey: .cpu)) ?? 0
        memoryMB = (try? c.decode(Double.self, forKey: .memory)) ?? 0
        uptime = (try? c.decode(Double.self, forKey: .uptime)) ?? 0
    }
}

struct HostedApp: Decodable, Identifiable, Hashable {
    var id: Int
    var userId: Int?
    var name: String
    var slug: String?
    var runtime: Runtime
    var createdAt: Date?
    var updatedAt: Date?
    var status: AppStatus
    var pid: Int?
    var venvPath: String?
    var appDir: String?
    var entrypoint: String?
    var startOnBoot: Bool
    var restartPolicy: RestartPolicy
    var lastExitCode: Int?
    var lastStartTime: Date?
    var lastStopTime: Date?
    var port: Int?
    /// Parsed from the server's JSON-encoded *string* field.
    var manualPorts: [Int]
    /// Parsed from the server's JSON-encoded *string* field.
    var detectedPorts: [Int]
    var externalURL: String?
    /// Parsed from the server's JSON-encoded *string* field. Empty for viewers.
    var envVars: [String: String]
    /// Only sent to viewers: true when env vars exist but were redacted.
    var envRedacted: Bool
    var healthcheckPath: String?
    var urlSourcePref: URLSourcePref
    var memoryLimitMB: Int?
    var cpuLimitPercent: Int?
    var systemdScope: String?
    var localURL: String?
    var metrics: AppMetrics
    var accessURL: String?
    var urlSource: String?

    enum CodingKeys: String, CodingKey {
        case id, name, slug, runtime, status, pid, entrypoint, port, metrics
        case userId = "user_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case venvPath = "venv_path"
        case appDir = "app_dir"
        case startOnBoot = "start_on_boot"
        case restartPolicy = "restart_policy"
        case lastExitCode = "last_exit_code"
        case lastStartTime = "last_start_time"
        case lastStopTime = "last_stop_time"
        case manualPorts = "manual_ports"
        case detectedPorts = "detected_ports"
        case externalURL = "external_url"
        case envJSON = "env_json"
        case envRedacted = "env_redacted"
        case healthcheckPath = "healthcheck_path"
        case urlSourcePref = "url_source_pref"
        case memoryLimitMB = "memory_limit_mb"
        case cpuLimitPercent = "cpu_limit_percent"
        case systemdScope = "systemd_scope"
        case localURL = "local_url"
        case accessURL = "access_url"
        case urlSource = "url_source"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        userId = try? c.decodeIfPresent(Int.self, forKey: .userId)
        name = (try? c.decode(String.self, forKey: .name)) ?? "Untitled"
        slug = try? c.decodeIfPresent(String.self, forKey: .slug)
        runtime = Runtime(rawValue: (try? c.decode(String.self, forKey: .runtime)) ?? "python") ?? .python
        createdAt = HostedApp.date(c, .createdAt)
        updatedAt = HostedApp.date(c, .updatedAt)
        status = AppStatus(fromServer: (try? c.decode(String.self, forKey: .status)) ?? "stopped")
        pid = try? c.decodeIfPresent(Int.self, forKey: .pid)
        venvPath = try? c.decodeIfPresent(String.self, forKey: .venvPath)
        appDir = try? c.decodeIfPresent(String.self, forKey: .appDir)
        entrypoint = try? c.decodeIfPresent(String.self, forKey: .entrypoint)
        startOnBoot = (try? c.decode(Bool.self, forKey: .startOnBoot)) ?? false
        restartPolicy = RestartPolicy(rawValue: (try? c.decode(String.self, forKey: .restartPolicy)) ?? "never") ?? .never
        lastExitCode = try? c.decodeIfPresent(Int.self, forKey: .lastExitCode)
        lastStartTime = HostedApp.date(c, .lastStartTime)
        lastStopTime = HostedApp.date(c, .lastStopTime)
        port = try? c.decodeIfPresent(Int.self, forKey: .port)
        manualPorts = HostedApp.intArray(c, .manualPorts)
        detectedPorts = HostedApp.intArray(c, .detectedPorts)
        externalURL = try? c.decodeIfPresent(String.self, forKey: .externalURL)
        envVars = HostedApp.stringDict(c, .envJSON)
        envRedacted = ((try? c.decodeIfPresent(Bool.self, forKey: .envRedacted)) ?? nil) ?? false
        healthcheckPath = try? c.decodeIfPresent(String.self, forKey: .healthcheckPath)
        urlSourcePref = URLSourcePref(rawValue: (try? c.decode(String.self, forKey: .urlSourcePref)) ?? "local") ?? .local
        memoryLimitMB = try? c.decodeIfPresent(Int.self, forKey: .memoryLimitMB)
        cpuLimitPercent = try? c.decodeIfPresent(Int.self, forKey: .cpuLimitPercent)
        systemdScope = try? c.decodeIfPresent(String.self, forKey: .systemdScope)
        localURL = try? c.decodeIfPresent(String.self, forKey: .localURL)
        metrics = ((try? c.decodeIfPresent(AppMetrics.self, forKey: .metrics)) ?? nil) ?? AppMetrics()
        accessURL = try? c.decodeIfPresent(String.self, forKey: .accessURL)
        urlSource = try? c.decodeIfPresent(String.self, forKey: .urlSource)
    }

    // `manual_ports` / `detected_ports` / `env_json` arrive as JSON *encoded
    // into a string*, so they need a second parse pass. Accept a native
    // array/object too, in case a future server build stops double-encoding.

    private static func date(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> Date? {
        guard let raw = ((try? c.decodeIfPresent(String.self, forKey: key)) ?? nil) else { return nil }
        return ISO8601.parse(raw)
    }

    private static func intArray(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> [Int] {
        if let native = ((try? c.decodeIfPresent([Int].self, forKey: key)) ?? nil) { return native }
        guard let raw = ((try? c.decodeIfPresent(String.self, forKey: key)) ?? nil),
              let data = raw.data(using: .utf8),
              let parsed = try? JSONDecoder().decode([Int].self, from: data) else { return [] }
        return parsed
    }

    private static func stringDict(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> [String: String] {
        if let native = ((try? c.decodeIfPresent([String: String].self, forKey: key)) ?? nil) { return native }
        guard let raw = ((try? c.decodeIfPresent(String.self, forKey: key)) ?? nil),
              let data = raw.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return [:] }
        // Values are normally strings; coerce anything else defensively.
        return obj.reduce(into: [String: String]()) { acc, kv in
            acc[kv.key] = (kv.value as? String) ?? String(describing: kv.value)
        }
    }

    /// Every port worth showing, primary first, de-duplicated.
    var allPorts: [Int] {
        var seen = Set<Int>()
        var out: [Int] = []
        for p in ([port].compactMap { $0 } + manualPorts + detectedPorts) where !seen.contains(p) {
            seen.insert(p)
            out.append(p)
        }
        return out
    }
}

// MARK: - Files

struct FileNode: Decodable, Identifiable, Hashable {
    var name: String
    var path: String
    var isDir: Bool
    var size: Int

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case name, path, size
        case isDir = "is_dir"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        path = try c.decode(String.self, forKey: .path)
        isDir = (try? c.decode(Bool.self, forKey: .isDir)) ?? false
        size = (try? c.decode(Int.self, forKey: .size)) ?? 0
    }
}

enum FileOperation: String, Encodable {
    case createFile = "create_file"
    case createFolder = "create_folder"
    case delete, rename, copy, save
}

// MARK: - Packages

struct Package: Decodable, Identifiable, Hashable {
    var name: String
    var version: String?
    var id: String { name }

    enum CodingKeys: String, CodingKey { case name, version }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        version = try? c.decodeIfPresent(String.self, forKey: .version)
    }
}

struct ImportScan: Decodable {
    var missing: [String]
    var installedCount: Int

    enum CodingKeys: String, CodingKey {
        case missing
        case installedCount = "installed_count"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        missing = (try? c.decode([String].self, forKey: .missing)) ?? []
        installedCount = (try? c.decode(Int.self, forKey: .installedCount)) ?? 0
    }
}

// MARK: - Logs

struct LogResponse: Decodable {
    var content: String
    var truncated: Bool
    var totalLines: Int

    enum CodingKeys: String, CodingKey {
        case content, truncated
        case totalLines = "total_lines"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        content = (try? c.decode(String.self, forKey: .content)) ?? ""
        truncated = (try? c.decode(Bool.self, forKey: .truncated)) ?? false
        totalLines = (try? c.decode(Int.self, forKey: .totalLines)) ?? 0
    }
}

// MARK: - System

struct SystemInfo: Decodable {
    var ipv4: String?
}

struct Backup: Decodable, Identifiable, Hashable {
    var filename: String
    var sizeBytes: Int
    var createdAt: String?

    var id: String { filename }

    enum CodingKeys: String, CodingKey {
        case filename
        case sizeBytes = "size_bytes"
        case createdAt = "created_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        filename = try c.decode(String.self, forKey: .filename)
        sizeBytes = (try? c.decode(Int.self, forKey: .sizeBytes)) ?? 0
        createdAt = try? c.decodeIfPresent(String.self, forKey: .createdAt)
    }
}

struct BackupResult: Decodable {
    var ok: Bool?
    var path: String?
    var sizeBytes: Int?

    enum CodingKeys: String, CodingKey {
        case ok, path
        case sizeBytes = "size_bytes"
    }
}

/// `GET /api/system/metrics`.
///
/// The server nests its values (`cpu.percent`, `memory.used`, `disk.percent`,
/// …) and returns `{"error": "psutil not installed"}` when the optional
/// dependency is missing, so decode defensively and accept flat spellings too
/// in case a different build returns them.
struct SystemMetrics: Decodable {
    var error: String?
    var cpuPercent: Double?
    var cpuCores: Int?
    var loadAverage: [Double]?
    var memoryPercent: Double?
    var memoryUsedBytes: Int?
    var memoryTotalBytes: Int?
    var memoryAvailableBytes: Int?
    var diskPercent: Double?
    var diskUsedBytes: Int?
    var diskTotalBytes: Int?
    var diskFreeBytes: Int?
    var localIP: String?
    var uptimeSeconds: Double?

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let root = (try? container.decode([String: JSONValue].self)) ?? [:]
        error = root["error"]?.stringValue

        let cpu = Self.object(root["cpu"])
        cpuPercent = cpu["percent"]?.doubleValue ?? root["cpu_percent"]?.doubleValue
            ?? root["cpu"]?.doubleValue
        cpuCores = cpu["cores"]?.doubleValue.map { Int($0) }
        if case let .array(items)? = (cpu["load"] ?? root["load_average"] ?? root["load"]) {
            let values = items.compactMap { $0.doubleValue }
            loadAverage = values.isEmpty ? nil : values
        }

        let memory = Self.object(root["memory"])
        memoryPercent = memory["percent"]?.doubleValue ?? root["memory_percent"]?.doubleValue
        memoryUsedBytes = Self.int(memory["used"] ?? root["memory_used"])
        memoryTotalBytes = Self.int(memory["total"] ?? root["memory_total"])
        memoryAvailableBytes = Self.int(memory["available"])

        let disk = Self.object(root["disk"])
        diskPercent = disk["percent"]?.doubleValue ?? root["disk_percent"]?.doubleValue
        diskUsedBytes = Self.int(disk["used"] ?? root["disk_used"])
        diskTotalBytes = Self.int(disk["total"] ?? root["disk_total"])
        diskFreeBytes = Self.int(disk["free"])

        localIP = root["local_ip"]?.stringValue
        uptimeSeconds = root["uptime"]?.doubleValue
    }

    private static func object(_ value: JSONValue?) -> [String: JSONValue] {
        if case let .object(dict)? = value { return dict }
        return [:]
    }

    private static func int(_ value: JSONValue?) -> Int? {
        value?.doubleValue.map { Int($0) }
    }
}

// MARK: - Cloudflare

struct CFZone: Decodable, Identifiable, Hashable {
    var id: String
    var name: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        id = (try? c.decode(String.self, forKey: AnyKey("id"))) ?? UUID().uuidString
        name = (try? c.decode(String.self, forKey: AnyKey("name"))) ?? ""
    }
}

struct CFRoute: Decodable, Identifiable, Hashable {
    var hostname: String
    var localPort: Int?
    var cfRouteId: String?
    /// A row can exist without a live Cloudflare route behind it — the server
    /// reports that as `published: false`.
    var published: Bool

    var id: String { hostname + "-" + String(localPort ?? 0) }

    enum CodingKeys: String, CodingKey {
        case hostname, published
        case localPort = "local_port"
        case cfRouteId = "cf_route_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hostname = (try? c.decode(String.self, forKey: .hostname)) ?? ""
        localPort = try? c.decodeIfPresent(Int.self, forKey: .localPort)
        cfRouteId = try? c.decodeIfPresent(String.self, forKey: .cfRouteId)
        published = ((try? c.decodeIfPresent(Bool.self, forKey: .published)) ?? nil)
            ?? (cfRouteId?.isEmpty == false)
    }
}

/// Result of `GET /tunnel/check`. Note this endpoint reports a *conflict*
/// rather than an ok/error — a conflict is a warning that publishing will
/// take the hostname over, not a hard failure.
struct TunnelCheck: Decodable {
    var conflict: Bool
    var conflictType: String?
    var reason: String?
    var ownedBy: String?

    enum CodingKeys: String, CodingKey {
        case conflict, reason
        case conflictType = "conflict_type"
        case ownedBy = "owned_by"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        conflict = (try? c.decode(Bool.self, forKey: .conflict)) ?? false
        conflictType = try? c.decodeIfPresent(String.self, forKey: .conflictType)
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
        ownedBy = try? c.decodeIfPresent(String.self, forKey: .ownedBy)
    }
}

struct PublishResult: Decodable {
    var ok: Bool?
    var hostname: String?
    var url: String?
}

// MARK: - Ports

struct PortsInfo: Decodable {
    var listening: [Int]
    var recommended: [Int]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyKey.self)
        listening = PortsInfo.ints(c, ["listening", "in_use", "used"])
        recommended = PortsInfo.ints(c, ["recommended", "free", "available"])
    }

    private static func ints(_ c: KeyedDecodingContainer<AnyKey>, _ keys: [String]) -> [Int] {
        for k in keys {
            if let v = ((try? c.decodeIfPresent([Int].self, forKey: AnyKey(k))) ?? nil) { return v }
            // Some builds return [{ "port": n }] objects instead of bare ints.
            if let objs = ((try? c.decodeIfPresent([[String: JSONValue]].self, forKey: AnyKey(k))) ?? nil) {
                let ports = objs.compactMap { $0["port"]?.doubleValue.map { Int($0) } }
                if !ports.isEmpty { return ports }
            }
        }
        return []
    }
}

// MARK: - Helpers

struct AnyKey: CodingKey {
    var stringValue: String
    var intValue: Int? { nil }
    init(_ s: String) { stringValue = s }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}

/// Minimal JSON tree, for endpoints whose shape varies between server builds.
indirect enum JSONValue: Decodable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .null }
    }

    var stringValue: String? { if case let .string(s) = self { return s }; return nil }
    var doubleValue: Double? {
        switch self {
        case .number(let n): return n
        case .string(let s): return Double(s)
        default: return nil
        }
    }
}

enum ISO8601 {
    private static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// Timestamps arrive timezone-aware, sometimes with fractional seconds
    /// and sometimes without, so try both spellings.
    static func parse(_ s: String) -> Date? {
        withFraction.date(from: s) ?? plain.date(from: s)
    }
}
