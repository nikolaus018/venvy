// Venvy service worker: keeps the app shell available when the network drops.
// Served from /sw.js (a worker can't be registered from a blob: URL).
const CACHE = 'venvy-v4';

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
    // Only cache the app shell and static assets on the same origin; never API calls.
    if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
    if (url.pathname !== '/' && !url.pathname.startsWith('/static/')) return;
    // App shell: network first, fall back to the cached copy when offline.
    e.respondWith(
        fetch(e.request)
            .then(resp => {
                const cc = (resp.headers.get('cache-control') || '').toLowerCase();
                if (resp && resp.status === 200 && !cc.includes('no-store')) {
                    const clone = resp.clone();
                    e.waitUntil(caches.open(CACHE).then(c => c.put(e.request, clone)).catch(() => { }));
                }
                return resp;
            })
            .catch(() => caches.match(e.request).then(cached =>
                cached || new Response('Offline — Venvy is unreachable.', {
                    status: 503,
                    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
                })
            ))
    );
});
