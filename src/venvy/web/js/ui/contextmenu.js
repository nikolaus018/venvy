/**
 * The file browser's right-click context menu and clipboard operations.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { showToast } from "../core/dom.js";
import { deletePaths, loadFiles, promptRename } from "../features/files.js";

export function showContextMenu(e, target) {
    e.preventDefault();
    const menu = document.getElementById('ctx-menu');
    const cutBtn = document.getElementById('ctx-cut');
    const copyBtn = document.getElementById('ctx-copy');
    const pasteBtn = document.getElementById('ctx-paste');
    const delBtn = document.getElementById('ctx-delete');
    const header = document.getElementById('ctx-header');
    const renameBtn = document.getElementById('ctx-rename');

    // If right-clicked row is not in selection, select only it
    if (target && !state.selectedFiles.has(target.path)) {
        state.selectedFiles.clear();
        state.selectedFiles.add(target.path);
        state.lastSelectedPath = target.path;
        document.querySelectorAll('.file-row').forEach(r => {
            const p = r.dataset.path;
            r.classList.toggle('bg-[var(--surface2)]', state.selectedFiles.has(p));
            r.classList.toggle('ring-1', state.selectedFiles.has(p));
            r.classList.toggle('ring-[var(--border)]', state.selectedFiles.has(p));
        });
    }

    state.ctxTarget = target;
    if (state.ctxTarget) {
        if (state.selectedFiles.size > 1) {
            header.innerText = `${state.selectedFiles.size} items selected`;
            renameBtn.style.display = 'none'; // Cannot rename multiple items at once
        } else {
            header.innerText = state.ctxTarget.name;
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
        pasteBtn.disabled = !state.fileClipboard || state.fileClipboard.paths.length === 0;
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

export function openCtxMenu(e, path, type, name) { showContextMenu(e, { path, type, name }); }

export function closeCtxMenu() {
    const menu = document.getElementById('ctx-menu');
    menu.classList.remove('scale-100', 'opacity-100');
    menu.classList.add('scale-95', 'opacity-0');
    setTimeout(() => menu.classList.add('hidden'), 100);
}

export function getSelectedFilesInfo() {
    const items = [];
    document.querySelectorAll('.file-row').forEach(r => {
        if (state.selectedFiles.has(r.dataset.path)) {
            items.push({ path: r.dataset.path, filename: r.dataset.name });
        }
    });
    // Fallback to clicked item if DOM state doesn't match
    if (items.length === 0 && state.ctxTarget) items.push({ path: state.ctxTarget.path, filename: state.ctxTarget.name });
    return items;
}

document.getElementById('ctx-rename').onclick = (e) => { e.stopPropagation(); if (state.ctxTarget && state.selectedFiles.size <= 1) promptRename(state.ctxTarget.path, state.ctxTarget.name); closeCtxMenu(); };

document.getElementById('ctx-cut').onclick = (e) => {
    e.stopPropagation();
    const items = getSelectedFilesInfo();
    if (items.length) { state.fileClipboard = { op: 'rename', paths: items.map(i => i.path), filenames: items.map(i => i.filename) }; }
    closeCtxMenu();
};

document.getElementById('ctx-copy').onclick = (e) => {
    e.stopPropagation();
    const items = getSelectedFilesInfo();
    if (items.length) { state.fileClipboard = { op: 'copy', paths: items.map(i => i.path), filenames: items.map(i => i.filename) }; }
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
    if (!state.fileClipboard || !state.fileClipboard.paths.length) return;
    const op = state.fileClipboard.op;
    let successCount = 0;
    const targetPath = state.currentBrowserPath;

    // Sequential block for simplicity
    for (let i = 0; i < state.fileClipboard.paths.length; i++) {
        const srcPath = state.fileClipboard.paths[i];
        const destPath = targetPath ? `${targetPath}/${state.fileClipboard.filenames[i]}` : state.fileClipboard.filenames[i];

        if (destPath === srcPath) continue; // skip identical
        try {
            const res = await fetch(`${API}/apps/${state.activeAppId}/files/op`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ operation: op, path: srcPath, new_path: destPath })
            });
            if (res.ok) successCount++;
        } catch (err) { console.error(`Failed to ${op} ${srcPath}`, err); }
    }
    if (successCount > 0) {
        showToast(`Pasted ${successCount} item${successCount > 1 ? 's' : ''}`, 'success', 2000);
    }
    if (state.fileClipboard.op === 'rename') state.fileClipboard = null;
    loadFiles(state.currentBrowserPath);
    closeCtxMenu();
};
