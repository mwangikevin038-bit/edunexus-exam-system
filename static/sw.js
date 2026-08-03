// Service worker disabled during development
// Clear all caches on activation
self.addEventListener('install', event => {
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(cacheNames => {
            return Promise.all(
                cacheNames.map(name => caches.delete(name))
            );
        })
    );
    self.clients.claim();
});

// Pass through all requests without caching
self.addEventListener('fetch', event => {
    event.respondWith(fetch(event.request));
});
