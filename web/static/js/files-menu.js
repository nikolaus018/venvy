// File manager context menu and clipboard (cut / copy / paste / delete).
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Context Menu ─────────────────────────────────────────────────────────
        function showContextMenu(e, target) {
            e.preventDefault();
            const menu = document.getElementById('ctx-menu');
            const cutBtn = document.getElementById('ctx-cut');
            const copyBtn = document.getElementById('ctx-copy');
            const pasteBtn = document.getElementById('ctx-paste');
            const delBtn = document.getElementById('ctx-delete');
            const header = document.getElementById('ctx-header');
            const renameBtn = document.getElementById('ctx-rename');

            // If right-clicked row is not in selection, select only it
            if (target && !selectedFiles.has(target.path)) {
                selectedFiles.clear();
                selectedFiles.add(target.path);
                lastSelectedPath = target.path;
                document.querySelectorAll('.file-row').forEach(r => {
                    const p = r.dataset.path;
                    r.classList.toggle('bg-[var(--surface2)]', selectedFiles.has(p));
                    r.classList.toggle('ring-1', selectedFiles.has(p));
                    r.classList.toggle('ring-[var(--border)]', selectedFiles.has(p));
                });
            }

            ctxTarget = target;
            if (ctxTarget) {
                if (selectedFiles.size > 1) {
                    header.innerText = `${selectedFiles.size} items selected`;
                    renameBtn.style.display = 'none'; // Cannot rename multiple items at once
                } else {
                    header.innerText = ctxTarget.name;
                    renameBtn.style.display = 'flex';
                }
                cutBtn.style.display = 'flex';
                copyBtn.style.display = 'flex';
                delBtn.style.display = 'flex';
                pasteBtn.style.display = 'none';
            } else {
                header.innerText = "Folder Options";
                renameBtn.style.display = 'none'; cutBtn.style.display = 'none';
                copyBtn.style.display = 'none'; delBtn.style.display = 'none';
                pasteBtn.style.display = 'flex';
                pasteBtn.disabled = !fileClipboard || fileClipboard.paths.length === 0;
                pasteBtn.classList.toggle('opacity-50', pasteBtn.disabled);
            }
            const rect = document.getElementById('app-shell').getBoundingClientRect();
            let x = e.clientX || (e.touches && e.touches[0].clientX);
            let y = e.clientY || (e.touches && e.touches[0].clientY);
            if (!x && !y && e.target) { const br = e.target.getBoundingClientRect(); x = br.left; y = br.bottom; }
            if (x + 200 > rect.right) x -= 200;
            if (y + 220 > rect.bottom) y -= 220;
            menu.style.left = `${x}px`; menu.style.top = `${y}px`;
            menu.classList.remove('hidden');
            void menu.offsetWidth;
            menu.classList.remove('scale-95', 'opacity-0');
            menu.classList.add('scale-100', 'opacity-100');
        }
        document.getElementById('file-tree-container').addEventListener('contextmenu', (e) => {
            if (e.target.closest('.file-row')) return;
            showContextMenu(e, null);
        });
        function closeCtxMenu() {
            const menu = document.getElementById('ctx-menu');
            menu.classList.remove('scale-100', 'opacity-100');
            menu.classList.add('scale-95', 'opacity-0');
            setTimeout(() => menu.classList.add('hidden'), 100);
        }

        // Context Actions helper to get all selection items
        function getSelectedFilesInfo() {
            const items = [];
            document.querySelectorAll('.file-row').forEach(r => {
                if (selectedFiles.has(r.dataset.path)) {
                    items.push({ path: r.dataset.path, filename: r.dataset.name });
                }
            });
            // Fallback to clicked item if DOM state doesn't match
            if (items.length === 0 && ctxTarget) items.push({ path: ctxTarget.path, filename: ctxTarget.name });
            return items;
        }

        document.getElementById('ctx-rename').onclick = (e) => { e.stopPropagation(); if (ctxTarget && selectedFiles.size <= 1) promptRename(ctxTarget.path, ctxTarget.name); closeCtxMenu(); };
        document.getElementById('ctx-cut').onclick = (e) => {
            e.stopPropagation();
            const items = getSelectedFilesInfo();
            if (items.length) { fileClipboard = { op: 'rename', paths: items.map(i => i.path), filenames: items.map(i => i.filename) }; }
            closeCtxMenu();
        };
        document.getElementById('ctx-copy').onclick = (e) => {
            e.stopPropagation();
            const items = getSelectedFilesInfo();
            if (items.length) { fileClipboard = { op: 'copy', paths: items.map(i => i.path), filenames: items.map(i => i.filename) }; }
            closeCtxMenu();
        };
        document.getElementById('ctx-delete').onclick = (e) => {
            e.stopPropagation();
            const items = getSelectedFilesInfo();
            if (items.length) deletePaths(items);
            closeCtxMenu();
        };

        document.getElementById('ctx-paste').onclick = async (e) => {
            e.stopPropagation();
            if (!fileClipboard || !fileClipboard.paths.length) return;
            closeCtxMenu();
            const clip = fileClipboard, op = clip.op, appId = activeAppId;
            const targetPath = currentBrowserPath, failed = [];
            let successCount = 0;

            // Sequential block for simplicity
            for (let i = 0; i < clip.paths.length; i++) {
                const srcPath = clip.paths[i];
                const destPath = targetPath ? `${targetPath}/${clip.filenames[i]}` : clip.filenames[i];

                if (destPath === srcPath) continue; // skip identical
                // A folder can't be put inside itself.
                if (targetPath === srcPath || targetPath.startsWith(srcPath + '/')) {
                    failed.push({ name: clip.filenames[i], error: 'a folder can\'t go inside itself' });
                    continue;
                }
                const error = await _fileOp(appId, { operation: op, path: srcPath, new_path: destPath });
                if (error) failed.push({ name: clip.filenames[i], error });
                else { successCount++; if (op === 'rename') _onPathMoved(srcPath, destPath); }
            }
            if (successCount > 0) {
                showToast(`Pasted ${successCount} item${successCount > 1 ? 's' : ''}`, 'success', 2000);
            }
            _reportFileOpFailures(op === 'copy' ? 'copy' : 'move', failed);
            // A cut is used up — unless nothing moved, so it can be retried elsewhere.
            if (op === 'rename' && successCount > 0 && fileClipboard === clip) fileClipboard = null;
            if (appId === activeAppId) loadFiles(currentBrowserPath);
        };
