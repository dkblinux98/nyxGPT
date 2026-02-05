// nyxGPT Service Worker
// Provides offline support, caching, and background sync

const CACHE_VERSION = 'v1';
const STATIC_CACHE = `nyxgpt-static-${CACHE_VERSION}`;
const DYNAMIC_CACHE = `nyxgpt-dynamic-${CACHE_VERSION}`;
const API_CACHE = `nyxgpt-api-${CACHE_VERSION}`;

// Assets to cache immediately on install
const STATIC_ASSETS = [
  '/',
  '/offline',
  '/stone-soup-creative-logo.png',
  '/nyxGPT-logo.png',
];

// Install event - cache static assets
self.addEventListener('install', (event) => {
  console.log('[Service Worker] Installing...');
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => {
      console.log('[Service Worker] Caching static assets');
      return cache.addAll(STATIC_ASSETS);
    })
  );
  // Activate immediately
  self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  console.log('[Service Worker] Activating...');
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (
            cacheName.startsWith('nyxgpt-') &&
            cacheName !== STATIC_CACHE &&
            cacheName !== DYNAMIC_CACHE &&
            cacheName !== API_CACHE
          ) {
            console.log('[Service Worker] Deleting old cache:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  // Take control immediately
  return self.clients.claim();
});

// Fetch event - implement cache strategies
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET requests
  if (request.method !== 'GET') {
    return;
  }

  // API requests - Network first, fallback to cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstStrategy(request, API_CACHE));
    return;
  }

  // Static assets - Cache first, fallback to network
  if (
    url.pathname.match(/\.(js|css|woff2?|ttf|eot|svg|png|jpg|jpeg|gif|ico)$/)
  ) {
    event.respondWith(cacheFirstStrategy(request, STATIC_CACHE));
    return;
  }

  // HTML pages - Network first, fallback to cache, then offline page
  if (request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(
      networkFirstStrategy(request, DYNAMIC_CACHE).catch(() => {
        return caches.match('/offline');
      })
    );
    return;
  }

  // Default - Network first
  event.respondWith(networkFirstStrategy(request, DYNAMIC_CACHE));
});

// Cache first strategy
async function cacheFirstStrategy(request, cacheName) {
  const cachedResponse = await caches.match(request);
  if (cachedResponse) {
    return cachedResponse;
  }

  try {
    const networkResponse = await fetch(request);
    if (networkResponse.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch (error) {
    console.error('[Service Worker] Fetch failed:', error);
    throw error;
  }
}

// Network first strategy
async function networkFirstStrategy(request, cacheName) {
  try {
    const networkResponse = await fetch(request);
    if (networkResponse.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch (error) {
    const cachedResponse = await caches.match(request);
    if (cachedResponse) {
      return cachedResponse;
    }
    throw error;
  }
}

// Background sync for offline actions
self.addEventListener('sync', (event) => {
  console.log('[Service Worker] Background sync:', event.tag);

  if (event.tag === 'sync-chat-messages') {
    event.waitUntil(syncChatMessages());
  }
});

// Sync queued chat messages
async function syncChatMessages() {
  try {
    // In a real implementation, this would:
    // 1. Retrieve queued messages from IndexedDB
    // 2. Send them to the API
    // 3. Clear the queue on success
    console.log('[Service Worker] Syncing chat messages...');

    // For now, just log that sync is ready
    // The actual implementation would be added when IndexedDB message queuing is implemented
    return Promise.resolve();
  } catch (error) {
    console.error('[Service Worker] Sync failed:', error);
    throw error;
  }
}

// Message handling for manual cache updates
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  if (event.data && event.data.type === 'CLEAR_CACHE') {
    event.waitUntil(
      caches.keys().then((cacheNames) => {
        return Promise.all(
          cacheNames.map((cacheName) => {
            if (cacheName.startsWith('nyxgpt-')) {
              return caches.delete(cacheName);
            }
          })
        );
      })
    );
  }
});
