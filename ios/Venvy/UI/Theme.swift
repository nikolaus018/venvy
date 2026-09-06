import SwiftUI

/// Palette carried over from the web frontend so both clients read as one product.
enum VenvyColor {
    static let accent = Color.accentColor
    static let success = Color(red: 0.13, green: 0.77, blue: 0.37)   // #22c55e
    static let danger  = Color(red: 0.94, green: 0.27, blue: 0.27)   // #ef4444
    static let warning = Color(red: 0.96, green: 0.62, blue: 0.04)   // #f59e0b
    static let muted   = Color.secondary

    static func status(_ status: AppStatus) -> Color {
        switch status {
        case .running: return success
        case .stopped: return .secondary
        case .starting, .stopping: return warning
        case .error: return danger
        }
    }
}

extension AppStatus {
    var label: String {
        switch self {
        case .stopped: return "Stopped"
        case .starting: return "Starting"
        case .running: return "Running"
        case .stopping: return "Stopping"
        case .error: return "Error"
        }
    }

    var icon: String {
        switch self {
        case .running: return "play.circle.fill"
        case .stopped: return "stop.circle.fill"
        case .starting, .stopping: return "clock.fill"
        case .error: return "exclamationmark.triangle.fill"
        }
    }
}

extension Runtime {
    var icon: String { self == .python ? "chevron.left.forwardslash.chevron.right" : "gearshape.2.fill" }
}

// MARK: - Formatting

enum Fmt {
    static func bytes(_ value: Int) -> String {
        guard value > 0 else { return "0 B" }
        let units = ["B", "KB", "MB", "GB", "TB"]
        var v = Double(value)
        var i = 0
        while v >= 1024 && i < units.count - 1 { v /= 1024; i += 1 }
        return String(format: v >= 100 || i == 0 ? "%.0f %@" : "%.1f %@", v, units[i])
    }

    static func megabytes(_ value: Double) -> String {
        guard value > 0 else { return "0 MB" }
        if value >= 1024 { return String(format: "%.2f GB", value / 1024) }
        return String(format: value >= 100 ? "%.0f MB" : "%.1f MB", value)
    }

    static func percent(_ value: Double) -> String {
        String(format: value >= 10 ? "%.0f%%" : "%.1f%%", value)
    }

    /// Compact uptime: 4d 3h, 3h 12m, 12m 5s, 43s.
    static func uptime(_ seconds: Double) -> String {
        guard seconds >= 1 else { return "—" }
        let s = Int(seconds)
        let d = s / 86400, h = (s % 86400) / 3600, m = (s % 3600) / 60, sec = s % 60
        if d > 0 { return "\(d)d \(h)h" }
        if h > 0 { return "\(h)h \(m)m" }
        if m > 0 { return "\(m)m \(sec)s" }
        return "\(sec)s"
    }

    static func relative(_ date: Date?) -> String {
        guard let date else { return "—" }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f.localizedString(for: date, relativeTo: Date())
    }
}

// MARK: - Shared building blocks

struct StatusBadge: View {
    let status: AppStatus
    var compact = false

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(VenvyColor.status(status))
                .frame(width: 7, height: 7)
            Text(status.label)
                .font(compact ? .caption2.weight(.semibold) : .caption.weight(.semibold))
                .foregroundStyle(VenvyColor.status(status))
        }
        .padding(.horizontal, compact ? 7 : 9)
        .padding(.vertical, compact ? 3 : 4)
        .background(VenvyColor.status(status).opacity(0.12),
                    in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Status: \(status.label)")
    }
}

struct StatTile: View {
    let title: String
    let value: String
    var systemImage: String?
    var tint: Color = .accentColor

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                if let systemImage {
                    Image(systemName: systemImage)
                        .font(.caption2)
                        .foregroundStyle(tint)
                }
                Text(title)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)
            }
            Text(value)
                .font(.system(.title3, design: .rounded).weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color(.secondarySystemGroupedBackground),
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title): \(value)")
    }
}

/// Full-bleed placeholder for empty lists and error states.
struct EmptyStateView: View {
    let icon: String
    let title: String
    var message: String?
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 38, weight: .light))
                .foregroundStyle(.tertiary)
            Text(title)
                .font(.headline)
                .multilineTextAlignment(.center)
            if let message {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(.borderedProminent)
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: 340)
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A labelled row of monospaced detail, used across the dashboard and settings.
struct DetailRow: View {
    let label: String
    let value: String
    var monospaced = false
    var copyable = false

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(monospaced ? .system(.subheadline, design: .monospaced) : .subheadline)
                .foregroundStyle(.primary)
                .multilineTextAlignment(.trailing)
                .textSelection(.enabled)
        }
        .contextMenu {
            if copyable {
                Button {
                    UIPasteboard.general.string = value
                } label: {
                    Label("Copy", systemImage: "doc.on.doc")
                }
            }
        }
    }
}

/// Inline error banner with an optional retry.
struct ErrorBanner: View {
    let message: String
    var retry: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(VenvyColor.danger)
            VStack(alignment: .leading, spacing: 6) {
                Text(message)
                    .font(.subheadline)
                    .fixedSize(horizontal: false, vertical: true)
                if let retry {
                    Button("Try again", action: retry)
                        .font(.subheadline.weight(.semibold))
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(VenvyColor.danger.opacity(0.1),
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

// MARK: - ANSI rendering

extension ANSIParser.Span {
    /// Renders one styled span. `defaultColor` is used when the span carries
    /// no explicit SGR foreground, letting callers fall back to their own
    /// tinting (e.g. the logs view's severity colouring).
    func text(defaultColor: Color = .primary) -> Text {
        var t = Text(self.text.isEmpty ? " " : self.text)
        let style = self.style
        if style.inverse {
            t = t.foregroundColor(style.background ?? Color.accentColor)
        } else if let fg = style.foreground {
            t = t.foregroundColor(fg)
        } else if style.dim {
            t = t.foregroundColor(.secondary)
        } else {
            t = t.foregroundColor(defaultColor)
        }
        if style.bold { t = t.bold() }
        if style.italic { t = t.italic() }
        if style.underline { t = t.underline() }
        return t
    }
}
