/**
 * The create-app modal: file selection, drag and drop, and upload progress.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { showToast } from "../core/dom.js";
import { _lastAppListHash, loadApps, renderAppList, selectApp } from "./apps.js";
import { _showReAuthOverlay, checkAuth } from "./auth.js";
import { loadLogs } from "./logs.js";
import { startPolling } from "./polling.js";
import { closeModal } from "../ui/modals.js";
import { _featherReplace } from "../core/dom.js";

export let _createAppDroppedFiles = [];

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

export const createAppDropArea = document.getElementById('create-app-drop-area');

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

            const resultFiles = [];
            async function traverse(entry, pathPrefix) {
                if (entry.isFile) {
                    const file = await new Promise(r => entry.file(r));
                    file.customPath = pathPrefix + file.name;
                    resultFiles.push(file);
                } else if (entry.isDirectory) {
                    const dirReader = entry.createReader();
                    // simple fallback, real recursive might need robust array batching 
                    const readAll = async () => {
                        const localRes = [];
                        let partial;
                        do {
                            partial = await new Promise(r => dirReader.readEntries(r));
                            localRes.push(...partial);
                        } while (partial && partial.length > 0);
                        return localRes;
                    };
                    const subEntries = await readAll();
                    for (let i = 0; i < subEntries.length; i++) {
                        await traverse(subEntries[i], pathPrefix + entry.name + '/');
                    }
                }
            }
            for (const entry of entries) { await traverse(entry, ''); }
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

        if (_createAppDroppedFiles.length > 0) {
            for (const f of _createAppDroppedFiles) {
                fd.append('files', f, f.customPath);
            }
        } else {
            const fileInput = e.target.querySelector('input[name="files"]');
            if (fileInput && fileInput.files.length) {
                for (const f of fileInput.files) {
                    fd.append('files', f, f.webkitRelativePath || f.name);
                }
            }
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
        clearInterval(stageTimer);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'Creation failed' }));
            throw new Error(err.detail || 'Creation failed');
        }
        const newApp = await res.json();
        showToast('App created!', 'success');
        // Reset form before closing so it's clean next time
        e.target.reset();
        closeModal('create-app-modal');
        // Immediately inject the new app into the cache and force a sidebar refresh.
        // We reset the hash guard so renderAppList always re-paints the list —
        // without this the deduplication check can suppress the new entry.
        if (newApp?.id) {
            state._cachedApps = [...state._cachedApps.filter(a => a.id !== newApp.id), newApp];
            _lastAppListHash = ''; // force re-render
            renderAppList(state._cachedApps);
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
    if (state._editorDirty) { e.preventDefault(); e.returnValue = ''; }
});

document.addEventListener('visibilitychange', () => {
    const logsTabVisible = !document.getElementById('tab-logs').classList.contains('hidden');
    if (document.hidden) {
        if (state.logInterval) { clearInterval(state.logInterval); state.logInterval = null; }
    } else {
        // Page became visible — re-check auth first (handles iOS PWA session restore)
        // then resume live-polling and log streaming if they had stopped
        if (state.currentUser) {
            if (!state.pollInterval) {
                // Re-verify session is still good before resuming poll
                fetch(`${API}/auth/status`).then(r => r.json()).then(d => {
                    if (d.authenticated) {
                        startPolling();
                        if (logsTabVisible && state.activeAppId) { loadLogs(); state.logInterval = setInterval(loadLogs, 2500); }
                    } else {
                        _showReAuthOverlay();
                    }
                }).catch(() => { startPolling(); }); // network error — optimistically resume
            } else {
                if (logsTabVisible && state.activeAppId && !state.logInterval) { loadLogs(); state.logInterval = setInterval(loadLogs, 2500); }
            }
        }
    }
});

window.addEventListener('pageshow', (e) => {
    // e.persisted = true means restored from bfcache (iOS background)
    if (e.persisted && state.currentUser) {
        if (state.pollInterval) { clearInterval(state.pollInterval); state.pollInterval = null; }
        fetch(`${API}/auth/status`).then(r => r.json()).then(d => {
            if (d.authenticated) { startPolling(); }
            else { _showReAuthOverlay(); }
        }).catch(() => startPolling());
    }
});

checkAuth();

_featherReplace();
