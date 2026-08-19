/**
 * The file browser: tree navigation, selection, editing, upload and download.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { escapeHtml, showToast } from "../core/dom.js";
import { showContextMenu } from "../ui/contextmenu.js";
import { closeModal, openModal } from "../ui/modals.js";
import { _featherReplace } from "../core/dom.js";

export async function loadFiles(path = '') {
    state.currentBrowserPath = path || '';
    state.selectedFiles.clear(); // Clear selections on navigation
    state.lastSelectedPath = null;
    const pathEl = document.getElementById('current-browser-path');
    if (!state.currentBrowserPath) {
        pathEl.innerHTML = '';
    } else {
        const parts = state.currentBrowserPath.split('/');
        let html = '';
        let accum = '';
        parts.forEach((p, _i) => {
            accum += (accum ? '/' : '') + p;
            html += `<span class="opacity-50">/</span><span class="cursor-pointer hover:text-white transition" onclick="loadFiles('${escapeHtml(accum)}')">${escapeHtml(p)}</span>`;
        });
        pathEl.innerHTML = html;
    }
    let res;
    try { res = await fetch(`${API}/apps/${state.activeAppId}/files/tree?path=${encodeURIComponent(path)}`); }
    catch { showToast('Network error loading files.', 'error'); return; }
    if (!res.ok) { showToast('Failed to load file tree.', 'error'); return; }
    const files = await res.json();
    const tree = document.getElementById('file-tree');
    // Don't clear immediately to avoid flicker or empty state on network jitter
    let html = '';
    if (path !== '') {
        const parts = path.split('/'); parts.pop();
        const parent = parts.join('/');
        html += `<div class="back-btn file-row flex items-center gap-3 cursor-pointer p-2 mb-1 transition" data-parent="${escapeHtml(parent)}" style="color:var(--text3)">
        <div class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0" style="background:var(--surface2)"><i data-feather="corner-left-up" class="w-3.5 h-3.5"></i></div>
        <span class="text-sm font-medium">Back</span>
    </div>`;
    }
    files.forEach(f => {
        const icon = f.is_dir ? 'folder' : 'file-text';
        const iconColor = f.is_dir ? 'var(--text)' : 'var(--text3)';
        const ePath = escapeHtml(f.path), eName = escapeHtml(f.name), eType = f.is_dir ? 'dir' : 'file';
        const sizeStr = (!f.is_dir && f.size != null)
            ? (f.size < 1024 ? `${f.size}B` : f.size < 1024 * 1024 ? `${(f.size / 1024).toFixed(1)}KB` : `${(f.size / (1024 * 1024)).toFixed(1)}MB`)
            : '';
        const isTouch = 'ontouchstart' in window;
        html += `<div class="file-row mb-1 flex items-center gap-2 cursor-pointer p-2 rounded-lg group transition"
                 ${isTouch ? '' : 'draggable="true"'}
                 data-path="${ePath}" data-type="${eType}" data-name="${eName}" data-isdir="${f.is_dir ? '1' : '0'}">
                <div class="file-row-main flex items-center gap-2.5 flex-1 min-w-0">
                    <i data-feather="${icon}" class="w-4 h-4 shrink-0" style="color:${iconColor}"></i>
                    <div class="flex flex-col min-w-0">
                        <span class="truncate text-sm font-medium" style="color:var(--text)">${eName}</span>
                        ${sizeStr ? `<span class="text-[10px] mono" style="color:var(--text3)">${sizeStr}</span>` : ''}
                    </div>
                </div>
                <button class="file-ctx-btn p-1.5 -mr-1 rounded-lg opacity-0 group-hover:opacity-100 transition" style="color:var(--text3)">
                    <i data-feather="more-horizontal" class="w-3.5 h-3.5"></i>
                </button>
            </div>`;
    });
    tree.innerHTML = html || `<div class="p-8 text-center"><p class="text-xs" style="color:var(--text3)">This folder is empty</p></div>`;
    _featherReplace();

    // Back button — separate from .file-row rows so it never accidentally
    // triggers the file-open logic below.
    const backBtn = tree.querySelector('.back-btn');
    if (backBtn) {
        backBtn.addEventListener('click', e => {
            e.stopPropagation();
            loadFiles(backBtn.dataset.parent || '');
        });
        let _bLpt = null;
        backBtn.addEventListener('touchstart', _e => {
            _bLpt = setTimeout(() => { }, 600); // no context menu on back btn
        }, { passive: true });
        backBtn.addEventListener('touchend', e => {
            clearTimeout(_bLpt);
            e.preventDefault(); // suppress synthetic click on mobile
            loadFiles(backBtn.dataset.parent || '');
        }, { passive: false });
        backBtn.addEventListener('touchmove', () => clearTimeout(_bLpt), { passive: true });
    }

    // File/folder rows — exclude the back button
    tree.querySelectorAll('.file-row:not(.back-btn)').forEach(row => {
        const rPath = row.dataset.path, rName = row.dataset.name, rIsDir = row.dataset.isdir === '1', rType = row.dataset.type;

        // Helper to update selection visual
        const updateSelectionVisual = () => {
            row.classList.toggle('bg-[var(--surface2)]', state.selectedFiles.has(rPath));
            row.classList.toggle('ring-1', state.selectedFiles.has(rPath));
            row.classList.toggle('ring-[var(--border)]', state.selectedFiles.has(rPath));
        };
        updateSelectionVisual();

        // Desktop click
        row.addEventListener('click', e => {
            if (e.target.closest('.file-ctx-btn')) return;
            e.stopPropagation();

            if (e.ctrlKey || e.metaKey) {
                if (state.selectedFiles.has(rPath)) state.selectedFiles.delete(rPath);
                else state.selectedFiles.add(rPath);
                state.lastSelectedPath = rPath;
                updateSelectionVisual();
                return;
            }
            if (e.shiftKey && state.lastSelectedPath) {
                // Find range
                const rows = Array.from(tree.querySelectorAll('.file-row:not(.back-btn)'));
                const idx1 = rows.findIndex(r => r.dataset.path === state.lastSelectedPath);
                const idx2 = rows.findIndex(r => r === row);
                if (idx1 !== -1 && idx2 !== -1) {
                    const start = Math.min(idx1, idx2);
                    const end = Math.max(idx1, idx2);
                    for (let i = start; i <= end; i++) state.selectedFiles.add(rows[i].dataset.path);
                    // Re-render visuals for all rows
                    rows.forEach(r => {
                        const p = r.dataset.path;
                        r.classList.toggle('bg-[var(--surface2)]', state.selectedFiles.has(p));
                        r.classList.toggle('ring-1', state.selectedFiles.has(p));
                        r.classList.toggle('ring-[var(--border)]', state.selectedFiles.has(p));
                    });
                }
                return;
            }

            // Normal click clears selection unless clicking already selected row to drag
            state.selectedFiles.clear();
            state.selectedFiles.add(rPath);
            state.lastSelectedPath = rPath;

            if (rIsDir) loadFiles(rPath); else openFile(rPath, rName);
        });

        // Mobile tap — touchend with preventDefault stops the 300ms synthetic click
        let _lpt = null;
        row.addEventListener('touchstart', e => {
            _lpt = setTimeout(() => {
                _lpt = null;
                showContextMenu(e, { path: rPath, type: rType, name: rName });
            }, 600);
        }, { passive: true });
        row.addEventListener('touchend', e => {
            if (_lpt === null) return; // was a long-press, already handled
            clearTimeout(_lpt);
            _lpt = null;
            if (e.target.closest('.file-ctx-btn')) {
                e.preventDefault();
                showContextMenu(e, { path: rPath, type: rType, name: rName });
                return;
            }
            e.preventDefault(); // suppress synthetic click so we don't open twice
            if (rIsDir) loadFiles(rPath); else openFile(rPath, rName);
        }, { passive: false });
        row.addEventListener('touchmove', () => { clearTimeout(_lpt); _lpt = null; }, { passive: true });

        row.querySelector('.file-ctx-btn').addEventListener('click', e => { e.stopPropagation(); showContextMenu(e, { path: rPath, type: rType, name: rName }); });
        row.addEventListener('contextmenu', e => { e.preventDefault(); showContextMenu(e, { path: rPath, type: rType, name: rName }); });

        // Drag and drop to move (Desktop only)
        if (!('ontouchstart' in window)) {
            row.addEventListener('dragstart', e => {
                if (!state.selectedFiles.has(rPath)) {
                    state.selectedFiles.clear();
                    state.selectedFiles.add(rPath);
                    state.lastSelectedPath = rPath;
                    updateSelectionVisual();
                }
                const items = Array.from(state.selectedFiles).map(p => {
                    const name = p.split('/').pop() || p;
                    return { sourcePath: p, name };
                });
                const payload = JSON.stringify({ venvy_move: true, items });
                e.dataTransfer.setData('application/json', payload);
                e.dataTransfer.setData('text/plain', payload);
                e.dataTransfer.effectAllowed = 'move';
            });
        }

        if (rIsDir) {
            row.addEventListener('dragover', e => {
                e.preventDefault();
                row.classList.add('bg-[rgba(255,255,255,0.1)]');
            });
            row.addEventListener('dragleave', _e => { row.classList.remove('bg-[rgba(255,255,255,0.1)]'); });
            row.addEventListener('drop', async e => {
                e.preventDefault(); e.stopPropagation();
                row.classList.remove('bg-[rgba(255,255,255,0.1)]');

                let dataStr = null;
                try { dataStr = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text'); } catch { }

                if (dataStr && dataStr.includes('venvy_move')) {
                    try {
                        const data = JSON.parse(dataStr);
                        let successCount = 0;
                        const targetPath = rPath; // The folder we dropped ONTO
                        for (const item of data.items) {
                            const destPath = targetPath ? `${targetPath}/${item.name}` : item.name;
                            if (item.sourcePath === destPath || targetPath.startsWith(item.sourcePath + '/') || targetPath === item.sourcePath) continue;
                            const res = await fetch(`${API}/apps/${state.activeAppId}/files/op`, {
                                method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ operation: 'rename', path: item.sourcePath, new_path: destPath })
                            });
                            if (res.ok) successCount++;
                        }
                        if (successCount > 0) loadFiles(state.currentBrowserPath);
                    } catch { showToast('Move failed.', 'error'); }
                    return;
                }

                if (e.dataTransfer.files && e.dataTransfer.files.length > 0 && e.dataTransfer.items) {
                    if (e.dataTransfer.types && (e.dataTransfer.types.includes('Files') || e.dataTransfer.types.contains('Files') || Array.from(e.dataTransfer.types).includes('Files'))) {
                        const entries = Array.from(e.dataTransfer.items).map(i => i.webkitGetAsEntry()).filter(Boolean);
                        if (entries.length > 0) { processDroppedEntries(entries, rPath); return; }
                    }
                }
            });
        }
    });
}

export const _EDITOR_MODES = {
    py: 'python', js: 'javascript', ts: 'typescript', jsx: 'jsx', html: 'html', css: 'css',
    json: 'json', md: 'markdown', sh: 'sh', bash: 'sh', yaml: 'yaml', yml: 'yaml',
    txt: 'text', env: 'text', ini: 'ini', toml: 'toml', sql: 'sql', xml: 'xml', rs: 'rust', go: 'golang',
};

export function _setEditorDirty(dirty) {
    state._editorDirty = dirty;
    const indicator = document.getElementById('editor-dirty-indicator');
    if (indicator) indicator.classList.toggle('hidden', !dirty);
}

export function aceSearch() {
    if (!state.aceEditor) return;
    // Use the ext-searchbox API directly to avoid circular execCommand('find') loops.
    // ext-searchbox attaches its showFind method to the editor instance.
    if (state.aceEditor.searchBox && typeof state.aceEditor.searchBox.show === 'function') {
        state.aceEditor.searchBox.show(state.aceEditor.getCopyText() || '');
    } else {
        // Fallback: trigger via the searchbox extension command
        state.aceEditor.execCommand('find');
    }
}

export async function openFile(path, name) {
    if (path === state._openFilePath) {
        document.getElementById('editor-panel').classList.remove('translate-x-full');
        document.getElementById('file-tree-container').classList.add('slide-out-left', 'md:translate-x-0');
        state.isMobileFileEditorOpen = true;
        return;
    }
    if (state._editorDirty) { if (!confirm('Unsaved changes. Discard and open new file?')) return; }
    const filenameEl = document.getElementById('current-filename');
    filenameEl.innerText = 'Loading…'; filenameEl.dataset.path = ''; state._openFilePath = null;
    const res = await fetch(`${API}/apps/${state.activeAppId}/files/download?path=${encodeURIComponent(path)}`);
    if (!res.ok) { showToast("File not found or unreadable", 'error'); filenameEl.innerText = 'Error'; return; }
    const text = await res.text();
    if (state.aceEditor) {
        const ext = name.split('.').pop().toLowerCase();
        state.aceEditor.session.setMode('ace/mode/' + (_EDITOR_MODES[ext] || 'text'));
        state.aceEditor.setValue(text, -1);
        state.aceEditor.session.getUndoManager().reset();
    }
    filenameEl.innerText = name; filenameEl.dataset.path = path; state._openFilePath = path; _setEditorDirty(false);
    document.getElementById('editor-panel').classList.remove('translate-x-full');
    document.getElementById('file-tree-container').classList.add('slide-out-left', 'md:translate-x-0');
    state.isMobileFileEditorOpen = true;
    if (state.aceEditor) setTimeout(() => state.aceEditor.focus(), 100);
}

export function closeEditorMobile() {
    if (state._editorDirty) { if (!confirm('Unsaved changes. Discard?')) return; }
    document.getElementById('editor-panel').classList.add('translate-x-full');
    document.getElementById('file-tree-container').classList.remove('slide-out-left');
    state.isMobileFileEditorOpen = false;
    state._openFilePath = null; _setEditorDirty(false);
}

export async function saveCurrentFile() {
    const path = document.getElementById('current-filename').dataset.path;
    if (!path) { showToast('No file open.', 'warn'); return; }
    if (!state.aceEditor) { showToast('Editor not ready.', 'warn'); return; }
    const fileContent = (typeof state.aceEditor.getValue === 'function' ? state.aceEditor.getValue() : null) ?? '';
    const savedAppId = state.activeAppId; // capture in case user switches apps during the async save
    const btn = document.getElementById('editor-save-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }
    let success = false;
    try {
        const res = await fetch(`${API}/apps/${savedAppId}/files/op`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operation: 'save', path, content: fileContent }),
        });
        if (!res.ok) { const e = await res.json().catch(() => ({ detail: 'Save failed' })); throw new Error(e.detail); }
        success = true;
        // Only mark clean if we're still looking at the same file
        if (state.activeAppId === savedAppId && state._openFilePath === path) _setEditorDirty(false);
        showToast('Saved!', 'success', 1500);
        if (btn) {
            btn.textContent = '\u2713 Saved';
            btn.style.background = 'var(--green)';
            setTimeout(() => { if (btn) { btn.textContent = 'Save'; btn.style.background = ''; btn.disabled = false; } }, 1500);
        }
    } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
    finally {
        if (btn && !success) {
            btn.disabled = false;
            btn.textContent = 'Save';
            btn.style.background = '';
        }
    }
}

export function downloadCurrentFile() {
    const path = document.getElementById('current-filename').dataset.path;
    const name = document.getElementById('current-filename').innerText;
    if (!path) { showToast("No file selected.", 'warn'); return; }
    const link = document.createElement('a');
    link.href = `${API}/apps/${state.activeAppId}/files/download?path=${encodeURIComponent(path)}`;
    link.download = name || path.split('/').pop();
    link.style.display = 'none';
    document.body.appendChild(link); link.click(); document.body.removeChild(link);
}

export let _pendingFileOp = null, _pendingRenamePath = null;

export function promptFileOp(op) {
    _pendingFileOp = op; _pendingRenamePath = null;
    const isFolder = op === 'create_folder';
    document.getElementById('file-name-modal-title').textContent = isFolder ? 'New Folder' : 'New File';
    document.getElementById('file-name-confirm-btn').textContent = isFolder ? 'Create Folder' : 'Create File';
    document.getElementById('file-name-input').value = '';
    document.getElementById('file-name-input').placeholder = isFolder ? 'folder-name' : 'filename.py';
    openModal('file-name-modal');
    setTimeout(() => document.getElementById('file-name-input').focus(), 150);
}

export function promptRename(path, currentName) {
    _pendingFileOp = 'rename'; _pendingRenamePath = path;
    document.getElementById('file-name-modal-title').textContent = 'Rename';
    document.getElementById('file-name-confirm-btn').textContent = 'Rename';
    const input = document.getElementById('file-name-input');
    input.value = currentName; input.placeholder = currentName;
    openModal('file-name-modal');
    setTimeout(() => { input.focus(); input.select(); }, 150);
}

export async function confirmFileNameOp() {
    const name = document.getElementById('file-name-input').value.trim();
    if (!name) { document.getElementById('file-name-input').focus(); return; }
    closeModal('file-name-modal');
    const op = _pendingFileOp, renamePath = _pendingRenamePath;
    _pendingFileOp = null; _pendingRenamePath = null;
    try {
        let body;
        if (op === 'rename') {
            const parts = renamePath.split('/'); parts[parts.length - 1] = name;
            const newPath = parts.join('/');
            if (newPath === renamePath) return;
            body = { operation: 'rename', path: renamePath, new_path: newPath };
        } else {
            const fullPath = state.currentBrowserPath ? state.currentBrowserPath + '/' + name : name;
            body = { operation: op, path: fullPath };
        }
        const res = await fetch(`${API}/apps/${state.activeAppId}/files/op`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!res.ok) { const e = await res.json().catch(() => ({ detail: 'Failed' })); showToast('Operation failed: ' + e.detail, 'error'); }
        else if (op === 'rename') showToast(`Renamed to "${name}"`, 'success', 2000);
    } catch (e) { showToast('Operation failed: ' + e.message, 'error'); }
    loadFiles(state.currentBrowserPath);
}

document.addEventListener('DOMContentLoaded', () => {
    const inp = document.getElementById('file-name-input');
    if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') confirmFileNameOp(); if (e.key === 'Escape') { e.stopPropagation(); closeModal('file-name-modal'); } });
});

export async function deletePath(path) {
    const filename = path.split('/').pop() || path;
    const rowEl = document.querySelector(`[data-path="${CSS.escape(path)}"]`);
    const isDir = rowEl ? rowEl.dataset.isdir === '1' : true;
    if (!confirm(isDir
        ? `Delete folder "${filename}" and all its contents? This cannot be undone.`
        : `Delete "${filename}"? This cannot be undone.`)) return;
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/files/op`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operation: 'delete', path }) });
        if (res.ok) showToast(`Deleted "${filename}"`, 'info', 2000);
        else showToast('Delete failed.', 'error');
    } catch { showToast('Delete failed.', 'error'); }
    loadFiles(state.currentBrowserPath);
}

export async function deletePaths(items) {
    if (!items || !items.length) return;
    if (!confirm(`Delete ${items.length} item(s)? This cannot be undone.`)) return;
    let successCount = 0;
    for (const item of items) {
        try {
            const res = await fetch(`${API}/apps/${state.activeAppId}/files/op`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ operation: 'delete', path: item.path })
            });
            if (res.ok) successCount++;
        } catch (e) { console.error(`Failed to delete ${item.path}`, e); }
    }
    if (successCount > 0) showToast(`Deleted ${successCount} item(s)`, 'info', 2000);
    state.selectedFiles.clear();
    loadFiles(state.currentBrowserPath);
}

export let currentUploadXhr = null;

export function formatBytes(bytes, decimals = 1) {
    if (!+bytes) return '0 Bytes';
    const k = 1024, dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

export function showUploadProgress(title) {
    const container = document.getElementById('upload-progress-container');
    document.getElementById('upload-progress-title').textContent = title || 'Uploading...';
    document.getElementById('upload-progress-bar').style.width = '0%';
    document.getElementById('upload-progress-stats').textContent = 'Preparing...';
    document.getElementById('upload-progress-speed-time').textContent = '';

    container.classList.remove('hidden');
    // Small semantic reflow trick to trigger transition
    void container.offsetWidth;
    container.classList.remove('translate-y-full');

    document.getElementById('upload-progress-cancel').onclick = () => {
        if (currentUploadXhr) {
            currentUploadXhr.abort();
            hideUploadProgress();
            showToast('Upload cancelled.', 'warn');
            loadFiles(state.currentBrowserPath);
        }
    };
}

export function hideUploadProgress() {
    const container = document.getElementById('upload-progress-container');
    container.classList.add('translate-y-full');
    setTimeout(() => {
        container.classList.add('hidden');
        currentUploadXhr = null;
    }, 300);
}

export async function performXhrUpload(formData, targetPath, fileCount) {
    showUploadProgress(`Uploading ${fileCount} file${fileCount > 1 ? 's' : ''}`);

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        currentUploadXhr = xhr;

        const startTime = Date.now();
        let lastLoaded = 0;
        let lastTime = startTime;

        xhr.upload.addEventListener("progress", e => {
            if (e.lengthComputable) {
                const now = Date.now();
                const timeDiffSec = (now - lastTime) / 1000;

                if (timeDiffSec > 0.2 || e.loaded === e.total) { // Update roughly 5 times a sec
                    const deltaBytes = e.loaded - lastLoaded;
                    const bytesPerSec = deltaBytes / timeDiffSec;
                    const remainingBytes = e.total - e.loaded;
                    const remainingTimeSec = bytesPerSec > 0 ? Math.round(remainingBytes / bytesPerSec) : 0;

                    const percent = Math.round((e.loaded / e.total) * 100);

                    document.getElementById('upload-progress-bar').style.width = `${percent}%`;
                    document.getElementById('upload-progress-stats').textContent = `${percent}% (${formatBytes(e.loaded)} / ${formatBytes(e.total)})`;
                    document.getElementById('upload-progress-speed-time').textContent = `${formatBytes(bytesPerSec)}/s • ${remainingTimeSec}s left`;

                    lastLoaded = e.loaded;
                    lastTime = now;
                }
            }
        });

        xhr.addEventListener("load", () => {
            hideUploadProgress();
            if (xhr.status >= 200 && xhr.status < 300) {
                showToast(`Uploaded ${fileCount} file(s) successfully.`, 'success', 2000);
                resolve();
            } else {
                try {
                    const err = JSON.parse(xhr.responseText);
                    reject(new Error(err.detail || 'Upload failed'));
                } catch {
                    reject(new Error('Upload failed'));
                }
            }
        });

        xhr.addEventListener("error", () => {
            hideUploadProgress();
            reject(new Error('Network error during upload'));
        });

        xhr.addEventListener("abort", () => {
            // Handled synchronously in cancel click
            reject(new Error('Upload aborted'));
        });

        xhr.open("POST", `${API}/apps/${state.activeAppId}/files/upload`);
        const tokenMatch = document.cookie.match(/(?:^|;\\s*)csrf_token=([^;]*)/);
        if (tokenMatch) {
            xhr.setRequestHeader('x-csrf-token', decodeURIComponent(tokenMatch[1]));
        }
        xhr.send(formData);
    });
}

export async function uploadFile(input) {
    if (!input.files.length) return;
    const formData = new FormData();
    for (const f of input.files) formData.append('files', f, f.webkitRelativePath || f.name);
    formData.append('path', state.currentBrowserPath);
    try {
        await performXhrUpload(formData, state.currentBrowserPath, input.files.length);
    } catch (e) {
        if (e.message !== 'Upload aborted') {
            showToast('Upload failed: ' + e.message, 'error');
        }
    } finally {
        input.value = '';
        loadFiles(state.currentBrowserPath);
    }
}

export async function processDroppedEntries(entries, targetPath) {
    const resultFiles = [];
    async function traverse(entry, pathPrefix) {
        if (entry.isFile) {
            const file = await new Promise(r => entry.file(r));
            file.customPath = pathPrefix + file.name;
            resultFiles.push(file);
        } else if (entry.isDirectory) {
            const dirReader = entry.createReader();
            const subEntries = await new Promise(r => dirReader.readEntries(r));
            for (let i = 0; i < subEntries.length; i++) {
                await traverse(subEntries[i], pathPrefix + entry.name + '/');
            }
        }
    }
    for (const entry of entries) { await traverse(entry, ''); }
    if (resultFiles.length === 0) return;

    const formData = new FormData();
    for (const f of resultFiles) formData.append('files', f, f.customPath);
    formData.append('path', targetPath);

    try {
        await performXhrUpload(formData, targetPath, resultFiles.length);
    } catch (e) {
        if (e.message !== 'Upload aborted') {
            showToast('Upload failed: ' + e.message, 'error');
        }
    } finally {
        loadFiles(targetPath);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const treeContainer = document.getElementById('file-tree-container');
    const dropOverlay = document.getElementById('file-drop-overlay');
    let dragCounter = 0;

    if (treeContainer) {
        // Background click to clear selection
        treeContainer.addEventListener('click', e => {
            if (!e.target.closest('.file-row') && !e.target.closest('.file-ctx-btn') && !e.target.closest('#ctx-menu')) {
                state.selectedFiles.clear();
                state.lastSelectedPath = null;
                document.querySelectorAll('.file-row').forEach(r => {
                    r.classList.remove('bg-[var(--surface2)]', 'ring-1', 'ring-[var(--border)]');
                });
            }
        });

        treeContainer.addEventListener('dragenter', e => {
            e.preventDefault();
            if (e.dataTransfer.types.includes('Files')) {
                dragCounter++;
                if (dragCounter === 1 && dropOverlay) {
                    dropOverlay.classList.remove('hidden');
                    // slight delay to allow display:block to apply before opacity transition
                    requestAnimationFrame(() => dropOverlay.classList.remove('opacity-0'));
                    const icon = dropOverlay.querySelector('.w-16');
                    if (icon) icon.classList.replace('scale-90', 'scale-100');
                }
            }
        });

        treeContainer.addEventListener('dragleave', e => {
            e.preventDefault();
            if (e.dataTransfer.types.includes('Files')) {
                dragCounter--;
                if (dragCounter === 0 && dropOverlay) {
                    dropOverlay.classList.add('opacity-0');
                    const icon = dropOverlay.querySelector('.w-16');
                    if (icon) icon.classList.replace('scale-100', 'scale-90');
                    setTimeout(() => dropOverlay.classList.add('hidden'), 200);
                }
            }
        });

        treeContainer.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });

        treeContainer.addEventListener('drop', async e => {
            e.preventDefault();
            dragCounter = 0;
            if (dropOverlay) {
                dropOverlay.classList.add('opacity-0');
                setTimeout(() => dropOverlay.classList.add('hidden'), 200);
            }

            let dataStr = null;
            try { dataStr = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text'); } catch { }

            if (dataStr && dataStr.includes('venvy_move')) {
                try {
                    const data = JSON.parse(dataStr);
                    let successCount = 0;
                    const targetPath = state.currentBrowserPath;
                    for (const item of data.items) {
                        const destPath = targetPath ? `${targetPath}/${item.name}` : item.name;
                        if (item.sourcePath === destPath || targetPath.startsWith(item.sourcePath + '/') || targetPath === item.sourcePath) continue;
                        const res = await fetch(`${API}/apps/${state.activeAppId}/files/op`, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ operation: 'rename', path: item.sourcePath, new_path: destPath })
                        });
                        if (res.ok) successCount++;
                    }
                    if (successCount > 0) loadFiles(state.currentBrowserPath);
                } catch { showToast('Move failed.', 'error'); }
                return;
            }

            if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
                const entries = Array.from(e.dataTransfer.items).map(i => i.webkitGetAsEntry()).filter(Boolean);
                if (entries.length > 0) processDroppedEntries(entries, state.currentBrowserPath);
            } else if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                const input = document.createElement('input'); input.type = 'file'; input.files = e.dataTransfer.files;
                uploadFile(input);
            }
        });
    }
});
