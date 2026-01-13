'use client';

import { useEffect, useState } from 'react';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastProps {
  id: string;
  type: ToastType;
  message: string;
  duration?: number;
  onDismiss: (id: string) => void;
}

export function Toast({ id, type, message, duration = 5000, onDismiss }: ToastProps) {
  const [isExiting, setIsExiting] = useState(false);

  useEffect(() => {
    if (duration > 0) {
      const timer = setTimeout(() => {
        handleDismiss();
      }, duration);

      return () => clearTimeout(timer);
    }
  }, [duration]);

  const handleDismiss = () => {
    setIsExiting(true);
    setTimeout(() => {
      onDismiss(id);
    }, 200); // Match animation duration
  };

  const getIcon = () => {
    switch (type) {
      case 'success':
        return '✓';
      case 'error':
        return '✕';
      case 'warning':
        return '⚠';
      case 'info':
        return 'ℹ';
      default:
        return '';
    }
  };

  const getBackgroundColor = () => {
    switch (type) {
      case 'success':
        return 'var(--toast-success-bg, #10b981)';
      case 'error':
        return 'var(--toast-error-bg, #ef4444)';
      case 'warning':
        return 'var(--toast-warning-bg, #f59e0b)';
      case 'info':
        return 'var(--toast-info-bg, #3b82f6)';
      default:
        return 'var(--toast-info-bg, #3b82f6)';
    }
  };

  return (
    <div
      role="alert"
      aria-live="assertive"
      aria-atomic="true"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '12px 16px',
        background: getBackgroundColor(),
        color: 'white',
        borderRadius: 8,
        boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
        minWidth: 300,
        maxWidth: 500,
        fontSize: 14,
        fontWeight: 500,
        animation: isExiting
          ? 'toast-exit 0.2s ease-out forwards'
          : 'toast-enter 0.2s ease-out',
        willChange: 'transform, opacity',
      }}
    >
      <span
        style={{
          fontSize: 18,
          fontWeight: 700,
          flexShrink: 0,
        }}
      >
        {getIcon()}
      </span>
      <span style={{ flex: 1 }}>{message}</span>
      <button
        onClick={handleDismiss}
        aria-label="Dismiss notification"
        style={{
          background: 'transparent',
          border: 'none',
          color: 'white',
          fontSize: 20,
          cursor: 'pointer',
          padding: 0,
          width: 24,
          height: 24,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: 4,
          opacity: 0.8,
          transition: 'opacity 0.2s ease',
          flexShrink: 0,
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.opacity = '1';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.opacity = '0.8';
        }}
      >
        ×
      </button>
    </div>
  );
}
