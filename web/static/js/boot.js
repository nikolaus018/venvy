// Boot: first auth check, sidebar swipe gesture, service worker registration, PWA install prompt.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ══════════════════════════════════════════════════════════════════════
        //  BOOT
        // ══════════════════════════════════════════════════════════════════════
        applyTheme();
        syncAppearanceControls();
        checkAuth();
        _featherReplace();

        // ── Swipe Gesture ─────────────────────────────────────────────────────────
        let touchStartX = 0, touchCurrentX = 0, isSwipingSidebar = false, hasMoved = false;
        const sidebar = document.getElementById('main-sidebar');
        const overlay = document.getElementById('sidebar-overlay');
        const SWIPE_THRESHOLD = 30, SIDEBAR_WIDTH = 280;

        document.addEventListener('touchstart', (e) => {
            touchStartX = e.touches[0].clientX; hasMoved = false;
            const isClosed = sidebar.classList.contains('-translate-x-full');
            if (isClosed && touchStartX < SWIPE_THRESHOLD) isSwipingSidebar = true;
            if (!isClosed && e.target.closest('#main-sidebar')) { isSwipingSidebar = true; sidebar.style.transition = 'none'; overlay.style.transition = 'none'; }
        }, { passive: true });

        document.addEventListener('touchmove', (e) => {
            if (!isSwipingSidebar) return;
            touchCurrentX = e.touches[0].clientX;
            if (Math.abs(touchCurrentX - touchStartX) < 5) return;
            if (!hasMoved) {
                hasMoved = true; sidebar.style.transition = 'none';
                if (sidebar.classList.contains('-translate-x-full')) { overlay.classList.remove('hidden'); overlay.style.transition = 'none'; overlay.style.display = 'block'; }
                else overlay.style.transition = 'none';
            }
            let translateX = 0;
            const isClosedInitially = sidebar.classList.contains('-translate-x-full');
            if (isClosedInitially) translateX = Math.min(0, touchCurrentX - SIDEBAR_WIDTH);
            else translateX = Math.min(0, touchCurrentX - touchStartX);
            sidebar.style.transform = `translateX(${translateX}px)`;
            const progress = 1 - (Math.abs(translateX) / SIDEBAR_WIDTH);
            overlay.style.opacity = Math.max(0, Math.min(0.6, progress * 0.6));
        }, { passive: true });

        document.addEventListener('touchend', (e) => {
            if (!isSwipingSidebar) return;
            isSwipingSidebar = false;
            if (!hasMoved) return;
            sidebar.style.transition = 'transform 0.28s cubic-bezier(0.4, 0, 0.2, 1)';
            overlay.style.transition = 'opacity 0.3s ease';
            const transformMatch = sidebar.style.transform.match(/translateX\(([-\d.]+)px\)/);
            const currentTranslate = transformMatch ? parseFloat(transformMatch[1]) : 0;
            sidebar.style.transform = ''; overlay.style.opacity = ''; overlay.style.display = '';
            if (currentTranslate > -(SIDEBAR_WIDTH / 2)) openSidebar(); else closeSidebar();
        });

        // ── PWA: Service Worker ───────────────────────────────────────────────────
        // Registered from a real URL: browsers refuse blob: URLs for service
        // workers, so the previous inline-blob registration never took effect.
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js', { scope: '/' })
                .then(reg => reg.update())
                .catch(() => { });
        }

        // ── PWA: Install prompt ───────────────────────────────────────────────────
        let _deferredInstallPrompt = null;
        const _installBanner = document.getElementById('pwa-install-banner');
        const _isStandalone = () =>
            window.matchMedia('(display-mode: standalone)').matches ||
            window.navigator.standalone === true;

        window.addEventListener('beforeinstallprompt', e => {
            e.preventDefault();
            _deferredInstallPrompt = e;
            if (!_isStandalone() && !localStorage.getItem('pwa-dismissed')) {
                setTimeout(() => { 
                    if (!localStorage.getItem('pwa-dismissed')) {
                        _installBanner.style.display = ''; 
                    }
                }, 3500);
            }
        });
        window.addEventListener('appinstalled', () => {
            _installBanner.style.display = 'none';
            _deferredInstallPrompt = null;
            showToast('Venvy installed!', 'success', 3000);
        });
        function triggerPwaInstall() {
            if (!_deferredInstallPrompt) return;
            _deferredInstallPrompt.prompt();
            _deferredInstallPrompt.userChoice.then(r => {
                _installBanner.style.display = 'none';
                _deferredInstallPrompt = null;
            });
        }
        function dismissPwaBanner() {
            _installBanner.style.display = 'none';
            localStorage.setItem('pwa-dismissed', '1');
        }
