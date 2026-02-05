"use client";

import { useEffect, useState } from "react";
import { registerServiceWorker, skipWaiting } from "../lib/serviceWorker";

export function ServiceWorkerProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [showUpdatePrompt, setShowUpdatePrompt] = useState(false);

  useEffect(() => {
    registerServiceWorker({
      onSuccess: () => {
        console.log("Service worker registered successfully");
      },
      onUpdate: () => {
        setShowUpdatePrompt(true);
      },
      onError: (error) => {
        console.error("Service worker registration error:", error);
      },
    });
  }, []);

  const handleUpdate = () => {
    skipWaiting();
    window.location.reload();
  };

  return (
    <>
      {children}
      {showUpdatePrompt && (
        <div className="fixed bottom-4 right-4 z-50 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg p-4 max-w-sm">
          <div className="flex items-start gap-3">
            <div className="flex-shrink-0">
              <svg
                className="h-6 w-6 text-blue-600"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
            </div>
            <div className="flex-1">
              <h3 className="text-sm font-medium text-gray-900 dark:text-white">
                Update Available
              </h3>
              <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
                A new version of nyxGPT is available. Reload to update.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  onClick={handleUpdate}
                  className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded font-medium transition-colors"
                >
                  Reload
                </button>
                <button
                  onClick={() => setShowUpdatePrompt(false)}
                  className="px-3 py-1.5 bg-gray-200 hover:bg-gray-300 dark:bg-gray-700 dark:hover:bg-gray-600 text-gray-800 dark:text-gray-200 text-sm rounded font-medium transition-colors"
                >
                  Later
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
