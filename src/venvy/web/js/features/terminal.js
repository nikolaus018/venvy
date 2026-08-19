/**
 * The xterm.js terminal, its WebSocket transport and the mobile key bar.
 *
 * Reconnects with a bounded backoff so a brief network blip does not drop the
 * session, but a genuinely unreachable backend stops retrying.
 */

import { state } from "../core/state.js";

export let _termResizeObserver = null;

export let _termVpListener = null;

export let _termReconnectTimer = null;

export let _termAppId = null;

export let _termAttempt = 0;

export const _TERM_MAX_ATTEMPTS = 6;

export function _termSetStatus(state) {
    // state: 'connected' | 'connecting' | 'disconnected'
    const dot = document.getElementById('term-status-dot');
    if (!dot) return;
    // Use CSS variable values that match our design tokens
    const map = {
        connected: { bg: 'var(--green)', shadow: '0 0 6px rgba(52,211,153,0.6)', title: 'Connected' },
        connecting: { bg: 'var(--yellow)', shadow: '0 0 6px rgba(245,158,11,0.5)', title: 'Connecting…' },
        disconnected: { bg: 'var(--surface4)', shadow: 'none', title: 'Disconnected' },
    };
    const s = map[state] || map.disconnected;
    dot.style.background = s.bg;
    dot.style.boxShadow = s.shadow;
    dot.title = s.title;
}

export function _termSetLabel(appName) {
    const lbl = document.getElementById('term-app-label');
    if (lbl) lbl.textContent = appName ? `~/${appName}` : '~/app';
}

export function termSend(data) {
    if (state.termSocket && state.termSocket.readyState === WebSocket.OPEN) state.termSocket.send(data);
    if (window.term) window.term.focus();
}

export let _mobMod = null;

export let _mobModTimer = null;

export function _mobHaptic(ms = 8) {
    try { if (navigator.vibrate) navigator.vibrate(ms); } catch { }
}

export function termModToggle(mod) {
    clearTimeout(_mobModTimer);
    if (_mobMod === mod) {
        // Second tap = deactivate
        _mobMod = null;
    } else {
        _mobMod = mod;
        // Auto-clear after 3 seconds if no key pressed
        _mobModTimer = setTimeout(() => {
            _mobMod = null;
            _updateModBtns();
        }, 3000);
    }
    _updateModBtns();
    _mobHaptic(12);
    if (window.term) window.term.focus();
}

export function _updateModBtns() {
    const ctrlBtn = document.getElementById('mob-ctrl-btn');
    const altBtn = document.getElementById('mob-alt-btn');
    if (ctrlBtn) ctrlBtn.classList.toggle('mod-active', _mobMod === 'ctrl');
    if (altBtn) altBtn.classList.toggle('mod-active', _mobMod === 'alt');
}

export function termMobKey(data) {
    _mobHaptic(8);
    let toSend = data;

    if (_mobMod && data.length === 1 && !data.startsWith('\x1b')) {
        if (_mobMod === 'ctrl') {
            const code = data.toUpperCase().charCodeAt(0);
            if (code >= 64 && code <= 95) {
                toSend = String.fromCharCode(code - 64);
            }
        } else if (_mobMod === 'alt') {
            toSend = '\x1b' + data;
        }
        // Clear modifier after use
        clearTimeout(_mobModTimer);
        _mobMod = null;
        _updateModBtns();
    }

    if (state.termSocket && state.termSocket.readyState === WebSocket.OPEN) state.termSocket.send(toSend);
    if (window.term) window.term.focus();
}

export function termMobToggleMore() {
    const row = document.getElementById('term-mob-more-row');
    const btn = document.getElementById('mob-more-btn');
    if (!row) return;
    const isOpen = !row.classList.contains('hidden');
    row.classList.toggle('hidden', isOpen);
    row.classList.toggle('flex', !isOpen);
    if (btn) btn.classList.toggle('more-open', !isOpen);
    _mobHaptic(6);
    // Re-fit terminal since height changed
    requestAnimationFrame(_termFit);
}

export let _termFontSize = null;

export function termFontResize(delta) {
    if (!window.term) return;
    _termFontSize = (_termFontSize ?? window.term.options.fontSize) + delta;
    _termFontSize = Math.max(9, Math.min(24, _termFontSize));
    window.term.options.fontSize = _termFontSize;
    requestAnimationFrame(_termFit);
    _mobHaptic(6);
}

window.termMobKey = termMobKey;

window.termModToggle = termModToggle;

window.termMobToggleMore = termMobToggleMore;

