import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { OptimizedImage } from '../../src/components/OptimizedImage';

// Mock Next.js Image component
vi.mock('next/image', () => ({
  default: ({ onLoad, ...props }: any) => {
    return (
      <img
        {...props}
        onLoad={(e) => {
          if (onLoad) onLoad(e);
        }}
      />
    );
  },
}));

describe('OptimizedImage', () => {
  it('renders with required props', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute('src', '/test-image.png');
  });

  it('applies default quality setting', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('quality', '85');
  });

  it('applies custom quality setting', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        quality={95}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('quality', '95');
  });

  it('sets lazy loading by default', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('loading', 'lazy');
  });

  it('disables lazy loading when priority is true', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        priority
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).not.toHaveAttribute('loading', 'lazy');
  });

  it('enables blur placeholder by default', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('placeholder', 'blur');
  });

  it('disables blur placeholder when enableBlur is false', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        enableBlur={false}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('placeholder', 'empty');
  });

  it('uses custom blur data URL when provided', () => {
    const customBlur = 'data:image/png;base64,custombase64string';
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        customBlurDataURL={customBlur}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('blurDataURL', customBlur);
  });

  it('shows skeleton when showSkeleton is true', () => {
    const { container } = render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        showSkeleton
      />
    );

    const skeleton = container.querySelector('[aria-hidden="true"]');
    expect(skeleton).toBeInTheDocument();
  });

  it('hides skeleton after image loads', async () => {
    const { container } = render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        showSkeleton
      />
    );

    const img = screen.getByAltText('Test image');

    // Trigger load event
    img.dispatchEvent(new Event('load'));

    await waitFor(() => {
      const skeleton = container.querySelector('[aria-hidden="true"]');
      expect(skeleton).not.toBeInTheDocument();
    });
  });

  it('calls onLoad callback when image loads', () => {
    const handleLoad = vi.fn();
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        onLoad={handleLoad}
      />
    );

    const img = screen.getByAltText('Test image');
    img.dispatchEvent(new Event('load'));

    expect(handleLoad).toHaveBeenCalledTimes(1);
  });

  it('applies custom skeleton color', () => {
    const customColor = '#ff0000';
    const { container } = render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        showSkeleton
        skeletonColor={customColor}
      />
    );

    const skeleton = container.querySelector('[aria-hidden="true"]') as HTMLElement;
    expect(skeleton).toHaveStyle({ backgroundColor: customColor });
  });

  it('forwards additional props to Image component', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        className="custom-class"
        style={{ borderRadius: '8px' }}
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveClass('custom-class');
    expect(img).toHaveStyle({ borderRadius: '8px' });
  });

  it('handles responsive sizing with fill prop', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        fill
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toBeInTheDocument();
  });

  it('respects custom loading prop', () => {
    render(
      <OptimizedImage
        src="/test-image.png"
        alt="Test image"
        width={800}
        height={600}
        loading="eager"
      />
    );

    const img = screen.getByAltText('Test image');
    expect(img).toHaveAttribute('loading', 'eager');
  });
});
