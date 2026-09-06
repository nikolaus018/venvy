import SwiftUI

struct Toast: Identifiable, Equatable {
    enum Kind {
        case info, success, error, warning

        var icon: String {
            switch self {
            case .info: return "info.circle.fill"
            case .success: return "checkmark.circle.fill"
            case .error: return "exclamationmark.triangle.fill"
            case .warning: return "exclamationmark.circle.fill"
            }
        }

        var tint: Color {
            switch self {
            case .info: return .accentColor
            case .success: return VenvyColor.success
            case .error: return VenvyColor.danger
            case .warning: return VenvyColor.warning
            }
        }
    }

    let id = UUID()
    var message: String
    var kind: Kind
    var duration: TimeInterval
}

@MainActor
final class ToastCenter: ObservableObject {
    @Published private(set) var toasts: [Toast] = []

    func show(_ message: String, kind: Toast.Kind = .info, duration: TimeInterval? = nil) {
        // Errors linger longer — they usually need reading.
        let seconds = duration ?? (kind == .error ? 5 : 2.8)
        let toast = Toast(message: message, kind: kind, duration: seconds)
        toasts.append(toast)
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            self?.dismiss(toast.id)
        }
    }

    func info(_ m: String) { show(m, kind: .info) }
    func success(_ m: String) { show(m, kind: .success) }
    func warning(_ m: String) { show(m, kind: .warning) }
    func error(_ m: String) { show(m, kind: .error) }

    func dismiss(_ id: UUID) {
        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
            toasts.removeAll { $0.id == id }
        }
    }
}

struct ToastOverlay: View {
    @ObservedObject var center: ToastCenter

    var body: some View {
        GlassGroup(spacing: 10) {
            VStack(spacing: 8) {
            Spacer(minLength: 0)
            ForEach(center.toasts) { toast in
                HStack(spacing: 10) {
                    Image(systemName: toast.kind.icon)
                        .foregroundStyle(toast.kind.tint)
                        .font(.system(size: 15, weight: .semibold))
                    Text(toast.message)
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .venvyGlass(in: RoundedRectangle(cornerRadius: 14, style: .continuous),
                            tint: toast.kind.tint.opacity(0.18),
                            interactive: true)
                .shadow(color: .black.opacity(0.12), radius: 12, y: 5)
                .onTapGesture { center.dismiss(toast.id) }
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            }
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 12)
        .animation(.spring(response: 0.34, dampingFraction: 0.85), value: center.toasts)
        .allowsHitTesting(!center.toasts.isEmpty)
    }
}
