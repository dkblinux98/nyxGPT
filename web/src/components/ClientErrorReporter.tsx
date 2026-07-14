'use client';

import { useEffect } from 'react';

function reportError(message: string, stack?: string) {
  fetch('/api/v1/error-tracking/report', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: message.slice(0, 2000),
      stack: stack?.slice(0, 8000),
      url: window.location.pathname,
    }),
  }).catch(() => {
    // Best-effort: never let error reporting itself surface an error.
  });
}

/**
 * Forwards uncaught client-side errors and unhandled promise rejections to
 * the backend's error tracking endpoint, which is itself a no-op unless an
 * operator has opted into a local, self-hosted error tracker.
 */
export default function ClientErrorReporter() {
  useEffect(() => {
    function handleError(event: ErrorEvent) {
      reportError(event.message, event.error?.stack);
    }

    function handleRejection(event: PromiseRejectionEvent) {
      const reason = event.reason;
      if (reason instanceof Error) {
        reportError(reason.message, reason.stack);
      } else {
        reportError(`Unhandled rejection: ${String(reason)}`);
      }
    }

    window.addEventListener('error', handleError);
    window.addEventListener('unhandledrejection', handleRejection);
    return () => {
      window.removeEventListener('error', handleError);
      window.removeEventListener('unhandledrejection', handleRejection);
    };
  }, []);

  return null;
}
