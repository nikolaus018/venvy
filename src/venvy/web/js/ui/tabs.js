/**
 * Tab switching within an app's detail view.
 */

import { state } from "../core/state.js";
import { loadCfRouteStatus } from "../features/cloudflare.js";
import { loadFiles } from "../features/files.js";
import { _logsInflight, loadLogs, loadPackages } from "../features/logs.js";
import { checkSettingsDirty } from "../features/settings.js";
import { _termFit } from "../features/terminal.js";

export function switchTab(tab) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    document.querySelectorAll('.tab-btn').forEach(el => {
        el.classList.remove('active');
        el.style.color = 'var(--text3)';
        el.style.borderBottomColor = 'transparent';
    });
    document.getElementById(`tab-${tab}`).classList.remove('hidden');
    const btn = document.querySelector(`button[onclick="switchTab('${tab}')"]`);
    if (btn) {
        btn.classList.add('active');
        btn.style.color = 'var(--text)';
        btn.style.borderBottomColor = 'var(--accent)';
    }
    if (state.logInterval) { clearInterval(state.logInterval); state.logInterval = null; _logsInflight = false; }
    if (tab === 'logs' && !document.hidden) { loadLogs(); state.logInterval = setInterval(loadLogs, 1000); }
    else if (tab === 'logs') loadLogs();
    if (tab === 'files') loadFiles(state.currentBrowserPath);
    if (tab === 'packages') loadPackages();
    if (tab === 'terminal' && window.term && window.fitAddon) setTimeout(_termFit, 100);
    if (tab === 'settings') {
        loadCfRouteStatus();
        document.getElementById('settings-save-container').classList.remove('hidden');
        document.getElementById('settings-save-container').classList.add('flex');
        checkSettingsDirty();
    } else {
        document.getElementById('settings-save-container').classList.add('hidden');
        document.getElementById('settings-save-container').classList.remove('flex');
    }
}

export function fileOp(op) { if (op === 'refresh') loadFiles(state.currentBrowserPath); }
