import SwiftUI
import UIKit

extension Notification.Name {
    /// Posted with a `String` payload to insert text at the editor's caret.
    static let venvyCodeEditorInsert = Notification.Name("venvy.codeEditor.insert")
}

/// A syntax-highlighting code editor.
///
/// SwiftUI's `TextEditor` renders plain text only on this deployment target,
/// so this wraps `UITextView` and colours the text storage in place. It's
/// built on TextKit 1 explicitly, because the line-number gutter needs
/// `NSLayoutManager` geometry that TextKit 2 doesn't expose the same way.
struct CodeEditor: UIViewRepresentable {
    @Binding var text: String
    var language: CodeLanguage
    var isEditable: Bool
    var showsLineNumbers: Bool
    var wrapLines: Bool
    var fontSize: CGFloat

    func makeUIView(context: Context) -> LineNumberTextView {
        // Build the TextKit 1 stack by hand; the plain UITextView initialiser
        // opts into TextKit 2, where `layoutManager` silently falls back.
        let storage = NSTextStorage()
        let layoutManager = NSLayoutManager()
        let container = NSTextContainer(size: CGSize(width: 0, height: CGFloat.greatestFiniteMagnitude))
        container.widthTracksTextView = true
        layoutManager.addTextContainer(container)
        storage.addLayoutManager(layoutManager)

        let view = LineNumberTextView(frame: .zero, textContainer: container)
        view.delegate = context.coordinator
        view.backgroundColor = .systemBackground
        view.autocorrectionType = .no
        view.autocapitalizationType = .none
        view.smartQuotesType = .no
        view.smartDashesType = .no
        view.smartInsertDeleteType = .no
        view.spellCheckingType = .no
        view.keyboardType = .asciiCapable
        view.alwaysBounceVertical = true
        view.textContainerInset = UIEdgeInsets(top: 10, left: 4, bottom: 24, right: 8)
        view.showsLineNumbers = showsLineNumbers
        view.gutterFont = Self.font(size: fontSize)

        context.coordinator.textView = view
        view.text = text
        context.coordinator.highlight(force: true)
        context.coordinator.observeInsertions()
        return view
    }

    func updateUIView(_ view: LineNumberTextView, context: Context) {
        context.coordinator.parent = self

        // Only overwrite the view's text when the model genuinely diverges —
        // assigning during typing would reset the caret.
        if view.text != text {
            let selected = view.selectedRange
            view.text = text
            view.selectedRange = NSRange(location: min(selected.location, (text as NSString).length),
                                         length: 0)
            context.coordinator.highlight(force: true)
        }

        view.isEditable = isEditable
        view.showsLineNumbers = showsLineNumbers

        let font = Self.font(size: fontSize)
        if view.gutterFont != font {
            view.gutterFont = font
            context.coordinator.highlight(force: true)
        }

        // Wrapping: track the view's width, or let the container run wide and
        // scroll horizontally instead.
        let container = view.textContainer
        if wrapLines {
            container.widthTracksTextView = true
            container.size = CGSize(width: max(view.bounds.width - view.gutterWidth, 1),
                                    height: CGFloat.greatestFiniteMagnitude)
        } else {
            container.widthTracksTextView = false
            container.size = CGSize(width: 10_000, height: CGFloat.greatestFiniteMagnitude)
        }
        view.setNeedsDisplay()
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    static func font(size: CGFloat) -> UIFont {
        UIFont.monospacedSystemFont(ofSize: size, weight: .regular)
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: CodeEditor
        weak var textView: LineNumberTextView?
        private var highlightTask: Task<Void, Never>?
        private var highlighter: SyntaxHighlighter

        private var insertObserver: NSObjectProtocol?

        init(_ parent: CodeEditor) {
            self.parent = parent
            self.highlighter = SyntaxHighlighter(language: parent.language)
        }

        deinit {
            if let insertObserver {
                NotificationCenter.default.removeObserver(insertObserver)
            }
        }

        /// Lets the keyboard accessory bar insert punctuation at the caret,
        /// which only the text view knows the position of.
        func observeInsertions() {
            insertObserver = NotificationCenter.default.addObserver(
                forName: .venvyCodeEditorInsert, object: nil, queue: .main
            ) { [weak self] note in
                MainActor.assumeIsolated {
                    guard let self, let textView = self.textView,
                          textView.isFirstResponder,
                          let snippet = note.object as? String else { return }
                    textView.insertText(snippet)
                    self.parent.text = textView.text
                    self.highlight()
                }
            }
        }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text
            (textView as? LineNumberTextView)?.setNeedsDisplay()
            highlight()
        }

        /// Re-colours on a short delay so a burst of keystrokes triggers one
        /// pass rather than one per character.
        func highlight(force: Bool = false) {
            highlightTask?.cancel()
            guard !force else {
                applyHighlight()
                return
            }
            highlightTask = Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 140_000_000)
                guard !Task.isCancelled else { return }
                self?.applyHighlight()
            }
        }

        @MainActor
        private func applyHighlight() {
            guard let textView else { return }
            if highlighter.language != parent.language {
                highlighter = SyntaxHighlighter(language: parent.language)
            }
            highlighter.apply(to: textView.textStorage,
                              font: CodeEditor.font(size: parent.fontSize))
            textView.setNeedsDisplay()
        }
    }
}

