import UIKit

// MARK: - Languages

enum CodeLanguage: String {
    case python, rust, javascript, json, toml, yaml, shell, markdown, html, css, plain

    static func detect(filename: String) -> CodeLanguage {
        switch (filename as NSString).pathExtension.lowercased() {
        case "py", "pyw", "pyi": return .python
        case "rs": return .rust
        case "js", "mjs", "cjs", "jsx", "ts", "tsx": return .javascript
        case "json", "jsonc": return .json
        case "toml": return .toml
        case "yaml", "yml": return .yaml
        case "sh", "bash", "zsh", "env": return .shell
        case "md", "markdown": return .markdown
        case "html", "htm": return .html
        case "css", "scss": return .css
        default:
            // Extensionless files that still have a well-known shape.
            let name = (filename as NSString).lastPathComponent.lowercased()
            if name == "dockerfile" || name.hasPrefix(".env") { return .shell }
            if name == "cargo.lock" { return .toml }
            if name == "requirements.txt" { return .shell }
            return .plain
        }
    }

    var displayName: String {
        switch self {
        case .python: return "Python"
        case .rust: return "Rust"
        case .javascript: return "JavaScript"
        case .json: return "JSON"
        case .toml: return "TOML"
        case .yaml: return "YAML"
        case .shell: return "Shell"
        case .markdown: return "Markdown"
        case .html: return "HTML"
        case .css: return "CSS"
        case .plain: return "Plain text"
        }
    }
}

// MARK: - Theme

/// Token colours modelled on VS Code's Dark+ / Light+ themes. Every colour is
/// a dynamic `UIColor`, so the editor follows the system appearance without
/// re-highlighting.
enum SyntaxTheme {
    static let plain      = dynamic(light: 0x1F1F1F, dark: 0xD4D4D4)
    static let comment    = dynamic(light: 0x008000, dark: 0x6A9955)
    static let string     = dynamic(light: 0xA31515, dark: 0xCE9178)
    static let number     = dynamic(light: 0x098658, dark: 0xB5CEA8)
    static let keyword    = dynamic(light: 0x0000FF, dark: 0x569CD6)
    static let control    = dynamic(light: 0xAF00DB, dark: 0xC586C0)
    static let type       = dynamic(light: 0x267F99, dark: 0x4EC9B0)
    static let function   = dynamic(light: 0x795E26, dark: 0xDCDCAA)
    static let property   = dynamic(light: 0x0451A5, dark: 0x9CDCFE)
    static let constant   = dynamic(light: 0x0000FF, dark: 0x569CD6)
    static let punctuation = dynamic(light: 0x3B3B3B, dark: 0x808080)
    static let heading    = dynamic(light: 0x800000, dark: 0x569CD6)

    static let gutter     = dynamic(light: 0x9A9A9A, dark: 0x6E7681)
    static let background = UIColor.systemBackground

    private static func dynamic(light: UInt32, dark: UInt32) -> UIColor {
        UIColor { traits in
            traits.userInterfaceStyle == .dark ? color(dark) : color(light)
        }
    }

    private static func color(_ hex: UInt32) -> UIColor {
        UIColor(red: CGFloat((hex >> 16) & 0xFF) / 255,
                green: CGFloat((hex >> 8) & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255,
                alpha: 1)
    }
}

// MARK: - Highlighter

/// Regex-based syntax highlighter.
///
/// Deliberately not a parser: it applies an ordered list of patterns and lets
/// the first match win for any given character, so comments and strings mask
/// keywords inside them. That's enough for readable colouring and stays fast
/// on a phone, but it won't understand nesting or context the way a real
/// grammar would.
struct SyntaxHighlighter {

    /// Files past this size are left unhighlighted — a full-document regex
    /// pass on every keystroke stops being affordable well before this.
    static let maximumLength = 120_000

    struct Rule {
        let regex: NSRegularExpression
        let color: UIColor
        /// When set, colour this capture group instead of the whole match, so
        /// a pattern can use context it shouldn't paint (e.g. `foo` in `foo(`).
        let captureGroup: Int

