import SwiftUI

/// Publish an app on a public hostname through the operator's existing
/// Cloudflare Tunnel, and see what's already published.
struct TunnelView: View {
    let appID: Int
    let app: HostedApp

    @EnvironmentObject private var session: VenvySession
    @Environment(\.openURL) private var openURL

    @State private var routes: [CFRoute] = []
    @State private var zones: [CFZone] = []
    @State private var isLoading = true
    @State private var loadError: String?
    @State private var zonesError: String?

    @State private var subdomain = ""
    @State private var selectedZone: CFZone?
    @State private var selectedPort: Int?
    @State private var isPublishing = false
    @State private var checkResult: String?
    @State private var checkOK: Bool?
    @State private var pendingUnpublish: CFRoute?

    private var canOperate: Bool { session.canOperate }

    private var hostnamePreview: String {
        guard let zone = selectedZone else { return "" }
        let sub = subdomain.trimmingCharacters(in: .whitespaces).lowercased()
        return sub.isEmpty ? zone.name : "\(sub).\(zone.name)"
    }

    var body: some View {
        Form {
            if isLoading {
                Section { ProgressView().frame(maxWidth: .infinity) }
            }

            if let loadError {
                Section {
                    ErrorBanner(message: loadError) { Task { await load() } }
                        .listRowBackground(Color.clear)
                }
            }

            publishedSection

            if canOperate {
                publishSection
            }

            Section {
                Text("Publishing updates your Cloudflare Tunnel's ingress rules and upserts a DNS CNAME. If another app already holds the hostname, it's released from that app first.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .listRowBackground(Color.clear)
        }
        .navigationTitle("Public access")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
        .alert("Unpublish \(pendingUnpublish?.hostname ?? "")?", isPresented: .init(
            get: { pendingUnpublish != nil }, set: { if !$0 { pendingUnpublish = nil } }
        ), presenting: pendingUnpublish) { route in
            Button("Unpublish", role: .destructive) { unpublish(route) }
            Button("Cancel", role: .cancel) { pendingUnpublish = nil }
        } message: { _ in
            Text("This removes the tunnel ingress rule and the DNS record. The app keeps running locally.")
        }
    }

    // MARK: - Sections

    private var publishedSection: some View {
        Section {
            if routes.isEmpty && !isLoading {
                Text("Not published. This app is only reachable on your local network.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(routes) { route in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Image(systemName: "globe")
                                .foregroundStyle(Color.accentColor)
                            Text(route.hostname)
                                .font(.subheadline.weight(.medium))
                            Spacer()
                            if let port = route.localPort {
                                Text(":\(String(port))")
                                    .font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                            }
                        }
                        if let url = URL(string: "https://\(route.hostname)") {
                            Button {
                                openURL(url)
                            } label: {
                                Label("https://\(route.hostname)", systemImage: "arrow.up.right.square")
                                    .font(.caption)
                            }
                        }
                    }
                    .swipeActions(edge: .trailing) {
                        if canOperate {
                            Button(role: .destructive) {
                                pendingUnpublish = route
                            } label: { Label("Unpublish", systemImage: "globe.badge.chevron.backward") }
                        }
                    }
                }
            }
        } header: {
            Text("Published routes")
        }
    }

