const CACHE_NAME = 'fashion-erp-v2';
const ASSETS = [
    '/static/style.css',
    '/static/manifest.json',
    '/static/icon-512.png'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(names => Promise.all(names.filter(name => name !== CACHE_NAME).map(name => caches.delete(name))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (url.origin === self.location.origin && (url.pathname.startsWith('/api/') || event.request.mode === 'navigate')) {
        event.respondWith(fetch(event.request));
        return;
    }

    // Para simplificar e garantir que os dados dinâmicos funcionem, 
    // usamos Network First com fallback para Cache
    event.respondWith(
        fetch(event.request)
            .catch(() => caches.match(event.request))
    );
});
