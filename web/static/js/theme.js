// Theme and motion preferences, applied to the page, Ace and xterm.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ══════════════════════════════════════════════════════════════════════
        //  THEME
        //  Preference is one of light | dark | system and lives in localStorage.
        //  Everything downstream (CSS tokens, Ace, xterm, the PWA status bar)
        //  is re-pointed whenever it changes.
        // ══════════════════════════════════════════════════════════════════════
        const THEME_KEY = 'venvy-theme';
        const MOTION_KEY = 'venvy-motion';

        function getThemePref() {
            try { return localStorage.getItem(THEME_KEY) || 'system'; } catch (e) { return 'system'; }
        }

        function resolvedTheme() {
            const pref = getThemePref();
            if (pref === 'light' || pref === 'dark') return pref;
            return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
        }

        function xtermTheme() {
            return resolvedTheme() === 'light'
                ? {
                    background: '#ffffff', foreground: '#1e293b',
                    cursor: '#2563eb', cursorAccent: '#ffffff',
                    selectionBackground: 'rgba(37, 99, 235, 0.22)', selectionForeground: '#0f172a',
                    black: '#1e293b', red: '#dc2626', green: '#047857', yellow: '#b45309',
                    blue: '#2563eb', magenta: '#7e22ce', cyan: '#0e7490', white: '#64748b',
                    brightBlack: '#94a3b8', brightRed: '#ef4444', brightGreen: '#059669',
                    brightYellow: '#d97706', brightBlue: '#3b82f6', brightMagenta: '#9333ea',
                    brightCyan: '#0891b2', brightWhite: '#0f172a',
                }
                : {
                    background: '#111826', foreground: '#e2e8f0',
                    cursor: '#60a5fa', cursorAccent: '#111826',
                    selectionBackground: 'rgba(96, 165, 250, 0.3)', selectionForeground: '#ffffff',
                    black: '#1e293b', red: '#f87171', green: '#4ade80', yellow: '#fbbf24',
                    blue: '#60a5fa', magenta: '#c084fc', cyan: '#22d3ee', white: '#cbd5e1',
                    brightBlack: '#475569', brightRed: '#fca5a5', brightGreen: '#86efac',
                    brightYellow: '#fde68a', brightBlue: '#93c5fd', brightMagenta: '#d8b4fe',
                    brightCyan: '#67e8f9', brightWhite: '#ffffff',
                };
        }

        function applyTheme() {
            const t = resolvedTheme();
            document.documentElement.setAttribute('data-theme', t);

            // Keep the PWA status bar / browser chrome in step with the page.
            const color = t === 'light' ? '#ffffff' : '#0c111c';
            document.querySelectorAll('meta[name="theme-color"]').forEach(m => m.remove());
            const meta = document.createElement('meta');
            meta.name = 'theme-color';
            meta.content = color;
            document.head.appendChild(meta);

            if (aceEditor) {
                aceEditor.setTheme(t === 'light' ? 'ace/theme/chrome' : 'ace/theme/one_dark');
                aceEditor.container.style.background = 'var(--surface)';
            }
            if (window.term) {
                try { window.term.options.theme = xtermTheme(); } catch (e) { }
            }
            syncAppearanceControls();
        }

        function setTheme(pref) {
            try { localStorage.setItem(THEME_KEY, pref); } catch (e) { }
            applyTheme();
            announce(`Theme set to ${pref}`);
        }

        function toggleTheme() {
            setTheme(resolvedTheme() === 'light' ? 'dark' : 'light');
        }

        function reduceMotionOn() {
            try { return localStorage.getItem(MOTION_KEY) === 'reduce'; } catch (e) { return false; }
        }

        function toggleReduceMotion() {
            const next = !reduceMotionOn();
            try { localStorage.setItem(MOTION_KEY, next ? 'reduce' : 'full'); } catch (e) { }
            document.documentElement.toggleAttribute('data-motion', false);
            if (next) document.documentElement.setAttribute('data-motion', 'reduce');
            else document.documentElement.removeAttribute('data-motion');
            syncAppearanceControls();
        }

        function syncAppearanceControls() {
            const pref = getThemePref();
            document.querySelectorAll('[data-theme-opt]').forEach(b => {
                b.setAttribute('aria-checked', String(b.dataset.themeOpt === pref));
            });
            const t = document.getElementById('motion-toggle');
            if (t) {
                const on = reduceMotionOn();
                t.setAttribute('aria-checked', String(on));
                t.classList.toggle('on', on);
            }
        }

        // Follow the OS when the preference is "system".
        try {
            window.matchMedia('(prefers-color-scheme: light)')
                .addEventListener('change', () => { if (getThemePref() === 'system') applyTheme(); });
        } catch (e) { }
