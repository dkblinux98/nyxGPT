'use client';

type ErrorMessageProps = {
  message: string;
  title?: string;
  onRetry?: () => void;
  retrying?: boolean;
};

export default function ErrorMessage({ message, title = 'Error', onRetry, retrying = false }: ErrorMessageProps) {
  return (
    <div
      role="alert"
      style={{
        padding: '1rem',
        border: '1px solid #ffcccc',
        borderRadius: 8,
        background: 'var(--error-bg)',
        color: 'var(--error-text)',
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <span style={{ fontSize: 20, flexShrink: 0 }}>⚠️</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
          <div style={{ fontSize: 14, opacity: 0.9 }}>{message}</div>
        </div>
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          disabled={retrying}
          style={{
            padding: '8px 16px',
            background: retrying ? 'var(--button-hover)' : 'var(--error-text)',
            color: 'white',
            border: 'none',
            borderRadius: 6,
            fontSize: 14,
            fontWeight: 600,
            cursor: retrying ? 'not-allowed' : 'pointer',
            alignSelf: 'flex-start',
          }}
        >
          {retrying ? 'Retrying...' : 'Retry'}
        </button>
      )}
    </div>
  );
}
