/**
 * Live log tailing, including the ANSI-to-HTML renderer for coloured output.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { escapeHtml, showToast } from "../core/dom.js";
import { closeModal, openModal } from "../ui/modals.js";

export let _logsInflight = false;

export async function loadLogs() {
    if (!state.activeAppId || _logsInflight) return;
    _logsInflight = true;
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/logs`);
        if (!res.ok) return;
        const data = await res.json();
        const el = document.getElementById('log-viewer');
        const wasBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        if (!data.content && !data.truncated) {
            el.innerHTML = '<span class="italic" style="color:var(--text3)">No log output yet. Start the application to see live logs.</span>';
        } else {
            const raw = data.truncated
                ? `[showing last ${data.total_lines > 0 ? data.total_lines : ''} lines (truncated) — clear logs to reset]\n\n` + data.content
                : data.content;
            el.innerHTML = window._ansiToHtml ? window._ansiToHtml(raw) : raw.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            el.dataset.rawText = raw;
        }
        if (wasBottom || !el.innerHTML) el.scrollTop = el.scrollHeight;
    } catch { /* network error — silently skip poll tick */ }
    finally { _logsInflight = false; }
}

export async function deleteLogs() {
    const res = await fetch(`${API}/apps/${state.activeAppId}/logs`, { method: 'DELETE' });
    if (res.ok) { document.getElementById('log-viewer').innerHTML = '<span class="italic" style="color:var(--text3)">No log output yet. Start the application to see live logs.</span>'; showToast('Logs cleared.', 'info', 1500); }
}

