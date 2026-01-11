'use client';

type SkeletonLoaderProps = {
  width?: string | number;
  height?: string | number;
  borderRadius?: number;
  count?: number;
};

function SkeletonItem({ width = '100%', height = 20, borderRadius = 4 }: Omit<SkeletonLoaderProps, 'count'>) {
  return (
    <div
      style={{
        width,
        height,
        borderRadius,
        background: 'linear-gradient(90deg, var(--skeleton-base) 25%, var(--skeleton-shimmer) 50%, var(--skeleton-base) 75%)',
        backgroundSize: '200% 100%',
        animation: 'shimmer 1.5s infinite',
      }}
      aria-hidden="true"
    >
      <style jsx>{`
        @keyframes shimmer {
          0% {
            background-position: 200% 0;
          }
          100% {
            background-position: -200% 0;
          }
        }
      `}</style>
    </div>
  );
}

export default function SkeletonLoader({ width, height, borderRadius, count = 1 }: SkeletonLoaderProps) {
  if (count === 1) {
    return <SkeletonItem width={width} height={height} borderRadius={borderRadius} />;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonItem key={i} width={width} height={height} borderRadius={borderRadius} />
      ))}
    </div>
  );
}

export function SessionListSkeleton() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }} aria-label="Loading sessions">
      {Array.from({ length: 5 }).map((_, i) => (
        <div
          key={i}
          style={{
            padding: '10px',
            borderRadius: 8,
            border: '1px solid var(--border-light)',
            background: 'var(--sidebar-bg)',
          }}
        >
          <SkeletonItem height={16} width="80%" borderRadius={4} />
          <div style={{ marginTop: 8 }}>
            <SkeletonItem height={12} width="60%" borderRadius={4} />
          </div>
        </div>
      ))}
    </div>
  );
}
