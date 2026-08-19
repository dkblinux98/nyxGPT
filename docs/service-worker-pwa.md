# Service Worker & Progressive Web App (PWA) Implementation

## Overview
Implemented offline support for nyxGPT using a service worker with intelligent caching strategies, enabling the application to work offline and load faster.

## Features Implemented

### 1. Service Worker Registration
- Automatic service worker registration
- Skip waiting for immediate activation
- Disabled in development to avoid caching issues

### 2. Cache Strategies

#### CacheFirst Strategy
- **Google Fonts**: Cached for 1 year (maxEntries: 4)
- Prioritizes cached content for fast loading
- Best for resources that don't change frequently

#### StaleWhileRevalidate Strategy
- **Static Images**: PNG, JPG, JPEG, SVG, GIF, WebP (30 days, maxEntries: 64)
- **JavaScript & CSS**: Static resources
- Serves cached content immediately while fetching updates in background
- Provides instant loading with fresh content on next visit

#### NetworkFirst Strategy
- **API Calls**: `/api/*` routes
- Tries network first with 10-second timeout
- Falls back to cache if network unavailable
- Cache expires after 5 minutes (maxEntries: 50)
- Best for dynamic content that needs to be fresh

### 3. Offline Fallback
- Custom offline page at `/offline`
- Shows user-friendly message when network is unavailable
- "Try again" button to reload when connection restored
- Automatically served when navigating offline

### 4. PWA Manifest
- App name: nyxGPT
- Standalone display mode (full-screen app-like experience)
- Theme color: #0070f3
- Portrait orientation
- App icons configured

## Files Added/Modified

### Configuration
- `web/next.config.ts`: Service worker configuration with @ducanh2912/next-pwa
- `web/public/manifest.json`: PWA manifest for app metadata
- `web/.gitignore`: Excludes generated service worker files

### Pages
- `web/src/app/offline/page.tsx`: Offline fallback page
- `web/src/app/layout.tsx`: Added manifest and PWA metadata

### Generated Files (Git-ignored)
- `public/sw.js`: Service worker script
- `public/sw.js.map`: Source map for debugging
- `public/workbox-*.js`: Workbox runtime files

## Installation

The service worker is automatically installed on first visit. Users can install the app to their home screen when prompted.

### Browser Support
- Chrome/Edge: ✅ Full support
- Firefox: ✅ Full support
- Safari: ⚠️ Limited PWA support (no install prompt)
- Mobile browsers: ✅ Full support with install prompts

## Cache Management

### Cache Names
- `google-fonts`: Font files from Google Fonts CDN
- `static-images`: Image assets
- `static-resources`: JS/CSS bundles
- `api-cache`: API responses

### Cache Limits
```
Google Fonts:     4 entries, 1 year
Static Images:   64 entries, 30 days
API Cache:       50 entries, 5 minutes
```

### Cache Invalidation
- Service worker automatically updates when deployed
- `skipWaiting: true` ensures immediate activation, so tabs do not have to be
  closed first
