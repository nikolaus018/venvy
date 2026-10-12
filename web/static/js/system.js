// System view: host metrics and ports, backups (create, upload, restore) and their live status.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── System View ───────────────────────────────────────────────────────────
        function _isSystemViewActive() {
            return !document.getElementById('system-view').classList.contains('hidden');
        }
        function openSystemView(userTriggered = false) {
            document.getElementById('empty-state').classList.add('hidden');
            document.getElementById('dashboard').classList.add('hidden');
            document.getElementById('system-view').classList.remove('hidden');
            document.getElementById('system-nav-btn').classList.add('active');
            if (userTriggered && window.innerWidth < 768) closeSidebar();
            document.title = 'System · Venvy';
            if (userTriggered) syncHash();
            loadSystemView(); loadCfCredsStatus(); loadBackups();
        }
        function closeSystemView() {
            document.getElementById('system-view').classList.add('hidden');
            document.getElementById('system-nav-btn').classList.remove('active');
            if (activeAppId) {
                document.getElementById('dashboard').classList.remove('hidden');
                document.title = `${activeApp?.name || 'App'} · Venvy`;
            } else {
                document.getElementById('empty-state').classList.remove('hidden');
                document.title = 'Venvy';
            }
            syncHash();
        }
        async function loadSystemView(btn = null) {
            if (btn) {
                btn.disabled = true;
                const icon = btn.querySelector('svg');
                if (icon) icon.classList.add('animate-spin');
            }
            const usedEl = document.getElementById('sys-used-ports');
            const freeEl = document.getElementById('sys-free-ports');
            const countEl = document.getElementById('sys-used-count');

            // Only show Loading state if the list is currently empty (initial load)
            if (!usedEl.innerHTML.includes('span')) {
                usedEl.innerHTML = `<span class="text-xs italic" style="color:var(--text3)">Loading…</span>`;
                freeEl.innerHTML = `<span class="text-xs italic" style="color:var(--text3)">Loading…</span>`;
            }

            try {
                const [pRes, mRes] = await Promise.all([
                    fetch(`${API}/system/ports`),
                    fetch(`${API}/system/metrics`).catch(() => null)
                ]);
                if (!pRes.ok) throw new Error('Failed');
                const data = await pRes.json();
                const listening = data.listening || [], free = data.free || [];
                countEl.textContent = `${listening.length} port${listening.length !== 1 ? 's' : ''} in use`;

                // Refresh metrics if available
                if (mRes && mRes.ok) {
                    const mData = await mRes.json();
                    document.getElementById('sys-cpu-val').textContent = `${mData.cpu.percent.toFixed(1)}%`;
                    document.getElementById('sys-cpu-bar').style.width = `${mData.cpu.percent}%`;
                    document.getElementById('sys-mem-val').textContent = `${mData.memory.percent.toFixed(1)}%`;
                    document.getElementById('sys-mem-bar').style.width = `${mData.memory.percent}%`;

                    const diskUsed = (mData.disk.used / 1024 / 1024 / 1024).toFixed(1);
                    const diskTotal = (mData.disk.total / 1024 / 1024 / 1024).toFixed(0);
                    document.getElementById('sys-disk-text').textContent = `${diskUsed} GB / ${diskTotal} GB (${mData.disk.percent}%)`;

                    const uptime = mData.uptime;
                    const h = Math.floor(uptime / 3600);
                    const m = Math.floor((uptime % 3600) / 60);
                    document.getElementById('sys-uptime-text').textContent = `${h}h ${m}m ${uptime % 60}s`;
                    _featherReplace();
                }

                const listeningHtml = !listening.length
                    ? `<span class="text-xs italic" style="color:var(--text3)">No listening ports found</span>`
                    : '<div class="flex flex-wrap gap-2">' + listening.map(p => {
                        const isManaged = !!p.app_name;
                        const style = isManaged
                            ? 'background:var(--surface2);color:var(--text);border:1px solid rgba(var(--accent-rgb),0.3)'
                            : 'background:var(--surface2);color:var(--text2);border:1px solid var(--border)';
                        return `<span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs mono font-bold" style="${style}">
                            ${p.port}${isManaged ? `<span class="font-sans font-medium text-[10px]" style="opacity:0.8">${escapeHtml(p.app_name)}</span>` : ''}
                        </span>`;
                    }).join('') + '</div>';

                if (usedEl.innerHTML !== listeningHtml) usedEl.innerHTML = listeningHtml;

                const freeHtml = !free.length
                    ? `<span class="text-xs italic" style="color:var(--text3)">No suggestions available</span>`
                    : '<div class="flex flex-wrap gap-2">' + free.map(port => `<button onclick="copyPortToClipboard(${port})" title="Click to copy"
                        class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs mono font-bold transition active:scale-95"
                        style="background:var(--ov-soft);color:var(--text);border:1px dashed var(--border3)">
                        ${port}
                        <svg class="w-3 h-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                    </button>`).join('') + '</div>';

                if (freeEl.innerHTML !== freeHtml) freeEl.innerHTML = freeHtml;

            } catch (e) {
                if (!usedEl.innerHTML.includes('rounded-lg')) {
                    usedEl.innerHTML = `<span class="text-xs" style="color:var(--red)">Failed to load</span>`;
                    freeEl.innerHTML = `<span class="text-xs" style="color:var(--red)">Failed to load</span>`;
                }
            } finally {
                if (btn) {
                    btn.disabled = false;
                    const icon = btn.querySelector('svg');
                    if (icon) icon.classList.remove('animate-spin');
                }
            }
        }

        // ── Backups ───────────────────────────────────────────────────────────────
        // Deliberately NOT called from loadSystemView() — that also runs on every
        // tick of the 1s live-metrics poll while System view is open, and backups
        // don't change that often. Call sites: opening System view, and the
        // Refresh button (both explicit user actions).
        let _backupsReqSeq = 0;
        async function loadBackups() {
            const card = document.getElementById('sys-backups-card');
            if (!card) return;
            // Admin-only feature server-side — don't show a card full of 403s to anyone else.
            if (currentUser?.role !== 'admin') { card.classList.add('hidden'); return; }
            card.classList.remove('hidden');

            const listEl = document.getElementById('sys-backups-list');
            const seq = ++_backupsReqSeq;
            try {
                const res = await fetch(`${API}/system/backups`);
                if (!res.ok) throw new Error('Failed');
                const backups = await res.json();
                // Create/delete/refresh can overlap — drop this response if a
                // newer request has since been issued, so a slow reply can't
                // clobber a fresher list with stale data.
                if (seq !== _backupsReqSeq) return;

                _backupsSummaryIdle = backups.length
                    ? `${backups.length} snapshot${backups.length !== 1 ? 's' : ''} · latest ${new Date(backups[0].created_at).toLocaleDateString()}`
                    : 'No backups yet';
                _paintBackupOp();

                if (!backups.length) {
                    listEl.innerHTML = `<span class="text-xs italic" style="color:var(--text3)">No backups yet — create one to get started.</span>`;
                    return;
                }

                // Filenames go in data-* attributes (read back via this.dataset),
                // never interpolated into onclick's JS string, so nothing about
                // a filename can break out of that context.
                listEl.innerHTML = backups.map(b => `
                    <div class="flex items-center gap-3 px-4 py-3 rounded-xl" style="background:var(--surface2);border:1px solid var(--border)">
                        <i data-feather="${b.type === 'full' ? 'archive' : 'database'}" class="w-4 h-4 shrink-0" style="color:var(--text3)"></i>
                        <div class="flex-1 min-w-0">
                            <p class="text-xs font-semibold mono truncate" style="color:var(--text)">${escapeHtml(b.filename)}</p>
                            <p class="text-[11px] mt-0.5" style="color:var(--text3)">${new Date(b.created_at).toLocaleString()} · ${formatBytes(b.size_bytes)}${b.type !== 'full' ? ' · legacy, database only' : ''}</p>
                        </div>
                        ${b.type === 'full' ? `
                        <button data-filename="${escapeHtml(b.filename)}" onclick="openRestoreBackupModal(this.dataset.filename)" title="Restore this backup"
                            class="shrink-0 p-1.5 rounded-lg transition active:scale-95" style="color:var(--amber)">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                                <polyline points="1 4 1 10 7 10" />
                                <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
                            </svg>
                        </button>` : ''}
                        <button data-filename="${escapeHtml(b.filename)}" onclick="downloadBackup(this.dataset.filename)" title="Download"
                            class="shrink-0 p-1.5 rounded-lg transition active:scale-95" style="color:var(--text)">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                                <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5 5 5-5M12 15V3" />
                            </svg>
                        </button>
                        <button data-filename="${escapeHtml(b.filename)}" onclick="deleteBackup(this.dataset.filename)" title="Delete"
                            class="shrink-0 p-1.5 rounded-lg transition active:scale-95" style="color:var(--red)">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                                <path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6M9 7V4a1 1 0 011-1h4a1 1 0 011 1v3M4 7h16" />
                            </svg>
                        </button>
                    </div>
                `).join('');
                _featherReplace();
            } catch (e) {
                if (seq === _backupsReqSeq) {
                    listEl.innerHTML = `<span class="text-xs" style="color:var(--red)">Failed to load backups.</span>`;
                    _backupsSummaryIdle = 'Failed to load';
                    _paintBackupOp();
                }
            }
        }

        function openBackupsModal() {
            openModal('backups-modal');
            loadBackups();
        }

        async function createBackup() {
            const btn = document.getElementById('btn-create-backup');
            if (btn) btn.disabled = true;   // re-enabled by _paintBackupOp() once nothing is running
            try {
                const res = await fetch(`${API}/system/backup`, { method: 'POST' });
                if (!res.ok) {
                    const e = await res.json().catch(() => ({}));
                    showToast('Backup not started: ' + (e.detail || `HTTP ${res.status}`), 'error', 6000);
                }
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
            }
            // Started or not, show what the server says is going on.
            await _pollBackupOp();
        }

        // ── Backup / restore status ───────────────────────────────────────────────
        // A backup or restore runs on the server and reports what it is doing at
        // GET /system/backup/status. That is polled here for as long as an admin
        // is signed in — not just by the tab that pressed the button — so the
        // progress is still there after closing the modal, reloading, or coming
        // back in another browser.
        let _backupOp = null;            // the server's last answer
        let _backupOpTimer = null;
        let _backupOpPollSeq = 0;        // a poll that was overtaken by a newer one steps aside
        let _backupOpWatching = false;
        let _backupOpWatchedId = null;   // an operation this page saw while it was still running
        let _backupOpOffline = 0;        // consecutive failed polls while the restore screen is up
        let _backupUpload = null;        // { name, total, sent, startedAt, ctl, id } while a backup file uploads
        let _backupsSummaryIdle = 'Loading…';
        const _BACKUP_OP_DISMISSED = 'venvy_backup_op_dismissed';

        function startBackupOpWatch() {
            if (currentUser?.role !== 'admin') return;   // the endpoint is admin-only
            _backupOpWatching = true;
            _pollBackupOp();
        }

        function stopBackupOpWatch() {
            _backupOpWatching = false;
            _backupOpPollSeq++;
            clearTimeout(_backupOpTimer);
        }

        function _restoreOverlayShown() {
            return !document.getElementById('restore-progress-overlay').classList.contains('hidden');
        }

        async function _pollBackupOp() {
            clearTimeout(_backupOpTimer);
            if (!_backupOpWatching) return;
            const seq = ++_backupOpPollSeq;
            let delay = 10000;   // idle: just often enough to notice one started elsewhere
            try {
                const res = await fetch(`${API}/system/backup/status`, { cache: 'no-store' });
                if (seq !== _backupOpPollSeq) return;
                if (res.status === 401 || res.status === 403) {
                    // A restore replaces the sessions too: if that is what just
                    // happened, the page has to start over at the login screen.
                    if (_restoreOverlayShown()) window.location.reload();
                    return;   // otherwise signed out — the next login starts the watch again
                }
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const body = await res.json();
                if (seq !== _backupOpPollSeq) return;
                _backupOpOffline = 0;
                _onBackupOp(body.operation);
                if (_backupOp?.status === 'running') delay = 1000;
            } catch (e) {
                if (seq !== _backupOpPollSeq) return;
                if (_restoreOverlayShown()) {
                    // Expected at the end of a restore: Venvy is restarting.
                    delay = 2000;
                    document.getElementById('restore-progress-text').textContent = ++_backupOpOffline > 90
                        ? 'Still waiting for Venvy to come back — you can also refresh this page yourself.'
                        : 'Venvy is restarting on the restored data — waiting for it to come back…';
                } else if (_backupOp?.status === 'running') {
                    delay = 2000;
                }
            }
            if (_backupOpWatching) {
                clearTimeout(_backupOpTimer);
                _backupOpTimer = setTimeout(_pollBackupOp, delay);
            }
        }

        function _onBackupOp(op) {
            _backupOp = op;
            const running = op?.status === 'running';
            if (running) _backupOpWatchedId = op.id;

            if (op?.kind === 'restore' && running) {
                _showRestoreOverlay(op);
            } else if (_restoreOverlayShown()) {
                if (op?.kind === 'restore' && op.status === 'done') {
                    document.getElementById('restore-progress-text').textContent = 'Restore finished — reloading…';
                    _renderOpSteps(document.getElementById('restore-progress-steps'), op);
                    stopBackupOpWatch();
                    setTimeout(() => window.location.reload(), 800);
                    return;
                }
                // The restore failed before anything restarted; Venvy carries on as it was.
                const overlay = document.getElementById('restore-progress-overlay');
                overlay.classList.add('hidden'); overlay.classList.remove('flex');
                startPolling();
            }

            if (op && !running && _backupOpWatchedId === op.id) {
                _backupOpWatchedId = null;
                const what = op.kind === 'restore' ? 'Restore' : 'Backup';
                if (op.status === 'error') showToast(`${what} failed: ${op.error}`, 'error', 10000);
                else if (op.kind === 'backup') showToast(`Backup created — ${op.filename} (${formatBytes(op.size_bytes)}).`, 'success', 5000);
                loadBackups();
            }
            _paintBackupOp();
        }

        function _showRestoreOverlay(op) {
            const overlay = document.getElementById('restore-progress-overlay');
            if (!_restoreOverlayShown()) {
                overlay.classList.remove('hidden');
                overlay.classList.add('flex');
                // Everything is about to be replaced and the server restarts at
                // the end — the 1s app-list poll would only report errors.
                if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
                closeModal('restore-backup-modal');
                closeModal('backups-modal');
            }
            document.getElementById('restore-progress-file').textContent =
                `${op.filename} · started by ${op.started_by} · ${_formatDuration(op.elapsed_sec)}`;
            document.getElementById('restore-progress-text').textContent =
                'This runs on the server — closing this page does not stop it. It reloads by itself once Venvy is back.';
            _renderOpSteps(document.getElementById('restore-progress-steps'), op);
        }

        function _formatDuration(sec) {
            sec = Math.max(0, Math.round(sec || 0));
            if (sec < 60) return `${sec}s`;
            const pad = n => String(n).padStart(2, '0');
            if (sec < 3600) return `${Math.floor(sec / 60)}m ${pad(sec % 60)}s`;
            return `${Math.floor(sec / 3600)}h ${pad(Math.floor(sec % 3600 / 60))}m`;
        }

        const _OP_ICONS = {
            done: '<svg class="w-3.5 h-3.5" fill="none" stroke="var(--green)" viewBox="0 0 24 24" stroke-width="3"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" /></svg>',
            active: '<div class="w-3.5 h-3.5 border-2 rounded-full animate-spin" style="border-color:var(--ov-border);border-top-color:var(--accent)"></div>',
            failed: '<svg class="w-3.5 h-3.5" fill="none" stroke="var(--red)" viewBox="0 0 24 24" stroke-width="3"><path stroke-linecap="round" d="M6 6l12 12M18 6L6 18" /></svg>',
            todo: '<div class="w-1.5 h-1.5 rounded-full" style="background:var(--text3);opacity:.5"></div>',
        };

        // The step list of a backup/restore. Rebuilt only when the step or the
        // outcome changes; the line under the active step and its bar are
        // updated in place, so the spinner and the bar don't restart every second.
        function _renderOpSteps(el, op) {
            const key = `${op.id}:${op.status}:${op.step}`;
            if (el.dataset.key !== key) {
                el.dataset.key = key;
                el.innerHTML = op.steps.map((label, i) => {
                    const state = op.status === 'done' || i < op.step ? 'done'
                        : i > op.step ? 'todo' : op.status === 'error' ? 'failed' : 'active';
                    const now = state === 'active';
                    return `
                    <div class="flex items-start gap-2.5 py-1">
                        <div class="w-4 h-4 mt-px shrink-0 flex items-center justify-center">${_OP_ICONS[state]}</div>
                        <div class="flex-1 min-w-0">
                            <p class="text-xs ${now ? 'font-bold' : 'font-medium'}" style="color:var(${state === 'todo' ? '--text3' : state === 'failed' ? '--red' : '--text'})">${escapeHtml(label)}</p>
                            ${now ? `<p class="text-[11px] mt-0.5 truncate hidden" data-op-detail style="color:var(--text3)"></p>
                            <div class="op-bar mt-1.5"><div class="op-bar-fill" data-op-fill></div></div>` : ''}
                        </div>
                    </div>`;
                }).join('');
            }
            const detail = el.querySelector('[data-op-detail]');
            if (detail) {
                detail.textContent = op.detail || '';
                detail.classList.toggle('hidden', !op.detail);
            }
            _setOpBar(el.querySelector('[data-op-fill]'), op.percent);
        }

        // percent == null: the work can't be measured, so show movement instead of a number.
        function _setOpBar(fill, percent) {
            if (!fill) return;
            const unknown = percent == null;
            fill.classList.toggle('op-bar-indeterminate', unknown);
            fill.style.width = unknown ? '' : `${Math.max(2, Math.min(100, percent))}%`;
        }

        function dismissBackupOp() {
            try { if (_backupOp) localStorage.setItem(_BACKUP_OP_DISMISSED, _backupOp.id); } catch { }
            _paintBackupOp();
        }

        function _backupOpDismissed(op) {
            try { return localStorage.getItem(_BACKUP_OP_DISMISSED) === op.id; } catch { return false; }
        }

        // Everything on the page that reflects the status: the panel in the
        // Backups modal, its buttons, the Backups card and the System nav item.
        function _paintBackupOp() {
            const op = _backupOp, up = _backupUpload;
            const running = op?.status === 'running';
            const what = op?.kind === 'restore' ? 'Restore' : 'Backup';
            const pct = p => p == null ? '…' : ` ${p}%`;

            const busy = up ? `Uploading${pct(Math.floor(up.sent * 100 / up.total))}`
                : running ? `${op.kind === 'restore' ? 'Restoring' : 'Backing up'}${pct(op.kind === 'backup' ? op.percent : null)}` : '';
            const nav = document.getElementById('system-nav-op');
            nav.textContent = busy;
            nav.classList.toggle('hidden', !busy);
            const summary = document.getElementById('sys-backups-summary');
            if (summary) {
                summary.textContent = up ? `Uploading ${up.name} — ${Math.floor(up.sent * 100 / up.total)}%`
                    : running ? `${op.steps[op.step]}${op.percent != null ? ` — ${op.percent}%` : '…'}`
                        : _backupsSummaryIdle;
            }
            for (const id of ['btn-create-backup', 'btn-upload-backup']) {
                document.getElementById(id).disabled = running || !!up;
            }

            const panel = document.getElementById('backup-op-panel');
            const show = up || (op && (running || !_backupOpDismissed(op)));
            panel.classList.toggle('hidden', !show);
            if (!show) { panel.dataset.key = ''; return; }

            const key = up ? `upload:${up.id}` : `${op.id}:${op.status}`;
            if (panel.dataset.key !== key) {
                panel.dataset.key = key;
                const head = (title, color = '--text') => `
                    <div class="flex items-center justify-between gap-3 mb-2">
                        <p class="text-sm font-bold truncate" style="color:var(${color})">${escapeHtml(title)}</p>
                        <span class="text-[11px] mono shrink-0" data-op-side style="color:var(--text3)"></span>
                    </div>`;
                const dismiss = `<button onclick="dismissBackupOp()" class="btn-ghost shrink-0 px-2.5 py-1 text-[11px] font-bold rounded-lg">Dismiss</button>`;
                if (up) {
                    panel.innerHTML = head(`Uploading ${up.name}`) + `
                        <div class="op-bar"><div class="op-bar-fill" data-op-fill></div></div>
                        <p class="text-[11px] mt-2" data-op-detail style="color:var(--text3)"></p>
                        <div class="flex items-center justify-between gap-3 mt-2">
                            <p class="text-[11px]" style="color:var(--text3)">The upload comes from this tab — keep it open until it is done.</p>
                            <button onclick="cancelBackupUpload()" class="shrink-0 text-[11px] font-bold px-2.5 py-1 rounded-lg" style="color:var(--red)">Cancel</button>
                        </div>`;
                } else if (running) {
                    panel.innerHTML = head(op.kind === 'restore' ? `Restoring ${op.filename}` : 'Creating backup…') + `
                        <div data-op-steps></div>
                        <p class="text-[11px] mt-2" style="color:var(--text3)">Started by ${escapeHtml(op.started_by)}. This runs
                            on the server: you can close this window or the browser and come back — the progress will be here.</p>`;
                } else if (op.status === 'done') {
                    panel.innerHTML = `
                        <div class="flex items-center gap-3">
                            <div class="shrink-0">${_OP_ICONS.done.replace('w-3.5 h-3.5', 'w-4 h-4')}</div>
                            <div class="flex-1 min-w-0">
                                <p class="text-sm font-bold" style="color:var(--text)">${what} finished</p>
                                <p class="text-[11px] mt-0.5 break-words" style="color:var(--text3)">${escapeHtml(op.filename || '')}${op.kind === 'backup' && op.size_bytes ? ` · ${formatBytes(op.size_bytes)}` : ''} · took ${_formatDuration(op.elapsed_sec)} · ${new Date(op.finished_at).toLocaleString()}</p>
                            </div>
                            ${dismiss}
                        </div>`;
                } else {
                    panel.innerHTML = `
                        <div class="flex items-center justify-between gap-3 mb-2">
                            <p class="text-sm font-bold" style="color:var(--red)">${what} failed</p>
                            ${dismiss}
                        </div>
                        <div data-op-steps></div>
                        <p class="text-xs mt-2 break-words" style="color:var(--text2)">${escapeHtml(op.error || 'Unknown error')}</p>
                        <p class="text-[11px] mt-1" style="color:var(--text3)">${new Date(op.finished_at).toLocaleString()} · after ${_formatDuration(op.elapsed_sec)}</p>`;
                }
            }

            if (up) {
                const sent = Math.min(up.sent, up.total), secs = (Date.now() - up.startedAt) / 1000;
                const checking = sent >= up.total;
                _setOpBar(panel.querySelector('[data-op-fill]'), checking ? null : Math.floor(sent * 100 / up.total));
                panel.querySelector('[data-op-side]').textContent = checking ? '' : `${Math.floor(sent * 100 / up.total)}%`;
                panel.querySelector('[data-op-detail]').textContent = checking
                    ? 'Upload complete — the server is checking that this is a Venvy backup…'
                    : `${formatBytes(sent)} of ${formatBytes(up.total)}${secs > 2 ? ` · ${formatBytes(sent / secs)}/s` : ''}`;
                return;
            }
            const side = panel.querySelector('[data-op-side]');
            if (side) side.textContent = _formatDuration(op.elapsed_sec);
            const steps = panel.querySelector('[data-op-steps]');
            if (steps) _renderOpSteps(steps, op);
        }

        // ── Upload a backup ───────────────────────────────────────────────────────
        // Backups are meant to be kept somewhere else, so the one you want to
        // restore is often not on this server. Sent in chunks like any other
        // upload (see files.js); once the server has checked it, it is in the
        // list and the usual restore confirmation opens for it.
        async function uploadBackupFile(input) {
            const file = input.files[0];
            input.value = '';   // picking the same file again must fire onchange again
            if (!file || _backupUpload) return;
            if (!/\.(tar\.gz|tgz)$/i.test(file.name)) {
                showToast('Choose the .tar.gz file that downloading a backup gave you.', 'error', 6000);
                return;
            }
            const ctl = { cancelled: false, xhrs: new Set() };
            const up = _backupUpload = { name: file.name, total: file.size, sent: 0, startedAt: Date.now(), ctl, id: _newUploadId() };
            const progress = bytes => { up.sent = bytes; _paintBackupOp(); };
            const url = `${API}/system/backups/upload/chunk`;
            let offset = 0, retries = 0, result = null;
            _paintBackupOp();
            try {
                while (!result) {
                    if (ctl.cancelled) throw new Error('Upload aborted');
                    const end = Math.min(offset + UPLOAD_CHUNK_BYTES, file.size);
                    const fd = new FormData();
                    fd.append('upload_id', up.id);
                    fd.append('filename', file.name);
                    fd.append('offset', String(offset));
                    fd.append('total', String(file.size));
                    fd.append('chunk', file.slice(offset, end), 'chunk');
                    const start = offset, len = end - offset;
                    let r = null;
                    try {
                        r = await _postChunk(url, fd, frac => progress(start + frac * len), ctl);
                    } catch (e) {
                        if (ctl.cancelled) throw e;
                    }
                    if (r && r.status >= 200 && r.status < 300 && r.body) {
                        offset = r.body.received; retries = 0;
                        if (r.body.done) result = r.body;
                        progress(offset);
                        continue;
                    }
                    if (r && r.status === 409 && typeof r.body?.detail?.received === 'number') {
                        // A response got lost — carry on from what the server has.
                        offset = r.body.detail.received; progress(offset);
                        continue;
                    }
                    const retryable = !r || r.status >= 500 || r.status === 408 || r.status === 429;
                    if (!retryable || r.status === 507 || ++retries > UPLOAD_MAX_RETRIES) throw new Error(_uploadErrorText(r));
                    progress(start);
                    await new Promise(res => setTimeout(res, Math.min(1000 * 2 ** (retries - 1), 15000)));
                }
            } catch (e) {
                fetch(`${API}/system/backups/upload/${up.id}`, { method: 'DELETE' }).catch(() => { });
                _backupUpload = null;
                _paintBackupOp();
                if (ctl.cancelled) showToast('Upload cancelled.', 'info');
                else showToast('Upload failed: ' + e.message, 'error', 8000);
                return;
            }
            _backupUpload = null;
            _paintBackupOp();
            await loadBackups();
            showToast(`Backup uploaded — ${result.filename}.`, 'success', 5000);
            openRestoreBackupModal(result.filename, true);
        }

        function cancelBackupUpload() {
            const ctl = _backupUpload?.ctl;
            if (!ctl) return;
            ctl.cancelled = true;
            for (const xhr of ctl.xhrs) xhr.abort();
        }

        // A backup upload dies with the tab, unlike everything else here.
        window.addEventListener('beforeunload', e => { if (_backupUpload) { e.preventDefault(); e.returnValue = ''; } });

        function downloadBackup(filename) {
            triggerDownload(`${API}/system/backups/${encodeURIComponent(filename)}/download`, filename);
        }

        async function deleteBackup(filename) {
            const ok = await confirmDialog({
                title: 'Delete this backup?',
                sub: filename,
                body: 'This snapshot will be permanently removed. This cannot be undone.',
                confirmLabel: 'Delete',
                cancelLabel: 'Cancel',
                danger: true,
                icon: 'trash-2',
            });
            if (!ok) return;
            try {
                const res = await fetch(`${API}/system/backups/${encodeURIComponent(filename)}`, { method: 'DELETE' });
                if (!res.ok) {
                    const e = await res.json().catch(() => ({ detail: 'Delete failed' }));
                    showToast(e.detail || 'Delete failed', 'error');
                    return;
                }
                showToast('Backup deleted.', 'success');
                loadBackups();
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
            }
        }

        // ── Restore ───────────────────────────────────────────────────────────────
        let _pendingRestoreFilename = null;

        function openRestoreBackupModal(filename, justUploaded = false) {
            _pendingRestoreFilename = filename;
            document.getElementById('restore-backup-uploaded-note').classList.toggle('hidden', !justUploaded);
            document.getElementById('restore-backup-filename').textContent = filename;
            document.getElementById('restore-backup-confirm-input').value = '';
            document.getElementById('restore-backup-confirm-btn').disabled = true;
            openModal('restore-backup-modal');
        }

        function onRestoreConfirmInput() {
            document.getElementById('restore-backup-confirm-btn').disabled =
                document.getElementById('restore-backup-confirm-input').value !== 'RESTORE';
        }

        async function confirmRestoreBackup() {
            if (!_pendingRestoreFilename) return;
            const btn = document.getElementById('restore-backup-confirm-btn');
            const original = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<div class="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin mx-auto"></div>';
            try {
                const res = await fetch(`${API}/system/backup/restore/${encodeURIComponent(_pendingRestoreFilename)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirm: 'RESTORE' }),
                });
                if (!res.ok) {
                    const e = await res.json().catch(() => ({ detail: 'Restore failed' }));
                    showToast(e.detail || 'Restore failed', 'error', 6000);
                    btn.disabled = false;
                    btn.innerHTML = original;
                    return;
                }
                btn.innerHTML = original;
                // The status poll takes it from here: it puts up the restore
                // screen and follows the restore through the restart.
                await _pollBackupOp();
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
                btn.disabled = false;
                btn.innerHTML = original;
            }
        }
