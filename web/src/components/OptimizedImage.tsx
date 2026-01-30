'use client';

import Image, { ImageProps } from 'next/image';
import { useState } from 'react';

type OptimizedImageProps = Omit<ImageProps, 'placeholder' | 'blurDataURL'> & {
  /**
   * Whether to enable blur placeholder while loading
   * @default true
   */
  enableBlur?: boolean;
  /**
   * Custom blur data URL (base64 encoded image)
   * If not provided, a default gray placeholder will be used
   */
  customBlurDataURL?: string;
  /**
   * Whether to show a loading skeleton
   * @default false
   */
  showSkeleton?: boolean;
  /**
   * Skeleton background color
   * @default 'var(--skeleton-bg, #e0e0e0)'
   */
  skeletonColor?: string;
};

/**
 * OptimizedImage - A wrapper around Next.js Image component with enhanced optimization features
 *
 * Features:
 * - Automatic WebP/AVIF format conversion
 * - Lazy loading by default
 * - Responsive image sizing
 * - Blur placeholder support
 * - Loading skeleton option
 *
 * @example
 * ```tsx
 * <OptimizedImage
 *   src="/my-image.png"
 *   alt="Description"
 *   width={800}
 *   height={600}
 * />
 * ```
 */
export function OptimizedImage({
  enableBlur = true,
  customBlurDataURL,
  showSkeleton = false,
  skeletonColor = 'var(--skeleton-bg, #e0e0e0)',
  priority = false,
  quality = 85,
  loading,
  onLoad,
  ...props
}: OptimizedImageProps) {
  const [isLoading, setIsLoading] = useState(true);

  // Default blur data URL (1x1 gray pixel)
  const defaultBlurDataURL =
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

  const handleLoad = (event: React.SyntheticEvent<HTMLImageElement, Event>) => {
    setIsLoading(false);
    onLoad?.(event);
  };

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      {showSkeleton && isLoading && (
        <div
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            width: '100%',
            height: '100%',
            backgroundColor: skeletonColor,
            animation: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
            borderRadius: 4,
          }}
          aria-hidden="true"
        />
      )}
      <Image
        {...props}
        priority={priority}
        quality={quality}
        loading={loading ?? (priority ? undefined : 'lazy')}
        placeholder={enableBlur ? 'blur' : 'empty'}
        blurDataURL={enableBlur ? (customBlurDataURL ?? defaultBlurDataURL) : undefined}
        onLoad={handleLoad}
      />
      <style jsx global>{`
        @keyframes pulse {
          0%, 100% {
            opacity: 1;
          }
          50% {
            opacity: 0.5;
          }
        }
      `}</style>
    </div>
  );
}
