// Updating Venvy from the System view (admins): the version card, and a modal that
// follows the update while the installer stops and restarts Venvy.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        let _updateInfo = null;
        let _updateWatch = null;
        let _updateFrom = null;     // the version an update under way started from

        async function loadUpdateCard(refresh = false) {
            const sub = $id('sys-update-card-sub'), btn = $id('sys-update-btn'), check = $id('sys-update-check');
            if (refresh) { check.disabled = true; sub.textContent = 'Checking…'; }
            const r = await apiJson('GET', `/system/update${refresh ? '?refresh=true' : ''}`);
            check.disabled = false;
            if (!r.ok) { sub.textContent = r.error; btn.classList.add('hidden'); return; }
            const d = _updateInfo = r.data;
            $id('sys-update-card-title').textContent = `Venvy ${d.current}`;
            const underway = d.state === 'running' || d.state === 'requested';
            let text;
            if (underway) text = 'An update is under way…';
            else if (d.update_available) text = `${d.latest} is available`;
            else if (d.check_error) text = d.check_error;
            else if (d.latest) text = 'Up to date';
            else text = 'Not checked yet';
            if (d.checked_at && !underway) text += ` · checked ${fmtAgo(d.checked_at)}`;
            if (d.state === 'failed' && !d.update_available) text += ' · the last update failed';
            sub.textContent = text;
            const notes = $id('sys-update-notes');
            notes.classList.toggle('hidden', !d.latest);
            notes.textContent = d.update_available ? "What's new" : 'Release notes';
            sub.title = d.supported ? '' : (d.unsupported_reason || '');
            btn.classList.toggle('hidden', !(d.update_available || d.state === 'failed'));
            btn.disabled = !d.supported || underway;
            btn.title = d.supported ? '' : d.unsupported_reason;
            btn.textContent = d.update_available ? `Update to ${d.latest}` : 'Details';
            if (underway) _watchUpdate();
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
            loadUpdateCard();
        }

        function _showUpdateModal() {
            const body = buildModal('update-modal', 'Updating Venvy', { width: 'max-w-2xl', zIndex: 90 });
            body.innerHTML = `
                <p id="upd-status" class="text-sm mb-3" style="color:var(--text2)"></p>
                <pre id="upd-log" class="mono text-xs p-3 rounded-lg overflow-auto whitespace-pre-wrap break-words hidden"
                    style="background:var(--surface2);border:1px solid var(--border2);color:var(--text2);max-height:22rem"></pre>
                <div class="flex justify-end mt-4"><button id="upd-reload" class="btn-primary px-4 py-2 text-sm font-bold hidden" data-on-click="reloadPage()">Reload Venvy</button></div>`;
            openModal('update-modal');
        }

        function _paintUpdate(d, note) {
            const status = $id('upd-status');
            if (!status) return;
            const from = _updateFrom || _updateInfo?.current;
            let text;
            if (note) text = note;
            else if (d.state === 'requested') text = 'Starting the update…';
            else if (d.state === 'running' && from && d.current && d.current !== from) text = `Venvy ${d.current} is up; finishing the update…`;
            else if (d.state === 'running') text = 'Downloading, checking and installing. Venvy restarts at the end — this page waits for it.';
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
            // Kept across the restart: the new Venvy reports "running" until update.sh is done.
            const startedFrom = _updateFrom = _updateFrom || _updateInfo?.current;
            _updateWatch = setInterval(async () => {
                const r = await apiJson('GET', '/system/update');
                if (!r.ok) {
                    // Expected for a while: the installer has stopped Venvy to switch it over.
                    _paintUpdate({ state: 'running', log: [] }, 'Venvy is restarting with the new version…');
                    return;
                }
                const d = r.data;
                _paintUpdate(d, null);
                if (d.state === 'ok' || d.state === 'failed') {
                    clearInterval(_updateWatch); _updateWatch = null; _updateFrom = null;
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
            const cur = escapeHtml(r.data.current);
            let rel = r.data.releases || [], intro;
            if (r.data.newer) {
                intro = rel.length === 1 ? `You run ${cur}. This is what ${escapeHtml(rel[0].version)} brings:`
                                         : `You run ${cur}. These ${rel.length} releases came out since:`;
            } else if (d.update_available) {
                // The offered release published no notes: say so rather than show older ones as new.
                intro = `You run ${cur}. ${escapeHtml(d.latest)} did not publish release notes.`;
                rel = [];
            } else {
                intro = `You run the latest release, ${cur}.${rel.length ? ' What it brought:' : ''}`;
            }
            body.innerHTML = `
                <p class="text-sm mb-4" style="color:var(--text2)">${intro}</p>
                ${rel.length ? rel.map(sec => `
                    <div class="mb-4">
                        <h3 class="text-sm font-bold mb-1.5" style="color:var(--text)">${escapeHtml(sec.version)}</h3>
                        <ul class="list-disc pl-5 space-y-1 text-sm" style="color:var(--text2)">
                            ${sec.items.map(it => `<li>${escapeHtml(it)}</li>`).join('')}
                        </ul>
                    </div>`).join('') : (r.data.error ? `<p class="text-xs" style="color:var(--text3)">${escapeHtml(r.data.error)}</p>` : '')}
                ${d.update_available && d.supported ? `<div class="flex justify-end mt-2"><button class="btn-primary px-4 py-2 text-sm font-bold" data-on-click="closeModal('notes-modal'); onUpdateButton()">Update to ${escapeHtml(d.latest)}</button></div>` : ''}`;
        }
