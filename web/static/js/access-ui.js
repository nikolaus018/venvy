// API tokens (account menu) and per-user app access (Manage Users).
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        // ── API tokens ───────────────────────────────────────────────────────────
        async function openTokensModal() {
            const body = buildModal('tokens-modal', 'API tokens', { width: 'max-w-2xl', zIndex: 95 });
            body.innerHTML = '<p class="text-xs py-6 text-center" style="color:var(--text3)">Loading…</p>';
            openModal('tokens-modal');
            await renderTokens();
        }

        async function renderTokens(fresh = null) {
            const r = await apiJson('GET', '/tokens');
            const body = $id('tokens-modal-body');
            if (!r.ok) { body.innerHTML = `<p class="text-xs" style="color:var(--red)">${escapeHtml(r.error)}</p>`; return; }
            body.innerHTML = `
                <p class="text-xs mb-4" style="color:var(--text3)">For scripts and CI: send <span class="mono">Authorization: Bearer &lt;token&gt;</span> to this server's API. A token acts as you — your role, your apps. It cannot make other tokens or change your password.</p>
                ${fresh ? `<div class="rounded-xl p-4 mb-4" style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25)">
                    <p class="text-xs font-semibold mb-2" style="color:var(--green)">Copy it now — it is not shown again.</p>
                    <div class="flex gap-2"><input readonly id="tok-new" class="inp flex-1 px-3 py-2 text-xs mono" value="${escapeHtml(fresh)}"><button class="btn-secondary px-3 py-1.5 text-xs" data-on-click="copyFrom('tok-new', 'Token copied')">Copy</button></div></div>` : ''}
                <div class="grid grid-cols-1 md:grid-cols-4 gap-2 mb-5 items-end">
                    <div class="md:col-span-2">${fieldHtml('tok-name', 'Name', { placeholder: 'GitHub Actions' })}</div>
                    ${selectHtml('tok-scope', 'Access', [['full', 'Full'], ['read', 'Read-only']], 'full')}
                    ${selectHtml('tok-exp', 'Expires', [['', 'Never'], ['30', 'In 30 days'], ['90', 'In 90 days'], ['365', 'In a year']], '90')}
                </div>
                <button class="btn-primary px-4 py-2 text-xs font-bold mb-5" data-on-click="createToken()">Create token</button>
                <div class="space-y-2">${r.data.length ? r.data.map(t => `
                    <div class="card px-4 py-3 flex items-center gap-3">
                        <div class="flex-1 min-w-0"><p class="text-sm font-semibold truncate" style="color:var(--text)">${escapeHtml(t.name)} <span class="text-[10px] uppercase font-bold ml-1" style="color:var(--text3)">${t.scope === 'read' ? 'read-only' : 'full'}</span></p>
                            <p class="text-xs mt-0.5" style="color:var(--text3)"><span class="mono">${escapeHtml(t.prefix)}…</span> · ${t.last_used_at ? 'used ' + escapeHtml(fmtAgo(t.last_used_at)) : 'never used'}${t.expires_at ? ' · expires ' + escapeHtml(fmtWhen(t.expires_at)) : ''}</p></div>
                        <button class="tok-del btn-danger px-2.5 py-1.5 text-xs rounded-lg font-bold" data-id="${t.id}">Revoke</button>
                    </div>`).join('') : '<p class="text-xs py-4 text-center" style="color:var(--text3)">No tokens yet.</p>'}</div>`;
            body.querySelectorAll('.tok-del').forEach(b => b.addEventListener('click', async () => {
                const d = await apiJson('DELETE', `/tokens/${b.dataset.id}`);
                if (!d.ok) { showToast(d.error, 'error'); return; }
                renderTokens();
            }));
        }

        async function createToken() {
            const name = _val('tok-name');
            if (!name) { showToast('Give the token a name.', 'warn'); return; }
            const exp = $id('tok-exp').value;
            const r = await apiJson('POST', '/tokens', { name, scope: $id('tok-scope').value, expires_days: exp ? parseInt(exp, 10) : null });
            if (!r.ok) { showToast(r.error, 'error'); return; }
            renderTokens(r.data.token);
        }

        // ── Which apps a user may use ────────────────────────────────────────────
        async function openUserAccess(userId, username) {
            const [u, apps] = await Promise.all([apiJson('GET', '/users'), apiJson('GET', '/apps')]);
            if (!u.ok || !apps.ok) { showToast(u.error || apps.error, 'error'); return; }
            const user = u.data.find(x => x.id === userId);
            const body = buildModal('access-modal', `Apps for ${username}`, { width: 'max-w-md', zIndex: 95 });
            body.innerHTML = `<div class="space-y-3">
                <label class="flex items-center gap-2 text-sm cursor-pointer select-none" style="color:var(--text)"><input type="radio" name="acc-mode" value="all" ${user.all_apps ? 'checked' : ''}> All apps, including ones made later</label>
                <label class="flex items-center gap-2 text-sm cursor-pointer select-none" style="color:var(--text)"><input type="radio" name="acc-mode" value="some" ${user.all_apps ? '' : 'checked'}> Only these:</label>
                <div id="acc-list" class="pl-6 space-y-1 max-h-56 overflow-y-auto ${user.all_apps ? 'opacity-40 pointer-events-none' : ''}">
                    ${apps.data.map(a => `<label class="flex items-center gap-2 text-sm cursor-pointer select-none" style="color:var(--text2)"><input type="checkbox" class="acc-app rounded" value="${a.id}" ${user.app_ids.includes(a.id) ? 'checked' : ''}> ${escapeHtml(a.name)}</label>`).join('') || '<p class="text-xs" style="color:var(--text3)">No apps yet.</p>'}
                </div>
                <p class="text-xs" style="color:var(--text3)">Without access an app does not exist for them: it is not listed, and its files, logs and terminal are out of reach. What they create themselves, they can use.</p>
                <div class="flex gap-3 pt-1"><button class="btn-ghost flex-1 py-2.5 text-sm font-medium rounded-xl" data-on-click="closeModal('access-modal')">Cancel</button>
                    <button class="btn-primary flex-1 py-2.5 text-sm rounded-xl" id="acc-save">Save</button></div></div>`;
            document.querySelectorAll('input[name="acc-mode"]').forEach(r => r.addEventListener('change', () => {
                const all = document.querySelector('input[name="acc-mode"]:checked').value === 'all';
                $id('acc-list').classList.toggle('opacity-40', all); $id('acc-list').classList.toggle('pointer-events-none', all);
            }));
            $id('acc-save').addEventListener('click', async () => {
                const all = document.querySelector('input[name="acc-mode"]:checked').value === 'all';
                const ids = Array.from(document.querySelectorAll('.acc-app:checked')).map(c => Number(c.value));
                const r = await apiJson('PUT', `/users/${userId}/access`, { all_apps: all, app_ids: ids });
                if (!r.ok) { showToast(r.error, 'error'); return; }
                closeModal('access-modal'); showToast('Saved', 'success', 1500); loadUsers();
            });
            openModal('access-modal');
        }
