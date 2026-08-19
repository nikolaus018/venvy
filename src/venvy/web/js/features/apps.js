/**
 * The app list, app selection, the network card and lifecycle controls.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { escapeHtml, showToast } from "../core/dom.js";
import { _setEditorDirty } from "./files.js";
import { _wireSettingsDirtyCheck, checkSettingsDirty, onRuntimeChange, saveSettings } from "./settings.js";
import { _isSystemViewActive, openSystemView } from "./system.js";
import { initTerminal } from "./terminal.js";
import { closeSidebar } from "../ui/sidebar.js";
import { switchTab } from "../ui/tabs.js";
import { _featherReplace } from "../core/dom.js";

/** Previously selected app, so the view can tell a switch from a refresh. */
let _lastActiveAppId = null;

export async function loadApps() {
    try {
        const res = await fetch(`${API}/apps`);
        if (!res.ok) return;
        const apps = await res.json();
        state._cachedApps = apps;
        renderAppList(apps);
    } catch { /* loadApps error suppressed */ }
}

export let _lastAppListHash = '';

export function renderAppList(apps) {
    const hash = apps.map(a => `${a.id}:${a.name}:${a.status}`).join('|') + '|active:' + state.activeAppId;
    if (hash === _lastAppListHash) return;
    _lastAppListHash = hash;
    if (!apps.length) {
        document.getElementById('app-list').innerHTML = `<div class="text-center py-10 px-4">
        <div class="w-9 h-9 rounded-xl mx-auto flex items-center justify-center mb-3" style="background:var(--surface2);border:1px solid var(--border)">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" style="color:var(--text3)"><path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>
        </div>
        <p class="text-xs font-medium" style="color:var(--text3)">No applications yet</p>
        <p class="text-xs mt-1" style="color:var(--border2)">Use <strong style="color:var(--text3)">New Application</strong> to create one</p>
    </div>`;
        // If no app is selected, show system view as the default dashboard
        if (!state.activeAppId) openSystemView();
        return;
    }
    document.getElementById('app-list').innerHTML = apps.map(app => {
        const isActive = app.id === state.activeAppId;
        return `
        <div onclick="selectApp(${app.id})"
             class="nav-item flex items-center justify-between gap-2.5 px-3 py-2.5 rounded-xl cursor-pointer transition active:scale-95 ${isActive ? 'active' : ''}"
             style="color:${isActive ? 'var(--accent-light)' : 'var(--text2)'}">
            <div class="flex items-center gap-2.5 min-w-0">
                <div id="status-dot-${app.id}" class="w-2 h-2 rounded-full flex-shrink-0 nav-dot ${app.status === 'running' ? 'pulse-dot' : ''}" style="background:${app.status === 'running' ? 'var(--green)' : 'var(--border3)'}"></div>
                <span class="truncate text-sm font-medium">${escapeHtml(app.name)}</span>
            </div>
        </div>`;
    }).join('');
}

export function _getOpenUrlPref() {
    if (state._tempOpenUrlPref) return state._tempOpenUrlPref;
    return state.activeApp?.url_source_pref || 'local';
}

export function setOpenUrlPref(pref) {
    state._tempOpenUrlPref = pref;
    _renderOpenPrefButtons(pref);
    checkSettingsDirty();
    // Update Open App button tooltip immediately
    if (state.activeApp) _updateNetworkCard(state.activeApp);
}

export function _renderOpenPrefButtons(pref) {
    const localBtn = document.getElementById('open-pref-local');
    const publicBtn = document.getElementById('open-pref-public');
    if (!localBtn || !publicBtn) return;
    const active = 'background:var(--accent);color:white';
    const inactive = 'color:var(--text3)';
    localBtn.style.cssText = pref === 'local' ? active : inactive;
    publicBtn.style.cssText = pref === 'public' ? active : inactive;
}

export function _getEffectiveUrl(app) {
    if (!app) return null;
    const localUrl = app.local_url || null;
    const publicUrl = app.external_url || null;
    const pref = _getOpenUrlPref();
    if (pref === 'public' && publicUrl) return publicUrl;
    return localUrl || publicUrl;   // fall back gracefully
}

