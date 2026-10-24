// Files tab: tree, editor (open/save/download), file operations, chunked uploads, drag and drop.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Files ─────────────────────────────────────────────────────────────────
        // One request to the file API for a change (create, rename, copy,
        // delete). Resolves to null on success, otherwise to why it failed.
        async function _fileOp(appId, body) {
            try {
                const res = await fetch(`${API}/apps/${appId}/files/op`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (res.ok) return null;
                const e = await res.json().catch(() => ({}));
                return (typeof e.detail === 'string' && e.detail) || `HTTP ${res.status}`;
            } catch (e) { return 'network error'; }
        }

        function _reportFileOpFailures(verb, failed) {
            if (!failed.length) return;
            const shown = failed.slice(0, 3).map(f => `${f.name} (${f.error})`).join('; ');
            showToast(`Could not ${verb} ${shown}${failed.length > 3 ? ` …and ${failed.length - 3} more` : ''}`, 'error');
        }

        // The file in the editor is saved back by its path. When that path
        // moves or goes away underneath it, the editor has to follow —
        // otherwise the next Save quietly re-creates the old file.
        function _onPathMoved(oldPath, newPath) {
            if (!_openFilePath) return;
            if (_openFilePath !== oldPath && !_openFilePath.startsWith(oldPath + '/')) return;
            _openFilePath = newPath + _openFilePath.slice(oldPath.length);
            const el = document.getElementById('current-filename');
            if (el) { el.dataset.path = _openFilePath; el.innerText = _openFilePath.split('/').pop(); }
        }

        function _onPathDeleted(path) {
            if (_openFilePath && (_openFilePath === path || _openFilePath.startsWith(path + '/'))) _resetEditor();
        }

        // Rows dragged inside the file manager, dropped on a folder.
        async function _moveDraggedInto(dataStr, targetPath) {
            let items;
            try { items = JSON.parse(dataStr).items; } catch (e) { showToast('Move failed.', 'error'); return; }
            const appId = activeAppId, failed = [];
            let moved = 0;
            for (const item of items || []) {
                const destPath = targetPath ? `${targetPath}/${item.name}` : item.name;
                // Onto itself, or a folder into its own subtree.
                if (item.sourcePath === destPath || targetPath === item.sourcePath || targetPath.startsWith(item.sourcePath + '/')) continue;
                const error = await _fileOp(appId, { operation: 'rename', path: item.sourcePath, new_path: destPath });
                if (error) failed.push({ name: item.name, error });
                else { moved++; _onPathMoved(item.sourcePath, destPath); }
            }
            _reportFileOpFailures('move', failed);
            if ((moved || failed.length) && appId === activeAppId) loadFiles(currentBrowserPath);
        }

        let _filesReqSeq = 0;
        async function loadFiles(path = '') {
            currentBrowserPath = path || '';
            const appId = activeAppId, seq = ++_filesReqSeq;
            selectedFiles.clear(); // Clear selections on navigation
            lastSelectedPath = null;
            const pathEl = document.getElementById('current-browser-path');
            if (!currentBrowserPath) {
                pathEl.innerHTML = '';
            } else {
                // Built as DOM nodes — never put a path inside a
                // data-on-click="…" call: HTML-escaping doesn't stop a folder named
                // x');alert(1);// from breaking out of the JS string there.
                pathEl.replaceChildren();
                let accum = '';
                for (const p of currentBrowserPath.split('/')) {
                    accum += (accum ? '/' : '') + p;
                    const sep = document.createElement('span');
                    sep.className = 'opacity-50'; sep.textContent = '/';
                    const crumb = document.createElement('span');
                    crumb.className = 'cursor-pointer hover:text-white transition';
                    crumb.textContent = p;
                    const target = accum;
                    crumb.addEventListener('click', () => loadFiles(target));
                    pathEl.append(sep, crumb);
                }
            }
            let res;
            try { res = await fetch(`${API}/apps/${appId}/files/tree?path=${encodeURIComponent(path)}`); }
            catch (e) { if (seq === _filesReqSeq) showToast('Network error loading files.', 'error'); return; }
            // A slower answer for a folder (or an app) the user has already
            // left must not replace the listing that is on screen now.
            if (seq !== _filesReqSeq || appId !== activeAppId) return;
            if (!res.ok) { showToast('Failed to load file tree.', 'error'); return; }
            const files = await res.json();
            if (seq !== _filesReqSeq || appId !== activeAppId) return;
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
                backBtn.addEventListener('touchstart', e => {
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
                    row.classList.toggle('bg-[var(--surface2)]', selectedFiles.has(rPath));
                    row.classList.toggle('ring-1', selectedFiles.has(rPath));
                    row.classList.toggle('ring-[var(--border)]', selectedFiles.has(rPath));
                };
                updateSelectionVisual();

                // Desktop click
                row.addEventListener('click', e => {
                    if (e.target.closest('.file-ctx-btn')) return;
                    e.stopPropagation();

                    if (e.ctrlKey || e.metaKey) {
                        if (selectedFiles.has(rPath)) selectedFiles.delete(rPath);
                        else selectedFiles.add(rPath);
                        lastSelectedPath = rPath;
                        updateSelectionVisual();
                        return;
                    }
                    if (e.shiftKey && lastSelectedPath) {
                        // Find range
                        const rows = Array.from(tree.querySelectorAll('.file-row:not(.back-btn)'));
                        const idx1 = rows.findIndex(r => r.dataset.path === lastSelectedPath);
                        const idx2 = rows.findIndex(r => r === row);
                        if (idx1 !== -1 && idx2 !== -1) {
                            const start = Math.min(idx1, idx2);
                            const end = Math.max(idx1, idx2);
                            for (let i = start; i <= end; i++) selectedFiles.add(rows[i].dataset.path);
                            // Re-render visuals for all rows
                            rows.forEach(r => {
                                const p = r.dataset.path;
                                r.classList.toggle('bg-[var(--surface2)]', selectedFiles.has(p));
                                r.classList.toggle('ring-1', selectedFiles.has(p));
                                r.classList.toggle('ring-[var(--border)]', selectedFiles.has(p));
                            });
                        }
                        return;
                    }

                    // Normal click clears selection unless clicking already selected row to drag
                    selectedFiles.clear();
                    selectedFiles.add(rPath);
                    lastSelectedPath = rPath;

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
                        if (!selectedFiles.has(rPath)) {
                            selectedFiles.clear();
                            selectedFiles.add(rPath);
                            lastSelectedPath = rPath;
                            updateSelectionVisual();
                        }
                        const items = Array.from(selectedFiles).map(p => {
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
                        row.classList.add('drop-target');
                    });
                    row.addEventListener('dragleave', e => { row.classList.remove('drop-target'); });
                    row.addEventListener('drop', async e => {
                        e.preventDefault(); e.stopPropagation();
                        row.classList.remove('drop-target');

                        let dataStr = null;
                        try { dataStr = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text'); } catch (err) { }

                        if (dataStr && dataStr.includes('venvy_move')) {
                            await _moveDraggedInto(dataStr, rPath);   // the folder it was dropped onto
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

        const _EDITOR_MODES = {
            py: 'python', js: 'javascript', ts: 'typescript', jsx: 'jsx', html: 'html', css: 'css',
            json: 'json', md: 'markdown', sh: 'sh', bash: 'sh', yaml: 'yaml', yml: 'yaml',
            txt: 'text', env: 'text', ini: 'ini', toml: 'toml', sql: 'sql', xml: 'xml', rs: 'rust', go: 'golang',
        };

        function _setEditorDirty(dirty) {
            _editorDirty = dirty;
            const indicator = document.getElementById('editor-dirty-indicator');
            if (indicator) indicator.classList.toggle('hidden', !dirty);
        }

        // Back to "No file open". Saving goes by the path stored on
        // #current-filename plus the active app, so a file left open across
        // an app switch would be written into the *other* app on the next
        // Save / Ctrl+S, replacing its file of the same name.
        function _resetEditor() {
            _openFilePath = null;
            const filenameEl = document.getElementById('current-filename');
            if (filenameEl) { filenameEl.innerText = 'No file open'; filenameEl.dataset.path = ''; }
            if (aceEditor) {
                aceEditor.setValue('', -1);
                aceEditor.session.getUndoManager().reset();
            }
            _setEditorDirty(false);
            document.getElementById('editor-panel')?.classList.add('translate-x-full');
            document.getElementById('file-tree-container')?.classList.remove('slide-out-left');
            isMobileFileEditorOpen = false;
        }

        function aceSearch() {
            if (!aceEditor) return;
            // Use the ext-searchbox API directly to avoid circular execCommand('find') loops.
            // ext-searchbox attaches its showFind method to the editor instance.
            if (aceEditor.searchBox && typeof aceEditor.searchBox.show === 'function') {
                aceEditor.searchBox.show(aceEditor.getCopyText() || '');
            } else {
                // Fallback: trigger via the searchbox extension command
                aceEditor.execCommand('find');
            }
        }

        const EDITOR_MAX_BYTES = 5 * 1024 * 1024;

        async function _offerDownloadInstead(path, name, message) {
            const ok = await confirmDialog({
                title: 'Can\'t open in editor',
                sub: name,
                body: message,
                confirmLabel: 'Download',
                cancelLabel: 'Close',
                icon: 'download',
            });
            if (ok) triggerDownload(`${API}/apps/${activeAppId}/files/download?path=${encodeURIComponent(path)}`, name);
        }

        async function openFile(path, name) {
            if (path === _openFilePath) {
                document.getElementById('editor-panel').classList.remove('translate-x-full');
                document.getElementById('file-tree-container').classList.add('slide-out-left', 'md:translate-x-0');
                isMobileFileEditorOpen = true;
                return;
            }
            if (_editorDirty) {
                const discard = await confirmDialog({
                    title: 'Discard unsaved changes?',
                    sub: document.getElementById('current-filename')?.innerText || '',
                    body: 'This file has edits that have not been saved. Opening another file will lose them.',
                    confirmLabel: 'Discard & open',
                    cancelLabel: 'Keep editing',
                    danger: true,
                    icon: 'alert-triangle',
                });
                if (!discard) return;
            }
            const filenameEl = document.getElementById('current-filename');
            const prevName = filenameEl.innerText, prevPath = filenameEl.dataset.path, prevOpen = _openFilePath;
            const restorePrev = () => { filenameEl.innerText = prevName; filenameEl.dataset.path = prevPath || ''; _openFilePath = prevOpen; };
            filenameEl.innerText = 'Loading…'; filenameEl.dataset.path = ''; _openFilePath = null;
            const ctrl = new AbortController();
            // If the user switches to another app while this loads, the
            // editor has been reset for that app: putting this file into it
            // would have the next Save write it into the wrong application.
            const appId = activeAppId;
            const stale = () => appId !== activeAppId;
            // On any failure put the header back the way it was: the editor
            // still holds the previous file, and it must stay saveable.
            let res;
            try { res = await fetch(`${API}/apps/${appId}/files/download?path=${encodeURIComponent(path)}`, { signal: ctrl.signal }); }
            catch (e) { if (!stale()) { restorePrev(); showToast('Network error opening file.', 'error'); } return; }
            if (stale()) { ctrl.abort(); return; }
            if (!res.ok) { restorePrev(); showToast("File not found or unreadable", 'error'); return; }
            // Don't pull huge files into the editor — Ace freezes the tab on them.
            const size = parseInt(res.headers.get('content-length') || '0', 10);
            if (size > EDITOR_MAX_BYTES) {
                ctrl.abort(); restorePrev();
                _offerDownloadInstead(path, name, `${name} is ${formatBytes(size)} — too large to open in the editor.`);
                return;
            }
            let buf;
            try { buf = new Uint8Array(await res.arrayBuffer()); }
            catch (e) { if (!stale()) { restorePrev(); showToast('Network error opening file.', 'error'); } return; }
            if (stale()) return;
            // A NUL byte in the first 8 KB is the usual "this is binary" heuristic.
            if (buf.subarray(0, 8192).includes(0)) {
                restorePrev();
                _offerDownloadInstead(path, name, `${name} looks like a binary file and can't be edited here.`);
                return;
            }
            // Strict: bytes that are not UTF-8 would be shown as "\ufffd" and
            // then written back that way, destroying the original on Save.
            let text;
            try { text = new TextDecoder('utf-8', { fatal: true }).decode(buf); }
            catch (e) {
                restorePrev();
                _offerDownloadInstead(path, name, `${name} is not UTF-8 text. Editing it here would damage it.`);
                return;
            }
            if (aceEditor) {
                const ext = name.split('.').pop().toLowerCase();
                aceEditor.session.setMode('ace/mode/' + (_EDITOR_MODES[ext] || 'text'));
                aceEditor.setValue(text, -1);
                aceEditor.session.getUndoManager().reset();
            }
            filenameEl.innerText = name; filenameEl.dataset.path = path; _openFilePath = path; _setEditorDirty(false);
            document.getElementById('editor-panel').classList.remove('translate-x-full');
            document.getElementById('file-tree-container').classList.add('slide-out-left', 'md:translate-x-0');
            isMobileFileEditorOpen = true;
            if (aceEditor) setTimeout(() => aceEditor.focus(), 100);
        }

        async function closeEditorMobile() {
            if (_editorDirty) {
                const discard = await confirmDialog({
                    title: 'Discard unsaved changes?',
                    sub: document.getElementById('current-filename')?.innerText || '',
                    body: 'Closing the editor will lose the edits you have not saved.',
                    confirmLabel: 'Discard',
                    cancelLabel: 'Keep editing',
                    danger: true,
                    icon: 'alert-triangle',
                });
                if (!discard) return;
            }
            document.getElementById('editor-panel').classList.add('translate-x-full');
            document.getElementById('file-tree-container').classList.remove('slide-out-left');
            isMobileFileEditorOpen = false;
            _openFilePath = null; _setEditorDirty(false);
        }

        async function saveCurrentFile() {
            const path = document.getElementById('current-filename').dataset.path;
            if (!path) { showToast('No file open.', 'warn'); return; }
            if (!aceEditor) { showToast('Editor not ready.', 'warn'); return; }
            const fileContent = (typeof aceEditor.getValue === 'function' ? aceEditor.getValue() : null) ?? '';
            const savedAppId = activeAppId; // capture in case user switches apps during the async save
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
                if (activeAppId === savedAppId && _openFilePath === path) _setEditorDirty(false);
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

        // Shared by downloadCurrentFile() and downloadBackup() — a same-origin
        // URL with Content-Disposition: attachment just needs a click to save.
        // A plain link can't send X-Venvy-Key, so trade it for a one-time,
        // 60-second ticket first (see /api/auth/download-ticket).
        async function triggerDownload(url, filename) {
            let ticket;
            try {
                const r = await fetch(`${API}/auth/download-ticket`, { method: 'POST' });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                ticket = (await r.json()).ticket;
            } catch (e) { showToast('Download failed: ' + e.message, 'error'); return; }
            const link = document.createElement('a');
            link.href = url + (url.includes('?') ? '&' : '?') + 'ticket=' + encodeURIComponent(ticket);
            link.download = filename;
            link.style.display = 'none';
            document.body.appendChild(link); link.click(); document.body.removeChild(link);
        }

        function downloadCurrentFile() {
            const path = document.getElementById('current-filename').dataset.path;
            const name = document.getElementById('current-filename').innerText;
            if (!path) { showToast("No file selected.", 'warn'); return; }
            triggerDownload(`${API}/apps/${activeAppId}/files/download?path=${encodeURIComponent(path)}`, name || path.split('/').pop());
        }

        let _pendingFileOp = null, _pendingRenamePath = null;
        function promptFileOp(op) {
            _pendingFileOp = op; _pendingRenamePath = null;
            const isFolder = op === 'create_folder';
            document.getElementById('file-name-modal-title').textContent = isFolder ? 'New Folder' : 'New File';
            document.getElementById('file-name-confirm-btn').textContent = isFolder ? 'Create Folder' : 'Create File';
            document.getElementById('file-name-input').value = '';
            document.getElementById('file-name-input').placeholder = isFolder ? 'folder-name' : 'filename.py';
            openModal('file-name-modal');
            setTimeout(() => document.getElementById('file-name-input').focus(), 150);
        }
        function promptRename(path, currentName) {
            _pendingFileOp = 'rename'; _pendingRenamePath = path;
            document.getElementById('file-name-modal-title').textContent = 'Rename';
            document.getElementById('file-name-confirm-btn').textContent = 'Rename';
            const input = document.getElementById('file-name-input');
            input.value = currentName; input.placeholder = currentName;
            openModal('file-name-modal');
            setTimeout(() => { input.focus(); input.select(); }, 150);
        }
        async function confirmFileNameOp() {
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
                    const fullPath = currentBrowserPath ? currentBrowserPath + '/' + name : name;
                    body = { operation: op, path: fullPath };
                }
                const error = await _fileOp(activeAppId, body);
                if (error) showToast('Operation failed: ' + error, 'error');
                else if (op === 'rename') { _onPathMoved(body.path, body.new_path); showToast(`Renamed to "${name}"`, 'success', 2000); }
            } catch (e) { showToast('Operation failed: ' + e.message, 'error'); }
            loadFiles(currentBrowserPath);
        }
        document.addEventListener('DOMContentLoaded', () => {
            const inp = document.getElementById('file-name-input');
            if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') confirmFileNameOp(); if (e.key === 'Escape') { e.stopPropagation(); closeModal('file-name-modal'); } });
        });

        async function deletePaths(items) {
            if (!items || !items.length) return;
            const ok = await confirmDialog({
                title: `Delete ${items.length} item${items.length === 1 ? '' : 's'}?`,
                sub: 'This cannot be undone',
                body: items.slice(0, 6).map(i => i.filename).join(', ')
                    + (items.length > 6 ? `, and ${items.length - 6} more` : ''),
                confirmLabel: `Delete ${items.length}`,
                danger: true,
            });
            if (!ok) return;
            const appId = activeAppId, failed = [];
            let successCount = 0;
            for (const item of items) {
                const error = await _fileOp(appId, { operation: 'delete', path: item.path });
                if (error) failed.push({ name: item.filename, error });
                else { successCount++; if (appId === activeAppId) _onPathDeleted(item.path); }
            }
            if (successCount > 0) showToast(`Deleted ${successCount} item(s)`, 'info', 2000);
            _reportFileOpFailures('delete', failed);
            if (appId !== activeAppId) return;
            selectedFiles.clear();
            loadFiles(currentBrowserPath);
        }

        // ── Advanced Upload UI ────────────────────────────────────────────────────
        // Every file is sent in UPLOAD_CHUNK_BYTES pieces to /files/upload/chunk,
        // so no single request is large (Cloudflare caps bodies at 100 MB), a
        // dropped connection retries/resumes the current chunk instead of
        // restarting the batch, and one bad file doesn't sink the others.
        const UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024;
        const UPLOAD_PARALLEL_FILES = 3;
        const UPLOAD_MAX_RETRIES = 5;
        let _activeUpload = null;   // { cancelled, xhrs:Set, ids:Set, appId }

        function formatBytes(bytes, decimals = 1) {
            if (!+bytes) return '0 Bytes';
            const k = 1024, dm = decimals < 0 ? 0 : decimals;
            const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
        }

        function showUploadProgress(title) {
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
                if (_activeUpload) cancelActiveUpload();
            };
        }

        function hideUploadProgress() {
            const container = document.getElementById('upload-progress-container');
            container.classList.add('translate-y-full');
            setTimeout(() => container.classList.add('hidden'), 300);
        }

        function cancelActiveUpload() {
            const ctl = _activeUpload;
            if (!ctl) return;
            ctl.cancelled = true;
            for (const xhr of ctl.xhrs) xhr.abort();
            // Drop the server-side partial files; stale ones are purged anyway.
            for (const id of ctl.ids) {
                fetch(`${API}/apps/${ctl.appId}/files/upload/${id}`, { method: 'DELETE' }).catch(() => { });
            }
        }

        function _newUploadId() {
            // crypto.randomUUID() needs a secure context; plain-HTTP LAN access isn't one.
            const b = new Uint8Array(16);
            crypto.getRandomValues(b);
            return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
        }

        function _postChunk(url, formData, onLoaded, ctl) {
            return new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                ctl.xhrs.add(xhr);
                const done = () => ctl.xhrs.delete(xhr);
                xhr.upload.addEventListener('progress', e => { if (e.lengthComputable) onLoaded(e.loaded / e.total); });
                xhr.addEventListener('load', () => {
                    done();
                    let body = null;
                    try { body = JSON.parse(xhr.responseText); } catch { }
                    resolve({ status: xhr.status, body });
                });
                xhr.addEventListener('error', () => { done(); reject(new Error('Network error')); });
                xhr.addEventListener('abort', () => { done(); reject(new Error('Upload aborted')); });
                xhr.open('POST', url);
                const csrf = _csrfToken();
                if (csrf) xhr.setRequestHeader('x-csrf-token', csrf);
                const key = getAuthKey();
                if (key) xhr.setRequestHeader('x-venvy-key', key);
                xhr.send(formData);
            });
        }

        function _uploadErrorText(r) {
            const d = r && r.body && r.body.detail;
            if (typeof d === 'string') return d;
            if (d && d.message) return d.message;
            return r ? `HTTP ${r.status}` : 'Network error';
        }

        async function _uploadOneFile(ctl, targetPath, file, relPath, onBytes) {
            const url = `${API}/apps/${ctl.appId}/files/upload/chunk`;
            const id = _newUploadId();
            ctl.ids.add(id);
            const total = file.size;
            let offset = 0, retries = 0, done = false;
            while (!done) {
                if (ctl.cancelled) throw new Error('Upload aborted');
                const end = Math.min(offset + UPLOAD_CHUNK_BYTES, total);
                const fd = new FormData();
                fd.append('upload_id', id);
                fd.append('path', targetPath);
                fd.append('rel_path', relPath);
                fd.append('offset', String(offset));
                fd.append('total', String(total));
                fd.append('chunk', file.slice(offset, end), 'chunk');
                const chunkStart = offset, chunkLen = end - offset;
                let r = null;
                try {
                    r = await _postChunk(url, fd, frac => onBytes(chunkStart + frac * chunkLen), ctl);
                } catch (e) {
                    if (ctl.cancelled) throw e;
                }
                if (r && r.status >= 200 && r.status < 300 && r.body) {
                    offset = r.body.received; done = r.body.done; retries = 0;
                    onBytes(offset);
                    continue;
                }
                if (r && r.status === 409 && typeof r.body?.detail?.received === 'number') {
                    // Server has a different byte count (e.g. a response got lost) — resume there.
                    offset = r.body.detail.received; onBytes(offset);
                    continue;
                }
                const retryable = !r || r.status >= 500 || r.status === 408 || r.status === 429;
                if (!retryable || r?.status === 507 || ++retries > UPLOAD_MAX_RETRIES) {
                    fetch(`${API}/apps/${ctl.appId}/files/upload/${id}`, { method: 'DELETE' }).catch(() => { });
                    ctl.ids.delete(id);
                    throw new Error(_uploadErrorText(r));
                }
                onBytes(chunkStart);
                await new Promise(res => setTimeout(res, Math.min(1000 * 2 ** (retries - 1), 15000)));
            }
            ctl.ids.delete(id);
        }

        // items: [{ file: File, relPath: 'dir/name.ext' }]. Resolves to
        // { uploaded, failed: [{ path, error }], cancelled }.
        async function uploadFilesChunked(appId, targetPath, items, title) {
            if (_activeUpload) throw new Error('Another upload is still running.');
            const ctl = { appId, cancelled: false, xhrs: new Set(), ids: new Set() };
            _activeUpload = ctl;
            const totalBytes = items.reduce((n, it) => n + it.file.size, 0);
            const perFile = new Array(items.length).fill(0);
            let doneBytes = 0;   // sum of perFile, kept incrementally
            const startTime = Date.now();
            let lastPaint = 0, lastBytes = 0, lastTime = startTime, rate = 0;
            let filesDone = 0;

            showUploadProgress(title || `Uploading ${items.length} file${items.length === 1 ? '' : 's'}`);
            const paint = (force) => {
                const now = Date.now();
                if (!force && now - lastPaint < 200) return;
                lastPaint = now;
                const dt = (now - lastTime) / 1000;
                if (dt >= 0.5) {
                    const inst = (doneBytes - lastBytes) / dt;
                    rate = rate ? rate * 0.7 + inst * 0.3 : inst;
                    lastBytes = doneBytes; lastTime = now;
                }
                const pct = totalBytes ? Math.min(100, Math.round(doneBytes / totalBytes * 100)) : Math.round(filesDone / items.length * 100);
                document.getElementById('upload-progress-bar').style.width = `${pct}%`;
                const filesPart = items.length > 1 ? ` · ${filesDone}/${items.length} files` : '';
                document.getElementById('upload-progress-stats').textContent = `${pct}% (${formatBytes(doneBytes)} / ${formatBytes(totalBytes)})${filesPart}`;
                const left = rate > 0 ? Math.round((totalBytes - doneBytes) / rate) : null;
                document.getElementById('upload-progress-speed-time').textContent =
                    rate > 0 ? `${formatBytes(rate)}/s • ${left}s left` : '';
            };

            const failed = [];
            let uploaded = 0, next = 0;
            const worker = async () => {
                while (!ctl.cancelled && next < items.length) {
                    const i = next++;
                    const { file, relPath } = items[i];
                    const setBytes = b => { doneBytes += b - perFile[i]; perFile[i] = b; paint(false); };
                    try {
                        await _uploadOneFile(ctl, targetPath, file, relPath, setBytes);
                        uploaded++;
                    } catch (e) {
                        if (ctl.cancelled) return;
                        failed.push({ path: relPath, error: e.message });
                        setBytes(file.size);   // count it as processed so the bar still reaches 100%
                    }
                    filesDone++;
                    paint(true);
                }
            };
            try {
                await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL_FILES, items.length) }, worker));
            } finally {
                _activeUpload = null;
                hideUploadProgress();
            }
            return { uploaded, failed, cancelled: ctl.cancelled };
        }

        function reportUploadResult(res, skippedMsg = '') {
            if (res.cancelled) { showToast('Upload cancelled.', 'warn'); return; }
            if (!res.failed.length) {
                showToast(`Uploaded ${res.uploaded} file${res.uploaded === 1 ? '' : 's'}.`, 'success', 2000);
                return;
            }
            const shown = res.failed.slice(0, 3).map(f => `${f.path} (${f.error})`).join('; ');
            const more = res.failed.length > 3 ? ` …and ${res.failed.length - 3} more` : '';
            showToast(`${res.failed.length} of ${res.failed.length + res.uploaded} file(s) failed: ${shown}${more}`, 'error', 10000);
        }

        async function _uploadIntoBrowser(items, targetPath) {
            if (!items.length) return;
            try {
                reportUploadResult(await uploadFilesChunked(activeAppId, targetPath, items));
            } catch (e) {
                showToast('Upload failed: ' + e.message, 'error');
            } finally {
                loadFiles(currentBrowserPath);
            }
        }

        async function uploadFile(input) {
            if (!input.files.length) return;
            const items = Array.from(input.files, f => ({ file: f, relPath: f.webkitRelativePath || f.name }));
            input.value = '';
            await _uploadIntoBrowser(items, currentBrowserPath);
        }

        // Walks dropped FileSystemEntries into [{ file, relPath }].
        async function collectDroppedEntries(entries) {
            const out = [];
            async function traverse(entry, pathPrefix) {
                if (entry.isFile) {
                    const file = await new Promise((res, rej) => entry.file(res, rej));
                    out.push({ file, relPath: pathPrefix + file.name });
                } else if (entry.isDirectory) {
                    const reader = entry.createReader();
                    // readEntries() returns at most ~100 entries per call — keep
                    // calling until it returns an empty batch.
                    let batch;
                    do {
                        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
                        for (const sub of batch) await traverse(sub, pathPrefix + entry.name + '/');
                    } while (batch.length > 0);
                }
            }
            for (const entry of entries) await traverse(entry, '');
            return out;
        }

        async function processDroppedEntries(entries, targetPath) {
            let items;
            try { items = await collectDroppedEntries(entries); }
            catch (e) { showToast('Could not read dropped files: ' + e.message, 'error'); return; }
            await _uploadIntoBrowser(items, targetPath);
        }

        document.addEventListener('DOMContentLoaded', () => {
            const treeContainer = document.getElementById('file-tree-container');
            const dropOverlay = document.getElementById('file-drop-overlay');
            let dragCounter = 0;

            if (treeContainer) {
                // Background click to clear selection
                treeContainer.addEventListener('click', e => {
                    if (!e.target.closest('.file-row') && !e.target.closest('.file-ctx-btn') && !e.target.closest('#ctx-menu')) {
                        selectedFiles.clear();
                        lastSelectedPath = null;
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
                    try { dataStr = e.dataTransfer.getData('application/json') || e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text'); } catch (err) { }

                    if (dataStr && dataStr.includes('venvy_move')) {
                        await _moveDraggedInto(dataStr, currentBrowserPath);
                        return;
                    }

                    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
                        const entries = Array.from(e.dataTransfer.items).map(i => i.webkitGetAsEntry()).filter(Boolean);
                        if (entries.length > 0) processDroppedEntries(entries, currentBrowserPath);
                    } else if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                        const input = document.createElement('input'); input.type = 'file'; input.files = e.dataTransfer.files;
                        uploadFile(input);
                    }
                });
            }
        });
