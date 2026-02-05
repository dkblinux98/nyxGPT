# Service Worker & Progressive Web App (PWA) Implementation

## Overview
Implemented offline support for nyxGPT using a service worker with intelligent caching strategies, enabling the application to work offline and load faster.

## Features Implemented

### 1. Service Worker Registration
- Automatic service worker registration
- Skip waiting for immediate activation
- Disabled in development to avoid caching issues

### 2. Install Prompt
- Custom install prompt UI component
- Detects when app is installable
- Handles beforeinstallprompt event
- Dismissible prompt with "Install" and "Not now" options
- Automatically hides when app is already installed
- Auto-detects standalone display mode

### 3. Cache Strategies

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

### 4. Offline Fallback
- Custom offline page at `/offline`
- Shows user-friendly message when network is unavailable
- "Try again" button to reload when connection restored
- Automatically served when navigating offline

### 5. PWA Manifest
- App name: nyxGPT - Local AI Assistant
- Standalone display mode (full-screen app-like experience)
- Theme color: #0070f3
- Portrait orientation
- Multiple icon sizes for splash screens (192x192, 512x512)
- App categories: productivity, utilities
- Screenshots for app store listings

### 6. Push Notifications
- Push notification permission handling
- Service worker push subscription management
- VAPID key support for web push
- Notification settings UI component
- Test notification functionality
- Subscribe/unsubscribe capabilities
- Browser notification support detection

## Files Added/Modified

### Configuration
- `web/next.config.ts`: Service worker configuration with @ducanh2912/next-pwa
- `web/public/manifest.json`: PWA manifest for app metadata
- `web/.gitignore`: Excludes generated service worker files

### Pages & Components
- `web/src/app/offline/page.tsx`: Offline fallback page
- `web/src/app/layout.tsx`: Added manifest, PWA metadata, and InstallPrompt
- `web/src/components/InstallPrompt.tsx`: Custom install prompt UI
- `web/src/components/NotificationSettings.tsx`: Push notification settings UI
- `web/src/hooks/useInstallPrompt.ts`: Install prompt logic hook
- `web/src/hooks/usePushNotifications.ts`: Push notification management hook

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
- Users will get new service worker after closing all tabs
- `skipWaiting: true` ensures immediate activation

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

### Test Service Worker Updates
1. Make changes to the app
2. Build and deploy
3. Open app in browser (with old version)
4. Service worker will download new version in background
5. Close all tabs and reopen - new version activates

### Test Install Prompt
1. Build the application: `npm run build`
2. Start production server: `npm start`
3. Open in Chrome/Edge (desktop or mobile)
4. Install prompt should appear at bottom of screen
5. Click "Install" to add app to home screen/desktop
6. Verify app opens in standalone mode

### Test Push Notifications
1. Open the app in a browser that supports push notifications
2. Use the NotificationSettings component or implement it in your UI
3. Click "Enable Notifications" to request permission
4. Grant permission when browser prompts
5. Click "Test Notification" to verify notifications work
6. Check that notifications appear even when app is not in focus

## Background Sync (Future Enhancement)

Current implementation focuses on:
- ✅ Cache static assets
- ✅ Offline fallback page
- ✅ Cache strategies

Background sync for queueing offline actions (e.g., sending messages while offline) can be added in a future iteration by:

```typescript
// In next.config.ts, add to runtimeCaching:
{
  urlPattern: /^\/api\/chat\/.*/i,
  handler: "NetworkOnly",
  options: {
    backgroundSync: {
      name: "chat-queue",
      options: {
        maxRetentionTime: 24 * 60, // 24 hours in minutes
      },
    },
  },
}
```

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