        init?(_ pattern: String, _ color: UIColor, group: Int = 0,
              options: NSRegularExpression.Options = []) {
            guard let regex = try? NSRegularExpression(pattern: pattern, options: options) else {
                assertionFailure("Bad syntax pattern: \(pattern)")
                return nil
            }
            self.regex = regex
            self.color = color
            self.captureGroup = group
        }
    }

    let language: CodeLanguage
    private let rules: [Rule]

    init(language: CodeLanguage) {
        self.language = language
        self.rules = Self.rules(for: language)
    }

    /// Applies colours to `storage` in place, preserving the caret and any
    /// selection — replacing `attributedText` wholesale would reset both.
    func apply(to storage: NSTextStorage, font: UIFont) {
        let text = storage.string
        let full = NSRange(location: 0, length: (text as NSString).length)

        storage.beginEditing()
        storage.setAttributes([.font: font, .foregroundColor: SyntaxTheme.plain], range: full)

        if !rules.isEmpty && (text as NSString).length <= Self.maximumLength {
            // Tracks which characters already have a colour, so an earlier
            // (higher-priority) rule wins over later ones.
            var claimed = [Bool](repeating: false, count: full.length)

            for rule in rules {
                rule.regex.enumerateMatches(in: text, range: full) { match, _, _ in
                    guard let match else { return }
                    let range = rule.captureGroup < match.numberOfRanges
                        ? match.range(at: rule.captureGroup)
                        : match.range
                    guard range.location != NSNotFound, range.length > 0,
                          range.location + range.length <= full.length else { return }

                    for i in range.location..<(range.location + range.length) where claimed[i] {
                        return   // overlaps something already coloured
                    }
                    for i in range.location..<(range.location + range.length) {
                        claimed[i] = true
                    }
                    storage.addAttribute(.foregroundColor, value: rule.color, range: range)
                }
            }
        }
        storage.endEditing()
    }

    // MARK: - Patterns

    // Order matters: comments and strings come first so they mask everything
    // that might look like a keyword or number inside them.
    private static func rules(for language: CodeLanguage) -> [Rule] {
        switch language {
        case .python:   return python
        case .rust:     return rust
        case .javascript: return javascript
        case .json:     return json
        case .toml:     return toml
        case .yaml:     return yaml
        case .shell:    return shell
        case .markdown: return markdown
        case .html:     return html
        case .css:      return css
        case .plain:    return []
        }
    }

