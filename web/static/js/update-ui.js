// Updating Venvy from the System view (admins): the version card, and a modal that
// follows the update while the installer stops and restarts Venvy.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        let _updateInfo = null;
        let _updateWatch = null;

        async function loadUpdateCard(refresh = false) {
            const sub = $id('sys-update-card-sub'), btn = $id('sys-update-btn'), check = $id('sys-update-check');
            if (refresh) { check.disabled = true; sub.textContent = 'Checking…'; }
            const r = await apiJson('GET', `/system/update${refresh ? '?refresh=true' : ''}`);
            check.disabled = false;
            if (!r.ok) { sub.textContent = r.error; btn.classList.add('hidden'); return; }
            const d = _updateInfo = r.data;
            $id('sys-update-card-title').textContent = `Venvy ${d.current}`;
            let text;
            if (d.state === 'running' || d.state === 'requested') text = 'An update is under way…';
            else if (d.update_available) text = `${d.latest} is available`;
            else if (d.check_error) text = d.check_error;
            else if (d.latest) text = 'Up to date';
            else text = 'Not checked yet';
            if (d.state === 'failed' && !d.update_available) text += ' · the last update failed';
            sub.textContent = text;
            const notes = $id('sys-update-notes');
            notes.classList.toggle('hidden', !d.latest);
            notes.textContent = d.update_available ? "What's new" : 'Release notes';
            sub.title = d.supported ? '' : (d.unsupported_reason || '');
            btn.classList.toggle('hidden', !(d.update_available || d.state === 'failed'));
            btn.disabled = !d.supported || d.state === 'running' || d.state === 'requested';
            btn.title = d.supported ? '' : d.unsupported_reason;
            btn.textContent = d.update_available ? `Update to ${d.latest}` : 'Details';
            if (d.state === 'running' || d.state === 'requested') _watchUpdate();
        }

        async function onUpdateButton() {
            const d = _updateInfo;
            if (!d) return;
            if (!d.update_available) { _showUpdateModal(); _paintUpdate(d, null); return; }
            if (!d.supported) { showToast(d.unsupported_reason, 'error', 9000); return; }
            if (!await confirmDialog({
                title: `Update to ${d.latest}?`,
                body: 'Venvy downloads the release, checks it and switches over. It is unreachable for a minute or so while it restarts; your apps are stopped and started again with it.',
                sub: 'If the new version does not come up, the previous one is put back by itself.',
                confirmLabel: 'Update now',
            })) return;
            const r = await apiJson('POST', '/system/update');
            if (!r.ok) { showToast(r.error, 'error'); return; }
            _showUpdateModal();
            _paintUpdate({ ...d, state: 'requested', log: [] }, null);
            _watchUpdate();
        }

        function _showUpdateModal() {
            const body = buildModal('update-modal', 'Updating Venvy', { width: 'max-w-2xl', zIndex: 90 });
            body.innerHTML = `
                <p id="upd-status" class="text-sm mb-3" style="color:var(--text2)"></p>
                <pre id="upd-log" class="mono text-xs p-3 rounded-lg overflow-auto whitespace-pre-wrap break-words hidden"
                    style="background:var(--surface2);border:1px solid var(--border2);color:var(--text2);max-height:22rem"></pre>
                <div class="flex justify-end mt-4"><button id="upd-reload" class="btn-primary px-4 py-2 text-sm font-bold hidden" onclick="location.reload()">Reload Venvy</button></div>`;
            openModal('update-modal');
        }

        function _paintUpdate(d, note) {
            const status = $id('upd-status');
            if (!status) return;
            const from = _updateInfo?.current;
            let text;
            if (note) text = note;
            else if (d.state === 'requested') text = 'Starting the update…';
            else if (d.state === 'running') text = 'Downloading, checking and installing. Venvy restarts at the end — this page waits for it.';
            else if (d.state === 'ok' && from && d.current !== from) text = `Done: Venvy ${d.current} is running.`;
            else if (d.state === 'ok') text = `Done: Venvy ${d.current} is running.`;
            else if (d.state === 'failed') text = `The update failed. Venvy ${d.current} is still running — the log below says why.`;
            else text = '';
            status.textContent = text;
            const log = $id('upd-log');
            const lines = d.log || [];
            log.classList.toggle('hidden', !lines.length);
            if (lines.length) { log.textContent = lines.join('\n'); log.scrollTop = log.scrollHeight; }
            $id('upd-reload').classList.toggle('hidden', !(d.state === 'ok' && from && d.current !== from));
        }

        function _watchUpdate() {
            if (_updateWatch) return;
            const startedFrom = _updateInfo?.current;
            let away = false;
            _updateWatch = setInterval(async () => {
                const r = await apiJson('GET', '/system/update');
                if (!r.ok) {
                    // Expected for a while: the installer has stopped Venvy to switch it over.
                    away = true;
                    _paintUpdate({ state: 'running', log: [] }, 'Venvy is restarting with the new version…');
                    return;
                }
                const d = r.data;
                _paintUpdate(d, null);
                const finished = d.state === 'ok' || d.state === 'failed';
                if (finished || (away && d.current !== startedFrom)) {
                    clearInterval(_updateWatch); _updateWatch = null;
                    _updateInfo = { ..._updateInfo, ...d, current: startedFrom };
                    _paintUpdate(d, null);
                    if (d.state === 'ok' && d.current !== startedFrom) showToast(`Venvy ${d.current} is running — reload to use it.`, 'success', 9000);
                    else if (d.state === 'failed') showToast('The update failed; nothing changed.', 'error');
                    loadUpdateCard();
                }
            }, 2500);
        }

        // ── What's new: the changelog sections newer than the running version ──
        async function openReleaseNotes() {
            const d = _updateInfo || {};
            const body = buildModal('notes-modal', d.update_available ? "What's new" : 'Release notes', { width: 'max-w-xl', zIndex: 85 });
            body.innerHTML = '<p class="text-xs py-6 text-center" style="color:var(--text3)">Loading…</p>';
            openModal('notes-modal');
            const r = await apiJson('GET', '/system/update/notes');
            if (!r.ok) { body.innerHTML = `<p class="text-xs" style="color:var(--red)">${escapeHtml(r.error)}</p>`; return; }
            const rel = r.data.releases || [];
            const intro = d.update_available
                ? `You run ${escapeHtml(r.data.current)}. ${rel.length === 1 ? 'This is what the new release brings' : `These ${rel.length} releases came out since`}:`
                : `You run the latest release, ${escapeHtml(r.data.current)}.`;
            body.innerHTML = `
                <p class="text-sm mb-4" style="color:var(--text2)">${intro}</p>
                ${rel.length ? rel.map(sec => `
                    <div class="mb-4">
                        <h3 class="text-sm font-bold mb-1.5" style="color:var(--text)">${escapeHtml(sec.version)}</h3>
                        <ul class="list-disc pl-5 space-y-1 text-sm" style="color:var(--text2)">
                            ${sec.items.map(it => `<li>${escapeHtml(it)}</li>`).join('')}
                        </ul>
                    </div>`).join('') : `<p class="text-xs" style="color:var(--text3)">${escapeHtml(r.data.error || 'No release notes were published.')}</p>`}
                ${d.update_available && d.supported ? `<div class="flex justify-end mt-2"><button class="btn-primary px-4 py-2 text-sm font-bold" onclick="closeModal('notes-modal'); onUpdateButton()">Update to ${escapeHtml(d.latest)}</button></div>` : ''}`;
        }
