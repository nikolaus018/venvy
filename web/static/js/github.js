// GitHub sign-in (device flow) and the per-app Git tab.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── GitHub (system-level sign-in) ────────────────────────────────────────
        let _ghConnected = false;
        let _ghDevicePollTimer = null;
        let _ghDeviceFlowId = null;

        async function loadGhCardStatus() {
            try {
                const res = await fetch(`${API}/system/github/status`);
                const data = await res.json();
                _ghConnected = !!data.connected;
                const signedOut = document.getElementById('gh-signed-out');
                const signedIn = document.getElementById('gh-signed-in');
                const badge = document.getElementById('gh-connected-badge');
                if (data.connected) {
                    signedOut.classList.add('hidden');
                    signedIn.classList.remove('hidden');
                    document.getElementById('gh-username-big').textContent = data.username || '';
                    document.getElementById('gh-username-label').textContent = data.username || '';
                    if (data.avatar_url) {
                        document.getElementById('gh-avatar-big').src = data.avatar_url;
                        const av = document.getElementById('gh-avatar');
                        av.src = data.avatar_url; av.classList.remove('hidden');
                    }
                    badge.classList.remove('hidden'); badge.classList.add('flex');
                } else {
                    signedOut.classList.remove('hidden');
                    signedIn.classList.add('hidden');
                    badge.classList.add('hidden'); badge.classList.remove('flex');
                    if (data.client_id_set) document.getElementById('gh-client-id').placeholder = 'Saved — paste to replace';
                }
            } catch (e) { }
        }

        async function saveGhClientId() {
            const input = document.getElementById('gh-client-id');
            const clientId = input.value.trim();
            if (!clientId) { showToast('Enter a Client ID first.', 'warn'); return; }
            const btn = document.getElementById('gh-client-id-save-btn');
            btn.disabled = true; btn.textContent = 'Saving…';
            try {
                const res = await fetch(`${API}/system/github/client-id`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ client_id: clientId }),
                });
                if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
                input.value = '';
                showToast('Client ID saved.', 'success');
                await loadGhCardStatus();
            } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
            finally { btn.disabled = false; btn.textContent = 'Save'; }
        }

        async function startGhDeviceFlow() {
            const btn = document.getElementById('gh-connect-btn');
            btn.disabled = true;
            try {
                const res = await fetch(`${API}/system/github/device/start`, { method: 'POST' });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                _ghDeviceFlowId = data.flow_id;
                document.getElementById('gh-device-code').textContent = data.user_code;
                document.getElementById('gh-device-uri').href = data.verification_uri;
                document.getElementById('gh-device-uri').textContent = data.verification_uri.replace(/^https?:\/\//, '');
                document.getElementById('gh-device-panel').classList.remove('hidden');
                window.open(data.verification_uri, '_blank');
                _ghPollDevice(Math.max(data.interval || 5, 5) * 1000);
            } catch (e) {
                showToast('Could not start GitHub sign-in: ' + e.message, 'error', 6000);
            } finally { btn.disabled = false; }
        }

        function _ghPollDevice(intervalMs) {
            clearTimeout(_ghDevicePollTimer);
            if (!_ghDeviceFlowId) return;
            _ghDevicePollTimer = setTimeout(async () => {
                try {
                    const res = await fetch(`${API}/system/github/device/poll`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ flow_id: _ghDeviceFlowId }),
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                    if (data.status === 'complete') {
                        _ghDeviceFlowId = null;
                        document.getElementById('gh-device-panel').classList.add('hidden');
                        showToast(`Signed in to GitHub as ${data.username}`, 'success');
                        await loadGhCardStatus();
                        return;
                    }
                    _ghPollDevice((data.interval || 5) * 1000);
                } catch (e) {
                    _ghDeviceFlowId = null;
                    document.getElementById('gh-device-panel').classList.add('hidden');
                    showToast('GitHub sign-in failed: ' + e.message, 'error', 6000);
                }
            }, intervalMs);
        }

        function cancelGhDeviceFlow() {
            _ghDeviceFlowId = null;
            clearTimeout(_ghDevicePollTimer);
            document.getElementById('gh-device-panel').classList.add('hidden');
        }

        function copyGhDeviceCode() {
            const code = document.getElementById('gh-device-code').textContent;
            navigator.clipboard.writeText(code).catch(() => { });
            showToast('Code copied!', 'success', 1500);
        }

        async function disconnectGh() {
            const ok = await confirmDialog({
                title: 'Sign out of GitHub?',
                body: 'Apps already cloned keep working, but private repos won\'t fetch/push until you sign in again.',
                confirmLabel: 'Sign out', danger: true,
            });
            if (!ok) return;
            try {
                const res = await fetch(`${API}/system/github/disconnect`, { method: 'POST' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                showToast('Signed out of GitHub.', 'success');
                await loadGhCardStatus();
            } catch (e) { showToast('Sign-out failed: ' + e.message, 'error'); }
        }

        // ── Git (per-app) ─────────────────────────────────────────────────────────
        let _ghRepoCache = null; // {ts, repos} — shared across apps for the session
        async function loadGitRepoPicker() {
            const picker = document.getElementById('git-repo-picker');
            try {
                const statusRes = await fetch(`${API}/system/github/status`);
                const status = await statusRes.json();
                _ghConnected = !!status.connected;
            } catch (e) { }
            document.getElementById('git-gh-hint').classList.toggle('hidden', _ghConnected);
            if (!_ghConnected) { picker.classList.add('hidden'); return; }
            picker.classList.remove('hidden');
            const select = document.getElementById('git-repo-select');
            try {
                if (!_ghRepoCache || Date.now() - _ghRepoCache.ts > 60000) {
                    const res = await fetch(`${API}/system/github/repos`);
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                    _ghRepoCache = { ts: Date.now(), repos: data.repos || [] };
                }
                const repos = _ghRepoCache.repos;
                select.innerHTML = '<option value="">Choose a repository…</option>' + repos.map(r =>
                    `<option value="${escapeHtml(r.clone_url)}" data-branch="${escapeHtml(r.default_branch)}">${escapeHtml(r.full_name)}${r.private ? ' 🔒' : ''}</option>`
                ).join('');
            } catch (e) {
                select.innerHTML = '<option value="">Could not load repos</option>';
            }
        }
        function onGitRepoPicked() {
            const select = document.getElementById('git-repo-select');
            const opt = select.options[select.selectedIndex];
            if (!opt || !opt.value) return;
            document.getElementById('git-clone-url').value = opt.value;
        }

        async function loadGitStatus() {
            if (!activeAppId) return;
            loadGitRepoPicker();
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git/status`);
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                document.getElementById('git-clone-card').classList.toggle('hidden', data.is_repo);
                document.getElementById('git-repo-card').classList.toggle('hidden', !data.is_repo);
                if (!data.is_repo) return;

                document.getElementById('git-branch-name').textContent = data.branch || '(detached)';
                document.getElementById('git-remote-url').textContent = data.remote_url || 'no remote';
                document.getElementById('git-last-commit').textContent = data.last_commit || '';

                const aheadPill = document.getElementById('git-ahead-pill');
                const behindPill = document.getElementById('git-behind-pill');
                const dirtyPill = document.getElementById('git-dirty-pill');
                const cleanPill = document.getElementById('git-clean-pill');
                aheadPill.classList.toggle('hidden', !data.ahead);
                if (data.ahead) aheadPill.textContent = `↑ ${data.ahead} ahead`;
                behindPill.classList.toggle('hidden', !data.behind);
                if (data.behind) behindPill.textContent = `↓ ${data.behind} behind`;
                dirtyPill.classList.toggle('hidden', !data.dirty_count);
                if (data.dirty_count) dirtyPill.textContent = `${data.dirty_count} uncommitted`;
                cleanPill.classList.toggle('hidden', !!(data.ahead || data.behind || data.dirty_count));
            } catch (e) {
                showToast('Could not load git status: ' + e.message, 'error');
            }
        }

        async function gitClone() {
            const url = document.getElementById('git-clone-url').value.trim();
            if (!url) { showToast('Enter a repository URL.', 'warn'); return; }
            const btn = document.getElementById('git-clone-btn');
            btn.disabled = true; btn.textContent = 'Cloning…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git/clone`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                await waitForJob(res);
                showToast('Repository connected.', 'success');
                await loadGitStatus();
            } catch (e) { showToast('Clone failed: ' + e.message, 'error', 6000); }
            finally { btn.disabled = false; btn.textContent = 'Clone into this app'; }
        }

        async function gitFetch() {
            const btn = document.getElementById('git-fetch-btn');
            btn.disabled = true; btn.textContent = 'Fetching…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git/fetch`, { method: 'POST' });
                await waitForJob(res);
                showToast('Fetched.', 'success');
                await loadGitStatus();
            } catch (e) { showToast('Fetch failed: ' + e.message, 'error', 6000); }
            finally { btn.disabled = false; btn.textContent = 'Fetch'; }
        }

        async function gitPull() {
            const btn = document.getElementById('git-pull-btn');
            btn.disabled = true; btn.textContent = 'Pulling…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git/pull`, { method: 'POST' });
                await waitForJob(res);
                showToast('Pulled latest changes.', 'success');
                await loadGitStatus();
            } catch (e) { showToast('Pull failed: ' + e.message, 'error', 6000); }
            finally { btn.disabled = false; btn.textContent = 'Pull'; }
        }

        async function gitPush() {
            const btn = document.getElementById('git-push-btn');
            btn.disabled = true; btn.textContent = 'Pushing…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git/push`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force: false }),
                });
                await waitForJob(res);
                showToast('Pushed.', 'success');
                await loadGitStatus();
            } catch (e) { showToast('Push failed: ' + e.message, 'error', 6000); }
            finally { btn.disabled = false; btn.textContent = 'Push'; }
        }

        async function gitCommit() {
            const input = document.getElementById('git-commit-msg');
            const message = input.value.trim();
            if (!message) { showToast('Enter a commit message.', 'warn'); return; }
            const btn = document.getElementById('git-commit-btn');
            btn.disabled = true; btn.textContent = 'Committing…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git/commit`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message }),
                });
                await waitForJob(res);
                input.value = '';
                showToast('Committed.', 'success');
                await loadGitStatus();
            } catch (e) { showToast('Commit failed: ' + e.message, 'error', 6000); }
            finally { btn.disabled = false; btn.textContent = 'Commit all changes'; }
        }

        async function gitDisconnect() {
            const ok = await confirmDialog({
                title: 'Disconnect this repository?',
                body: 'The .git folder is removed but your files stay.',
                confirmLabel: 'Disconnect', danger: true,
            });
            if (!ok) return;
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/git`, { method: 'DELETE' });
                if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
                showToast('Repository disconnected.', 'success');
                await loadGitStatus();
            } catch (e) { showToast('Disconnect failed: ' + e.message, 'error'); }
        }
