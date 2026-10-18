// Small helpers shared by the newer views (deploy, jobs, activity, notifications, tokens, charts):
// a JSON fetch, formatting, a modal builder and an SVG line chart.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.

        // fetch + JSON, never throws: { ok, status, data, error }
        async function apiJson(method, path, body) {
            try {
                const init = { method };
                if (body !== undefined) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(body); }
                const res = await fetch(`${API}${path}`, init);
                let data = null;
                try { data = await res.json(); } catch { }
                const error = res.ok ? null : (data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || `Request failed (${res.status})`;
                return { ok: res.ok, status: res.status, data, error };
            } catch (e) {
                return { ok: false, status: 0, data: null, error: 'Network error — is the server running?' };
            }
        }

        function fmtBytes(n) {
            if (n == null || isNaN(n)) return '—';
            const u = ['B', 'KB', 'MB', 'GB', 'TB'];
            let i = 0, v = Number(n);
            while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
            return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
        }

        function fmtWhen(iso) {
            if (!iso) return '—';
            const d = new Date(iso);
            if (isNaN(d)) return String(iso);
            return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        }

        function fmtAgo(iso) {
            if (!iso) return 'never';
            const s = (Date.now() - new Date(iso).getTime()) / 1000;
            if (isNaN(s)) return '—';
            if (s < 0) return 'in ' + fmtDuration(-s);
            if (s < 45) return 'just now';
            return fmtDuration(s) + ' ago';
        }

        function fmtDuration(sec) {
            sec = Math.round(sec);
            if (sec < 90) return `${sec}s`;
            if (sec < 5400) return `${Math.round(sec / 60)} min`;
            if (sec < 172800) return `${Math.round(sec / 3600)} h`;
            return `${Math.round(sec / 86400)} d`;
        }

        async function copyText(text, what = 'Copied') {
            try { await navigator.clipboard.writeText(text); showToast(`${what} to the clipboard`, 'success', 1800); }
            catch {
                const ta = document.createElement('textarea');
                ta.value = text; document.body.appendChild(ta); ta.select();
                try { document.execCommand('copy'); showToast(`${what} to the clipboard`, 'success', 1800); }
                catch { showToast('Could not copy — select the text and copy it by hand.', 'warn'); }
                ta.remove();
            }
        }

        // A coloured pill for a status word (ok, failed, timeout, running, rolled-back, ...)
        function statusPill(status) {
            const map = {
                ok: ['var(--green)', 'rgba(16,185,129,0.12)'], healthy: ['var(--green)', 'rgba(16,185,129,0.12)'],
                running: ['var(--amber)', 'rgba(245,158,11,0.12)'], unknown: ['var(--text3)', 'var(--surface3)'],
                failed: ['var(--red)', 'rgba(239,68,68,0.12)'], timeout: ['var(--red)', 'rgba(239,68,68,0.12)'],
                unhealthy: ['var(--red)', 'rgba(239,68,68,0.12)'], 'rolled-back': ['var(--amber)', 'rgba(245,158,11,0.12)'],
            };
            const [fg, bg] = map[status] || ['var(--text3)', 'var(--surface3)'];
            return `<span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide whitespace-nowrap" style="color:${fg};background:${bg}">${escapeHtml(status || '—')}</span>`;
        }

        // A modal built on demand, in the same shell as the others. Returns the body element.
        function buildModal(id, title, { width = 'max-w-2xl', zIndex = 80 } = {}) {
            let el = document.getElementById(id);
            if (!el) {
                el = document.createElement('div');
                el.id = id;
                el.setAttribute('role', 'dialog'); el.setAttribute('aria-modal', 'true'); el.setAttribute('aria-labelledby', `${id}-title`);
                el.className = `fixed inset-0 z-[${zIndex}] modal-overlay hidden flex items-end md:items-center justify-center p-4 md:p-0`;
                el.innerHTML = `
                    <div class="modal-panel p-5 w-full ${width} shadow-lg flex flex-col" style="max-height:88vh">
                        <div class="flex justify-between items-center mb-4 shrink-0">
                            <h2 class="text-lg font-bold" id="${id}-title" style="color:var(--text)"></h2>
                            <button type="button" aria-label="Close" onclick="closeModal('${id}')" class="p-1.5 rounded-lg" style="color:var(--text3)">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M6 18L18 6M6 6l12 12"/></svg>
                            </button>
                        </div>
                        <div id="${id}-body" class="flex-1 overflow-y-auto min-h-0"></div>
                    </div>`;
                document.body.appendChild(el);
            }
            document.getElementById(`${id}-title`).textContent = title;
            return document.getElementById(`${id}-body`);
        }

        function fieldHtml(id, label, { type = 'text', value = '', placeholder = '', hint = '', attrs = '' } = {}) {
            return `<div>
                <label class="block section-label mb-1.5" for="${id}">${escapeHtml(label)}</label>
                <input id="${id}" type="${type}" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false"
                    class="inp w-full px-3.5 py-2.5 text-sm ${type === 'password' || attrs.includes('mono') ? 'mono' : ''}" ${attrs}>
                ${hint ? `<p class="text-xs mt-1" style="color:var(--text3)">${hint}</p>` : ''}
            </div>`;
        }

        function selectHtml(id, label, options, selected = '', attrs = '') {
            return `<div>
                <label class="block section-label mb-1.5" for="${id}">${escapeHtml(label)}</label>
                <select id="${id}" class="inp w-full px-3.5 py-2.5 text-sm" ${attrs}>
                    ${options.map(([v, l]) => `<option value="${escapeHtml(v)}" ${String(v) === String(selected) ? 'selected' : ''}>${escapeHtml(l)}</option>`).join('')}
                </select>
            </div>`;
        }

        const $id = id => document.getElementById(id);
        const _val = id => ($id(id)?.value ?? '').trim();

        // ── Line chart ───────────────────────────────────────────────────────────
        // points: [{ts, <key>: number}], returns an SVG string. Auto-scales to
        // the data (or to opts.max); marks the latest value.
        function chartSvg(points, key, { height = 90, color = 'var(--accent)', max = null, fmt = v => String(Math.round(v * 10) / 10), min = 0 } = {}) {
            const W = 600, H = height, padL = 4, padR = 4, padT = 8, padB = 14;
            if (!points || points.length < 2) {
                return `<div class="text-xs flex items-center justify-center" style="height:${H}px;color:var(--text3)">Collecting data — a point is recorded every minute.</div>`;
            }
            const vals = points.map(p => Number(p[key]) || 0);
            const hi = max != null ? max : Math.max(...vals, 1e-9) * 1.15;
            const lo = min;
            const t0 = new Date(points[0].ts).getTime(), t1 = new Date(points[points.length - 1].ts).getTime();
            const span = Math.max(1, t1 - t0);
            const x = p => padL + ((new Date(p.ts).getTime() - t0) / span) * (W - padL - padR);
            const y = v => padT + (1 - (Math.min(Math.max(v, lo), hi) - lo) / Math.max(1e-9, hi - lo)) * (H - padT - padB);
            const pts = points.map((p, i) => `${x(p).toFixed(1)},${y(vals[i]).toFixed(1)}`);
            const area = `${padL},${H - padB} ${pts.join(' ')} ${(W - padR)},${H - padB}`;
            const last = vals[vals.length - 1], peak = Math.max(...vals);
            return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="w-full" style="height:${H}px;display:block" role="img" aria-label="${escapeHtml(key)} over time">
                <line x1="${padL}" y1="${y(0)}" x2="${W - padR}" y2="${y(0)}" stroke="var(--border2)" stroke-width="1" vector-effect="non-scaling-stroke"/>
                <polygon points="${area}" fill="${color}" opacity="0.12"/>
                <polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
                <circle cx="${x(points[points.length - 1]).toFixed(1)}" cy="${y(last).toFixed(1)}" r="2.6" fill="${color}"/>
                <text x="${padL}" y="${H - 2}" font-size="10" fill="var(--text3)">${escapeHtml(fmtWhen(points[0].ts))}</text>
                <text x="${W - padR}" y="${H - 2}" font-size="10" fill="var(--text3)" text-anchor="end">${escapeHtml(fmtWhen(points[points.length - 1].ts))}</text>
            </svg>
            <div class="flex justify-between text-[11px] mono mt-0.5" style="color:var(--text3)"><span>peak ${escapeHtml(fmt(peak))}</span><span style="color:var(--text2)">now ${escapeHtml(fmt(last))}</span></div>`;
        }
