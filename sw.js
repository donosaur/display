// Service Worker for display kiosk
// Runtime caching: immutable hashed photos (cache-first), shell and manifest (network-first)

const SHELL_CACHE = 'display-shell-v1';
const PHOTO_CACHE = 'display-photos-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((shellCache) => {
      return shellCache.match('/photos/photos.json').then((res) => {
        if (!res) return null;
        return res.json().catch(() => null);
      });
    }).then((manifest) => {
      if (!manifest || !Array.isArray(manifest)) return null;
      const validPhotos = new Set();
      manifest.forEach((item) => {
        const f = typeof item === 'string' ? item : item.f;
        if (f) {
          validPhotos.add(f);
          validPhotos.add('/photos/' + f);
        }
      });
      return caches.open(PHOTO_CACHE).then((photoCache) => {
        return photoCache.keys().then((requests) => {
          return Promise.all(
            requests.map((req) => {
              const url = new URL(req.url);
              const path = url.pathname;
              const fname = path.replace(/^\/photos\//, '');
              if (!validPhotos.has(fname) && !validPhotos.has(path)) {
                return photoCache.delete(req);
              }
            })
          );
        });
      });
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Strategy 1: Hashed photo files under /photos/ (.jpg, .jpeg, .png, .webp) -> Cache-first (immutable)
  if (url.pathname.startsWith('/photos/') && /\.(jpg|jpeg|png|webp)$/i.test(url.pathname)) {
    event.respondWith(
      caches.open(PHOTO_CACHE).then((cache) => {
        return cache.match(request).then((cachedResponse) => {
          if (cachedResponse) {
            return cachedResponse;
          }
          return fetch(request).then((networkResponse) => {
            if (networkResponse && networkResponse.ok) {
              cache.put(request, networkResponse.clone());
            }
            return networkResponse;
          });
        });
      })
    );
    return;
  }

  // Strategy 2: /, /index.html, /photos/photos.json -> Network-first, falling back to cache
  if (url.pathname === '/' || url.pathname === '/index.html' || url.pathname === '/photos/photos.json') {
    event.respondWith(
      fetch(request).then((networkResponse) => {
        if (networkResponse && networkResponse.ok) {
          const clone = networkResponse.clone();
          caches.open(SHELL_CACHE).then((cache) => {
            cache.put(request, clone);
          });
        }
        return networkResponse;
      }).catch(() => {
        return caches.open(SHELL_CACHE).then((cache) => {
          return cache.match(request, { ignoreSearch: true }).then((cached) => {
            if (cached) return cached;
            if (url.pathname === '/' || url.pathname === '/index.html') {
              return cache.match('/index.html', { ignoreSearch: true }).then((idx) => idx || cache.match('/', { ignoreSearch: true }));
            }
          });
        });
      })
    );
    return;
  }
});
