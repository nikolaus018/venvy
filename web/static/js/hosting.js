// Hosting tab (how an app runs, builds, deploys, is reached and kept) and the
// Platform part of the System view (toolchains, services, volumes, monitors,
// the edge, log shipping, telemetry, replication, other servers, host jobs).
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them. Buttons call
// hostingAction('name', arg) / platformAction('name', arg): one allow-listed
// entry point each, dispatching only to the handlers defined here.

        let _hostingApp = null;
        const _H = (s) => escapeHtml(s == null ? '' : String(s));
        const _canOp = () => currentUser && currentUser.role !== 'viewer';
        const _isAdmin = () => currentUser && currentUser.role === 'admin';

        // ── small form kit ──────────────────────────────────────────────────
        // A field: [key, label, kind, extra]; kind: text | number | bool | select | textarea.
        function _field(prefix, [key, label, kind, extra = {}], value) {
            const id = `${prefix}-${key}`;
            const dis = _canOp() ? '' : 'disabled';
            const help = extra.help ? `<p class="text-[11px] mt-1" style="color:var(--text3)">${_H(extra.help)}</p>` : '';
            if (kind === 'bool') {
                return `<label class="flex items-start gap-2 text-xs py-1" style="color:var(--text2)">
                    <input type="checkbox" id="${id}" class="rounded mt-0.5" ${value ? 'checked' : ''} ${dis}>
                    <span><span class="font-semibold" style="color:var(--text)">${_H(label)}</span>${help}</span></label>`;
            }
            let input;
            if (kind === 'select') {
                input = `<select id="${id}" class="inp w-full px-3 py-2 text-xs" ${dis}>${extra.options.map(([v, t]) =>
                    `<option value="${_H(v)}" ${String(value ?? '') === String(v) ? 'selected' : ''}>${_H(t)}</option>`).join('')}</select>`;
            } else if (kind === 'textarea') {
                input = `<textarea id="${id}" rows="${extra.rows || 4}" class="inp w-full px-3 py-2 text-xs mono" placeholder="${_H(extra.ph || '')}" ${dis}>${_H(value ?? '')}</textarea>`;
            } else {
                input = `<input id="${id}" type="${kind === 'number' ? 'number' : 'text'}" class="inp w-full px-3 py-2 text-xs ${extra.mono ? 'mono' : ''}"
                    value="${_H(value ?? '')}" placeholder="${_H(extra.ph || '')}" ${dis}>`;
            }
            return `<div><label for="${id}" class="block text-[11px] font-bold uppercase tracking-wide mb-1" style="color:var(--text3)">${_H(label)}</label>${input}${help}</div>`;
        }

        function _read(prefix, fields) {
            const out = {};
            for (const [key, , kind] of fields) {
                const el = document.getElementById(`${prefix}-${key}`);
                if (!el) continue;
                if (kind === 'bool') out[key] = el.checked;
                else if (kind === 'number') out[key] = el.value === '' ? 0 : Number(el.value);
                else out[key] = el.value;
            }
            return out;
        }

        function _card(title, icon, body, sub = '') {
            return `<div class="card p-5 space-y-3"><div><h3 class="text-sm font-semibold flex items-center gap-2" style="color:var(--text)">
                <i data-feather="${icon}" class="w-4 h-4"></i> ${_H(title)}</h3>${sub ? `<p class="text-xs mt-0.5" style="color:var(--text3)">${sub}</p>` : ''}</div>${body}</div>`;
        }

        function _btn(label, call, kind = 'btn-ghost', needs = 'operator') {
            const attr = needs === 'admin' ? 'data-requires-admin' : (needs === 'operator' ? 'data-requires-operator' : '');
            return `<button class="${kind} px-3 py-1.5 text-xs font-semibold" ${attr} data-on-click="${_H(call)}">${_H(label)}</button>`;
        }

        // A string literal for a data-on-* handler: backslashes first, then quotes
        // (the attribute itself is HTML-escaped by _btn).
        const _jsq = (s) => String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        const _ha = (name, arg = '') => `hostingAction('${_jsq(name)}', '${_jsq(arg)}')`;
        const _pa = (name, arg = '') => `platformAction('${_jsq(name)}', '${_jsq(arg)}')`;

        async function _toastResult(r, ok = 'Saved') {
            if (r.ok) showToast(ok, 'success');
            else showToast(r.error || 'Failed', 'error');
            return r.ok;
        }

        // ── Hosting tab ─────────────────────────────────────────────────────
        const RUN_FIELDS = [
            ['start_command', 'Start command', 'text', { mono: true, ph: 'e.g. gunicorn app:app -b 0.0.0.0:$PORT  (blank = the runtime default)', help: 'Runs instead of the runtime default. $PORT, $APP_DATA_DIR and the app\'s variables are expanded; no shell — write sh -c "…" for one.' }],
            ['install_command', 'Install command', 'text', { mono: true, ph: 'blank = from the manifest (npm ci, uv sync, cargo fetch …)' }],
            ['build_command', 'Build command', 'text', { mono: true, ph: 'runs after the runtime\'s own build (cargo build --release, go build, npm run build …)' }],
            ['release_command', 'Release command', 'text', { mono: true, ph: 'e.g. python manage.py migrate — after the build, before the restart' }],
            ['root_dir', 'Root folder', 'text', { mono: true, ph: 'blank = the app folder; e.g. backend (monorepos)' }],
            ['runtime_version', 'Version', 'text', { ph: 'e.g. 20 (Node), 1.82 (Rust), 3.13 (Python) — blank = the host\'s', help: 'Installed on first use under System → Toolchains. .nvmrc, rust-toolchain.toml, .python-version … are read too.' }],
            ['container_port', 'Container port', 'number', { ph: 'docker apps: the port inside the container (8080)' }],
            ['stop_signal', 'Stop signal', 'select', { options: ['SIGTERM', 'SIGINT', 'SIGQUIT', 'SIGHUP', 'SIGUSR1', 'SIGUSR2', 'SIGWINCH'].map(x => [x, x]) }],
            ['stop_timeout', 'Stop timeout (s)', 'number', { ph: 'blank = 5', help: 'How long it gets to shut down before it is killed.' }],
        ];
        const PROC_FIELDS = [
            ['instances', 'Instances', 'number', { help: 'Copies of the app behind Venvy\'s proxy (each gets its own $PORT). Restarts go one at a time.' }],
            ['zero_downtime', 'Zero-downtime restarts', 'bool', { help: 'With one instance: start the new copy, wait for it, then restart the app.' }],
        ];
        const DEPLOY_FIELDS = [
            ['deploy_strategy', 'Deploy strategy', 'select', { options: [['in_place', 'In place'], ['atomic', 'Atomic (build next to it, then switch)']], help: 'Atomic: keep data in $APP_DATA_DIR, not in the app folder.' }],
            ['spec_from_repo', 'Use venvy.toml / Procfile from the repository', 'bool'],
            ['commit_status', 'Report deploys to GitHub / GitLab / Gitea (needs the git token)', 'bool'],
            ['previews_enabled', 'Preview app for every pushed branch', 'bool'],
            ['preview_domain', 'Preview domain', 'text', { ph: 'previews.example.com → <branch>.previews.example.com' }],
            ['environment', 'Environment label', 'text', { ph: 'production' }],
        ];
        const HEALTH_FIELDS = [
            ['health_type', 'Health check', 'select', { options: [['http', 'HTTP path (Settings)'], ['tcp', 'TCP port accepts'], ['command', 'Command exits 0'], ['none', 'None']] }],
            ['health_command', 'Health command', 'text', { mono: true, ph: 'e.g. pg_isready -h 127.0.0.1' }],
            ['health_start_period', 'Start period (s)', 'number', { help: 'Failures right after a start don\'t count (slow starters, migrations).' }],
        ];
        const NET_FIELDS = [
            ['bind_public', 'Reachable from the network on its port', 'bool', { help: 'Off: only through Venvy (proxy, tunnel, Caddy) — the firewall closes the port and HOST=127.0.0.1 is set.' }],
            ['egress_policy', 'Outgoing connections', 'select', { options: [['allow', 'Anywhere'], ['deny', 'Only this host'], ['allowlist', 'This host and the list below']] }],
            ['egress_allow', 'Allowed addresses', 'text', { mono: true, ph: '10.0.0.0/8, 1.1.1.1' }],
            ['sandbox', 'Filesystem sandbox (read-only system, private /tmp, nothing of other apps)', 'bool'],
            ['pids_limit', 'Max processes', 'number', { ph: 'blank = 4096' }],
            ['io_weight', 'I/O weight (1-10000)', 'number', { ph: 'blank = 100' }],
            ['nofile_limit', 'Max open files', 'number', {}],
            ['disk_limit_action', 'When over the disk limit', 'select', { options: [['alert', 'Alert'], ['stop', 'Stop the app']] }],
            ['access_log', 'Access log of proxied requests', 'bool'],
        ];
        const SECRET_FIELDS = [
            ['sops_file', 'SOPS file in the repository', 'text', { mono: true, ph: 'secrets.enc.yaml' }],
            ['vault_path', 'Vault path', 'text', { mono: true, ph: 'secret/myapp' }],
        ];

        async function loadHosting() {
            const appId = activeAppId;
            const root = $id('hosting-root');
            if (!appId || !root) return;
            const all = await apiJson('GET', '/apps');
            const app = all.ok ? all.data.find(a => a.id === appId) : null;
            if (!app) { root.innerHTML = `<p class="text-xs" style="color:var(--red)">${_H(all.error || 'App not found')}</p>`; return; }
            _hostingApp = app;
            const child = !!app.parent_app_id;
            const [procs, routes, svcs, vols, fwd, backups, traffic, previews, tmpl] = await Promise.all([
                apiJson('GET', `/apps/${child ? app.parent_app_id : appId}/processes`),
                child ? null : apiJson('GET', `/apps/${appId}/routes`),
                child ? null : apiJson('GET', `/apps/${appId}/services`),
                child ? null : apiJson('GET', '/volumes'),
                child ? null : apiJson('GET', `/apps/${appId}/forwards`),
                child || !_canOp() ? null : apiJson('GET', `/apps/${appId}/backups`),
                child ? null : apiJson('GET', `/apps/${appId}/traffic`),
                child ? null : apiJson('GET', `/apps/${appId}/previews`),
                child ? null : apiJson('GET', '/services/templates'),
            ]);
            if (appId !== activeAppId) return;
            if (child) {
                root.innerHTML = _card('Process of another app', 'git-branch',
                    `<p class="text-xs" style="color:var(--text2)">This ${_H(app.process_kind)} runs with the settings of its app; change them there.</p>
                     ${_btn('Open the app', `selectApp(${app.parent_app_id})`, 'btn-primary', '')}`);
                if (window.feather) feather.replace();
                return;
            }
            const sections = [];
            sections.push(_card('Run and build', 'play-circle',
                `<div class="grid grid-cols-1 gap-3">${RUN_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                 <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'run'), 'btn-primary')}</div>`,
                'What runs, how it is built, and how it stops.'));
            const plist = (procs && procs.ok ? procs.data : []);
            sections.push(_card('Processes', 'layers',
                `<div class="grid grid-cols-1 md:grid-cols-2 gap-3">${PROC_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                 <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'proc'), 'btn-primary')}</div>
                 <div class="space-y-2">${plist.length ? plist.map(p => `<div class="flex items-center gap-2 text-xs">
                    ${statusPill(p.status)} <span class="font-semibold" style="color:var(--text)">${_H(p.process_name)}</span>
                    <span style="color:var(--text3)">${_H(p.process_kind)}${p.port ? ' · port ' + p.port : ''}</span>
                    <span class="mono truncate flex-1" style="color:var(--text2)">${_H(p.start_command || '')}</span>
                    ${_btn('Logs', `selectApp(${p.id}, 'logs')`, 'btn-ghost', '')}
                    ${p.process_kind === 'worker' ? _btn('Remove', _ha('removeProcess', p.id)) : ''}</div>`).join('')
                    : '<p class="text-xs" style="color:var(--text3)">Only the app itself. Add a worker, or declare them in a Procfile / venvy.toml.</p>'}</div>
                 <div class="grid grid-cols-1 md:grid-cols-3 gap-2" data-requires-operator>
                    <input id="h-proc-name" class="inp px-3 py-2 text-xs" placeholder="worker">
                    <input id="h-proc-cmd" class="inp px-3 py-2 text-xs mono md:col-span-2" placeholder="celery -A app worker">
                 </div><div class="flex justify-end">${_btn('Add worker', _ha('addProcess'))}</div>`,
                'Workers and replicas share the app\'s folder, user, variables and limits; each is supervised on its own.'));
            sections.push(_card('Health', 'activity',
                `<div class="grid grid-cols-1 md:grid-cols-3 gap-3">${HEALTH_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                 <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'health'), 'btn-primary')}</div>`));
            sections.push(_card('Deploys', 'upload-cloud',
                `<div class="grid grid-cols-1 gap-2">${DEPLOY_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                 <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'deploy'), 'btn-primary')}</div>
                 <div class="flex items-center gap-2 flex-wrap" data-requires-operator><input type="file" id="h-artifact" accept=".tar.gz,.tgz" class="text-xs">
                 ${_btn('Deploy artifact', _ha('deployArtifact'))}</div>
                 <p class="text-[11px]" style="color:var(--text3)">An artifact (.tar.gz of the app's files, from CI or <span class="mono">ship.py deploy</span>) replaces the folder as a whole; venv, node_modules, target and .env / *.db carry over.</p>
                 ${previews && previews.ok && previews.data.length ? `<div class="space-y-1">${previews.data.map(p => `<div class="text-xs flex gap-2 items-center">
                    ${statusPill(p.last_deploy_status || p.status)} <span class="mono">${_H(p.git_branch)}</span>
                    <span style="color:var(--text3)">${_H(p.domain || '')}</span>${_btn('Open', `selectApp(${p.id})`, 'btn-ghost', '')}</div>`).join('')}</div>` : ''}`));
            const rl = routes && routes.ok ? routes.data : [];
            sections.push(_card('Routes', 'map',
                `<div class="space-y-1">${rl.length ? rl.map(r => `<div class="flex items-center gap-2 text-xs">
                    <span class="mono font-semibold" style="color:var(--text)">${_H(r.hostname)}${r.path_prefix !== '/' ? _H(r.path_prefix) : ''}</span>
                    <span style="color:var(--text3)">${r.kind === 'redirect' ? '→ ' + _H(r.redirect_to) + ' (' + r.redirect_code + ')' : 'proxy'}${r.strip_prefix ? ' · strip' : ''}${r.basic_auth.length ? ' · auth' : ''}${r.ip_allow ? ' · ip list' : ''}${r.rate_limit ? ' · ' + r.rate_limit + '/min' : ''}${r.gzip ? ' · gzip' : ''}${r.cache_seconds ? ' · cache ' + r.cache_seconds + 's' : ''}</span>
                    <span class="flex-1"></span>${_btn('Delete', _ha('deleteRoute', r.id))}</div>`).join('') : '<p class="text-xs" style="color:var(--text3)">The domain in Settings is the only hostname. Add aliases, wildcards (*.example.com), paths (/api → this app) or redirects.</p>'}</div>
                 <div class="grid grid-cols-2 md:grid-cols-4 gap-2" data-requires-operator>
                    <input id="h-r-host" class="inp px-3 py-2 text-xs mono col-span-2" placeholder="app.example.com or *.example.com">
                    <input id="h-r-path" class="inp px-3 py-2 text-xs mono" placeholder="/ (path)">
                    <select id="h-r-kind" class="inp px-3 py-2 text-xs"><option value="proxy">Proxy</option><option value="redirect">Redirect</option></select>
                    <input id="h-r-to" class="inp px-3 py-2 text-xs mono col-span-2" placeholder="redirect to (host or URL)">
                    <input id="h-r-ip" class="inp px-3 py-2 text-xs mono col-span-2" placeholder="allowed IPs (blank = all)">
                    <input id="h-r-user" class="inp px-3 py-2 text-xs" placeholder="basic auth user">
                    <input id="h-r-pass" type="password" class="inp px-3 py-2 text-xs" placeholder="password">
                    <input id="h-r-rate" type="number" class="inp px-3 py-2 text-xs" placeholder="requests/min">
                    <input id="h-r-body" type="number" class="inp px-3 py-2 text-xs" placeholder="max body MB">
                    <input id="h-r-cache" type="number" class="inp px-3 py-2 text-xs" placeholder="cache seconds">
                    <label class="text-xs flex items-center gap-1"><input type="checkbox" id="h-r-strip"> strip path</label>
                    <label class="text-xs flex items-center gap-1"><input type="checkbox" id="h-r-gzip"> gzip</label>
                    <textarea id="h-r-headers" rows="2" class="inp px-3 py-2 text-xs mono col-span-2 md:col-span-4" placeholder='extra response headers as JSON: {"X-Frame-Options": "DENY"}'></textarea>
                 </div><div class="flex justify-end">${_btn('Add route', _ha('addRoute'))}</div>
                 ${traffic && traffic.ok ? `<p class="text-xs" style="color:var(--text3)">Through the proxy: ${Object.entries(traffic.data.requests || {}).map(([k, v]) => `${_H(k)} ${v}`).join(' · ') || 'no requests yet'}${traffic.data.avg_ms != null ? ` · ${traffic.data.avg_ms.toFixed(1)} ms avg` : ''}. ${_btn('Access log', _ha('accessLog'), 'btn-ghost', '')}</p><pre id="h-access-log" class="text-[11px] mono hidden overflow-x-auto p-2 rounded" style="background:var(--surface)"></pre>` : ''}`,
                'Hostnames Venvy\'s proxy serves for this app, with per-route controls.'));
            sections.push(_card('Network and sandbox', 'shield',
                `<div class="grid grid-cols-1 md:grid-cols-2 gap-3">${NET_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                 <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'net'), 'btn-primary')}</div>
                 <p class="text-[11px]" style="color:var(--text3)">Limits, egress rules and the sandbox are enforced when apps run as systemd services (Venvy as root on systemd).</p>`));
            sections.push(_card('Secrets', 'lock',
                `<div class="grid grid-cols-1 md:grid-cols-2 gap-3">${SECRET_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                 <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'secret'), 'btn-primary')}</div>
                 <div data-requires-operator><label class="block text-[11px] font-bold uppercase tracking-wide mb-1" style="color:var(--text3)">Secret files ${app.has_secret_files ? '(set — saving replaces all)' : ''}</label>
                 <textarea id="h-secret-files" rows="4" class="inp w-full px-3 py-2 text-xs mono" placeholder='{"credentials.json": "{...}", "tls.key": "-----BEGIN …"}'></textarea>
                 <p class="text-[11px] mt-1" style="color:var(--text3)">Written to $SECRETS_DIR at every start, readable by the app only; never shown again.</p></div>
                 <div class="flex justify-end">${_btn('Replace secret files', _ha('saveSecretFiles'))}</div>`));
            const svcList = svcs && svcs.ok ? svcs.data : [];
            const allApps = all.data.filter(a => a.service_template && a.id !== appId);
            sections.push(_card('Services and volumes', 'database',
                `<div class="space-y-1">${svcList.map(b => `<div class="text-xs flex gap-2 items-center"><span class="font-semibold">${_H(b.service)}</span>
                    <span style="color:var(--text3)">${_H(b.template)} → ${b.variables.map(_H).join(', ')}</span><span class="flex-1"></span>${_btn('Detach', _ha('unbind', b.id))}</div>`).join('')
                    || '<p class="text-xs" style="color:var(--text3)">Uses no managed service. Make one under System → Platform → Services.</p>'}</div>
                 ${allApps.length ? `<div class="flex gap-2" data-requires-operator><select id="h-bind" class="inp px-3 py-2 text-xs flex-1">${allApps.map(a => `<option value="${a.id}">${_H(a.name)} (${_H(a.service_template)})</option>`).join('')}</select>
                 <input id="h-bind-prefix" class="inp px-3 py-2 text-xs w-28" placeholder="PREFIX">${_btn('Use', _ha('bind'))}</div>` : ''}
                 <div class="space-y-1">${(vols && vols.ok ? vols.data : []).map(v => {
                    const at = v.apps.find(x => x.app_id === appId);
                    return `<div class="text-xs flex gap-2 items-center"><span class="mono">${_H(v.name)}</span><span style="color:var(--text3)">$${_H(v.env)}</span>
                    <span class="flex-1"></span>${at ? _btn('Detach', _ha('detachVolume', v.id), 'btn-ghost', 'admin') : _btn('Attach', _ha('attachVolume', v.id), 'btn-ghost', 'admin')}</div>`;
                 }).join('')}</div>`));
            const fl = fwd && fwd.ok ? fwd.data : [];
            sections.push(_card('Publish', 'globe',
                `<div class="space-y-1">${fl.map(f => `<div class="text-xs flex gap-2"><span class="mono">${_H(f.protocol)} ${_H(f.listen_host)}:${f.listen_port} → ${f.target_port}</span>
                    <span style="color:${f.listening ? 'var(--green)' : 'var(--red)'}">${f.listening ? 'listening' : _H(f.error || 'not listening')}</span><span class="flex-1"></span>${_btn('Remove', _ha('deleteForward', f.id), 'btn-ghost', 'admin')}</div>`).join('')}</div>
                 <div class="flex gap-2 flex-wrap" data-requires-admin><select id="h-f-proto" class="inp px-3 py-2 text-xs"><option>tcp</option><option>udp</option></select>
                 <input id="h-f-port" type="number" class="inp px-3 py-2 text-xs w-28" placeholder="host port">
                 <input id="h-f-target" type="number" class="inp px-3 py-2 text-xs w-28" placeholder="app port">${_btn('Forward', _ha('addForward'), 'btn-ghost', 'admin')}</div>
                 <div class="flex gap-2 flex-wrap" data-requires-admin><select id="h-p-mode" class="inp px-3 py-2 text-xs">
                 <option value="serve">Tailscale (tailnet)</option><option value="funnel">Tailscale Funnel (internet)</option><option value="ngrok">ngrok</option></select>
                 <input id="h-p-arg" class="inp px-3 py-2 text-xs flex-1" placeholder="https port (443/8443/10000) or ngrok domain">
                 ${_btn('Publish', _ha('publish'), 'btn-ghost', 'admin')}${_btn('Unpublish', _ha('unpublish'), 'btn-ghost', 'admin')}</div>
                 ${app.external_url ? `<p class="text-xs mono">${_H(app.external_url)}</p>` : ''}`,
                'Raw TCP/UDP ports, Tailscale and ngrok. (Own domains with HTTPS: System → Platform → Edge; Cloudflare: Settings.)'));
            if (backups && backups.ok) {
                sections.push(_card('App backups', 'archive',
                    `<div class="space-y-1">${backups.data.map(b => `<div class="text-xs flex gap-2 items-center"><span class="mono">${_H(b.name)}</span>
                        <span style="color:var(--text3)">${fmtBytes(b.size)} · ${_H(fmtWhen(b.created_at))}</span><span class="flex-1"></span>
                        ${_btn('Restore', _ha('restoreBackup', b.name))}${_btn('Delete', _ha('deleteBackup', b.name))}</div>`).join('') || '<p class="text-xs" style="color:var(--text3)">None yet.</p>'}</div>
                     <div class="flex justify-end">${_btn('Back up now', _ha('backup'), 'btn-primary')}</div>`,
                    'The app\'s folder and $APP_DATA_DIR, with consistent copies of SQLite databases.'));
            }
            sections.push(_card('Run a command', 'terminal',
                `<div class="flex gap-2" data-requires-operator><input id="h-run" class="inp px-3 py-2 text-xs mono flex-1" placeholder="python manage.py createsuperuser --noinput" data-on-enter="${_ha('run')}">
                 ${_btn('Run', _ha('run'), 'btn-primary')}</div><pre id="h-run-out" class="text-[11px] mono hidden overflow-x-auto p-2 rounded max-h-80" style="background:var(--surface)"></pre>`,
                'Once, as the app, in its folder, with its variables.'));
            root.innerHTML = sections.join('');
            if (window.feather) feather.replace();
        }

        const _GROUPS = { run: RUN_FIELDS, proc: PROC_FIELDS, deploy: DEPLOY_FIELDS, health: HEALTH_FIELDS, net: NET_FIELDS, secret: SECRET_FIELDS };

        const _hostingHandlers = {
            async saveFields(group) {
                const body = _read('h', _GROUPS[group]);
                if (group === 'run' && body.stop_timeout === 0) body.stop_timeout = 0;
                const r = await apiJson('PUT', `/apps/${activeAppId}`, body);
                if (await _toastResult(r)) loadHosting();
            },
            async addProcess() {
                const r = await apiJson('POST', `/apps/${activeAppId}/processes`, { name: $id('h-proc-name').value, command: $id('h-proc-cmd').value });
                if (await _toastResult(r, 'Worker added')) loadHosting();
            },
            async removeProcess(id) {
                if (!await confirmDialog({ title: 'Remove this worker?', body: 'It is stopped and removed.', danger: true, confirmLabel: 'Remove' })) return;
                if (await _toastResult(await apiJson('DELETE', `/apps/${id}`), 'Removed')) loadHosting();
            },
            async addRoute() {
                let headers = {};
                const raw = $id('h-r-headers').value.trim();
                if (raw) { try { headers = JSON.parse(raw); } catch { showToast('Headers must be JSON', 'error'); return; } }
                const user = $id('h-r-user').value.trim();
                const body = {
                    hostname: $id('h-r-host').value, path_prefix: $id('h-r-path').value || '/', kind: $id('h-r-kind').value,
                    redirect_to: $id('h-r-to').value || null, ip_allow: $id('h-r-ip').value, rate_limit: Number($id('h-r-rate').value || 0),
                    max_body_mb: Number($id('h-r-body').value || 0), cache_seconds: Number($id('h-r-cache').value || 0),
                    strip_prefix: $id('h-r-strip').checked, gzip: $id('h-r-gzip').checked, headers,
                    basic_auth: user ? { [user]: $id('h-r-pass').value } : null,
                };
                if (await _toastResult(await apiJson('POST', `/apps/${activeAppId}/routes`, body), 'Route added')) loadHosting();
            },
            async deleteRoute(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/routes/${id}`), 'Route deleted')) loadHosting();
            },
            async accessLog() {
                const r = await apiJson('GET', `/apps/${activeAppId}/access-log?lines=200`);
                const el = $id('h-access-log');
                el.classList.remove('hidden');
                el.textContent = r.ok ? (r.data.lines.join('\n') || '(empty)') : r.error;
            },
            async saveSecretFiles() {
                let files;
                try { files = JSON.parse($id('h-secret-files').value || '{}'); } catch { showToast('Write the files as JSON {name: content}', 'error'); return; }
                if (await _toastResult(await apiJson('PUT', `/apps/${activeAppId}`, { secret_files: files }), 'Secret files replaced')) loadHosting();
            },
            async bind() {
                const r = await apiJson('POST', `/apps/${activeAppId}/services`, { service_app_id: Number($id('h-bind').value), prefix: $id('h-bind-prefix').value });
                if (await _toastResult(r, 'Service attached — restart the app to use it')) loadHosting();
            },
            async unbind(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/services/${id}`), 'Detached')) loadHosting();
            },
            async attachVolume(id) {
                if (await _toastResult(await apiJson('POST', `/apps/${activeAppId}/volumes/${id}`, { read_only: false }), 'Attached — restart the app')) loadHosting();
            },
            async detachVolume(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/volumes/${id}`), 'Detached')) loadHosting();
            },
            async addForward() {
                const body = { protocol: $id('h-f-proto').value, listen_port: Number($id('h-f-port').value), target_port: Number($id('h-f-target').value) || null };
                if (await _toastResult(await apiJson('POST', `/apps/${activeAppId}/forwards`, body), 'Forwarding')) loadHosting();
            },
            async deleteForward(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/forwards/${id}`), 'Removed')) loadHosting();
            },
            async publish() {
                const mode = $id('h-p-mode').value, arg = $id('h-p-arg').value.trim();
                const body = mode === 'ngrok' ? { provider: 'ngrok', domain: arg } : { provider: 'tailscale', mode, https_port: Number(arg || 443) };
                const r = await apiJson('POST', `/apps/${activeAppId}/publish`, body);
                if (await _toastResult(r, r.data?.url ? `Published at ${r.data.url}` : 'Published')) loadHosting();
            },
            async unpublish() {
                const mode = $id('h-p-mode').value;
                const r = await apiJson('DELETE', `/apps/${activeAppId}/publish/${mode === 'ngrok' ? 'ngrok' : 'tailscale'}`);
                if (await _toastResult(r, 'Unpublished')) loadHosting();
            },
            async deployArtifact() {
                const f = $id('h-artifact').files[0];
                if (!f) { showToast('Choose a .tar.gz first', 'error'); return; }
                const fd = new FormData();
                fd.append('file', f);
                const res = await fetch(`${API}/apps/${activeAppId}/deploy/artifact`, { method: 'POST', body: fd });
                const body = await res.json().catch(() => ({}));
                if (!res.ok) { showToast(body.detail || 'Upload failed', 'error'); return; }
                showToast(body.queued ? 'Queued behind the running deploy' : 'Deploy started — see the Deploy tab', 'success');
                switchTab('deploy');
            },
            async backup() {
                showToast('Backing up…', 'info');
                try { await waitForJob(await fetch(`${API}/apps/${activeAppId}/backups`, { method: 'POST' })); showToast('Backup made', 'success'); } catch (e) { showToast(e.message, 'error'); }
                loadHosting();
            },
            async restoreBackup(name) {
                if (!await confirmDialog({ title: 'Restore this backup?', body: 'The app is stopped and its folder and data are replaced by the backup.', danger: true, confirmLabel: 'Restore' })) return;
                try {
                    await waitForJob(await fetch(`${API}/apps/${activeAppId}/backups/${encodeURIComponent(name)}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }));
                    showToast('Restored', 'success');
                } catch (e) { showToast(e.message, 'error'); }
            },
            async deleteBackup(name) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/backups/${encodeURIComponent(name)}`), 'Deleted')) loadHosting();
            },
            async run() {
                const cmd = $id('h-run').value.trim();
                if (!cmd) return;
                const out = $id('h-run-out');
                out.classList.remove('hidden');
                out.textContent = 'Running…';
                try {
                    const res = await waitForJob(await fetch(`${API}/apps/${activeAppId}/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: cmd }) }));
                    out.textContent = `[${res.status}${res.exit_code != null ? ', exit ' + res.exit_code : ''}, ${res.duration}s]\n${res.output}`;
                } catch (e) { out.textContent = e.message; }
            },
        };

        function hostingAction(name, arg) {
            const fn = Object.prototype.hasOwnProperty.call(_hostingHandlers, name) ? _hostingHandlers[name] : null;
            if (fn) return fn(arg);
        }

        // ── Platform (System view) ──────────────────────────────────────────
        async function loadPlatformView() {
            const root = $id('sys-platform');
            if (!root) return;
            if (!_canOp()) { root.classList.add('hidden'); return; }
            root.classList.remove('hidden');
            const admin = _isAdmin();
            const reqs = [apiJson('GET', '/toolchains'), apiJson('GET', '/services/templates'), apiJson('GET', '/volumes'), apiJson('GET', '/monitors')];
            if (admin) reqs.push(apiJson('GET', '/system/edge'), apiJson('GET', '/system/log-targets'), apiJson('GET', '/system/telemetry'),
                apiJson('GET', '/system/replication'), apiJson('GET', '/servers'), apiJson('GET', '/system/jobs'), apiJson('GET', '/system/secrets'),
                apiJson('GET', '/system/status-page'));
            const [tc, st, vols, mons, edge, logs, tele, repl, servers, hjobs, secr, sp] = await Promise.all(reqs);
            const out = [];
            out.push(_card('Toolchains', 'tool', (tc.ok ? tc.data.kinds : []).map(k => `<div class="text-xs flex gap-2 items-center">
                <span class="font-semibold w-16">${_H(k.kind)}</span><span class="mono flex-1" style="color:var(--text2)">${k.installed.map(_H).join(', ') || '—'}</span>
                ${k.installed.map(v => _btn('×' + v, _pa('removeToolchain', k.kind + '/' + v), 'btn-ghost', 'admin')).join('')}</div>`).join('')
                + `<div class="flex gap-2" data-requires-admin><select id="p-tc-kind" class="inp px-3 py-2 text-xs">${(tc.ok ? tc.data.kinds : []).map(k => `<option ${k.can_install ? '' : 'disabled'}>${_H(k.kind)}</option>`).join('')}</select>
                  <input id="p-tc-ver" class="inp px-3 py-2 text-xs flex-1" placeholder="version: 20, 1.82, 3.13, latest">${_btn('Install', _pa('installToolchain'), 'btn-primary', 'admin')}</div>`,
                `Language versions apps can choose (Settings or Hosting → Version), checksum-verified. In ${_H(tc.ok ? tc.data.dir : '')}.`));
            out.push(_card('Services', 'database', `${st.ok && !st.data.docker ? '<p class="text-xs" style="color:var(--yellow)">Docker is not installed (installer: --with-docker).</p>' : ''}
                <div class="flex gap-2"><select id="p-svc" class="inp px-3 py-2 text-xs">${(st.ok ? st.data.templates : []).map(t => `<option value="${_H(t.key)}">${_H(t.label)}</option>`).join('')}</select>
                <input id="p-svc-name" class="inp px-3 py-2 text-xs flex-1" placeholder="name, e.g. main-db">${_btn('Create', _pa('createService'), 'btn-primary')}</div>`,
                'Databases, caches and queues as containers with generated credentials; attach them to apps in Hosting → Services.'));
            out.push(_card('Volumes', 'folder', (vols.ok ? vols.data : []).map(v => `<div class="text-xs flex gap-2"><span class="mono">${_H(v.name)}</span>
                <span style="color:var(--text3)">${v.apps.length} app(s) · $${_H(v.env)}</span><span class="flex-1"></span>${_btn('Delete', _pa('deleteVolume', v.id), 'btn-ghost', 'admin')}</div>`).join('')
                + `<div class="flex gap-2" data-requires-admin><input id="p-vol" class="inp px-3 py-2 text-xs flex-1" placeholder="shared-uploads">${_btn('Create', _pa('createVolume'), 'btn-primary', 'admin')}</div>`,
                'Folders apps can share (attach in Hosting).'));
            out.push(_card('Monitors', 'radio', (mons.ok ? mons.data : []).map(m => `<div class="text-xs flex gap-2 items-center">
                ${statusPill(m.last_ok == null ? 'unknown' : m.last_ok ? 'healthy' : 'unhealthy')}<span class="font-semibold">${_H(m.name)}</span>
                <span class="mono truncate flex-1" style="color:var(--text3)">${_H(m.target)}</span><span>${m.uptime_24h != null ? m.uptime_24h.toFixed(2) + '%' : ''}</span>
                ${_btn('Delete', _pa('deleteMonitor', m.id))}</div>`).join('')
                + `<div class="flex gap-2 flex-wrap"><input id="p-mon-name" class="inp px-3 py-2 text-xs" placeholder="name">
                  <input id="p-mon-target" class="inp px-3 py-2 text-xs mono flex-1" placeholder="https://example.com/health or tcp://db:5432">
                  <label class="text-xs flex items-center gap-1"><input type="checkbox" id="p-mon-public"> on status page</label>${_btn('Add', _pa('addMonitor'), 'btn-primary')}</div>`,
                'Anything reachable from here, checked on a schedule, with notifications.'));
            if (admin) {
                const e = edge.ok ? edge.data : {};
                out.push(_card('Edge', 'globe', `
                    <p class="text-xs">Caddy: ${e.caddy?.installed ? (e.caddy.running ? 'running' : 'installed, not running') : 'not installed (--with-caddy)'}</p>
                    <label class="text-xs flex items-center gap-2"><input type="checkbox" id="p-caddy-on" ${e.caddy?.enabled ? 'checked' : ''}> HTTPS for every routed hostname (Let's Encrypt, on demand)</label>
                    <div class="flex gap-2"><input id="p-caddy-email" class="inp px-3 py-2 text-xs flex-1" placeholder="ACME email (optional)" value="${_H(e.caddy?.email || '')}">
                    <input id="p-caddy-hosts" class="inp px-3 py-2 text-xs flex-1" placeholder="Venvy's own hostnames, comma separated" value="${_H((e.caddy?.venvy_hosts || []).join(', '))}">
                    ${_btn('Apply', _pa('saveCaddy'), 'btn-primary', 'admin')}</div>
                    <p class="text-xs">Tailscale: ${e.tailscale?.installed ? _H(e.tailscale.backend || '') + ' ' + _H(e.tailscale.dns_name || '') : 'not installed (--with-tailscale)'}</p>
                    <p class="text-xs">ngrok: ${e.ngrok?.installed ? 'installed' : 'not installed (--with-ngrok)'}${e.ngrok?.has_token ? ' · token set' : ''}</p>
                    <div class="flex gap-2"><input id="p-ngrok" type="password" class="inp px-3 py-2 text-xs flex-1" placeholder="ngrok authtoken">${_btn('Save token', _pa('saveNgrok'), 'btn-ghost', 'admin')}</div>`,
                    'Ways in from the internet besides the Cloudflare tunnel.'));
                out.push(_card('Log shipping', 'send', (logs.ok ? logs.data : []).map(t => `<div class="text-xs flex gap-2"><span class="font-semibold">${_H(t.kind)}</span>
                    <span class="mono flex-1" style="color:var(--text3)">${_H(t.url || (t.host ? t.host + ':' + t.port + '/' + t.protocol : '/dev/log'))}</span>
                    <span style="color:var(--text3)">${t.stats?.sent || 0} sent${t.stats?.last_error ? ' · ' + _H(t.stats.last_error) : ''}</span>${_btn('Remove', _pa('removeLogTarget', t.id), 'btn-ghost', 'admin')}</div>`).join('')
                    + `<div class="flex gap-2 flex-wrap"><select id="p-lt-kind" class="inp px-3 py-2 text-xs"><option>loki</option><option>http</option><option>syslog</option><option>journald</option></select>
                      <input id="p-lt-url" class="inp px-3 py-2 text-xs mono flex-1" placeholder="https://loki:3100  ·  or host:port for syslog">
                      <input id="p-lt-token" type="password" class="inp px-3 py-2 text-xs" placeholder="token (optional)">${_btn('Add', _pa('addLogTarget'), 'btn-primary', 'admin')}</div>`));
                const t = tele.ok ? tele.data : {};
                out.push(_card('Metrics and heartbeat', 'bar-chart-2', `
                    <p class="text-xs" style="color:var(--text3)">Prometheus: <span class="mono">GET /metrics</span> with an API token (scope read). Firewall: ${t.firewall?.active ? 'active' : _H(t.firewall?.reason || 'off')}.</p>
                    <div class="flex gap-2"><input id="p-otlp" class="inp px-3 py-2 text-xs mono flex-1" placeholder="OTLP collector, e.g. http://otel:4318" value="${_H(t.otlp_url || '')}">
                    <input id="p-hb" class="inp px-3 py-2 text-xs mono flex-1" placeholder="${t.heartbeat_url ? 'heartbeat URL set — enter to replace' : 'heartbeat URL (healthchecks.io …)'}">
                    ${_btn('Save', _pa('saveTelemetry'), 'btn-primary', 'admin')}</div>
                    <p class="text-xs" style="color:var(--text3)">Heartbeat: ${_H(t.state?.heartbeat?.last_ok || 'never')}${t.state?.heartbeat?.last_error ? ' · ' + _H(t.state.heartbeat.last_error) : ''} · OTLP: ${_H(t.state?.otlp?.last_ok || 'never')}</p>`));
                const s = sp.ok ? sp.data : {};
                out.push(_card('Status page', 'check-circle', `<label class="text-xs flex items-center gap-2"><input type="checkbox" id="p-sp-on" ${s.enabled ? 'checked' : ''}> Public status page at <span class="mono">/status</span></label>
                    <input id="p-sp-title" class="inp px-3 py-2 text-xs w-full" value="${_H(s.title || 'Status')}">
                    <div class="flex flex-wrap gap-2">${(_cachedApps || []).filter(a => !a.parent_app_id).map(a => `<label class="text-xs flex items-center gap-1"><input type="checkbox" class="p-sp-app" value="${a.id}" ${(s.apps || []).includes(a.id) ? 'checked' : ''}> ${_H(a.name)}</label>`).join('')}</div>
                    <div class="flex justify-end">${_btn('Save', _pa('saveStatusPage'), 'btn-primary', 'admin')}</div>`));
                const rp = repl.ok ? repl.data : {};
                out.push(_card('Database replication', 'copy', `<p class="text-xs" style="color:var(--text3)">${rp.remote ? 'To the off-site backup destination.' : 'Set an off-site destination under Backups first.'} Last: ${_H(rp.last_ok || 'never')}${rp.last_error ? ' · ' + _H(rp.last_error) : ''}</p>
                    <div class="flex gap-2"><input id="p-repl" type="number" class="inp px-3 py-2 text-xs w-32" value="${rp.minutes || 0}" placeholder="minutes">${_btn('Save', _pa('saveReplication'), 'btn-primary', 'admin')}${_btn('Run now', _pa('runReplication'), 'btn-ghost', 'admin')}</div>`,
                    'A standby copy of settings, users, routes and variables every few minutes (0 = off).'));
                out.push(_card('Other servers', 'server', (servers.ok ? servers.data : []).map(sv => `<div class="text-xs flex gap-2"><span class="font-semibold">${_H(sv.name)}</span>
                    <span class="mono flex-1" style="color:var(--text3)">${_H(sv.url)}</span><span>${_H(sv.last_status)}</span>${_btn('Remove', _pa('removeServer', sv.id), 'btn-ghost', 'admin')}</div>`).join('')
                    + `<div id="p-remote-apps" class="space-y-1"></div><div class="flex gap-2 flex-wrap"><input id="p-srv-name" class="inp px-3 py-2 text-xs" placeholder="name">
                      <input id="p-srv-url" class="inp px-3 py-2 text-xs mono flex-1" placeholder="https://venvy2.example.com">
                      <input id="p-srv-token" type="password" class="inp px-3 py-2 text-xs" placeholder="API token from that server">${_btn('Add', _pa('addServer'), 'btn-primary', 'admin')}${_btn('Show apps', _pa('remoteApps'), 'btn-ghost', 'admin')}</div>`,
                    'Watch and control the apps of other Venvy servers from here.'));
                out.push(_card('Host jobs', 'clock', (hjobs.ok ? hjobs.data : []).map(j => `<div class="text-xs flex gap-2 items-center">${j.last_status ? statusPill(j.last_status) : ''}<span class="font-semibold">${_H(j.name)}</span>
                    <span class="mono" style="color:var(--text3)">${_H(j.schedule)}</span><span class="mono truncate flex-1">${_H(j.command)}</span>${_btn('Run', _pa('runHostJob', j.id), 'btn-ghost', 'admin')}${_btn('Delete', _pa('deleteHostJob', j.id), 'btn-ghost', 'admin')}</div>`).join('')
                    + `<div class="flex gap-2 flex-wrap"><input id="p-hj-name" class="inp px-3 py-2 text-xs" placeholder="name"><input id="p-hj-sched" class="inp px-3 py-2 text-xs mono w-32" placeholder="0 4 * * *">
                      <input id="p-hj-cmd" class="inp px-3 py-2 text-xs mono flex-1" placeholder="curl -fsS https://example.com/cron">${_btn('Add', _pa('addHostJob'), 'btn-primary', 'admin')}</div>`,
                    'Scheduled commands that belong to no app (run as the unprivileged user venvy-app-0).'));
                const se = secr.ok ? secr.data : {};
                out.push(_card('Secrets back-ends', 'key', `<div class="flex gap-2 flex-wrap"><input id="p-vault-addr" class="inp px-3 py-2 text-xs mono flex-1" placeholder="Vault address" value="${_H(se.vault_addr || '')}">
                    <input id="p-vault-token" type="password" class="inp px-3 py-2 text-xs" placeholder="${se.vault_token_set ? 'token set — enter to replace' : 'Vault token'}">
                    <input id="p-vault-ns" class="inp px-3 py-2 text-xs w-32" placeholder="namespace" value="${_H(se.vault_namespace || '')}"></div>
                    <input id="p-age" type="password" class="inp px-3 py-2 text-xs mono w-full" placeholder="${se.sops_age_key_set ? 'age key set — enter to replace' : 'AGE-SECRET-KEY-… for SOPS files'}">
                    <p class="text-xs" style="color:var(--text3)">sops ${se.sops_installed ? 'installed' : 'not installed (--with-sops)'}.</p>
                    <div class="flex justify-end">${_btn('Save', _pa('saveSecrets'), 'btn-primary', 'admin')}</div>`));
            }
            root.innerHTML = `<h2 class="text-xs font-bold uppercase tracking-wide pt-2" style="color:var(--text3)">Platform</h2>` + out.join('');
            if (window.feather) feather.replace();
        }

        const _platformHandlers = {
            async installToolchain() {
                showToast('Installing… (this can take a few minutes)', 'info');
                try {
                    const res = await waitForJob(await fetch(`${API}/toolchains`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: $id('p-tc-kind').value, version: $id('p-tc-ver').value || 'latest' }) }));
                    showToast(`${res.kind} ${res.version} installed`, 'success');
                } catch (e) { showToast(e.message, 'error'); }
                loadPlatformView();
            },
            async removeToolchain(kv) {
                const [k, v] = kv.split('/');
                if (await _toastResult(await apiJson('DELETE', `/toolchains/${k}/${encodeURIComponent(v)}`), 'Removed')) loadPlatformView();
            },
            async createService() {
                const r = await apiJson('POST', '/services', { template: $id('p-svc').value, name: $id('p-svc-name').value || $id('p-svc').value });
                if (await _toastResult(r, 'Service created')) { loadApps(); loadPlatformView(); }
            },
            async createVolume() { if (await _toastResult(await apiJson('POST', '/volumes', { name: $id('p-vol').value }), 'Volume created')) loadPlatformView(); },
            async deleteVolume(id) { if (await _toastResult(await apiJson('DELETE', `/volumes/${id}`), 'Deleted')) loadPlatformView(); },
            async addMonitor() {
                const r = await apiJson('POST', '/monitors', { name: $id('p-mon-name').value, target: $id('p-mon-target').value, public: $id('p-mon-public').checked });
                if (await _toastResult(r, 'Monitor added')) loadPlatformView();
            },
            async deleteMonitor(id) { if (await _toastResult(await apiJson('DELETE', `/monitors/${id}`), 'Deleted')) loadPlatformView(); },
            async saveCaddy() {
                const r = await apiJson('PUT', '/system/edge/caddy', { enabled: $id('p-caddy-on').checked, email: $id('p-caddy-email').value,
                    venvy_hosts: $id('p-caddy-hosts').value.split(',').map(x => x.trim()).filter(Boolean) });
                if (await _toastResult(r, 'Caddy configured')) loadPlatformView();
            },
            async saveNgrok() { if (await _toastResult(await apiJson('PUT', '/system/edge/ngrok', { token: $id('p-ngrok').value }), 'Saved')) loadPlatformView(); },
            async addLogTarget() {
                const kind = $id('p-lt-kind').value, url = $id('p-lt-url').value.trim();
                const cur = (await apiJson('GET', '/system/log-targets')).data || [];
                let t = { kind, enabled: true };
                if (kind === 'syslog') { const [h, p] = url.split(':'); t = { ...t, host: h, port: Number(p || 514), protocol: 'udp' }; }
                else if (kind !== 'journald') t = { ...t, url, token: $id('p-lt-token').value || undefined };
                const r = await apiJson('PUT', '/system/log-targets', [...cur.map(c => ({ id: c.id })), t]);
                if (await _toastResult(r, 'Log target added')) loadPlatformView();
            },
            async removeLogTarget(id) {
                const cur = (await apiJson('GET', '/system/log-targets')).data || [];
                const r = await apiJson('PUT', '/system/log-targets', cur.filter(c => String(c.id) !== String(id)).map(c => ({ id: c.id })));
                if (await _toastResult(r, 'Removed')) loadPlatformView();
            },
            async saveTelemetry() {
                const body = { otlp_url: $id('p-otlp').value };
                if ($id('p-hb').value) body.heartbeat_url = $id('p-hb').value;
                if (await _toastResult(await apiJson('PUT', '/system/telemetry', body))) loadPlatformView();
            },
            async saveStatusPage() {
                const apps = [...document.querySelectorAll('.p-sp-app')].filter(x => x.checked).map(x => Number(x.value));
                await _toastResult(await apiJson('PUT', '/system/status-page', { enabled: $id('p-sp-on').checked, title: $id('p-sp-title').value, apps }));
            },
            async saveReplication() { if (await _toastResult(await apiJson('PUT', '/system/replication', { minutes: Number($id('p-repl').value || 0) }))) loadPlatformView(); },
            async runReplication() { if (await _toastResult(await apiJson('POST', '/system/replication/run'), 'Replicated')) loadPlatformView(); },
            async addServer() {
                const r = await apiJson('POST', '/servers', { name: $id('p-srv-name').value, url: $id('p-srv-url').value, token: $id('p-srv-token').value });
                if (await _toastResult(r, r.data?.status === 'ok' ? 'Server added' : `Added — ${r.data?.status || ''}`)) loadPlatformView();
            },
            async removeServer(id) { if (await _toastResult(await apiJson('DELETE', `/servers/${id}`), 'Removed')) loadPlatformView(); },
            async remoteApps() {
                const r = await apiJson('GET', '/servers/apps');
                $id('p-remote-apps').innerHTML = r.ok ? r.data.map(a => a.error ? `<p class="text-xs" style="color:var(--red)">${_H(a.server)}: ${_H(a.error)}</p>` :
                    `<div class="text-xs flex gap-2 items-center">${statusPill(a.status)}<span style="color:var(--text3)">${_H(a.server)}</span><span class="font-semibold flex-1">${_H(a.name)}</span>
                    ${['start', 'stop', 'restart', 'deploy'].map(x => _btn(x, _pa('remoteAction', `${a.server_id}/${a.id}/${x}`), 'btn-ghost', 'admin')).join('')}</div>`).join('') : _H(r.error);
            },
            async remoteAction(spec) {
                const [sid, aid, action] = spec.split('/');
                if (await _toastResult(await apiJson('POST', `/servers/${sid}/apps/${aid}/${action}`), `${action} sent`)) setTimeout(() => _platformHandlers.remoteApps(), 1500);
            },
            async addHostJob() {
                const r = await apiJson('POST', '/system/jobs', { name: $id('p-hj-name').value, schedule: $id('p-hj-sched').value, command: $id('p-hj-cmd').value });
                if (await _toastResult(r, 'Job added')) loadPlatformView();
            },
            async runHostJob(id) { if (await _toastResult(await apiJson('POST', `/system/jobs/${id}/run`), 'Started')) setTimeout(loadPlatformView, 1500); },
            async deleteHostJob(id) { if (await _toastResult(await apiJson('DELETE', `/system/jobs/${id}`), 'Deleted')) loadPlatformView(); },
            async saveSecrets() {
                const body = { vault_addr: $id('p-vault-addr').value, vault_namespace: $id('p-vault-ns').value };
                if ($id('p-vault-token').value) body.vault_token = $id('p-vault-token').value;
                if ($id('p-age').value) body.sops_age_key = $id('p-age').value;
                if (await _toastResult(await apiJson('PUT', '/system/secrets', body))) loadPlatformView();
            },
        };

        function platformAction(name, arg) {
            const fn = Object.prototype.hasOwnProperty.call(_platformHandlers, name) ? _platformHandlers[name] : null;
            if (fn) return fn(arg);
        }
