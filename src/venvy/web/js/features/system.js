/**
 * The system view: host metrics and the port map.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { escapeHtml } from "../core/dom.js";
import { loadCfCredsStatus } from "./cloudflare.js";
import { closeSidebar } from "../ui/sidebar.js";
import { _featherReplace } from "../core/dom.js";

export function _isSystemViewActive() {
    return !document.getElementById('system-view').classList.contains('hidden');
}

export function openSystemView(userTriggered = false) {
    document.getElementById('empty-state').classList.add('hidden');
    document.getElementById('dashboard').classList.add('hidden');
    document.getElementById('system-view').classList.remove('hidden');
    document.getElementById('system-nav-btn').classList.add('active');
    if (userTriggered && window.innerWidth < 768) closeSidebar();
    loadSystemView(); loadCfCredsStatus();
}

export function closeSystemView() {
    document.getElementById('system-view').classList.add('hidden');
    document.getElementById('system-nav-btn').classList.remove('active');
    if (state.activeAppId) document.getElementById('dashboard').classList.remove('hidden');
    else document.getElementById('empty-state').classList.remove('hidden');
}

export async function loadSystemView(btn = null) {
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
                style="background:rgba(255,255,255,0.03);color:var(--text);border:1px dashed var(--border3)">
                ${port}
                <svg class="w-3 h-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
            </button>`).join('') + '</div>';

        if (freeEl.innerHTML !== freeHtml) freeEl.innerHTML = freeHtml;

    } catch {
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
