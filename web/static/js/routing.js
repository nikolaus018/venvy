// Deep links (#/app/<id>/<tab>, #/system), the connection banner and crash notices.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ══════════════════════════════════════════════════════════════════════
        //  DEEP LINKS  —  #/app/<id>/<tab>  and  #/system
        //  Reload, the back button and bookmarks all land where you left off.
        // ══════════════════════════════════════════════════════════════════════
        let _suppressHashHandler = false;
        let _routeApplied = false;

        function currentHash() {
            if (_isSystemViewActive()) return '#/system';
            if (activeAppId) return `#/app/${activeAppId}/${currentTab}`;
            return '';
        }

        function syncHash() {
            const want = currentHash();
            if (want === window.location.hash) return;
            _suppressHashHandler = true;
            try {
                // replaceState keeps tab switches out of the back-button history;
                // only app/system changes push a new entry.
                history.replaceState(null, '', want || window.location.pathname + window.location.search);
            } catch (e) { window.location.hash = want; }
            setTimeout(() => { _suppressHashHandler = false; }, 0);
        }

        function parseHash() {
            const h = (window.location.hash || '').replace(/^#/, '');
            if (h === '/system') return { view: 'system' };
            const m = h.match(/^\/app\/(\d+)(?:\/([a-z]+))?$/);
            if (m) return { view: 'app', id: parseInt(m[1], 10), tab: m[2] || 'terminal' };
            return null;
        }

        function applyRoute() {
            const r = parseHash();
            if (!r) return false;
            if (r.view === 'system') { openSystemView(); return true; }
            if (r.view === 'app') {
                if (!_cachedApps.some(a => a.id === r.id)) return false;
                selectApp(r.id, { tab: r.tab, force: true });
                return true;
            }
            return false;
        }

        window.addEventListener('hashchange', () => {
            if (_suppressHashHandler) return;
            applyRoute();
        });

        // ══════════════════════════════════════════════════════════════════════
        //  CONNECTION BANNER
        // ══════════════════════════════════════════════════════════════════════
        let _connOk = true;
        function setConnected(ok) {
            if (ok === _connOk) return;
            _connOk = ok;
            const b = document.getElementById('conn-banner');
            if (b) {
                b.classList.toggle('show', !ok);
                // Reflow the app below the banner rather than covering the app
                // header with it — body is border-box, so padding shrinks it.
                document.body.style.paddingTop = ok ? '' : b.offsetHeight + 'px';
                // The terminal sizes itself to its container, so re-fit it.
                setTimeout(() => { try { _termFit(); } catch (e) { } }, 320);
            }
            if (ok) showToast('Reconnected to the server.', 'success', 2000);
            else announce('Connection to the server lost. Retrying.');
        }

        // ══════════════════════════════════════════════════════════════════════
        //  CRASH NOTICES
        //  A process that dies on its own should say so, once, with a way to
        //  jump straight to the evidence.
        // ══════════════════════════════════════════════════════════════════════
        const _lastKnownStatus = {};
        function _noticeCrash(app) {
            const prev = _lastKnownStatus[app.id];
            const nowCrashed = statusMeta(app.status).crashed;
            _lastKnownStatus[app.id] = app.status;
            if (prev === undefined || !nowCrashed || statusMeta(prev).crashed) return;
            showToast(`"${app.name}" stopped unexpectedly.`, 'error', 0, {
                actionLabel: 'View logs',
                onAction: () => selectApp(app.id, { tab: 'logs', force: true }),
            });
        }
