// App shell: editor instance, sidebar, and the 1-second status poll.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Ace Editor ───────────────────────────────────────────────────────────
        function initAceEditor() {
            if (aceEditor) return;
            aceEditor = ace.edit("file-editor");
            aceEditor.setTheme(resolvedTheme() === 'light' ? 'ace/theme/chrome' : 'ace/theme/one_dark');
            aceEditor.session.setMode("ace/mode/python");
            aceEditor.setFontSize(window.innerWidth < 768 ? 12 : 13);
            aceEditor.setOption("showPrintMargin", false);
            aceEditor.setOption("wrap", false);
            aceEditor.renderer.setScrollMargin(6, 6);
            // Custom theme overrides for dark look
            aceEditor.container.style.background = 'var(--surface)';
            aceEditor.on('change', () => { if (_openFilePath) _setEditorDirty(true); });
            // Note: ext-searchbox registers its own Ctrl-F handler — no override needed.
        }

        // ── Sidebar ──────────────────────────────────────────────────────────────
        function openSidebar() {
            const sidebar = document.getElementById('main-sidebar');
            const overlay = document.getElementById('sidebar-overlay');
            sidebar.classList.remove('-translate-x-full');
            sidebar.style.transform = ''; sidebar.style.transition = '';
            overlay.style.transition = ''; overlay.style.opacity = ''; overlay.style.display = '';
            overlay.classList.remove('hidden');
            setTimeout(() => overlay.classList.remove('opacity-0'), 10);
        }
        function closeSidebar() {
            const sidebar = document.getElementById('main-sidebar');
            const overlay = document.getElementById('sidebar-overlay');
            sidebar.classList.add('-translate-x-full');
            sidebar.style.transform = ''; sidebar.style.transition = '';
            overlay.style.transition = ''; overlay.style.opacity = ''; overlay.style.display = '';
            overlay.classList.add('opacity-0');
            setTimeout(() => overlay.classList.add('hidden'), 300);
        }
        function toggleSidebar() {
            const isClosed = document.getElementById('main-sidebar').classList.contains('-translate-x-full');
            if (isClosed) openSidebar(); else closeSidebar();
        }

        // ── Polling ──────────────────────────────────────────────────────────────
        // Track consecutive poll failures to avoid flipping to login on a single
        // network hiccup (common when iOS resumes a backgrounded PWA).
        let _loginTotpPending = false; // waiting for TOTP code after password OK
        let _pollFailures = 0;
        const _MAX_POLL_FAILURES = 3;

        let _pollInflight = false;
        let _pollTick = 0;
        function startPolling() {
            if (pollInterval) clearInterval(pollInterval);
            _pollFailures = 0;
            _pollInflight = false;
            pollInterval = setInterval(async () => {
                // Drop tick entirely if the previous one hasn't finished yet.
                // Without this guard, slow server responses stack up concurrent fetches.
                if (_pollInflight) return;
                _pollInflight = true;
                try {
                    const res = await fetch(`${API}/apps`);
                    if (!res.ok) {
                        if (res.status === 401) {
                            _pollFailures++;
                            if (_pollFailures >= 10) { // More lenient
                                clearInterval(pollInterval);
                                pollInterval = null;
                                _showReAuthOverlay();
                            }
                        }
                        return;
                    }
                    _pollFailures = 0;  // reset on success
                    const apps = await res.json();
                    setConnected(true);
                    // The sidebar filter, the command palette, [ / ] and deep
                    // links all read this list; it has to follow the server.
                    _cachedApps = apps;
                    // Deleted from another browser, or by a restore: don't
                    // leave the dashboard of an app that no longer exists.
                    if (activeAppId != null && !apps.some(a => a.id === activeAppId)) {
                        deselectApp(`"${activeApp?.name || 'This application'}" no longer exists — it was deleted.`);
                    }
                    apps.forEach(app => {
                        const m = statusMeta(app.status);
                        const dot = document.getElementById(`status-dot-${app.id}`);
                        if (dot) {
                            dot.className = `w-2 h-2 rounded-full flex-shrink-0 nav-dot ${m.dotCls}`;
                            dot.style.background = m.dot;
                        }
                        // A crash is worth telling the user about once, unprompted.
                        _noticeCrash(app);
                        if (activeAppId === app.id) {
                            const prev = activeApp;
                            activeApp = app;
                            updateHealthPill(app);
                            // Always keep title in sync (handles renames)
                            const titleEl = document.getElementById('dash-title');
                            if (titleEl && titleEl.innerText !== app.name) titleEl.innerText = app.name;
                            // Status badge
                            if (!prev || prev.status !== app.status) {
                                applyStatusBadge(document.getElementById('dash-status'), app.status);
                                updateLogLiveIndicator();
                                updateControlButtons(app);
                            }
                            // Network card — update on port or url change
                            if (!prev || prev.port !== app.port || prev.external_url !== app.external_url) {
                                _updateNetworkCard(app);
                                const portEl = document.getElementById('setting-port');
                                if (portEl && (!prev || prev.port !== app.port)) portEl.value = app.port || '';
                                // Clear port poll timer if port just appeared
                                if (app.port && _portPollTimer) { clearInterval(_portPollTimer); _portPollTimer = null; }
                            }
                        }
                    });
                    // renderAppList already fingerprints ids/names/statuses, so it
                    // re-renders on its own when the list actually changes.
                    renderAppList(apps);

                    // If settings tab is active, keep dirty indicator in sync with server data
                    const tabSettings = document.getElementById('tab-settings');
                    if (tabSettings && !tabSettings.classList.contains('hidden')) {
                        checkSettingsDirty();
                    }

                    // If we were showing STALE, repaint from the real status —
                    // just dropping the class would leave the old label behind.
                    const badge = document.getElementById('dash-status');
                    if (badge && badge.classList.contains('status-stale')) {
                        applyStatusBadge(badge, activeApp?.status);
                    }

                    // If system view is active, update system metrics. Every
                    // third tick: the server samples them every 2.5 s, and
                    // listing the host's ports is not free.
                    if (++_pollTick % 3 === 0 && !document.getElementById('system-view').classList.contains('hidden')) {
                        loadSystemView();
                    }

                } catch (e) {
                    _pollFailures++;
                    // NEVER stop the loop on generic network errors.
                    // Just keep trying and show the STALE indicator.
                    applyStatusBadge(document.getElementById('dash-status'), 'stale');
                    // Two misses in a row is a real outage, not a hiccup.
                    if (_pollFailures >= 2) setConnected(false);
                } finally {
                    _pollInflight = false;
                }
            }, 1000);
        }
