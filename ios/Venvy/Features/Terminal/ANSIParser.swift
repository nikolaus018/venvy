import SwiftUI

/// A minimal ANSI/VT sequence interpreter.
///
/// This is *not* a full terminal emulator — there's no cursor addressing,
/// scroll regions or alternate screen buffer. It handles the subset that
/// ordinary command output uses: SGR colour/style, erase-line, carriage
/// returns (so progress bars overwrite in place) and backspace. Full-screen
/// TUI programs like vim or htop will not render correctly.
struct ANSIParser {

    struct Style: Equatable {
        var foreground: Color?
        var background: Color?
        var bold = false
        var dim = false
        var italic = false
        var underline = false
        var inverse = false

        static let none = Style()
    }

    struct Span: Identifiable, Equatable {
        let id = UUID()
        var text: String
        var style: Style
    }

    struct Line: Identifiable, Equatable {
        let id: Int
        var spans: [Span]

        var plainText: String { spans.map(\.text).joined() }
        var isEmpty: Bool { spans.allSatisfy { $0.text.isEmpty } }
    }

    // MARK: - State carried across chunks

    private var current = Style.none
    private var lines: [[Span]] = [[]]
    /// Column within the last line, so \r and \b can overwrite characters.
    private var column = 0
    /// Partial escape sequence left over when a chunk splits mid-sequence.
    private var pending = ""
    private var nextLineID = 0
    private var lineIDs: [Int] = [0]

    private let maxLines: Int

    init(maxLines: Int = 5000) {
        self.maxLines = maxLines
        nextLineID = 1
    }

    mutating func reset() {
        current = .none
        lines = [[]]
        lineIDs = [0]
        nextLineID = 1
        column = 0
        pending = ""
    }

    /// Feeds a chunk of PTY output and returns the full rendered scrollback.
    ///
    /// Iterates Unicode *scalars* rather than Characters on purpose: Swift
    /// treats CRLF as a single grapheme cluster, so a Character-driven loop
    /// matches neither "\r" nor "\n" and silently swallows every DOS-style
    /// line break — which is exactly what a PTY sends.
    mutating func feed(_ chunk: String) -> [Line] {
        let scalars = Array((pending + chunk).unicodeScalars)
        pending = ""

        var i = 0
        while i < scalars.count {
            let scalar = scalars[i]

            if scalar == "\u{1B}" {
                // Escape sequences can be split across frames.
                guard let (next, isComplete) = Self.scanEscape(scalars, from: i), isComplete else {
                    pending = String(String.UnicodeScalarView(scalars[i...]))
                    break
                }
                apply(escape: String(String.UnicodeScalarView(scalars[i..<next])))
                i = next
                continue
            }

            switch scalar {
            case "\n":
                newline()
            case "\r":
                column = 0
            case "\u{08}":   // backspace
                if column > 0 { column -= 1 }
            case "\u{07}":   // bell — nothing sensible to do on iOS
                break
            case "\t":
                write(String(repeating: " ", count: 8 - (column % 8)))
            default:
                if scalar.value < 0x20 || scalar.value == 0x7F {
                    break     // drop remaining control characters
                }
                write(String(Character(scalar)))
            }
            i += 1
        }

        return renderedLines
    }

    var renderedLines: [Line] {
        zip(lineIDs, lines).map { Line(id: $0.0, spans: $0.1) }
    }

    // MARK: - Text handling

    private mutating func newline() {
        lines.append([])
        lineIDs.append(nextLineID)
        nextLineID += 1
        column = 0
        if lines.count > maxLines {
            let overflow = lines.count - maxLines
            lines.removeFirst(overflow)
            lineIDs.removeFirst(overflow)
        }
    }

