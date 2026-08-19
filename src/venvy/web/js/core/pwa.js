/**
 * Progressive-web-app support: the service worker and the install prompt.
 *
 * The service worker is registered from an inline blob rather than a separate
 * `sw.js`, so Venvy stays deployable as a single served directory.
 */

import { showToast } from "./dom.js";

if ('serviceWorker' in navigator) {
    const SW_VERSION = 'venvy-v2';
    const swCode = `
const CACHE = '${SW_VERSION}';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => {
    e.waitUntil(
caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())
    );
});
self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);
    // Never intercept API, WebSocket upgrades, or external CDN resources
    if (url.pathname.startsWith('/api/') ||
e.request.method !== 'GET' ||
url.origin !== self.location.origin) return;
    // App shell: network-first, fall back to cache for offline resilience
    e.respondWith(
fetch(e.request)
    .then(resp => {
        if (resp && resp.status === 200) {
            const clone = resp.clone();
            caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
    })
    .catch(() => caches.match(e.request))
    );
});
`;
    try {
        const swBlob = new Blob([swCode], { type: 'application/javascript' });
        const swUrl = URL.createObjectURL(swBlob);
        navigator.serviceWorker.register(swUrl, { scope: '/' })
            .then(reg => reg.update())
            .catch(() => { });
    } catch { }
}

export let _deferredInstallPrompt = null;

export const _installBanner = document.getElementById('pwa-install-banner');

export const _isStandalone = () =>
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

export function triggerPwaInstall() {
    if (!_deferredInstallPrompt) return;
    _deferredInstallPrompt.prompt();
    _deferredInstallPrompt.userChoice.then(_r => {
        _installBanner.style.display = 'none';
        _deferredInstallPrompt = null;
    });
}

export function dismissPwaBanner() {
    _installBanner.style.display = 'none';
    localStorage.setItem('pwa-dismissed', '1');
}
