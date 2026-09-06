import Foundation

// MARK: - Request bodies

struct LoginRequest: Encodable {
    var username: String
    var password: String
    var totp_code: String?
}

struct SetupRequest: Encodable {
    var username: String
    var password: String
}

struct PasswordChangeRequest: Encodable {
    var current_password: String
    var password: String
    var password_confirm: String
}

struct TOTPCodeRequest: Encodable { var totp_code: String }
struct PasswordRequest: Encodable { var password: String }

struct CreateUserRequest: Encodable {
    var username: String
    var password: String
    var role: String
}

/// Partial update. Swift's synthesised encoder uses `encodeIfPresent` for
/// optionals, so `nil` fields are omitted entirely and the server leaves them
/// untouched — exactly the semantics `PUT /api/apps/{id}` documents.
///
/// Note the server's sentinel values: `port: 0` clears the port, and
/// `memory_limit_mb: 0` / `cpu_limit_percent: 0` mean *unlimited*. Those are
/// how "no limit" is expressed, since an omitted field means "unchanged".
struct AppUpdateRequest: Encodable {
    var name: String?
    var start_on_boot: Bool?
    var restart_policy: String?
    var runtime: String?
    var port: Int?
    var manual_ports: [Int]?
    var env_vars: [String: String]?
    var entrypoint: String?
    var url_source_pref: String?
    var memory_limit_mb: Int?
    var cpu_limit_percent: Int?
}

struct FileOpRequest: Encodable {
    var operation: String
    var path: String
    var new_path: String?
    var content: String?
}

struct PackageRequest: Encodable { var name: String }
struct SettingRequest: Encodable { var value: String }
struct SettingItem: Encodable {
    var key: String
    var value: String
}

struct TunnelPublishRequest: Encodable {
    var hostname: String
    var local_port: Int
}

// MARK: - Endpoints

extension APIClient {

    // ---- Auth ----

    func authStatus() async throws -> AuthStatus {
        try await get("/api/auth/status")
    }

    func health() async throws -> HealthResponse {
        try await get("/api/health")
    }

    func login(username: String, password: String, totpCode: String?) async throws -> LoginResponse {
        // Guarantee a csrf_token cookie exists before this POST.
        await ensureCSRFForLogin()
        let body = LoginRequest(username: username, password: password,
                                totp_code: totpCode?.isEmpty == false ? totpCode : nil)
        return try await post("/api/auth/login", body: body)
    }

    private func ensureCSRFForLogin() async {
        if csrfToken == nil { _ = try? await bootstrapCSRF() }
    }

    func setup(username: String, password: String) async throws -> EmptyResponse {
        await ensureCSRFForLogin()
        return try await post("/api/auth/setup", body: SetupRequest(username: username, password: password))
    }

    func logout() async throws {
        let _: EmptyResponse = try await post("/api/auth/logout")
    }

    func changePassword(current: String, new: String, confirm: String) async throws {
        let _: EmptyResponse = try await put("/api/auth/password",
            body: PasswordChangeRequest(current_password: current, password: new, password_confirm: confirm))
    }

    func totpSetup() async throws -> TOTPSetup {
        try await get("/api/auth/totp/setup")
    }

    func totpConfirm(code: String) async throws {
        let _: EmptyResponse = try await post("/api/auth/totp/setup/confirm", body: TOTPCodeRequest(totp_code: code))
    }

    func totpDisable(password: String) async throws {
        let _: EmptyResponse = try await post("/api/auth/totp/disable", body: PasswordRequest(password: password))
    }

    // ---- Users (admin) ----

    func users() async throws -> [VenvyUser] {
        try await get("/api/users")
    }

    func createUser(username: String, password: String, role: Role) async throws {
        let _: EmptyResponse = try await post("/api/users",
            body: CreateUserRequest(username: username, password: password, role: role.rawValue))
    }

    func deleteUser(id: Int) async throws {
        let _: EmptyResponse = try await delete("/api/users/\(id)")
    }

    // ---- Apps ----

    func apps() async throws -> [HostedApp] {
        try await get("/api/apps")
    }

    func updateApp(id: Int, _ update: AppUpdateRequest) async throws -> HostedApp {
        try await put("/api/apps/\(id)", body: update)
    }

    func deleteApp(id: Int) async throws {
        let _: EmptyResponse = try await delete("/api/apps/\(id)")
    }

    /// Queues start/stop/restart. The server returns immediately with an
    /// intermediate status — poll `apps()` for the settled state.
    func control(id: Int, action: String) async throws {
        let _: EmptyResponse = try await post("/api/apps/\(id)/control",
                                              query: [URLQueryItem(name: "action", value: action)])
    }

    func stats(id: Int) async throws -> AppStats {
        try await get("/api/apps/\(id)/stats")
    }

