// Applications: sidebar list and filter, network card, selecting an app, the dashboard, start/stop/restart.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Apps ──────────────────────────────────────────────────────────────────
        let _cachedApps = [];
        let _appsEverLoaded = false;

        function renderAppListSkeleton() {
            const el = document.getElementById('app-list');
            if (!el || _appsEverLoaded) return;
            el.innerHTML = Array.from({ length: 4 }, (_, i) => `
                <div class="flex items-center gap-2.5 px-3 py-2.5" aria-hidden="true">
                    <div class="skeleton w-2 h-2 rounded-full shrink-0"></div>
                    <div class="skeleton h-3 rounded" style="width:${70 - i * 9}%"></div>
                </div>`).join('');
        }

        async function loadApps() {
            renderAppListSkeleton();
            try {
                const res = await fetch(`${API}/apps`);
                if (!res.ok) {
                    if (!_appsEverLoaded) renderAppListError('Could not load applications.');
                    return;
                }
                const apps = await res.json();
                _appsEverLoaded = true;
                _cachedApps = apps;
                _lastAppListHash = '';
                renderAppList(apps);
                // Seed crash tracking so we don't alert on apps that were already
                // down before this session started.
                apps.forEach(a => { if (!(a.id in _lastKnownStatus)) _lastKnownStatus[a.id] = a.status; });
                if (!_routeApplied) { _routeApplied = true; applyRoute(); }
            } catch (e) {
                if (!_appsEverLoaded) renderAppListError('Network error — is the server running?');
            }
        }

        function renderAppListError(msg) {
            const el = document.getElementById('app-list');
            if (!el) return;
            _lastAppListHash = '';
            el.innerHTML = `<div class="text-center py-8 px-4">
                <p class="text-xs font-medium mb-3" style="color:var(--red)">${escapeHtml(msg)}</p>
                <button data-on-click="loadApps()" class="btn-ghost text-xs px-3 py-1.5 rounded-lg font-medium">Retry</button>
            </div>`;
        }

        let _lastAppListHash = '';
        let _appFilter = '';
        let _emptyFallbackDone = false;
        // Highlights the matched substring so it's obvious why a row survived the filter.
        function _highlight(name, needle) {
            const safe = escapeHtml(name);
            if (!needle) return safe;
            const i = name.toLowerCase().indexOf(needle);
            if (i === -1) return safe;
            return escapeHtml(name.slice(0, i))
                + '<mark class="search-hit">' + escapeHtml(name.slice(i, i + needle.length)) + '</mark>'
                + escapeHtml(name.slice(i + needle.length));
        }

        function renderAppList(apps) {
            const needle = _appFilter.trim().toLowerCase();
            const shown = needle ? apps.filter(a => (a.name || '').toLowerCase().includes(needle)) : apps;

            const hash = shown.map(a => `${a.id}:${a.name}:${a.status}`).join('|')
                + '|active:' + activeAppId + '|q:' + needle;
            if (hash === _lastAppListHash) return;
            _lastAppListHash = hash;

            // Counter reflects filtered/total so the filter's effect is legible.
            const countEl = document.getElementById('app-count');
            if (countEl) {
                countEl.textContent = needle ? `${shown.length}/${apps.length}` : String(apps.length);
                const crashed = apps.filter(a => statusMeta(a.status).crashed).length;
                countEl.title = crashed ? `${crashed} application(s) crashed` : '';
                countEl.style.color = crashed ? 'var(--red)' : '';
                countEl.style.borderColor = crashed ? 'rgba(var(--red-rgb),0.35)' : '';
            }

            // Search box only earns its space once there's something to search.
            const searchWrap = document.getElementById('app-search-wrap');
            if (searchWrap) searchWrap.classList.toggle('hidden', apps.length < 5 && !needle);

            const listEl = document.getElementById('app-list');

            if (!apps.length) {
                listEl.innerHTML = `<div class="text-center py-10 px-4">
                <div class="w-9 h-9 rounded-xl mx-auto flex items-center justify-center mb-3" style="background:var(--surface2);border:1px solid var(--border)">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" style="color:var(--text3)"><path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
                </div>
                <p class="text-xs font-medium" style="color:var(--text3)">No applications yet</p>
                <p class="text-xs mt-1" style="color:var(--text3);opacity:.7">Use <strong style="color:var(--text3)">New Application</strong> to create one</p>
            </div>`;
                // With nothing to show, System is the only useful landing view —
                // but only offer it once, and never over a selected app.
                if (!activeAppId && !_emptyFallbackDone) {
                    _emptyFallbackDone = true;
                    openSystemView();
                }
                return;
            }

            if (!shown.length) {
                listEl.innerHTML = `<div class="text-center py-8 px-4">
                <p class="text-xs font-medium" style="color:var(--text3)">No match for &ldquo;${escapeHtml(_appFilter)}&rdquo;</p>
                <button data-on-click="clearAppSearch()" class="btn-ghost text-xs px-3 py-1.5 rounded-lg font-medium mt-3">Clear filter</button>
            </div>`;
                return;
            }

            listEl.innerHTML = shown.map(app => {
                const isActive = app.id === activeAppId;
                const m = statusMeta(app.status);
                return `
                <button type="button" data-on-click="selectApp(${app.id})" data-app-id="${app.id}"
                     aria-current="${isActive ? 'true' : 'false'}"
                     title="${escapeHtml(app.name)} — ${m.label}"
                     class="nav-item app-row w-full text-left flex items-center justify-between gap-2.5 px-3 py-2.5 rounded-xl cursor-pointer transition active:scale-95 ${isActive ? 'active' : ''}"
                     style="color:${isActive ? 'var(--accent-light)' : 'var(--text2)'}">
                    <div class="flex items-center gap-2.5 min-w-0">
                        <span id="status-dot-${app.id}" class="w-2 h-2 rounded-full flex-shrink-0 nav-dot ${m.dotCls}" style="background:${m.dot}"></span>
                        <span class="truncate text-sm font-medium">${_highlight(app.name || '', needle)}</span>
                    </div>
                    ${m.crashed ? '<span class="text-[9px] font-black uppercase tracking-wider shrink-0" style="color:var(--red)">crashed</span>' : ''}
                </button>`;
            }).join('');
        }

        // ── Sidebar filter ───────────────────────────────────────────────────────
        function onAppSearchInput() {
            const input = document.getElementById('app-search');
            _appFilter = input.value;
            document.getElementById('app-search-wrap').classList.toggle('has-value', !!_appFilter);
            _lastAppListHash = '';
            renderAppList(_cachedApps);
        }

        function clearAppSearch() {
            const input = document.getElementById('app-search');
            if (!input) return;
            input.value = '';
            _appFilter = '';
            document.getElementById('app-search-wrap').classList.remove('has-value');
            _lastAppListHash = '';
            renderAppList(_cachedApps);
            input.focus();
        }

        // Enter opens the only remaining match; Down arrow moves into the list.
        function onAppSearchKey(e) {
            if (e.key === 'Enter') {
                const rows = document.querySelectorAll('#app-list .app-row');
                if (rows.length >= 1) { e.preventDefault(); rows[0].click(); }
            } else if (e.key === 'ArrowDown') {
                const first = document.querySelector('#app-list .app-row');
                if (first) { e.preventDefault(); first.focus(); }
            }
        }

        // Up/Down roving focus through the application list.
        document.getElementById('app-list').addEventListener('keydown', (e) => {
            if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
            const rows = Array.from(document.querySelectorAll('#app-list .app-row'));
            const i = rows.indexOf(document.activeElement);
            if (i === -1) return;
            e.preventDefault();
            if (e.key === 'ArrowDown') {
                if (i < rows.length - 1) rows[i + 1].focus();
            } else {
                if (i > 0) rows[i - 1].focus();
                else document.getElementById('app-search')?.focus();
            }
        });

        // ── Network card ─────────────────────────────────────────────────────────
        // Preference: which URL the "Open App" button uses when both local + CF exist.
        // Stored per-app in the DB. We track a transient state for the "Save" dirty check.
        let _tempOpenUrlPref = null;

        function _getOpenUrlPref() {
            if (_tempOpenUrlPref) return _tempOpenUrlPref;
            return activeApp?.url_source_pref || 'local';
        }

        function setOpenUrlPref(pref) {
            _tempOpenUrlPref = pref;
            _renderOpenPrefButtons(pref);
            checkSettingsDirty();
            // Update Open App button tooltip immediately
            if (activeApp) _updateNetworkCard(activeApp);
        }

        function _renderOpenPrefButtons(pref) {
            const localBtn = document.getElementById('open-pref-local');
            const publicBtn = document.getElementById('open-pref-public');
            if (!localBtn || !publicBtn) return;
            const active = 'background:var(--accent);color:white';
            const inactive = 'color:var(--text3)';
            localBtn.style.cssText = pref === 'local' ? active : inactive;
            publicBtn.style.cssText = pref === 'public' ? active : inactive;
        }

        function _getEffectiveUrl(app) {
            if (!app) return null;
            const localUrl = app.local_url || null;
            const publicUrl = app.external_url || null;
            const pref = _getOpenUrlPref();
            if (pref === 'public' && publicUrl) return publicUrl;
            return localUrl || publicUrl;   // fall back gracefully
        }

        function _updateNetworkCard(app) {
            const localUrl = app.local_url || null;
            const publicUrl = app.external_url || null;
            const hasBoth = !!(localUrl && publicUrl);

            // Open App button — always enabled when we have ANY url
            const effectiveUrl = _getEffectiveUrl(app);
            const btnOpen = document.getElementById('btn-open');
            if (btnOpen) {
                btnOpen.disabled = !effectiveUrl;
                btnOpen.title = effectiveUrl
                    ? `Open: ${effectiveUrl}`
                    : (app.status === 'running' ? 'Detecting port…' : 'Start the app to detect its port');
            }

            // Open URL preference toggle — visible only when both URLs exist
            const prefRow = document.getElementById('open-url-pref-row');
            if (prefRow) {
                prefRow.classList.toggle('hidden', !hasBoth);
                if (hasBoth) _renderOpenPrefButtons(_getOpenUrlPref());
            }

            const card = document.getElementById('net-status-card');
            if (!card) return;

            if (localUrl) {
                // Always show local IP:port — clean, always useful, never confusing
                const portStr = app.port;
                card.style.cssText = `border-radius:var(--radius-md);padding:1rem;background:var(--green-dim);border:1px solid var(--green-border)`;
                card.innerHTML = `
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0 flex-1">
                        <p class="text-[10px] font-bold uppercase tracking-wider mb-1 flex items-center gap-1.5" style="color:var(--green)">
                            <svg class="w-3.5 h-3.5 shrink-0" fill="none" stroke="var(--green)" viewBox="0 0 24 24" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                            Local Network · port ${escapeHtml(portStr)}
                        </p>
                        <p class="text-sm mono font-semibold truncate select-all leading-snug" style="color:var(--green)">${escapeHtml(localUrl)}</p>
                    </div>
                    <div class="flex gap-1 shrink-0">
                        <button data-on-click="copyAccessUrl()" title="Copy local URL" class="p-1.5 rounded-lg transition active:scale-95" style="color:var(--text3)">
                            <i data-feather="copy" class="w-3.5 h-3.5"></i>
                        </button>
                        <a href="${escapeHtml(localUrl)}" target="_blank" rel="noopener" title="Open local" class="p-1.5 rounded-lg transition active:scale-95 flex items-center" style="color:var(--text3)">
                            <i data-feather="external-link" class="w-3.5 h-3.5"></i>
                        </a>
                    </div>
                </div>`;
            } else {
                const detecting = app.status === 'running';
                card.style.cssText = `border-radius:var(--radius-md);padding:1rem;background:var(--surface2);border:1px solid var(--border)`;
                card.innerHTML = detecting
                    ? `<div class="flex items-center gap-3">
                       <svg class="w-4 h-4 animate-spin shrink-0" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
                       <div>
                           <p class="text-xs font-bold" style="color:var(--text)">Detecting port…</p>
                           <p class="text-xs mt-0.5" style="color:var(--text3)">Scanning for a listening TCP socket. Set manually below if needed.</p>
                       </div>
                   </div>`
                    : `<div class="flex items-center gap-3">
                       <i data-feather="wifi-off" class="w-4 h-4 shrink-0" style="color:var(--text3)"></i>
                       <div>
                           <p class="text-xs font-bold" style="color:var(--text2)">No local URL</p>
                           <p class="text-xs mt-0.5" style="color:var(--text3)">Start the app to auto-detect port, or set one manually below.</p>
                       </div>
                   </div>`;
            }
            _featherReplace();
        }

        // ── Select App ───────────────────────────────────────────────────────────
        // Tracks in-flight port poll so switching apps cancels the old one cleanly
        let _portPollTimer = null;

        // True when the Settings form holds edits that have not been saved.
        function isSettingsDirty() {
            const dot = document.getElementById('save-status-dot');
            return !!dot && !dot.classList.contains('hidden');
        }

        async function selectApp(id, opts = {}) {
            if (id === activeAppId && !opts.force) {
                // Re-selecting the already-active app should still leave System view,
                // since that's how the user navigates back from it. activeAppId is
                // truthy here, so this is exactly closeSystemView()'s own transition.
                if (_isSystemViewActive()) closeSystemView();
                if (opts.tab) switchTab(opts.tab);
                if (window.innerWidth < 768) closeSidebar();
                return;
            }

            // Switching apps overwrites the Settings form and closes the open
            // file — confirm before losing edits in either.
            if (activeAppId != null && id !== activeAppId && (isSettingsDirty() || _editorDirty)) {
                const what = isSettingsDirty() && _editorDirty ? 'Settings and in the open file'
                    : isSettingsDirty() ? 'Settings' : 'the open file';
                const leave = await confirmDialog({
                    title: 'Discard unsaved changes?',
                    sub: activeApp?.name || '',
                    body: `You have changes in ${what} that have not been saved. Switching to another application will discard them.`,
                    confirmLabel: 'Discard & switch',
                    cancelLabel: 'Stay here',
                    danger: true,
                    icon: 'alert-triangle',
                });
                if (!leave) return;
            }

            // Cancel any existing port poll from a previous app
            if (_portPollTimer) { clearInterval(_portPollTimer); _portPollTimer = null; }

            // Reset per-app log state so the previous app's output never bleeds through.
            _logRaw = ''; _logRenderedKey = ''; _logFollow = true;
            clearLogSearch();


            activeAppId = id;
            if (logInterval) { clearInterval(logInterval); logInterval = null; }
            document.getElementById('system-view').classList.add('hidden');
            document.getElementById('system-nav-btn').classList.remove('active');
            if (window.innerWidth < 768) closeSidebar();

            // Try to set activeApp synchronously from cache before anything else
            // to ensure UI elements (like the terminal label) have the correct data immediately.
            const found = _cachedApps.find(a => a.id === id);
            if (found) activeApp = found;
            else if (activeApp && activeApp.id !== id) activeApp = null;

            // Show the dashboard immediately with whatever we already know from the
            // sidebar list — don't wait for the network before rendering anything.
            const knownApp = activeApp?.id === id ? activeApp : null;
            if (knownApp) {
                _renderDashboard(knownApp);
            } else {
                // First time: show skeleton title while fetch is in flight
                if (!_isSystemViewActive()) {
                    document.getElementById('empty-state').classList.add('hidden');
                    document.getElementById('dashboard').classList.remove('hidden');
                }
                document.getElementById('dash-title').innerText = '\u2026';
            }

            // Close system view when user explicitly selects an app
            if (_isSystemViewActive()) {
                document.getElementById('system-view').classList.add('hidden');
                document.getElementById('system-nav-btn').classList.remove('active');
                document.getElementById('empty-state').classList.add('hidden');
                document.getElementById('dashboard').classList.remove('hidden');
            }

            switchTab(opts.tab && TABS.includes(opts.tab) ? opts.tab : defaultTab());
            // A viewer may not open a shell; connecting anyway only produced
            // a row of failed reconnects.
            if (tabAllowed('terminal')) initTerminal(id); else _termDestroy();
            syncHash();
            if (found) announce(`${found.name} selected, ${statusMeta(found.status).label}`);

            // Fetch authoritative data in background to ensure we are up to date
            try {
                const res = await fetch(`${API}/apps`);
                if (!res.ok) return;
                const apps = await res.json();
                _cachedApps = apps;
                if (activeAppId !== id) return;   // switched again while this was loading
                activeApp = apps.find(a => a.id === id);
                if (!activeApp) { deselectApp('That application no longer exists.'); return; }
                renderAppList(apps);
                _renderDashboard(activeApp);
            } catch (e) {
                // If we have cached data, don't show an error toast for a background update fail
                if (!activeApp) showToast('Network error loading app.', 'error');
            }

            // Port polling — only when running with no port detected yet
            // (activeApp is null here if the refresh above failed with nothing cached)
            if (activeApp && activeApp.status === 'running' && !activeApp.port) {
                let _polls = 0;
                _portPollTimer = setInterval(async () => {
                    _polls++;
                    if (activeAppId !== id || _polls >= 60) { clearInterval(_portPollTimer); _portPollTimer = null; return; }
                    try {
                        const r = await fetch(`${API}/apps`);
                        if (!r.ok) return;
                        const apps = await r.json();
                        const updated = apps.find(a => a.id === id);
                        if (updated && activeAppId === id) {
                            activeApp = updated;
                            _updateNetworkCard(activeApp);
                            document.getElementById('setting-port').value = activeApp.port || '';
                            if (activeApp.port) { clearInterval(_portPollTimer); _portPollTimer = null; }
                        }
                    } catch (e) { }
                }, 2000);
            }
        }

        // Back to "no application selected": after deleting the open app, or
        // when it turns out to be gone (deleted elsewhere, or by a restore).
        function deselectApp(message) {
            if (_portPollTimer) { clearInterval(_portPollTimer); _portPollTimer = null; }
            if (logInterval) { clearInterval(logInterval); logInterval = null; }
            _termDestroy();
            _termAppId = null;
            activeAppId = null; activeApp = null;
            _resetEditor();
            currentBrowserPath = ''; fileClipboard = null;
            _logRaw = ''; _logRenderedKey = '';
            // Nothing left that could be unsaved.
            document.getElementById('save-status-dot')?.classList.add('hidden');
            document.querySelector('#tabbtn-settings .settings-dirty-dot')?.remove();
            ['delete-app-modal', 'remove-pkg-modal', 'file-name-modal', 'tunnel-conflict-modal'].forEach(closeModal);
            _lastAppListHash = '';
            document.getElementById('dashboard').classList.add('hidden');
            if (!_isSystemViewActive()) {
                document.getElementById('empty-state').classList.remove('hidden');
                document.title = 'Venvy';
            }
            syncHash();
            renderAppList(_cachedApps);
            if (message) showToast(message, 'warn', 6000);
        }

        // Renders all dashboard fields from an app object — called on select and on poll update.
        // Guard: if system-view is currently open we must NOT forcibly un-hide the dashboard.
        function _renderDashboard(app) {
            if (_isSystemViewActive()) return;
            document.getElementById('empty-state').classList.add('hidden');
            document.getElementById('dashboard').classList.remove('hidden');
            const titleEl = document.getElementById('dash-title');
            titleEl.innerText = app.name;
            titleEl.title = app.name;
            document.title = `${app.name} · Venvy`;
            applyStatusBadge(document.getElementById('dash-status'), app.status);
            updateHealthPill(app);
            updateControlButtons(app);
            updateLogLiveIndicator();

            // ONLY update settings if they are not dirty (preserving user unsaved changes)
            // EXCEPT if we are switching to a completely different app.
            const isAppSwitch = (typeof _lastActiveAppId !== 'undefined' && _lastActiveAppId !== app.id);
            const isCurrentlyDirty = !isAppSwitch && document.getElementById('save-status-dot') && !document.getElementById('save-status-dot').classList.contains('hidden');

            if (!isCurrentlyDirty) {
                document.getElementById('setting-policy').value = app.restart_policy || 'never';
                document.getElementById('setting-boot').checked = app.start_on_boot;
                document.getElementById('setting-port').value = app.port || '';
                envEditor.load(app);
                renderSettingsExtras(app);
                document.getElementById('setting-health-path').value = app.healthcheck_path || '';
                document.getElementById('setting-health-interval').value = app.health_interval ?? 30;
                document.getElementById('setting-health-failures').value = app.health_failures ?? 3;
                document.getElementById('setting-health-restart').checked = app.health_restart ?? true;
                document.getElementById('setting-domain').value = app.domain || '';
                document.getElementById('setting-disk-limit').value = app.disk_limit_mb ?? '';
                document.getElementById('setting-name').value = app.name || '';
                if (document.getElementById('setting-runtime')) {
                    document.getElementById('setting-runtime').value = app.runtime || 'python';
                }
                document.getElementById('setting-entrypoint').value = app.entrypoint || '';
                const boot = document.getElementById('setting-boot');
                if (app.start_on_boot) boot.checked = true; else boot.checked = false;
                const appMemLimit = app.memory_limit_mb ?? 0, appCpuLimit = app.cpu_limit_percent ?? 0;
                _setResourceLimitsToggle(appMemLimit > 0 || appCpuLimit > 0);
                if (appMemLimit > 0) _setSliderValue(document.getElementById('setting-memory-limit'), appMemLimit);
                if (appCpuLimit > 0) _setSliderValue(document.getElementById('setting-cpu-limit'), appCpuLimit);
                _updateResourceLimitLabels();
                const limitsWarnEl = document.getElementById('setting-limits-unenforced-warn');
                limitsWarnEl.classList.toggle('flex', !app.resource_limits_enforced);
                limitsWarnEl.classList.toggle('hidden', !!app.resource_limits_enforced);

                onRuntimeChange('settings');
                document.getElementById('setting-entrypoint').value = app.entrypoint || '';
                _tempOpenUrlPref = app.url_source_pref || 'local';
                _renderOpenPrefButtons(_tempOpenUrlPref);
            }

            _updateNetworkCard(app);

            _wireSettingsDirtyCheck();
            checkSettingsDirty();

            // ONLY clear path if we are switching to a COMPLETELY DIFFERENT app
            if (typeof _lastActiveAppId !== 'undefined' && _lastActiveAppId !== app.id) {
                currentBrowserPath = ""; fileClipboard = null; _resetEditor();
            }
            window._lastActiveAppId = app.id;
        }

        // Disable the controls that cannot apply right now, and say why on hover.
        let _controlsBusy = false;
        function updateControlButtons(app) {
            if (_controlsBusy) return;
            const running = app?.status === 'running';
            const crashed = statusMeta(app?.status).crashed;
            const set = (id, disabled, title) => {
                const b = document.getElementById(id);
                if (!b) return;
                b.disabled = !!disabled;
                b.title = title;
            };
            set('btn-start', running, running ? 'Already running' : (crashed ? 'Start again after the crash' : 'Start'));
            set('btn-restart', !running, running ? 'Restart' : 'Not running');
            set('btn-stop', !running, running ? 'Stop' : 'Not running');
        }

        // ── Control App ──────────────────────────────────────────────────────────
        async function controlApp(action) {
            const targetAppId = activeAppId;
            const btn = document.getElementById(`btn-${action}`);
            const originalContent = btn ? btn.innerHTML : '';

            function _resetBtn() {
                _controlsBusy = false;
                if (!btn) return;
                btn.disabled = false;
                btn.innerHTML = originalContent;
                _featherReplace();
                // Re-apply state-based enablement so we don't light up an
                // action that can't actually run (e.g. Stop on a stopped app).
                updateControlButtons(activeApp);
            }

            if (btn) {
                _controlsBusy = true;
                btn.disabled = true;
                btn.innerHTML = '<i data-feather="loader" class="w-4 h-4 animate-spin"></i>';
                _featherReplace();
            }
            announce(`${action} requested`);

            try {
                // The backend returns 202 immediately (fire-and-forget).
                // 5 s is plenty since the server responds before doing any real work.
                const controller = new AbortController();
                const tId = setTimeout(() => controller.abort(), 5000);
                let res;
                try {
                    res = await fetch(`${API}/apps/${targetAppId}/control?action=${action}`, {
                        method: 'POST', signal: controller.signal
                    });
                } finally {
                    clearTimeout(tId);
                }

                if (!res.ok) {
                    const e = await res.json().catch(() => ({ detail: 'Action failed' }));
                    showToast(e.detail || 'Action failed', 'error');
                    return;
                }
            } catch (e) {
                showToast(e.name === 'AbortError' ? 'Server not responding.' : 'Network error: ' + e.message, 'error');
                return;
            } finally {
                // Button unlocks the moment the HTTP response arrives — milliseconds after click.
                _resetBtn();
            }

            // ── Poll until state settles (running/stopped/error) ─────────────────
            // The actual work (kill tree, spawn process) happens server-side in a background
            // task. We just poll until we see the expected final state.
            const expectedFinal = action === 'stop' ? 'stopped' : 'running';
            let polls = 0;
            const pollTimer = setInterval(async () => {
                polls++;
                // Stop polling after 30 s regardless
                if (polls > 30 || activeAppId !== targetAppId) {
                    clearInterval(pollTimer);
                    return;
                }
                try {
                    const r = await fetch(`${API}/apps`);
                    if (!r.ok) return;
                    const apps = await r.json();
                    _cachedApps = apps;
                    renderAppList(apps);
                    const updated = apps.find(a => a.id === targetAppId);
                    if (!updated || activeAppId !== targetAppId) { clearInterval(pollTimer); return; }
                    activeApp = updated;
                    _renderDashboard(activeApp);

                    const settled = updated.status === expectedFinal || updated.status === 'error';
                    if (settled) {
                        clearInterval(pollTimer);
                        showToast(
                            updated.status === 'error' ? 'Action failed — check logs.' :
                                { start: 'Started ✓', stop: 'Stopped ✓', restart: 'Restarted ✓' }[action] || action,
                            updated.status === 'error' ? 'error' : 'success', 1500
                        );
                        // Also kick off port-detection poll on start/restart
                        if ((action === 'start' || action === 'restart') && updated.status === 'running') {
                            if (_portPollTimer) { clearInterval(_portPollTimer); _portPollTimer = null; }
                            let _pp = 0;
                            _portPollTimer = setInterval(async () => {
                                _pp++;
                                if (activeAppId !== targetAppId || _pp >= 60) { clearInterval(_portPollTimer); _portPollTimer = null; return; }
                                try {
                                    const pr = await fetch(`${API}/apps`);
                                    if (!pr.ok) return;
                                    const papps = await pr.json();
                                    const pu = papps.find(a => a.id === targetAppId);
                                    if (pu && activeAppId === targetAppId) {
                                        activeApp = pu;
                                        _updateNetworkCard(activeApp);
                                        const portInp = document.getElementById('setting-port');
                                        if (portInp) portInp.value = activeApp.port || '';
                                        if (activeApp.port) { clearInterval(_portPollTimer); _portPollTimer = null; }
                                    }
                                } catch (e) { }
                            }, 2000);
                        }
                    }
                } catch (e) { }
            }, 1000);
        }

        function openExternal() {
            const url = _getEffectiveUrl(activeApp);
            if (!url) {
                showToast(activeApp?.status === 'running' ? 'Port not yet detected — try again.' : 'Start the app first, or set a port in Settings.', 'warn', 4000);
                return;
            }
            window.open(url, '_blank');
        }

        async function copyAccessUrl() {
            const url = _getEffectiveUrl(activeApp);
            if (!url) { showToast('No URL available yet.', 'warn'); return; }
            try { await navigator.clipboard.writeText(url); }
            catch (e) { const ta = document.createElement('textarea'); ta.value = url; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
            showToast('URL copied!', 'success', 1500);
        }