export function _updateNetworkCard(app) {
    const localUrl = app.local_url || null;
    const publicUrl = app.external_url || null;
    const hasBoth = !!(localUrl && publicUrl);

    // Open App button — always enabled when we have ANY url
    const effectiveUrl = _getEffectiveUrl(app);
    const btnOpen = document.getElementById('btn-open');
    if (btnOpen) {
        btnOpen.disabled = !effectiveUrl;
        btnOpen.title = effectiveUrl
            ? `Open: ${effectiveUrl}`
            : (app.status === 'running' ? 'Detecting port…' : 'Start the app to detect its port');
    }

    // Open URL preference toggle — visible only when both URLs exist
    const prefRow = document.getElementById('open-url-pref-row');
    if (prefRow) {
        prefRow.classList.toggle('hidden', !hasBoth);
        if (hasBoth) _renderOpenPrefButtons(_getOpenUrlPref());
    }

    const card = document.getElementById('net-status-card');
    if (!card) return;

    if (localUrl) {
        // Always show local IP:port — clean, always useful, never confusing
        const portStr = app.port;
        card.style.cssText = `border-radius:var(--radius-md);padding:1rem;background:var(--green-dim);border:1px solid var(--green-border)`;
        card.innerHTML = `
        <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
                <p class="text-[10px] font-bold uppercase tracking-wider mb-1 flex items-center gap-1.5" style="color:var(--green)">
                    <svg class="w-3.5 h-3.5 shrink-0" fill="none" stroke="var(--green)" viewBox="0 0 24 24" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                    Local Network · port ${portStr}
                </p>
                <p class="text-sm mono font-semibold truncate select-all leading-snug" style="color:var(--green)">${localUrl}</p>
            </div>
            <div class="flex gap-1 shrink-0">
                <button onclick="copyAccessUrl()" title="Copy local URL" class="p-1.5 rounded-lg transition active:scale-95" style="color:var(--text3)">
                    <i data-feather="copy" class="w-3.5 h-3.5"></i>
                </button>
                <a href="${localUrl}" target="_blank" title="Open local" class="p-1.5 rounded-lg transition active:scale-95 flex items-center" style="color:var(--text3)">
                    <i data-feather="external-link" class="w-3.5 h-3.5"></i>
                </a>
            </div>
        </div>`;
    } else {
        const detecting = app.status === 'running';
        card.style.cssText = `border-radius:var(--radius-md);padding:1rem;background:var(--surface2);border:1px solid var(--border)`;
        card.innerHTML = detecting
            ? `<div class="flex items-center gap-3">
               <svg class="w-4 h-4 animate-spin shrink-0" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
               <div>
                   <p class="text-xs font-bold" style="color:var(--text)">Detecting port…</p>
                   <p class="text-xs mt-0.5" style="color:var(--text3)">Scanning for a listening TCP socket. Set manually below if needed.</p>
               </div>
           </div>`
            : `<div class="flex items-center gap-3">
               <i data-feather="wifi-off" class="w-4 h-4 shrink-0" style="color:var(--text3)"></i>
               <div>
                   <p class="text-xs font-bold" style="color:var(--text2)">No local URL</p>
                   <p class="text-xs mt-0.5" style="color:var(--text3)">Start the app to auto-detect port, or set one manually below.</p>
               </div>
           </div>`;
    }
    _featherReplace();
}

