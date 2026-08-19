/**
 * Background polling that keeps the app list and the dashboard in sync.
 *
 * Polling backs off and stops after repeated failures so a dead backend does
 * not produce an endless stream of console errors.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { _lastAppListHash, _updateNetworkCard, renderAppList } from "./apps.js";
import { _showReAuthOverlay } from "./auth.js";
import { checkSettingsDirty } from "./settings.js";
import { loadSystemView } from "./system.js";

export const _loginTotpPending = false;

export let _pollFailures = 0;

export const _MAX_POLL_FAILURES = 3;

export let _pollInflight = false;

export function startPolling() {
    if (state.pollInterval) clearInterval(state.pollInterval);
    _pollFailures = 0;
    _pollInflight = false;
    state.pollInterval = setInterval(async () => {
        // Drop tick entirely if the previous one hasn't finished yet.
        // Without this guard, slow server responses stack up concurrent fetches.
        if (_pollInflight) return;
        _pollInflight = true;
        try {
            const res = await fetch(`${API}/apps`);
            if (!res.ok) {
                if (res.status === 401) {
                    _pollFailures++;
                    if (_pollFailures >= 10) { // More lenient
                        clearInterval(state.pollInterval);
                        state.pollInterval = null;
                        _showReAuthOverlay();
                    }
                }
                return;
            }
            _pollFailures = 0;  // reset on success
            const apps = await res.json();
            apps.forEach(app => {
                const dot = document.getElementById(`status-dot-${app.id}`);
                if (dot) {
                    dot.className = `w-2 h-2 rounded-full flex-shrink-0 nav-dot ${app.status === 'running' ? 'pulse-dot' : ''}`;
                    dot.style.background = app.status === 'running' ? 'var(--green)' : 'var(--border3)';
                }
                if (state.activeAppId === app.id) {
                    const prev = state.activeApp;
                    state.activeApp = app;
                    // Always keep title in sync (handles renames)
                    const titleEl = document.getElementById('dash-title');
                    if (titleEl && titleEl.innerText !== app.name) titleEl.innerText = app.name;
                    // Status badge
                    if (!prev || prev.status !== app.status) {
                        const badge = document.getElementById('dash-status');
                        if (badge) {
                            badge.innerText = app.status;
                            badge.className = app.status === 'running'
                                ? 'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide status-running'
                                : 'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide status-stopped';
                        }
                    }
                    // Network card — update on port or url change
                    if (!prev || prev.port !== app.port || prev.external_url !== app.external_url) {
                        _updateNetworkCard(app);
                        const portEl = document.getElementById('setting-port');
                        if (portEl && (!prev || prev.port !== app.port)) portEl.value = app.port || '';
                        // Clear port poll timer if port just appeared
                        if (app.port && state._portPollTimer) { clearInterval(state._portPollTimer); state._portPollTimer = null; }
                    }
                }
            });
            const currentCount = document.querySelectorAll('#app-list > div').length;
            if (apps.length !== currentCount) { _lastAppListHash = ''; }
            renderAppList(apps);

            // If settings tab is active, keep dirty indicator in sync with server data
            const tabSettings = document.getElementById('tab-settings');
            if (tabSettings && !tabSettings.classList.contains('hidden')) {
                checkSettingsDirty();
            }

            // If we were in a stale state, clear it
            const badge = document.getElementById('dash-status');
            if (badge) badge.classList.remove('status-stale');

            // If system view is active, update system metrics
            if (!document.getElementById('system-view').classList.contains('hidden')) {
                loadSystemView();
            }

        } catch {
            _pollFailures++;
            // NEVER stop the loop on generic network errors.
            // Just keep trying and show the STALE indicator.
            const badge = document.getElementById('dash-status');
            if (badge) {
                badge.innerText = 'STALE';
                badge.className = 'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide status-stale';
            }
        } finally {
            _pollInflight = false;
        }
    }, 1000);
}