    /// Writes text at the current column, overwriting what's already there —
    /// this is what makes `\r`-driven progress bars behave.
    private mutating func write(_ text: String) {
        guard var last = lines.last else { return }
        let existing = last.reduce(into: "") { $0 += $1.text }

        if column >= existing.count {
            // Appending at the end: extend with padding if we skipped ahead.
            if column > existing.count {
                let pad = String(repeating: " ", count: column - existing.count)
                last.append(Span(text: pad, style: .none))
            }
            if let lastSpan = last.last, lastSpan.style == current {
                last[last.count - 1].text += text
            } else {
                last.append(Span(text: text, style: current))
            }
        } else {
            // Overwriting mid-line: rebuild the line as flat characters and
            // splice, which is simpler than surgically editing spans.
            var chars = Self.flatten(last)
            for (offset, ch) in text.enumerated() {
                let idx = column + offset
                if idx < chars.count {
                    chars[idx] = (ch, current)
                } else {
                    chars.append((ch, current))
                }
            }
            last = Self.coalesce(chars)
        }

        lines[lines.count - 1] = last
        column += text.count
    }

    private static func flatten(_ spans: [Span]) -> [(Character, Style)] {
        spans.flatMap { span in span.text.map { ($0, span.style) } }
    }

    private static func coalesce(_ chars: [(Character, Style)]) -> [Span] {
        var out: [Span] = []
        for (ch, style) in chars {
            if var last = out.last, last.style == style {
                last.text.append(ch)
                out[out.count - 1] = last
            } else {
                out.append(Span(text: String(ch), style: style))
            }
        }
        return out
    }

    // MARK: - Escape sequences

    /// Returns the index just past the escape sequence, plus whether it was
    /// complete. Incomplete sequences are buffered until the next chunk.
    private static func scanEscape(_ s: [Unicode.Scalar], from start: Int) -> (Int, Bool)? {
        var i = start + 1
        guard i < s.count else { return (start, false) }

        let second = s[i]
        if second == "[" {
            // CSI: ESC [ params... final-byte (0x40–0x7E)
            i += 1
            while i < s.count {
                let v = s[i].value
                if v >= 0x40 && v <= 0x7E { return (i + 1, true) }
                i += 1
            }
            return (start, false)
        }
        if second == "]" {
            // OSC: ESC ] ... terminated by BEL or ESC \
            i += 1
            while i < s.count {
                if s[i] == "\u{07}" { return (i + 1, true) }
                if s[i] == "\u{1B}", i + 1 < s.count, s[i + 1] == "\\" { return (i + 2, true) }
                i += 1
            }
            return (start, false)
        }
        // Two-character escape (e.g. ESC = , ESC > , ESC M)
        return (i + 1, true)
    }

    private mutating func apply(escape: String) {
        guard escape.count >= 2 else { return }
        let body = escape.dropFirst()          // drop ESC
        guard body.first == "[" else { return } // only CSI carries what we support
        let inner = body.dropFirst()
        guard let final = inner.last else { return }
        let params = inner.dropLast()

        switch final {
        case "m":
            applySGR(String(params))
        case "K":
            eraseInLine(String(params))
        case "J":
            // Erase display: treat 2/3 as "clear scrollback", which is what
            // `clear` sends and the only variant worth honouring here.
            let code = Int(params) ?? 0
            if code == 2 || code == 3 { reset() }
        case "C":
            column += max(Int(params) ?? 1, 1)
        case "D":
            column = max(0, column - max(Int(params) ?? 1, 1))
        case "G":
            column = max(0, (Int(params) ?? 1) - 1)
        default:
            break   // cursor addressing, scroll regions etc. are out of scope
        }
    }

    private mutating func eraseInLine(_ params: String) {
        let mode = Int(params) ?? 0
        guard var last = lines.last else { return }
        var chars = Self.flatten(last)
        switch mode {
        case 0:  // cursor to end of line
            if column < chars.count { chars.removeSubrange(column..<chars.count) }
        case 1:  // start of line to cursor
            for i in 0..<min(column, chars.count) { chars[i] = (" ", .none) }
        case 2:
            chars.removeAll()
        default:
            break
        }
        last = Self.coalesce(chars)
        lines[lines.count - 1] = last
    }