window.termFontResize = termFontResize;

export function _termClear() {
    if (window.term) {
        window.term.clear();
        window.term.focus();
    }
}

export function _termReconnect() {
    if (_termAppId) initTerminal(_termAppId);
}

export function _termFit() {
    if (!window.fitAddon || !window.term) return;
    try {
        window.fitAddon.fit();
        if (state.termSocket && state.termSocket.readyState === WebSocket.OPEN) {
            state.termSocket.send('\x00' + JSON.stringify({ cols: window.term.cols, rows: window.term.rows }));
        }
    } catch { }
}

export function _termDestroy() {
    // Kill pending reconnect
    if (_termReconnectTimer) { clearTimeout(_termReconnectTimer); _termReconnectTimer = null; }

    // Close WebSocket cleanly
    if (state.termSocket) {
        try { state.termSocket.onclose = null; state.termSocket.close(); } catch { }
        state.termSocket = null;
    }

    // Disconnect resize observer
    if (_termResizeObserver) { _termResizeObserver.disconnect(); _termResizeObserver = null; }

    // Remove visual-viewport listener and reset any keyboard-induced styles
    if (_termVpListener && window.visualViewport) {
        window.visualViewport.removeEventListener('resize', _termVpListener);
        window.visualViewport.removeEventListener('scroll', _termVpListener);
        _termVpListener = null;
    }
    // Reset any styles set by the keyboard handler
    const _tabEl = document.getElementById('tab-terminal');
    if (_tabEl) { _tabEl.style.height = ''; _tabEl.style.position = ''; _tabEl.style.top = ''; }

    // Dispose xterm instance
    if (window.term) { try { window.term.dispose(); } catch { } window.term = null; }
    if (window.fitAddon) { window.fitAddon = null; }

    _termSetStatus('disconnected');
}

