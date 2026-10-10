// Applies the stored theme/motion preference before first paint.
        (function () {
            try {
                var p = localStorage.getItem('venvy-theme') || 'system';
                var t = p === 'system'
                    ? (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
                    : p;
                document.documentElement.setAttribute('data-theme', t);
                if (localStorage.getItem('venvy-motion') === 'reduce') {
                    document.documentElement.setAttribute('data-motion', 'reduce');
                }
            } catch (e) { }
        })();
