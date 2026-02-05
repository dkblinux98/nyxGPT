"use client";

import { useEffect, useState } from "react";

/**
 * Hook to detect online/offline status
 *
 * Usage:
 * const isOnline = useOfflineDetection();
 *
 * if (!isOnline) {
 *   // Show offline message or queue actions
 * }
 */
export function useOfflineDetection() {
  const [isOnline, setIsOnline] = useState(true);

  useEffect(() => {
    // Check initial status
    if (typeof window !== "undefined") {
      setIsOnline(navigator.onLine);
    }

    // Listen for online/offline events
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);

    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);

    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  return isOnline;
}