export function initTerminal(appId) {
    _termDestroy();
    _termAppId = appId;
    _termAttempt = 0;

    // Reset mobile modifier and secondary row on each new terminal session
    clearTimeout(_mobModTimer);
    _mobMod = null;
    _termFontSize = null;
    _updateModBtns();
    const moreRow = document.getElementById('term-mob-more-row');
    const moreBtn = document.getElementById('mob-more-btn');
    if (moreRow) { moreRow.classList.add('hidden'); moreRow.classList.remove('flex'); }
    if (moreBtn) moreBtn.classList.remove('more-open');

    const appName = state.activeApp ? state.activeApp.name : '';
    _termSetLabel(appName);
    _termSetStatus('connecting');

    const container = document.getElementById('terminal-container');
    const _tabEl = document.getElementById('tab-terminal');
    container.innerHTML = '';

    const isMobile = window.innerWidth < 768;

    // ── Create xterm instance ────────────────────────────────────────────
    window.term = new Terminal({
        theme: {
            background: '#111826',  /* matches --surface token */
            foreground: '#e2e8f0',
            cursor: '#60a5fa',
            cursorAccent: '#111826',  /* matches --surface token */
            selectionBackground: 'rgba(96, 165, 250, 0.3)',
            selectionForeground: '#ffffff',
            black: '#1e293b',
            red: '#f87171',
            green: '#4ade80',
            yellow: '#fbbf24',
            blue: '#60a5fa',
            magenta: '#c084fc',
            cyan: '#22d3ee',
            white: '#cbd5e1',
            brightBlack: '#475569',
            brightRed: '#fca5a5',
            brightGreen: '#86efac',
            brightYellow: '#fde68a',
            brightBlue: '#93c5fd',
            brightMagenta: '#d8b4fe',
            brightCyan: '#67e8f9',
            brightWhite: '#ffffff',
        },
        fontFamily: '"JetBrains Mono", "Cascadia Code", Menlo, Monaco, "Courier New", monospace',
        fontSize: isMobile ? 12 : 13,
        lineHeight: 1.35,
        letterSpacing: 0,
        // On mobile: no blinking cursor (saves battery, avoids touch conflicts)
        // and no copyOnSelect (text selection is rare/broken on mobile touch)
        cursorBlink: !isMobile,
        cursorStyle: 'bar',
        convertEol: true,
        scrollback: 8000,
        allowTransparency: false,
        copyOnSelect: !isMobile,
        rightClickSelectsWord: !isMobile,
    });

    window.fitAddon = new FitAddon.FitAddon();
    window.term.loadAddon(window.fitAddon);
    window.term.open(container);

    // Nudge padding via the xterm screen element
    try {
        const screen = container.querySelector('.xterm-screen');
        if (screen) screen.style.padding = '8px 10px';
        const vp = container.querySelector('.xterm-viewport');
        if (vp) vp.style.overflowY = 'scroll';
        // On mobile, the xterm canvas steals all touch events which prevents
        // scrolling and makes the quick-key bar hard to reach. We allow the
        // terminal to scroll naturally by not blocking touch on the viewport,
        // but we DO let the canvas intercept touches for text input focus.
        if (isMobile && vp) {
            vp.style.touchAction = 'pan-y';
            vp.style.overscrollBehavior = 'contain';
        }
    } catch { }

    // Auto-copy to clipboard whenever the user finishes a selection.
    // This makes Ctrl+C unnecessary for copying — just select and it's copied.
    window.term.onSelectionChange(() => {
        const sel = window.term.getSelection();
        if (sel) {
            navigator.clipboard.writeText(sel).catch(() => {
                // Fallback for browsers that block clipboard without gesture
                const ta = document.createElement('textarea');
                ta.value = sel;
                ta.style.position = 'fixed'; ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select(); document.execCommand('copy');
                document.body.removeChild(ta);
            });
        }
    });

    // Initial fit — tab is visible at this point (switchTab() called before us)
    // Initial fit
    requestAnimationFrame(() => {
        try {
            if (window.fitAddon) window.fitAddon.fit();
        } catch { }
    });

    // ── Resize observer — refit whenever the container changes size ──────
    _termResizeObserver = new ResizeObserver(() => {
        if (container.clientWidth > 0 && container.clientHeight > 0) _termFit();
    });
    _termResizeObserver.observe(container);

    // ── Visual-viewport listener — shrinks terminal when soft keyboard appears ─
    // On mobile, when the software keyboard opens, window.innerHeight stays
    // the same but visualViewport.height shrinks. We use this to explicitly
    // set the tab height so the flexbox layout clamps the xterm canvas.
    if (window.visualViewport) {
        _termVpListener = () => {
            if (window.term && !document.getElementById('tab-terminal').classList.contains('hidden')) {
                _termFit();
            }
        };
        window.visualViewport.addEventListener('resize', _termVpListener);
        window.visualViewport.addEventListener('scroll', _termVpListener);
    }

    // ── Register onData ONCE — never inside _connect() to avoid duplicate listeners ──
    window.term.onData(data => {
        if (state.termSocket && state.termSocket.readyState === WebSocket.OPEN) state.termSocket.send(data);
    });

    // ── WebSocket connect (with exponential-backoff retry) ───────────────
    const _proto = window.location.protocol === 'https:' ? 'wss' : 'ws';

    function _connect() {
        // Bail if the user navigated away from this app
        if (_termAppId !== state.activeAppId) return;

        _termSetStatus('connecting');
        const wsUrl = `${_proto}://${window.location.host}/api/apps/${_termAppId}/terminal`;
        state.termSocket = new WebSocket(wsUrl);

        state.termSocket.onopen = () => {
            _termAttempt = 0;
            _termSetStatus('connected');
            // Send current dimensions immediately so PTY initialises at the right size
            setTimeout(() => { _termFit(); window.term && window.term.focus(); }, 60);
        };

        state.termSocket.onmessage = (ev) => {
            if (!window.term) return;
            window.term.write(ev.data);
        };

        state.termSocket.onclose = () => {
            _termSetStatus('disconnected');

            // Don't retry if we've been destroyed or switched apps
            if (_termAppId !== state.activeAppId || !window.term) return;

            if (_termAttempt < _TERM_MAX_ATTEMPTS) {
                const delay = Math.min(800 * Math.pow(2, _termAttempt), 15000);
                _termAttempt++;
                if (window.term) {
                    window.term.write(
                        `\r\n\x1b[38;5;240m─── reconnecting in ${(delay / 1000).toFixed(1)}s (${_termAttempt}/${_TERM_MAX_ATTEMPTS}) ───\x1b[0m\r\n`
                    );
                }
                _termSetStatus('connecting');
                _termReconnectTimer = setTimeout(_connect, delay);
            } else {
                if (window.term) {
                    window.term.write(
                        '\r\n\x1b[31m─── connection lost — click ↺ to retry ───\x1b[0m\r\n'
                    );
                }
            }
        };

        state.termSocket.onerror = () => {
            // onclose fires right after onerror, so just update status here
            _termSetStatus('disconnected');
        };
    }

    // Defer first connect until fitAddon has had one rAF to calculate dimensions
    requestAnimationFrame(_connect);
}