export async function selectApp(id) {
    // Cancel any existing port poll from a previous app
    if (state._portPollTimer) { clearInterval(state._portPollTimer); state._portPollTimer = null; }

    state.activeAppId = id;
    if (state.logInterval) { clearInterval(state.logInterval); state.logInterval = null; }
    document.getElementById('system-view').classList.add('hidden');
    document.getElementById('system-nav-btn').classList.remove('active');
    if (window.innerWidth < 768) closeSidebar();

    // Try to set activeApp synchronously from cache before anything else
    // to ensure UI elements (like the terminal label) have the correct data immediately.
    const found = state._cachedApps.find(a => a.id === id);
    if (found) state.activeApp = found;
    else if (state.activeApp && state.activeApp.id !== id) state.activeApp = null;

    // Show the dashboard immediately with whatever we already know from the
    // sidebar list — don't wait for the network before rendering anything.
    const knownApp = state.activeApp?.id === id ? state.activeApp : null;
    if (knownApp) {
        _renderDashboard(knownApp);
    } else {
        // First time: show skeleton title while fetch is in flight
        if (!_isSystemViewActive()) {
            document.getElementById('empty-state').classList.add('hidden');
            document.getElementById('dashboard').classList.remove('hidden');
        }
        document.getElementById('dash-title').innerText = '\u2026';
    }

    // Close system view when user explicitly selects an app
    if (_isSystemViewActive()) {
        document.getElementById('system-view').classList.add('hidden');
        document.getElementById('system-nav-btn').classList.remove('active');
        document.getElementById('empty-state').classList.add('hidden');
        document.getElementById('dashboard').classList.remove('hidden');
    }

    switchTab('terminal');
    initTerminal(id);

    // Fetch authoritative data in background to ensure we are up to date
    try {
        const res = await fetch(`${API}/apps`);
        if (!res.ok) return;
        const apps = await res.json();
        state._cachedApps = apps;
        state.activeApp = apps.find(a => a.id === id);
        if (!state.activeApp) { state.activeAppId = null; return; }
        renderAppList(apps);
        _renderDashboard(state.activeApp);
    } catch {
        // If we have cached data, don't show an error toast for a background update fail
        if (!state.activeApp) showToast('Network error loading app.', 'error');
    }

    // Port polling — only when running with no port detected yet
    if (state.activeApp.status === 'running' && !state.activeApp.port) {
        let _polls = 0;
        state._portPollTimer = setInterval(async () => {
            _polls++;
            if (state.activeAppId !== id || _polls >= 60) { clearInterval(state._portPollTimer); state._portPollTimer = null; return; }
            try {
                const r = await fetch(`${API}/apps`);
                if (!r.ok) return;
                const apps = await r.json();
                const updated = apps.find(a => a.id === id);
                if (updated && state.activeAppId === id) {
                    state.activeApp = updated;
                    _updateNetworkCard(state.activeApp);
                    document.getElementById('setting-port').value = state.activeApp.port || '';
                    if (state.activeApp.port) { clearInterval(state._portPollTimer); state._portPollTimer = null; }
                }
            } catch { }
        }, 2000);
    }
}

export function _renderDashboard(app) {
    if (_isSystemViewActive()) return;
    document.getElementById('empty-state').classList.add('hidden');
    document.getElementById('dashboard').classList.remove('hidden');
    document.getElementById('dash-title').innerText = app.name;
    const badge = document.getElementById('dash-status');
    badge.innerText = app.status;
    badge.className = app.status === 'running'
        ? 'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide status-running'
        : 'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide status-stopped';

    // ONLY update settings if they are not dirty (preserving user unsaved changes)
    // EXCEPT if we are switching to a completely different app.
    const isAppSwitch = (_lastActiveAppId !== null && _lastActiveAppId !== app.id);
    const isCurrentlyDirty = !isAppSwitch && document.getElementById('save-status-dot') && !document.getElementById('save-status-dot').classList.contains('hidden');

    if (!isCurrentlyDirty) {
        document.getElementById('setting-policy').value = app.restart_policy || 'never';
        document.getElementById('setting-boot').checked = app.start_on_boot;
        document.getElementById('setting-port').value = app.port || '';
        document.getElementById('setting-env').value = app.env_redacted
            ? '// Hidden — operator or admin access is required to view or edit environment variables.'
            : (() => {
                try { return JSON.stringify(JSON.parse(app.env_json || '{}'), null, 2); }
                catch { return app.env_json || '{}'; }
            })();
        document.getElementById('setting-env').readOnly = !!app.env_redacted;
        document.getElementById('setting-name').value = app.name || '';
        if (document.getElementById('setting-runtime')) {
            document.getElementById('setting-runtime').value = app.runtime || 'python';
        }
        document.getElementById('setting-entrypoint').value = app.entrypoint || '';
        const boot = document.getElementById('setting-boot');
        if (app.start_on_boot) boot.checked = true; else boot.checked = false;

        onRuntimeChange('settings');
        document.getElementById('setting-entrypoint').value = app.entrypoint || '';
        state._tempOpenUrlPref = app.url_source_pref || 'local';
        _renderOpenPrefButtons(state._tempOpenUrlPref);
    }

    _updateNetworkCard(app);

    _wireSettingsDirtyCheck();
    checkSettingsDirty();

    // ONLY clear path if we are switching to a COMPLETELY DIFFERENT app
    if (_lastActiveAppId !== null && _lastActiveAppId !== app.id) {
        state.currentBrowserPath = ""; state.fileClipboard = null; state._openFilePath = null; _setEditorDirty(false);
    }
    _lastActiveAppId = app.id;
}

