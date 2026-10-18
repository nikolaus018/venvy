// Tabs, the modal system (focus trap, backdrop, Escape) and editor/escape keyboard handling.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Tab Switching ─────────────────────────────────────────────────────────
        const TABS = ['terminal', 'files', 'logs', 'packages', 'deploy', 'jobs', 'settings'];
        let currentTab = 'terminal';

        // A shell and the file manager need an operator; a viewer's app view
        // starts (and stays) on what it may read.
        function tabAllowed(tab) {
            return currentUser?.role !== 'viewer' || (tab !== 'terminal' && tab !== 'files' && tab !== 'deploy');
        }

        function defaultTab() {
            return currentUser?.role === 'viewer' ? 'logs' : 'terminal';
        }

        const LOG_POLL_MS = 1000;
        function startLogPolling() {
            if (logInterval) { clearInterval(logInterval); logInterval = null; }
            loadLogs();
            if (!document.hidden) logInterval = setInterval(loadLogs, LOG_POLL_MS);
        }

        function switchTab(tab, opts = {}) {
            if (!TABS.includes(tab) || !tabAllowed(tab)) tab = defaultTab();
            currentTab = tab;
            document.querySelectorAll('.tab-content').forEach(el => {
                el.classList.add('hidden');
                el.classList.remove('tab-content-active');
            });
            document.querySelectorAll('.tab-btn').forEach(el => {
                el.classList.remove('active');
                el.style.color = 'var(--text3)';
                el.style.borderBottomColor = 'transparent';
                el.setAttribute('aria-selected', 'false');
                el.tabIndex = -1;
            });
            const panel = document.getElementById(`tab-${tab}`);
            panel.classList.remove('hidden');
            panel.classList.add('tab-content-active');

            const btn = document.getElementById(`tabbtn-${tab}`);
            if (btn) {
                btn.classList.add('active');
                btn.style.color = 'var(--text)';
                btn.style.borderBottomColor = 'var(--accent)';
                btn.setAttribute('aria-selected', 'true');
                btn.tabIndex = 0;
                if (opts.focusTab) btn.focus();
                // Keep the active tab visible in the horizontal scroller on mobile.
                btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }

            if (logInterval) { clearInterval(logInterval); logInterval = null; _logsInflight = false; }
            stopDeployPolling();
            if (tab === 'logs') startLogPolling();
            if (tab === 'files') loadFiles(currentBrowserPath);
            if (tab === 'packages') loadPackages();
            if (tab === 'deploy') loadDeploy();
            if (tab === 'jobs') loadJobs();
            if (tab === 'terminal' && window.term && window.fitAddon) setTimeout(_termFit, 100);
            if (tab === 'settings') {
                loadCfRouteStatus();
                loadUsage();
                document.getElementById('settings-save-container').classList.remove('hidden');
                document.getElementById('settings-save-container').classList.add('flex');
                checkSettingsDirty();
            } else {
                document.getElementById('settings-save-container').classList.add('hidden');
                document.getElementById('settings-save-container').classList.remove('flex');
            }
            if (!opts.silent) syncHash();
        }

        // Left/Right arrows move between tabs, Home/End jump to the ends (WAI-ARIA).
        function onTabListKey(e) {
            const i = TABS.indexOf(currentTab);
            let next = null;
            if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
            else if (e.key === 'ArrowLeft') next = TABS[(i - 1 + TABS.length) % TABS.length];
            else if (e.key === 'Home') next = TABS[0];
            else if (e.key === 'End') next = TABS[TABS.length - 1];
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
            if ((e.metaKey || e.ctrlKey) && e.key === 's') {
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
