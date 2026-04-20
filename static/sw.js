const CACHE_NAME = 'fashion-erp-v1';
const ASSETS = [
    '/painel',
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
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
    // Para simplificar e garantir que os dados dinâmicos funcionem, 
    // usamos Network First com fallback para Cache
    event.respondWith(
        fetch(event.request)
            .catch(() => caches.match(event.request))
    );
});
