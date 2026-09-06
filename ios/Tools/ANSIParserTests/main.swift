//
// Regression tests for ANSIParser, runnable without an Xcode test target:
//
//   swiftc -o /tmp/ansitests \
//       ios/Venvy/Features/Terminal/ANSIParser.swift \
//       ios/Tools/ANSIParserTests/main.swift \
//       -sdk $(xcrun --sdk macosx --show-sdk-path) && /tmp/ansitests
//
// (The file must be called main.swift for Swift to allow top-level code.)
//
// The cases below are the sequences a real bash PTY sends, including the CRLF
// case that once collapsed the whole session onto one line (Swift treats
// "\r\n" as a single Character, so a Character-driven loop drops it).
//
import Foundation

var failures = 0

func expect(_ label: String, _ chunks: [String], _ expected: [String]) {
    var parser = ANSIParser()
    var lines: [ANSIParser.Line] = []
    for chunk in chunks { lines = parser.feed(chunk) }
    let actual = lines.map(\.plainText)
    if actual == expected {
        print("✓ \(label)")
    } else {
        failures += 1
        print("✗ \(label)")
        print("    expected: \(expected)")
        print("    actual:   \(actual)")
    }
}

let prompt = "\u{1B}[01;32mhost\u{1B}[00m \u{1B}[01;34m/srv/app\u{1B}[00m# "
let bare = "host /srv/app# "

expect("LF line breaks", ["a\nb\nc"], ["a", "b", "c"])
expect("CRLF line breaks", ["a\r\nb\r\n"], ["a", "b", ""])
expect("CR split across chunks", ["abc\r", "\ndef\r\n"], ["abc", "def", ""])
expect("coloured prompt", [prompt], [bare])
expect("command, output, new prompt",
       [prompt, "ls\r\n", "main.py\r\n", prompt],
       [bare + "ls", "main.py", bare])
expect("bracketed paste toggles ignored",
       ["\u{1B}[?2004h" + prompt, "\u{1B}[?2004lecho hi\r\n", "hi\r\n"],
       [bare + "echo hi", "hi", ""])
expect("carriage return overwrites in place",
       ["10%\r", "55%\r", "100%\r\n"],
       ["100%", ""])
expect("erase to end of line", ["abcdef\r\u{1B}[Kxy\r\n"], ["xy", ""])
expect("clear screen resets scrollback",
       [prompt, "ls\r\n", "a\r\n", "\u{1B}[H\u{1B}[2J" + prompt],
       [bare])
expect("escape split across chunks",
       [prompt, "\u{1B}[01;3", "2mgreen\u{1B}[00m\r\n"],
       [bare + "green", ""])
expect("backspace moves the cursor back", ["abc\u{08}\u{08}X\r\n"], ["aXc", ""])
expect("tab expands to the next stop", ["a\tb\r\n"], ["a       b", ""])

// Styling must survive the scalar-driven scan.
var styleParser = ANSIParser()
let styled = styleParser.feed("\u{1B}[1;31mred\u{1B}[0m plain")
let spans = styled.first?.spans ?? []
if spans.count == 2, spans[0].text == "red", spans[0].style.bold,
   spans[0].style.foreground != nil, spans[1].style.foreground == nil {
    print("✓ SGR colour and bold attribution")
} else {
    failures += 1
    print("✗ SGR colour and bold attribution → \(spans.map { ($0.text, $0.style.bold) })")
}

print(failures == 0 ? "\nAll parser tests passed." : "\n\(failures) test(s) failed.")
exit(failures == 0 ? 0 : 1)
