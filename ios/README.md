# Venvy for iOS

A native SwiftUI client for a self-hosted [Venvy](../venvy-api-documentation.md)
server. You point it at your server's address, sign in, and manage your hosted
apps — start/stop/restart, browse and edit files, tail logs, manage
pip/cargo packages, open a real shell, and publish through Cloudflare.

Everything the web frontend does, done natively.

## Requirements

| | |
|---|---|
| Xcode | 26 or newer (iOS 26 SDK) |
| Deployment target | iOS 18.0 |
| Dependencies | None — no SPM packages, no CocoaPods |

## Build and run

```bash
open ios/Venvy.xcodeproj
```

Pick a simulator or your device and hit Run. From the command line:

```bash
xcodebuild -project ios/Venvy.xcodeproj -scheme Venvy \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
```

To run on a physical device, set your own team and bundle identifier under
Signing & Capabilities (the project ships with `com.venvy.ios` and automatic
signing).

## Connecting to a server

On first launch you enter your server's address. You can type it loosely —
**Test connection** works out the rest and rewrites the field with what it
found:

| You type | It tries |
|---|---|
| `192.168.1.20` | `http://192.168.1.20:54783`, then `:80`, then https |
| `192.168.1.20:8080` | `http://…:8080`, then `https://…:8080` |
| `venvy.local` | `http://venvy.local:54783`, then `:80`, then https |
| `venvy.example.com` | `https://…` first, then the http fallbacks |
| `https://venvy.example.com` | exactly that — an explicit scheme is never second-guessed |

A bare LAN-style host (IP literal, single-label name, `.local`) is assumed to be
plain HTTP on Venvy's default port; a registered-looking domain is assumed to be
behind TLS. That distinction matters: guessing `http://…:54783` for a public
domain produces a connection that simply hangs.

You can save several servers and switch between them from the Apps tab's filter
menu, the login screen, or Account → Switch server.

## Authentication

The client speaks the same auth protocol as the browser frontend:

- a server-issued `session` cookie (HttpOnly, 30 days, sliding)
- a `csrf_token` cookie echoed back in `X-CSRF-Token` on every mutating request

`URLSession` stores and replays both. Because the CSRF middleware only mints a
token on a request that arrives *without* one, the client always performs a
`GET /api/auth/status` before its first `POST` — see `APIClient.bootstrapCSRF()`.

Being a native client, it isn't subject to CORS, so **you don't need to set
`ALLOWED_ORIGINS` on the server** for this app to work.

Supported flows: first-run admin setup, password login, TOTP two-factor
(including the `403 totp_required` challenge), password change, enabling and
disabling TOTP, and per-IP rate-limit messages.

Passwords are only stored if you ask them to be, in the Keychain, as
`kSecAttrAccessibleWhenUnlockedThisDeviceOnly` (never synced off the device).

## What's covered

| Area | |
|---|---|
| **Apps** | List with search and status filters, live status/metrics polling, pull-to-refresh, swipe to start/stop/restart |
| **Dashboard** | Status, freshly sampled CPU/memory/uptime, start/stop/restart, access URL |
| **Files** | Browse the sandbox, create/rename/duplicate/delete, upload from Files.app, download and share, text editor with a code-oriented key bar |
| **Logs** | Tail with follow mode, ANSI colour, severity highlighting, filtering, copy, export, clear |
| **Packages** | pip and cargo listing, install, uninstall, plus Python import scanning with one-tap install of what's missing |
| **Terminal** | Real PTY over WebSocket, ANSI rendering, a key strip for `esc`/`tab`/`^C`/arrows, resize control messages |
| **Settings** | Name, runtime, entrypoint, ports, restart policy, boot behaviour, resource limits, environment variables |
| **Public access** | Cloudflare tunnel routes, pre-flight checks, publish and unpublish |
| **System** (admin) | Host CPU/memory/disk/load, ports, users, database snapshots, Cloudflare credentials |
| **Account** | Profile, password, TOTP, saved-password control, sign out |

Role permissions are honoured throughout — a `viewer` gets a read-only UI rather
than buttons that fail, and redacted env vars are labelled as such.

## Design notes

Built against the iOS 26 SDK, so navigation bars, the tab bar, toolbars and
sheets adopt **Liquid Glass** automatically. `UI/Glass.swift` extends the same
material to the app's own floating surfaces (toasts, the start/stop cluster,
overlays) via `glassEffect` and `GlassEffectContainer`, with a
`.regularMaterial` fallback so the app still looks right on iOS 18–25.

The palette is carried over from the web frontend so both clients read as one
product. Light and dark are both supported.

## Networking and ATS

Self-hosted Venvy instances usually run over plain HTTP, so `Info.plist` sets
`NSAllowsArbitraryLoads`. Note that it deliberately does **not** set
`NSAllowsLocalNetworking`: when that key (or `NSAllowsArbitraryLoadsInWebContent`
/ `ForMedia`) is present, iOS 10+ *ignores* `NSAllowsArbitraryLoads`, which would
block plain-HTTP servers outside the local network. `NSAllowsArbitraryLoads`
alone already covers local hosts.

Connecting to a LAN server also requires the local-network permission, prompted
on first use (`NSLocalNetworkUsageDescription`).

## Terminal scope

`ANSIParser` is a pragmatic subset, not a full terminal emulator. It handles SGR
styling (16-colour, 256-colour and truecolor), carriage returns so progress bars
overwrite in place, backspace, tabs, erase-line and erase-display. It does not
implement cursor addressing, scroll regions or the alternate screen buffer, so
full-screen TUI programs (`vim`, `htop`) will not render correctly. Line-oriented
commands work well.

Run its regression tests with:

```bash
swiftc -o /tmp/ansitests \
  ios/Venvy/Features/Terminal/ANSIParser.swift \
  ios/Tools/ANSIParserTests/main.swift \
  -sdk $(xcrun --sdk macosx --show-sdk-path) && /tmp/ansitests
```

## App icon

`Assets.xcassets/AppIcon.appiconset/AppIcon.png` is generated from the web
frontend's mark, so it stays reproducible rather than being a checked-in binary
nobody can regenerate:

```bash
swift ios/Tools/make-appicon.swift \
  ios/Venvy/Assets.xcassets/AppIcon.appiconset/AppIcon.png
```

## Layout

```
ios/
  Venvy.xcodeproj/          file-system-synchronized — new .swift files are picked up automatically
  Venvy/
    VenvyApp.swift          entry point and phase routing
    Core/                   models, API client, endpoints, session, keychain, server config
    Features/               one folder per area (Apps, Files, Logs, Packages, Terminal, …)
    UI/                     theme, shared components, toasts, Liquid Glass helpers
  Tools/
    make-appicon.swift      icon generator
    ANSIParserTests/        parser regression tests
```
