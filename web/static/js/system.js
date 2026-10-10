// System view: host metrics and ports, backups, restore.
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
            loadSystemView(); loadCfCredsStatus(); loadBackups(); loadGhCardStatus();
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
            const summaryEl = document.getElementById('sys-backups-summary');
            const seq = ++_backupsReqSeq;
            try {
                const res = await fetch(`${API}/system/backups`);
                if (!res.ok) throw new Error('Failed');
                const backups = await res.json();
                // Create/delete/refresh can overlap — drop this response if a
                // newer request has since been issued, so a slow reply can't
                // clobber a fresher list with stale data.
                if (seq !== _backupsReqSeq) return;

                if (summaryEl) {
                    summaryEl.textContent = backups.length
                        ? `${backups.length} snapshot${backups.length !== 1 ? 's' : ''} · latest ${new Date(backups[0].created_at).toLocaleDateString()}`
                        : 'No backups yet';
                }

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
                    if (summaryEl) summaryEl.textContent = 'Failed to load';
                }
            }
        }

        function openBackupsModal() {
            openModal('backups-modal');
            loadBackups();
        }

        async function createBackup(btn) {
            const original = btn ? btn.innerHTML : '';
            if (btn) {
                btn.disabled = true;
                // A full backup tars the whole apps/ directory, so this can
                // take a while for apps with large venvs — say so up front.
                btn.innerHTML = '<i data-feather="loader" class="w-3.5 h-3.5 animate-spin"></i> Creating… this can take a bit';
                _featherReplace();
            }
            try {
                const res = await fetch(`${API}/system/backup`, { method: 'POST' });
                await waitForJob(res);
                showToast('Backup created.', 'success');
                await loadBackups();
            } catch (e) {
                showToast('Backup failed: ' + e.message, 'error');
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = original;
                    _featherReplace();
                }
            }
        }

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

        function openRestoreBackupModal(filename) {
            _pendingRestoreFilename = filename;
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
                closeModal('restore-backup-modal');
                closeModal('backups-modal');
                _showRestoreInProgressOverlay(res);
            } catch (e) {
                showToast('Network error: ' + e.message, 'error');
                btn.disabled = false;
                btn.innerHTML = original;
            }
        }

        async function _showRestoreInProgressOverlay(res) {
            const overlay = document.getElementById('restore-progress-overlay');
            overlay.classList.remove('hidden');
            overlay.classList.add('flex');
            // The server is about to restart — the normal 1s app-list poll
            // would just spam connection-refused errors while it's down.
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
            // The restore runs as a job; the server only restarts once it's done.
            try { await waitForJob(res); }
            catch (e) {
                overlay.classList.add('hidden'); overlay.classList.remove('flex');
                showToast('Restore failed: ' + e.message, 'error', 10000);
                return;
            }
            document.getElementById('restore-progress-text').textContent = 'Restored — waiting for Venvy to restart…';
            await new Promise(r => setTimeout(r, 5000));
            let attempts = 0;
            const check = setInterval(async () => {
                attempts++;
                try {
                    const res = await fetch(`${API}/auth/status`, { cache: 'no-store' });
                    if (res.ok) {
                        clearInterval(check);
                        document.getElementById('restore-progress-text').textContent = 'Back online — reloading…';
                        setTimeout(() => window.location.reload(), 600);
                    }
                } catch (e) { /* still down — keep polling */ }
                if (attempts > 60) { // ~2 minutes at 2s
                    clearInterval(check);
                    document.getElementById('restore-progress-text').textContent =
                        "Still waiting for Venvy to come back — you can also just refresh this page manually.";
                }
            }, 2000);
        }
