/**
 * Service Worker registration utility
 * Handles registration, updates, and lifecycle management
 */

export interface ServiceWorkerConfig {
  onSuccess?: (registration: ServiceWorkerRegistration) => void;
  onUpdate?: (registration: ServiceWorkerRegistration) => void;
  onError?: (error: Error) => void;
}

export function registerServiceWorker(config?: ServiceWorkerConfig) {
  // Only run in browser
  if (typeof window === "undefined") {
    return;
  }

  // Check if service workers are supported
  if (!("serviceWorker" in navigator)) {
    console.log("[Service Worker] Not supported in this browser");
    return;
  }

  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .then((registration) => {
        console.log("[Service Worker] Registered:", registration.scope);

        // Check for updates
        registration.addEventListener("updatefound", () => {
          const newWorker = registration.installing;
          if (!newWorker) return;

          newWorker.addEventListener("statechange", () => {
            if (newWorker.state === "installed") {
              if (navigator.serviceWorker.controller) {
                // New service worker available
                console.log("[Service Worker] New version available");
                config?.onUpdate?.(registration);
              } else {
                // Service worker installed for the first time
                console.log("[Service Worker] Content cached for offline use");
                config?.onSuccess?.(registration);
              }
            }
          });
        });

        config?.onSuccess?.(registration);
      })
      .catch((error) => {
        console.error("[Service Worker] Registration failed:", error);
        config?.onError?.(error);
      });
  });
}

export function unregisterServiceWorker() {
  if (typeof window === "undefined") {
    return;
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.ready
      .then((registration) => {
        registration.unregister();
        console.log("[Service Worker] Unregistered");
      })
      .catch((error) => {
        console.error("[Service Worker] Unregistration failed:", error);
      });
  }
}

export function skipWaiting() {
  if (typeof window === "undefined") {
    return;
  }

  if ("serviceWorker" in navigator && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: "SKIP_WAITING" });
  }
}

export function clearCache() {
  if (typeof window === "undefined") {
    return;
  }

  if ("serviceWorker" in navigator && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: "CLEAR_CACHE" });
  }
}

export function requestSync(tag: string) {
  if (typeof window === "undefined") {
    return;
  }

  if ("serviceWorker" in navigator && "sync" in ServiceWorkerRegistration.prototype) {
    navigator.serviceWorker.ready
      .then((registration) => {
        // @ts-expect-error - sync is not in the type definitions
        return registration.sync.register(tag);
      })
      .then(() => {
        console.log(`[Service Worker] Background sync registered: ${tag}`);
      })
      .catch((error) => {
        console.error("[Service Worker] Background sync failed:", error);
      });
  } else {
    console.log("[Service Worker] Background sync not supported");
  }
}