    private static let python: [Rule] = [
        Rule(#"("""[\s\S]*?"""|'''[\s\S]*?''')"#, SyntaxTheme.string),
        Rule(#"#[^\n]*"#, SyntaxTheme.comment),
        Rule(#"([rbfu]{0,2})("([^"\\\n]|\\.)*"|'([^'\\\n]|\\.)*')"#, SyntaxTheme.string),
        Rule(#"@[A-Za-z_][A-Za-z0-9_.]*"#, SyntaxTheme.function),
        Rule(#"\b(def|class|return|yield|lambda|import|from|as|pass|raise|global|nonlocal|assert|del|async|await)\b"#, SyntaxTheme.keyword),
        Rule(#"\b(if|elif|else|for|while|break|continue|try|except|finally|with|in|is|not|and|or|match|case)\b"#, SyntaxTheme.control),
        Rule(#"\b(None|True|False|self|cls|__name__|__main__)\b"#, SyntaxTheme.constant),
        Rule(#"\b(int|str|float|bool|bytes|list|dict|set|tuple|object|type|Exception|BaseException|Any|Optional|List|Dict)\b"#, SyntaxTheme.type),
        Rule(#"\b(print|len|range|open|super|isinstance|enumerate|zip|map|filter|sorted|sum|min|max|abs|round|input|format|repr)\b"#, SyntaxTheme.function),
        Rule(#"\b(0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?\d*([eE][+-]?\d+)?)\b"#, SyntaxTheme.number),
        Rule(#"\b([A-Za-z_][A-Za-z0-9_]*)\s*\("#, SyntaxTheme.function, group: 1),
    ].compactMap { $0 }

    private static let rust: [Rule] = [
        Rule(#"//[^\n]*|/\*[\s\S]*?\*/"#, SyntaxTheme.comment),
        Rule(##"("([^"\\\n]|\\.)*"|r#"[\s\S]*?"#)"##, SyntaxTheme.string),
        Rule(#"'(\\.|[^'\\])'"#, SyntaxTheme.string),
        Rule(##"#!?\[[^\]]*\]"##, SyntaxTheme.function),
        Rule(#"\b(fn|let|mut|const|static|struct|enum|union|impl|trait|type|mod|use|pub|crate|extern|unsafe|where|dyn|ref|move|async|await)\b"#, SyntaxTheme.keyword),
        Rule(#"\b(if|else|match|loop|while|for|in|break|continue|return|yield)\b"#, SyntaxTheme.control),
        Rule(#"\b(true|false|None|Some|Ok|Err|self|Self|super)\b"#, SyntaxTheme.constant),
        Rule(#"\b(i8|i16|i32|i64|i128|u8|u16|u32|u64|u128|usize|isize|f32|f64|bool|char|str|String|Vec|Option|Result|Box|Rc|Arc|HashMap|HashSet)\b"#, SyntaxTheme.type),
        Rule(#"\b[a-z_][a-z0-9_]*!"#, SyntaxTheme.function),
        Rule(#"\b(0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?\d*([eE][+-]?\d+)?)(_?[iuf](8|16|32|64|128|size))?\b"#, SyntaxTheme.number),
        Rule(#"\b([A-Za-z_][A-Za-z0-9_]*)\s*\("#, SyntaxTheme.function, group: 1),
        Rule(#"\b[A-Z][A-Za-z0-9_]*\b"#, SyntaxTheme.type),
    ].compactMap { $0 }

    private static let javascript: [Rule] = [
        Rule(#"//[^\n]*|/\*[\s\S]*?\*/"#, SyntaxTheme.comment),
        Rule(#"(`[\s\S]*?`|"([^"\\\n]|\\.)*"|'([^'\\\n]|\\.)*')"#, SyntaxTheme.string),
        Rule(#"\b(function|const|let|var|class|extends|new|return|import|export|from|default|async|await|yield|typeof|instanceof|delete|void)\b"#, SyntaxTheme.keyword),
        Rule(#"\b(if|else|for|while|do|switch|case|break|continue|try|catch|finally|throw)\b"#, SyntaxTheme.control),
        Rule(#"\b(true|false|null|undefined|this|NaN|Infinity)\b"#, SyntaxTheme.constant),
        Rule(#"\b(Object|Array|String|Number|Boolean|Promise|Map|Set|JSON|Math|Date|Error|console|document|window)\b"#, SyntaxTheme.type),
        Rule(#"\b(0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*\.?\d*([eE][+-]?\d+)?)\b"#, SyntaxTheme.number),
        Rule(#"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\("#, SyntaxTheme.function, group: 1),
    ].compactMap { $0 }

    private static let json: [Rule] = [
        Rule(#""([^"\\]|\\.)*"\s*:"#, SyntaxTheme.property),
        Rule(#""([^"\\]|\\.)*""#, SyntaxTheme.string),
        Rule(#"\b(true|false|null)\b"#, SyntaxTheme.constant),
        Rule(#"-?\b\d+\.?\d*([eE][+-]?\d+)?\b"#, SyntaxTheme.number),
    ].compactMap { $0 }

    private static let toml: [Rule] = [
        Rule(#"#[^\n]*"#, SyntaxTheme.comment),
        Rule(#"^\s*\[\[?[^\]\n]+\]\]?"#, SyntaxTheme.type, options: [.anchorsMatchLines]),
        Rule(#"("""[\s\S]*?"""|"([^"\\\n]|\\.)*"|'([^'\n]*)')"#, SyntaxTheme.string),
        Rule(#"^\s*([A-Za-z0-9_.\-"]+)\s*="#, SyntaxTheme.property, group: 1, options: [.anchorsMatchLines]),
        Rule(#"\b(true|false)\b"#, SyntaxTheme.constant),
        Rule(#"\b\d[\d_]*\.?\d*\b"#, SyntaxTheme.number),
    ].compactMap { $0 }

    private static let yaml: [Rule] = [
        Rule(#"#[^\n]*"#, SyntaxTheme.comment),
        Rule(#"^\s*(-\s*)?([A-Za-z0-9_.\-]+)\s*:"#, SyntaxTheme.property, group: 2, options: [.anchorsMatchLines]),
        Rule(#"("([^"\\\n]|\\.)*"|'([^'\n]*)')"#, SyntaxTheme.string),
        Rule(#"\b(true|false|null|yes|no|on|off|~)\b"#, SyntaxTheme.constant),
        Rule(#"\b\d+\.?\d*\b"#, SyntaxTheme.number),
    ].compactMap { $0 }

    private static let shell: [Rule] = [
        Rule(#"#[^\n]*"#, SyntaxTheme.comment),
        Rule(#"("([^"\\]|\\.)*"|'[^']*')"#, SyntaxTheme.string),
        Rule(#"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?]"#, SyntaxTheme.property),
        Rule(#"\b(if|then|elif|else|fi|for|while|until|do|done|case|esac|function|return|break|continue|in|select)\b"#, SyntaxTheme.control),
        Rule(#"\b(echo|export|source|cd|set|unset|local|readonly|declare|eval|exec|exit|printf|read|shift|trap|alias)\b"#, SyntaxTheme.keyword),
        Rule(#"\b\d+\b"#, SyntaxTheme.number),
    ].compactMap { $0 }

    private static let markdown: [Rule] = [
        Rule(#"^#{1,6}\s+[^\n]*"#, SyntaxTheme.heading, options: [.anchorsMatchLines]),
        Rule(#"```[\s\S]*?```|`[^`\n]+`"#, SyntaxTheme.string),
        Rule(#"\[[^\]]*\]\([^)]*\)"#, SyntaxTheme.property),
        Rule(#"(\*\*|__)[^\n]*?\1"#, SyntaxTheme.keyword),
        Rule(#"^\s*([-*+]|\d+\.)\s"#, SyntaxTheme.punctuation, options: [.anchorsMatchLines]),
        Rule(#"^>\s[^\n]*"#, SyntaxTheme.comment, options: [.anchorsMatchLines]),
    ].compactMap { $0 }

    private static let html: [Rule] = [
        Rule(#"<!--[\s\S]*?-->"#, SyntaxTheme.comment),
        Rule(#"("([^"\\]|\\.)*"|'[^']*')"#, SyntaxTheme.string),
        Rule(#"</?([A-Za-z][A-Za-z0-9-]*)"#, SyntaxTheme.type, group: 1),
        Rule(#"\b([A-Za-z-]+)="#, SyntaxTheme.property, group: 1),
    ].compactMap { $0 }

    private static let css: [Rule] = [
        Rule(#"/\*[\s\S]*?\*/"#, SyntaxTheme.comment),
        Rule(#"("([^"\\]|\\.)*"|'[^']*')"#, SyntaxTheme.string),
        Rule(#"([A-Za-z-]+)\s*:"#, SyntaxTheme.property, group: 1),
        Rule(#"#[0-9a-fA-F]{3,8}\b"#, SyntaxTheme.number),
        Rule(#"\b\d+\.?\d*(px|em|rem|%|vh|vw|s|ms|fr|deg)?\b"#, SyntaxTheme.number),
        Rule(#"[.#][A-Za-z_-][A-Za-z0-9_-]*"#, SyntaxTheme.type),
    ].compactMap { $0 }
}
