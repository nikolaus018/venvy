// System view, admin side: host usage history, the activity log, notification channels and alerts,
// the backup schedule with its off-site copy, and variables shared by every app.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        async function loadHostHistory() {
            const minutes = parseInt($id('sys-history-range').value, 10) || 60;
            const r = await apiJson('GET', `/system/metrics/history?minutes=${minutes}`);
            if (!r.ok) return;
            const pts = r.data.samples;
            $id('sys-hist-cpu').innerHTML = chartSvg(pts, 'cpu', { max: 100, color: 'var(--blue, #60a5fa)', fmt: v => `${v.toFixed(0)}%` });
            $id('sys-hist-mem').innerHTML = chartSvg(pts, 'mem', { max: 100, color: 'var(--green)', fmt: v => `${v.toFixed(0)}%` });
            $id('sys-hist-disk').innerHTML = chartSvg(pts, 'disk', { max: 100, color: 'var(--amber)', fmt: v => `${v.toFixed(0)}%` });
        }

        async function loadOpsSummaries() {
            const [n, s, a] = await Promise.all([apiJson('GET', '/system/notify'), apiJson('GET', '/system/backup/schedule'), apiJson('GET', '/system/alerts')]);
            if (n.ok && a.ok) {
                const ch = n.data.channels.filter(c => c.enabled).length, firing = a.data.filter(x => x.firing).length;
                $id('sys-notify-card-sub').textContent = `${ch} channel${ch === 1 ? '' : 's'} · ${a.data.length} alert${a.data.length === 1 ? '' : 's'}${firing ? ` · ${firing} firing` : ''}`;
            }
            if (s.ok) {
                const d = s.data;
                $id('sys-schedule-card-sub').textContent = d.schedule
                    ? `${d.schedule} · keep ${d.keep}${d.remote ? ' · off-site on' : ''}${d.last ? (d.last.ok ? ' · last ok' : ' · last FAILED') : ''}`
                    : `Off${d.remote ? ' · off-site copy set up' : ''}`;
            }
        }

        // ── Activity ─────────────────────────────────────────────────────────────
        let _auditOffset = 0;
        function openAuditModal() {
            const body = buildModal('audit-modal', 'Activity', { width: 'max-w-4xl' });
            body.innerHTML = `
                <div class="flex flex-wrap gap-2 mb-3">
                    <input id="aud-user" class="inp px-2.5 py-1.5 text-xs w-28" placeholder="User" autocomplete="off">
                    <input id="aud-action" class="inp px-2.5 py-1.5 text-xs w-36 mono" placeholder="action (app_, user_…)" autocomplete="off">
                    <input id="aud-q" class="inp px-2.5 py-1.5 text-xs flex-1 min-w-[8rem]" placeholder="Search the details" autocomplete="off">
                    <select id="aud-since" class="inp px-2 py-1.5 text-xs"><option value="0">Any time</option><option value="1">Last hour</option><option value="24">Last day</option><option value="168">Last week</option></select>
                    <button class="btn-secondary px-3 py-1.5 text-xs" data-on-click="loadAudit(true)">Filter</button>
                </div>
                <div class="rounded-xl overflow-hidden" style="border:1px solid var(--border)">
                    <table class="w-full text-left text-xs"><thead class="section-label" style="background:var(--surface2);border-bottom:1px solid var(--border)">
                        <tr><th class="px-3 py-2.5">When</th><th class="px-3 py-2.5">Who</th><th class="px-3 py-2.5">Action</th><th class="px-3 py-2.5 hidden md:table-cell">Detail</th></tr></thead>
                        <tbody id="aud-rows" class="divide-y" style="border-color:var(--border)"></tbody></table>
                </div>
                <div class="flex items-center justify-between mt-3"><span id="aud-count" class="text-xs" style="color:var(--text3)"></span>
                    <button id="aud-more" class="btn-ghost px-3 py-1.5 text-xs hidden" data-on-click="loadAudit(false)">Load more</button></div>`;
            ['aud-user', 'aud-action', 'aud-q'].forEach(id => $id(id).addEventListener('keydown', e => { if (e.key === 'Enter') loadAudit(true); }));
            openModal('audit-modal');
            loadAudit(true);
        }

        async function loadAudit(reset) {
            if (reset) { _auditOffset = 0; $id('aud-rows').innerHTML = ''; }
            const p = new URLSearchParams({ limit: 50, offset: _auditOffset });
            if (_val('aud-user')) p.set('user', _val('aud-user'));
            if (_val('aud-action')) p.set('action', _val('aud-action'));
            if (_val('aud-q')) p.set('q', _val('aud-q'));
            if ($id('aud-since').value !== '0') p.set('since_hours', $id('aud-since').value);
            const r = await apiJson('GET', `/audit?${p}`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            $id('aud-rows').insertAdjacentHTML('beforeend', r.data.entries.map(e => `
                <tr><td class="px-3 py-2 whitespace-nowrap" style="color:var(--text3)" title="${escapeHtml(e.ts)}">${escapeHtml(fmtWhen(e.ts))}</td>
                    <td class="px-3 py-2 whitespace-nowrap" style="color:var(--text)">${escapeHtml(e.user)}</td>
                    <td class="px-3 py-2 mono whitespace-nowrap" style="color:var(--text2)">${escapeHtml(e.action)}</td>
                    <td class="px-3 py-2 hidden md:table-cell break-words" style="color:var(--text3)">${escapeHtml(e.detail)}${e.ip ? ` <span class="mono" style="opacity:.7">[${escapeHtml(e.ip)}]</span>` : ''}</td></tr>`).join(''));
            _auditOffset += r.data.entries.length;
            $id('aud-count').textContent = r.data.total ? `${_auditOffset} of ${r.data.total}` : 'Nothing recorded for this filter.';
            $id('aud-more').classList.toggle('hidden', _auditOffset >= r.data.total);
        }

        // ── Notifications and alerts ─────────────────────────────────────────────
        const _NOTIFY_FIELDS = {
            webhook: [['url', 'URL', 'text', 'https://example.com/hooks/venvy'], ['secret', 'Signing secret (optional)', 'password', 'Signs each request: X-Venvy-Signature: sha256=…']],
            slack: [['url', 'Incoming webhook URL', 'password', 'https://hooks.slack.com/services/…']],
            discord: [['url', 'Webhook URL', 'password', 'https://discord.com/api/webhooks/…']],
            ntfy: [['url', 'Server (optional)', 'text', 'https://ntfy.sh'], ['topic', 'Topic', 'text', 'my-venvy-alerts'], ['token', 'Access token (optional)', 'password', '']],
            telegram: [['bot_token', 'Bot token', 'password', '123456:ABC…'], ['chat_id', 'Chat id', 'text', '']],
            email: [['host', 'SMTP host', 'text', 'smtp.example.com'], ['port', 'Port', 'number', '587'], ['security', 'Security', 'select', ''],
                    ['username', 'Username', 'text', ''], ['password', 'Password', 'password', ''], ['from', 'From', 'text', 'venvy@example.com'], ['to', 'To', 'text', 'you@example.com']],
        };
        let _notifyData = null, _alertsData = [];

        async function openNotifyModal() {
            const body = buildModal('notify-modal', 'Notifications & alerts', { width: 'max-w-3xl' });
            body.innerHTML = '<p class="text-xs py-6 text-center" style="color:var(--text3)">Loading…</p>';
            openModal('notify-modal');
            await refreshNotify();
        }

        async function refreshNotify() {
            const [n, a, apps] = await Promise.all([apiJson('GET', '/system/notify'), apiJson('GET', '/system/alerts'), Promise.resolve(_cachedApps)]);
            const body = $id('notify-modal-body');
            if (!n.ok || !a.ok) { body.innerHTML = `<p class="text-xs" style="color:var(--red)">${escapeHtml(n.error || a.error)}</p>`; return; }
            _notifyData = n.data; _alertsData = a.data;
            body.innerHTML = `
                <div class="flex items-center justify-between mb-2"><h3 class="text-sm font-semibold" style="color:var(--text)">Channels</h3>
                    <button class="btn-primary px-3 py-1.5 text-xs font-bold" data-on-click="openChannelModal()">Add channel</button></div>
                <p class="text-xs mb-3" style="color:var(--text3)">Where Venvy tells you about crashes, failed deploys and jobs, backups and resource alerts.</p>
                <div class="space-y-2 mb-6">${n.data.channels.length ? n.data.channels.map(c => `
                    <div class="card px-4 py-3 flex items-center gap-3" data-ch="${c.id}">
                        <div class="flex-1 min-w-0">
                            <p class="text-sm font-semibold truncate" style="color:var(--text)">${escapeHtml(c.name)} <span class="text-[10px] uppercase font-bold ml-1" style="color:var(--text3)">${escapeHtml(c.kind)}</span>${c.enabled ? '' : ' <span class="text-[10px] uppercase font-bold" style="color:var(--text3)">paused</span>'}</p>
                            <p class="text-xs mt-0.5 truncate" style="color:var(--text3)">${c.events === '*' ? 'All events' : escapeHtml(c.events.split(',').map(e => n.data.events[e] ? e.replace(/_/g, ' ') : e).join(', '))}${c.last_status ? ` · last: ${escapeHtml(c.last_status)}` : ''}</p>
                        </div>
                        <button class="ch-test btn-ghost px-2.5 py-1.5 text-xs">Test</button>
                        <button class="ch-edit btn-ghost px-2.5 py-1.5 text-xs">Edit</button>
                        <button class="ch-del btn-danger px-2.5 py-1.5 text-xs rounded-lg font-bold">Delete</button>
                    </div>`).join('') : '<p class="text-xs py-4 text-center" style="color:var(--text3)">No channels yet — nobody will be told.</p>'}</div>

                <div class="flex items-center justify-between mb-2"><h3 class="text-sm font-semibold" style="color:var(--text)">Resource alerts</h3>
                    <button class="btn-primary px-3 py-1.5 text-xs font-bold" data-on-click="openAlertModal()">Add alert</button></div>
                <p class="text-xs mb-3" style="color:var(--text3)">Fires when a value stays above its threshold for the time given, and again when it is back under.</p>
                <div class="space-y-2">${a.data.length ? a.data.map(r => `
                    <div class="card px-4 py-3 flex items-center gap-3" data-al="${r.id}">
                        <div class="flex-1 min-w-0"><p class="text-sm" style="color:var(--text)">${escapeHtml(r.description)} ${r.firing ? statusPill('failed').replace('failed', 'firing') : ''}${r.enabled ? '' : ' <span class="text-[10px] uppercase font-bold" style="color:var(--text3)">paused</span>'}</p></div>
                        <button class="al-toggle btn-ghost px-2.5 py-1.5 text-xs">${r.enabled ? 'Pause' : 'Resume'}</button>
                        <button class="al-del btn-danger px-2.5 py-1.5 text-xs rounded-lg font-bold">Delete</button>
                    </div>`).join('') : '<p class="text-xs py-4 text-center" style="color:var(--text3)">No alerts yet.</p>'}</div>`;
            body.querySelectorAll('[data-ch]').forEach(el => {
                const id = Number(el.dataset.ch);
                el.querySelector('.ch-test').addEventListener('click', () => testChannel(id));
                el.querySelector('.ch-edit').addEventListener('click', () => openChannelModal(id));
                el.querySelector('.ch-del').addEventListener('click', () => deleteChannel(id));
            });
            body.querySelectorAll('[data-al]').forEach(el => {
                const id = Number(el.dataset.al);
                el.querySelector('.al-toggle').addEventListener('click', () => toggleAlert(id));
                el.querySelector('.al-del').addEventListener('click', () => deleteAlert(id));
            });
            loadOpsSummaries();
        }

        async function testChannel(id) {
            const r = await apiJson('POST', `/system/notify/${id}/test`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast(r.data.ok ? 'Test notification sent' : `Failed: ${r.data.status}`, r.data.ok ? 'success' : 'error', r.data.ok ? 2500 : 6000);
            refreshNotify();
        }

        async function deleteChannel(id) {
            const c = _notifyData.channels.find(x => x.id === id);
            if (!await confirmDialog({ title: `Delete "${c?.name}"?`, body: 'It stops receiving notifications.', confirmLabel: 'Delete', danger: true })) return;
            const r = await apiJson('DELETE', `/system/notify/${id}`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            refreshNotify();
        }

        function openChannelModal(id = null) {
            const c = id ? _notifyData.channels.find(x => x.id === id) : null;
            const body = buildModal('channel-modal', c ? 'Edit channel' : 'Add channel', { width: 'max-w-lg', zIndex: 90 });
            const kinds = _notifyData.kinds;
            const events = _notifyData.events;
            const sel = c ? new Set(c.events === '*' ? [] : c.events.split(',')) : new Set();
            body.innerHTML = `<div class="space-y-4">
                ${fieldHtml('ch-name', 'Name', { value: c?.name || '', placeholder: 'Team chat' })}
                ${selectHtml('ch-kind', 'Type', kinds.map(k => [k, ({ webhook: 'Webhook (JSON)', slack: 'Slack', discord: 'Discord', ntfy: 'ntfy', telegram: 'Telegram', email: 'Email' })[k] || k]), c?.kind || 'webhook')}
                <div id="ch-fields" class="space-y-4"></div>
                <div><p class="section-label mb-1.5">Events</p>
                    <label class="flex items-center gap-2 text-sm mb-1.5 select-none cursor-pointer" style="color:var(--text2)"><input type="checkbox" id="ch-all" class="rounded" ${!c || c.events === '*' ? 'checked' : ''}> All events</label>
                    <div id="ch-events" class="grid grid-cols-1 gap-1 pl-1 ${!c || c.events === '*' ? 'opacity-40 pointer-events-none' : ''}">
                        ${Object.entries(events).map(([k, d]) => `<label class="flex items-start gap-2 text-xs select-none cursor-pointer" style="color:var(--text2)"><input type="checkbox" class="ch-ev rounded mt-0.5" value="${k}" ${sel.has(k) ? 'checked' : ''}> <span>${escapeHtml(d)}</span></label>`).join('')}
                    </div></div>
                <label class="flex items-center gap-2 text-sm cursor-pointer select-none" style="color:var(--text2)"><input type="checkbox" id="ch-enabled" class="rounded" ${!c || c.enabled ? 'checked' : ''}> Enabled</label>
                <div class="flex gap-3"><button type="button" class="btn-ghost flex-1 py-2.5 text-sm font-medium rounded-xl" data-on-click="closeModal('channel-modal')">Cancel</button>
                    <button type="button" id="ch-save" class="btn-primary flex-1 py-2.5 text-sm rounded-xl">${c ? 'Save' : 'Add channel'}</button></div></div>`;
            const renderFields = () => {
                const kind = $id('ch-kind').value;
                const cfg = (c && c.kind === kind) ? c.config : {};
                $id('ch-fields').innerHTML = _NOTIFY_FIELDS[kind].map(([key, label, type, ph]) => {
                    if (type === 'select') return selectHtml(`chf-${key}`, label, [['starttls', 'STARTTLS (587)'], ['ssl', 'SSL/TLS (465)'], ['none', 'None']], cfg[key] || 'starttls');
                    return fieldHtml(`chf-${key}`, label, { type, value: cfg[key] != null ? String(cfg[key]) : '', placeholder: ph || '', attrs: type === 'password' ? 'autocomplete="new-password"' : '' });
                }).join('');
            };
            $id('ch-kind').addEventListener('change', renderFields);
            renderFields();
            $id('ch-all').addEventListener('change', () => $id('ch-events').classList.toggle('opacity-40', $id('ch-all').checked) || $id('ch-events').classList.toggle('pointer-events-none', $id('ch-all').checked));
            $id('ch-save').addEventListener('click', () => saveChannel(id));
            openModal('channel-modal');
        }

        async function saveChannel(id) {
            const kind = $id('ch-kind').value;
            const config = {};
            for (const [key, , type] of _NOTIFY_FIELDS[kind]) {
                const el = $id(`chf-${key}`);
                if (!el) continue;
                const v = type === 'password' ? el.value : el.value.trim();
                if (v !== '') config[key] = type === 'number' ? Number(v) : v;
            }
            const evs = $id('ch-all').checked ? '*' : Array.from(document.querySelectorAll('.ch-ev:checked')).map(e => e.value).join(',');
            if (!evs) { showToast('Pick at least one event, or all of them.', 'warn'); return; }
            const body = { name: _val('ch-name'), kind, config, events: evs, enabled: $id('ch-enabled').checked };
            const btn = $id('ch-save'); btn.disabled = true;
            const r = id ? await apiJson('PUT', `/system/notify/${id}`, body) : await apiJson('POST', '/system/notify', body);
            btn.disabled = false;
            if (!r.ok) { showToast(r.error, 'error'); return; }
            closeModal('channel-modal');
            showToast('Saved — use Test to check it', 'success');
            refreshNotify();
        }

        function openAlertModal() {
            const body = buildModal('alert-modal', 'Add alert', { width: 'max-w-md', zIndex: 90 });
            body.innerHTML = `<div class="space-y-4">
                ${selectHtml('al-scope', 'Watch', [['0', 'The host'], ..._cachedApps.map(a => [String(a.id), a.name])], '0')}
                ${selectHtml('al-metric', 'Metric', [['cpu', 'CPU'], ['memory', 'Memory'], ['disk', 'Disk']], 'cpu')}
                ${fieldHtml('al-threshold', 'Above', { type: 'number', value: '90', attrs: 'min="1" step="any"', hint: '<span id="al-unit">percent</span>' })}
                ${fieldHtml('al-minutes', 'For (minutes)', { type: 'number', value: '5', attrs: 'min="1" max="1440"' })}
                <div class="flex gap-3"><button type="button" class="btn-ghost flex-1 py-2.5 text-sm font-medium rounded-xl" data-on-click="closeModal('alert-modal')">Cancel</button>
                    <button type="button" id="al-save" class="btn-primary flex-1 py-2.5 text-sm rounded-xl">Add alert</button></div></div>`;
            const unit = () => {
                const host = $id('al-scope').value === '0', m = $id('al-metric').value;
                $id('al-unit').textContent = host ? 'percent of the host' : (m === 'cpu' ? 'percent of one core' : 'megabytes');
            };
            $id('al-scope').addEventListener('change', unit); $id('al-metric').addEventListener('change', unit); unit();
            $id('al-save').addEventListener('click', async () => {
                const r = await apiJson('POST', '/system/alerts', { app_id: Number($id('al-scope').value), metric: $id('al-metric').value, threshold: Number($id('al-threshold').value), minutes: parseInt($id('al-minutes').value, 10) || 5 });
                if (!r.ok) { showToast(r.error, 'error'); return; }
                closeModal('alert-modal'); refreshNotify();
            });
            openModal('alert-modal');
        }

        async function toggleAlert(id) {
            const r0 = _alertsData.find(x => x.id === id);
            const r = await apiJson('PUT', `/system/alerts/${id}`, { app_id: r0.app_id, metric: r0.metric, threshold: r0.threshold, minutes: r0.minutes, enabled: !r0.enabled });
            if (!r.ok) { showToast(r.error, 'error'); return; }
            refreshNotify();
        }

        async function deleteAlert(id) {
            const r = await apiJson('DELETE', `/system/alerts/${id}`);
            if (!r.ok) { showToast(r.error, 'error'); return; }
            refreshNotify();
        }

        // ── Backup schedule and off-site copy ────────────────────────────────────
        const _BACKUP_PRESETS = [['', 'Off'], ['0 * * * *', 'Every hour'], ['0 3 * * *', 'Every day at 03:00'], ['0 3 * * 0', 'Every Sunday at 03:00'], ['custom', 'Custom…']];
        const _REMOTE_FIELDS = {
            s3: [['endpoint', 'Endpoint (blank for AWS)', 'text', 'https://s3.eu-central-003.backblazeb2.com'], ['region', 'Region', 'text', 'us-east-1'], ['bucket', 'Bucket', 'text', ''], ['prefix', 'Folder in the bucket', 'text', 'venvy/'], ['access_key', 'Access key', 'text', ''], ['secret_key', 'Secret key', 'password', '']],
            webdav: [['url', 'Folder URL', 'text', 'https://cloud.example.com/remote.php/dav/files/me/venvy'], ['username', 'Username', 'text', ''], ['password', 'Password', 'password', '']],
            scp: [['host', 'Host', 'text', 'backup.example.com'], ['port', 'Port', 'number', '22'], ['user', 'User', 'text', 'backup'], ['path', 'Folder on the server', 'text', '/srv/backups/venvy'], ['private_key', 'Private key', 'textarea', '-----BEGIN OPENSSH PRIVATE KEY-----']],
        };

        async function openScheduleModal() {
            const body = buildModal('schedule-modal', 'Backup schedule & off-site copy', { width: 'max-w-2xl' });
            body.innerHTML = '<p class="text-xs py-6 text-center" style="color:var(--text3)">Loading…</p>';
            openModal('schedule-modal');
            const r = await apiJson('GET', '/system/backup/schedule');
            if (!r.ok) { body.innerHTML = `<p class="text-xs" style="color:var(--red)">${escapeHtml(r.error)}</p>`; return; }
            const d = r.data;
            const preset = _BACKUP_PRESETS.some(p => p[0] === d.schedule) ? d.schedule : 'custom';
            const remote = d.remote || null;
            body.innerHTML = `
                <h3 class="text-sm font-semibold mb-1" style="color:var(--text)">Automatic backups</h3>
                <p class="text-xs mb-3" style="color:var(--text3)">The same full backup as the button in Backups: the database, the key and every app. Server time.</p>
                <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                    ${selectHtml('sch-preset', 'When', _BACKUP_PRESETS, preset)}
                    <div id="sch-cron-row" class="${preset === 'custom' ? '' : 'hidden'}">${fieldHtml('sch-cron', 'Cron expression', { value: d.schedule, placeholder: '30 2 * * *', attrs: 'mono' })}</div>
                    ${fieldHtml('sch-keep', 'Keep the last', { type: 'number', value: String(d.keep), attrs: 'min="1" max="365"', hint: 'automatic backups (older ones are deleted)' })}
                </div>
                <div class="flex items-center gap-2 mt-3 flex-wrap">
                    <button class="btn-primary px-4 py-2 text-xs font-bold" data-on-click="saveBackupSchedule()">Save schedule</button>
                    <button class="btn-ghost px-3 py-2 text-xs" data-on-click="runScheduledBackupNow()">Run it now</button>
                    <span class="text-xs" style="color:var(--text3)" id="sch-status">${scheduleStatusText(d)}</span>
                </div>

                <h3 class="text-sm font-semibold mt-7 mb-1" style="color:var(--text)">Off-site copy</h3>
                <p class="text-xs mb-3" style="color:var(--text3)">Every automatic backup is also sent there, so losing this server does not lose the backups. Backups hold your apps and the key that decrypts their variables — use storage you trust.</p>
                ${selectHtml('rem-kind', 'Send backups to', [['', 'Nowhere'], ['s3', 'S3-compatible storage (AWS, Backblaze, MinIO, Wasabi…)'], ['webdav', 'WebDAV (Nextcloud, …)'], ['scp', 'A server over SSH (scp)']], remote?.kind || '')}
                <div id="rem-fields" class="space-y-3 mt-3"></div>
                <div id="rem-buttons" class="flex gap-2 mt-4 ${remote || false ? '' : 'hidden'}">
                    <button class="btn-primary px-4 py-2 text-xs font-bold" data-on-click="saveBackupRemote()">Save</button>
                    <button class="btn-ghost px-3 py-2 text-xs" data-on-click="testBackupRemote()">Test</button>
                    <button class="btn-danger px-3 py-2 text-xs rounded-lg font-bold" data-on-click="removeBackupRemote()">Remove</button>
                    <span id="rem-status" class="text-xs self-center" style="color:var(--text3)"></span>
                </div>`;
            $id('sch-preset').addEventListener('change', () => $id('sch-cron-row').classList.toggle('hidden', $id('sch-preset').value !== 'custom'));
            const renderRemote = () => {
                const kind = $id('rem-kind').value;
                $id('rem-buttons').classList.toggle('hidden', !kind);
                const cfg = remote && remote.kind === kind ? remote : {};
                $id('rem-fields').innerHTML = kind ? _REMOTE_FIELDS[kind].map(([k, l, t, ph]) =>
                    t === 'textarea'
                        ? `<div><label class="block section-label mb-1.5" for="remf-${k}">${l}</label><textarea id="remf-${k}" rows="4" spellcheck="false" class="inp textarea w-full px-3.5 py-2.5 text-xs mono" style="min-height:0" placeholder="${escapeHtml(ph)}">${escapeHtml(cfg[k] || '')}</textarea></div>`
                        : fieldHtml(`remf-${k}`, l, { type: t, value: cfg[k] != null ? String(cfg[k]) : '', placeholder: ph, attrs: t === 'password' ? 'autocomplete="new-password"' : '' })).join('') : '';
            };
            $id('rem-kind').addEventListener('change', renderRemote);
            renderRemote();
        }

        function scheduleStatusText(d) {
            const bits = [];
            if (d.schedule && d.next_run) bits.push(`Next: ${fmtWhen(d.next_run)}`);
            if (d.last) bits.push(d.last.ok ? `Last: ${fmtAgo(d.last.at)}, ${d.last.file}${d.last.remote ? ' — off-site ' + d.last.remote.split(':')[0] : ''}` : `Last run failed: ${d.last.error || ''}`);
            return bits.join(' · ');
        }

        async function saveBackupSchedule() {
            const preset = $id('sch-preset').value;
            const r = await apiJson('PUT', '/system/backup/schedule', { schedule: preset === 'custom' ? _val('sch-cron') : preset, keep: parseInt($id('sch-keep').value, 10) || 7 });
            if (!r.ok) { showToast(r.error, 'error'); return; }
            $id('sch-status').textContent = scheduleStatusText(r.data);
            showToast('Schedule saved', 'success', 1500);
            loadOpsSummaries();
        }

        async function runScheduledBackupNow() {
            const r = await apiJson('POST', '/system/backup/schedule/run');
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Backup started — see Backups for progress', 'info');
        }

        function _remoteBody() {
            const kind = $id('rem-kind').value;
            const cfg = { kind };
            for (const [k, , t] of _REMOTE_FIELDS[kind]) {
                const el = $id(`remf-${k}`);
                const v = t === 'password' || t === 'textarea' ? el.value : el.value.trim();
                if (v !== '') cfg[k] = t === 'number' ? Number(v) : v;
            }
            return cfg;
        }

        async function saveBackupRemote() {
            const r = await apiJson('PUT', '/system/backup/remote', _remoteBody());
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Saved — use Test to check it', 'success');
            loadOpsSummaries();
            openScheduleModal();
        }

        async function testBackupRemote() {
            $id('rem-status').textContent = 'Testing…';
            const r = await apiJson('POST', '/system/backup/remote/test', _remoteBody());
            if (!r.ok) { $id('rem-status').textContent = ''; showToast(r.error, 'error'); return; }
            $id('rem-status').textContent = r.data.ok ? 'Works — a test file was uploaded.' : `Failed: ${r.data.error}`;
            $id('rem-status').style.color = r.data.ok ? 'var(--green)' : 'var(--red)';
        }

        async function removeBackupRemote() {
            if (!await confirmDialog({ title: 'Stop sending backups off-site?', body: 'Copies already there are not deleted.', confirmLabel: 'Remove', danger: true })) return;
            const r = await apiJson('DELETE', '/system/backup/remote');
            if (!r.ok) { showToast(r.error, 'error'); return; }
            loadOpsSummaries();
            openScheduleModal();
        }

        // ── Variables shared by every app ────────────────────────────────────────
        let _sharedEnv = null;
        async function openSharedEnvModal() {
            const body = buildModal('shared-env-modal', 'Shared variables', { width: 'max-w-2xl' });
            body.innerHTML = `<p class="text-xs mb-3" style="color:var(--text3)">Every app starts with these in its environment. An app's own variable of the same name wins. Applied the next time an app starts.</p>
                <div id="genv-mount"></div>
                <div class="flex justify-end mt-4"><button class="btn-primary px-5 py-2 text-sm font-bold" data-on-click="saveSharedEnv()" id="genv-save">Save</button></div>`;
            _sharedEnv = makeEnvEditor('genv', 'genv-mount', { onChange: () => { }, noHistory: true, noImport: false, title: 'Variables', emptyText: 'No shared variables.' });
            openModal('shared-env-modal');
            const r = await apiJson('GET', '/system/shared-env');
            if (!r.ok) { showToast(r.error, 'error'); return; }
            _sharedEnv.loadVars(r.data.vars, r.data.unreadable);
        }

        async function saveSharedEnv() {
            const st = _sharedEnv.state();
            if (st.error) { showToast(st.error, 'error'); _sharedEnv.focusProblem(); return; }
            if (st.untouched) { showToast('Nothing to save — these variables cannot be read here.', 'warn'); return; }
            const r = await apiJson('PUT', '/system/shared-env', { vars: st.vars });
            if (!r.ok) { showToast(r.error, 'error'); return; }
            showToast('Saved — running apps pick this up when they restart', 'success');
            closeModal('shared-env-modal');
        }
