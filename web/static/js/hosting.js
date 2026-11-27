// The app's cards that come from the API (processes, deploy options, routes and
// the Hostnames list, ports and tunnels, secrets, services and volumes, app
// backups, run once), the app settings that live in those tabs' cards, and the
// System view's platform cards (toolchains, services, volumes, monitors, the
// edge, log shipping, telemetry, replication, other servers, host jobs).
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
                input = `<select id="${id}" class="inp w-full px-3.5 py-2.5 text-sm" ${dis}>${extra.options.map(([v, t]) =>
                    `<option value="${_H(v)}" ${String(value ?? '') === String(v) ? 'selected' : ''}>${_H(t)}</option>`).join('')}</select>`;
            } else if (kind === 'textarea') {
                input = `<textarea id="${id}" rows="${extra.rows || 4}" class="inp w-full px-3.5 py-2.5 text-sm mono" placeholder="${_H(extra.ph || '')}" ${dis}>${_H(value ?? '')}</textarea>`;
            } else {
                input = `<input id="${id}" type="${kind === 'number' ? 'number' : 'text'}" class="inp w-full px-3.5 py-2.5 text-sm ${extra.mono ? 'mono' : ''}"
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
                <i data-feather="${icon}" class="w-4 h-4"></i> ${_H(title)}</h3>${sub ? `<p class="text-xs mt-0.5" style="color:var(--text3);overflow-wrap:anywhere">${sub}</p>` : ''}</div>${body}</div>`;
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

        // ── The app's settings that live with other cards ───────────────────
        // Saved with the app's other settings (saveSettings, PUT /apps/{id}): only
        // the ones that changed are sent. Each sits in the card of its question.
        const _TEXT = (k, label, extra = {}) => [k, label, 'text', { mono: true, ...extra }];
        const FORM_FIELDS = {
            'run-extra-mount': [
                _TEXT('start_command', 'Start command', { ph: 'e.g. gunicorn app:app -b 0.0.0.0:$PORT  (blank = the runtime default)', help: 'Runs instead of the runtime default. $PORT, $APP_DATA_DIR and the app\'s variables are expanded; no shell — write sh -c "…" for one.' }),
                _TEXT('install_command', 'Install command', { ph: 'blank = from the manifest (npm ci, uv sync, cargo fetch …)' }),
                _TEXT('build_command', 'Build command', { ph: 'e.g. npm run build — after the runtime\'s own build', help: 'Runs in the app\'s folder after the dependencies are installed, with the app\'s variables. Without a repository a deploy just rebuilds.' }),
                _TEXT('release_command', 'Release command', { ph: 'e.g. python manage.py migrate — after the build, before the restart' }),
                _TEXT('root_dir', 'Root folder', { ph: 'blank = the app folder; e.g. backend (monorepos)' }),
                ['runtime_version', 'Version', 'text', { ph: 'e.g. 20 (Node), 1.82 (Rust) — blank = the host\'s', help: 'Installed on first use (System → Platform → Toolchains). .nvmrc, rust-toolchain.toml … are read too.', only: a => a.runtime !== 'python' }],
                ['container_port', 'Container port', 'number', { ph: '8080', help: 'The port inside the container.', only: a => a.runtime === 'docker' }],
            ],
            'health-extra-mount': [
                ['health_type', 'Check', 'select', { options: [['http', 'HTTP path'], ['tcp', 'TCP port accepts'], ['command', 'Command exits 0'], ['none', 'None']], dflt: 'http' }],
                _TEXT('health_command', 'Health command', { ph: 'e.g. pg_isready -h 127.0.0.1' }),
                ['health_start_period', 'Start period (s)', 'number', { help: 'Failures right after a start don\'t count.' }],
            ],
            'stop-extra-mount': [
                ['stop_signal', 'Stop signal', 'select', { options: ['SIGTERM', 'SIGINT', 'SIGQUIT', 'SIGHUP', 'SIGUSR1', 'SIGUSR2', 'SIGWINCH'].map(x => [x, x]), dflt: 'SIGTERM' }],
                ['stop_timeout', 'Stop timeout (s)', 'number', { ph: 'blank = 5', help: 'How long it gets to shut down before it is killed.' }],
            ],
            'limits-extra-mount': [
                ['pids_limit', 'Max processes', 'number', { ph: 'blank = 4096' }],
                ['io_weight', 'I/O weight (1-10000)', 'number', { ph: 'blank = 100' }],
                ['nofile_limit', 'Max open files', 'number', {}],
            ],
            'disk-action-mount': [
                ['disk_limit_action', 'When over the disk limit', 'select', { options: [['alert', 'Alert'], ['stop', 'Stop the app']], dflt: 'alert' }],
            ],
            'sandbox-mount': [
                ['sandbox', 'Filesystem sandbox (read-only system, private /tmp, nothing of other apps)', 'bool', { dflt: true, only: a => !['docker', 'compose'].includes(a.runtime) }],
                ['egress_policy', 'Outgoing connections', 'select', { dflt: 'allow', options: [['allow', 'Anywhere'], ['deny', 'Only this host'], ['allowlist', 'This host and the list below']] }],
                ['egress_allow', 'Allowed addresses', 'text', { mono: true, ph: '10.0.0.0/8, 1.1.1.1' }],
            ],
            'netx-mount': [
                ['bind_public', 'Reachable from the network on its port', 'bool', { dflt: true, help: 'Off: only through Venvy (proxy, tunnel, Caddy): the firewall closes the port and HOST=127.0.0.1 is set.' }],
                ['access_log', 'Access log of proxied requests', 'bool', { dflt: true }],
            ],
        };

        function _formFieldsFor(app) {
            const out = [];
            for (const [mount, fields] of Object.entries(FORM_FIELDS)) {
                for (const f of fields) {
                    // A worker or replica runs with its app's settings: only its own command is its own.
                    if (app.parent_app_id && f[0] !== 'start_command') continue;
                    if (!f[3]?.only || f[3].only(app)) out.push([mount, f]);
                }
            }
            return out;
        }

        // Egress choices a container app can't have are left out (the API refuses them).
        function _egressOptions(app, f) {
            if (f[0] !== 'egress_policy') return f;
            const opts = app.runtime === 'compose' ? [['allow', 'Anywhere (Docker\'s network; restrict it in the compose file)']]
                : app.runtime === 'docker' ? f[3].options.filter(([v]) => v === 'allow' || (v === 'deny' && !app.port)) : f[3].options;
            const cur = app.egress_policy || 'allow';
            if (!opts.some(([v]) => v === cur)) opts.push([cur, `${cur} (not enforced for this app)`]);
            return [f[0], f[1], f[2], { ...f[3], options: opts }];
        }

        function renderFormExtras(app) {
            if (!app) return;
            const byMount = {};
            for (const [mount, f] of _formFieldsFor(app)) (byMount[mount] = byMount[mount] || []).push(_egressOptions(app, f));
            for (const mount of Object.keys(FORM_FIELDS)) {
                const el = $id(mount);
                if (!el) continue;
                el.innerHTML = (byMount[mount] || []).map(f => _field('f', f, app[f[0]] ?? f[3]?.dflt ?? '')).join('');
            }
            syncHealthFields();
            const note = $id('sandbox-note');
            if (note) note.textContent = ['docker', 'compose'].includes(app.runtime)
                ? 'A container app runs in its own container: its limits are Docker\'s, and its network is Docker\'s.'
                : 'Enforced when apps run as systemd services (Venvy as root on systemd).';
        }

        // The health check shows the fields of its type: a path for HTTP, a command for a command.
        function syncHealthFields() {
            const t = $id('f-health_type')?.value || 'http';
            $id('f-health_command')?.closest('div')?.classList.toggle('hidden', t !== 'command');
            $id('setting-health-path')?.closest('div')?.classList.toggle('hidden', t !== 'http');
            const timing = t === 'none';
            ['setting-health-interval', 'setting-health-failures', 'f-health_start_period'].forEach(id => $id(id)?.closest('div')?.classList.toggle('hidden', timing));
            $id('setting-health-restart')?.closest('label')?.classList.toggle('hidden', timing);
        }

        function _norm(kind, v, dflt) {
            if (kind === 'bool') return v == null ? !!dflt : !!v;
            if (kind === 'number') return v == null || v === '' ? 0 : Number(v);
            return v == null ? String(dflt ?? '') : String(v);
        }

        // The form fields above whose value differs from the app's: {key: value}.
        function formExtrasChanged(app = activeApp) {
            const out = {};
            if (!app) return out;
            for (const [, [key, , kind, extra = {}]] of _formFieldsFor(app)) {
                const el = document.getElementById(`f-${key}`);
                if (!el) continue;
                const now = kind === 'bool' ? el.checked : kind === 'number' ? (el.value === '' ? 0 : Number(el.value)) : el.value;
                if (_norm(kind, now, extra.dflt) !== _norm(kind, app[key], extra.dflt)) out[key] = kind === 'text' || kind === 'select' ? String(now).trim() : now;
            }
            return out;
        }
        document.addEventListener('input', (e) => { if (e.target?.id?.startsWith('f-')) checkSettingsDirty(); });
        document.addEventListener('change', (e) => {
            if (!e.target?.id?.startsWith('f-')) return;
            if (e.target.id === 'f-health_type') syncHealthFields();
            checkSettingsDirty();
        });

        // ── Per-app cards, rendered into the tab they belong to ─────────────
        const PROC_FIELDS = [
            ['instances', 'Instances', 'number', { help: 'Copies of the app behind Venvy\'s proxy (each gets its own $PORT). Restarts go one at a time.' }],
            ['zero_downtime', 'Zero-downtime restarts', 'bool', { help: 'With one instance: start the new copy, wait for it, then restart the app.' }],
        ];
        const DEPLOY_FIELDS = [
            ['deploy_strategy', 'Deploy strategy', 'select', { options: [['in_place', 'In place'], ['atomic', 'Atomic (build next to it, then switch)']], help: 'Atomic: keep data in $APP_DATA_DIR, not in the app folder.' }],
            ['spec_from_repo', 'Use venvy.toml / Procfile from the repository', 'bool'],
            ['environment', 'Environment label', 'text', { ph: 'production' }],
        ];
        const PREVIEW_FIELDS = [
            ['commit_status', 'Report deploys to GitHub / GitLab / Gitea (needs the git token)', 'bool'],
            ['previews_enabled', 'Preview app for every pushed branch', 'bool'],
            ['preview_domain', 'Preview domain', 'text', { ph: 'previews.example.com → <branch>.previews.example.com' }],
        ];
        const SECRET_FIELDS = [
            ['sops_file', 'SOPS file in the repository', 'text', { mono: true, ph: 'secrets.enc.yaml' }],
            ['vault_path', 'Vault path', 'text', { mono: true, ph: 'secret/myapp' }],
        ];

        // A feature that holds nothing yet: one line, opened when it is used.
        function _fold(title, icon, summary, body, open, sub = '') {
            return `<details class="card fold" ${open ? 'open' : ''}><summary class="px-5 py-3.5 flex items-center gap-2 cursor-pointer select-none list-none">
                <i data-feather="${icon}" class="w-4 h-4 shrink-0" style="color:var(--text2)"></i>
                <span class="text-sm font-semibold" style="color:var(--text)">${_H(title)}</span>
                <span class="text-xs truncate min-w-0 flex-1" style="color:var(--text3)">${summary}</span>
                <i data-feather="chevron-right" class="w-4 h-4 shrink-0 fold-chev" style="color:var(--text3)"></i></summary>
                <div class="px-5 pb-5 space-y-3">${sub ? `<p class="text-xs" style="color:var(--text3);overflow-wrap:anywhere">${sub}</p>` : ''}${body}</div></details>`;
        }

        const _hostingSeq = {};
        async function loadHosting(section) {
            const appId = activeAppId;
            const roots = { deploy: 'deploy-hosting-root', network: 'network-hosting-root', config: 'config-hosting-root', data: 'data-hosting-root', jobs: 'jobs-run-root' };
            const root = $id(roots[section]);
            if (!appId || !root) return;
            const seq = (_hostingSeq[section] = (_hostingSeq[section] || 0) + 1);
            const all = await apiJson('GET', '/apps');
            const app = all.ok ? all.data.find(a => a.id === appId) : null;
            if (appId !== activeAppId || seq !== _hostingSeq[section]) return;
            if (!app) { root.innerHTML = `<p class="text-xs" style="color:var(--red)">${_H(all.error || 'App not found')}</p>`; return; }
            _hostingApp = app;
            if (app.parent_app_id) {
                root.innerHTML = _card('Process of another app', 'git-branch',
                    `<p class="text-xs" style="color:var(--text2)">This ${_H(app.process_kind)} runs with the settings of its app; change them there.</p>
                     ${_btn('Open the app', `selectApp(${app.parent_app_id}, '${_jsq(section)}')`, 'btn-primary', '')}`);
                if (window.feather) feather.replace();
                return;
            }
            const html = await _HOSTING_SECTIONS[section](app, all.data);
            if (appId !== activeAppId || seq !== _hostingSeq[section] || html == null) return;
            root.innerHTML = html;
            if (window.feather) feather.replace();
        }

        const _HOSTING_SECTIONS = {
            async deploy(app) {
                const [procs, previews] = await Promise.all([apiJson('GET', `/apps/${app.id}/processes`), apiJson('GET', `/apps/${app.id}/previews`)]);
                const plist = procs.ok ? procs.data : [];
                const out = [];
                out.push(_fold('Processes', 'layers', plist.length || app.instances > 1 || app.zero_downtime
                        ? `${app.instances} instance${app.instances === 1 ? '' : 's'}${plist.length ? ` · ${plist.length} more process${plist.length === 1 ? '' : 'es'}` : ''}`
                        : 'Only the app itself. Workers, replicas…',
                    `<div class="grid grid-cols-1 md:grid-cols-2 gap-3">${PROC_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                     <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'proc'), 'btn-primary')}</div>
                     <div class="space-y-2">${plist.map(p => `<div class="flex items-center gap-2 text-xs flex-wrap">
                        ${statusPill(p.status)} <span class="font-semibold" style="color:var(--text)">${_H(p.process_name)}</span>
                        <span style="color:var(--text3)">${_H(p.process_kind)}${p.port ? ' · port ' + p.port : ''}</span>
                        <span class="mono truncate flex-1 min-w-0" style="color:var(--text2)">${_H(p.start_command || '')}</span>
                        ${_btn('Logs', `selectApp(${p.id}, 'logs')`, 'btn-ghost', '')}
                        ${p.process_kind === 'worker' ? _btn('Remove', _ha('removeProcess', p.id)) : ''}</div>`).join('')}</div>
                     <div class="grid grid-cols-1 md:grid-cols-3 gap-2" data-requires-operator>
                        <input id="h-proc-name" class="inp px-3 py-2 text-xs" placeholder="worker" aria-label="Worker name">
                        <input id="h-proc-cmd" class="inp px-3 py-2 text-xs mono md:col-span-2" placeholder="celery -A app worker" aria-label="Worker command">
                     </div><div class="flex justify-end">${_btn('Add worker', _ha('addProcess'))}</div>`,
                    plist.length > 0 || app.instances > 1 || app.zero_downtime,
                    'Workers and replicas share the app\'s folder, user, variables and limits; each is supervised on its own. A Procfile or venvy.toml can declare them too.'));
                out.push(_card('Deploy options', 'upload-cloud',
                    `<div class="grid grid-cols-1 gap-2">${DEPLOY_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                     <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'deploy'), 'btn-primary')}</div>
                     <div class="pt-3 space-y-2" style="border-top:1px solid var(--border)" data-requires-operator>
                     <p class="text-[11px] font-bold uppercase tracking-wide" style="color:var(--text3)">Deploy an artifact</p>
                     <div class="flex items-center gap-2 flex-wrap"><input type="file" id="h-artifact" accept=".tar.gz,.tgz" class="text-xs min-w-0" aria-label="Artifact (.tar.gz)">
                     ${_btn('Deploy artifact', _ha('deployArtifact'))}</div>
                     <p class="text-[11px]" style="color:var(--text3)">A .tar.gz of the app's files (from CI, or <span class="mono">ship.py deploy</span>) replaces the folder as a whole; venv, node_modules, target and .env / *.db carry over.</p></div>`));
                const pv = previews.ok ? previews.data : [];
                out.push(_fold('Previews and commit statuses', 'git-pull-request',
                    app.previews_enabled ? `previews on${app.preview_domain ? ' · *.' + _H(app.preview_domain) : ''}${pv.length ? ` · ${pv.length} running` : ''}` : (app.commit_status ? 'commit statuses on' : 'Off'),
                    `<div class="grid grid-cols-1 gap-2">${PREVIEW_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                     <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'preview'), 'btn-primary')}</div>
                     ${pv.length ? `<div class="space-y-1">${pv.map(p => `<div class="text-xs flex gap-2 items-center">
                        ${statusPill(p.last_deploy_status || p.status)} <span class="mono">${_H(p.git_branch)}</span>
                        <span class="truncate" style="color:var(--text3)">${_H(p.domain || '')}</span>${_btn('Open', `selectApp(${p.id})`, 'btn-ghost', '')}</div>`).join('')}</div>` : ''}`,
                    app.previews_enabled || app.commit_status || pv.length > 0));
                return out.join('');
            },

            async network(app) {
                const mySeq = _hostingSeq.network;
                const [routes, tunnel, fwd, traffic] = await Promise.all([
                    apiJson('GET', `/apps/${app.id}/routes`), apiJson('GET', `/apps/${app.id}/tunnel`),
                    apiJson('GET', `/apps/${app.id}/forwards`), apiJson('GET', `/apps/${app.id}/traffic`)]);
                if (mySeq !== _hostingSeq.network || app.id !== activeAppId) return null;   // a newer load is on its way
                _renderHostnames(app, routes.ok ? routes.data : [], tunnel.ok ? tunnel.data.routes : []);
                const mount = $id('route-form-mount');
                if (mount && !mount.dataset.ready) {
                    mount.dataset.ready = '1';
                    mount.innerHTML = `<div class="grid grid-cols-2 md:grid-cols-4 gap-2">
                        <input id="h-r-host" class="inp px-3 py-2 text-xs mono col-span-2" placeholder="app.example.com or *.example.com" aria-label="Hostname">
                        <input id="h-r-path" class="inp px-3 py-2 text-xs mono" placeholder="/ (path)" aria-label="Path">
                        <select id="h-r-kind" class="inp px-3 py-2 text-xs" aria-label="Kind"><option value="proxy">Proxy</option><option value="redirect">Redirect</option></select>
                        <input id="h-r-to" class="inp px-3 py-2 text-xs mono col-span-2" placeholder="redirect to (host or URL)" aria-label="Redirect to">
                        <input id="h-r-ip" class="inp px-3 py-2 text-xs mono col-span-2" placeholder="allowed IPs (blank = all)" aria-label="Allowed IPs">
                        <input id="h-r-user" class="inp px-3 py-2 text-xs" placeholder="basic auth user" aria-label="Basic auth user">
                        <input id="h-r-pass" type="password" class="inp px-3 py-2 text-xs" placeholder="password" aria-label="Basic auth password">
                        <input id="h-r-rate" type="number" class="inp px-3 py-2 text-xs" placeholder="requests/min" aria-label="Rate limit per minute">
                        <input id="h-r-body" type="number" class="inp px-3 py-2 text-xs" placeholder="max body MB" aria-label="Max body MB">
                        <input id="h-r-cache" type="number" class="inp px-3 py-2 text-xs" placeholder="cache seconds" aria-label="Cache seconds">
                        <label class="text-xs flex items-center gap-1"><input type="checkbox" id="h-r-strip"> strip path</label>
                        <label class="text-xs flex items-center gap-1"><input type="checkbox" id="h-r-gzip"> gzip</label>
                        <textarea id="h-r-headers" rows="2" class="inp px-3 py-2 text-xs mono col-span-2 md:col-span-4" placeholder='extra response headers as JSON: {"X-Frame-Options": "DENY"}' aria-label="Extra response headers"></textarea>
                     </div><div class="flex justify-end mt-2">${_btn('Add route', _ha('addRoute'), 'btn-primary')}</div>`;
                }
                const out = [];
                const fl = fwd.ok ? fwd.data : [];
                out.push(_fold('Ports, Tailscale, ngrok', 'share-2',
                    [fl.length ? `${fl.length} port forward${fl.length === 1 ? '' : 's'}` : '', app.external_url && !/cloudflare|trycloudflare/.test(app.external_url) ? _H(app.external_url) : ''].filter(Boolean).join(' · ') || 'None',
                    `<div class="space-y-1">${fl.map(f => `<div class="text-xs flex gap-2 flex-wrap"><span class="mono">${_H(f.protocol)} ${_H(f.listen_host)}:${f.listen_port} → ${f.target_port}</span>
                        <span style="color:${f.listening ? 'var(--green)' : 'var(--red)'}">${f.listening ? 'listening' : _H(f.error || 'not listening')}</span><span class="flex-1"></span>${_btn('Remove', _ha('deleteForward', f.id), 'btn-ghost', 'admin')}</div>`).join('')}</div>
                     <div class="flex flex-wrap gap-2" data-requires-admin><select id="h-f-proto" class="inp px-3 py-2 text-xs" aria-label="Protocol"><option>tcp</option><option>udp</option></select>
                     <input id="h-f-port" type="number" class="inp px-3 py-2 text-xs w-28" placeholder="host port" aria-label="Host port">
                     <input id="h-f-target" type="number" class="inp px-3 py-2 text-xs w-28" placeholder="app port" aria-label="App port">${_btn('Forward', _ha('addForward'), 'btn-ghost', 'admin')}</div>
                     <div class="flex flex-wrap gap-2" data-requires-admin><select id="h-p-mode" class="inp px-3 py-2 text-xs" aria-label="Publish with">
                     <option value="serve">Tailscale (tailnet)</option><option value="funnel">Tailscale Funnel (internet)</option><option value="ngrok">ngrok</option></select>
                     <input id="h-p-arg" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="https port (443/8443/10000) or ngrok domain" aria-label="HTTPS port or ngrok domain">
                     ${_btn('Publish', _ha('publish'), 'btn-ghost', 'admin')}${_btn('Unpublish', _ha('unpublish'), 'btn-ghost', 'admin')}</div>`,
                    fl.length > 0, 'Raw TCP/UDP ports on the host, and publishing through Tailscale or ngrok (admins).'));
                if (traffic.ok) {
                    const req = Object.entries(traffic.data.requests || {});
                    out.push(_card('Traffic', 'bar-chart-2', `<p class="text-xs" style="color:var(--text3)">Through Venvy's proxy: ${req.map(([k, v]) => `${_H(k)} ${v}`).join(' · ') || 'no requests yet'}${traffic.data.avg_ms != null ? ` · ${traffic.data.avg_ms.toFixed(1)} ms avg` : ''}. ${_btn('Access log', _ha('accessLog'), 'btn-ghost', '')}</p>
                        <pre id="h-access-log" class="text-[11px] mono hidden overflow-x-auto p-2 rounded max-h-80" style="background:var(--surface)"></pre>`));
                }
                return out.join('');
            },

            async config(app) {
                const set = app.sops_file || app.vault_path || app.has_secret_files;
                return _fold('Secrets', 'lock', set ? [app.sops_file ? 'SOPS' : '', app.vault_path ? 'Vault' : '', app.has_secret_files ? 'secret files' : ''].filter(Boolean).join(' · ') : 'None: files, SOPS, Vault',
                    `<div class="grid grid-cols-1 md:grid-cols-2 gap-3">${SECRET_FIELDS.map(f => _field('h', f, app[f[0]])).join('')}</div>
                     <div class="flex justify-end">${_btn('Save', _ha('saveFields', 'secret'), 'btn-primary')}</div>
                     <div data-requires-operator><label for="h-secret-files" class="block text-[11px] font-bold uppercase tracking-wide mb-1" style="color:var(--text3)">Secret files ${app.has_secret_files ? '(set — saving replaces all)' : ''}</label>
                     <textarea id="h-secret-files" rows="4" class="inp w-full px-3 py-2 text-xs mono" placeholder='{"credentials.json": "{...}", "tls.key": "-----BEGIN …"}'></textarea>
                     <p class="text-[11px] mt-1" style="color:var(--text3)">Written to $SECRETS_DIR at every start, readable by the app only; never shown again.</p></div>
                     <div class="flex justify-end">${_btn('Replace secret files', _ha('saveSecretFiles'))}</div>`,
                    !!set, 'Kept out of the variables: files in $SECRETS_DIR, a SOPS file from the repository (age key: System → Platform), or a Vault path.');
            },

            async data(app, apps) {
                const [svcs, vols, backups] = await Promise.all([apiJson('GET', `/apps/${app.id}/services`), apiJson('GET', '/volumes'),
                    _canOp() ? apiJson('GET', `/apps/${app.id}/backups`) : null]);
                const svcList = svcs.ok ? svcs.data : [];
                const volList = vols.ok ? vols.data : [];
                const attached = volList.filter(v => v.apps.some(x => x.app_id === app.id));
                const services = apps.filter(a => a.service_template && a.id !== app.id);
                const out = [];
                out.push(_fold('Services and volumes', 'database',
                    [svcList.map(b => _H(b.service)).join(', '), attached.map(v => _H(v.name)).join(', ')].filter(Boolean).join(' · ') || 'None attached',
                    `<div class="space-y-1">${svcList.map(b => `<div class="text-xs flex gap-2 items-center flex-wrap"><span class="font-semibold">${_H(b.service)}</span>
                        <span class="min-w-0" style="color:var(--text3);overflow-wrap:anywhere">${_H(b.template)} → ${b.variables.map(_H).join(', ')}</span><span class="flex-1"></span>${_btn('Detach', _ha('unbind', b.id))}</div>`).join('')
                        || '<p class="text-xs" style="color:var(--text3)">Uses no managed service. Make one under System → Platform → Services.</p>'}</div>
                     ${services.length ? `<div class="flex flex-wrap gap-2" data-requires-operator><select id="h-bind" class="inp px-3 py-2 text-xs flex-1 min-w-0" aria-label="Service">${services.map(a => `<option value="${a.id}">${_H(a.name)} (${_H(a.service_template)})</option>`).join('')}</select>
                     <input id="h-bind-prefix" class="inp px-3 py-2 text-xs w-28" placeholder="PREFIX" aria-label="Variable prefix">${_btn('Use', _ha('bind'))}</div>` : ''}
                     <div class="space-y-1">${volList.map(v => {
                        const at = v.apps.find(x => x.app_id === app.id);
                        return `<div class="text-xs flex gap-2 items-center"><span class="mono">${_H(v.name)}</span><span style="color:var(--text3)">$${_H(v.env)}</span>
                        <span class="flex-1"></span>${at ? _btn('Detach', _ha('detachVolume', v.id), 'btn-ghost', 'admin') : _btn('Attach', _ha('attachVolume', v.id), 'btn-ghost', 'admin')}</div>`;
                     }).join('') || '<p class="text-xs" style="color:var(--text3)">No shared volumes yet (System → Platform → Volumes).</p>'}</div>`,
                    svcList.length > 0 || attached.length > 0, 'Managed databases and caches the app uses (their variables are set at start), and folders shared with other apps.'));
                if (backups && backups.ok) {
                    const bl = backups.data;
                    out.push(_card('App backups', 'archive',
                        `<div class="space-y-1">${bl.map(b => `<div class="text-xs flex gap-2 items-center flex-wrap"><span class="mono min-w-0" style="overflow-wrap:anywhere">${_H(b.name)}</span>
                            <span style="color:var(--text3)">${fmtBytes(b.size)} · ${_H(fmtWhen(b.created_at))}</span><span class="flex-1"></span>
                            ${_btn('Restore', _ha('restoreBackup', b.name))}${_btn('Delete', _ha('deleteBackup', b.name))}</div>`).join('') || '<p class="text-xs" style="color:var(--text3)">None yet.</p>'}</div>
                         <div class="flex justify-end">${_btn('Back up now', _ha('backup'), 'btn-primary')}</div>`,
                        'This app\'s folder and $APP_DATA_DIR, with consistent copies of SQLite databases. Not a release: releases (Deploy tab) are the app\'s files before each deploy. Everything at once: System → Backups.'));
                }
                return out.join('');
            },

            async jobs(app) {
                return _card('Run once', 'terminal',
                    `<div class="flex flex-wrap gap-2" data-requires-operator><input id="h-run" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="python manage.py createsuperuser --noinput" aria-label="Command" data-on-enter="${_ha('run')}">
                     ${_btn('Run', _ha('run'), 'btn-primary')}</div><pre id="h-run-out" class="text-[11px] mono hidden overflow-x-auto p-2 rounded max-h-80" style="background:var(--surface)"></pre>`,
                    'A command now, as the app, in its folder, with its variables (migrations, a console script, a check).');
            },
        };

        // ── Network → Hostnames: one list of every way in by name ───────────
        let _cfRoutesSeen = [];
        function _renderHostnames(app, routes, cfRoutes) {
            const el = $id('hostnames-list');
            if (!el) return;
            _cfRoutesSeen = cfRoutes || [];
            const rows = [];
            if (app.domain) rows.push({ host: app.domain, path: '/', by: 'Venvy\'s proxy', what: 'main domain', act: _btn('Change', `showHostnameEditor('domain')`, 'btn-ghost') });
            for (const r of routes) {
                const extras = [r.kind === 'redirect' ? `→ ${_H(r.redirect_to)} (${r.redirect_code})` : '', r.strip_prefix ? 'strip' : '',
                    r.basic_auth.length ? 'auth' : '', r.ip_allow ? 'ip list' : '', r.rate_limit ? `${r.rate_limit}/min` : '', r.gzip ? 'gzip' : '',
                    r.cache_seconds ? `cache ${r.cache_seconds}s` : ''].filter(Boolean).join(' · ');
                rows.push({ host: r.hostname, path: r.path_prefix, by: 'Venvy\'s proxy', what: r.kind === 'redirect' ? 'redirect' : 'route', extras, act: _btn('Delete', _ha('deleteRoute', r.id)) });
            }
            for (const c of _cfRoutesSeen) {
                rows.push({ host: c.hostname, path: '/', by: 'Cloudflare tunnel', what: c.published ? `port ${c.local_port}` : 'not published', act: _btn('Manage', `showHostnameEditor('tunnel')`, 'btn-ghost') });
            }
            el.innerHTML = rows.length ? rows.map(r => `<div class="flex items-center gap-2 text-xs flex-wrap py-1">
                    <span class="mono font-semibold min-w-0" style="color:var(--text);overflow-wrap:anywhere">${_H(r.host)}${r.path && r.path !== '/' ? _H(r.path) : ''}</span>
                    <span class="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide" style="background:var(--surface3);color:var(--text2)">${_H(r.by)}</span>
                    <span style="color:var(--text3)">${_H(r.what)}${r.extras ? ' · ' + r.extras : ''}</span>
                    <span class="flex-1"></span>${r.act}</div>`).join('')
                : `<p class="text-xs" style="color:var(--text3)">No hostname yet: it is reached on its port only${app.local_url ? ` (<span class="mono">${_H(app.local_url)}</span>)` : ''}. Add one below.</p>`;
            if (window.feather) feather.replace();
        }

        function showHostnameEditor(kind) {
            const sel = $id('host-add-kind');
            if (sel && sel.value !== kind) sel.value = kind;
            for (const k of ['domain', 'route', 'tunnel']) $id(`host-ed-${k}`)?.classList.toggle('hidden', k !== kind);
            if (kind === 'tunnel') loadCfRouteStatus();
            const target = kind === 'tunnel' ? $id('host-ed-tunnel') : $id(`host-ed-${kind}`);
            if (target) target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }

        const _GROUPS = { proc: PROC_FIELDS, deploy: DEPLOY_FIELDS, preview: PREVIEW_FIELDS, secret: SECRET_FIELDS };
        const _GROUP_TAB = { proc: 'deploy', deploy: 'deploy', preview: 'deploy', secret: 'config' };

        const _hostingHandlers = {
            async saveFields(group) {
                const body = _read('h', _GROUPS[group]);
                const r = await apiJson('PUT', `/apps/${activeAppId}`, body);
                if (await _toastResult(r)) { loadApps(); loadHosting(_GROUP_TAB[group]); }
            },
            async addProcess() {
                const r = await apiJson('POST', `/apps/${activeAppId}/processes`, { name: $id('h-proc-name').value, command: $id('h-proc-cmd').value });
                if (await _toastResult(r, 'Worker added')) loadHosting('deploy');
            },
            async removeProcess(id) {
                if (!await confirmDialog({ title: 'Remove this worker?', body: 'It is stopped and removed.', danger: true, confirmLabel: 'Remove' })) return;
                if (await _toastResult(await apiJson('DELETE', `/apps/${id}`), 'Removed')) loadHosting('deploy');
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
                if (await _toastResult(await apiJson('POST', `/apps/${activeAppId}/routes`, body), 'Route added')) {
                    ['h-r-host', 'h-r-path', 'h-r-to', 'h-r-ip', 'h-r-user', 'h-r-pass', 'h-r-headers'].forEach(id => { const e = $id(id); if (e) e.value = ''; });
                    loadHosting('network');
                }
            },
            async deleteRoute(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/routes/${id}`), 'Route deleted')) loadHosting('network');
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
                if (await _toastResult(await apiJson('PUT', `/apps/${activeAppId}`, { secret_files: files }), 'Secret files replaced')) loadHosting('config');
            },
            async bind() {
                const r = await apiJson('POST', `/apps/${activeAppId}/services`, { service_app_id: Number($id('h-bind').value), prefix: $id('h-bind-prefix').value });
                if (await _toastResult(r, 'Service attached — restart the app to use it')) loadHosting('data');
            },
            async unbind(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/services/${id}`), 'Detached')) loadHosting('data');
            },
            async attachVolume(id) {
                if (await _toastResult(await apiJson('POST', `/apps/${activeAppId}/volumes/${id}`, { read_only: false }), 'Attached — restart the app')) loadHosting('data');
            },
            async detachVolume(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/volumes/${id}`), 'Detached')) loadHosting('data');
            },
            async addForward() {
                const body = { protocol: $id('h-f-proto').value, listen_port: Number($id('h-f-port').value), target_port: Number($id('h-f-target').value) || null };
                if (await _toastResult(await apiJson('POST', `/apps/${activeAppId}/forwards`, body), 'Forwarding')) loadHosting('network');
            },
            async deleteForward(id) {
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/forwards/${id}`), 'Removed')) loadHosting('network');
            },
            async publish() {
                const mode = $id('h-p-mode').value, arg = $id('h-p-arg').value.trim();
                const body = mode === 'ngrok' ? { provider: 'ngrok', domain: arg } : { provider: 'tailscale', mode, https_port: Number(arg || 443) };
                const r = await apiJson('POST', `/apps/${activeAppId}/publish`, body);
                if (await _toastResult(r, r.data?.url ? `Published at ${r.data.url}` : 'Published')) loadHosting('network');
            },
            async unpublish() {
                const mode = $id('h-p-mode').value;
                const r = await apiJson('DELETE', `/apps/${activeAppId}/publish/${mode === 'ngrok' ? 'ngrok' : 'tailscale'}`);
                if (await _toastResult(r, 'Unpublished')) loadHosting('network');
            },
            async deployArtifact() {
                const f = $id('h-artifact').files[0];
                if (!f) { showToast('Choose a .tar.gz first', 'error'); return; }
                const fd = new FormData();
                fd.append('file', f);
                const res = await fetch(`${API}/apps/${activeAppId}/deploy/artifact`, { method: 'POST', body: fd });
                const body = await res.json().catch(() => ({}));
                if (!res.ok) { showToast(body.detail || 'Upload failed', 'error'); return; }
                showToast(body.queued ? 'Queued behind the running deploy' : 'Deploy started', 'success');
                loadDeploy();
                $id('tab-deploy')?.scrollTo({ top: 0, behavior: 'smooth' });
            },
            async backup() {
                showToast('Backing up…', 'info');
                try { await waitForJob(await fetch(`${API}/apps/${activeAppId}/backups`, { method: 'POST' })); showToast('Backup made', 'success'); } catch (e) { showToast(e.message, 'error'); }
                loadHosting('data');
            },
            async restoreBackup(name) {
                if (!await confirmDialog({ title: 'Restore this backup?', body: 'The app is stopped and its folder and data are replaced by the backup.', danger: true, confirmLabel: 'Restore' })) return;
                try {
                    await waitForJob(await fetch(`${API}/apps/${activeAppId}/backups/${encodeURIComponent(name)}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }));
                    showToast('Restored', 'success');
                } catch (e) { showToast(e.message, 'error'); }
            },
            async deleteBackup(name) {
                if (!await confirmDialog({ title: 'Delete this backup?', body: name, danger: true, confirmLabel: 'Delete' })) return;
                if (await _toastResult(await apiJson('DELETE', `/apps/${activeAppId}/backups/${encodeURIComponent(name)}`), 'Deleted')) loadHosting('data');
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
                    if (typeof loadJobs === 'function') loadJobs();
                } catch (e) { out.textContent = e.message; }
            },
        };

        function hostingAction(name, arg) {
            const fn = Object.prototype.hasOwnProperty.call(_hostingHandlers, name) ? _hostingHandlers[name] : null;
            if (fn) return fn(arg);
        }

        // ── Platform cards of the System view, each in its section ──────────
        async function loadPlatformView() {
            const roots = { platform: $id('sys-platform'), alerts: $id('sys-alerts-extra'), network: $id('sys-network-extra'),
                integrations: $id('sys-integrations-extra'), backups: $id('sys-backups-extra') };
            if (!roots.platform) return;
            if (!_canOp()) { Object.values(roots).forEach(r => r && (r.innerHTML = '')); roots.platform.classList.add('hidden'); return; }
            roots.platform.classList.remove('hidden');
            const admin = _isAdmin();
            const reqs = [apiJson('GET', '/toolchains'), apiJson('GET', '/services/templates'), apiJson('GET', '/volumes'), apiJson('GET', '/monitors')];
            if (admin) reqs.push(apiJson('GET', '/system/edge'), apiJson('GET', '/system/log-targets'), apiJson('GET', '/system/telemetry'),
                apiJson('GET', '/system/replication'), apiJson('GET', '/servers'), apiJson('GET', '/system/jobs'), apiJson('GET', '/system/secrets'),
                apiJson('GET', '/system/status-page'));
            const [tc, st, vols, mons, edge, logs, tele, repl, servers, hjobs, secr, sp] = await Promise.all(reqs);
            const out = { platform: [], alerts: [], network: [], integrations: [], backups: [] };
            const none = (t) => `<p class="text-xs" style="color:var(--text3)">${t}</p>`;
            const kinds = tc.ok ? tc.data.kinds : [];
            const installed = kinds.reduce((n, k) => n + k.installed.length, 0);
            out.platform.push(_fold('Toolchains', 'tool', installed ? kinds.filter(k => k.installed.length).map(k => `${_H(k.kind)} ${k.installed.map(_H).join(', ')}`).join(' · ') : 'None installed: the host\'s own tools are used',
                kinds.map(k => `<div class="text-xs flex gap-2 items-center flex-wrap">
                <span class="font-semibold w-16">${_H(k.kind)}</span><span class="mono flex-1 min-w-0" style="color:var(--text2)">${k.installed.map(_H).join(', ') || '—'}</span>
                ${k.installed.map(v => _btn('×' + v, _pa('removeToolchain', k.kind + '/' + v), 'btn-ghost', 'admin')).join('')}</div>`).join('')
                + `<div class="flex flex-wrap gap-2" data-requires-admin><select id="p-tc-kind" class="inp px-3 py-2 text-xs" aria-label="Language">${kinds.map((k, i, all) => `<option value="${_H(k.kind)}" ${k.can_install ? '' : 'disabled'} ${i === Math.max(0, all.findIndex(x => x.can_install)) ? 'selected' : ''}>${_H(k.kind)}${k.can_install ? '' : ' (unavailable)'}</option>`).join('')
                  || '<option disabled>none</option>'}</select>
                  <input id="p-tc-ver" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="version: 20, 1.82, 3.13, latest" aria-label="Version">${_btn('Install', _pa('installToolchain'), 'btn-primary', 'admin')}</div>`,
                installed > 0, `Language versions apps can choose (an app's Deploy → Build and run → Version), checksum-verified. In ${_H(tc.ok ? tc.data.dir : '')}.`));
            const svcApps = (_cachedApps || []).filter(a => a.service_template);
            out.platform.push(_fold('Managed services', 'database', svcApps.length ? svcApps.map(a => _H(a.name)).join(', ') : 'None: PostgreSQL, Redis, MariaDB … in one click',
                `${st.ok && !st.data.docker ? '<p class="text-xs" style="color:var(--yellow)">Docker is not installed (installer: --with-docker).</p>' : ''}
                <div class="flex flex-wrap gap-2"><select id="p-svc" class="inp px-3 py-2 text-xs" aria-label="Service">${(st.ok ? st.data.templates : []).map(t => `<option value="${_H(t.key)}">${_H(t.label)}</option>`).join('')}</select>
                <input id="p-svc-name" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="name, e.g. main-db" aria-label="Service name">${_btn('Create', _pa('createService'), 'btn-primary')}</div>`,
                svcApps.length > 0, 'Databases, caches and queues as containers with generated credentials. Each shows up as an app; attach it to an app in that app\'s Data tab.'));
            const vl = vols.ok ? vols.data : [];
            out.platform.push(_fold('Volumes', 'folder', vl.length ? vl.map(v => _H(v.name)).join(', ') : 'None: folders apps share',
                vl.map(v => `<div class="text-xs flex gap-2"><span class="mono">${_H(v.name)}</span>
                <span style="color:var(--text3)">${v.apps.length} app(s) · $${_H(v.env)}</span><span class="flex-1"></span>${_btn('Delete', _pa('deleteVolume', v.id), 'btn-ghost', 'admin')}</div>`).join('')
                + `<div class="flex flex-wrap gap-2" data-requires-admin><input id="p-vol" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="shared-uploads" aria-label="Volume name">${_btn('Create', _pa('createVolume'), 'btn-primary', 'admin')}</div>`,
                vl.length > 0, 'Folders apps can share (attach them in an app\'s Data tab).'));
            const ml = mons.ok ? mons.data : [];
            out.alerts.push(_fold('Monitors', 'radio', ml.length ? `${ml.length} · ${ml.filter(m => m.last_ok === false).length} failing` : 'None',
                ml.map(m => `<div class="text-xs flex gap-2 items-center flex-wrap">
                ${statusPill(m.last_ok == null ? 'unknown' : m.last_ok ? 'healthy' : 'unhealthy')}<span class="font-semibold">${_H(m.name)}</span>
                <span class="mono truncate flex-1 min-w-0" style="color:var(--text3)">${_H(m.target)}</span><span>${m.uptime_24h != null ? m.uptime_24h.toFixed(2) + '%' : ''}</span>
                ${_btn('Delete', _pa('deleteMonitor', m.id))}</div>`).join('')
                + `<div class="flex flex-wrap gap-2"><input id="p-mon-name" class="inp px-3 py-2 text-xs" placeholder="name" aria-label="Monitor name">
                  <input id="p-mon-target" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="https://example.com/health or tcp://db:5432" aria-label="Target">
                  <label class="text-xs flex items-center gap-1"><input type="checkbox" id="p-mon-public"> on status page</label>${_btn('Add', _pa('addMonitor'), 'btn-primary')}</div>`,
                ml.length > 0, 'For things outside Venvy, checked on a schedule, with notifications. An app\'s own health check is in its Health tab.'));
            if (admin) {
                const s = sp.ok ? sp.data : {};
                out.alerts.push(_fold('Status page', 'check-circle', s.enabled ? `On at /status · ${(s.apps || []).length} app(s)` : 'Off',
                    `<label class="text-xs flex items-center gap-2"><input type="checkbox" id="p-sp-on" ${s.enabled ? 'checked' : ''}> Public status page at <span class="mono">/status</span></label>
                    <input id="p-sp-title" class="inp px-3 py-2 text-xs w-full" value="${_H(s.title || 'Status')}" aria-label="Title">
                    <div class="flex flex-wrap gap-2">${(_cachedApps || []).filter(a => !a.parent_app_id).map(a => `<label class="text-xs flex items-center gap-1"><input type="checkbox" class="p-sp-app" value="${a.id}" ${(s.apps || []).includes(a.id) ? 'checked' : ''}> ${_H(a.name)}</label>`).join('')}</div>
                    <div class="flex justify-end">${_btn('Save', _pa('saveStatusPage'), 'btn-primary', 'admin')}</div>`, !!s.enabled));
                const t = tele.ok ? tele.data : {};
                const hb = t.state?.heartbeat || {};
                out.alerts.push(_fold('Heartbeat', 'heart', t.heartbeat_url ? `On · last ${_H(hb.last_ok ? fmtAgo(hb.last_ok) : 'never')}` : 'Off',
                    `<div class="flex flex-wrap gap-2"><input id="p-hb" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" aria-label="Heartbeat URL" placeholder="${t.heartbeat_url ? 'set — enter a new one to replace it' : 'https://hc-ping.com/… (healthchecks.io and the like)'}">
                    ${_btn('Save', _pa('saveHeartbeat'), 'btn-primary', 'admin')}${t.heartbeat_url ? _btn('Remove', _pa('removeHeartbeat'), 'btn-ghost', 'admin') : ''}</div>
                    <p class="text-xs" style="color:var(--text3)">Last: ${_H(hb.last_ok || 'never')}${hb.last_error ? ' · ' + _H(hb.last_error) : ''}</p>`,
                    !!t.heartbeat_url, 'Venvy calls this URL every minute; the service there tells you when the calls stop (this server is down).'));
                const e = edge.ok ? edge.data : {};
                out.network.push(_card('Caddy, Tailscale, ngrok', 'shield', `
                    <p class="text-xs">Caddy: ${e.caddy?.installed ? (e.caddy.running ? 'running' : 'installed, not running') : 'not installed (--with-caddy)'}</p>
                    <label class="text-xs flex items-center gap-2"><input type="checkbox" id="p-caddy-on" ${e.caddy?.enabled ? 'checked' : ''}> HTTPS for every hostname of every app (Let's Encrypt, on demand)</label>
                    <div class="flex flex-wrap gap-2"><input id="p-caddy-email" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="ACME email (optional)" value="${_H(e.caddy?.email || '')}" aria-label="ACME email">
                    <input id="p-caddy-hosts" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="Venvy's own hostnames, comma separated" value="${_H((e.caddy?.venvy_hosts || []).join(', '))}" aria-label="Venvy's own hostnames">
                    ${_btn('Apply', _pa('saveCaddy'), 'btn-primary', 'admin')}</div>
                    <p class="text-xs">Tailscale: ${e.tailscale?.installed ? _H(e.tailscale.backend || '') + ' ' + _H(e.tailscale.dns_name || '') : 'not installed (--with-tailscale)'}</p>
                    <p class="text-xs">ngrok: ${e.ngrok?.installed ? 'installed' : 'not installed (--with-ngrok)'}${e.ngrok?.has_token ? ' · token set' : ''}</p>
                    ${e.ngrok?.installed || e.ngrok?.has_token ? `<div class="flex flex-wrap gap-2"><input id="p-ngrok" type="password" class="inp px-3 py-2 text-xs flex-1 min-w-0" placeholder="ngrok authtoken" aria-label="ngrok authtoken">${_btn('Save token', _pa('saveNgrok'), 'btn-ghost', 'admin')}</div>` : ''}`,
                    'Ways in besides the Cloudflare tunnel. Which app is published how: its Network tab.'));
                const ll = logs.ok ? logs.data : [];
                out.integrations.push(_fold('Log shipping', 'send', ll.length ? ll.map(x => _H(x.kind)).join(', ') : 'Off',
                    ll.map(x => `<div class="text-xs flex gap-2 flex-wrap"><span class="font-semibold">${_H(x.kind)}</span>
                    <span class="mono flex-1 min-w-0" style="color:var(--text3);overflow-wrap:anywhere">${_H(x.url || (x.host ? x.host + ':' + x.port + '/' + x.protocol : '/dev/log'))}</span>
                    <span style="color:var(--text3)">${x.stats?.sent || 0} sent${x.stats?.last_error ? ' · ' + _H(x.stats.last_error) : ''}</span>${_btn('Remove', _pa('removeLogTarget', x.id), 'btn-ghost', 'admin')}</div>`).join('')
                    + `<div class="flex flex-wrap gap-2"><select id="p-lt-kind" class="inp px-3 py-2 text-xs" aria-label="Kind"><option>loki</option><option>http</option><option>syslog</option><option>journald</option></select>
                      <input id="p-lt-url" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="https://loki:3100  ·  or host:port for syslog" aria-label="Destination">
                      <input id="p-lt-token" type="password" class="inp px-3 py-2 text-xs" placeholder="token (optional)" aria-label="Token">${_btn('Add', _pa('addLogTarget'), 'btn-primary', 'admin')}</div>`,
                    ll.length > 0, 'Every app\'s log, also sent to syslog, journald, Loki or any HTTP endpoint.'));
                out.integrations.push(_fold('Metrics', 'bar-chart-2', t.otlp_url ? `OTLP to ${_H(t.otlp_url)}` : 'Prometheus at /metrics · OTLP off',
                    `<p class="text-xs" style="color:var(--text3)">Prometheus: <span class="mono">GET /metrics</span> with an API token (scope read). Firewall: ${t.firewall?.active ? 'active' : _H(t.firewall?.reason || 'off')}.</p>
                    <div class="flex flex-wrap gap-2"><input id="p-otlp" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="OTLP collector, e.g. http://otel:4318" value="${_H(t.otlp_url || '')}" aria-label="OTLP collector URL">
                    ${_btn('Save', _pa('saveOtlp'), 'btn-primary', 'admin')}</div>
                    <p class="text-xs" style="color:var(--text3)">OTLP: last ${_H(t.state?.otlp?.last_ok || 'never')}</p>`, !!t.otlp_url));
                const sl = servers.ok ? servers.data : [];
                out.integrations.push(_fold('Other servers', 'server', sl.length ? sl.map(x => _H(x.name)).join(', ') : 'None',
                    sl.map(sv => `<div class="text-xs flex gap-2 flex-wrap"><span class="font-semibold">${_H(sv.name)}</span>
                    <span class="mono flex-1 min-w-0" style="color:var(--text3);overflow-wrap:anywhere">${_H(sv.url)}</span><span>${_H(sv.last_status)}</span>${_btn('Remove', _pa('removeServer', sv.id), 'btn-ghost', 'admin')}</div>`).join('')
                    + `<div id="p-remote-apps" class="space-y-1"></div><div class="flex flex-wrap gap-2"><input id="p-srv-name" class="inp px-3 py-2 text-xs" placeholder="name" aria-label="Server name">
                      <input id="p-srv-url" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="https://venvy2.example.com" aria-label="Server URL">
                      <input id="p-srv-token" type="password" class="inp px-3 py-2 text-xs" placeholder="API token from that server" aria-label="API token">${_btn('Add', _pa('addServer'), 'btn-primary', 'admin')}${sl.length ? _btn('Show apps', _pa('remoteApps'), 'btn-ghost', 'admin') : ''}</div>`,
                    sl.length > 0, 'Watch and control the apps of other Venvy servers from here.'));
                const hj = hjobs.ok ? hjobs.data : [];
                out.platform.push(_fold('Host jobs', 'clock', hj.length ? `${hj.length} job${hj.length === 1 ? '' : 's'}` : 'None',
                    hj.map(j => `<div class="text-xs flex gap-2 items-center flex-wrap">${j.last_status ? statusPill(j.last_status) : ''}<span class="font-semibold">${_H(j.name)}</span>
                    <span class="mono" style="color:var(--text3)">${_H(j.schedule)}</span><span class="mono truncate flex-1 min-w-0">${_H(j.command)}</span>${_btn('Run', _pa('runHostJob', j.id), 'btn-ghost', 'admin')}${_btn('Delete', _pa('deleteHostJob', j.id), 'btn-ghost', 'admin')}</div>`).join('')
                    + `<div class="flex flex-wrap gap-2"><input id="p-hj-name" class="inp px-3 py-2 text-xs" placeholder="name" aria-label="Job name"><input id="p-hj-sched" class="inp px-3 py-2 text-xs mono w-32" placeholder="0 4 * * *" aria-label="Schedule">
                      <input id="p-hj-cmd" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="curl -fsS https://example.com/cron" aria-label="Command">${_btn('Add', _pa('addHostJob'), 'btn-primary', 'admin')}</div>`,
                    hj.length > 0, 'Scheduled commands that belong to no app (run as the unprivileged user venvy-app-0). An app\'s own jobs: its Jobs tab.'));
                const se = secr.ok ? secr.data : {};
                const seSet = se.vault_addr || se.vault_token_set || se.sops_age_key_set;
                out.platform.push(_fold('Secrets back-ends', 'key', seSet ? [se.vault_addr ? 'Vault' : '', se.sops_age_key_set ? 'SOPS age key' : ''].filter(Boolean).join(' · ') : 'None: Vault, SOPS',
                    `<div class="flex flex-wrap gap-2"><input id="p-vault-addr" class="inp px-3 py-2 text-xs mono flex-1 min-w-0" placeholder="Vault address" value="${_H(se.vault_addr || '')}" aria-label="Vault address">
                    <input id="p-vault-token" type="password" class="inp px-3 py-2 text-xs" placeholder="${se.vault_token_set ? 'token set — enter to replace' : 'Vault token'}" aria-label="Vault token">
                    <input id="p-vault-ns" class="inp px-3 py-2 text-xs w-32" placeholder="namespace" value="${_H(se.vault_namespace || '')}" aria-label="Vault namespace"></div>
                    <input id="p-age" type="password" class="inp px-3 py-2 text-xs mono w-full" placeholder="${se.sops_age_key_set ? 'age key set — enter to replace' : 'AGE-SECRET-KEY-… for SOPS files'}" aria-label="SOPS age key">
                    <p class="text-xs" style="color:var(--text3)">sops ${se.sops_installed ? 'installed' : 'not installed (--with-sops)'}.</p>
                    <div class="flex justify-end">${_btn('Save', _pa('saveSecrets'), 'btn-primary', 'admin')}</div>`,
                    !!seSet, 'Where an app\'s Config → Secrets come from.'));
                const rp = repl.ok ? repl.data : {};
                out.backups.push(_fold('Database replication', 'copy', rp.minutes ? `Every ${rp.minutes} min · last ${_H(rp.last_ok || 'never')}` : 'Off',
                    `<p class="text-xs" style="color:var(--text3)">${rp.remote ? 'To the off-site backup destination.' : 'Set an off-site destination (Automatic backups) first.'} Last: ${_H(rp.last_ok || 'never')}${rp.last_error ? ' · ' + _H(rp.last_error) : ''}</p>
                    <div class="flex flex-wrap gap-2"><input id="p-repl" type="number" class="inp px-3 py-2 text-xs w-32" value="${rp.minutes || 0}" placeholder="minutes" aria-label="Every N minutes">${_btn('Save', _pa('saveReplication'), 'btn-primary', 'admin')}${_btn('Run now', _pa('runReplication'), 'btn-ghost', 'admin')}</div>`,
                    !!rp.minutes, 'Venvy\'s own database (settings, users, routes, variables; no app files) copied off-site every few minutes, between full backups. 0 = off.'));
                window._protectionRepl = rp;
                if (typeof loadProtection === 'function') loadProtection();
            }
            for (const [k, r] of Object.entries(roots)) if (r) r.innerHTML = out[k].join('');
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
            async deleteMonitor(id) {
                if (!await confirmDialog({ title: 'Delete this monitor?', body: 'It is no longer checked, and leaves the status page.', danger: true, confirmLabel: 'Delete' })) return;
                if (await _toastResult(await apiJson('DELETE', `/monitors/${id}`), 'Deleted')) loadPlatformView(); },
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
            async saveOtlp() {
                if (await _toastResult(await apiJson('PUT', '/system/telemetry', { otlp_url: $id('p-otlp').value }))) loadPlatformView();
            },
            async saveHeartbeat() {
                const u = $id('p-hb').value.trim();
                if (!u) { showToast('Enter the URL to call.', 'warn'); return; }
                if (await _toastResult(await apiJson('PUT', '/system/telemetry', { heartbeat_url: u }))) loadPlatformView();
            },
            async removeHeartbeat() {
                if (await _toastResult(await apiJson('PUT', '/system/telemetry', { heartbeat_url: '' }), 'Heartbeat off')) loadPlatformView();
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