export async function controlApp(action) {
    const targetAppId = state.activeAppId;
    const btn = document.getElementById(`btn-${action}`);
    const originalContent = btn ? btn.innerHTML : '';

    function _resetBtn() {
        if (!btn) return;
        btn.disabled = false;
        btn.innerHTML = originalContent;
        feather.replace();
    }

    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i data-feather="loader" class="w-4 h-4 animate-spin"></i>';
        feather.replace();
    }

    try {
        // The backend returns 202 immediately (fire-and-forget).
        // 5 s is plenty since the server responds before doing any real work.
        const controller = new AbortController();
        const tId = setTimeout(() => controller.abort(), 5000);
        let res;
        try {
            res = await fetch(`${API}/apps/${targetAppId}/control?action=${action}`, {
                method: 'POST', signal: controller.signal
            });
        } finally {
            clearTimeout(tId);
        }

        if (!res.ok) {
            const e = await res.json().catch(() => ({ detail: 'Action failed' }));
            showToast(e.detail || 'Action failed', 'error');
            return;
        }
    } catch (e) {
        showToast(e.name === 'AbortError' ? 'Server not responding.' : 'Network error: ' + e.message, 'error');
        return;
    } finally {
        // Button unlocks the moment the HTTP response arrives — milliseconds after click.
        _resetBtn();
    }

    // ── Poll until state settles (running/stopped/error) ─────────────────
    // The actual work (kill tree, spawn process) happens server-side in a background
    // task. We just poll until we see the expected final state.
    const expectedFinal = action === 'stop' ? 'stopped' : 'running';
    let polls = 0;
    const pollTimer = setInterval(async () => {
        polls++;
        // Stop polling after 30 s regardless
        if (polls > 30 || state.activeAppId !== targetAppId) {
            clearInterval(pollTimer);
            return;
        }
        try {
            const r = await fetch(`${API}/apps`);
            if (!r.ok) return;
            const apps = await r.json();
            state._cachedApps = apps;
            renderAppList(apps);
            const updated = apps.find(a => a.id === targetAppId);
            if (!updated || state.activeAppId !== targetAppId) { clearInterval(pollTimer); return; }
            state.activeApp = updated;
            _renderDashboard(state.activeApp);

            const settled = updated.status === expectedFinal || updated.status === 'error';
            if (settled) {
                clearInterval(pollTimer);
                showToast(
                    updated.status === 'error' ? 'Action failed — check logs.' :
                        { start: 'Started ✓', stop: 'Stopped ✓', restart: 'Restarted ✓' }[action] || action,
                    updated.status === 'error' ? 'error' : 'success', 1500
                );
                // Also kick off port-detection poll on start/restart
                if ((action === 'start' || action === 'restart') && updated.status === 'running') {
                    if (state._portPollTimer) { clearInterval(state._portPollTimer); state._portPollTimer = null; }
                    let _pp = 0;
                    state._portPollTimer = setInterval(async () => {
                        _pp++;
                        if (state.activeAppId !== targetAppId || _pp >= 60) { clearInterval(state._portPollTimer); state._portPollTimer = null; return; }
                        try {
                            const pr = await fetch(`${API}/apps`);
                            if (!pr.ok) return;
                            const papps = await pr.json();
                            const pu = papps.find(a => a.id === targetAppId);
                            if (pu && state.activeAppId === targetAppId) {
                                state.activeApp = pu;
                                _updateNetworkCard(state.activeApp);
                                const portInp = document.getElementById('setting-port');
                                if (portInp) portInp.value = state.activeApp.port || '';
                                if (state.activeApp.port) { clearInterval(state._portPollTimer); state._portPollTimer = null; }
                            }
                        } catch { }
                    }, 2000);
                }
            }
        } catch { }
    }, 1000);
}

export function openExternal() {
    const url = _getEffectiveUrl(state.activeApp);
    if (!url) {
        showToast(state.activeApp?.status === 'running' ? 'Port not yet detected — try again.' : 'Start the app first, or set a port in Settings.', 'warn', 4000);
        return;
    }
    window.open(url, '_blank');
}

export async function copyAccessUrl() {
    const url = _getEffectiveUrl(state.activeApp);
    if (!url) { showToast('No URL available yet.', 'warn'); return; }
    try { await navigator.clipboard.writeText(url); }
    catch { const ta = document.createElement('textarea'); ta.value = url; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
    showToast('URL copied!', 'success', 1500);
}

export async function clearManualPort() {
    if (!state.activeAppId) return;
    document.getElementById('setting-port').value = '';
    await saveSettings();
    showToast('Port cleared — will auto-detect on next start.', 'info');
}
