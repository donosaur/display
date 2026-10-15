// Service Worker for display kiosk
// Runtime caching: immutable hashed photos and videos (cache-first), shell and manifest (network-first)

const SHELL_CACHE = 'display-shell-v5';
const PHOTO_CACHE = 'display-photos-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

function prunePhotos(manifest) {
  if (!manifest || !Array.isArray(manifest)) return Promise.resolve();
  const validPhotos = new Set();
  manifest.forEach((item) => {
    const f = typeof item === 'string' ? item : (item && item.f);
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
}

function handleRangeRequest(response, rangeHeader) {
  return response.arrayBuffer().then((buffer) => {
    const total = buffer.byteLength;
    const match = /bytes=(\d*)-(\d*)/.exec(rangeHeader);
    if (!match) {
      return new Response(buffer, {
        status: 200,
        headers: response.headers
      });
    }
    let start = match[1] ? parseInt(match[1], 10) : 0;
    let end = match[2] ? parseInt(match[2], 10) : total - 1;
    if (isNaN(start)) start = 0;
    if (isNaN(end) || end >= total) end = total - 1;
    if (start > end || start >= total) {
      return new Response(null, {
        status: 416,
        headers: {
          'Content-Range': `bytes */${total}`
        }
      });
    }
    const sliced = buffer.slice(start, end + 1);
    const headers = new Headers(response.headers);
    headers.set('Content-Range', `bytes ${start}-${end}/${total}`);
    headers.set('Content-Length', String(sliced.byteLength));
    headers.set('Accept-Ranges', 'bytes');
    return new Response(sliced, {
      status: 206,
      statusText: 'Partial Content',
      headers: headers
    });
  });
}

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((name) => {
          if (name.startsWith('display-shell-') && name !== SHELL_CACHE) {
            return caches.delete(name);
          }
        })
      );
    }).then(() => {
      return caches.open(SHELL_CACHE).then((shellCache) => {
        return shellCache.match('/photos/photos.json').then((res) => {
          if (!res) return null;
          return res.json().catch(() => null);
        });
      }).then((manifest) => {
        return prunePhotos(manifest);
      });
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (!event.data) return;
  if (event.data.type === 'PRUNE_PHOTOS') {
    const list = event.data.photos || event.data.manifest;
    if (list) {
      event.waitUntil(prunePhotos(list));
    }
  }
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Strategy 1: Hashed media files under /photos/ (.jpg, .jpeg, .png, .webp, .mp4, .webm) -> Cache-first (immutable)
  if (url.pathname.startsWith('/photos/') && /\.(jpg|jpeg|png|webp|mp4|webm)$/i.test(url.pathname)) {
    event.respondWith(
      caches.open(PHOTO_CACHE).then((cache) => {
        const cacheKey = url.pathname;
        return cache.match(cacheKey).then((cachedResponse) => {
          const rangeHeader = request.headers.get('range');
          if (cachedResponse) {
            if (rangeHeader) {
              return handleRangeRequest(cachedResponse, rangeHeader);
            }
            return cachedResponse;
          }
          // Fetch full resource (without Range header to avoid 206 which Cache API rejects)
          return fetch(request.url).then((networkResponse) => {
            if (networkResponse && networkResponse.status === 200) {
              cache.put(cacheKey, networkResponse.clone());
            }
            if (rangeHeader && networkResponse && networkResponse.status === 200) {
              return handleRangeRequest(networkResponse, rangeHeader);
            }
            return networkResponse;
          });
        });
      })
    );
    return;
  }

  // Strategy 2: /, /index.html, /photos/photos.json -> Network-first (no-cache), falling back to cache
  if (url.pathname === '/' || url.pathname === '/index.html' || url.pathname === '/photos/photos.json') {
    event.respondWith(
      fetch(request, { cache: 'no-cache' }).then((networkResponse) => {
        if (networkResponse && networkResponse.ok) {
          const clone = networkResponse.clone();
          caches.open(SHELL_CACHE).then((cache) => {
            if (url.pathname === '/photos/photos.json') {
              cache.put('/photos/photos.json', clone);
            } else {
              cache.put(request, clone);
            }
          });
        }
        return networkResponse;
      }).catch(() => {
        return caches.open(SHELL_CACHE).then((cache) => {
          if (url.pathname === '/photos/photos.json') {
            return cache.match('/photos/photos.json');
          }
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
