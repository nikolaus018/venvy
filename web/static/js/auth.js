// Sign-in: session check, login and first-run setup, 2FA, account menu, password change.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Auth ─────────────────────────────────────────────────────────────────
        let _appInitialized = false;
        let _reauthUser = null;     // who had unsaved edits when the session expired

        function _clearAuthInputs() {
            ['password', 'totp-code', 'setup-code'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
            const step1 = document.getElementById('login-step-password');
            const step2 = document.getElementById('login-step-totp');
            if (step1) step1.classList.remove('hidden');
            if (step2) step2.classList.add('hidden');
            const submitBtn = document.getElementById('login-submit-btn');
            if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Sign In'; }
        }

        function _clearSessionCaches() {
            if (typeof stopBackupOpWatch === 'function') stopBackupOpWatch();
            if (typeof stopDeployPolling === 'function') stopDeployPolling();
            if (typeof stopUpdateWatch === 'function') stopUpdateWatch();
            if (typeof _jobsTimer !== 'undefined' && _jobsTimer) { clearTimeout(_jobsTimer); _jobsTimer = null; }
            if (typeof _cachedApps !== 'undefined') _cachedApps = [];
            if (typeof _appsEverLoaded !== 'undefined') _appsEverLoaded = false;
            if (typeof _lastAppListHash !== 'undefined') _lastAppListHash = '';
            if (typeof _lastKnownStatus !== 'undefined') {
                for (const k of Object.keys(_lastKnownStatus)) delete _lastKnownStatus[k];
            }
            if (typeof _cfCredsCache !== 'undefined') _cfCredsCache = null;
            if (typeof _cfZonesCache !== 'undefined') _cfZonesCache = null;
            if (typeof _cfRouteCache !== 'undefined') {
                for (const k of Object.keys(_cfRouteCache)) delete _cfRouteCache[k];
            }
            if (typeof _depData !== 'undefined') _depData = null;
            if (typeof _depApp !== 'undefined') _depApp = null;
            if (typeof _jobs !== 'undefined') _jobs = [];
            if (typeof _notifyData !== 'undefined') _notifyData = null;
            if (typeof _alertsData !== 'undefined') _alertsData = [];
            if (typeof _updateInfo !== 'undefined') _updateInfo = null;
            if (typeof _updateFrom !== 'undefined') _updateFrom = null;
            if (typeof _backupOp !== 'undefined') _backupOp = null;
            if (typeof _hostCapacity !== 'undefined') _hostCapacity = { memMB: null, cores: null };
        }

        function _showBootRetryError(msg) {
            const shellHidden = document.getElementById('app-shell')?.classList.contains('hidden');
            const authHidden = document.getElementById('auth-screen')?.classList.contains('hidden');
            if (shellHidden && authHidden) {
                document.getElementById('auth-screen').classList.remove('hidden');
                showToast(msg, 'error', 0, { actionLabel: 'Retry', onAction: checkAuth });
            }
        }

        async function checkAuth() {
            try {
                const res = await fetch(`${API}/auth/status`);
                if (!res.ok) {
                    _showBootRetryError(`Could not reach Venvy (HTTP ${res.status}).`);
                    return;
                }
                const data = await res.json();
                if (data.authenticated) {
                    // Edits kept through a session expiry are only for the same user.
                    if (_reauthUser !== null) {
                        if (_reauthUser !== data.username) deselectApp();
                        _reauthUser = null;
                    }
                    // Hide auth screen, show app — no reload needed
                    document.getElementById('auth-screen').classList.add('hidden');
                    document.getElementById('app-shell').classList.remove('hidden');
                    document.getElementById('current-user').innerText = data.username;
                    document.getElementById('user-avatar').innerText = (data.username || 'U')[0].toUpperCase();
                    currentUser = data;
                    document.getElementById('admin-users-btn').classList.toggle('hidden', data.role !== 'admin');
                    applyRoleUiGating(data.role);
                    if (!_appInitialized) {
                        _appInitialized = true;
                        initAceEditor();
                        loadRuntimes();
                        loadApps();
                        _loadHostCapacity();
                    }
                    startPolling();
                    startBackupOpWatch();
                } else {
                    // Not authenticated — show login
                    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
                    _clearSessionCaches();
                    _clearAuthInputs();
                    _appInitialized = false;
                    document.getElementById('app-shell').classList.add('hidden');
                    document.getElementById('auth-screen').classList.remove('hidden');
                    if (!data.user_exists) {
                        document.getElementById('setup-hint').classList.remove('hidden');
                        document.getElementById('setup-code').classList.remove('hidden');
                        document.getElementById('login-form').dataset.mode = 'setup';
                        // A password being chosen, not recalled: lets a
                        // password manager offer to generate and save one.
                        document.getElementById('password').autocomplete = 'new-password';
                    } else {
                        document.getElementById('setup-hint').classList.add('hidden');
                        document.getElementById('setup-code').classList.add('hidden');
                        delete document.getElementById('login-form').dataset.mode;
                        document.getElementById('password').autocomplete = 'current-password';
                    }
                }
            } catch (e) {
                // On initial boot when neither screen is visible, reveal login + retry.
                _showBootRetryError('Could not connect to Venvy — check your connection.');
            }
        }

        // Viewers are read-only: the backend already rejects mutating requests from
        // a viewer role (403), this just hides the controls so they don't hit that
        // error in the first place. Not a security boundary — the API checks are.
        function applyRoleUiGating(role) {
            const isViewer = role === 'viewer';
            document.body.classList.toggle('role-viewer', isViewer);
            document.querySelectorAll('#tab-settings input, #tab-settings select, #tab-settings textarea, #tab-settings [role="switch"]').forEach(el => {
                if (el.id === 'usage-range') return;
                if (isViewer) {
                    if (!el.disabled) el.dataset.viewerDisabled = '1';
                    el.disabled = true;
                } else if (el.dataset.viewerDisabled === '1') {
                    delete el.dataset.viewerDisabled;
                    el.disabled = false;
                }
            });
        }

        // Called when polling detects session has expired (10 consecutive 401s).
        // Shows a minimal re-auth overlay without wiping the current view — feels
        // native on iOS PWA instead of a jarring white-flash reload.
        function _showReAuthOverlay() {
            // If the full auth screen is already showing, nothing to do
            if (!document.getElementById('auth-screen').classList.contains('hidden')) return;
            // Tear down active polling, watchers, terminal, caches, and expired session key
            _clearSessionCaches();
            if (typeof _termDestroy === 'function') _termDestroy();
            // Unsaved edits stay for whoever signs back in; see checkAuth.
            if (_editorDirty) { _reauthUser = currentUser?.username || ''; _termAppId = null; }
            else if (typeof deselectApp === 'function') deselectApp();
            if (window._controlPollTimer) { clearInterval(window._controlPollTimer); window._controlPollTimer = null; }
            setAuthKey(null);
            document.getElementById('admin-users-btn')?.classList.add('hidden');
            // Reset login form to clean state and wipe any sensitive input values
            _loginTotpPending = false;
            _clearAuthInputs();
            document.getElementById('app-shell').classList.add('hidden');
            document.getElementById('auth-screen').classList.remove('hidden');
            _appInitialized = false;
            currentUser = null;
        }
        let _loginCooldownTimer = null;

        function _startLoginCooldown(seconds, submitBtn) {
            if (_loginCooldownTimer) clearInterval(_loginCooldownTimer);
            let remaining = seconds;
            submitBtn.disabled = true;
            submitBtn.textContent = `Try again in ${remaining}s`;
            _loginCooldownTimer = setInterval(() => {
                remaining--;
                if (remaining <= 0) {
                    clearInterval(_loginCooldownTimer);
                    _loginCooldownTimer = null;
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Sign In';
                } else {
                    submitBtn.textContent = `Try again in ${remaining}s`;
                }
            }, 1000);
        }

        document.getElementById('login-form').onsubmit = async (e) => {
            e.preventDefault();
            const mode = e.target.dataset.mode;
            const submitBtn = document.getElementById('login-submit-btn');
            const origText = submitBtn.textContent;
            try {

                if (mode === 'setup') {
                    const u = document.getElementById('username').value;
                    const p = document.getElementById('password').value;
                    const setupCode = document.getElementById('setup-code').value.trim();
                    if (!setupCode) { showToast('Enter the setup code.', 'warn'); return; }
                    submitBtn.disabled = true; submitBtn.textContent = 'Creating…';
                    const res = await fetch(API + '/auth/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p, setup_code: setupCode }) });
                    if (res.ok) {
                        setAuthKey((await res.json()).auth_key);
                        dismissAllToasts();      // a failed attempt's error is no longer true
                        _clearAuthInputs();
                        delete document.getElementById('login-form').dataset.mode;
                        await checkAuth();
                    }
                    else {
                        const err = await res.json().catch(() => ({ detail: 'Setup failed' }));
                        showToast(err.detail || 'Setup failed', 'error');
                        submitBtn.disabled = false; submitBtn.textContent = origText;
                    }
                    return;
                }

                // Login flow — may be step 1 (password) or step 2 (TOTP code)
                if (_loginTotpPending) {
                    // Step 2: submit TOTP code
                    const u = document.getElementById('username').value;
                    const p = document.getElementById('password').value;
                    const code = document.getElementById('totp-code').value.trim();
                    if (!code) { showToast('Enter the 6-digit code.', 'warn'); return; }
                    submitBtn.disabled = true; submitBtn.textContent = 'Verifying…';
                    const res = await fetch(API + '/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p, totp_code: code }) });
                    if (res.ok) {
                        _loginTotpPending = false;
                        setAuthKey((await res.json()).auth_key);
                        dismissAllToasts();      // a failed attempt's error is no longer true
                        _clearAuthInputs();
                        await checkAuth();
                    }
                    else {
                        const err = await res.json().catch(() => ({ detail: 'Invalid code' }));
                        showToast(err.detail || 'Invalid code', 'error');
                        document.getElementById('totp-code').select();
                        submitBtn.disabled = false; submitBtn.textContent = 'Verify';
                    }
                    return;
                }

                // Step 1: password
                const u = document.getElementById('username').value;
                const p = document.getElementById('password').value;
                submitBtn.disabled = true; submitBtn.textContent = 'Signing in…';
                const res = await fetch(API + '/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }) });
                if (res.ok) {
                    setAuthKey((await res.json()).auth_key);
                    dismissAllToasts();      // a failed attempt's error is no longer true
                    _clearAuthInputs();
                    await checkAuth();
                } else {
                    const err = await res.json().catch(() => ({ detail: 'Authentication failed' }));
                    if (res.status === 403 && err.detail === 'totp_required') {
                        // Show TOTP step
                        _loginTotpPending = true;
                        document.getElementById('login-step-password').classList.add('hidden');
                        document.getElementById('login-step-totp').classList.remove('hidden');
                        submitBtn.disabled = false; submitBtn.textContent = 'Verify';
                        setTimeout(() => document.getElementById('totp-code').focus(), 80);
                    } else if (res.status === 429) {
                        const retryAfter = parseInt(res.headers.get('X-Retry-After') || '30', 10);
                        const match = (err.detail || '').match(/(\d+)\s*second/);
                        const secs = match ? parseInt(match[1], 10) : retryAfter;
                        showToast(err.detail || 'Too many attempts. Please wait.', 'error', (secs + 2) * 1000);
                        _startLoginCooldown(secs, submitBtn);
                    } else {
                        showToast(err.detail || 'Authentication failed', 'error');
                        submitBtn.disabled = false; submitBtn.textContent = origText;
                    }
                }
            } catch (err) {
                showToast('Connection error. Check your network.', 'error');
                submitBtn.disabled = false; submitBtn.textContent = origText;
            }
        };
        async function logout() {
            // Drop the local key even if the request fails (offline, server
            // down): otherwise "Sign out" threw, did nothing, and left the
            // browser signed in.
            try { await fetch(`${API}/auth/logout`, { method: 'POST' }); }
            catch (e) { }
            finally { setAuthKey(null); window.location.reload(); }
        }

        // ── Login / TOTP step ─────────────────────────────────────────────────────

        function cancelTotpStep() {
            _loginTotpPending = false;
            document.getElementById('login-step-password').classList.remove('hidden');
            document.getElementById('login-step-totp').classList.add('hidden');
            document.getElementById('login-submit-btn').textContent = 'Sign In';
            document.getElementById('totp-code').value = '';
            document.getElementById('username').focus();
        }

        // ── Account Menu ─────────────────────────────────────────────────────────
        function openAccountMenu() {
            const uname = currentUser?.username || 'User';
            document.getElementById('acct-menu-username').textContent = uname;
            document.getElementById('acct-menu-avatar').textContent = uname.charAt(0).toUpperCase();
            // Update TOTP badge
            const totpEnabled = currentUser?.totp_enabled;
            const badge = document.getElementById('acct-totp-badge');
            const label = document.getElementById('acct-totp-label');
            if (totpEnabled) {
                badge.classList.remove('hidden');
                label.textContent = 'Authenticator (2FA)';
            } else {
                badge.classList.add('hidden');
                label.textContent = 'Set up Authenticator (2FA)';
            }
            openModal('account-menu-modal');
            _featherReplace();
        }

        // ── TOTP Setup / Disable ─────────────────────────────────────────────────
        async function openTotpSetup() {
            const totpEnabled = currentUser?.totp_enabled;
            const setupFlow = document.getElementById('totp-setup-flow');
            const disableFlow = document.getElementById('totp-disable-flow');
            const title = document.getElementById('totp-modal-title');
            const sub = document.getElementById('totp-modal-sub');

            document.getElementById('totp-setup-error').classList.add('hidden');
            document.getElementById('totp-disable-error').classList.add('hidden');
            document.getElementById('totp-confirm-code').value = '';
            document.getElementById('totp-disable-pwd').value = '';

            if (totpEnabled) {
                // Show disable flow
                setupFlow.classList.add('hidden');
                disableFlow.classList.remove('hidden');
                title.textContent = 'Disable 2FA';
                sub.textContent = 'Remove authenticator from your account';
            } else {
                // Show setup flow
                disableFlow.classList.add('hidden');
                setupFlow.classList.remove('hidden');
                title.textContent = 'Set Up Authenticator';
                sub.textContent = 'Scan the QR code with your app';

                // Clear QR area while loading
                const qrContainer = document.getElementById('totp-qr-container');
                qrContainer.innerHTML = '<span class="text-xs" style="color:var(--text3)">Loading…</span>';
                document.getElementById('totp-secret-display').textContent = '';
            }

            openModal('totp-setup-modal');
            _featherReplace();

            if (!totpEnabled) {
                try {
                    const res = await fetch(`${API}/auth/totp/setup`);
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Failed');

                    document.getElementById('totp-secret-display').textContent = data.secret;

                    // Generate QR code locally using qrcodejs — works fully offline
                    // once the page shell is cached by the service worker.
                    const qrContainer2 = document.getElementById('totp-qr-container');
                    qrContainer2.innerHTML = '';
                    // QRCode renders into a <div> with a nested <canvas> or <img>
                    const qrDiv = document.createElement('div');
                    qrContainer2.appendChild(qrDiv);
                    new QRCode(qrDiv, {
                        text: data.uri,
                        width: 192,
                        height: 192,
                        colorDark: '#000000',
                        colorLight: '#ffffff',
                        correctLevel: QRCode.CorrectLevel.M,
                    });
                    // Scale the inner img/canvas to fill the container
                    requestAnimationFrame(() => {
                        const inner = qrDiv.querySelector('canvas,img');
                        if (inner) { inner.style.width = '192px'; inner.style.height = '192px'; inner.style.borderRadius = '12px'; }
                    });
                } catch (e) {
                    const errSpan = document.createElement('span');
                    errSpan.className = 'text-xs text-center px-2';
                    errSpan.style.color = 'var(--red)';
                    errSpan.textContent = e.message || 'QR code generation failed';
                    const container = document.getElementById('totp-qr-container');
                    if (container) { container.innerHTML = ''; container.appendChild(errSpan); }
                }
            }
        }

        function copyTotpSecret() {
            const secret = document.getElementById('totp-secret-display').textContent;
            if (!secret) return;
            copyText(secret, 'Secret copied');
        }

        async function confirmTotpSetup() {
            const code = document.getElementById('totp-confirm-code').value.trim();
            const errEl = document.getElementById('totp-setup-error');
            errEl.classList.add('hidden');
            if (!code || code.length < 6) { errEl.textContent = 'Enter the 6-digit code.'; errEl.classList.remove('hidden'); return; }
            const btn = document.getElementById('totp-confirm-btn');
            btn.disabled = true; btn.textContent = 'Verifying…';
            try {
                const res = await fetch(`${API}/auth/totp/setup/confirm`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ totp_code: code }) });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || 'Verification failed');
                closeModal('totp-setup-modal');
                showToast('Two-factor authentication is on. There are no backup codes — keep the authenticator app safe.', 'success', 7000);
                // Update local state
                if (currentUser) currentUser.totp_enabled = true;
            } catch (e) {
                errEl.textContent = e.message;
                errEl.classList.remove('hidden');
                document.getElementById('totp-confirm-code').select();
            } finally {
                btn.disabled = false; btn.textContent = 'Verify & Enable';
            }
        }

        async function disableTotp() {
            const pwd = document.getElementById('totp-disable-pwd').value;
            const errEl = document.getElementById('totp-disable-error');
            errEl.classList.add('hidden');
            if (!pwd) { errEl.textContent = 'Enter your password.'; errEl.classList.remove('hidden'); return; }
            const btn = document.getElementById('totp-disable-btn');
            btn.disabled = true; btn.textContent = 'Disabling…';
            try {
                const res = await fetch(`${API}/auth/totp/disable`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pwd }) });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || 'Failed');
                closeModal('totp-setup-modal');
                showToast('2FA disabled.', 'success');
                if (currentUser) currentUser.totp_enabled = false;
            } catch (e) {
                errEl.textContent = e.message;
                errEl.classList.remove('hidden');
                document.getElementById('totp-disable-pwd').select();
            } finally {
                btn.disabled = false; btn.textContent = 'Disable 2FA';
            }
        }
        function openChangePwdModal() {
            ['current-pwd-input', 'new-pwd-input', 'confirm-pwd-input'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
            document.getElementById('pwd-match-hint').classList.add('hidden');
            openModal('change-pwd-modal');
            setTimeout(() => { const el = document.getElementById('current-pwd-input'); if (el) el.focus(); }, 80);
        }
        async function changePassword() {
            const currentPwd = document.getElementById('current-pwd-input').value;
            const newPwd = document.getElementById('new-pwd-input').value;
            const confirmPwd = document.getElementById('confirm-pwd-input').value;
            const matchHint = document.getElementById('pwd-match-hint');
            matchHint.classList.add('hidden');
            if (!currentPwd) { showToast('Enter your current password.', 'warn'); document.getElementById('current-pwd-input').focus(); return; }
            if (!newPwd) { showToast('Enter a new password.', 'warn'); document.getElementById('new-pwd-input').focus(); return; }
            if (newPwd !== confirmPwd) {
                matchHint.classList.remove('hidden');
                document.getElementById('confirm-pwd-input').focus();
                return;
            }
            const btn = document.getElementById('change-pwd-btn');
            btn.disabled = true; btn.textContent = 'Saving…';
            try {
                const res = await fetch(`${API}/auth/password`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current_password: currentPwd, password: newPwd, password_confirm: confirmPwd }),
                });
                if (res.ok) {
                    closeModal('change-pwd-modal');
                    showToast('Password changed successfully.', 'success');
                } else {
                    const e = await res.json().catch(() => ({ detail: 'Failed' }));
                    showToast(e.detail || 'Failed', 'error');
                    if ((e.detail || '').toLowerCase().includes('current')) {
                        document.getElementById('current-pwd-input').select();
                    }
                }
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
            } finally {
                btn.disabled = false; btn.textContent = 'Save';
            }
        }
