import Foundation

enum APIError: LocalizedError, Equatable {
    /// Server replied 401 — the session cookie is missing, invalid or expired.
    case unauthorized(String)
    /// Server replied 403 with the sentinel detail "totp_required".
    case totpRequired
    /// Server replied 403 for a role, CSRF or IP-allowlist reason.
    case forbidden(String)
    case notFound(String)
    case conflict(String)
    case tooLarge(String)
    case rateLimited(message: String, retryAfter: Int?)
    case badRequest(String)
    case server(status: Int, message: String)
    /// URLSession-level failure: host unreachable, TLS, timeout, no route.
    case transport(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .unauthorized(let m): return m
        case .totpRequired: return "A two-factor code is required."
        case .forbidden(let m): return m
        case .notFound(let m): return m
        case .conflict(let m): return m
        case .tooLarge(let m): return m
        case .rateLimited(let m, _): return m
        case .badRequest(let m): return m
        case .server(_, let m): return m
        case .transport(let m): return m
        case .decoding(let m): return m
        }
    }

    /// True when the right response is to bounce the user back to the login screen.
    var isAuthFailure: Bool {
        if case .unauthorized = self { return true }
        return false
    }

    /// True when the failure is about reaching the server at all, rather than
    /// something the server said — worth showing the "check the address" hint.
    var isConnectivity: Bool {
        if case .transport = self { return true }
        return false
    }

    /// Builds the friendliest message we can for a URLSession error.
    static func fromTransport(_ error: Error) -> APIError {
        let ns = error as NSError
        guard ns.domain == NSURLErrorDomain else { return .transport(error.localizedDescription) }
        switch ns.code {
        case NSURLErrorCannotFindHost:
            return .transport("Can't find that host. Check the address.")
        case NSURLErrorCannotConnectToHost:
            return .transport("The server refused the connection. Is Venvy running, and is the port right?")
        case NSURLErrorTimedOut:
            return .transport("The server didn't respond in time.")
        case NSURLErrorNotConnectedToInternet:
            return .transport("This device is offline.")
        case NSURLErrorNetworkConnectionLost:
            return .transport("The connection was lost.")
        case NSURLErrorSecureConnectionFailed, NSURLErrorServerCertificateUntrusted,
             NSURLErrorServerCertificateHasBadDate, NSURLErrorServerCertificateNotYetValid,
             NSURLErrorServerCertificateHasUnknownRoot:
            return .transport("The server's TLS certificate wasn't accepted. If it's self-signed, use http:// on your LAN instead.")
        case NSURLErrorAppTransportSecurityRequiresSecureConnection:
            return .transport("iOS blocked a plain-HTTP connection to this host.")
        default:
            return .transport(ns.localizedDescription)
        }
    }
}
