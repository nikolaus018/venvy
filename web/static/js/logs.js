// Logs tab: polling, filtering, follow/wrap, copy/download, ANSI colour rendering.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Logs ──────────────────────────────────────────────────────────────────
        let _logsInflight = false;
        // ── Log viewer state ─────────────────────────────────────────────────────
        const _LOG_EMPTY_HTML = '<span class="italic" style="color:var(--text3)">No log output yet. Start the application to see live logs.</span>';
        let _logRaw = '';           // last raw text received from the server
        let _logRenderedKey = '';   // raw+filter fingerprint of what is on screen
        let _logFilter = '';
        let _logFollow = true;      // auto-scroll to the tail as new lines arrive
        let _logWrap = true;

        function _stripAnsi(t) { return t.replace(/\x1b\[[0-9;]*[mA-Za-z]/g, ''); }

        function _logIsAtBottom(el) {
            return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }

        // Wrapping the needle in <mark> is done over text nodes only, so it can never
        // corrupt the <span> markup the ANSI renderer produced.
        function _markMatches(root, needle) {
            if (!needle) return 0;
            const lower = needle.toLowerCase();
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            const targets = [];
            let n;
            while ((n = walker.nextNode())) {
                if (n.nodeValue && n.nodeValue.toLowerCase().includes(lower)) targets.push(n);
            }
            let count = 0;
            for (const node of targets) {
                const frag = document.createDocumentFragment();
                const text = node.nodeValue;
                let i = 0;
                for (; ;) {
                    const j = text.toLowerCase().indexOf(lower, i);
                    if (j === -1) break;
                    if (j > i) frag.appendChild(document.createTextNode(text.slice(i, j)));
                    const mark = document.createElement('mark');
                    mark.textContent = text.slice(j, j + needle.length);
                    frag.appendChild(mark);
                    count++;
                    i = j + needle.length;
                }
                if (i < text.length) frag.appendChild(document.createTextNode(text.slice(i)));
                node.parentNode.replaceChild(frag, node);
            }
            return count;
        }

        function _renderLogs(force = false) {
            const el = document.getElementById('log-viewer');
            if (!el) return;

            const q = _logFilter.trim();
            const key = q + '\u0000' + _logRaw.length + '\u0000' + _logRaw.slice(-400);
            if (!force && key === _logRenderedKey) return;

            // Never yank the DOM out from under an in-progress text selection.
            const sel = window.getSelection();
            if (!force && sel && !sel.isCollapsed && el.contains(sel.anchorNode)) return;

            _logRenderedKey = key;
            const wasBottom = _logIsAtBottom(el);

            if (!_logRaw) {
                el.innerHTML = _LOG_EMPTY_HTML;
                _updateLogCount(0, 0);
                _updateLogJumpBtn();
                return;
            }

            let text = _logRaw;
            let shown = 0, total = 0;
            if (q) {
                const lines = _logRaw.split('\n');
                total = lines.length;
                const needle = q.toLowerCase();
                const kept = lines.filter(l => _stripAnsi(l).toLowerCase().includes(needle));
                shown = kept.length;
                text = kept.length ? kept.join('\n') : '';
            }

            if (q && !shown) {
                el.innerHTML = `<span class="italic" style="color:var(--text3)">No lines match &ldquo;${escapeHtml(q)}&rdquo;.</span>`;
                _updateLogCount(0, total);
                _updateLogJumpBtn();
                return;
            }

            el.innerHTML = window._ansiToHtml
                ? window._ansiToHtml(text)
                : escapeHtml(text);

            if (q) _markMatches(el, q);
            _updateLogCount(shown, total);

            if (_logFollow || wasBottom) el.scrollTop = el.scrollHeight;
            _updateLogJumpBtn();
        }

        function _updateLogCount(shown, total) {
            const el = document.getElementById('log-search-count');
            if (!el) return;
            el.textContent = _logFilter.trim() ? `${shown}/${total}` : '';
        }

        function _updateLogJumpBtn() {
            const el = document.getElementById('log-viewer');
            const btn = document.getElementById('log-jump-btn');
            if (!el || !btn) return;
            btn.classList.toggle('show', !_logIsAtBottom(el) && !!_logRaw);
        }

        function logJumpToLatest() {
            const el = document.getElementById('log-viewer');
            if (!el) return;
            el.scrollTop = el.scrollHeight;
            _logFollow = true;
            _syncLogToolButtons();
            _updateLogJumpBtn();
        }

        function toggleLogFollow() {
            _logFollow = !_logFollow;
            if (_logFollow) {
                const el = document.getElementById('log-viewer');
                if (el) el.scrollTop = el.scrollHeight;
            }
            _syncLogToolButtons();
            _updateLogJumpBtn();
            showToast(_logFollow ? 'Following new output.' : 'Paused — scroll freely.', 'info', 1400);
        }

        function toggleLogWrap() {
            _logWrap = !_logWrap;
            document.getElementById('log-viewer')?.classList.toggle('wrap-off', !_logWrap);
            try { localStorage.setItem('venvy-log-wrap', _logWrap ? '1' : '0'); } catch (e) { }
            _syncLogToolButtons();
        }

        function _syncLogToolButtons() {
            const f = document.getElementById('log-follow-btn');
            const w = document.getElementById('log-wrap-btn');
            if (f) { f.setAttribute('aria-pressed', String(_logFollow)); f.title = _logFollow ? 'Following new output — click to pause (F)' : 'Paused — click to follow (F)'; }
            if (w) w.setAttribute('aria-pressed', String(_logWrap));
        }

        function onLogSearchInput() {
            _logFilter = document.getElementById('log-search').value;
            _renderLogs(true);
        }

        function onLogSearchKey(e) {
            if (e.key === 'Escape') { e.stopPropagation(); clearLogSearch(); }
            if (e.key === 'Enter') { e.preventDefault(); logJumpToLatest(); }
        }

        function clearLogSearch() {
            const input = document.getElementById('log-search');
            if (!input) return;
            input.value = '';
            _logFilter = '';
            _renderLogs(true);
        }

        // Live indicator tells the truth about whether anything is streaming.
        function updateLogLiveIndicator() {
            const dot = document.getElementById('log-live-dot');
            const text = document.getElementById('log-live-text');
            const label = document.getElementById('log-live-label');
            if (!dot || !text) return;
            const m = statusMeta(activeApp?.status);
            if (activeApp?.status === 'running') {
                dot.style.background = 'var(--green)';
                dot.classList.add('pulse-dot');
                text.textContent = 'Live';
                if (label) label.title = 'The app is running — new output appears automatically.';
            } else if (m.crashed) {
                dot.style.background = 'var(--red)';
                dot.classList.remove('pulse-dot');
                text.textContent = 'Crashed';
                if (label) label.title = 'The process exited unexpectedly. These are its final logs.';
            } else {
                dot.style.background = 'var(--text3)';
                dot.classList.remove('pulse-dot');
                text.textContent = 'Not running';
                if (label) label.title = 'The app is stopped — this is the last output it produced.';
            }
        }

        async function loadLogs() {
            if (!activeAppId || _logsInflight) return;
            _logsInflight = true;
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/logs`);
                if (!res.ok) return;
                const data = await res.json();
                if (!data.content && !data.truncated) {
                    _logRaw = '';
                } else {
                    _logRaw = data.truncated
                        ? `[showing last ${data.total_lines > 0 ? data.total_lines : ''} lines (truncated) — clear logs to reset]\n\n` + data.content
                        : data.content;
                }
                document.getElementById('log-viewer').dataset.rawText = _logRaw;
                _renderLogs();
            } catch (e) { /* network error — silently skip poll tick */ }
            finally { _logsInflight = false; }
        }

        async function deleteLogs() {
            const ok = await confirmDialog({
                title: 'Clear logs?',
                sub: 'This cannot be undone',
                body: `The log file for "${activeApp?.name || 'this app'}" will be emptied. Output produced from now on will still be captured.`,
                confirmLabel: 'Clear logs',
                danger: true,
                icon: 'trash-2',
            });
            if (!ok) return;
            try {
                const res = await fetch(`${API}/apps/${activeAppId}/logs`, { method: 'DELETE' });
                if (res.ok) {
                    _logRaw = ''; _logRenderedKey = '';
                    _renderLogs(true);
                    showToast('Logs cleared.', 'success', 1500);
                } else {
                    showToast('Could not clear the logs.', 'error');
                }
            } catch (e) { showToast('Could not clear the logs: ' + e.message, 'error'); }
        }

        function _logsPlainText() {
            const el = document.getElementById('log-viewer');
            return _stripAnsi(_logRaw || el?.dataset.rawText || el?.innerText || '');
        }

        async function copyLogs() {
            const clean = _logsPlainText();
            if (!clean.trim()) { showToast('There is nothing to copy yet.', 'warn'); return; }
            try { await navigator.clipboard.writeText(clean); }
            catch (e) { const ta = document.createElement('textarea'); ta.value = clean; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
            const lines = clean.split('\n').length;
            showToast(`Copied ${lines.toLocaleString()} line${lines === 1 ? '' : 's'}.`, 'success', 2000);
        }

        function downloadLogs() {
            const clean = _logsPlainText();
            if (!clean.trim()) { showToast('There is nothing to download yet.', 'warn'); return; }
            const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
            const name = `${(activeApp?.name || 'app').replace(/[^\w.-]+/g, '_')}-${stamp}.log`;
            const blob = new Blob([clean], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = name; a.style.display = 'none';
            document.body.appendChild(a); a.click();
            setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
            showToast(`Saved ${name}`, 'success', 2500);
        }

        (function wireLogScroll() {
            const el = document.getElementById('log-viewer');
            if (!el) return;
            let ticking = false;
            el.addEventListener('scroll', () => {
                if (ticking) return;
                ticking = true;
                requestAnimationFrame(() => {
                    ticking = false;
                    const bottom = _logIsAtBottom(el);
                    // Scrolling away from the tail pauses follow so output stops
                    // yanking the viewport; scrolling back resumes it.
                    if (_logFollow && !bottom) { _logFollow = false; _syncLogToolButtons(); }
                    else if (!_logFollow && bottom) { _logFollow = true; _syncLogToolButtons(); }
                    _updateLogJumpBtn();
                });
            }, { passive: true });

            try {
                if (localStorage.getItem('venvy-log-wrap') === '0') {
                    _logWrap = false;
                    el.classList.add('wrap-off');
                }
            } catch (e) { }
            _syncLogToolButtons();
        })();

        // ── ANSI → HTML renderer for live logs ───────────────────────────────────
        // Converts ANSI escape sequences to styled <span> elements so the log
        // viewer shows colors, bold, dim etc. exactly as the app intended.
        // Only a subset of codes is needed in practice: SGR (colors + attributes).
        (function () {
            // ANSI 16-color palette (indices 0-15)
            const _ANSI16 = [
                '#1a1a1a', '#ff453a', '#30d158', '#ffd60a',
                '#ffffff', '#a1a1aa', '#71717a', '#f2f2f2',
                '#48484a', '#ff6961', '#34c759', '#ffcc00',
                'var(--text)', '#da8fff', '#8ee2ff', '#ffffff',
            ];
            function _ansiColor256(n) {
                if (n < 16) return _ANSI16[n];
                if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
                n -= 16;
                const b = n % 6, g = Math.floor(n / 6) % 6, r = Math.floor(n / 36);
                const c = v => v ? 55 + v * 40 : 0;
                return `rgb(${c(r)},${c(g)},${c(b)})`;
            }

            window._ansiToHtml = function (raw) {
                // State
                let bold = false, dim = false, italic = false, underline = false, blink = false;
                let fg = null, bg = null;
                let out = '';

                // Escape any HTML in the raw string first, then process ANSI codes.
                // We process char-by-char rebuilding spans.
                // Strategy: split on ESC sequences, then process each chunk.
                const RE = /\x1b\[([0-9;]*)m|\x1b\[[0-9;]*[A-HJKSTfhilmnsu]|\x1b[()][AB012]|\x1b[^[]/g;

                let last = 0;
                const flushText = (text) => {
                    if (!text) return;
                    // Build style string for current state
                    const styles = [];
                    if (fg) styles.push(`color:${fg}`);
                    if (bg) styles.push(`background:${bg}`);
                    if (bold) styles.push('font-weight:700');
                    if (dim) styles.push('opacity:0.55');
                    if (italic) styles.push('font-style:italic');
                    if (underline) styles.push('text-decoration:underline');
                    if (blink) styles.push('animation:_logBlink 1s step-end infinite');
                    const escaped = text
                        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    if (styles.length) {
                        out += `<span style="${styles.join(';')}">${escaped}</span>`;
                    } else {
                        out += escaped;
                    }
                };

                let m;
                while ((m = RE.exec(raw)) !== null) {
                    // Flush plain text before this escape
                    flushText(raw.slice(last, m.index));
                    last = m.index + m[0].length;

                    // Only process SGR sequences (ending in 'm')
                    if (!m[0].endsWith('m') || m[1] === undefined) continue;

                    const params = m[1] === '' ? [0] : m[1].split(';').map(Number);
                    let i = 0;
                    while (i < params.length) {
                        const p = params[i++];
                        if (p === 0) { bold = false; dim = false; italic = false; underline = false; blink = false; fg = null; bg = null; }
                        else if (p === 1) bold = true;
                        else if (p === 2) dim = true;
                        else if (p === 3) italic = true;
                        else if (p === 4) underline = true;
                        else if (p === 5 || p === 6) blink = true;
                        else if (p === 22) { bold = false; dim = false; }
                        else if (p === 23) italic = false;
                        else if (p === 24) underline = false;
                        else if (p === 25) blink = false;
                        else if (p === 39) fg = null;
                        else if (p === 49) bg = null;
                        else if (p >= 30 && p <= 37) fg = _ANSI16[p - 30];
                        else if (p >= 40 && p <= 47) bg = _ANSI16[p - 40];
                        else if (p >= 90 && p <= 97) fg = _ANSI16[p - 90 + 8];
                        else if (p >= 100 && p <= 107) bg = _ANSI16[p - 100 + 8];
                        else if ((p === 38 || p === 48) && params[i] === 5 && i + 1 < params.length) {
                            const col = _ansiColor256(params[i + 1]); i += 2;
                            if (p === 38) fg = col; else bg = col;
                        } else if ((p === 38 || p === 48) && params[i] === 2 && i + 3 < params.length) {
                            const col = `rgb(${params[i + 1]},${params[i + 2]},${params[i + 3]})`; i += 4;
                            if (p === 38) fg = col; else bg = col;
                        }
                    }
                }
                flushText(raw.slice(last));
                return out;
            };
        })();
