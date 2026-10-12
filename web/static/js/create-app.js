// New-application modal, plus page lifecycle handlers (unload guard, visibility, bfcache resume).
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Create App Modal Logic ────────────────────────────────────────────────
        let _createAppDroppedFiles = [];

        document.getElementById('create-form').addEventListener('reset', () => {
            _createAppDroppedFiles = [];
            document.getElementById('create-app-files-text').innerHTML = `Drop files/folders here<br><span class="text-xs" style="color:var(--text3)">or click to browse</span>`;
        });

        document.getElementById('create-app-file-input').addEventListener('change', e => {
            _createAppDroppedFiles = []; // overriding drop with normal select
            const count = e.target.files.length;
            document.getElementById('create-app-files-text').innerHTML = count > 0
                ? `<span style="color:var(--text)">${count} file(s) selected</span>`
                : `Drop files/folders here<br><span class="text-xs" style="color:var(--text3)">or click to browse</span>`;
        });

        const createAppDropArea = document.getElementById('create-app-drop-area');
        createAppDropArea.addEventListener('dragover', e => {
            e.preventDefault();
            createAppDropArea.style.borderColor = 'var(--accent)';
            createAppDropArea.style.background = 'rgba(var(--accent-rgb),0.1)';
        });
        createAppDropArea.addEventListener('dragleave', e => {
            e.preventDefault();
            createAppDropArea.style.borderColor = 'var(--border)';
            createAppDropArea.style.background = 'var(--surface2)';
        });
        createAppDropArea.addEventListener('drop', async e => {
            e.preventDefault();
            createAppDropArea.style.borderColor = 'var(--border)';
            createAppDropArea.style.background = 'var(--surface2)';

            if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
                const entries = Array.from(e.dataTransfer.items).map(i => i.webkitGetAsEntry()).filter(Boolean);
                if (entries.length > 0) {
                    document.getElementById('create-app-files-text').innerHTML = `<svg class="w-4 h-4 animate-spin inline-block mb-1 mx-auto" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg><br><span class="text-xs" style="color:var(--text3)">Scanning folders…</span>`;

                    const resultFiles = await collectDroppedEntries(entries).catch(() => []);
                    _createAppDroppedFiles = resultFiles;

                    // Clear the native input to avoid double payloads
                    document.getElementById('create-app-file-input').value = '';
                    document.getElementById('create-app-files-text').innerHTML = `<span style="color:var(--text)">${resultFiles.length} file(s) loaded</span>`;
                }
            } else if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                document.getElementById('create-app-file-input').files = e.dataTransfer.files;
                _createAppDroppedFiles = [];
                document.getElementById('create-app-files-text').innerHTML = `<span style="color:var(--text)">${e.dataTransfer.files.length} file(s) selected</span>`;
            }
        });

        document.getElementById('create-form').onsubmit = async (e) => {
            e.preventDefault();
            const btn = e.target.querySelector('button[type="submit"]');
            const originalText = btn.innerText;

            // Show staged progress — venv creation + pip install can take 30-120s
            const stages = ['Creating…', 'Setting up venv…', 'Installing deps…', 'Almost done…'];
            let stageIdx = 0;
            btn.innerText = stages[0]; btn.disabled = true;
            const stageTimer = setInterval(() => {
                stageIdx = Math.min(stageIdx + 1, stages.length - 1);
                btn.innerText = stages[stageIdx];
            }, 8000);

            try {
                const fd = new FormData(e.target);
                fd.delete('files');

                // Files are uploaded in chunks after the app exists (see below),
                // not inside this request.
                let uploadItems = _createAppDroppedFiles;
                if (!uploadItems.length) {
                    const fileInput = e.target.querySelector('input[name="files"]');
                    uploadItems = Array.from(fileInput?.files || [], f => ({ file: f, relPath: f.webkitRelativePath || f.name }));
                }

                // Increase fetch timeout for app creation since it involves venv/pip
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 600000); // 10 min
                const res = await fetch(`${API}/apps/upload`, {
                    method: 'POST',
                    body: fd,
                    signal: controller.signal
                });
                clearTimeout(timeoutId);
                // venv + pip run as a server-side job; wait for it here.
                const newApp = await waitForJob(res);
                clearInterval(stageTimer);
                if (uploadItems.length && newApp?.id) {
                    btn.innerText = 'Uploading files…';
                    clearInterval(stageTimer);
                    const up = await uploadFilesChunked(newApp.id, '', uploadItems, `Uploading ${uploadItems.length} file${uploadItems.length === 1 ? '' : 's'} to ${newApp.name}`);
                    if (up.failed.length || up.cancelled) {
                        reportUploadResult(up);
                        showToast('App created, but not all files were uploaded — retry them from the Files tab.', 'warn', 8000);
                    } else {
                        showToast(`App created with ${up.uploaded} file${up.uploaded === 1 ? '' : 's'}!`, 'success');
                    }
                } else {
                    showToast('App created!', 'success');
                }
                _createAppDroppedFiles = [];
                // Reset form before closing so it's clean next time
                e.target.reset();
                closeModal('create-app-modal');
                // Immediately inject the new app into the cache and force a sidebar refresh.
                // We reset the hash guard so renderAppList always re-paints the list —
                // without this the deduplication check can suppress the new entry.
                if (newApp?.id) {
                    _cachedApps = [..._cachedApps.filter(a => a.id !== newApp.id), newApp];
                    _lastAppListHash = ''; // force re-render
                    renderAppList(_cachedApps);
                    selectApp(newApp.id);
                }
                // Background refresh to ensure data is authoritative
                loadApps();
            } catch (err) {
                clearInterval(stageTimer);
                showToast('Creation failed: ' + (err.message || err), 'error');
            } finally {
                btn.innerText = originalText; btn.disabled = false;
            }
        };

        window.addEventListener('beforeunload', (e) => {
            if (_editorDirty || isSettingsDirty() || _activeUpload) { e.preventDefault(); e.returnValue = ''; }
        });
        document.addEventListener('visibilitychange', () => {
            const logsTabVisible = !document.getElementById('tab-logs').classList.contains('hidden');
            if (document.hidden) {
                if (logInterval) { clearInterval(logInterval); logInterval = null; }
            } else {
                // Page became visible — re-check auth first (handles iOS PWA session restore)
                // then resume live-polling and log streaming if they had stopped
                if (currentUser) {
                    if (!pollInterval) {
                        // Re-verify session is still good before resuming poll
                        fetch(`${API}/auth/status`).then(r => r.json()).then(d => {
                            if (d.authenticated) {
                                startPolling();
                                if (logsTabVisible && activeAppId) startLogPolling();
                            } else {
                                _showReAuthOverlay();
                            }
                        }).catch(() => { startPolling(); }); // network error — optimistically resume
                    } else {
                        if (logsTabVisible && activeAppId && !logInterval) startLogPolling();
                    }
                }
            }
        });

        // iOS Safari fires 'pageshow' when restoring a PWA from the background
        // (bfcache / app switcher resume). The polling interval is dead at this point —
        // we re-check auth and restart it without any visual flash.
        window.addEventListener('pageshow', (e) => {
            // e.persisted = true means restored from bfcache (iOS background)
            if (e.persisted && currentUser) {
                if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
                fetch(`${API}/auth/status`).then(r => r.json()).then(d => {
                    if (d.authenticated) { startPolling(); }
                    else { _showReAuthOverlay(); }
                }).catch(() => startPolling());
            }
        });
