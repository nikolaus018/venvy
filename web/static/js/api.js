// API client and shared page state: base URL, auth key, CSRF/auth fetch wrapper, job polling, globals, escapeHtml.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        const API = window.location.origin + "/api";

        // --- Auth key ---
        // Login returns a per-session key that must accompany the session
        // cookie on every request (X-Venvy-Key). Browsers send cookies to every
        // port on this host — including hosted apps — but localStorage is per
        // origin, so a hosted app that sees the cookie still can't use it.
        const _AUTH_KEY_STORE = 'venvy_auth_key';
        let _authKeyMem = null;
        function getAuthKey() {
            try { return localStorage.getItem(_AUTH_KEY_STORE) || _authKeyMem; } catch { return _authKeyMem; }
        }
        function setAuthKey(key) {
            _authKeyMem = key || null;
            try { key ? localStorage.setItem(_AUTH_KEY_STORE, key) : localStorage.removeItem(_AUTH_KEY_STORE); } catch { }
        }
        function _csrfToken() {
            const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
            return m ? decodeURIComponent(m[1]) : null;
        }

        // --- CSRF + auth wrapper ---
        const originalFetch = window.fetch;
        window.fetch = async function (resource, init) {
            init = init || {};
            // The session key and the CSRF token are for Venvy's own API only.
            // Anything else fetched from this page (a CDN, an app's URL) must
            // never be handed them.
            try {
                const target = new URL(resource instanceof Request ? resource.url : String(resource), window.location.href);
                if (target.origin !== window.location.origin) return originalFetch.call(this, resource, init);
            } catch { }
            const method = (init.method || 'GET').toUpperCase();
            const headers = new Headers(init.headers || {});
            const key = getAuthKey();
            if (key) headers.set('x-venvy-key', key);
            if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
                const csrf = _csrfToken();
                if (csrf) headers.set('x-csrf-token', csrf);
            }
            init.headers = headers;
            return originalFetch.call(this, resource, init);
        };

        // Background jobs: long operations answer {job_id}; poll until done.
        // Resolves to the job's result, throws with its error message.
        async function waitForJob(res) {
            const body = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
            if (!body.job_id) return body;
            let misses = 0;
            for (; ;) {
                await new Promise(r => setTimeout(r, 1000));
                let r;
                try { r = await fetch(`${API}/jobs/${body.job_id}`, { cache: 'no-store' }); }
                catch { if (++misses > 60) throw new Error('Lost contact with the server.'); continue; }
                if (r.status === 404) throw new Error('The server lost track of this task (did it restart?).');
                if (!r.ok) { if (++misses > 60) throw new Error(`HTTP ${r.status}`); continue; }
                misses = 0;
                const job = await r.json();
                if (job.status === 'done') return job.result;
                if (job.status === 'error') throw new Error(typeof job.error === 'string' ? job.error : (job.error?.message || 'Failed'));
            }
        }
        // -------------------------------
        let activeAppId = null;
        let activeApp = null;
        let currentUser = null;
        // window.term and window.fitAddon are managed by initTerminal()/_termDestroy()
        let termSocket = null;
        let logInterval = null;
        let pollInterval = null;
        let currentBrowserPath = "";
        let fileClipboard = null; // {op, paths: [], filenames: []} or null
        let ctxTarget = null;
        let isMobileFileEditorOpen = false;
        let aceEditor = null;
        let _editorDirty = false;
        let _openFilePath = null;

        let selectedFiles = new Set();
        let lastSelectedPath = null;

        function escapeHtml(str) {
            if (str == null) return '';
            return String(str)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }
