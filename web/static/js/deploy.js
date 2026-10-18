// Deploy tab: git settings, deploy now with a live log, push webhook, releases and rollback.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        let _depPoll = null;
        let _depApp = null;          // the app the tab currently shows
        let _depData = null;

        function stopDeployPolling() {
            if (_depPoll) { clearInterval(_depPoll); _depPoll = null; }
        }

        async function loadDeploy(opts = {}) {
            const appId = activeAppId;
            if (!appId) return;
            _depApp = appId;
            const r = await apiJson('GET', `/apps/${appId}/deploy`);
            if (appId !== activeAppId) return;
            if (!r.ok) { showToast(r.error, 'error'); return; }
            _depData = r.data;
            renderDeploy(r.data, opts);
            stopDeployPolling();
            if (r.data.state.running && currentTab === 'deploy') {
                _depPoll = setInterval(async () => {
                    if (_depApp !== activeAppId || currentTab !== 'deploy') { stopDeployPolling(); return; }
                    const p = await apiJson('GET', `/apps/${_depApp}/deploy`);
                    if (!p.ok) return;
                    _depData = p.data;
                    renderDeployRun(p.data);
                    if (!p.data.state.running) { stopDeployPolling(); renderDeploy(p.data, { keepForm: true }); loadApps(); }
                }, 1000);
            }
        }

        function renderDeploy(d, opts = {}) {
            const g = d.git;
            if (!opts.keepForm) {
                $id('git-url').value = g.url;
                $id('git-branch').value = g.branch;
                $id('git-build').value = g.build_command;
                $id('git-token').value = '';
            }
            $id('git-token').placeholder = g.has_token ? 'Saved — type to replace' : 'Paste a read-only personal access token';
            $id('git-token-clear').classList.toggle('hidden', !g.has_token);
            $id('git-token-row').classList.toggle('hidden', !!g.ssh);
            $id('git-key-box').classList.toggle('hidden', !g.ssh);
            const pub = $id('git-key-pub');
            pub.textContent = g.public_key || '';
            pub.classList.toggle('hidden', !g.public_key);
            $id('git-key-copy').classList.toggle('hidden', !g.public_key);
            $id('git-key-btn').textContent = g.public_key ? 'Generate a new key' : 'Generate key';
            $id('git-auto').checked = !!g.auto_deploy;
            $id('hook-box').classList.toggle('hidden', !g.auto_deploy);
            $id('hook-url').value = window.location.origin + g.webhook_path;
            $id('hook-secret').value = g.webhook_secret || '';
            renderDeployRun(d);
            renderReleases(d);
        }

        function renderDeployRun(d) {
            const st = d.state, last = d.last;
            const status = st.running ? 'running' : (st.status || last.status);
            $id('dep-status-pill').innerHTML = status ? statusPill(status) : '';
            const bits = [];
            if (last.at) bits.push(`Last deploy ${fmtAgo(last.at)}`);
            if (last.commit) bits.push(`commit <span class="mono">${escapeHtml(last.commit.slice(0, 10))}</span>`);
            $id('dep-last').innerHTML = bits.length ? bits.join(' · ') : 'Nothing deployed yet.';
            const running = st.running;
            const btn = $id('dep-btn'), startBtn = $id('dep-btn-start');
            btn.disabled = running; startBtn.disabled = running;
            btn.textContent = running ? 'Deploying…' : (d.git.url || d.git.build_command ? 'Deploy now' : 'Deploy now');
            const stopped = activeApp && !['running', 'starting'].includes(activeApp.status);
            startBtn.classList.toggle('hidden', !stopped);
            const logEl = $id('dep-log');
            const lines = st.log || [];
            logEl.classList.toggle('hidden', lines.length === 0);
            if (lines.length) {
                const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 24;
                logEl.textContent = lines.join('\n');
                if (running || atBottom) logEl.scrollTop = logEl.scrollHeight;
            }
        }

        function renderReleases(d) {
            const rel = d.releases || [];
            const gitApp = !!d.git.url;
            $id('rel-help').textContent = gitApp
                ? 'Each deploy is a git commit you can return to. Data files in the app folder are not touched.'
                : 'Snapshots of the app\'s files (not its virtualenv or dependencies). Going back restores those files — including data stored in the app folder.';
            const kinds = { deploy: 'Deployed', 'before-deploy': 'Before a deploy', manual: 'Snapshot' };
            $id('rel-list').innerHTML = rel.length ? rel.map(r => `
                <div class="px-5 py-3 flex items-center gap-3">
                    <div class="flex-1 min-w-0">
                        <p class="text-sm font-medium truncate" style="color:var(--text)">${escapeHtml(kinds[r.kind] || r.kind)}${r.commit ? ` <span class="mono text-xs" style="color:var(--text3)">${escapeHtml(r.commit.slice(0, 10))}</span>` : ''}
                            ${r.message ? `<span class="font-normal" style="color:var(--text2)"> — ${escapeHtml(r.message)}</span>` : ''}</p>
                        <p class="text-xs mt-0.5" style="color:var(--text3)">${escapeHtml(fmtWhen(r.created_at))}${r.by ? ' · ' + escapeHtml(r.by) : ''}${r.size ? ' · ' + fmtBytes(r.size) : ''}</p>
                    </div>
                    ${r.restorable ? `<button data-rel="${r.id}" data-requires-operator class="rel-rollback btn-ghost px-3 py-1.5 text-xs shrink-0"${d.state.running ? ' disabled' : ''}>Roll back</button>` : ''}
                </div>`).join('') : '<p class="px-5 py-6 text-xs text-center" style="color:var(--text3)">No releases yet — one is recorded with every deploy.</p>';
            document.querySelectorAll('#rel-list .rel-rollback').forEach(b => b.addEventListener('click', () => rollbackTo(Number(b.dataset.rel))));
        }

        async function saveGit() {
            const appId = activeAppId;
            const btn = $id('git-save-btn');
            const body = { git_url: _val('git-url'), git_branch: _val('git-branch') || 'main', build_command: _val('git-build') };
            const tok = $id('git-token').value.trim();
            if (tok) body.token = tok;
            btn.disabled = true;
            const r = await apiJson('PUT', `/apps/${appId}/git`, body);
            btn.disabled = false;
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Saved', 'success', 1500);
            loadDeploy();
            loadApps();
        }

        async function clearGitToken() {
            const r = await apiJson('PUT', `/apps/${activeAppId}/git`, { token: '' });
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Token removed', 'info', 1500);
            loadDeploy({ keepForm: true });
        }

        async function makeDeployKey() {
            if (_depData?.git.public_key) {
                const ok = await confirmDialog({ title: 'Replace the deploy key?', body: 'The old key stops working — add the new one to the repository afterwards.', confirmLabel: 'Generate', danger: true });
                if (!ok) return;
            }
            const r = await apiJson('POST', `/apps/${activeAppId}/git/key`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Key generated — add it to the repository', 'success');
            loadDeploy({ keepForm: true });
        }

        async function toggleAutoDeploy(on) {
            const r = await apiJson('PUT', `/apps/${activeAppId}/git`, { auto_deploy: on });
            if (!r.ok) { showToast(r.error, 'error'); $id('git-auto').checked = !on; return; }
            loadDeploy({ keepForm: true });
        }

        async function regenWebhookSecret() {
            const ok = await confirmDialog({ title: 'Make a new webhook secret?', body: 'The old secret stops working at once — update it at the git host.', confirmLabel: 'New secret', danger: true });
            if (!ok) return;
            const r = await apiJson('POST', `/apps/${activeAppId}/git/webhook-secret`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            loadDeploy({ keepForm: true });
        }

        async function deployNow(start) {
            const g = _depData?.git;
            if (!g || (!g.url && !g.build_command)) { showToast('Set a repository or a build command first, and save.', 'warn'); return; }
            const r = await apiJson('POST', `/apps/${activeAppId}/deploy`, { start: !!start });
            if (!r.ok) { showToast(r.error, 'error'); return; }
            loadDeploy({ keepForm: true });
        }

        async function rollbackTo(id) {
            const rel = (_depData?.releases || []).find(x => x.id === id);
            const ok = await confirmDialog({
                title: 'Roll back?',
                body: 'The code goes back to this release, dependencies and the build are redone, and the app restarts.' + (rel && !rel.commit ? ' This replaces the app\'s files with the snapshot.' : ''),
                confirmLabel: 'Roll back', danger: true,
            });
            if (!ok) return;
            const r = await apiJson('POST', `/apps/${activeAppId}/releases/${id}/rollback`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            loadDeploy({ keepForm: true });
        }

        async function snapshotNow() {
            const res = await fetch(`${API}/apps/${activeAppId}/releases`, { method: 'POST' });
            try { await waitForJob(res); showToast('Snapshot taken', 'success'); }
            catch (e) { showToast(e.message, 'error'); }
            loadDeploy({ keepForm: true });
        }
