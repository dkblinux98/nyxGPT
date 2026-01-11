'use client';

type LoadingSpinnerProps = {
  size?: 'small' | 'medium' | 'large';
  color?: string;
  label?: string;
};

export default function LoadingSpinner({ size = 'medium', color, label }: LoadingSpinnerProps) {
  const sizeMap = {
    small: 16,
    medium: 24,
    large: 48,
  };

  const dimension = sizeMap[size];
  const spinnerColor = color || 'var(--foreground)';

  return (
    <div
      style={{
        display: 'inline-flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 8,
      }}
      role="status"
      aria-live="polite"
      aria-label={label || 'Loading'}
    >
      <div
        style={{
          width: dimension,
          height: dimension,
          border: `${Math.max(2, dimension / 8)}px solid var(--border-light)`,
          borderTop: `${Math.max(2, dimension / 8)}px solid ${spinnerColor}`,
          borderRadius: '50%',
          animation: 'spin 1s linear infinite',
        }}
      />
      {label && (
        <span style={{ fontSize: size === 'small' ? 12 : 14, opacity: 0.7 }}>
          {label}
        </span>
      )}
      <style jsx>{`
        @keyframes spin {
          0% {
            transform: rotate(0deg);
          }
          100% {
            transform: rotate(360deg);
          }
        }
      `}</style>
    </div>
  );
}
