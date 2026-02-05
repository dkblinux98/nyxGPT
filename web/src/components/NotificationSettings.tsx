"use client";

import { useState, useEffect } from "react";
import { usePushNotifications } from "../hooks/usePushNotifications";

export default function NotificationSettings() {
  const {
    isSupported,
    permission,
    subscription,
    requestPermission,
    subscribeToPush,
    unsubscribeFromPush,
    sendNotification,
  } = usePushNotifications();

  const [isLoading, setIsLoading] = useState(false);

  const handleEnableNotifications = async () => {
    setIsLoading(true);
    try {
      const granted = await requestPermission();
      if (granted) {
        await subscribeToPush();
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleDisableNotifications = async () => {
    setIsLoading(true);
    try {
      await unsubscribeFromPush();
    } finally {
      setIsLoading(false);
    }
  };

  const handleTestNotification = async () => {
    await sendNotification("Test Notification", {
      body: "This is a test notification from nyxGPT",
      tag: "test",
      requireInteraction: false,
    });
  };

  if (!isSupported) {
    return (
      <div
        style={{
          padding: "1rem",
          backgroundColor: "var(--background)",
          border: "1px solid var(--foreground)",
          borderRadius: "0.5rem",
        }}
      >
        <h3 style={{ margin: "0 0 0.5rem 0" }}>Push Notifications</h3>
        <p style={{ margin: 0, opacity: 0.7 }}>
          Push notifications are not supported in this browser.
        </p>
      </div>
    );
  }

  return (
    <div
      style={{
        padding: "1rem",
        backgroundColor: "var(--background)",
        border: "1px solid var(--foreground)",
        borderRadius: "0.5rem",
      }}
    >
      <h3 style={{ margin: "0 0 0.5rem 0" }}>Push Notifications</h3>
      <div style={{ marginBottom: "1rem" }}>
        <p style={{ margin: "0 0 0.5rem 0" }}>
          Status:{" "}
          <strong>
            {permission === "granted"
              ? "Enabled"
              : permission === "denied"
              ? "Blocked"
              : "Not configured"}
          </strong>
        </p>
        {subscription && (
          <p style={{ margin: 0, fontSize: "0.875rem", opacity: 0.7 }}>
            Subscribed to push notifications
          </p>
        )}
      </div>

      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        {permission === "default" && (
          <button
            onClick={handleEnableNotifications}
            disabled={isLoading}
            style={{
              padding: "0.5rem 1rem",
              fontSize: "0.875rem",
              border: "none",
              borderRadius: "0.25rem",
              backgroundColor: "#0070f3",
              color: "white",
              cursor: isLoading ? "not-allowed" : "pointer",
              opacity: isLoading ? 0.6 : 1,
            }}
          >
            {isLoading ? "Enabling..." : "Enable Notifications"}
          </button>
        )}

        {permission === "granted" && subscription && (
          <>
            <button
              onClick={handleTestNotification}
              style={{
                padding: "0.5rem 1rem",
                fontSize: "0.875rem",
                border: "1px solid var(--foreground)",
                borderRadius: "0.25rem",
                backgroundColor: "transparent",
                color: "var(--foreground)",
                cursor: "pointer",
              }}
            >
              Test Notification
            </button>
            <button
              onClick={handleDisableNotifications}
              disabled={isLoading}
              style={{
                padding: "0.5rem 1rem",
                fontSize: "0.875rem",
                border: "1px solid #dc2626",
                borderRadius: "0.25rem",
                backgroundColor: "transparent",
                color: "#dc2626",
                cursor: isLoading ? "not-allowed" : "pointer",
                opacity: isLoading ? 0.6 : 1,
              }}
            >
              {isLoading ? "Disabling..." : "Disable Notifications"}
            </button>
          </>
        )}

        {permission === "denied" && (
          <p style={{ margin: 0, fontSize: "0.875rem", color: "#dc2626" }}>
            Notifications are blocked. Please enable them in your browser
            settings.
          </p>
        )}
      </div>

      <details style={{ marginTop: "1rem" }}>
        <summary
          style={{ cursor: "pointer", fontSize: "0.875rem", opacity: 0.8 }}
        >
          About push notifications
        </summary>
        <div
          style={{
            marginTop: "0.5rem",
            fontSize: "0.875rem",
            opacity: 0.7,
            lineHeight: 1.6,
          }}
        >
          <p>
            Push notifications allow nyxGPT to send you updates even when the
            app is not open. This can include:
          </p>
          <ul style={{ margin: "0.5rem 0", paddingLeft: "1.5rem" }}>
            <li>Session updates</li>
            <li>Model download progress</li>
            <li>Background task completion</li>
          </ul>
          <p style={{ margin: "0.5rem 0 0 0" }}>
            You can disable notifications at any time.
          </p>
        </div>
      </details>
    </div>
  );
}
