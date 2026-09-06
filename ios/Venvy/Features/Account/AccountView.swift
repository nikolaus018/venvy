import SwiftUI

struct AccountView: View {
    @EnvironmentObject private var session: VenvySession
    @EnvironmentObject private var servers: ServerStore

    @State private var changingPassword = false
    @State private var settingUpTOTP = false
    @State private var disablingTOTP = false
    @State private var confirmLogout = false
    @State private var showingServers = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 14) {
                        ZStack {
                            Circle()
                                .fill(Color.accentColor.opacity(0.15))
                                .frame(width: 52, height: 52)
                            Text(initials)
                                .font(.system(.title3, design: .rounded).weight(.semibold))
                                .foregroundStyle(Color.accentColor)
                        }
                        VStack(alignment: .leading, spacing: 3) {
                            Text(session.username)
                                .font(.title3.weight(.semibold))
                            Text(session.role.label)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .padding(.vertical, 4)
                }

                Section("Server") {
                    if let server = session.currentServer {
                        DetailRow(label: "Name", value: server.name)
                        DetailRow(label: "Address", value: server.displayHost,
                                  monospaced: true, copyable: true)
                        if !server.isSecure {
                            Label("Traffic to this server isn't encrypted. Fine on a trusted LAN; use HTTPS if it's reachable from the internet.",
                                  systemImage: "exclamationmark.shield")
                                .font(.caption)
                                .foregroundStyle(VenvyColor.warning)
                        }
                    }
                    Button {
                        showingServers = true
                    } label: {
                        Label("Switch server", systemImage: "arrow.left.arrow.right")
                    }
                }

                Section {
                    Button {
                        changingPassword = true
                    } label: {
                        Label("Change password", systemImage: "key")
                    }

                    if session.totpEnabled {
                        HStack {
                            Label("Two-factor auth", systemImage: "lock.shield.fill")
                            Spacer()
                            Text("On")
                                .font(.caption.weight(.semibold))
                                .padding(.horizontal, 8)
                                .padding(.vertical, 3)
                                .background(VenvyColor.success.opacity(0.15), in: Capsule())
                                .foregroundStyle(VenvyColor.success)
                        }
                        Button(role: .destructive) {
                            disablingTOTP = true
                        } label: {
                            Label("Turn off two-factor", systemImage: "lock.open")
                        }
                    } else {
                        Button {
                            settingUpTOTP = true
                        } label: {
                            Label("Set up two-factor auth", systemImage: "lock.shield")
                        }
                    }
                } header: {
                    Text("Security")
                } footer: {
                    Text(session.totpEnabled
                         ? "You'll be asked for a code from your authenticator app each time you sign in."
                         : "Adds a 6-digit code from an authenticator app to every sign-in.")
                }

                Section {
                    if let server = session.currentServer, server.savePassword {
                        Button {
                            Keychain.deletePassword(for: server.id)
                            var updated = server
                            updated.savePassword = false
                            servers.upsert(updated)
                            session.toasts.success("Saved password removed.")
                        } label: {
                            Label("Forget saved password", systemImage: "key.slash")
                        }
                    }
                    Button(role: .destructive) {
                        confirmLogout = true
                    } label: {
                        Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
                    }
                }

                Section {
                    DetailRow(label: "Venvy for iOS", value: appVersion)
                } footer: {
                    Text("A native client for the Venvy application manager.")
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Account")
            .sheet(isPresented: $changingPassword) { ChangePasswordView() }
            .sheet(isPresented: $settingUpTOTP) { TOTPSetupView() }
            .sheet(isPresented: $showingServers) { ServerListView() }
            .sheet(isPresented: $disablingTOTP) { DisableTOTPView() }
            .alert("Sign out?", isPresented: $confirmLogout) {
                Button("Sign out", role: .destructive) {
                    Task { await session.logout() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("You'll need to sign in again to manage apps on this server.")
            }
        }
    }

    private var initials: String {
        let name = session.username
        guard !name.isEmpty else { return "?" }
        return String(name.prefix(2)).uppercased()
    }

    private var appVersion: String {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0"
        let b = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "1"
        return "\(v) (\(b))"
    }
}

// MARK: - Change password

struct ChangePasswordView: View {
    @EnvironmentObject private var session: VenvySession
    @EnvironmentObject private var servers: ServerStore
    @Environment(\.dismiss) private var dismiss

    @State private var current = ""
    @State private var newPassword = ""
    @State private var confirm = ""
    @State private var isSaving = false
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Current password") {
                    SecureField("current password", text: $current)
                        .textContentType(.password)
                }
                Section {
                    SecureField("new password", text: $newPassword)
                        .textContentType(.newPassword)
                    SecureField("repeat new password", text: $confirm)
                        .textContentType(.newPassword)
                } header: {
                    Text("New password")
                } footer: {
                    Text("At least 8 characters, including a letter and a digit.")
                }
                if let errorText {
                    Section {
                        Text(errorText)
                            .font(.subheadline)
                            .foregroundStyle(VenvyColor.danger)
                    }
                }
            }
            .navigationTitle("Change password")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isSaving {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Save") { save() }
                            .fontWeight(.semibold)
                            .disabled(current.isEmpty || newPassword.isEmpty || confirm.isEmpty)
                    }
                }
            }
        }
    }

    private func save() {
        guard let client = session.client else { return }
        errorText = nil
        guard newPassword == confirm else {
            errorText = "The new passwords don't match."
            return
        }
        guard newPassword.count >= 8,
              newPassword.rangeOfCharacter(from: .letters) != nil,
              newPassword.rangeOfCharacter(from: .decimalDigits) != nil else {
            errorText = "Use at least 8 characters, with a letter and a digit."
            return
        }
        isSaving = true
        Task {
            defer { isSaving = false }
            do {
                try await client.changePassword(current: current, new: newPassword, confirm: confirm)
                // A stored password is now stale — replace it rather than
                // leaving the old one to fail on next launch.
                if let server = session.currentServer, server.savePassword {
                    Keychain.savePassword(newPassword, for: server.id)
                }
                session.toasts.success("Password changed.")
                dismiss()
            } catch let error as APIError {
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

// MARK: - TOTP

struct TOTPSetupView: View {
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var setup: TOTPSetup?
    @State private var code = ""
    @State private var isLoading = true
    @State private var isConfirming = false
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            Form {
                if isLoading {
                    Section { ProgressView().frame(maxWidth: .infinity) }
                } else if let setup {
                    Section {
                        VStack(spacing: 12) {
                            Image(systemName: "lock.shield")
                                .font(.system(size: 34, weight: .light))
                                .foregroundStyle(Color.accentColor)
                            Text("Add this secret to your authenticator app, then enter the code it shows.")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                    }
                    .listRowBackground(Color.clear)

                    Section {
                        Text(setup.secret)
                            .font(.system(.body, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .center)
                        Button {
                            UIPasteboard.general.string = setup.secret
                            session.toasts.success("Secret copied.")
                        } label: {
                            Label("Copy secret", systemImage: "doc.on.doc")
                        }
                        if let url = URL(string: setup.uri) {
                            Link(destination: url) {
                                Label("Open in authenticator app", systemImage: "arrow.up.right.square")
                            }
                        }
                    } header: {
                        Text("Setup key")
                    } footer: {
                        Text("Standard TOTP — SHA-1, 6 digits, 30-second step.")
                    }

                    Section {
                        TextField("000000", text: $code)
                            .keyboardType(.numberPad)
                            .textContentType(.oneTimeCode)
                            .multilineTextAlignment(.center)
                            .font(.system(.title2, design: .monospaced).weight(.semibold))
                            .onChange(of: code) { _, new in
                                let digits = new.filter(\.isNumber)
                                if digits != new || digits.count > 6 {
                                    code = String(digits.prefix(6))
                                }
                            }
                    } header: {
                        Text("Verification code")
                    }
                } else if let errorText {
                    Section {
                        Text(errorText)
                            .font(.subheadline)
                            .foregroundStyle(VenvyColor.danger)
                    }
                }

                if setup != nil, let errorText {
                    Section {
                        Text(errorText)
                            .font(.subheadline)
                            .foregroundStyle(VenvyColor.danger)
                    }
                }
            }
            .navigationTitle("Two-factor auth")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isConfirming {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Enable") { confirm() }
                            .fontWeight(.semibold)
                            .disabled(code.count != 6)
                    }
                }
            }
            .task { await load() }
        }
    }

    private func load() async {
        guard let client = session.client else { return }
        isLoading = true
        do {
            setup = try await client.totpSetup()
            errorText = nil
        } catch let error as APIError {
            errorText = error.localizedDescription
        } catch {
            errorText = error.localizedDescription
        }
        isLoading = false
    }

    private func confirm() {
        guard let client = session.client else { return }
        errorText = nil
        isConfirming = true
        Task {
            defer { isConfirming = false }
            do {
                try await client.totpConfirm(code: code)
                session.toasts.success("Two-factor authentication is on.")
                await session.refreshAuthStatus()
                dismiss()
            } catch let error as APIError {
                code = ""
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}

struct DisableTOTPView: View {
    @EnvironmentObject private var session: VenvySession
    @Environment(\.dismiss) private var dismiss

    @State private var password = ""
    @State private var isSaving = false
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("current password", text: $password)
                        .textContentType(.password)
                } header: {
                    Text("Confirm it's you")
                } footer: {
                    Text("Turning off two-factor authentication means only your password protects this account.")
                }
                if let errorText {
                    Section {
                        Text(errorText)
                            .font(.subheadline)
                            .foregroundStyle(VenvyColor.danger)
                    }
                }
            }
            .navigationTitle("Turn off 2FA")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isSaving {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Turn off") { disable() }
                            .fontWeight(.semibold)
                            .disabled(password.isEmpty)
                    }
                }
            }
        }
    }

    private func disable() {
        guard let client = session.client else { return }
        errorText = nil
        isSaving = true
        Task {
            defer { isSaving = false }
            do {
                try await client.totpDisable(password: password)
                session.toasts.success("Two-factor authentication is off.")
                await session.refreshAuthStatus()
                dismiss()
            } catch let error as APIError {
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }
}
