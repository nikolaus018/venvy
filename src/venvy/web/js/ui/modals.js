/**
 * The modal stack and global keyboard shortcuts.
 *
 * Open modals are tracked as a stack so Escape always closes the topmost one.
 */

import { state } from "../core/state.js";
import { closeEditorMobile, saveCurrentFile } from "../features/files.js";

export const _NO_BACKDROP_CLOSE = new Set([]);

export const _MODAL_CLOSE_FN = {
};

export const _openModalIds = [];

export function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('hidden');
    if (!_openModalIds.includes(id)) _openModalIds.push(id);
}

export function closeModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.add('hidden');
    const idx = _openModalIds.indexOf(id);
    if (idx !== -1) _openModalIds.splice(idx, 1);
}

document.addEventListener('click', (e) => {
    for (const id of [..._openModalIds].reverse()) {
        const overlay = document.getElementById(id);
        if (!overlay || overlay.classList.contains('hidden')) continue;
        if (e.target === overlay && !_NO_BACKDROP_CLOSE.has(id)) {
            if (_MODAL_CLOSE_FN[id]) _MODAL_CLOSE_FN[id]();
            else closeModal(id);
            break;   // only close the topmost
        }
    }
}, { capture: false });

document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        const path = document.getElementById('current-filename')?.dataset?.path;
        if (path) { e.preventDefault(); saveCurrentFile(); }
    }
    if (e.key === 'Escape') {
        // Close topmost open modal
        if (_openModalIds.length > 0) {
            const topId = _openModalIds[_openModalIds.length - 1];
            if (!_NO_BACKDROP_CLOSE.has(topId)) {
                if (_MODAL_CLOSE_FN[topId]) _MODAL_CLOSE_FN[topId]();
                else closeModal(topId);
                return;
            }
        }
        if (state.isMobileFileEditorOpen && window.innerWidth < 768) closeEditorMobile();
    }
});
