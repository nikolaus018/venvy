// Settings tab: resource-limit sliders, save and dirty tracking, export as zip, delete app.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Settings ──────────────────────────────────────────────────────────────
        // ── Resource-limit sliders ───────────────────────────────────────────────
        // The backend stores memory as MB and CPU as a systemd-style percent
        // (100 = one core) — both sliders keep that as their underlying value
        // (so saveSettings/checkSettingsDirty don't need to know about units),
        // and only the label above each slider is translated into GB/cores for
        // display. Bounds default to a generic small-VPS guess until the real
        // host capacity loads (fetched once at boot; 403s for non-admins, so
        // this degrades to the generic guess rather than failing loudly).
        let _hostCapacity = { memMB: null, cores: null };

        async function _loadHostCapacity() {
            if (currentUser?.role !== 'admin') return;   // admin-only endpoint: keep the generic bounds
            try {
                const res = await fetch(`${API}/system/metrics`);
                if (!res.ok) return; // non-admin (403) or psutil missing — keep the generic fallback bounds
                const data = await res.json();
                if (data.memory?.total) _hostCapacity.memMB = Math.min(1048576, Math.floor(data.memory.total / (1024 * 1024)));
                if (data.cpu?.cores) _hostCapacity.cores = Math.min(100, data.cpu.cores);
                _applyHostCapacityToSliders();
            } catch (e) { /* offline at boot — keep the generic fallback bounds */ }
        }

        function _applyHostCapacityToSliders() {
            const memEl = document.getElementById('setting-memory-limit');
            const cpuEl = document.getElementById('setting-cpu-limit');
            const memHint = document.getElementById('setting-memory-limit-hint');
            const cpuHint = document.getElementById('setting-cpu-limit-hint');
            if (memEl && _hostCapacity.memMB) {
                memEl.max = Math.max(_hostCapacity.memMB, parseInt(memEl.value, 10) || 0);
                if (memHint) memHint.textContent = ` (${(_hostCapacity.memMB / 1024).toFixed(1)} GB)`;
            }
            if (cpuEl && _hostCapacity.cores) {
                cpuEl.max = Math.max(_hostCapacity.cores * 100, parseInt(cpuEl.value, 10) || 0);
                if (cpuHint) cpuHint.textContent = ` (${_hostCapacity.cores} core${_hostCapacity.cores === 1 ? '' : 's'})`;
            }
            _updateResourceLimitLabels();
        }

        function _formatMemoryLimitLabel(mb) {
            if (mb <= 0) return 'Unlimited'; // shouldn't happen with the toggle off-loading "unlimited" — kept defensively
            if (mb >= 1024) return `${(mb % 1024 === 0 ? mb / 1024 : (mb / 1024).toFixed(1))} GB`;
            return `${mb} MB`;
        }

        function _formatCpuLimitLabel(percent) {
            if (percent <= 0) return 'Unlimited'; // same defensive note as above
            const cores = percent / 100;
            return `${cores % 1 === 0 ? cores.toFixed(0) : cores.toFixed(2)} core${cores === 1 ? '' : 's'}`;
        }

        function _updateResourceLimitLabels() {
            const memEl = document.getElementById('setting-memory-limit');
            const cpuEl = document.getElementById('setting-cpu-limit');
            const memLabel = document.getElementById('setting-memory-limit-label');
            const cpuLabel = document.getElementById('setting-cpu-limit-label');
            if (memEl && memLabel) memLabel.textContent = _formatMemoryLimitLabel(parseInt(memEl.value, 10) || 0);
            if (cpuEl && cpuLabel) cpuLabel.textContent = _formatCpuLimitLabel(parseInt(cpuEl.value, 10) || 0);
        }

        // Sets a slider's value, widening `max` first if the stored value
        // exceeds it — otherwise the browser silently clamps the displayed
        // (and later re-saved) value down to `max`, quietly shrinking a limit
        // the user actually set before host capacity was known.
        function _setSliderValue(el, value) {
            if (!el) return;
            const v = Number(value) || 0;
            if (v > parseInt(el.max, 10)) el.max = v;
            el.value = v;
        }

        // Whether the "Limit Resources" toggle is on. The sliders below it
        // only ever hold a real value (min 16 MB / 0.25 core) — "unlimited"
        // is this toggle's off state, not a slider position, so 0 is never
        // sent while it's on and always sent while it's off, regardless of
        // whatever the (hidden) sliders happen to be showing.
        function isResourceLimitsEnabled() {
            return document.getElementById('setting-limits-toggle')?.getAttribute('aria-checked') === 'true';
        }

        function _setResourceLimitsToggle(enabled) {
            const toggle = document.getElementById('setting-limits-toggle');
            const wrap = document.getElementById('resource-limit-sliders-wrap');
            if (!toggle || !wrap) return;
            toggle.setAttribute('aria-checked', String(enabled));
            toggle.classList.toggle('on', enabled);
            wrap.classList.toggle('hidden', !enabled);
        }

        function toggleResourceLimits() {
            const nowOn = !isResourceLimitsEnabled();
            _setResourceLimitsToggle(nowOn);
            if (nowOn) {
                // Switching on fresh (both sliders still sitting at 0 from a
                // toggled-off state) — jump to a sensible starting point
                // rather than leaving them below their own min.
                const memEl = document.getElementById('setting-memory-limit');
                const cpuEl = document.getElementById('setting-cpu-limit');
                if (!(parseInt(memEl.value, 10) > 0)) _setSliderValue(memEl, 512);
                if (!(parseInt(cpuEl.value, 10) > 0)) _setSliderValue(cpuEl, 50);
                _updateResourceLimitLabels();
            }
            checkSettingsDirty();
        }

        // Effective memory/cpu limit given the toggle — the single source of
        // truth saveSettings() and checkSettingsDirty() both read from.
        function _readResourceLimits() {
            if (!isResourceLimitsEnabled()) return { memoryLimit: 0, cpuLimit: 0 };
            return {
                memoryLimit: parseInt(document.getElementById('setting-memory-limit').value, 10) || 0,
                cpuLimit: parseInt(document.getElementById('setting-cpu-limit').value, 10) || 0,
            };
        }

        // ── Auto-save settings ───────────────────────────────────────────────────
        // All settings fields save automatically:
        //   restart_policy, start_on_boot → onChange (instant)
        //   entrypoint → onBlur (after typing)
        //   port       → onBlur (after typing, or on Clear)
        //   env_vars   → onBlur (JSON parsed first)
        // A subtle indicator shows saving/saved state.

        let _autoSaveTimer = null;

        function _showAutosave(state) {
            // state: 'saving' | 'saved' | 'error'
            const row = document.getElementById('autosave-indicator');
            const spinner = document.getElementById('autosave-spinner');
            const check = document.getElementById('autosave-check');
            const text = document.getElementById('autosave-text');
            if (!row) return;
            row.classList.remove('hidden');
            spinner.classList.toggle('hidden', state !== 'saving');
            check.classList.toggle('hidden', state !== 'saved');
            if (state === 'saving') { text.textContent = 'Saving…'; text.style.color = 'var(--text3)'; }
            else if (state === 'saved') { text.textContent = 'Saved'; text.style.color = '#34d399'; }
            else { text.textContent = 'Save failed'; text.style.color = 'var(--red)'; }
            if (state === 'saved') {
                if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
                _autoSaveTimer = setTimeout(() => row.classList.add('hidden'), 2500);
            }
        }

        // ── Runtimes ──────────────────────────────────────────────────────────────
        // What each runtime needs and how its entrypoint behaves comes from the
        // server (GET /api/runtimes), so a new runtime is one entry there.
        let RUNTIMES = [];

        function runtimeMeta(key) {
            return RUNTIMES.find(r => r.key === key) || null;
        }

        async function loadRuntimes() {
            try {
                const res = await fetch(`${API}/runtimes`);
                if (!res.ok) return;
                RUNTIMES = await res.json();
            } catch (e) { return; }   // the selects keep their built-in Python option
            const fill = (sel, forCreate) => {
                if (!sel) return;
                const keep = sel.value;
                sel.replaceChildren(...RUNTIMES.map(r => {
                    const o = document.createElement('option');
                    o.value = r.key;
                    o.textContent = r.label + (r.installed ? '' : ' — not installed');
                    if (forCreate && !r.installed) o.disabled = true;
                    return o;
                }));
                if (RUNTIMES.some(r => r.key === keep)) sel.value = keep;
            };
            fill(document.getElementById('setting-runtime'), false);
            fill(document.querySelector('#create-form select[name="runtime"]'), true);
            onRuntimeChange('create');
        }

        function onRuntimeChange(context) {
            let runtime, entryInp, labelEl, helpEl;
            if (context === 'create') {
                const form = document.getElementById('create-form');
                runtime = form.runtime.value;
                entryInp = form.entrypoint;
                labelEl = document.getElementById('create-entrypoint-label');
                helpEl = document.getElementById('create-entrypoint-hint');
            } else {
                runtime = document.getElementById('setting-runtime').value;
                entryInp = document.getElementById('setting-entrypoint');
                labelEl = document.getElementById('setting-entrypoint-label');
                helpEl = document.getElementById('setting-entrypoint-help');
            }
            const meta = runtimeMeta(runtime);
            if (!meta) return;

            // Switching runtime: a value that was only the previous runtime's
            // suggestion (main.py, main.rs, ...) must not be carried over.
            const stale = RUNTIMES.some(r => r.entry_default && r.entry_default === entryInp.value);
            if (meta.entry === 'fixed') {
                entryInp.value = meta.entry_default;
            } else if (entryInp.value === '' || stale) {
                entryInp.value = context === 'create' ? '' : meta.entry === 'required' ? meta.entry_default : '';
            }
            const locked = meta.entry === 'fixed';
            entryInp.disabled = locked;
            entryInp.style.opacity = locked ? '0.5' : '';
            entryInp.style.cursor = locked ? 'not-allowed' : '';
            entryInp.placeholder = meta.entry_placeholder;
            if (labelEl) labelEl.textContent = meta.entry_label;
            if (helpEl) {
                if (context === 'create') helpEl.textContent = meta.entry === 'fixed' ? '' : meta.entry === 'required' ? `(e.g. ${meta.entry_default})` : '(optional)';
                else helpEl.textContent = meta.entry_help;
            }
        }

        async function saveSettings() {
            if (!activeAppId) return;
            // Pin the app this save is for — if the user switches apps while
            // the PUT is in flight, the response must not overwrite the
            // Settings form (or activeApp) now showing a different app.
            const targetAppId = activeAppId;
            const btn = document.getElementById('settings-save-btn');
            const btnText = document.getElementById('save-btn-text');

            const runtime = document.getElementById('setting-runtime').value;
            const policy = document.getElementById('setting-policy').value;
            const boot = document.getElementById('setting-boot').checked;
            const portsStr = document.getElementById('setting-port').value.trim();
            const manualPorts = portsStr.split(/[\s,]+/).map(p => parseInt(p.trim(), 10)).filter(p => !isNaN(p) && p > 0);
            const entrypoint = document.getElementById('setting-entrypoint').value.trim();
            const name = document.getElementById('setting-name').value.trim();
            const { memoryLimit, cpuLimit } = _readResourceLimits();

            if (portsStr && manualPorts.length === 0) {
                showToast('Invalid ports format. Use comma-separated numbers.', 'warn'); return;
            }
            if (!name) {
                showToast('The application needs a name.', 'warn');
                document.getElementById('setting-name').focus();
                return;
            }
            // The port box also shows a port that was only detected. Sending
            // that back would pin it as a manual port: detection stops, and
            // the app is told to use it (PORT=…) on every later start. Only
            // send the ports when they are really set by hand.
            let storedManualPorts = [];
            try {
                const parsed = JSON.parse(activeApp?.manual_ports || '[]');
                if (Array.isArray(parsed)) storedManualPorts = parsed;
            } catch { }
            const portsUntouched = storedManualPorts.length === 0
                && manualPorts.length === 1 && manualPorts[0] === activeApp?.port;
            if (memoryLimit !== 0 && (memoryLimit < 16 || memoryLimit > 1048576)) {
                showToast('Memory limit must be between 16 and 1,048,576 MB (or 0 for unlimited).', 'warn'); return;
            }
            if (cpuLimit !== 0 && (cpuLimit < 1 || cpuLimit > 10000)) {
                showToast('CPU limit must be between 1 and 10,000% (or 0 for unlimited).', 'warn'); return;
            }

            // Hidden or undecryptable variables are not being edited: leave them
            // out of the request so saving anything else can't wipe them.
            const envState = envEditor.state();
            if (envState.error) {
                showToast(envState.error, 'error');
                envEditor.focusProblem();
                return;
            }
            const envUntouched = envState.untouched;
            const env = envState.vars;

            const body = {
                name: name,
                runtime: runtime,
                restart_policy: policy,
                start_on_boot: boot,
                ...(envUntouched ? {} : { env_vars: env }),
                entrypoint,
                ...(portsUntouched ? {} : { manual_ports: manualPorts }),
                url_source_pref: _getOpenUrlPref(),
                memory_limit_mb: memoryLimit,
                cpu_limit_percent: cpuLimit
            };

            btn.disabled = true;
            btnText.innerText = 'Saving...';
            _showAutosave('saving');

            try {
                const res = await fetch(`${API}/apps/${targetAppId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (!res.ok) {
                    const e = await res.json().catch(() => ({ detail: 'Save failed' }));
                    showToast('Error: ' + (e.detail || 'Save failed'), 'error');
                    if (targetAppId === activeAppId) _showAutosave('error');
                    return;
                }
                const saved = await res.json();

                if (targetAppId !== activeAppId) {
                    // User switched to a different app while this save was in
                    // flight — the save itself succeeded server-side, but
                    // don't paint its response over the app now on screen.
                    // _showAutosave('saving') already ran for this save before
                    // the fetch — that indicator is shared DOM, not per-app, so
                    // without this it would stay stuck on "Saving…" over
                    // whichever app the user is now looking at.
                    document.getElementById('autosave-indicator')?.classList.add('hidden');
                    showToast(`Settings saved for ${saved.name || 'the app'}.`, 'success');
                    return;
                }
                activeApp = saved;

                // Sync UI
                let portsDisplay = activeApp.port ? String(activeApp.port) : '';
                try {
                    const m_ports = JSON.parse(activeApp.manual_ports || '[]');
                    if (Array.isArray(m_ports) && m_ports.length > 0) {
                        portsDisplay = m_ports.join(', ');
                    }
                } catch (e) { }
                document.getElementById('setting-port').value = portsDisplay;
                document.getElementById('setting-name').value = activeApp.name || '';
                document.getElementById('dash-title').innerText = activeApp.name;
                document.title = `${activeApp.name} · Venvy`;
                _cachedApps = _cachedApps.map(a => a.id === activeApp.id ? activeApp : a);
                renderAppList(_cachedApps);
                document.getElementById('setting-entrypoint').value = activeApp.entrypoint || '';
                const savedMemLimit = activeApp.memory_limit_mb ?? 0, savedCpuLimit = activeApp.cpu_limit_percent ?? 0;
                _setResourceLimitsToggle(savedMemLimit > 0 || savedCpuLimit > 0);
                if (savedMemLimit > 0) _setSliderValue(document.getElementById('setting-memory-limit'), savedMemLimit);
                if (savedCpuLimit > 0) _setSliderValue(document.getElementById('setting-cpu-limit'), savedCpuLimit);
                _updateResourceLimitLabels();
                _tempOpenUrlPref = activeApp.url_source_pref || 'local';
                _renderOpenPrefButtons(_tempOpenUrlPref);
                _updateNetworkCard(activeApp);
                checkSettingsDirty();

                _showAutosave('saved');
                checkSettingsDirty();
                loadCfRouteStatus();
                showToast('Settings saved successfully.', 'success');
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
                _showAutosave('error');
            } finally {
                btn.disabled = false;
                btnText.innerText = 'Save Changes';
            }
        }

        function checkSettingsDirty() {
            if (!activeApp) return;
            const runtime = document.getElementById('setting-runtime').value;
            const policy = document.getElementById('setting-policy').value;
            const boot = document.getElementById('setting-boot').checked;
            const portsStr = document.getElementById('setting-port').value.trim();
            const manualPorts = portsStr.split(/[\s,]+/).map(p => parseInt(p.trim(), 10)).filter(p => !isNaN(p) && p > 0);
            const entrypoint = document.getElementById('setting-entrypoint').value.trim();
            const name = document.getElementById('setting-name').value.trim();

            let currentManualPorts = [];
            try {
                const parsed = JSON.parse(activeApp.manual_ports || '[]');
                if (Array.isArray(parsed) && parsed.length > 0) currentManualPorts = parsed;
            } catch { }
            if (currentManualPorts.length === 0 && activeApp.port) {
                currentManualPorts = [activeApp.port];
            }
            const portsMatch = JSON.stringify(manualPorts) === JSON.stringify(currentManualPorts);

            const envMatch = !envEditor.differsFrom(activeApp);

            const fixedEntry = key => { const m = runtimeMeta(key); return m && m.entry === 'fixed' ? m.entry_default : null; };
            const currentEntryPoint = fixedEntry(activeApp.runtime) ?? fixedEntry(runtime) ?? (activeApp.entrypoint || '');
            const normalizedEntry = fixedEntry(runtime) ?? entrypoint;

            const { memoryLimit, cpuLimit } = _readResourceLimits();

            const isDirty = (
                name !== (activeApp.name || '').trim() ||
                runtime !== (activeApp.runtime || 'python') ||
                policy !== activeApp.restart_policy ||
                boot !== activeApp.start_on_boot ||
                !portsMatch ||
                normalizedEntry !== currentEntryPoint ||
                _getOpenUrlPref() !== (activeApp.url_source_pref || 'local') ||
                memoryLimit !== (activeApp.memory_limit_mb ?? 0) ||
                cpuLimit !== (activeApp.cpu_limit_percent ?? 0) ||
                !envMatch
            );

            const dot = document.getElementById('save-status-dot');
            const btn = document.getElementById('settings-save-btn');
            if (isDirty) {
                dot.classList.remove('hidden');
                btn.classList.add('pulse-active');
                btn.classList.remove('btn-primary');
                btn.classList.add('btn-orange');
            } else {
                dot.classList.add('hidden');
                btn.classList.remove('pulse-active');
                btn.classList.add('btn-primary');
                btn.classList.remove('btn-orange');
            }
            // Mark the Settings tab itself so unsaved work is visible from any tab.
            const tabBtn = document.getElementById('tabbtn-settings');
            if (tabBtn) {
                let mark = tabBtn.querySelector('.settings-dirty-dot');
                if (isDirty && !mark) {
                    mark = document.createElement('span');
                    mark.className = 'settings-dirty-dot';
                    mark.setAttribute('aria-hidden', 'true');
                    mark.style.cssText = 'display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--amber);margin-left:5px;vertical-align:middle';
                    tabBtn.appendChild(mark);
                    tabBtn.title = 'Settings (5) — unsaved changes';
                } else if (!isDirty && mark) {
                    mark.remove();
                    tabBtn.title = 'Settings (5)';
                }
            }
        }

        let _settingsWired = false;
        function _wireSettingsDirtyCheck() {
            if (_settingsWired) return;
            _settingsWired = true;
            ['setting-runtime', 'setting-policy', 'setting-boot'].forEach(id => {
                document.getElementById(id)?.addEventListener('change', checkSettingsDirty);
            });
            ['setting-name', 'setting-entrypoint', 'setting-port', 'setting-memory-limit', 'setting-cpu-limit'].forEach(id => {
                document.getElementById(id)?.addEventListener('input', checkSettingsDirty);
            });
        }




        // ── Export App ────────────────────────────────────────────────────────────
        async function _batchedRun(fns, batchSize = 5) {
            const results = [];
            for (let i = 0; i < fns.length; i += batchSize) {
                const batch = fns.slice(i, i + batchSize);
                results.push(...await Promise.all(batch.map(fn => fn())));
            }
            return results;
        }
        async function downloadAppZip() {
            if (!activeAppId) return;
            // Pinned for the whole export: it takes a while, and switching to
            // another app halfway would otherwise finish the archive with
            // that app's files.
            const appId = activeAppId, appName = activeApp?.name || 'app';
            const btn = document.getElementById('btn-download-zip');
            const originalHTML = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = `<div class="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin"></div> Compressing...`;
            try {
                const zip = new JSZip();
                const unreadable = [];
                async function addFolderToZip(folderPath, zipInstance) {
                    const res = await fetch(`${API}/apps/${appId}/files/tree?path=${encodeURIComponent(folderPath)}`);
                    if (!res.ok) throw new Error("Failed to list files");
                    const items = await res.json();
                    const dirs = items.filter(i => i.is_dir), files = items.filter(i => !i.is_dir);
                    for (const dir of dirs) await addFolderToZip(dir.path, zipInstance.folder(dir.name));
                    await _batchedRun(files.map(file => async () => {
                        const fileRes = await fetch(`${API}/apps/${appId}/files/download?path=${encodeURIComponent(file.path)}`);
                        if (fileRes.ok) zipInstance.file(file.name, await fileRes.blob());
                        else unreadable.push(file.path);
                    }), 5);
                }
                await addFolderToZip("", zip);
                const content = await zip.generateAsync({ type: "blob" });
                const url = window.URL.createObjectURL(content);
                const a = document.createElement('a');
                a.href = url; a.download = `${appName.replace(/[\\/:*?"<>|]+/g, '_')}_source.zip`;
                document.body.appendChild(a); a.click();
                // Not straight away: some browsers only start reading the
                // blob after this handler has returned.
                setTimeout(() => { window.URL.revokeObjectURL(url); a.remove(); }, 1000);
                if (unreadable.length) {
                    showToast(`${unreadable.length} file(s) could not be read and are not in the archive: ${unreadable.slice(0, 3).join(', ')}${unreadable.length > 3 ? '…' : ''}`, 'warn', 10000);
                }
            } catch (e) { showToast("Export failed: " + e.message, "error"); }
            finally { btn.disabled = false; btn.innerHTML = originalHTML; _featherReplace(); }
        }

        function deleteApp() {
            if (!activeApp) return;
            document.getElementById('delete-app-name').textContent = activeApp.name;
            document.getElementById('delete-app-confirm-hint').textContent = activeApp.name;
            const input = document.getElementById('delete-app-confirm-input');
            input.value = '';
            input.placeholder = activeApp.name;
            onDeleteAppConfirmInput();
            openModal('delete-app-modal');
        }

        // Deleting an app is irreversible, so the button stays inert until the
        // name is typed out — the same guard GitHub and Vercel use.
        function onDeleteAppConfirmInput() {
            const input = document.getElementById('delete-app-confirm-input');
            const btn = document.getElementById('delete-app-confirm-btn');
            if (!input || !btn) return;
            const match = input.value.trim() === (activeApp?.name || '').trim() && !!activeApp;
            btn.disabled = !match;
            input.style.borderColor = !input.value ? '' : (match ? 'var(--green)' : 'rgba(var(--red-rgb),0.5)');
        }

        async function confirmDeleteApp() {
            const input = document.getElementById('delete-app-confirm-input');
            if (!activeApp || input.value.trim() !== activeApp.name.trim()) {
                showToast('Type the application name exactly to confirm.', 'warn', 3000);
                input.focus();
                return;
            }
            const deletedName = activeApp.name, deletedId = activeAppId;
            closeModal('delete-app-modal');
            // Clean up terminal resources before deleting
            _termDestroy();
            try {
                const res = await fetch(`${API}/apps/${deletedId}`, { method: 'DELETE' });
                const body = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(body.detail || `Server returned ${res.status}`);
                showToast(`Deleted "${deletedName}".`, 'success', 3000);
                // The app is gone, but something outside Venvy may still point
                // at it (a tunnel hostname Cloudflare would not let go of).
                (body.warnings || []).forEach(w => showToast(w, 'warn'));
                _cachedApps = _cachedApps.filter(a => a.id !== deletedId);
                if (activeAppId === deletedId) deselectApp();
                loadApps();
            } catch (e) {
                showToast('Delete failed: ' + (e.message || 'unknown error'), 'error');
                // Still there, so its terminal should be too.
                if (activeAppId === deletedId && tabAllowed('terminal')) initTerminal(deletedId);
            }
        }