    private mutating func applySGR(_ params: String) {
        let codes = params.isEmpty
            ? [0]
            : params.split(separator: ";", omittingEmptySubsequences: false)
                    .map { Int($0) ?? 0 }

        var i = 0
        while i < codes.count {
            let code = codes[i]
            switch code {
            case 0: current = .none
            case 1: current.bold = true
            case 2: current.dim = true
            case 3: current.italic = true
            case 4: current.underline = true
            case 7: current.inverse = true
            case 22: current.bold = false; current.dim = false
            case 23: current.italic = false
            case 24: current.underline = false
            case 27: current.inverse = false
            case 30...37: current.foreground = Self.basic(code - 30, bright: false)
            case 90...97: current.foreground = Self.basic(code - 90, bright: true)
            case 39: current.foreground = nil
            case 40...47: current.background = Self.basic(code - 40, bright: false)
            case 100...107: current.background = Self.basic(code - 100, bright: true)
            case 49: current.background = nil
            case 38, 48:
                // Extended colour: 5;n (256-colour) or 2;r;g;b (truecolor)
                let isForeground = code == 38
                if i + 1 < codes.count {
                    let mode = codes[i + 1]
                    if mode == 5, i + 2 < codes.count {
                        let color = Self.xterm256(codes[i + 2])
                        if isForeground { current.foreground = color } else { current.background = color }
                        i += 2
                    } else if mode == 2, i + 4 < codes.count {
                        let color = Color(red: Double(codes[i + 2]) / 255,
                                          green: Double(codes[i + 3]) / 255,
                                          blue: Double(codes[i + 4]) / 255)
                        if isForeground { current.foreground = color } else { current.background = color }
                        i += 4
                    }
                }
            default:
                break
            }
            i += 1
        }
    }

    // MARK: - Palette

    private static func basic(_ index: Int, bright: Bool) -> Color {
        let normal: [Color] = [
            Color(red: 0.20, green: 0.22, blue: 0.26),  // black
            Color(red: 0.94, green: 0.33, blue: 0.31),  // red
            Color(red: 0.30, green: 0.76, blue: 0.44),  // green
            Color(red: 0.90, green: 0.68, blue: 0.24),  // yellow
            Color(red: 0.36, green: 0.60, blue: 0.94),  // blue
            Color(red: 0.76, green: 0.47, blue: 0.90),  // magenta
            Color(red: 0.29, green: 0.74, blue: 0.78),  // cyan
            Color(red: 0.85, green: 0.87, blue: 0.90),  // white
        ]
        let brightPalette: [Color] = [
            Color(red: 0.42, green: 0.45, blue: 0.50),
            Color(red: 1.00, green: 0.45, blue: 0.42),
            Color(red: 0.44, green: 0.88, blue: 0.56),
            Color(red: 1.00, green: 0.80, blue: 0.36),
            Color(red: 0.50, green: 0.72, blue: 1.00),
            Color(red: 0.87, green: 0.60, blue: 1.00),
            Color(red: 0.42, green: 0.86, blue: 0.90),
            Color.white,
        ]
        let palette = bright ? brightPalette : normal
        return palette[max(0, min(index, palette.count - 1))]
    }

    /// Standard xterm 256-colour cube: 0–15 basic, 16–231 6×6×6, 232–255 grey.
    private static func xterm256(_ n: Int) -> Color {
        switch n {
        case 0...7: return basic(n, bright: false)
        case 8...15: return basic(n - 8, bright: true)
        case 16...231:
            let v = n - 16
            let levels: [Double] = [0, 95, 135, 175, 215, 255]
            let r = levels[(v / 36) % 6] / 255
            let g = levels[(v / 6) % 6] / 255
            let b = levels[v % 6] / 255
            return Color(red: r, green: g, blue: b)
        case 232...255:
            let grey = Double(8 + (n - 232) * 10) / 255
            return Color(red: grey, green: grey, blue: grey)
        default:
            return .primary
        }
    }
}
