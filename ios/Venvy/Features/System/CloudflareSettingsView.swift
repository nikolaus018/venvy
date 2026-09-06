import SwiftUI

/// Admin-only Cloudflare credentials, stored server-side (encrypted at rest
/// under the `cf_` settings prefix) and verified with the test endpoint.
struct CloudflareSettingsView: View {
    @EnvironmentObject private var session: VenvySession

    @State private var apiToken = ""
    @State private var accountID = ""
    @State private var tunnelID = ""
    @State private var revealToken = false

    @State private var isLoading = true
    @State private var isSaving = false
    @State private var isTesting = false
    @State private var testLines: [TestLine] = []
    @State private var errorText: String?

    struct TestLine: Identifiable {
        let id = UUID()
        var label: String
        var ok: Bool
    }

    var body: some View {
        Form {
            Section {
                if revealToken {
                    TextField("API token", text: $apiToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                } else {
                    SecureField("API token", text: $apiToken)
                        .font(.body.monospaced())
                }
                Toggle("Reveal token", isOn: $revealToken)
                    .font(.subheadline)
            } header: {
                Text("API token")
            } footer: {
                Text("Needs Zone:DNS:Edit and Account:Cloudflare Tunnel:Edit permissions.")
            }

            Section("Identifiers") {
                LabeledContent("Account ID") {
                    TextField("account id", text: $accountID)
                        .multilineTextAlignment(.trailing)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                }
                LabeledContent("Tunnel ID") {
                    TextField("tunnel id", text: $tunnelID)
                        .multilineTextAlignment(.trailing)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                }
            }

            Section {
                Button {
                    save()
                } label: {
                    HStack {
                        if isSaving { ProgressView().controlSize(.small) }
                        Label("Save credentials", systemImage: "square.and.arrow.down")
                    }
                }
                .disabled(isSaving)

                Button {
                    runTest()
                } label: {
                    HStack {
                        if isTesting { ProgressView().controlSize(.small) }
                        Label("Test connection", systemImage: "checkmark.shield")
                    }
                }
                .disabled(isTesting)
            }

            if !testLines.isEmpty {
                Section("Test results") {
                    ForEach(testLines) { line in
                        Label(line.label, systemImage: line.ok
                              ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(line.ok ? VenvyColor.success : VenvyColor.danger)
                            .font(.subheadline)
                    }
                }
            }

            if let errorText {
                Section {
                    Text(errorText)
                        .font(.subheadline)
                        .foregroundStyle(VenvyColor.danger)
                }
            }

            Section {
                Text("Credentials are stored on the Venvy server, encrypted with its local key. They're never kept on this device.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .listRowBackground(Color.clear)
        }
        .navigationTitle("Cloudflare")
        .navigationBarTitleDisplayMode(.inline)
        .overlay { if isLoading { ProgressView() } }
        .task { await load() }
    }

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        // Each key is independent; a missing one just stays blank.
        async let token = try? await client.setting(key: "cf_api_token")
        async let account = try? await client.setting(key: "cf_account_id")
        async let tunnel = try? await client.setting(key: "cf_tunnel_id")
        apiToken = (await token) ?? ""
        accountID = (await account) ?? ""
        tunnelID = (await tunnel) ?? ""
        isLoading = false
    }

    private func save() {
        guard let client = session.client else { return }
        errorText = nil
        isSaving = true
        Task {
            defer { isSaving = false }
            do {
                try await client.setSettings([
                    SettingItem(key: "cf_api_token", value: apiToken),
                    SettingItem(key: "cf_account_id", value: accountID.trimmingCharacters(in: .whitespaces)),
                    SettingItem(key: "cf_tunnel_id", value: tunnelID.trimmingCharacters(in: .whitespaces)),
                ])
                session.toasts.success("Cloudflare credentials saved.")
            } catch let error as APIError {
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }

    private func runTest() {
        guard let client = session.client else { return }
        errorText = nil
        testLines = []
        isTesting = true
        Task {
            defer { isTesting = false }
            do {
                let result = try await client.testCloudflare()
                testLines = Self.summarize(result)
                if testLines.isEmpty {
                    testLines = [TestLine(label: "Credentials accepted.", ok: true)]
                }
            } catch let error as APIError {
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }

    /// The test endpoint's payload isn't strictly specified, so turn whatever
    /// boolean-ish checks it returns into readable rows.
    private static func summarize(_ result: [String: JSONValue]) -> [TestLine] {
        var lines: [TestLine] = []
        let labels: [String: String] = [
            "account": "Account access",
            "tunnel": "Tunnel access",
            "dns": "DNS permissions",
            "zones": "Zone list",
            "token": "API token",
        ]
        for (key, value) in result.sorted(by: { $0.key < $1.key }) {
            let label = labels[key] ?? key.replacingOccurrences(of: "_", with: " ").capitalized
            switch value {
            case .bool(let ok):
                lines.append(TestLine(label: label, ok: ok))
            case .object(let obj):
                let ok: Bool = {
                    if case let .bool(b)? = obj["ok"] { return b }
                    return true
                }()
                let detail = obj["message"]?.stringValue ?? obj["detail"]?.stringValue
                lines.append(TestLine(label: detail.map { "\(label): \($0)" } ?? label, ok: ok))
            case .string(let s) where key != "detail":
                lines.append(TestLine(label: "\(label): \(s)", ok: true))
            default:
                break
            }
        }
        return lines
    }
}
