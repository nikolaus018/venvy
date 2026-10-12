// Command palette, the shortcuts reference and global keyboard shortcuts.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ══════════════════════════════════════════════════════════════════════
        //  COMMAND PALETTE
        // ══════════════════════════════════════════════════════════════════════
        let _paletteItems = [];
        let _paletteIndex = 0;

        function _paletteCommands() {
            const cmds = [];
            const running = activeApp?.status === 'running';

            // Read-only accounts are not offered what the server would refuse.
            const canOperate = currentUser?.role !== 'viewer';

            if (activeApp) {
                if (canOperate && !running) cmds.push({ icon: 'play', label: 'Start application', hint: activeApp.name, run: () => controlApp('start') });
                if (canOperate && running) cmds.push({ icon: 'refresh-cw', label: 'Restart application', hint: activeApp.name, run: () => controlApp('restart') });
                if (canOperate && running) cmds.push({ icon: 'square', label: 'Stop application', hint: activeApp.name, run: () => controlApp('stop') });
                TABS.forEach((t, i) => {
                    if (!tabAllowed(t)) return;
                    cmds.push({
                        icon: 'chevron-right',
                        label: 'Go to ' + t.charAt(0).toUpperCase() + t.slice(1),
                        hint: String(i + 1),
                        run: () => switchTab(t),
                    });
                });
            }
            if (canOperate) cmds.push({ icon: 'plus', label: 'New application', run: () => openModal('create-app-modal') });
            cmds.push({ icon: 'monitor', label: 'System overview', run: () => openSystemView(true) });
            if (currentUser?.role === 'admin') {
                cmds.push({ icon: 'users', label: 'Manage users', run: () => openUsersModal() });
            }
            cmds.push({ icon: 'lock', label: 'Change password', run: () => openChangePwdModal() });
            cmds.push({
                icon: resolvedTheme() === 'light' ? 'moon' : 'sun',
                label: `Switch to ${resolvedTheme() === 'light' ? 'dark' : 'light'} theme`,
                run: () => toggleTheme(),
            });
            cmds.push({ icon: 'command', label: 'Keyboard shortcuts', hint: '?', run: () => openShortcutsModal() });
            cmds.push({ icon: 'log-out', label: 'Sign out', run: () => logout() });
            return cmds;
        }

        function _buildPaletteItems(query) {
            const q = query.trim().toLowerCase();
            const items = [];

            _cachedApps.forEach(app => {
                const m = statusMeta(app.status);
                items.push({
                    kind: 'app',
                    icon: null,
                    dot: m,
                    label: app.name,
                    hint: m.label,
                    run: () => selectApp(app.id, { force: true }),
                });
            });
            _paletteCommands().forEach(c => items.push({ kind: 'cmd', ...c }));

            if (!q) return items;
            // Subsequence match so "stpp" still finds "Stop application".
            const score = (text) => {
                const t = text.toLowerCase();
                const idx = t.indexOf(q);
                if (idx === 0) return 0;
                if (idx > 0) return 1;
                let i = 0;
                for (const ch of t) { if (ch === q[i]) i++; if (i === q.length) return 2; }
                return -1;
            };
            return items
                .map(it => ({ it, s: score(it.label) }))
                .filter(x => x.s >= 0)
                .sort((a, b) => a.s - b.s)
                .map(x => x.it);
        }

        function renderPalette() {
            const wrap = document.getElementById('palette-results');
            if (!_paletteItems.length) {
                wrap.innerHTML = '<div class="px-4 py-8 text-center text-sm" style="color:var(--text3)">No matches</div>';
                return;
            }
            wrap.innerHTML = _paletteItems.map((it, i) => {
                const lead = it.kind === 'app'
                    ? `<span class="w-2 h-2 rounded-full shrink-0" style="background:${it.dot.dot}"></span>`
                    : `<i data-feather="${it.icon || 'chevron-right'}" class="w-3.5 h-3.5 shrink-0"></i>`;
                return `<div class="palette-item" role="option" id="palette-opt-${i}" data-i="${i}"
                             data-active="${i === _paletteIndex ? 1 : 0}"
                             aria-selected="${i === _paletteIndex}"
                             onmousemove="setPaletteIndex(${i})" onclick="runPaletteItem(${i})">
                    ${lead}
                    <span class="flex-1 min-w-0 truncate text-sm font-medium">${escapeHtml(it.label)}</span>
                    ${it.hint ? `<span class="text-[10.5px] mono shrink-0" style="color:var(--text3)">${escapeHtml(it.hint)}</span>` : ''}
                </div>`;
            }).join('');
            _featherReplace();
            document.getElementById('palette-input')
                ?.setAttribute('aria-activedescendant', `palette-opt-${_paletteIndex}`);
            _scrollPaletteIntoView();
        }

        function setPaletteIndex(i) {
            if (i === _paletteIndex) return;
            _paletteIndex = i;
            document.querySelectorAll('#palette-results .palette-item').forEach(el => {
                const active = Number(el.dataset.i) === i;
                el.dataset.active = active ? '1' : '0';
                el.setAttribute('aria-selected', String(active));
            });
            document.getElementById('palette-input')?.setAttribute('aria-activedescendant', `palette-opt-${i}`);
        }

        function _scrollPaletteIntoView() {
            document.querySelector(`#palette-results .palette-item[data-i="${_paletteIndex}"]`)
                ?.scrollIntoView({ block: 'nearest' });
        }

        function runPaletteItem(i) {
            const it = _paletteItems[i];
            closePalette();
            if (it && typeof it.run === 'function') setTimeout(() => it.run(), 10);
        }

        function openPalette() {
            if (!currentUser) return;
            const input = document.getElementById('palette-input');
            input.value = '';
            _paletteItems = _buildPaletteItems('');
            _paletteIndex = 0;
            renderPalette();
            openModal('command-palette');
            setTimeout(() => input.focus(), 50);
        }

        function closePalette() { closeModal('command-palette'); }

        document.getElementById('palette-input').addEventListener('input', (e) => {
            _paletteItems = _buildPaletteItems(e.target.value);
            _paletteIndex = 0;
            renderPalette();
        });

        document.getElementById('palette-input').addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setPaletteIndex(Math.min(_paletteIndex + 1, _paletteItems.length - 1));
                _scrollPaletteIntoView();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setPaletteIndex(Math.max(_paletteIndex - 1, 0));
                _scrollPaletteIntoView();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                runPaletteItem(_paletteIndex);
            }
        });

        // ══════════════════════════════════════════════════════════════════════
        //  SHORTCUTS REFERENCE
        // ══════════════════════════════════════════════════════════════════════
        const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
        const MOD = IS_MAC ? '⌘' : 'Ctrl';

        const SHORTCUT_GROUPS = [
            {
                title: 'Global', items: [
                    [[MOD, 'K'], 'Open the command palette'],
                    [['?'], 'Show this list'],
                    [['/'], 'Filter applications (or log lines)'],
                    [['Esc'], 'Close dialog · clear filter · dismiss toasts'],
                ]
            },
            {
                title: 'Navigation', items: [
                    [['1'], 'Terminal'],
                    [['2'], 'Files'],
                    [['3'], 'Logs'],
                    [['4'], 'Packages'],
                    [['5'], 'Settings'],
                    [['['], 'Previous application'],
                    [[']'], 'Next application'],
                    [['←', '→'], 'Move between tabs (when a tab is focused)'],
                ]
            },
            {
                title: 'Editor', items: [
                    [[MOD, 'S'], 'Save the open file'],
                    [[MOD, 'F'], 'Find in the open file'],
                ]
            },
            {
                title: 'Logs', items: [
                    [['F'], 'Follow / pause new output'],
                    [['W'], 'Toggle line wrapping'],
                ]
            },
        ];

        function openShortcutsModal() {
            document.getElementById('shortcuts-body').innerHTML = SHORTCUT_GROUPS.map(g => `
                <div class="mb-5 last:mb-0">
                    <div class="section-label mb-2.5">${g.title}</div>
                    <div class="space-y-1.5">
                        ${g.items.map(([keys, desc]) => `
                            <div class="flex items-center gap-3 text-sm">
                                <span class="flex items-center gap-1 shrink-0" style="min-width:92px">
                                    ${keys.map(k => `<span class="kbd">${escapeHtml(k)}</span>`).join('')}
                                </span>
                                <span style="color:var(--text2)">${escapeHtml(desc)}</span>
                            </div>`).join('')}
                    </div>
                </div>`).join('');
            openModal('shortcuts-modal');
            _featherReplace();
        }

        // ══════════════════════════════════════════════════════════════════════
        //  GLOBAL SHORTCUTS
        // ══════════════════════════════════════════════════════════════════════
        // A shortcut must never fire while the user is composing text — that
        // includes the xterm helper textarea, which is a real focusable input.
        function isTypingTarget(el) {
            if (!el) return false;
            if (el.isContentEditable) return true;
            const tag = el.tagName;
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
            if (el.closest?.('.ace_editor, .xterm')) return true;
            return false;
        }

        document.addEventListener('keydown', (e) => {
            const mod = e.metaKey || e.ctrlKey;

            // Command palette works from anywhere, including the terminal.
            if (mod && (e.key === 'k' || e.key === 'K')) {
                e.preventDefault();
                if (document.getElementById('command-palette').classList.contains('hidden')) openPalette();
                else closePalette();
                return;
            }

            if (isTypingTarget(e.target) || _openModalIds.length > 0) return;
            if (e.altKey || mod) return;
            if (!currentUser) return;

            switch (e.key) {
                case '?':
                    e.preventDefault(); openShortcutsModal(); return;
                case '/':
                    e.preventDefault();
                    if (currentTab === 'logs' && activeAppId) document.getElementById('log-search')?.focus();
                    else {
                        if (window.innerWidth < 768) openSidebar();
                        document.getElementById('app-search')?.focus();
                    }
                    return;
                case '1': case '2': case '3': case '4': case '5':
                    if (!activeAppId) return;
                    e.preventDefault(); switchTab(TABS[Number(e.key) - 1]); return;
                case '[': case ']':
                    e.preventDefault(); cycleApp(e.key === ']' ? 1 : -1); return;
                case 'f': case 'F':
                    if (currentTab === 'logs' && activeAppId) { e.preventDefault(); toggleLogFollow(); }
                    return;
                case 'w': case 'W':
                    if (currentTab === 'logs' && activeAppId) { e.preventDefault(); toggleLogWrap(); }
                    return;
            }
        });

        function cycleApp(delta) {
            if (!_cachedApps.length) return;
            const i = _cachedApps.findIndex(a => a.id === activeAppId);
            const next = _cachedApps[(i + delta + _cachedApps.length) % _cachedApps.length];
            if (next && next.id !== activeAppId) selectApp(next.id, { tab: currentTab });
        }
