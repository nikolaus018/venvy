/**
 * The app settings tab, its autosave behaviour and app export.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { showToast } from "../core/dom.js";
import { _getOpenUrlPref, _lastAppListHash, _renderOpenPrefButtons, _updateNetworkCard, loadApps } from "./apps.js";
import { loadCfRouteStatus } from "./cloudflare.js";
import { _termDestroy } from "./terminal.js";
import { closeModal, openModal } from "../ui/modals.js";
import { _featherReplace } from "../core/dom.js";

export let _autoSaveTimer = null;

export function _showAutosave(state) {
    // state: 'saving' | 'saved' | 'error'
    const row = document.getElementById('autosave-indicator');
    const spinner = document.getElementById('autosave-spinner');
    const check = document.getElementById('autosave-check');
    const text = document.getElementById('autosave-text');
    if (!row) return;
    row.classList.remove('hidden');
    spinner.classList.toggle('hidden', state !== 'saving');
    check.classList.toggle('hidden', state !== 'saved');
    if (state === 'saving') { text.textContent = 'Saving…'; text.style.color = 'var(--text3)'; }
    else if (state === 'saved') { text.textContent = 'Saved'; text.style.color = '#34d399'; }
    else { text.textContent = 'Save failed'; text.style.color = 'var(--red)'; }
    if (state === 'saved') {
        if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
        _autoSaveTimer = setTimeout(() => row.classList.add('hidden'), 2500);
    }
}

export function onRuntimeChange(context) {
    let runtime, entryInp;
    if (context === 'create') {
        const form = document.getElementById('create-form');
        runtime = form.runtime.value;
        entryInp = form.entrypoint;
    } else {
        runtime = document.getElementById('setting-runtime').value;
        entryInp = document.getElementById('setting-entrypoint');
    }

    if (runtime === 'rust') {
        entryInp.value = 'main.rs';
        entryInp.disabled = true;
        entryInp.style.opacity = '0.5';
        entryInp.style.cursor = 'not-allowed';
    } else {
        if (entryInp.value === 'main.rs') entryInp.value = 'main.py';
        entryInp.disabled = false;
        entryInp.style.opacity = '';
        entryInp.style.cursor = '';
    }
}

export async function saveSettings() {
    if (!state.activeAppId) return;
    const btn = document.getElementById('settings-save-btn');
    const btnText = document.getElementById('save-btn-text');

    const runtime = document.getElementById('setting-runtime').value;
    const policy = document.getElementById('setting-policy').value;
    const boot = document.getElementById('setting-boot').checked;
    const portsStr = document.getElementById('setting-port').value.trim();
    const manualPorts = portsStr.split(/[\s,]+/).map(p => parseInt(p.trim(), 10)).filter(p => !isNaN(p) && p > 0);
    const entrypoint = document.getElementById('setting-entrypoint').value.trim();
    const name = document.getElementById('setting-name').value.trim();

    if (portsStr && manualPorts.length === 0) {
        showToast('Invalid ports format. Use comma-separated numbers.', 'warn'); return;
    }

    const env = {};
    try {
        const raw = document.getElementById('setting-env').value || '{}';
        const parsed = JSON.parse(raw);
        if (typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
        for (const [k, v] of Object.entries(parsed)) env[String(k)] = String(v);
    } catch {
        showToast('Invalid JSON in Environment Variables.', 'error');
        return;
    }

    const body = {
        name: name,
        runtime: runtime,
        restart_policy: policy,
        start_on_boot: boot,
        env_vars: env,
        entrypoint,
        manual_ports: manualPorts,
        url_source_pref: _getOpenUrlPref()
    };

    btn.disabled = true;
    btnText.innerText = 'Saving...';
    _showAutosave('saving');

    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!res.ok) {
            const e = await res.json().catch(() => ({ detail: 'Save failed' }));
            showToast('Error: ' + (e.detail || 'Save failed'), 'error');
            _showAutosave('error');
            return;
        }
        state.activeApp = await res.json();

        // Sync UI
        let portsDisplay = '';
        try {
            const m_ports = JSON.parse(state.activeApp.manual_ports || '[]');
            if (Array.isArray(m_ports) && m_ports.length > 0) {
                portsDisplay = m_ports.join(', ');
            }
        } catch { }
        document.getElementById('setting-port').value = portsDisplay;
        document.getElementById('setting-entrypoint').value = state.activeApp.entrypoint || '';
        state._tempOpenUrlPref = state.activeApp.url_source_pref || 'local';
        _renderOpenPrefButtons(state._tempOpenUrlPref);
        _updateNetworkCard(state.activeApp);
        checkSettingsDirty();

        _showAutosave('saved');
        checkSettingsDirty();
        loadCfRouteStatus();
        showToast('Settings saved successfully.', 'success');
    } catch (e) {
        showToast('Network error: ' + e.message, 'error');
        _showAutosave('error');
    } finally {
        btn.disabled = false;
        btnText.innerText = 'Save Changes';
    }
}

export function checkSettingsDirty() {
    if (!state.activeApp) return;
    const runtime = document.getElementById('setting-runtime').value;
    const policy = document.getElementById('setting-policy').value;
    const boot = document.getElementById('setting-boot').checked;
    const portsStr = document.getElementById('setting-port').value.trim();
    const manualPorts = portsStr.split(/[\s,]+/).map(p => parseInt(p.trim(), 10)).filter(p => !isNaN(p) && p > 0);
    const entrypoint = document.getElementById('setting-entrypoint').value.trim();

    let currentManualPorts = [];
    try {
        const parsed = JSON.parse(state.activeApp.manual_ports || '[]');
        if (Array.isArray(parsed) && parsed.length > 0) currentManualPorts = parsed;
    } catch { }
    if (currentManualPorts.length === 0 && state.activeApp.port) {
        currentManualPorts = [state.activeApp.port];
    }
    const portsMatch = JSON.stringify(manualPorts) === JSON.stringify(currentManualPorts);

    let envMatch = true;
    try {
        const raw = (document.getElementById('setting-env').value || '{}').trim();
        if (raw && raw.startsWith('// Hidden')) {
            envMatch = true;
        } else {
            const activeEnv = JSON.parse(state.activeApp.env_json || '{}');
            const inputEnv = JSON.parse(raw || '{}');
            if (JSON.stringify(activeEnv) !== JSON.stringify(inputEnv)) {
                envMatch = false;
            }
        }
    } catch { envMatch = false; }

    const currentEntryPoint = (state.activeApp.runtime === 'rust' || runtime === 'rust') ? 'main.rs' : (state.activeApp.entrypoint || '');
    const normalizedEntry = runtime === 'rust' ? 'main.rs' : entrypoint;

    const isDirty = (
        runtime !== (state.activeApp.runtime || 'python') ||
        policy !== state.activeApp.restart_policy ||
        boot !== state.activeApp.start_on_boot ||
        !portsMatch ||
        normalizedEntry !== currentEntryPoint ||
        _getOpenUrlPref() !== (state.activeApp.url_source_pref || 'local') ||
        !envMatch
    );

    const dot = document.getElementById('save-status-dot');
    const btn = document.getElementById('settings-save-btn');
    if (isDirty) {
        dot.classList.remove('hidden');
        btn.classList.add('pulse-active');
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-orange');
    } else {
        dot.classList.add('hidden');
        btn.classList.remove('pulse-active');
        btn.classList.add('btn-primary');
        btn.classList.remove('btn-orange');
    }
}

export let _settingsWired = false;

export function _wireSettingsDirtyCheck() {
    if (_settingsWired) return;
    _settingsWired = true;
    ['setting-runtime', 'setting-policy', 'setting-boot'].forEach(id => {
        document.getElementById(id)?.addEventListener('change', checkSettingsDirty);
    });
    ['setting-entrypoint', 'setting-env', 'setting-port'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', checkSettingsDirty);
    });
}

export async function _batchedRun(fns, batchSize = 5) {
    const results = [];
    for (let i = 0; i < fns.length; i += batchSize) {
        const batch = fns.slice(i, i + batchSize);
        results.push(...await Promise.all(batch.map(fn => fn())));
    }
    return results;
}

export async function downloadAppZip() {
    if (!state.activeAppId) return;
    const btn = document.getElementById('btn-download-zip');
    const originalHTML = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<div class="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin"></div> Compressing...`;
    try {
        const zip = new JSZip();
        async function addFolderToZip(folderPath, zipInstance) {
            const res = await fetch(`${API}/apps/${state.activeAppId}/files/tree?path=${encodeURIComponent(folderPath)}`);
            if (!res.ok) throw new Error("Failed to list files");
            const items = await res.json();
            const dirs = items.filter(i => i.is_dir), files = items.filter(i => !i.is_dir);
            for (const dir of dirs) await addFolderToZip(dir.path, zipInstance.folder(dir.name));
            await _batchedRun(files.map(file => async () => {
                const fileRes = await fetch(`${API}/apps/${state.activeAppId}/files/download?path=${encodeURIComponent(file.path)}`);
                if (fileRes.ok) zipInstance.file(file.name, await fileRes.blob());
            }), 5);
        }
        await addFolderToZip("", zip);
        const content = await zip.generateAsync({ type: "blob" });
        const url = window.URL.createObjectURL(content);
        const a = document.createElement('a');
        a.href = url; a.download = `${state.activeApp?.name || 'app'}_source.zip`;
        document.body.appendChild(a); a.click();
        window.URL.revokeObjectURL(url); document.body.removeChild(a);
    } catch (e) { showToast("Export failed: " + e.message, "error"); }
    finally { btn.disabled = false; btn.innerHTML = originalHTML; _featherReplace(); }
}

export function deleteApp() {
    if (!state.activeApp) return;
    document.getElementById('delete-app-name').textContent = state.activeApp.name;
    openModal('delete-app-modal');
}

export async function confirmDeleteApp() {
    closeModal('delete-app-modal');
    // Clean up terminal resources before deleting
    _termDestroy();
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error();
        showToast('App deleted.', 'info');
        state.activeAppId = null; state.activeApp = null;
        if (state.logInterval) { clearInterval(state.logInterval); state.logInterval = null; }
        _lastAppListHash = '';
        document.getElementById('dashboard').classList.add('hidden');
        document.getElementById('empty-state').classList.remove('hidden');
        loadApps();
    } catch { showToast('Delete failed.', 'error'); }
}
