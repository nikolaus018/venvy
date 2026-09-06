import Foundation

/// Talks to one Venvy instance.
///
/// Auth model mirrors the web frontend: an HttpOnly `session` cookie plus a
/// readable `csrf_token` cookie that must be echoed in `X-CSRF-Token` on every
/// mutating request. URLSession stores and replays both cookies for us, so the
/// only manual work is lifting `csrf_token` out of the cookie jar.
///
/// Being native, we're not subject to CORS, so `ALLOWED_ORIGINS` on the server
/// doesn't need to be configured for this client.
final class APIClient: @unchecked Sendable {
    let baseURL: URL
    private let session: URLSession
    private let decoder = JSONDecoder()

    init(baseURL: URL, timeout: TimeInterval = 30) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.httpCookieStorage = HTTPCookieStorage.shared
        config.httpCookieAcceptPolicy = .always
        config.httpShouldSetCookies = true
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = 600   // uploads install deps inline
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
    }

    // MARK: - Cookies

    private var cookies: [HTTPCookie] {
        HTTPCookieStorage.shared.cookies(for: baseURL) ?? []
    }

    var csrfToken: String? {
        cookies.first { $0.name == "csrf_token" }?.value
    }

    var hasSessionCookie: Bool {
        cookies.contains { $0.name == "session" }
    }

    /// Drops this server's cookies so a stale session can't leak into the next login.
    func clearCookies() {
        for cookie in cookies {
            HTTPCookieStorage.shared.deleteCookie(cookie)
        }
    }

    /// The CSRF middleware only mints `csrf_token` on a request that arrives
    /// without one, so a plain GET has to happen before the first POST.
    @discardableResult
    func bootstrapCSRF() async throws -> AuthStatus {
        let status: AuthStatus = try await get("/api/auth/status")
        return status
    }

    private func ensureCSRF() async {
        guard csrfToken == nil else { return }
        _ = try? await bootstrapCSRF()
    }

    // MARK: - Request plumbing

    private func url(_ path: String, query: [URLQueryItem] = []) -> URL {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path.hasPrefix("/") ? String(path.dropFirst()) : path),
                                  resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        return comps.url!
    }

    private func makeRequest(_ method: String, _ path: String, query: [URLQueryItem]) -> URLRequest {
        var req = URLRequest(url: url(path, query: query))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if method != "GET" && method != "HEAD", let token = csrfToken {
            req.setValue(token, forHTTPHeaderField: "X-CSRF-Token")
        }
        return req
    }

    /// Runs the request and maps non-2xx responses onto `APIError`.
    private func perform(_ req: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw APIError.fromTransport(error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("Malformed response from the server.")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw Self.error(status: http.statusCode, data: data, headers: http.allHeaderFields)
        }
        return (data, http)
    }

    /// Extracts FastAPI's `{"detail": ...}` shape, which is a string for most
    /// errors but an array of field objects for 422 validation failures.
    static func detail(from data: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            let s = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            return (s?.isEmpty == false) ? s : nil
        }
        if let s = obj["detail"] as? String { return s }
        if let arr = obj["detail"] as? [[String: Any]] {
            let msgs = arr.compactMap { item -> String? in
                let msg = item["msg"] as? String
                if let loc = item["loc"] as? [Any], let field = loc.last {
                    return "\(field): \(msg ?? "invalid")"
                }
                return msg
            }
            if !msgs.isEmpty { return msgs.joined(separator: "\n") }
        }
        if let s = obj["message"] as? String { return s }
        return nil
    }

    private static func error(status: Int, data: Data, headers: [AnyHashable: Any]) -> APIError {
        let detail = Self.detail(from: data)
        switch status {
        case 400:
            return .badRequest(detail ?? "The server rejected that request.")
        case 401:
            return .unauthorized(detail ?? "Your session has expired. Sign in again.")
        case 403:
            if detail == "totp_required" { return .totpRequired }
            if let d = detail, d.localizedCaseInsensitiveContains("csrf") {
                return .forbidden("Security check failed (CSRF). Try signing in again.")
            }
            return .forbidden(detail ?? "You don't have permission to do that.")
        case 404:
            return .notFound(detail ?? "Not found.")
        case 409:
            return .conflict(detail ?? "That conflicts with something that already exists.")
        case 413:
            return .tooLarge(detail ?? "That file is too large (the limit is 50 MB).")
        case 429:
            let retry = (headers["X-Retry-After"] as? String).flatMap { Int($0) }
                ?? (headers["x-retry-after"] as? String).flatMap { Int($0) }
            return .rateLimited(message: detail ?? "Too many requests. Wait a moment and try again.",
                                retryAfter: retry)
        case 502:
            return .server(status: status, message: detail ?? "The server couldn't reach an upstream service.")
        default:
            return .server(status: status, message: detail ?? "Server error (\(status)).")
        }
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding("Couldn't read the server's response. It may be running a different Venvy version.")
        }
    }

    // MARK: - Verbs

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        let (data, _) = try await perform(makeRequest("GET", path, query: query))
        return try decode(data)
    }

    @discardableResult
    func post<T: Decodable>(_ path: String, body: Encodable? = nil, query: [URLQueryItem] = []) async throws -> T {
        try await send("POST", path, body: body, query: query)
    }

    @discardableResult
    func put<T: Decodable>(_ path: String, body: Encodable? = nil, query: [URLQueryItem] = []) async throws -> T {
        try await send("PUT", path, body: body, query: query)
    }

    @discardableResult
    func delete<T: Decodable>(_ path: String, body: Encodable? = nil, query: [URLQueryItem] = []) async throws -> T {
        try await send("DELETE", path, body: body, query: query)
    }

    /// Same as `post` but with a longer timeout, for endpoints that run a
    /// package manager inline (pip install is allowed 300s server-side).
    @discardableResult
    func postLongRunning<T: Decodable>(_ path: String, body: Encodable? = nil,
                                       timeout: TimeInterval) async throws -> T {
        await ensureCSRF()
        var req = makeRequest("POST", path, query: [])
        req.timeoutInterval = timeout
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        let (data, _) = try await perform(req)
        return try decode(data)
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: Encodable?, query: [URLQueryItem]) async throws -> T {
        await ensureCSRF()
        var req = makeRequest(method, path, query: query)
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(AnyEncodable(body))
        }
        let (data, _) = try await perform(req)
        return try decode(data)
    }

    // MARK: - Raw file transfer

    /// Downloads a file to a temporary URL, preserving the server's filename.
    func downloadFile(appID: Int, path: String) async throws -> URL {
        let req = makeRequest("GET", "/api/apps/\(appID)/files/download",
                              query: [URLQueryItem(name: "path", value: path)])
        let (data, http) = try await perform(req)
        var filename = (path as NSString).lastPathComponent
        if let disp = http.value(forHTTPHeaderField: "Content-Disposition"),
           let name = Self.filename(fromContentDisposition: disp) {
            filename = name
        }
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("venvy-downloads", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(filename.isEmpty ? "download" : filename)
        try? FileManager.default.removeItem(at: dest)
        try data.write(to: dest)
        return dest
    }

    static func filename(fromContentDisposition value: String) -> String? {
        for part in value.split(separator: ";") {
            let trimmed = part.trimmingCharacters(in: .whitespaces)
            if trimmed.lowercased().hasPrefix("filename=") {
                var name = String(trimmed.dropFirst("filename=".count))
                name = name.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
                if !name.isEmpty { return name }
            }
        }
        return nil
    }

    /// Fetches a file's contents as text for the editor.
    func fileContents(appID: Int, path: String) async throws -> String {
        let req = makeRequest("GET", "/api/apps/\(appID)/files/download",
                              query: [URLQueryItem(name: "path", value: path)])
        let (data, _) = try await perform(req)
        if let s = String(data: data, encoding: .utf8) { return s }
        if let s = String(data: data, encoding: .isoLatin1) { return s }
        throw APIError.decoding("This file isn't valid text, so it can't be edited here.")
    }

    // MARK: - Multipart upload

    struct UploadPart {
        var name: String        // form field name
        var filename: String?   // nil for a plain value
        var mimeType: String
        var data: Data

        static func value(_ name: String, _ value: String) -> UploadPart {
            UploadPart(name: name, filename: nil, mimeType: "text/plain",
                       data: Data(value.utf8))
        }

        static func file(_ name: String, filename: String, data: Data,
                         mimeType: String = "application/octet-stream") -> UploadPart {
            UploadPart(name: name, filename: filename, mimeType: mimeType, data: data)
        }
    }

    func upload<T: Decodable>(_ path: String, parts: [UploadPart], timeout: TimeInterval = 600) async throws -> T {
        await ensureCSRF()
        let boundary = "Boundary-\(UUID().uuidString)"
        var req = makeRequest("POST", path, query: [])
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = timeout

        var body = Data()
        for part in parts {
            body.append("--\(boundary)\r\n")
            if let filename = part.filename {
                body.append("Content-Disposition: form-data; name=\"\(part.name)\"; filename=\"\(filename)\"\r\n")
                body.append("Content-Type: \(part.mimeType)\r\n\r\n")
            } else {
                body.append("Content-Disposition: form-data; name=\"\(part.name)\"\r\n\r\n")
            }
            body.append(part.data)
            body.append("\r\n")
        }
        body.append("--\(boundary)--\r\n")
        req.httpBody = body

        let (data, _) = try await perform(req)
        return try decode(data)
    }

    // MARK: - WebSocket

    /// URL for the terminal PTY socket, carrying the same session cookie.
    func terminalURL(appID: Int) -> URL? {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        comps?.scheme = (baseURL.scheme == "https") ? "wss" : "ws"
        comps?.path = "/api/apps/\(appID)/terminal"
        return comps?.url
    }

    func makeWebSocketTask(appID: Int) -> URLSessionWebSocketTask? {
        guard let url = terminalURL(appID: appID) else { return nil }
        var req = URLRequest(url: url)
        // URLSession doesn't attach stored cookies to a WS handshake reliably,
        // so set the Cookie header explicitly from the jar.
        let jar = cookies.filter { $0.name == "session" || $0.name == "csrf_token" }
        if !jar.isEmpty {
            let header = jar.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
            req.setValue(header, forHTTPHeaderField: "Cookie")
        }
        return session.webSocketTask(with: req)
    }
}

// MARK: - Small helpers

struct EmptyResponse: Decodable { init() {} }

/// Lets a heterogeneous `Encodable` be encoded without generics leaking
/// into every call site.
struct AnyEncodable: Encodable {
    private let encodeFunc: (Encoder) throws -> Void
    init(_ wrapped: Encodable) {
        encodeFunc = { encoder in try wrapped.encode(to: encoder) }
    }
    func encode(to encoder: Encoder) throws { try encodeFunc(encoder) }
}

extension Data {
    mutating func append(_ string: String) {
        if let d = string.data(using: .utf8) { append(d) }
    }
}
