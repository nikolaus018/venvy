// Venvy service worker: keeps the app shell available when the network drops.
// Served from /sw.js (a worker can't be registered from a blob: URL).
const CACHE = 'venvy-v3';

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys()
            .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);
    // Never intercept API calls, non-GET requests, or other origins (CDNs).
    if (url.pathname.startsWith('/api/') ||
        e.request.method !== 'GET' ||
        url.origin !== self.location.origin) return;
    // App shell: network first, fall back to the cached copy when offline.
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
