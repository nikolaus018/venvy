// User feedback: screen-reader announcements, toasts, the confirm dialog, app status badges.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Screen-reader announcements ──────────────────────────────────────────
        function announce(msg) {
            const el = document.getElementById('sr-live');
            if (!el) return;
            // Clearing first forces AT to re-read even if the text is identical
            el.textContent = '';
            setTimeout(() => { el.textContent = msg; }, 30);
        }

        // ── Toast ────────────────────────────────────────────────────────────────
        // Errors and warnings never auto-dismiss (you need time to read a traceback),
        // wrap onto multiple lines, and their text is selectable so it can be copied.
        const _TOAST_ICONS = {
            success: '<path d="M20 6L9 17l-5-5" stroke-linecap="round" stroke-linejoin="round"/>',
            error: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5m0 3h.01" stroke-linecap="round"/>',
            warn: '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><path d="M12 9v4m0 4h.01" stroke-linecap="round"/>',
            info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5m0-3h.01" stroke-linecap="round"/>',
        };
        const _TOAST_COLORS = { success: 'var(--green)', error: 'var(--red)', warn: 'var(--amber)', info: 'var(--accent)' };
        const _MAX_TOASTS = 4;
        let _lastToast = { msg: '', at: 0, el: null };

        function _dismissToast(toast) {
            if (!toast || toast.dataset.dismissed === '1') return;
            toast.dataset.dismissed = '1';
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 250);
        }

        function showToast(msg, type = 'info', duration = null, opts = {}) {
            const container = document.getElementById('toast-container');
            if (!container) return null;
            msg = String(msg ?? '');

            // Sticky by default for anything the user must actually read.
            const sticky = duration === 0 || (duration == null && (type === 'error' || type === 'warn'));
            if (duration == null) duration = 3000;

            // Collapse a repeat of the same message fired within 1.5s into a counter.
            const now = Date.now();
            if (_lastToast.el && _lastToast.el.isConnected && _lastToast.msg === msg && now - _lastToast.at < 1500) {
                _lastToast.at = now;
                const badge = _lastToast.el.querySelector('.toast-repeat');
                const n = parseInt(badge?.dataset.n || '1', 10) + 1;
                if (badge) { badge.dataset.n = String(n); badge.textContent = '\u00d7' + n; badge.classList.remove('hidden'); }
                return _lastToast.el;
            }

            // Cap the stack so a burst of errors can't cover the whole screen.
            while (container.children.length >= _MAX_TOASTS) _dismissToast(container.firstElementChild);

            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

            const icon = document.createElement('span');
            icon.className = 'toast-icon';
            icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="${_TOAST_COLORS[type] || _TOAST_COLORS.info}" stroke-width="2.2" width="15" height="15">${_TOAST_ICONS[type] || _TOAST_ICONS.info}</svg>`;

            const body = document.createElement('span');
            body.className = 'toast-msg';
            body.textContent = msg;
            const repeat = document.createElement('span');
            repeat.className = 'toast-repeat hidden ml-1 text-[11px] font-bold opacity-60';
            repeat.dataset.n = '1';
            body.appendChild(repeat);

            toast.appendChild(icon);
            toast.appendChild(body);

            if (opts.actionLabel && typeof opts.onAction === 'function') {
                const act = document.createElement('button');
                act.type = 'button';
                act.className = 'toast-action';
                act.textContent = opts.actionLabel;
                act.onclick = (e) => { e.stopPropagation(); _dismissToast(toast); opts.onAction(); };
                toast.appendChild(act);
            }

            const close = document.createElement('button');
            close.type = 'button';
            close.className = 'toast-close';
            close.setAttribute('aria-label', 'Dismiss notification');
            close.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="12" height="12"><path d="M18 6L6 18M6 6l12 12" stroke-linecap="round"/></svg>';
            close.onclick = (e) => { e.stopPropagation(); _dismissToast(toast); };
            toast.appendChild(close);

            container.appendChild(toast);
            requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('show')));
            announce(msg);

            _lastToast = { msg, at: now, el: toast };

            if (!sticky) {
                let timer = setTimeout(() => _dismissToast(toast), duration);
                // Hovering pauses the countdown so a toast can't vanish mid-read.
                toast.addEventListener('mouseenter', () => clearTimeout(timer));
                toast.addEventListener('mouseleave', () => { timer = setTimeout(() => _dismissToast(toast), 1200); });
            }
            return toast;
        }

        function dismissAllToasts() {
            document.querySelectorAll('#toast-container .toast').forEach(_dismissToast);
        }

        // ── Confirm dialog (themed replacement for window.confirm) ───────────────
        let _confirmPending = null;

        function _confirmResolve(value) {
            const fn = _confirmPending;
            _confirmPending = null;
            closeModal('confirm-modal');
            if (fn) fn(value);
        }

        // Returns a promise resolving true/false. Escape / backdrop resolve false.
        function confirmDialog({ title, body, sub = '', confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false, icon = null }) {
            // Resolve any dialog already open so we never orphan a promise.
            if (_confirmPending) _confirmResolve(false);
            return new Promise(resolve => {
                _confirmPending = resolve;
                document.getElementById('confirm-modal-title').textContent = title;
                document.getElementById('confirm-modal-sub').textContent = sub;
                document.getElementById('confirm-modal-body').textContent = body || '';
                document.getElementById('confirm-modal-body').classList.toggle('hidden', !body);

                const iconWrap = document.getElementById('confirm-modal-icon');
                const tone = danger ? 'var(--red-rgb)' : 'var(--accent-rgb)';
                iconWrap.style.cssText = `background:rgba(${danger ? '239,68,68' : 'var(--accent-rgb)'},0.1);border:1px solid rgba(${danger ? '239,68,68' : 'var(--accent-rgb)'},0.22)`;
                iconWrap.innerHTML = `<i data-feather="${icon || (danger ? 'trash-2' : 'help-circle')}" class="w-5 h-5" style="color:${danger ? 'var(--red)' : 'var(--accent-light)'}"></i>`;

                const ok = document.getElementById('confirm-modal-ok');
                ok.textContent = confirmLabel;
                ok.className = (danger ? 'btn-danger-solid' : 'btn-primary') + ' flex-1 py-2.5 text-sm font-semibold';
                document.getElementById('confirm-modal-cancel').textContent = cancelLabel;

                openModal('confirm-modal');
                _featherReplace();
                // Focus the safe choice on destructive prompts so a stray Enter
                // cancels rather than deletes.
                const initial = danger ? document.getElementById('confirm-modal-cancel') : ok;
                setTimeout(() => initial.focus(), 60);
            });
        }

        // ── App status presentation ──────────────────────────────────────────────
        // The backend reports running / stopped / error. A crashed app must never
        // look like one the user deliberately stopped.
        function statusMeta(status) {
            switch (status) {
                case 'running':
                    return { label: 'Running', cls: 'status-running', dot: 'var(--green)', dotCls: 'pulse-dot', crashed: false };
                case 'error':
                case 'crashed':
                case 'failed':
                    return { label: 'Crashed', cls: 'status-error', dot: 'var(--red)', dotCls: 'crash-dot', crashed: true };
                case 'starting':
                case 'restarting':
                    return { label: 'Starting', cls: 'status-starting', dot: 'var(--amber)', dotCls: 'pulse-dot', crashed: false };
                case 'installing':
                    return { label: 'Installing', cls: 'status-starting', dot: 'var(--amber)', dotCls: 'pulse-dot', crashed: false };
                case 'stale':
                    return { label: 'Stale', cls: 'status-stale', dot: 'var(--red)', dotCls: '', crashed: false };
                default:
                    return { label: 'Stopped', cls: 'status-stopped', dot: 'var(--border3)', dotCls: '', crashed: false };
            }
        }

        const _BADGE_BASE = 'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide ';

        function applyStatusBadge(el, status) {
            if (!el) return;
            const m = statusMeta(status);
            el.innerText = m.label;
            el.className = _BADGE_BASE + m.cls;
            el.title = m.crashed ? 'The process exited unexpectedly — check the Logs tab.' : '';
        }