export async function copyLogs() {
    const el = document.getElementById('log-viewer');
    // Use stored raw text (stripped of ANSI) if available, else fall back to innerText
    const text = el.dataset.rawText || el.innerText;
    // Strip ANSI escape sequences for clean clipboard copy
    const clean = text.replace(/\x1b\[[0-9;]*[mA-Za-z]/g, '');
    try { await navigator.clipboard.writeText(clean); }
    catch { const ta = document.createElement('textarea'); ta.value = clean; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
    showToast('Logs copied!', 'success', 2000);
}

(function () {
    // ANSI 16-color palette (indices 0-15)
    const _ANSI16 = [
        '#1a1a1a', '#ff453a', '#30d158', '#ffd60a',
        '#ffffff', '#a1a1aa', '#71717a', '#f2f2f2',
        '#48484a', '#ff6961', '#34c759', '#ffcc00',
        'var(--text)', '#da8fff', '#8ee2ff', '#ffffff',
    ];
    function _ansiColor256(n) {
        if (n < 16) return _ANSI16[n];
        if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
        n -= 16;
        const b = n % 6, g = Math.floor(n / 6) % 6, r = Math.floor(n / 36);
        const c = v => v ? 55 + v * 40 : 0;
        return `rgb(${c(r)},${c(g)},${c(b)})`;
    }

    window._ansiToHtml = function (raw) {
        // State
        let bold = false, dim = false, italic = false, underline = false, blink = false;
        let fg = null, bg = null;
        let out = '';

        // Escape any HTML in the raw string first, then process ANSI codes.
        // We process char-by-char rebuilding spans.
        // Strategy: split on ESC sequences, then process each chunk.
        const RE = /\x1b\[([0-9;]*)m|\x1b\[[0-9;]*[A-HJKSTfhilmnsu]|\x1b[()][AB012]|\x1b[^[]/g;

        let last = 0;
        const flushText = (text) => {
            if (!text) return;
            // Build style string for current state
            const styles = [];
            if (fg) styles.push(`color:${fg}`);
            if (bg) styles.push(`background:${bg}`);
            if (bold) styles.push('font-weight:700');
            if (dim) styles.push('opacity:0.55');
            if (italic) styles.push('font-style:italic');
            if (underline) styles.push('text-decoration:underline');
            if (blink) styles.push('animation:_logBlink 1s step-end infinite');
            const escaped = text
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            if (styles.length) {
                out += `<span style="${styles.join(';')}">${escaped}</span>`;
            } else {
                out += escaped;
            }
        };

        let m;
        while ((m = RE.exec(raw)) !== null) {
            // Flush plain text before this escape
            flushText(raw.slice(last, m.index));
            last = m.index + m[0].length;

            // Only process SGR sequences (ending in 'm')
            if (!m[0].endsWith('m') || m[1] === undefined) continue;

            const params = m[1] === '' ? [0] : m[1].split(';').map(Number);
            let i = 0;
            while (i < params.length) {
                const p = params[i++];
                if (p === 0) { bold = false; dim = false; italic = false; underline = false; blink = false; fg = null; bg = null; }
                else if (p === 1) bold = true;
                else if (p === 2) dim = true;
                else if (p === 3) italic = true;
                else if (p === 4) underline = true;
                else if (p === 5 || p === 6) blink = true;
                else if (p === 22) { bold = false; dim = false; }
                else if (p === 23) italic = false;
                else if (p === 24) underline = false;
                else if (p === 25) blink = false;
                else if (p === 39) fg = null;
                else if (p === 49) bg = null;
                else if (p >= 30 && p <= 37) fg = _ANSI16[p - 30];
                else if (p >= 40 && p <= 47) bg = _ANSI16[p - 40];
                else if (p >= 90 && p <= 97) fg = _ANSI16[p - 90 + 8];
                else if (p >= 100 && p <= 107) bg = _ANSI16[p - 100 + 8];
                else if ((p === 38 || p === 48) && params[i] === 5 && i + 1 < params.length) {
                    const col = _ansiColor256(params[i + 1]); i += 2;
                    if (p === 38) fg = col; else bg = col;
                } else if ((p === 38 || p === 48) && params[i] === 2 && i + 3 < params.length) {
                    const col = `rgb(${params[i + 1]},${params[i + 2]},${params[i + 3]})`; i += 4;
                    if (p === 38) fg = col; else bg = col;
                }
            }
        }
        flushText(raw.slice(last));
        return out;
    };
})();

export function _isPythonPackageRuntime() {
    return (state.activeApp?.runtime || 'python') === 'python';
}

export function _syncPackagesTabRuntimeUi() {
    const isPythonRuntime = _isPythonPackageRuntime();
    const installControls = document.getElementById('pkg-install-controls');
    const autoScanCard = document.getElementById('pkg-auto-scan-card');
    const actionHeader = document.getElementById('pkg-action-header');
    const scanResults = document.getElementById('scan-results');
    const scanMissingSection = document.getElementById('scan-missing-section');
    const scanOkSection = document.getElementById('scan-ok-section');

    if (installControls) installControls.classList.toggle('hidden', !isPythonRuntime);
    if (autoScanCard) autoScanCard.classList.toggle('hidden', !isPythonRuntime);
    if (actionHeader) actionHeader.classList.toggle('hidden', !isPythonRuntime);
    if (scanResults) scanResults.classList.add('hidden');
    if (scanMissingSection) scanMissingSection.classList.add('hidden');
    if (scanOkSection) scanOkSection.classList.add('hidden');
}

export async function loadPackages() {
    const tbody = document.getElementById('pkg-list');
    const isPythonRuntime = _isPythonPackageRuntime();
    _syncPackagesTabRuntimeUi();
    tbody.innerHTML = `<tr><td colspan="${isPythonRuntime ? 3 : 2}" class="text-center py-8 text-sm" style="color:var(--text3)">Fetching environment...</td></tr>`;
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/packages`);
        if (!res.ok) {
            tbody.innerHTML = `<tr><td colspan="${isPythonRuntime ? 3 : 2}" class="text-center py-8 text-sm" style="color:var(--red)">Failed to load packages.</td></tr>`;
            return;
        }
        const pkgs = await res.json();
        tbody.innerHTML = pkgs.map(p => {
            const rowHtml = `
    <tr class="transition" style="border-color:var(--border)">
        <td class="px-4 md:px-5 py-3 mono font-medium text-xs md:text-sm" style="color:var(--blue)">${escapeHtml(p.name)}</td>
        <td class="px-2 md:px-5 py-3 text-xs mono hidden sm:table-cell" style="color:var(--text3)">${escapeHtml(p.version)}</td>
        ${isPythonRuntime ? `
            <td class="px-3 md:px-5 py-3 text-right">
                ${p.name.toLowerCase() === 'pip' ? `<span class="text-xs italic hidden sm:inline" style="color:var(--text3)">System</span>` :
                    `<button data-pkg="${escapeHtml(p.name)}" class="pkg-rm-btn remove-btn btn-danger inline-flex items-center justify-center gap-1.5 text-xs font-bold px-2.5 py-1.5 rounded-lg transition">
                    <svg class="w-3 h-3 remove-btn-icon shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                    <span class="hidden md:inline">Remove</span>
                </button>`}
            </td>` : ''}
    </tr>`;
            return rowHtml;
        }).join('');
        if (isPythonRuntime) {
            tbody.querySelectorAll('.pkg-rm-btn').forEach(btn => btn.addEventListener('click', () => uninstallPackage(btn.dataset.pkg)));
        }
    } catch { tbody.innerHTML = `<tr><td colspan="${isPythonRuntime ? 3 : 2}" class="text-center py-8 text-sm" style="color:var(--red)">Network error loading packages.</td></tr>`; }
}

export async function installPackage() {
    const input = document.getElementById('pkg-input');
    const btn = document.getElementById('pkg-install-btn');
    const name = input.value.trim();
    if (!name) return;
    btn.disabled = true; btn.innerText = "Installing...";
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/packages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
        if (!res.ok) throw new Error((await res.json()).detail);
        showToast(`Installed ${name}!`, 'success'); input.value = ''; loadPackages();
    } catch (e) { showToast("Install failed: " + e.message, 'error'); }
    finally { btn.disabled = false; btn.innerText = "Install"; }
}

export async function uninstallPackage(name) {
    document.getElementById('remove-pkg-name').textContent = name;
    document.getElementById('remove-pkg-modal').dataset.pkg = name;
    openModal('remove-pkg-modal');
}

export async function confirmRemovePackage() {
    const name = document.getElementById('remove-pkg-modal').dataset.pkg;
    closeModal('remove-pkg-modal');
    if (!name) return;
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/packages/${name}`, { method: 'DELETE' });
        if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: 'Failed' }))).detail);
        showToast(`Removed ${name}`, 'success'); loadPackages();
    } catch (e) { showToast('Remove failed: ' + e.message, 'error'); }
}