    func createApp(name: String, runtime: Runtime, entrypoint: String,
                   startOnBoot: Bool, restartPolicy: RestartPolicy,
                   files: [(name: String, data: Data)],
                   requirements: (name: String, data: Data)?) async throws -> HostedApp {
        var parts: [UploadPart] = [
            .value("name", name),
            .value("runtime", runtime.rawValue),
            .value("entrypoint", entrypoint),
            .value("start_on_boot", startOnBoot ? "true" : "false"),
            .value("restart_policy", restartPolicy.rawValue),
        ]
        for f in files {
            parts.append(.file("files", filename: f.name, data: f.data))
        }
        if let r = requirements {
            parts.append(.file("requirements", filename: r.name, data: r.data, mimeType: "text/plain"))
        }
        // Dependency install happens inline, so allow the full 10-minute window.
        return try await upload("/api/apps/upload", parts: parts, timeout: 660)
    }

    // ---- Files ----

    func fileTree(appID: Int, path: String) async throws -> [FileNode] {
        try await get("/api/apps/\(appID)/files/tree",
                      query: [URLQueryItem(name: "path", value: path)])
    }

    func fileOp(appID: Int, operation: FileOperation, path: String,
                newPath: String? = nil, content: String? = nil) async throws {
        let body = FileOpRequest(operation: operation.rawValue, path: path,
                                 new_path: newPath, content: content)
        let _: EmptyResponse = try await post("/api/apps/\(appID)/files/op", body: body)
    }

    func uploadFiles(appID: Int, path: String, files: [(name: String, data: Data)]) async throws {
        var parts: [UploadPart] = [.value("path", path)]
        for f in files { parts.append(.file("files", filename: f.name, data: f.data)) }
        let _: EmptyResponse = try await upload("/api/apps/\(appID)/files/upload", parts: parts)
    }

    // ---- Packages ----

    func packages(appID: Int) async throws -> [Package] {
        try await get("/api/apps/\(appID)/packages")
    }

    func installPackage(appID: Int, spec: String) async throws {
        // pip/cargo installs are allowed up to 300s server-side.
        let _: EmptyResponse = try await postLongRunning("/api/apps/\(appID)/packages",
                                                         body: PackageRequest(name: spec),
                                                         timeout: 330)
    }

    func uninstallPackage(appID: Int, name: String) async throws {
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let _: EmptyResponse = try await delete("/api/apps/\(appID)/packages/\(encoded)")
    }

    func scanImports(appID: Int) async throws -> ImportScan {
        try await get("/api/apps/\(appID)/scan-imports")
    }

    // ---- Logs ----

    func logs(appID: Int, lines: Int = 2000) async throws -> LogResponse {
        try await get("/api/apps/\(appID)/logs",
                      query: [URLQueryItem(name: "lines", value: String(lines))])
    }

    func clearLogs(appID: Int) async throws {
        let _: EmptyResponse = try await delete("/api/apps/\(appID)/logs")
    }

    // ---- System ----

    func systemInfo() async throws -> SystemInfo {
        try await get("/api/system/info")
    }

    func systemPorts() async throws -> PortsInfo {
        try await get("/api/system/ports")
    }

    func systemMetrics() async throws -> SystemMetrics {
        try await get("/api/system/metrics")
    }

    func createBackup() async throws -> BackupResult {
        try await post("/api/system/backup")
    }

    func backups() async throws -> [Backup] {
        try await get("/api/system/backups")
    }

    func setting(key: String) async throws -> String? {
        struct Wrapper: Decodable {
            var value: String?
            init(from decoder: Decoder) throws {
                if let c = try? decoder.container(keyedBy: AnyKey.self) {
                    value = (try? c.decodeIfPresent(String.self, forKey: AnyKey("value"))) ?? nil
                } else {
                    value = try? decoder.singleValueContainer().decode(String.self)
                }
            }
        }
        let w: Wrapper = try await get("/api/system/settings/\(key)")
        return w.value
    }

    func setSetting(key: String, value: String) async throws {
        let _: EmptyResponse = try await put("/api/system/settings/\(key)", body: SettingRequest(value: value))
    }

    func setSettings(_ items: [SettingItem]) async throws {
        let _: EmptyResponse = try await post("/api/system/settings/batch", body: items)
    }

    // ---- Cloudflare ----

    func testCloudflare() async throws -> [String: JSONValue] {
        try await post("/api/system/cloudflare/test")
    }

    func cloudflareZones() async throws -> [CFZone] {
        // Server replies {"ok": true, "zones": [...]}.
        try await getList("/api/system/cloudflare/zones", keys: ["zones"])
    }

    func tunnelRoutes(appID: Int) async throws -> [CFRoute] {
        // Server replies {"routes": [...]}.
        try await getList("/api/apps/\(appID)/tunnel", keys: ["routes"])
    }

    func tunnelCheck(appID: Int, hostname: String, localPort: Int) async throws -> TunnelCheck {
        try await get("/api/apps/\(appID)/tunnel/check", query: [
            URLQueryItem(name: "hostname", value: hostname),
            URLQueryItem(name: "local_port", value: String(localPort)),
        ])
    }

    func tunnelPublish(appID: Int, hostname: String, localPort: Int) async throws -> PublishResult {
        try await post("/api/apps/\(appID)/tunnel/publish",
                       body: TunnelPublishRequest(hostname: hostname, local_port: localPort))
    }

    func tunnelUnpublish(appID: Int, localPort: Int) async throws {
        let _: EmptyResponse = try await delete("/api/apps/\(appID)/tunnel/publish",
                                                query: [URLQueryItem(name: "local_port", value: String(localPort))])
    }
}
