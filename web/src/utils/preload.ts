/**
 * Utility functions for preloading critical code chunks
 * Improves perceived performance by loading code before it's needed
 */

// Track which components have been preloaded to avoid duplicate requests
const preloadedComponents = new Set<string>();

/**
 * Preload the ChatPane component
 * Call this on hover or focus of elements that will trigger ChatPane to load
 */
export function preloadChatPane() {
  if (preloadedComponents.has('ChatPane')) return;

  preloadedComponents.add('ChatPane');

  // Dynamic import with webpack magic comment for prefetch
  import(/* webpackPrefetch: true */ '../app/components/ChatPane').catch(() => {
    // If preload fails, it will be loaded on demand later
    preloadedComponents.delete('ChatPane');
  });
}

/**
 * Preload the UnifiedSearch component
 */
export function preloadUnifiedSearch() {
  if (preloadedComponents.has('UnifiedSearch')) return;

  preloadedComponents.add('UnifiedSearch');

  import(/* webpackPrefetch: true */ '../components/UnifiedSearch').catch(() => {
    preloadedComponents.delete('UnifiedSearch');
  });
}

/**
 * Preload the VirtualizedSessionList component
 */
export function preloadVirtualizedSessionList() {
  if (preloadedComponents.has('VirtualizedSessionList')) return;

  preloadedComponents.add('VirtualizedSessionList');

  import(/* webpackPrefetch: true */ '../components/VirtualizedSessionList').catch(() => {
    preloadedComponents.delete('VirtualizedSessionList');
  });
}

/**
 * Preload admin pages on hover
 */
export function preloadAdminPage() {
  if (preloadedComponents.has('AdminPage')) return;

  preloadedComponents.add('AdminPage');

  import(/* webpackPrefetch: true */ '../app/admin/page').catch(() => {
    preloadedComponents.delete('AdminPage');
  });
}

/**
 * Preload models page on hover
 */
export function preloadModelsPage() {
  if (preloadedComponents.has('ModelsPage')) return;

  preloadedComponents.add('ModelsPage');

  import(/* webpackPrefetch: true */ '../app/models/page').catch(() => {
    preloadedComponents.delete('ModelsPage');
  });
}

/**
 * Preload all critical path components on idle
 * Call this after initial page load to preload components the user is likely to need
 */
export function preloadCriticalPath() {
  if (typeof window === 'undefined') return;

  // Use requestIdleCallback if available, otherwise use setTimeout
  const schedulePreload = (callback: () => void) => {
    if ('requestIdleCallback' in window) {
      requestIdleCallback(callback, { timeout: 2000 });
    } else {
      setTimeout(callback, 1);
    }
  };

  // Preload in priority order
  schedulePreload(() => {
    preloadChatPane();
    schedulePreload(() => {
      preloadUnifiedSearch();
      schedulePreload(() => {
        preloadVirtualizedSessionList();
      });
    });
  });
}
