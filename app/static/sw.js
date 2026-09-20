// Cache-first for static assets only. Every other request (dashboard, /today,
// any HTML route) passes straight through to the network, uncached — this app's
// safe-to-spend figure must never be served stale, so no page content is ever
// cached here. Bump CACHE_NAME on any deploy that changes files under /static/
// so old clients don't keep serving a stale CSS/icon from a previous version.
const CACHE_NAME = 'budget-static-v11';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const isStaticAsset = event.request.method === 'GET' && url.pathname.startsWith('/static/');

  if (!isStaticAsset) {
    return; // let the browser handle it normally — no caching, no interception
  }

  event.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          cache.put(event.request, response.clone());
          return response;
        });
      })
    )
  );
});

// Push payloads are {title, body, url} JSON, written by app/services/push.py.
self.addEventListener('push', (event) => {
  let payload = { title: 'Budget', body: '' , url: '/' };
  if (event.data) {
    try {
      payload = Object.assign(payload, event.data.json());
    } catch (e) {
      payload.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/static/icons/icon-192.png',
      data: { url: payload.url },
    })
  );

  // Badging API (Phase 11) -- update the icon badge even if no page/tab is open,
  // matching how native app badges behave. Feature-detected: unsupported on
  // Android Chrome, no-ops there.
  if ('setAppBadge' in self.navigator) {
    event.waitUntil(
      fetch('/bank/unmatched-count')
        .then((r) => r.json())
        .then((data) => {
          if (data.count > 0) return self.navigator.setAppBadge(data.count);
          return self.navigator.clearAppBadge();
        })
        .catch(() => {})
    );
  }
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url === url && 'focus' in client) {
          // navigator.vibrate isn't available in a service worker -- ask the
          // focused page to do it instead (Phase 12 haptics).
          client.postMessage({ type: 'vibrate' });
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
