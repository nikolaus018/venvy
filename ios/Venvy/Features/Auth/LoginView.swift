import SwiftUI

struct LoginView: View {
    enum Mode { case login, firstRunSetup }
    let mode: Mode

    @EnvironmentObject private var session: VenvySession
    @EnvironmentObject private var servers: ServerStore

    @State private var username = ""
    @State private var password = ""
    @State private var confirmPassword = ""
    @State private var totpCode = ""
    @State private var needsTOTP = false
    @State private var savePassword = false
    @State private var isSubmitting = false
    @State private var errorText: String?
    @State private var showingServers = false

    @FocusState private var focus: Field?
    private enum Field { case username, password, confirm, totp }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 20) {
                    header
                    card
                    if mode == .login { serverSwitcher }
                }
                .padding(.horizontal, 20)
                .padding(.top, 28)
                .padding(.bottom, 40)
                .frame(maxWidth: 460)
                .frame(maxWidth: .infinity)
            }
            .background(Color(.systemGroupedBackground))
            .scrollDismissesKeyboard(.interactively)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showingServers = true
                    } label: {
                        Image(systemName: "server.rack")
                    }
                    .accessibilityLabel("Servers")
                }
            }
            .sheet(isPresented: $showingServers) { ServerListView() }
        }
        .onAppear(perform: prefill)
    }

    // MARK: - Pieces

    private var header: some View {
        VStack(spacing: 8) {
            Text("Venvy")
                .font(.system(size: 40, design: .rounded).weight(.bold))
            if let server = session.currentServer {
                HStack(spacing: 5) {
                    Image(systemName: server.isSecure ? "lock.fill" : "network")
                        .font(.caption2)
                    Text(server.displayHost)
                        .font(.caption.monospaced())
                }
                .foregroundStyle(.secondary)
            }
            Text(mode == .firstRunSetup
                 ? "This server has no accounts yet. Create the first admin."
                 : "Sign in to manage your apps.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 2)
        }
    }

    private var card: some View {
        VStack(spacing: 14) {
            if needsTOTP {
                totpField
            } else {
                credentialFields
            }

            if let errorText {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(VenvyColor.danger)
                    Text(errorText)
                        .font(.subheadline)
                        .foregroundStyle(VenvyColor.danger)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Button(action: submit) {
                HStack(spacing: 8) {
                    if isSubmitting { ProgressView().controlSize(.small).tint(.white) }
                    Text(primaryButtonTitle)
                        .fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 4)
            }
            .venvyProminentButton()
            .controlSize(.large)
            .disabled(!canSubmit || isSubmitting)

            if needsTOTP {
                Button("Use a different account") {
                    withAnimation {
                        needsTOTP = false
                        totpCode = ""
                        errorText = nil
                    }
                }
                .font(.subheadline)
            }
        }
        .padding(18)
        .background(Color(.secondarySystemGroupedBackground),
                    in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var credentialFields: some View {
        VStack(spacing: 12) {
            LabeledField(title: "Username", systemImage: "person") {
                TextField("username", text: $username)
                    .textContentType(.username)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focus, equals: .username)
                    .submitLabel(.next)
                    .onSubmit { focus = .password }
            }

            LabeledField(title: "Password", systemImage: "lock") {
                SecureField("password", text: $password)
                    .textContentType(mode == .firstRunSetup ? .newPassword : .password)
                    .focused($focus, equals: .password)
                    .submitLabel(mode == .firstRunSetup ? .next : .go)
                    .onSubmit {
                        if mode == .firstRunSetup { focus = .confirm } else { submit() }
                    }
            }

            if mode == .firstRunSetup {
                LabeledField(title: "Confirm password", systemImage: "lock.rotation") {
                    SecureField("repeat password", text: $confirmPassword)
                        .textContentType(.newPassword)
                        .focused($focus, equals: .confirm)
                        .submitLabel(.go)
                        .onSubmit(submit)
                }
                Text("At least 8 characters, including a letter and a digit.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            if mode == .login {
                Toggle(isOn: $savePassword) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Remember password").font(.subheadline)
                        Text("Stored in this device's Keychain.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.top, 2)
            }
        }
    }

    private var totpField: some View {
        VStack(spacing: 12) {
            Image(systemName: "lock.shield")
                .font(.system(size: 30, weight: .light))
                .foregroundStyle(Color.accentColor)
            Text("Two-factor code")
                .font(.headline)
            Text("Enter the 6-digit code for \(username) from your authenticator app.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)

            TextField("000000", text: $totpCode)
                .textContentType(.oneTimeCode)
                .keyboardType(.numberPad)
                .multilineTextAlignment(.center)
                .font(.system(.title2, design: .monospaced).weight(.semibold))
                .padding(.vertical, 10)
                .background(Color(.tertiarySystemFill),
                            in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .focused($focus, equals: .totp)
                .onChange(of: totpCode) { _, new in
                    // Keep it to 6 digits and submit automatically once complete.
                    let digits = new.filter(\.isNumber)
                    if digits != new { totpCode = String(digits.prefix(6)); return }
                    if digits.count > 6 { totpCode = String(digits.prefix(6)) }
                    if totpCode.count == 6 && !isSubmitting { submit() }
                }
        }
    }

    private var serverSwitcher: some View {
        Button {
            showingServers = true
        } label: {
            Label("Change server", systemImage: "arrow.left.arrow.right")
                .font(.subheadline)
        }
    }

    private var primaryButtonTitle: String {
        if needsTOTP { return "Verify" }
        return mode == .firstRunSetup ? "Create admin account" : "Sign in"
    }

    private var canSubmit: Bool {
        if needsTOTP { return totpCode.count == 6 }
        let u = username.trimmingCharacters(in: .whitespaces)
        if u.isEmpty || password.isEmpty { return false }
        if mode == .firstRunSetup { return !confirmPassword.isEmpty }
        return true
    }

    // MARK: - Actions

    private func prefill() {
        guard let server = session.currentServer else { return }
        if username.isEmpty { username = server.username }
        savePassword = server.savePassword
        if server.savePassword, let saved = Keychain.password(for: server.id) {
            password = saved
        }
        if username.isEmpty { focus = .username }
        else if password.isEmpty { focus = .password }
    }

    private func submit() {
        guard canSubmit, !isSubmitting else { return }
        focus = nil
        errorText = nil
        isSubmitting = true

        Task {
            defer { isSubmitting = false }
            do {
                if mode == .firstRunSetup {
                    guard password == confirmPassword else {
                        errorText = "Those passwords don't match."
                        return
                    }
                    guard isPasswordAcceptable(password) else {
                        errorText = "Use at least 8 characters, with a letter and a digit."
                        return
                    }
                    try await session.firstRunSetup(
                        username: username.trimmingCharacters(in: .whitespaces),
                        password: password)
                    session.toasts.success("Welcome to Venvy.")
                } else {
                    try await session.login(
                        username: username.trimmingCharacters(in: .whitespaces),
                        password: password,
                        totpCode: needsTOTP ? totpCode : nil)
                    persistPasswordPreference()
                    session.toasts.success("Signed in as \(session.username).")
                }
            } catch APIError.totpRequired {
                withAnimation {
                    needsTOTP = true
                    focus = .totp
                }
            } catch let error as APIError {
                if needsTOTP { totpCode = "" }
                errorText = error.localizedDescription
            } catch {
                errorText = error.localizedDescription
            }
        }
    }

    /// Mirrors the server's policy so we fail fast with a clearer message.
    private func isPasswordAcceptable(_ p: String) -> Bool {
        p.count >= 8
            && p.rangeOfCharacter(from: .letters) != nil
            && p.rangeOfCharacter(from: .decimalDigits) != nil
    }

    private func persistPasswordPreference() {
        guard var server = session.currentServer else { return }
        server.savePassword = savePassword
        server.username = username.trimmingCharacters(in: .whitespaces)
        servers.upsert(server)
        if savePassword {
            Keychain.savePassword(password, for: server.id)
        } else {
            Keychain.deletePassword(for: server.id)
        }
    }
}

/// Small labelled input wrapper used on the auth screen.
struct LabeledField<Content: View>: View {
    let title: String
    var systemImage: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                if let systemImage {
                    Image(systemName: systemImage).font(.caption2)
                }
                Text(title).font(.caption.weight(.medium))
            }
            .foregroundStyle(.secondary)

            content
                .padding(.horizontal, 12)
                .padding(.vertical, 11)
                .background(Color(.tertiarySystemFill),
                            in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
    }
}
