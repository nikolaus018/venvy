// Tabs, the modal system (focus trap, backdrop, Escape) and editor/escape keyboard handling.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Tab Switching ─────────────────────────────────────────────────────────
        // The tab bar: one tab per question. Terminal, Files and Packages are the
        // panes of Code (a row of their own under the tabs). Settings and Hosting
        // were tabs before v5.1: links and bookmarks that name them still work.
        const MAIN_TABS = ['overview', 'code', 'deploy', 'network', 'config', 'health', 'data', 'logs', 'jobs'];
        const CODE_PANES = ['terminal', 'files', 'packages'];
        const TABS = [...MAIN_TABS, ...CODE_PANES];
        const TAB_ALIASES = { settings: 'overview', hosting: 'deploy' };
        // Tabs that hold the app's settings form (Save in the tab bar).
        const SETTINGS_TABS = new Set(['overview', 'deploy', 'network', 'config', 'health']);
        let currentTab = 'overview';
        let _codePane = null;     // the Code pane last open

        // A shell, the file manager and deploys need an operator; a viewer's
        // app view starts (and stays) on what it may read.
        function tabAllowed(tab) {
            return currentUser?.role !== 'viewer' || (tab !== 'terminal' && tab !== 'files' && tab !== 'deploy');
        }

        function defaultTab() {
            return 'overview';
        }

        function _resolveTab(tab) {
            tab = TAB_ALIASES[tab] || tab;
            if (tab === 'code') tab = (_codePane && tabAllowed(_codePane)) ? _codePane : CODE_PANES.find(tabAllowed);
            return tab;
        }

        const LOG_POLL_MS = 1000;
        function startLogPolling() {
            if (logInterval) { clearInterval(logInterval); logInterval = null; }
            loadLogs();
            if (!document.hidden) logInterval = setInterval(loadLogs, LOG_POLL_MS);
        }

        function switchTab(tab, opts = {}) {
            tab = _resolveTab(tab);
            if (!TABS.includes(tab) || !tabAllowed(tab)) tab = defaultTab();
            currentTab = tab;
            const inCode = CODE_PANES.includes(tab);
            if (inCode) _codePane = tab;
            const mainTab = inCode ? 'code' : tab;
            document.querySelectorAll('.tab-content').forEach(el => {
                el.classList.add('hidden');
                el.classList.remove('tab-content-active');
            });
            document.querySelectorAll('.tab-btn, .code-tab').forEach(el => {
                el.classList.remove('active');
                el.style.color = 'var(--text3)';
                if (el.classList.contains('tab-btn')) el.style.borderBottomColor = 'transparent';
                else el.style.background = 'transparent';
                el.setAttribute('aria-selected', 'false');
                el.tabIndex = -1;
            });
            const panel = document.getElementById(`tab-${tab}`);
            panel.classList.remove('hidden');
            panel.classList.add('tab-content-active');

            const btn = document.getElementById(`tabbtn-${mainTab}`);
            if (btn) {
                btn.classList.add('active');
                btn.style.color = 'var(--text)';
                btn.style.borderBottomColor = 'var(--accent)';
                btn.setAttribute('aria-selected', 'true');
                btn.tabIndex = 0;
                if (opts.focusTab && !inCode) btn.focus();
            }
            const sub = document.getElementById('code-subnav');
            if (sub) { sub.classList.toggle('hidden', !inCode); sub.classList.toggle('flex', inCode); }
            if (inCode) {
                const pb = document.getElementById(`tabbtn-${tab}`);
                if (pb) {
                    pb.classList.add('active');
                    pb.style.color = 'var(--text)';
                    pb.style.background = 'var(--surface3)';
                    pb.setAttribute('aria-selected', 'true');
                    pb.tabIndex = 0;
                    if (opts.focusTab) pb.focus();
                }
            }

            if (logInterval) { clearInterval(logInterval); logInterval = null; _logsInflight = false; }
            stopDeployPolling();
            if (tab === 'logs') startLogPolling();
            if (tab === 'files') loadFiles(currentBrowserPath);
            if (tab === 'packages') loadPackages();
            if (tab === 'deploy') { loadDeploy(); loadHosting('deploy'); }
            if (tab === 'jobs') { loadJobs(); loadHosting('jobs'); }
            if (tab === 'network') { loadCfRouteStatus(); loadHosting('network'); }
            if (tab === 'config' || tab === 'data') loadHosting(tab);
            if (tab === 'overview') { loadUsage(); renderOverview(activeApp); }
            if (tab === 'terminal' && window.term && window.fitAddon) setTimeout(_termFit, 100);
            const saveBox = document.getElementById('settings-save-container');
            const withSave = SETTINGS_TABS.has(tab);
            saveBox.classList.toggle('hidden', !withSave);
            saveBox.classList.toggle('flex', withSave);
            if (withSave) checkSettingsDirty();
            // Keep the active tab visible in the horizontal scroller on mobile, after the
            // Save button has (or hasn't) taken its share of the row.
            if (btn) btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            if (!opts.silent) syncHash();
        }

        // Scrolls a card into view inside its tab (a "see …" link).
        function scrollToCard(id) {
            const el = document.getElementById(id);
            if (!el) return;
            el.scrollIntoView({ block: 'start', behavior: 'smooth' });
            el.classList.add('card-flash');
            setTimeout(() => el.classList.remove('card-flash'), 1200);
        }

        // The ⋯ menu in the app's header: Duplicate, Data, Delete.
        function toggleAppMenu(force) {
            const menu = document.getElementById('app-menu');
            const btn = document.getElementById('btn-app-menu');
            if (!menu || !btn) return;
            const open = typeof force === 'boolean' ? force : menu.classList.contains('hidden');
            menu.classList.toggle('hidden', !open);
            btn.setAttribute('aria-expanded', String(open));
            if (open) setTimeout(() => menu.querySelector('[role=menuitem]:not([disabled])')?.focus(), 0);
        }
        document.addEventListener('click', (e) => {
            const menu = document.getElementById('app-menu');
            if (menu && !menu.classList.contains('hidden') && !e.target.closest('#app-menu, #btn-app-menu')) toggleAppMenu(false);
        });
        document.addEventListener('keydown', (e) => {
            const menu = document.getElementById('app-menu');
            if (e.key === 'Escape' && menu && !menu.classList.contains('hidden')) { toggleAppMenu(false); document.getElementById('btn-app-menu')?.focus(); }
        });

        // Left/Right arrows move between tabs, Home/End jump to the ends (WAI-ARIA).
        function onTabListKey(e) {
            const allowed = MAIN_TABS.filter(t => t === 'code' ? CODE_PANES.some(tabAllowed) : tabAllowed(t));
            if (!allowed.length) return;
            const here = CODE_PANES.includes(currentTab) ? 'code' : currentTab;
            const i = Math.max(0, allowed.indexOf(here));
            let next = null;
            if (e.key === 'ArrowRight') next = allowed[(i + 1) % allowed.length];
            else if (e.key === 'ArrowLeft') next = allowed[(i - 1 + allowed.length) % allowed.length];
            else if (e.key === 'Home') next = allowed[0];
            else if (e.key === 'End') next = allowed[allowed.length - 1];
            if (!next) return;
            e.preventDefault();
            switchTab(next, { focusTab: true });
        }

        // The same keys in Code's own row (Terminal, Files, Packages).
        function onCodeTabKey(e) {
            const allowed = CODE_PANES.filter(tabAllowed);
            if (!allowed.length || !CODE_PANES.includes(currentTab)) return;
            const i = Math.max(0, allowed.indexOf(currentTab));
            let next = null;
            if (e.key === 'ArrowRight') next = allowed[(i + 1) % allowed.length];
            else if (e.key === 'ArrowLeft') next = allowed[(i - 1 + allowed.length) % allowed.length];
            else if (e.key === 'Home') next = allowed[0];
            else if (e.key === 'End') next = allowed[allowed.length - 1];
            if (!next) return;
            e.preventDefault();
            switchTab(next, { focusTab: true });
        }

        function fileOp(op) { if (op === 'refresh') loadFiles(currentBrowserPath); }
        // ── Modal system ─────────────────────────────────────────────────────────
        // Modals that should NOT close when clicking the backdrop
        // (e.g. security prompts where accidental dismiss is dangerous)
        const _NO_BACKDROP_CLOSE = new Set([]);

        // Custom close callbacks for modals that need cleanup on dismiss
        const _MODAL_CLOSE_FN = {
            'confirm-modal': () => _confirmResolve(false),
        };

        const _openModalIds = [];   // stack so Escape closes the topmost one
        const _modalReturnFocus = {}; // id -> element that had focus when it opened

        const _FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

        function _visibleFocusable(root) {
            return Array.from(root.querySelectorAll(_FOCUSABLE))
                .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement);
        }

        function openModal(id) {
            const el = document.getElementById(id);
            if (!el) return;
            // Remember where focus came from so we can put it back on close.
            const prev = document.activeElement;
            if (prev && prev !== document.body) _modalReturnFocus[id] = prev;
            el.classList.remove('hidden');
            if (!_openModalIds.includes(id)) _openModalIds.push(id);
            document.body.style.overflow = 'hidden';
            // Move focus inside so Tab is trapped and screen readers enter the dialog.
            setTimeout(() => {
                if (el.contains(document.activeElement)) return;
                const focusables = _visibleFocusable(el);
                const preferred = el.querySelector('input:not([type="hidden"]):not([disabled]), textarea:not([disabled])');
                (preferred || focusables[0] || el).focus?.();
            }, 40);
        }

        function closeModal(id) {
            const el = document.getElementById(id);
            if (!el) return;
            // Don't leave focus on a node inside a hidden subtree.
            if (el.contains(document.activeElement)) document.activeElement.blur();
            el.classList.add('hidden');
            const idx = _openModalIds.indexOf(id);
            if (idx !== -1) _openModalIds.splice(idx, 1);
            if (_openModalIds.length === 0) document.body.style.overflow = '';
            const back = _modalReturnFocus[id];
            delete _modalReturnFocus[id];
            if (back && back.isConnected && back.offsetParent !== null) {
                setTimeout(() => back.focus?.(), 0);
            }
        }

        // Tab / Shift+Tab cycle within the topmost dialog instead of escaping it.
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Tab' || _openModalIds.length === 0) return;
            const top = document.getElementById(_openModalIds[_openModalIds.length - 1]);
            if (!top || top.classList.contains('hidden')) return;
            const items = _visibleFocusable(top);
            if (!items.length) return;
            const first = items[0], last = items[items.length - 1];
            if (e.shiftKey && (document.activeElement === first || !top.contains(document.activeElement))) {
                e.preventDefault(); last.focus();
            } else if (!e.shiftKey && (document.activeElement === last || !top.contains(document.activeElement))) {
                e.preventDefault(); first.focus();
            }
        }, true);

        // Backdrop-click: close if the click target IS the overlay div itself
        // (i.e. the user clicked outside the inner panel)
        document.addEventListener('click', (e) => {
            for (const id of [..._openModalIds].reverse()) {
                const overlay = document.getElementById(id);
                if (!overlay || overlay.classList.contains('hidden')) continue;
                if (e.target === overlay && !_NO_BACKDROP_CLOSE.has(id)) {
                    if (_MODAL_CLOSE_FN[id]) _MODAL_CLOSE_FN[id]();
                    else closeModal(id);
                    break;   // only close the topmost
                }
            }
        }, { capture: false });

        // ── Keyboard shortcuts ────────────────────────────────────────────────────
        document.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 's' && currentTab === 'files' && _openModalIds.length === 0) {
                const path = document.getElementById('current-filename')?.dataset?.path;
                if (path) { e.preventDefault(); saveCurrentFile(); }
            }
            if (e.key === 'Escape') {
                // Close topmost open modal
                if (_openModalIds.length > 0) {
                    const topId = _openModalIds[_openModalIds.length - 1];
                    if (!_NO_BACKDROP_CLOSE.has(topId)) {
                        if (topId === 'confirm-modal') { _confirmResolve(false); return; }
                        if (_MODAL_CLOSE_FN[topId]) _MODAL_CLOSE_FN[topId]();
                        else closeModal(topId);
                        return;
                    }
                }
                // Nothing modal is open. Escape then walks the least-destructive
                // dismissal it can find, most specific first.
                if (document.getElementById('log-search') === document.activeElement) {
                    clearLogSearch();
                    return;
                }
                if (document.getElementById('app-search') === document.activeElement) {
                    clearAppSearch();
                    return;
                }
                if (isMobileFileEditorOpen && window.innerWidth < 768) {
                    closeEditorMobile();
                    return;
                }
                if (document.querySelector('#toast-container .toast')) {
                    dismissAllToasts();
                }
            }
        });
