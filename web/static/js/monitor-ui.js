// Per-app monitoring in the UI: the health pill, the usage charts, disk usage, the Python
// environment rebuild, and duplicating an app.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        function updateHealthPill(app) {
            const el = $id('dash-health');
            const h = app && app.status === 'running' ? app.health : null;
            if (el) {
                el.classList.toggle('hidden', !h);
                if (h) {
                    el.innerHTML = statusPill(h.status);
                    el.title = h.status === 'unhealthy'
                        ? `Health check failing: ${h.error || ''} (${h.failures} in a row)`
                        : h.checked_at ? `Health check ${h.code ?? ''} · ${fmtAgo(h.checked_at)}` : 'Waiting for the first check';
                }
            }
            const sp = $id('health-pill');
            if (sp) sp.innerHTML = h ? statusPill(h.status) : '';
        }

        let _usageApp = null;
        async function loadUsage() {
            const appId = activeAppId;
            if (!appId || !$id('usage-cpu')) return;
            _usageApp = appId;
            const minutes = parseInt($id('usage-range').value, 10) || 60;
            const [h, d] = await Promise.all([
                apiJson('GET', `/apps/${appId}/metrics/history?minutes=${minutes}`),
                apiJson('GET', `/apps/${appId}/disk`),
            ]);
            if (appId !== activeAppId || _usageApp !== appId) return;
            const pts = h.ok ? h.data.samples : [];
            $id('usage-cpu').innerHTML = chartSvg(pts, 'cpu', { color: 'var(--blue, #60a5fa)', fmt: v => `${v.toFixed(1)}%` });
            $id('usage-mem').innerHTML = chartSvg(pts, 'mem', { color: 'var(--green)', fmt: v => fmtBytes(v) });
            if (d.ok) {
                const limit = d.data.limit_mb ? ` of ${d.data.limit_mb} MB` : '';
                $id('usage-disk').textContent = d.data.bytes != null ? `${fmtBytes(d.data.bytes)}${limit}` : '—';
            }
        }

        // Python card: the version picker and its rebuild button.
        function renderSettingsExtras(app) {
            const card = $id('pyenv-card');
            if (!card) return;
            const isPy = (app.runtime || 'python') === 'python';
            card.classList.toggle('hidden', !isPy);
            if (!isPy) return;
            const meta = typeof runtimeMeta === 'function' ? runtimeMeta('python') : null;
            const versions = meta?.versions || [];
            const sel = $id('setting-pyver');
            sel.innerHTML = [['', 'System default']].concat(versions.map(v => [v, `Python ${v}`]))
                .map(([v, l]) => `<option value="${escapeHtml(v)}">${escapeHtml(l)}</option>`).join('');
            sel.value = app.runtime_version || '';
            if (sel.value !== (app.runtime_version || '')) sel.value = '';
        }

        async function rebuildVenv() {
            const appId = activeAppId;
            if (activeApp && ['running', 'starting'].includes(activeApp.status)) { showToast('Stop the app first.', 'warn'); return; }
            const ok = await confirmDialog({ title: 'Rebuild the virtual environment?', body: 'The current one is deleted and requirements.txt is installed again. Packages that were installed by hand are lost.', confirmLabel: 'Rebuild', danger: true });
            if (!ok) return;
            const btn = $id('pyenv-btn'); btn.disabled = true; btn.textContent = 'Rebuilding…';
            try {
                const res = await fetch(`${API}/apps/${appId}/venv/rebuild`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version: $id('setting-pyver').value }) });
                await waitForJob(res);
                showToast('Environment rebuilt', 'success');
                loadApps();
            } catch (e) { showToast('Rebuild failed: ' + e.message, 'error'); }
            finally { btn.disabled = false; btn.textContent = 'Rebuild'; }
        }

        async function duplicateApp() {
            if (!activeApp) return;
            const body = buildModal('dup-modal', 'Duplicate application', { width: 'max-w-md', zIndex: 85 });
            body.innerHTML = `<div class="space-y-4">
                ${fieldHtml('dup-name', 'Name of the copy', { value: `${activeApp.name} copy` })}
                <p class="text-xs" style="color:var(--text3)">Copies the files, settings and variables, and builds a fresh environment (this can take a minute).</p>
                <div class="flex gap-3"><button type="button" class="btn-ghost flex-1 py-2.5 text-sm font-medium rounded-xl" data-on-click="closeModal('dup-modal')">Cancel</button>
                <button type="button" id="dup-go" class="btn-primary flex-1 py-2.5 text-sm rounded-xl">Duplicate</button></div></div>`;
            $id('dup-go').addEventListener('click', async () => {
                const name = _val('dup-name');
                if (!name) { showToast('Give the copy a name.', 'warn'); return; }
                const btn = $id('dup-go'); btn.disabled = true; btn.textContent = 'Copying…';
                try {
                    const res = await fetch(`${API}/apps/${activeAppId}/duplicate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
                    const created = await waitForJob(res);
                    closeModal('dup-modal');
                    showToast(`Created "${created.name}"`, 'success');
                    await loadApps();
                    if (created?.id) selectApp(created.id);
                } catch (e) { showToast('Could not duplicate: ' + e.message, 'error'); btn.disabled = false; btn.textContent = 'Duplicate'; }
            });
            openModal('dup-modal');
        }