- On every load, `ServiceWorkerVersionGuard` compares the running build
  against the one this browser last loaded and, when they differ, deletes
  `static-resources` and the Workbox precaches and calls `registration.update()`
  -- see [Automatic update recovery](#automatic-update-recovery)

## Testing

### Test Offline Functionality
1. Build the application: `npm run build`
2. Start production server: `npm start`
3. Open in browser and navigate the app
4. Open DevTools > Network tab
5. Select "Offline" throttling
6. Navigate to different pages - should show offline page for uncached routes
7. Cached pages and assets should still load

### Test Cache Strategies
1. Open DevTools > Application > Storage
2. Check "Cache Storage" to see cached resources
3. Clear cache and reload to see caching behavior
4. Use Network tab to verify cache hits vs network requests

### Test Background Sync
1. Build and start the production server, open the app, and go offline
   (DevTools > Network > Offline)
2. Rename, pin/unpin, or delete a session, or delete a document
3. Confirm the UI updates optimistically and *stays* updated (no rollback
   or "failed" toast) — instead you should see an info toast noting the
   change will complete once you're back online
4. Open DevTools > Application > Background Services > Background Sync (or
   inspect the `session-mutations-queue`/`document-mutations-queue`
   IndexedDB store, matching whichever action you triggered) to see the
   queued request
5. Go back online and confirm the queued request fires automatically
   (visible in the Network tab or by reloading and seeing the change
   persisted server-side)

### Test Service Worker Updates
1. Make changes to the app
2. Build and deploy (`nyxgpt ops install` restarts `nyxgpt-web` for you --
   see [ops.md](ops.md#nyxgpt-ops-install))
3. Open app in browser (with old version) and confirm the console has no
   `duplicate-queue-name` error (Application > Service Workers should show
   the worker `activated and is running`, not stuck `waiting`/`redundant`)
4. The new service worker installs, activates, and (via `skipWaiting`/
   `clientsClaim`) takes over the already-open tab automatically -- no tab
   close/reopen needed

### Test Stranded-Client Recovery (#3445, manual)

Not automated by the test harness (it would require actually swapping build
output under a live browser session). To verify by hand:

1. `npm run build && npm start` (or `nyxgpt ops install`) to get a running
   production server
2. Open the app in a browser tab and navigate around so some route chunks
   are loaded
3. Rebuild with different content (bump anything that changes the output,
   e.g. a comment) *without* restarting the server, to reproduce the
   original bug's window: `npm run build` again in the same terminal while
   the old `npm start` process is still serving
4. In the still-open tab, trigger a client-side navigation to a route whose
   chunk hash changed -- it should now show the "A new version of nyxGPT is
   available" banner (`AppUpdateBanner`) with a "Reload to update" button,
   instead of hanging with no feedback
5. Click "Reload to update" and confirm the page reloads to the new build
   successfully
6. Restart the server (or use `nyxgpt ops install`, which does this for you)
   and confirm a fresh load has no console errors

## Background Sync

Implemented:
- ✅ Cache static assets
- ✅ Offline fallback page
- ✅ Cache strategies
- ✅ Background sync

Session-mutation requests made while offline are queued by the service
worker and automatically replayed once connectivity returns, instead of
simply failing:

- **`session-mutations-queue`**: `POST /api/sessions/{name}/rename`,
  `/pin`, `/unpin`, `/delete`, `/title`, `/sync-filename`.
- **`document-mutations-queue`**: `DELETE /api/sessions/{name}/documents/{doc_id}`.
- Uses the `NetworkOnly` handler with a `backgroundSync` plugin
  (`workbox-background-sync`), retaining queued requests for 24 hours
  (`maxRetentionTime: 24 * 60`).
- Each queued route needs its **own** queue name: Workbox creates one `Queue`
  per `runtimeCaching` entry with a `backgroundSync` option and throws
  `duplicate-queue-name` if two `Queue`s in the same generated service
  worker share a name. Both routes originally used
  `session-mutations-queue`, which wedged every client's service worker on
  install (#3445) -- see
  [Automatic update recovery](#automatic-update-recovery) below.
- The browser replays the queue via the Background Sync API when available,
  falling back to a retry-on-reconnect strategy otherwise.
- These routes are safe to queue because the UI already applies optimistic
  updates (see `web/src/hooks/useSessionCache.ts`). However, Workbox's
  `NetworkOnly` strategy still rejects the calling `fetch()` even for a
  queued request — the call sites in `web/src/app/page.tsx` (`deleteSession`,
  `renameSession`, `togglePin`) and `web/src/app/components/ChatPane.tsx`
  (`detachDocument`) use `isQueuedForBackgroundSync()`
  (`web/src/app/lib/backgroundSync.ts`) to detect that case — offline, with
  an active service worker controller, and a network-error `TypeError` — and
  skip the rollback/error-toast so the optimistic update sticks and the user
  sees an informational toast instead.

Chat streaming (`/api/chat/stream`) is intentionally excluded from
background sync: it's a long-lived SSE response, and replaying a queued
request has no live client to stream the reply to. File uploads
(`POST /api/sessions/{name}/documents`) are also excluded, since large
request bodies are a poor fit for the background-sync queue and users
expect immediate upload feedback rather than a silent retry.

## Automatic update recovery

A web rebuild (`nyxgpt ops install`) restarts the `nyxgpt-web` service (see
[ops.md](ops.md#nyxgpt-ops-install)) so the server side never serves HTML
referencing chunk hashes that no longer exist on disk. But a tab that was
already open before the rebuild can still be running old client-side code
that references the old chunk names, or (more rarely) get stuck on a wedged
service worker update. `useAppUpdate`
(`web/src/hooks/useAppUpdate.ts`) detects this on the client and
`AppUpdateBanner` (`web/src/components/AppUpdateBanner.tsx`, mounted in
`web/src/app/layout.tsx`) surfaces an actionable "reload to update" banner
instead of a silent infinite spinner, on any of:

- an unhandled `ChunkLoadError`/CSS-chunk-load promise rejection (a dynamic
  import for a route chunk that no longer exists on disk)
- a failed resource load (`error` event, capture phase) for a
  `/_next/static/` script or stylesheet
- the service worker's `controller` changing after this page was already
  under an existing SW's control -- a genuine version swap, as distinct from
  the very first `clientsClaim` on a page that had no controller yet (which
  is ignored so first-time visitors don't see a spurious prompt)

`skipWaiting`/`clientsClaim`/`cleanupOutdatedCaches` are all enabled by
default by `@ducanh2912/next-pwa` (no explicit override needed in
`next.config.ts`), so once the service worker itself installs successfully
(see the `duplicate-queue-name` note above -- that bug prevented the SW from
ever reaching this point), it takes over existing tabs without waiting for
them to close.

### Chunks that never arrive (#3857)

The three signals above all require the browser to *notice* a failure. A chunk
request that is simply never answered produces none of them: `next/dynamic`
renders its `loading:` fallback until the import settles, and an import that
never settles leaves that fallback on screen for the life of the page. The
result is a UI that looks like it is still working while it is permanently
broken -- reported in #3857 as skeleton rows and a spinner that never resolved
while every backend endpoint answered in under 110 ms.

Three pieces close that hole:

- **`withChunkTimeout`** (`web/src/lib/chunkLoader.ts`) wraps every
  `next/dynamic` loader so an import still pending after
  `CHUNK_LOAD_TIMEOUT_MS` (20 s) rejects instead of hanging. The rejection is
  named `ChunkLoadError`, so `useAppUpdate`'s existing detection treats a hung
  chunk exactly like a failed one.
- **`ChunkErrorBoundary`** (`web/src/components/ChunkErrorBoundary.tsx`) wraps
  each `dynamic()` call site and renders "Failed to load the interface" with a
  **Reload** button that unregisters the service worker and clears every cache
  before reloading (`recoverAndReload` in
  `web/src/lib/serviceWorkerRecovery.ts`) -- an ordinary refresh does not
  remove a service worker. Non-chunk errors are re-thrown so the surrounding
  boundaries still own real bugs. Its Details panel reports whether a service
  worker was controlling the page, which is the discriminator between a stale
  cached client and chunk URLs that are genuinely unavailable.
- **`ServiceWorkerVersionGuard`** (mounted in `web/src/app/layout.tsx`) runs
  the build comparison described under
  [Cache Invalidation](#cache-invalidation). The build stamp is read out of
  the document's `webpack-*.js` / `main-app-*.js` bootstrap chunk URLs
  (`web/src/lib/buildFingerprint.ts`), so it needs no build-argument plumbing
  and is inert rather than wrong where those markers are absent.

Any new `next/dynamic` call must go through `withChunkTimeout` and sit inside
a `ChunkErrorBoundary`; a `loading:` fallback with neither is the #3857 trap
rebuilt.

Note for test authors: Next resolves `next/dynamic` to the App Router
implementation (React.lazy + Suspense) for everything under `app/`, while the
bare entry point is the Pages Router loadable, which swallows a rejected chunk
import and keeps rendering its fallback. `web/vitest.config.ts` aliases
`next/dynamic` to `next/dist/api/app-dynamic` so tests exercise the semantics
the product actually runs.

## Monitoring

### Service Worker Status
Check service worker status in DevTools > Application > Service Workers:
- Registered: ✅ Active and running
- Waiting: ⏳ New version ready (close tabs to activate)
- Redundant: ❌ Old version replaced

### Performance Impact
- **First Load**: Slightly longer (service worker registration)
- **Subsequent Loads**: Significantly faster (cached resources)
- **Offline Experience**: Seamless for cached content

## Troubleshooting

### Service Worker Not Registering
- Ensure production build: `npm run build && npm start`
- Service worker disabled in development mode
- Check browser console for errors

### Cache Not Updating
- Hard refresh: Ctrl+Shift+R (Windows/Linux) or Cmd+Shift+R (Mac)
- Clear site data: DevTools > Application > Clear Storage
- Unregister service worker manually in DevTools

### Offline Page Not Showing
- Verify `/offline` route exists and builds correctly
- Check network throttling is set to "Offline"
- Ensure service worker is active

## Dependencies

- `@ducanh2912/next-pwa@^10.8.0`: Modern, maintained fork of next-pwa
  - Supports Next.js 13+ App Router
  - TypeScript support
  - ESM compatible
  - Active maintenance

## Resources

- [Next PWA Documentation](https://ducanh2912.github.io/next-pwa/)
- [Workbox Strategies](https://developer.chrome.com/docs/workbox/modules/workbox-strategies/)
- [PWA Best Practices](https://web.dev/pwa/)
- [Service Worker Lifecycle](https://web.dev/service-worker-lifecycle/)
