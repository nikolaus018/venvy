import SwiftUI

/// Liquid Glass adoption layer.
///
/// Building against the iOS 26 SDK already gives system chrome — navigation
/// bars, the tab bar, toolbars and sheets — the Liquid Glass treatment for
/// free. These helpers extend the same material to *our* floating surfaces
/// (toasts, overlays, control clusters), with a `.regularMaterial` fallback so
/// the app still looks right on iOS 18–25.
extension View {

    /// Floating glass surface. Falls back to a material + hairline border.
    @ViewBuilder
    func venvyGlass<S: Shape>(in shape: S, interactive: Bool = false) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(interactive ? .regular.interactive() : .regular, in: shape)
        } else {
            self
                .background(.regularMaterial, in: shape)
                .overlay(shape.stroke(Color.primary.opacity(0.08), lineWidth: 1))
        }
    }

    /// Tinted glass, for surfaces that carry a status colour (toasts).
    @ViewBuilder
    func venvyGlass<S: Shape>(in shape: S, tint: Color, interactive: Bool = false) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(
                (interactive ? Glass.regular.interactive() : Glass.regular).tint(tint),
                in: shape
            )
        } else {
            self
                .background(.regularMaterial, in: shape)
                .overlay(shape.stroke(tint.opacity(0.28), lineWidth: 1))
        }
    }
}

/// Groups sibling glass elements so they blend and morph together instead of
/// each rendering its own isolated pane. No-op before iOS 26.
struct GlassGroup<Content: View>: View {
    var spacing: CGFloat = 8
    @ViewBuilder var content: Content

    var body: some View {
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: spacing) { content }
        } else {
            content
        }
    }
}

/// Prominent action button: Liquid Glass on iOS 26, bordered-prominent before.
///
/// `GlassButtonStyle` is a `PrimitiveButtonStyle`, so this is a view modifier
/// rather than a wrapping `ButtonStyle`.
extension View {
    @ViewBuilder
    func venvyProminentButton() -> some View {
        if #available(iOS 26.0, *) {
            self.buttonStyle(.glassProminent)
        } else {
            self.buttonStyle(.borderedProminent)
        }
    }

    @ViewBuilder
    func venvyGlassButton() -> some View {
        if #available(iOS 26.0, *) {
            self.buttonStyle(.glass)
        } else {
            self.buttonStyle(.bordered)
        }
    }
}