    private var publishSection: some View {
        Section {
            if app.allPorts.isEmpty {
                Label("This app has no port assigned. Set one in Settings before publishing.",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                    .foregroundStyle(VenvyColor.warning)
            } else if let zonesError {
                Label(zonesError, systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                    .foregroundStyle(VenvyColor.warning)
            } else if zones.isEmpty && !isLoading {
                Label("No Cloudflare zones available. An admin needs to configure Cloudflare credentials in System settings.",
                      systemImage: "info.circle")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                Picker("Local port", selection: $selectedPort) {
                    ForEach(app.allPorts, id: \.self) { port in
                        Text(String(port)).tag(Optional(port))
                    }
                }
                LabeledContent("Subdomain") {
                    TextField("app", text: $subdomain)
                        .multilineTextAlignment(.trailing)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                }
                Picker("Domain", selection: $selectedZone) {
                    Text("Choose…").tag(Optional<CFZone>.none)
                    ForEach(zones) { zone in
                        Text(zone.name).tag(Optional(zone))
                    }
                }

                if !hostnamePreview.isEmpty {
                    DetailRow(label: "Hostname", value: hostnamePreview,
                              monospaced: true, copyable: true)
                }

                if let checkResult {
                    Label(checkResult, systemImage: checkOK == true
                          ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.subheadline)
                        .foregroundStyle(checkOK == true ? VenvyColor.success : VenvyColor.warning)
                }

                Button {
                    runCheck()
                } label: {
                    Label("Pre-flight check", systemImage: "checkmark.shield")
                }
                .disabled(!canPublish || isPublishing)

                Button {
                    publish()
                } label: {
                    HStack {
                        if isPublishing { ProgressView().controlSize(.small) }
                        Text("Publish")
                    }
                }
                .disabled(!canPublish || isPublishing)
            }
        } header: {
            Text("Publish")
        } footer: {
            if app.status != .running && !app.allPorts.isEmpty {
                Text("The app isn't running, so the public URL won't respond until you start it.")
            }
        }
    }

    private var canPublish: Bool {
        selectedZone != nil && selectedPort != nil && !hostnamePreview.isEmpty
    }

    // MARK: - Data

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        selectedPort = selectedPort ?? app.port ?? app.allPorts.first

        do {
            routes = try await client.tunnelRoutes(appID: appID)
            loadError = nil
        } catch let error as APIError {
            if error.isAuthFailure { session.handleAuthFailure(); isLoading = false; return }
            loadError = error.localizedDescription
        } catch {
            loadError = error.localizedDescription
        }

        // Zones need operator role and configured credentials; a failure here
        // is informational, not fatal to the screen.
        if canOperate {
            do {
                zones = try await client.cloudflareZones()
                zonesError = nil
            } catch let error as APIError {
                zonesError = error.localizedDescription
            } catch {
                zonesError = error.localizedDescription
            }
        }
        isLoading = false
    }

    private func runCheck() {
        guard let client = session.client, let port = selectedPort else { return }
        checkResult = nil
        checkOK = nil
        Task {
            do {
                let result = try await client.tunnelCheck(appID: appID,
                                                          hostname: hostnamePreview,
                                                          localPort: port)
                // The check endpoint's exact shape varies; surface whatever
                // message it gives, and treat an explicit ok:false as failure.
                let okFlag: Bool? = {
                    if case let .bool(b)? = result["ok"] { return b }
                    return nil
                }()
                let message = result["detail"]?.stringValue
                    ?? result["message"]?.stringValue
                    ?? (okFlag == false ? "That hostname or port isn't available." : "Looks good — ready to publish.")
                checkOK = okFlag ?? true
                checkResult = message
            } catch let error as APIError {
                checkOK = false
                checkResult = error.localizedDescription
            } catch {
                checkOK = false
                checkResult = error.localizedDescription
            }
        }
    }

    private func publish() {
        guard let client = session.client, let port = selectedPort else { return }
        isPublishing = true
        Task {
            defer { isPublishing = false }
            do {
                let result = try await client.tunnelPublish(appID: appID,
                                                            hostname: hostnamePreview,
                                                            localPort: port)
                session.toasts.success("Published at \(result.url ?? hostnamePreview).")
                subdomain = ""
                checkResult = nil
                await load()
                await session.loadApps(showSpinner: false)
            } catch {
                session.report(error, context: "Couldn't publish")
            }
        }
    }

    private func unpublish(_ route: CFRoute) {
        guard let client = session.client, let port = route.localPort else {
            pendingUnpublish = nil
            return
        }
        pendingUnpublish = nil
        Task {
            do {
                try await client.tunnelUnpublish(appID: appID, localPort: port)
                session.toasts.success("Unpublished \(route.hostname).")
                await load()
                await session.loadApps(showSpinner: false)
            } catch {
                session.report(error, context: "Couldn't unpublish")
            }
        }
    }
}
