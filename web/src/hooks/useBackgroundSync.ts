"use client";

import { useEffect, useState } from "react";
import { requestSync } from "../lib/serviceWorker";

export interface BackgroundSyncStatus {
  supported: boolean;
  registered: boolean;
  syncing: boolean;
}

/**
 * Hook to manage background sync for offline actions
 *
 * Usage:
 * const { sync, status } = useBackgroundSync();
 *
 * // Queue an action for background sync
 * sync('sync-chat-messages');
 */
export function useBackgroundSync() {
  const [status, setStatus] = useState<BackgroundSyncStatus>({
    supported: false,
    registered: false,
    syncing: false,
  });

  useEffect(() => {
    // Check if background sync is supported
    if (typeof window !== "undefined") {
      const supported =
        "serviceWorker" in navigator &&
        "sync" in ServiceWorkerRegistration.prototype;

      setStatus((prev) => ({ ...prev, supported }));
    }
  }, []);

  const sync = async (tag: string) => {
    if (!status.supported) {
      console.warn("[Background Sync] Not supported in this browser");
      return false;
    }

    try {
      setStatus((prev) => ({ ...prev, syncing: true }));
      requestSync(tag);
      setStatus((prev) => ({ ...prev, registered: true, syncing: false }));
      return true;
    } catch (error) {
      console.error("[Background Sync] Failed:", error);
      setStatus((prev) => ({ ...prev, syncing: false }));
      return false;
    }
  };

  return {
    sync,
    status,
  };
}
