// Packages tab: list, install, remove, and the missing-import scanner.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        function _isPythonPackageRuntime() {
            return (activeApp?.runtime || 'python') === 'python';
        }

        function _syncPackagesTabRuntimeUi() {
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

        async function loadPackages() {
            const tbody = document.getElementById('pkg-list');
            const isPythonRuntime = _isPythonPackageRuntime();
            _syncPackagesTabRuntimeUi();
            tbody.innerHTML = `<tr><td colspan="${isPythonRuntime ? 3 : 2}" class="text-center py-8 text-sm" style="color:var(--text3)">Fetching environment...</td></tr>`;
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/packages`);
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
            } catch (e) { tbody.innerHTML = `<tr><td colspan="${isPythonRuntime ? 3 : 2}" class="text-center py-8 text-sm" style="color:var(--red)">Network error loading packages.</td></tr>`; }
        }
        async function installPackage() {
            const input = document.getElementById('pkg-input');
            const btn = document.getElementById('pkg-install-btn');
            const name = input.value.trim();
            if (!name) return;
            btn.disabled = true; btn.innerText = "Installing...";
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/packages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
                await waitForJob(res);
                showToast(`Installed ${name}!`, 'success'); input.value = ''; loadPackages();
            } catch (e) { showToast("Install failed: " + e.message, 'error'); }
            finally { btn.disabled = false; btn.innerText = "Install"; }
        }
        async function uninstallPackage(name) {
            document.getElementById('remove-pkg-name').textContent = name;
            document.getElementById('remove-pkg-modal').dataset.pkg = name;
            openModal('remove-pkg-modal');
        }
        async function confirmRemovePackage() {
            const name = document.getElementById('remove-pkg-modal').dataset.pkg;
            closeModal('remove-pkg-modal');
            if (!name) return;
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/packages/${encodeURIComponent(name)}`, { method: 'DELETE' });
                await waitForJob(res);
                showToast(`Removed ${name}`, 'success'); loadPackages();
            } catch (e) { showToast('Remove failed: ' + e.message, 'error'); }
        }

        // ── Import Scanner ────────────────────────────────────────────────────────
        let _missingPackages = [];
        async function scanImports() {
            const btn = document.getElementById('scan-btn');
            const results = document.getElementById('scan-results');
            const missingSection = document.getElementById('scan-missing-section');
            const okSection = document.getElementById('scan-ok-section');
            const missingList = document.getElementById('scan-missing-list');
            const missingCount = document.getElementById('scan-missing-count');
            btn.disabled = true;
            btn.innerHTML = '<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Scanning…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/scan-imports`);
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
                        installBtn.style.cssText = 'color:var(--blue);background:var(--ov);border:1px solid var(--ov-strong)';
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
        async function installSingle(name, btnEl) {
            const btn = btnEl; if (!btn) return;
            btn.disabled = true; btn.textContent = '…';
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/packages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
                const ok = await waitForJob(res).then(() => true, err => { showToast('Install failed: ' + err.message, 'error'); return false; });
                if (ok) {
                    btn.closest('li').remove();
                    const remaining = document.getElementById('scan-missing-list').children.length;
                    if (remaining === 0) { document.getElementById('scan-missing-section').classList.add('hidden'); document.getElementById('scan-ok-section').classList.remove('hidden'); }
                    else document.getElementById('scan-missing-count').textContent = remaining;
                    loadPackages();
                } else {
                    btn.disabled = false; btn.textContent = 'Install';
                }
            } catch (e) { showToast('Error: ' + e.message, 'error'); btn.disabled = false; btn.textContent = 'Install'; }
        }
        async function installAllMissing() {
            if (!_missingPackages.length) return;
            const btn = document.getElementById('install-all-btn');
            btn.disabled = true;
            btn.innerHTML = '<svg class="w-3 h-3 animate-spin inline mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Installing…';
            const failed = [];
            for (const pkg of _missingPackages) {
                try {
                    const res = await fetch(`${API}/apps/${activeAppId}/packages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: pkg }) });
                    await waitForJob(res);
                } catch (e) { failed.push(pkg); }
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
