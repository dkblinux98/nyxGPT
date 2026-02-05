"use client";

import { useEffect, useState } from "react";

export default function PushNotifications() {
  const [permission, setPermission] = useState<NotificationPermission>("default");
  const [subscription, setSubscription] = useState<PushSubscription | null>(null);

  useEffect(() => {
    if ("Notification" in window) {
      setPermission(Notification.permission);
    }
  }, []);

  const requestPermission = async () => {
    if (!("Notification" in window)) {
      console.error("This browser does not support notifications");
      return;
    }

    if (!("serviceWorker" in navigator)) {
      console.error("Service Worker not supported");
      return;
    }

    try {
      const permission = await Notification.requestPermission();
      setPermission(permission);

      if (permission === "granted") {
        await subscribeToPush();
      }
    } catch (error) {
      console.error("Error requesting notification permission:", error);
    }
  };

  const subscribeToPush = async () => {
    try {
      const registration = await navigator.serviceWorker.ready;

      // Check if already subscribed
      let sub = await registration.pushManager.getSubscription();

      if (!sub) {
        // Subscribe to push notifications
        // Note: This requires a VAPID public key from your push service
        // For now, this is a placeholder that would need backend integration
        const response = await fetch("/api/push/vapid-public-key");
        const { publicKey } = await response.json();

        sub = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey) as BufferSource,
        });

        // Send subscription to backend
        await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sub),
        });
      }

      setSubscription(sub);
    } catch (error) {
      console.error("Error subscribing to push notifications:", error);
    }
  };

  const unsubscribeFromPush = async () => {
    if (!subscription) return;

    try {
      await subscription.unsubscribe();
      setSubscription(null);

      // Notify backend
      await fetch("/api/push/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      });
    } catch (error) {
      console.error("Error unsubscribing from push notifications:", error);
    }
  };

  // Helper function to convert VAPID key
  function urlBase64ToUint8Array(base64String: string): Uint8Array {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  // Don't render anything if notifications aren't supported
  if (!("Notification" in window) || !("serviceWorker" in navigator)) {
    return null;
  }

  return (
    <div style={{ padding: "1rem", maxWidth: "400px" }}>
      <h3>Push Notifications</h3>
      <p>
        Status:{" "}
        {permission === "granted"
          ? "Enabled"
          : permission === "denied"
            ? "Blocked"
            : "Not enabled"}
      </p>

      {permission === "default" && (
        <button
          onClick={requestPermission}
          style={{
            padding: "0.5rem 1rem",
            backgroundColor: "#0070f3",
            color: "white",
            border: "none",
            borderRadius: "0.25rem",
            cursor: "pointer",
          }}
        >
          Enable Notifications
        </button>
      )}

      {permission === "granted" && subscription && (
        <button
          onClick={unsubscribeFromPush}
          style={{
            padding: "0.5rem 1rem",
            backgroundColor: "#dc2626",
            color: "white",
            border: "none",
            borderRadius: "0.25rem",
            cursor: "pointer",
          }}
        >
          Disable Notifications
        </button>
      )}

      {permission === "denied" && (
        <p style={{ color: "#dc2626", fontSize: "0.875rem" }}>
          Notifications are blocked. Please enable them in your browser settings.
        </p>
      )}
    </div>
  );
}
