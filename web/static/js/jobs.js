// Jobs tab: an app's scheduled commands.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        let _jobs = [];
        let _jobsTimer = null;
        const _SCHEDULE_PRESETS = [
            ['*/5 * * * *', 'Every 5 minutes'], ['0 * * * *', 'Every hour'], ['0 3 * * *', 'Every day at 03:00'],
            ['0 3 * * 0', 'Every Sunday at 03:00'], ['0 3 1 * *', 'On the 1st of each month at 03:00'], ['* * * * *', 'Every minute'],
        ];

        async function loadJobs() {
            const appId = activeAppId;
            if (!appId) return;
            const r = await apiJson('GET', `/apps/${appId}/jobs`);
            if (appId !== activeAppId) return;
            if (!r.ok) { $id('jobs-list').innerHTML = `<p class="text-xs text-center py-8" style="color:var(--red)">${escapeHtml(r.error)}</p>`; return; }
            _jobs = r.data;
            renderJobs();
            if (_jobsTimer) { clearTimeout(_jobsTimer); _jobsTimer = null; }
            if (currentTab === 'jobs') _jobsTimer = setTimeout(() => { if (currentTab === 'jobs' && activeAppId === appId) loadJobs(); }, _jobs.some(j => j.running) ? 1500 : 15000);
        }

        function renderJobs() {
            const el = $id('jobs-list');
            if (!_jobs.length) {
                el.innerHTML = `<div class="card px-5 py-10 text-center"><p class="text-sm font-semibold" style="color:var(--text2)">No jobs yet</p>
                    <p class="text-xs mt-1" style="color:var(--text3)">Run a cleanup script every night, send a digest every Monday, refresh a cache every 10 minutes…</p></div>`;
                return;
            }
            el.innerHTML = _jobs.map(j => `
                <div class="card p-4" data-job="${j.id}">
                    <div class="flex items-start gap-3">
                        <div class="flex-1 min-w-0">
                            <p class="text-sm font-semibold flex items-center gap-2 flex-wrap" style="color:var(--text)">${escapeHtml(j.name)}
                                ${j.running ? statusPill('running') : (j.last_status ? statusPill(j.last_status) : '')}
                                ${j.enabled ? '' : '<span class="text-[10px] uppercase font-bold" style="color:var(--text3)">paused</span>'}</p>
                            <p class="text-xs mt-1 mono" style="color:var(--text3)">${escapeHtml(j.schedule)}${j.next_run ? ` · next ${escapeHtml(fmtAgo(j.next_run))}` : ''}</p>
                            ${j.command ? `<p class="text-xs mt-1 mono truncate" style="color:var(--text2)" title="${escapeHtml(j.command)}">$ ${escapeHtml(j.command)}</p>` : ''}
                            <p class="text-xs mt-1" style="color:var(--text3)">${j.last_run_at ? `Last run ${escapeHtml(fmtAgo(j.last_run_at))}${j.last_duration != null ? ` · ${j.last_duration}s` : ''}${j.last_exit_code != null && j.last_exit_code !== 0 ? ` · exit ${j.last_exit_code}` : ''}` : 'Has not run yet'}</p>
                        </div>
                        <div class="flex gap-1.5 shrink-0" data-requires-operator>
                            <button class="job-run btn-ghost px-2.5 py-1.5 text-xs" ${j.running ? 'disabled' : ''}>Run now</button>
                            <button class="job-edit btn-ghost px-2.5 py-1.5 text-xs">Edit</button>
                            <button class="job-del btn-danger px-2.5 py-1.5 text-xs rounded-lg font-bold">Delete</button>
                        </div>
                    </div>
                    ${j.last_output ? `<details class="mt-3"><summary class="text-xs cursor-pointer select-none" style="color:var(--text3)">Output of the last run</summary>
                        <pre class="mono text-xs mt-2 p-3 rounded-lg overflow-auto whitespace-pre-wrap break-words" style="background:var(--surface2);border:1px solid var(--border2);color:var(--text2);max-height:14rem">${escapeHtml(j.last_output)}</pre></details>` : ''}
                </div>`).join('');
            el.querySelectorAll('[data-job]').forEach(card => {
                const id = Number(card.dataset.job);
                card.querySelector('.job-run')?.addEventListener('click', () => runJobNow(id));
                card.querySelector('.job-edit')?.addEventListener('click', () => openJobModal(id));
                card.querySelector('.job-del')?.addEventListener('click', () => deleteJob(id));
            });
        }

        function openJobModal(id = null) {
            const j = id ? _jobs.find(x => x.id === id) : null;
            const body = buildModal('job-modal', j ? 'Edit job' : 'Add job', { width: 'max-w-lg', zIndex: 85 });
            const known = j && _SCHEDULE_PRESETS.some(p => p[0] === j.schedule) ? j.schedule : (j ? 'custom' : '0 3 * * *');
            body.innerHTML = `<div class="space-y-4">
                ${fieldHtml('job-name', 'Name', { value: j?.name || '', placeholder: 'Nightly cleanup' })}
                ${selectHtml('job-preset', 'Schedule', [..._SCHEDULE_PRESETS, ['custom', 'Custom…']], known)}
                <div id="job-cron-row" class="${known === 'custom' ? '' : 'hidden'}">
                    ${fieldHtml('job-cron', 'Cron expression', { value: j?.schedule || '', placeholder: '30 2 * * mon-fri', attrs: 'mono', hint: 'minute hour day-of-month month day-of-week — e.g. <span class="mono">*/10 * * * *</span> every ten minutes, <span class="mono">0 9 * * mon-fri</span> weekdays at nine.' })}
                </div>
                <div>
                    <label class="block section-label mb-1.5" for="job-command">Command</label>
                    <textarea id="job-command" rows="3" spellcheck="false" class="inp textarea w-full px-3.5 py-2.5 text-sm mono" style="min-height:0" placeholder="python cleanup.py --older-than 30">${escapeHtml(j?.command || '')}</textarea>
                    <p class="text-xs mt-1" style="color:var(--text3)">Run through <span class="mono">sh -c</span> in the app's folder. For a Python app <span class="mono">python</span> is the app's virtualenv.</p>
                </div>
                ${fieldHtml('job-timeout', 'Time limit (seconds)', { type: 'number', value: String(j?.timeout || 600), attrs: 'min="5" max="86400"' })}
                <label class="flex items-center gap-2 text-sm cursor-pointer select-none" style="color:var(--text2)"><input type="checkbox" id="job-enabled" class="rounded" ${!j || j.enabled ? 'checked' : ''}> Enabled</label>
                <div class="flex gap-3 pt-1">
                    <button type="button" class="btn-ghost flex-1 py-2.5 text-sm font-medium rounded-xl" data-on-click="closeModal('job-modal')">Cancel</button>
                    <button type="button" id="job-save" class="btn-primary flex-1 py-2.5 text-sm rounded-xl">${j ? 'Save' : 'Add job'}</button>
                </div></div>`;
            $id('job-preset').addEventListener('change', () => $id('job-cron-row').classList.toggle('hidden', $id('job-preset').value !== 'custom'));
            $id('job-save').addEventListener('click', () => saveJob(id));
            openModal('job-modal');
        }

        async function saveJob(id) {
            const preset = $id('job-preset').value;
            const body = {
                name: _val('job-name'), schedule: preset === 'custom' ? _val('job-cron') : preset,
                command: $id('job-command').value.trim(), timeout: parseInt($id('job-timeout').value, 10) || 600, enabled: $id('job-enabled').checked,
            };
            const btn = $id('job-save'); btn.disabled = true;
            const r = id ? await apiJson('PUT', `/apps/${activeAppId}/jobs/${id}`, body) : await apiJson('POST', `/apps/${activeAppId}/jobs`, body);
            btn.disabled = false;
            if (!r.ok) { showToast(r.error, 'error'); return; }
            closeModal('job-modal');
            showToast(id ? 'Job saved' : 'Job added', 'success', 1500);
            loadJobs();
        }

        async function runJobNow(id) {
            const r = await apiJson('POST', `/apps/${activeAppId}/jobs/${id}/run`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Started', 'info', 1200);
            setTimeout(loadJobs, 400);
        }

        async function deleteJob(id) {
            const j = _jobs.find(x => x.id === id);
            const ok = await confirmDialog({ title: `Delete "${j?.name || 'job'}"?`, body: 'It stops running on schedule.', confirmLabel: 'Delete', danger: true });
            if (!ok) return;
            const r = await apiJson('DELETE', `/apps/${activeAppId}/jobs/${id}`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            loadJobs();
        }
