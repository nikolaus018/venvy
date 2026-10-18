// Settings tab: the environment-variables editor — one row per variable.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
//
// The server still takes and gives a plain {NAME: value} object; this file is
// only the form around it. Three states, because the variables are secrets:
//   editable    a table of rows (the normal case)
//   hidden      this role may not see them: a note, nothing is sent on save
//   unreadable  stored, but the server key is gone: a note, nothing is sent on
//               save, until the user chooses to replace them
        function makeEnvEditor(P, mountId, opts = {}) {
            const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
            let mode = 'editable';          // editable | hidden | unreadable
            let revealAll = false;
            let wired = false;

            const emptyText = opts.emptyText || 'No variables yet. Each one becomes an environment variable of the app.';
            const importBtn = opts.noImport ? '' : `<button type="button" data-env-act="import" data-requires-operator class="btn-ghost text-xs font-semibold px-2.5 py-1 rounded-lg">Import .env</button>`;
            document.getElementById(mountId).innerHTML = `
                <div class="flex items-center justify-between gap-3 mb-2">
                    <span class="section-label"><span id="${P}-title">${opts.title || 'Environment Variables'}</span> <span id="${P}-count" class="font-normal normal-case ml-1" style="color:var(--text3)"></span></span>
                    <div class="flex items-center gap-1.5">
                        <button type="button" data-env-act="reveal" class="btn-ghost text-xs font-semibold px-2.5 py-1 rounded-lg"><span id="${P}-reveal-label">Show values</span></button>
                        ${importBtn}
                        ${opts.noHistory ? '' : `<button type="button" data-env-act="history" data-requires-operator class="btn-ghost text-xs font-semibold px-2.5 py-1 rounded-lg">History</button>`}
                    </div>
                </div>
                <div id="${P}-editor" class="rounded-xl overflow-hidden" style="background:var(--surface2);border:1px solid var(--border2)">
                    <div id="${P}-note" class="hidden px-4 py-4 text-xs" style="color:var(--text3)">
                        <p id="${P}-note-text"></p>
                        <button type="button" id="${P}-replace" data-env-act="replace" class="hidden btn-ghost text-xs font-semibold px-3 py-1.5 rounded-lg mt-3">Replace them with new variables</button>
                    </div>
                    <div id="${P}-body">
                        <div id="${P}-import" class="hidden p-3" style="border-bottom:1px solid var(--border)">
                            <textarea id="${P}-import-text" rows="5" spellcheck="false" class="inp textarea w-full px-3 py-2 mono text-xs"
                                placeholder="Paste the contents of a .env file:&#10;API_KEY=abc123&#10;DEBUG=true" style="min-height:0"></textarea>
                            <div class="flex justify-end gap-2 mt-2">
                                <button type="button" data-env-act="import" class="btn-ghost text-xs font-semibold px-3 py-1.5 rounded-lg">Cancel</button>
                                <button type="button" data-env-act="run-import" class="btn-secondary text-xs px-4 py-1.5">Import</button>
                            </div>
                        </div>
                        <div id="${P}-head" class="hidden grid gap-2 px-3 pt-2.5 pb-1 section-label" style="grid-template-columns:minmax(0,2fr) minmax(0,3fr) 2.25rem">
                            <span>Name</span><span>Value</span><span></span>
                        </div>
                        <div id="${P}-rows"></div>
                        <p id="${P}-empty" class="px-4 py-5 text-xs text-center" style="color:var(--text3)">${emptyText}</p>
                        <div class="px-3 py-2.5" style="border-top:1px solid var(--border)">
                            <button type="button" data-env-act="add" data-requires-operator class="btn-ghost text-xs font-semibold px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.4" d="M12 5v14M5 12h14"/></svg>
                                Add variable</button>
                        </div>
                    </div>
                </div>
                <p id="${P}-error" class="hidden text-xs mt-1.5" style="color:var(--red)"></p>
                <div id="${P}-history" class="hidden mt-2 rounded-xl text-xs" style="background:var(--surface2);border:1px solid var(--border2)"></div>`;

            const $ = id => document.getElementById(`${P}-${id}`);
            const changed = () => { _validate(); if (opts.onChange) opts.onChange(); else if (typeof checkSettingsDirty === 'function') checkSettingsDirty(); };

            const ICON_X = '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M6 18L18 6M6 6l12 12"/></svg>';

            function _rows() { return Array.from($('rows').querySelectorAll('.env-row')); }

            function _addRow(name = '', value = '', focus = false) {
                const row = document.createElement('div');
                row.className = 'env-row grid items-center gap-2 px-3 py-1.5';
                row.style.gridTemplateColumns = 'minmax(0,2fr) minmax(0,3fr) auto';
                row.innerHTML = `
                    <input type="text" class="env-key inp w-full px-3 py-2 mono text-xs" placeholder="API_KEY"
                        autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Variable name">
                    <input type="${revealAll ? 'text' : 'password'}" class="env-val inp w-full px-3 py-2 mono text-xs" placeholder="value"
                        autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Value">
                    <button type="button" class="env-del p-2 rounded-lg" style="color:var(--text3)" title="Remove" aria-label="Remove variable">${ICON_X}</button>`;
                const key = row.querySelector('.env-key'), val = row.querySelector('.env-val');
                key.value = name; val.value = value;
                // A value is hidden until it is being edited (or "Show values" is on).
                val.addEventListener('focus', () => { val.type = 'text'; });
                val.addEventListener('blur', () => { if (!revealAll) val.type = 'password'; });
                key.addEventListener('paste', e => {
                    const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
                    if (!/\n/.test(text) && !/^\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=/.test(text)) return;
                    const parsed = _parseDotenv(text);
                    if (!parsed.length) return;
                    e.preventDefault();
                    _merge(parsed, row);
                });
                row.querySelector('.env-del').addEventListener('click', () => {
                    row.remove(); _renderEmpty(); changed();
                });
                $('rows').appendChild(row);
                _renderEmpty();
                if (focus) key.focus();
                return row;
            }

            function _renderEmpty() {
                $('empty').classList.toggle('hidden', _rows().length > 0);
                $('head').classList.toggle('hidden', _rows().length === 0);
                const n = _vars().count;
                $('count').textContent = n ? `${n} variable${n === 1 ? '' : 's'}` : '';
            }

            // KEY=value lines as found in a .env file: comments, blank lines,
            // `export `, and values in single or double quotes.
            function _parseDotenv(text) {
                const out = [];
                for (const raw of text.split(/\r?\n/)) {
                    const line = raw.trim();
                    if (!line || line.startsWith('#')) continue;
                    const m = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
                    if (!m) continue;
                    let v = m[2];
                    const q = v[0];
                    if ((q === '"' || q === "'") && v.lastIndexOf(q) > 0) {
                        v = v.slice(1, v.lastIndexOf(q));
                        if (q === '"') v = v.replace(/\\n/g, '\n').replace(/\\"/g, '"');
                    } else {
                        v = v.replace(/\s+#.*$/, '').trim();    // an unquoted trailing comment
                    }
                    out.push([m[1], v]);
                }
                return out;
            }

            // Put pairs into the table: a name that is already there is
            // updated, the rest are added. `into` is a blank row to reuse.
            function _merge(pairs, into) {
                for (const [k, v] of pairs) {
                    let row = _rows().find(r => r.querySelector('.env-key').value.trim() === k);
                    if (!row && into && !into.querySelector('.env-key').value.trim() && !into.querySelector('.env-val').value) row = into;
                    if (!row) row = _addRow();
                    row.querySelector('.env-key').value = k;
                    row.querySelector('.env-val').value = v;
                }
                changed();
            }

            // The table as an object, plus what is wrong with it, if anything.
            function _vars() {
                const vars = {};
                let error = null, bad = null, count = 0;
                const seen = new Map();
                for (const row of _rows()) {
                    const k = row.querySelector('.env-key').value.trim();
                    const v = row.querySelector('.env-val').value;
                    row.classList.remove('env-bad');
                    if (!k && !v) continue;                       // a blank row is nothing
                    let why = null;
                    if (!k) why = 'A value needs a name.';
                    else if (!NAME_RE.test(k)) why = `"${k}" is not a valid name — use letters, digits and underscores, not starting with a digit.`;
                    else if (seen.has(k)) { why = `"${k}" is there twice.`; seen.get(k).dataset.why = why; seen.get(k).classList.add('env-bad'); }
                    else if (v.includes('\0')) why = `${k} contains a NUL character.`;
                    if (why) {
                        row.dataset.why = why; row.classList.add('env-bad');
                        if (!error) { error = why; bad = row; }
                        continue;
                    }
                    seen.set(k, row);
                    vars[k] = v; count++;
                }
                return { vars, error, bad, count };
            }

            function _validate() {
                const { error } = _vars();
                for (const row of _rows()) {
                    const isBad = row.classList.contains('env-bad');
                    row.querySelector('.env-key').style.borderColor = isBad ? 'var(--red)' : '';
                    row.title = isBad ? row.dataset.why : '';
                }
                const el = $('error');
                el.textContent = error || '';
                el.classList.toggle('hidden', !error);
                _renderEmpty();
            }

            function _show(which, noteText) {
                mode = which;
                const note = $('note');
                note.classList.toggle('hidden', which === 'editable');
                $('note-text').textContent = noteText || '';
                $('replace').classList.toggle('hidden', which !== 'unreadable');
                $('body').classList.toggle('hidden', which !== 'editable');
            }

            async function _toggleHistory() {
                const box = $('history');
                if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
                box.classList.remove('hidden');
                box.innerHTML = '<p class="px-4 py-3" style="color:var(--text3)">Loading…</p>';
                try {
                    const res = await fetch(opts.historyUrl());
                    if (!res.ok) throw new Error();
                    const rows = await res.json();
                    const chip = (names, color) => names.map(n => `<span class="mono px-1.5 py-0.5 rounded" style="background:var(--surface3);color:${color}">${escapeHtml(n)}</span>`).join(' ');
                    box.innerHTML = rows.length ? rows.map(r => `
                        <div class="px-4 py-2.5 flex flex-wrap items-center gap-x-2 gap-y-1" style="border-bottom:1px solid var(--border)">
                            <span style="color:var(--text3)">${escapeHtml(fmtWhen(r.ts))}</span><span style="color:var(--text2)">${escapeHtml(r.user || '')}</span>
                            ${r.added.length ? 'added ' + chip(r.added, 'var(--green)') : ''}
                            ${r.changed.length ? 'changed ' + chip(r.changed, 'var(--amber)') : ''}
                            ${r.removed.length ? 'removed ' + chip(r.removed, 'var(--red)') : ''}
                        </div>`).join('') + '<p class="px-4 py-2" style="color:var(--text3)">Names only — values are never kept.</p>'
                        : '<p class="px-4 py-3" style="color:var(--text3)">No changes recorded yet.</p>';
                } catch { box.innerHTML = '<p class="px-4 py-3" style="color:var(--red)">Could not load the history.</p>'; }
            }

            const api = {
                // Fill the editor from an app as the API returned it.
                load(app) {
                    if (!wired) { wired = true; $('editor').addEventListener('input', changed); }
                    $('rows').replaceChildren();
                    $('import').classList.add('hidden');
                    $('error').classList.add('hidden');
                    if (app.env_redacted) {
                        _show('hidden', 'Hidden — operator or admin access is required to view or edit environment variables.');
                    } else if (app.env_unreadable) {
                        _show('unreadable', 'These variables are stored but cannot be decrypted (the server\'s .venvy_key is missing or was replaced). They are left untouched until you replace them.');
                    } else {
                        _show('editable');
                        let obj = {};
                        try { const p = JSON.parse(app.env_json || '{}'); if (p && typeof p === 'object' && !Array.isArray(p)) obj = p; } catch { }
                        for (const [k, v] of Object.entries(obj)) _addRow(k, String(v));
                    }
                    _validate();
                },

                // {untouched, vars, error, bad}: untouched means leave the
                // stored variables out of the save.
                state() {
                    if (mode !== 'editable') return { untouched: true, vars: {}, error: null, bad: null };
                    const { vars, error, bad } = _vars();
                    return { untouched: false, vars, error, bad };
                },

                // Does the table differ from what the app has stored?
                differsFrom(app) {
                    const s = this.state();
                    if (s.untouched) return false;
                    if (s.error) return true;
                    let stored = {};
                    try { const p = JSON.parse(app.env_json || '{}'); if (p && typeof p === 'object' && !Array.isArray(p)) stored = p; } catch { }
                    const norm = o => JSON.stringify(Object.keys(o).sort().map(k => [k, String(o[k])]));
                    return norm(s.vars) !== norm(stored);
                },

                add() { _addRow('', '', true); changed(); },
                replaceUnreadable() { _show('editable'); $('rows').replaceChildren(); _validate(); changed(); this.add(); },

                toggleReveal() {
                    revealAll = !revealAll;
                    $('reveal-label').textContent = revealAll ? 'Hide values' : 'Show values';
                    $('rows').querySelectorAll('.env-val').forEach(i => { if (document.activeElement !== i) i.type = revealAll ? 'text' : 'password'; });
                },

                toggleImport() {
                    const box = $('import');
                    box.classList.toggle('hidden');
                    if (!box.classList.contains('hidden')) $('import-text').focus();
                },
                runImport() {
                    const pairs = _parseDotenv($('import-text').value);
                    if (!pairs.length) { showToast('No KEY=value lines found.', 'warn'); return; }
                    _merge(pairs, null);
                    $('import-text').value = '';
                    $('import').classList.add('hidden');
                    showToast(`Imported ${pairs.length} variable${pairs.length === 1 ? '' : 's'}`, 'success');
                },

                focusProblem() { const s = this.state(); s.bad?.querySelector('.env-key')?.focus(); },
            };
            api.loadVars = (obj, unreadable = false) => api.load({ env_json: JSON.stringify(obj || {}), env_unreadable: unreadable });
            document.getElementById(mountId).addEventListener('click', (e) => {
                const b = e.target.closest('[data-env-act]');
                if (!b) return;
                const act = b.dataset.envAct;
                if (act === 'add') api.add();
                else if (act === 'reveal') api.toggleReveal();
                else if (act === 'import') api.toggleImport();
                else if (act === 'run-import') api.runImport();
                else if (act === 'replace') api.replaceUnreadable();
                else if (act === 'history') _toggleHistory();
            });
            return api;
        }

        const envEditor = makeEnvEditor('env', 'env-mount', { historyUrl: () => `${API}/apps/${activeAppId}/env-history` });
