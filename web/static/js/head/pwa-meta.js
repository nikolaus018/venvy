// PWA metadata generated at load: apple-touch icon, iOS splash images and the web app manifest.
        (function () {
            // Generate a 180×180 PNG icon on a canvas and set it as apple-touch-icon
            try {
                const c = document.createElement('canvas');
                c.width = c.height = 180;
                const ctx = c.getContext('2d');
                const r = (x, y, w, h, rad, col) => {
                    ctx.beginPath(); ctx.roundRect(x, y, w, h, rad); ctx.fillStyle = col; ctx.fill();
                };
                r(0, 0, 180, 180, 40, '#090b0f');
                ctx.strokeStyle = '#3b82f6';
                ctx.lineWidth = 24; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
                ctx.beginPath(); ctx.moveTo(50, 56); ctx.lineTo(90, 132); ctx.lineTo(130, 56); ctx.stroke();
                const link = document.createElement('link');
                link.rel = 'apple-touch-icon';
                link.href = c.toDataURL('image/png');
                document.currentScript.parentNode.appendChild(link);
            } catch (e) { }

            // Generate iOS startup images for the installed PWA splash screen.
            try {
                const splashSpecs = [
                    [320, 568, '(device-width: 320px) and (device-height: 568px) and (-webkit-device-pixel-ratio: 2)'],
                    [320, 480, '(device-width: 320px) and (device-height: 480px) and (-webkit-device-pixel-ratio: 1)'],
                    [375, 667, '(device-width: 375px) and (device-height: 667px) and (-webkit-device-pixel-ratio: 2)'],
                    [375, 812, '(device-width: 375px) and (device-height: 812px) and (-webkit-device-pixel-ratio: 3)'],
                    [390, 844, '(device-width: 390px) and (device-height: 844px) and (-webkit-device-pixel-ratio: 3)'],
                    [393, 852, '(device-width: 393px) and (device-height: 852px) and (-webkit-device-pixel-ratio: 3)'],
                    [414, 736, '(device-width: 414px) and (device-height: 736px) and (-webkit-device-pixel-ratio: 3)'],
                    [414, 896, '(device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 2)'],
                    [414, 896, '(device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 3)'],
                    [402, 874, '(device-width: 402px) and (device-height: 874px) and (-webkit-device-pixel-ratio: 3)'],
                    [428, 926, '(device-width: 428px) and (device-height: 926px) and (-webkit-device-pixel-ratio: 3)'],
                    [430, 932, '(device-width: 430px) and (device-height: 932px) and (-webkit-device-pixel-ratio: 3)'],
                    [440, 956, '(device-width: 440px) and (device-height: 956px) and (-webkit-device-pixel-ratio: 3)'],
                    [768, 1024, '(device-width: 768px) and (device-height: 1024px) and (-webkit-device-pixel-ratio: 1)'],
                    [834, 1112, '(device-width: 834px) and (device-height: 1112px) and (-webkit-device-pixel-ratio: 2)'],
                    [1024, 1366, '(device-width: 1024px) and (device-height: 1366px) and (-webkit-device-pixel-ratio: 2)'],
                ];
                const drawSplashCanvas = (w, h) => {
                    const c = document.createElement('canvas');
                    c.width = w;
                    c.height = h;
                    const ctx = c.getContext('2d');
                    const bg = ctx.createLinearGradient(0, 0, w, h);
                    bg.addColorStop(0, '#090b0f');
                    bg.addColorStop(1, '#0c111c');
                    ctx.fillStyle = bg;
                    ctx.fillRect(0, 0, w, h);
                    ctx.strokeStyle = '#3b82f6';
                    ctx.lineWidth = Math.max(18, Math.round(w * 0.072));
                    ctx.lineCap = 'round';
                    ctx.lineJoin = 'round';
                    ctx.beginPath();
                    ctx.moveTo(w * 0.24, h * 0.34);
                    ctx.lineTo(w * 0.5, h * 0.62);
                    ctx.lineTo(w * 0.76, h * 0.34);
                    ctx.stroke();
                    return c.toDataURL('image/png');
                };
                splashSpecs.forEach(([w, h, media]) => {
                    const link = document.createElement('link');
                    link.rel = 'apple-touch-startup-image';
                    link.href = drawSplashCanvas(w, h);
                    link.media = media;
                    document.currentScript.parentNode.appendChild(link);
                });
            } catch (e) { }

            // Inject manifest as blob — richer than before, proper PNG icon via canvas
            try {
                const c2 = document.createElement('canvas');
                c2.width = c2.height = 512;
                const ctx2 = c2.getContext('2d');
                const r2 = (x, y, w, h, rad, col) => {
                    ctx2.beginPath(); ctx2.roundRect(x, y, w, h, rad); ctx2.fillStyle = col; ctx2.fill();
                };
                const s = 512;
                r2(0, 0, s, s, s * 0.22, '#090b0f');
                ctx2.strokeStyle = '#3b82f6';
                ctx2.lineWidth = 68; ctx2.lineCap = 'round'; ctx2.lineJoin = 'round';
                ctx2.beginPath(); ctx2.moveTo(s * 0.28, s * 0.31); ctx2.lineTo(s * 0.5, s * 0.73); ctx2.lineTo(s * 0.72, s * 0.31); ctx2.stroke();
                const iconDataUrl = c2.toDataURL('image/png');

                const manifest = {
                    name: 'Venvy',
                    short_name: 'Venvy',
                    description: 'Manage, deploy and monitor your Python applications',
                    start_url: window.location.origin + '/',
                    scope: window.location.origin + '/',
                    display: 'standalone',
                    orientation: 'any',
                    background_color: '#0c111c',
                    theme_color: '#0c111c',
                    categories: ['developer', 'utilities', 'productivity'],
                    icons: [
                        { src: iconDataUrl, sizes: '512x512', type: 'image/png', purpose: 'any maskable' },
                    ],
                    shortcuts: [],
                    prefer_related_applications: false,
                };
                const blob = new Blob([JSON.stringify(manifest)], { type: 'application/manifest+json' });
                const mlink = document.createElement('link');
                mlink.rel = 'manifest'; mlink.href = URL.createObjectURL(blob);
                document.currentScript.parentNode.appendChild(mlink);
            } catch (e) { }
        })();
