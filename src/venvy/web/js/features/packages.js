/**
 * Dependency management and the missing-import scanner.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { showToast } from "../core/dom.js";
import { loadPackages } from "./logs.js";
import { _featherReplace } from "../core/dom.js";

export let _missingPackages = [];

export async function scanImports() {
    const btn = document.getElementById('scan-btn');
    const results = document.getElementById('scan-results');
    const missingSection = document.getElementById('scan-missing-section');
    const okSection = document.getElementById('scan-ok-section');
    const missingList = document.getElementById('scan-missing-list');
    const missingCount = document.getElementById('scan-missing-count');
    btn.disabled = true;
    btn.innerHTML = '<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Scanning…';
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/scan-imports`);
        if (!res.ok) throw new Error('Scan failed');
        const data = await res.json();
        _missingPackages = data.missing || [];
        results.classList.remove('hidden');
        missingSection.classList.add('hidden'); okSection.classList.add('hidden'); missingList.innerHTML = '';
        if (_missingPackages.length === 0) {
            okSection.classList.remove('hidden');
        } else {
            missingCount.textContent = _missingPackages.length;
            missingSection.classList.remove('hidden');
            _missingPackages.forEach(pkg => {
                const li = document.createElement('li');
                li.className = 'flex items-center justify-between px-5 py-2.5';
                li.style.borderColor = 'var(--border)';
                const nameSpan = document.createElement('span');
                nameSpan.className = 'mono text-xs'; nameSpan.style.color = 'var(--text)'; nameSpan.textContent = pkg;
                const installBtn = document.createElement('button');
                installBtn.className = 'text-xs font-bold px-3 py-1 rounded-lg transition';
                installBtn.style.cssText = 'color:var(--blue);background:rgba(255, 255, 255,0.1);border:1px solid rgba(255, 255, 255,0.15)';
                installBtn.textContent = 'Install';
                installBtn.addEventListener('click', () => installSingle(pkg, installBtn));
                li.appendChild(nameSpan); li.appendChild(installBtn);
                missingList.appendChild(li);
            });
        }
        _featherReplace();
    } catch (e) { showToast('Scan error: ' + e.message, 'error'); }
    finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-feather="search" class="w-3.5 h-3.5"></i> Scan';
        _featherReplace();
    }
}

export async function installSingle(name, btnEl) {
    const btn = btnEl; if (!btn) return;
    btn.disabled = true; btn.textContent = '…';
    try {
        const res = await fetch(`${API}/apps/${state.activeAppId}/packages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
        if (res.ok) {
            btn.closest('li').remove();
            const remaining = document.getElementById('scan-missing-list').children.length;
            if (remaining === 0) { document.getElementById('scan-missing-section').classList.add('hidden'); document.getElementById('scan-ok-section').classList.remove('hidden'); }
            else document.getElementById('scan-missing-count').textContent = remaining;
            loadPackages();
        } else {
            const e = await res.json().catch(() => ({ detail: 'Failed' }));
            showToast('Install failed: ' + (e.detail || ''), 'error');
            btn.disabled = false; btn.textContent = 'Install';
        }
    } catch (e) { showToast('Error: ' + e.message, 'error'); btn.disabled = false; btn.textContent = 'Install'; }
}

export async function installAllMissing() {
    if (!_missingPackages.length) return;
    const btn = document.getElementById('install-all-btn');
    btn.disabled = true;
    btn.innerHTML = '<svg class="w-3 h-3 animate-spin inline mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Installing…';
    const failed = [];
    for (const pkg of _missingPackages) {
        try {
            const res = await fetch(`${API}/apps/${state.activeAppId}/packages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: pkg }) });
            if (!res.ok) failed.push(pkg);
        } catch { failed.push(pkg); }
    }
    if (failed.length) {
        showToast('Could not install: ' + failed.join(', '), 'error', 5000);
        _missingPackages = failed;
        document.getElementById('scan-missing-count').textContent = failed.length;
        document.getElementById('scan-missing-list').querySelectorAll('li').forEach(li => { if (!failed.includes(li.querySelector('span').textContent)) li.remove(); });
    } else {
        document.getElementById('scan-missing-section').classList.add('hidden');
        document.getElementById('scan-ok-section').classList.remove('hidden');
        _missingPackages = [];
    }
    btn.disabled = false;
    btn.innerHTML = '<i data-feather="download" class="w-3 h-3"></i> Install All';
    _featherReplace(); loadPackages();
}