/// `UITextView` with a line-number gutter drawn in the left inset.
final class LineNumberTextView: UITextView {
    var showsLineNumbers = true {
        didSet {
            guard oldValue != showsLineNumbers else { return }
            updateInset()
            setNeedsDisplay()
        }
    }

    var gutterFont: UIFont = .monospacedSystemFont(ofSize: 11.5, weight: .regular) {
        didSet {
            updateInset()
            setNeedsDisplay()
        }
    }

    /// Width reserved for the gutter, sized to the widest line number.
    var gutterWidth: CGFloat {
        guard showsLineNumbers else { return 0 }
        let digits = max(2, String(max(lineCount, 1)).count)
        let sample = String(repeating: "8", count: digits)
        let width = (sample as NSString).size(withAttributes: [.font: gutterFont]).width
        return width + 16
    }

    private var lineCount: Int {
        guard !text.isEmpty else { return 1 }
        return text.reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
    }

    private func updateInset() {
        textContainerInset.left = gutterWidth + 4
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        updateInset()
    }

    override func draw(_ rect: CGRect) {
        super.draw(rect)
        guard showsLineNumbers, let context = UIGraphicsGetCurrentContext() else { return }

        // A faint rule separating the gutter from the code.
        context.setStrokeColor(UIColor.separator.withAlphaComponent(0.35).cgColor)
        context.setLineWidth(1 / (window?.screen.scale ?? 2))
        context.move(to: CGPoint(x: gutterWidth + 2, y: rect.minY))
        context.addLine(to: CGPoint(x: gutterWidth + 2, y: rect.maxY))
        context.strokePath()

        let nsText = text as NSString
        let attributes: [NSAttributedString.Key: Any] = [
            .font: gutterFont,
            .foregroundColor: SyntaxTheme.gutter,
        ]

        var lineNumber = 1
        var charIndex = 0

        // Walk paragraph by paragraph and draw each number beside the first
        // line fragment of that paragraph, so wrapped continuation lines stay
        // unnumbered — the way a code editor behaves.
        while charIndex < nsText.length {
            let paragraph = nsText.paragraphRange(for: NSRange(location: charIndex, length: 0))
            let glyphRange = layoutManager.glyphRange(forCharacterRange: paragraph,
                                                      actualCharacterRange: nil)
            if glyphRange.length > 0 || paragraph.length > 0 {
                var fragment = layoutManager.lineFragmentRect(forGlyphAt: glyphRange.location,
                                                              effectiveRange: nil)
                fragment.origin.y += textContainerInset.top
                if fragment.maxY >= rect.minY && fragment.minY <= rect.maxY {
                    draw(number: lineNumber, in: fragment, attributes: attributes)
                }
            }
            lineNumber += 1
            charIndex = NSMaxRange(paragraph)
            if paragraph.length == 0 { break }
        }

        // A trailing newline opens one more (empty) line that the loop above
        // can't see, since it has no characters of its own.
        if nsText.length == 0 || nsText.hasSuffix("\n") {
            let glyphIndex = max(layoutManager.numberOfGlyphs - 1, 0)
            var fragment = layoutManager.numberOfGlyphs > 0
                ? layoutManager.lineFragmentRect(forGlyphAt: glyphIndex, effectiveRange: nil)
                : CGRect(x: 0, y: 0, width: bounds.width, height: gutterFont.lineHeight)
            if nsText.length > 0 { fragment.origin.y += fragment.height }
            fragment.origin.y += textContainerInset.top
            if fragment.maxY >= rect.minY && fragment.minY <= rect.maxY {
                draw(number: lineNumber, in: fragment, attributes: attributes)
            }
        }
    }

    private func draw(number: Int, in fragment: CGRect,
                      attributes: [NSAttributedString.Key: Any]) {
        let label = String(number) as NSString
        let size = label.size(withAttributes: attributes)
        // Right-align against the gutter edge, baseline-matched to the line.
        let point = CGPoint(x: gutterWidth - size.width - 4,
                            y: fragment.minY + (fragment.height - size.height) / 2)
        label.draw(at: point, withAttributes: attributes)
    }
}
