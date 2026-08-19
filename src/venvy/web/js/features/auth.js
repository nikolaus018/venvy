/**
 * Sign-in, sign-out, the TOTP challenge, the account menu and password changes.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { showToast } from "../core/dom.js";
import { loadApps } from "./apps.js";
import { initAceEditor } from "./editor.js";
import { _loginTotpPending, startPolling } from "./polling.js";
import { closeModal, openModal } from "../ui/modals.js";
import { _featherReplace } from "../core/dom.js";

export let _appInitialized = false;

export async function checkAuth() {
    try {
        const res = await fetch(`${API}/auth/status`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.authenticated) {
            // Hide auth screen, show app — no reload needed
            document.getElementById('auth-screen').classList.add('hidden');
            document.getElementById('app-shell').classList.remove('hidden');
            document.getElementById('current-user').innerText = data.username;
            document.getElementById('user-avatar').innerText = (data.username || 'U')[0].toUpperCase();
            state.currentUser = data;
            if (data.role === 'admin') document.getElementById('admin-users-btn').classList.remove('hidden');
            applyRoleUiGating(data.role);
            if (!_appInitialized) {
                _appInitialized = true;
                initAceEditor();
                loadApps();
            }
            startPolling();
        } else {
            // Not authenticated — show login
            if (state.pollInterval) { clearInterval(state.pollInterval); state.pollInterval = null; }
            _appInitialized = false;
            document.getElementById('app-shell').classList.add('hidden');
            document.getElementById('auth-screen').classList.remove('hidden');
            if (!data.user_exists) {
                document.getElementById('setup-hint').classList.remove('hidden');
                document.getElementById('login-form').dataset.mode = 'setup';
            }
        }
    } catch {
        // Network error — don't log out, just leave current state
    }
}

export function applyRoleUiGating(role) {
    const isViewer = role === 'viewer';
    document.body.classList.toggle('role-viewer', isViewer);
}

export function _showReAuthOverlay() {
    // If the full auth screen is already showing, nothing to do
    if (!document.getElementById('auth-screen').classList.contains('hidden')) return;
    // Reset login form to clean state
    _loginTotpPending = false;
    const step1 = document.getElementById('login-step-password');
    const step2 = document.getElementById('login-step-totp');
    if (step1) step1.classList.remove('hidden');
    if (step2) step2.classList.add('hidden');
    const submitBtn = document.getElementById('login-submit-btn');
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Sign In'; }
    document.getElementById('app-shell').classList.add('hidden');
    document.getElementById('auth-screen').classList.remove('hidden');
    _appInitialized = false;
    state.currentUser = null;
}

export let _loginCooldownTimer = null;

export function _startLoginCooldown(seconds, submitBtn) {
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
            submitBtn.disabled = true; submitBtn.textContent = 'Creating…';
            const res = await fetch(API + '/auth/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }) });
            if (res.ok) { await checkAuth(); }
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
            if (res.ok) { await checkAuth(); }
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
    } catch {
        showToast('Connection error. Check your network.', 'error');
        submitBtn.disabled = false; submitBtn.textContent = origText;
    }
};

export async function logout() { await fetch(`${API}/auth/logout`, { method: 'POST' }); window.location.reload(); }

export function cancelTotpStep() {
    _loginTotpPending = false;
    document.getElementById('login-step-password').classList.remove('hidden');
    document.getElementById('login-step-totp').classList.add('hidden');
    document.getElementById('login-submit-btn').textContent = 'Sign In';
    document.getElementById('totp-code').value = '';
    document.getElementById('username').focus();
}

export function openAccountMenu() {
    const uname = state.currentUser?.username || 'User';
    document.getElementById('acct-menu-username').textContent = uname;
    document.getElementById('acct-menu-avatar').textContent = uname.charAt(0).toUpperCase();
    // Update TOTP badge
    const totpEnabled = state.currentUser?.totp_enabled;
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

export async function openTotpSetup() {
    const totpEnabled = state.currentUser?.totp_enabled;
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

export function copyTotpSecret() {
    const secret = document.getElementById('totp-secret-display').textContent;
    if (!secret) return;
    navigator.clipboard.writeText(secret).then(() => showToast('Secret copied.', 'success')).catch(() => { });
}

export async function confirmTotpSetup() {
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
        showToast('2FA enabled — protect your backup codes!', 'success', 4000);
        // Update local state
        if (state.currentUser) state.currentUser.totp_enabled = true;
    } catch (e) {
        errEl.textContent = e.message;
        errEl.classList.remove('hidden');
        document.getElementById('totp-confirm-code').select();
    } finally {
        btn.disabled = false; btn.textContent = 'Verify & Enable';
    }
}

export async function disableTotp() {
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
        if (state.currentUser) state.currentUser.totp_enabled = false;
    } catch (e) {
        errEl.textContent = e.message;
        errEl.classList.remove('hidden');
        document.getElementById('totp-disable-pwd').select();
    } finally {
        btn.disabled = false; btn.textContent = 'Disable 2FA';
    }
}

export function openChangePwdModal() {
    ['current-pwd-input', 'new-pwd-input', 'confirm-pwd-input'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    document.getElementById('pwd-match-hint').classList.add('hidden');
    openModal('change-pwd-modal');
    setTimeout(() => { const el = document.getElementById('current-pwd-input'); if (el) el.focus(); }, 80);
}

export async function changePassword() {
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
    } finally {
        btn.disabled = false; btn.textContent = 'Save';
    }
}
