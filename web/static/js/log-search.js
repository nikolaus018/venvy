// Logs tab: search across every log file of the app (rotated ones too), and download them all.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        function toggleLogSearchPanel(force) {
            const panel = $id('log-search-panel');
            const open = force !== undefined ? force : panel.classList.contains('hidden');
            panel.classList.toggle('hidden', !open);
            $id('log-search-all-btn').setAttribute('aria-pressed', String(open));
            if (open) $id('lsp-q').focus();
        }

        async function runLogSearch() {
            const q = _val('lsp-q');
            const out = $id('lsp-results');
            if (!q) { out.classList.add('hidden'); return; }
            out.classList.remove('hidden');
            out.innerHTML = '<p style="color:var(--text3)">Searching…</p>';
            const btn = $id('lsp-go'); btn.disabled = true;
            const appId = activeAppId;
            const params = new URLSearchParams({ q, regex: $id('lsp-regex').checked, case: $id('lsp-case').checked, context: $id('lsp-ctx').value, limit: 300 });
            const r = await apiJson('GET', `/apps/${appId}/logs/search?${params}`);
            btn.disabled = false;
            if (appId !== activeAppId) return;
            if (!r.ok) { out.innerHTML = `<p style="color:var(--red)">${escapeHtml(r.error)}</p>`; return; }
            const { matches, truncated, files } = r.data;
            if (!matches.length) { out.innerHTML = `<p style="color:var(--text3)">No matches in ${files} log file${files === 1 ? '' : 's'}.</p>`; return; }
            let rx;
            try { rx = new RegExp($id('lsp-regex').checked ? q : q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), $id('lsp-case').checked ? 'g' : 'gi'); } catch { rx = null; }
            const mark = line => {
                const safe = escapeHtml(line);
                if (!rx) return safe;
                // Highlight on the raw line, escaping piece by piece.
                let out = '', last = 0, m;
                rx.lastIndex = 0;
                while ((m = rx.exec(line)) !== null) {
                    if (m[0] === '') { rx.lastIndex++; continue; }
                    out += escapeHtml(line.slice(last, m.index)) + `<mark class="search-hit">${escapeHtml(m[0])}</mark>`;
                    last = m.index + m[0].length;
                }
                return out + escapeHtml(line.slice(last));
            };
            out.innerHTML = `<p class="mb-2" style="color:var(--text3)">${matches.length}${truncated ? '+' : ''} match${matches.length === 1 ? '' : 'es'} in ${files} file${files === 1 ? '' : 's'}${truncated ? ' — showing the first ones; narrow the search' : ''}</p>` +
                matches.map(m => `<div class="py-1.5" style="border-top:1px solid var(--border)">
                    <div style="color:var(--text3)">${escapeHtml(m.file === 'app.log' ? 'current log' : 'older log')} · line ${m.line}</div>
                    ${m.before.map(l => `<div class="whitespace-pre-wrap break-words" style="color:var(--text3)">${escapeHtml(l)}</div>`).join('')}
                    <div class="whitespace-pre-wrap break-words" style="color:var(--text)">${mark(m.text)}</div>
                    ${m.after.map(l => `<div class="whitespace-pre-wrap break-words" style="color:var(--text3)">${escapeHtml(l)}</div>`).join('')}
                </div>`).join('');
        }

        function downloadAllLogs() {
            if (!activeApp) return;
            triggerDownload(`${API}/apps/${activeAppId}/logs/download`, `${activeApp.slug || 'app'}-logs.txt`);
        }
