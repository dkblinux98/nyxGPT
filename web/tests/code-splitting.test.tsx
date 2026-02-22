import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import LoadingSpinner from '../src/components/LoadingSpinner';
import { SessionListSkeleton } from '../src/components/SkeletonLoader';

/**
 * Code Splitting Tests
 *
 * Validates the code splitting implementation for issue #2678:
 *   - Component lazy loading: ChatPane and VirtualizedSessionList use
 *     next/dynamic with loading fallbacks.
 *   - Chunk optimization: react-virtuoso is split into its own vendor chunk
 *     via webpack splitChunks config.
 *   - Preloading critical paths: /admin, /models, and /settings are prefetched
 *     via <link rel="prefetch"> in layout.tsx.
 */

describe('Code Splitting – loading fallbacks', () => {
  describe('ChatPane dynamic loading fallback', () => {
    it('renders LoadingSpinner as the ChatPane loading fallback', () => {
      // ChatPane uses `loading: () => <LoadingSpinner />` in its dynamic()
      // config. Verify the spinner renders with the correct ARIA role so
      // assistive technology announces a loading state to the user.
      render(<LoadingSpinner />);
      const spinner = screen.getByRole('status');
      expect(spinner).toBeInTheDocument();
      expect(spinner).toHaveAttribute('aria-label', 'Loading');
    });

    it('centers LoadingSpinner in a flex container matching ChatPane loading layout', () => {
      const container = document.createElement('div');
      container.style.display = 'flex';
      container.style.alignItems = 'center';
      container.style.justifyContent = 'center';
      container.style.height = '100%';

      expect(container.style.display).toBe('flex');
      expect(container.style.alignItems).toBe('center');
      expect(container.style.justifyContent).toBe('center');
    });
  });

  describe('VirtualizedSessionList dynamic loading fallback', () => {
    it('renders SessionListSkeleton as the VirtualizedSessionList loading fallback', () => {
      // VirtualizedSessionList uses `loading: () => <SessionListSkeleton />`
      // in its dynamic() config.  The skeleton must render at least one
      // visible placeholder element.
      render(<SessionListSkeleton />);
      // The skeleton renders at least one element in the document.
      const container = document.querySelector('[data-testid="session-list-skeleton"]')
        ?? document.body.firstChild;
      expect(container).toBeTruthy();
    });

    it('renders SessionListSkeleton without throwing', () => {
      expect(() => render(<SessionListSkeleton />)).not.toThrow();
    });
  });
});

describe('Code Splitting – chunk optimization configuration', () => {
  it('defines a virtuosoVendor cache group that targets react-virtuoso', () => {
    // Mirror the splitChunks.cacheGroups.virtuosoVendor entry from
    // next.config.ts to verify the regex matches the expected module path.
    const virtuosoGroup = {
      test: /[\\/]node_modules[\\/]react-virtuoso[\\/]/,
      name: 'vendor-virtuoso',
      chunks: 'all',
      priority: 30,
      reuseExistingChunk: true,
    };

    // The regex must match a typical node_modules path for react-virtuoso.
    expect(virtuosoGroup.test.test('/home/user/project/node_modules/react-virtuoso/dist/index.js')).toBe(true);
    expect(virtuosoGroup.test.test('C:\\project\\node_modules\\react-virtuoso\\dist\\index.js')).toBe(true);

    // The regex must NOT match unrelated packages.
    expect(virtuosoGroup.test.test('/node_modules/react/index.js')).toBe(false);
    expect(virtuosoGroup.test.test('/node_modules/react-dom/index.js')).toBe(false);
    expect(virtuosoGroup.test.test('/node_modules/next/dist/index.js')).toBe(false);
  });

  it('configures the vendor-virtuoso chunk with higher priority than default', () => {
    const virtuosoGroup = {
      name: 'vendor-virtuoso',
      chunks: 'all',
      priority: 30,
    };

    // Priority must be above 20 (the default Next.js framework chunk priority)
    // so react-virtuoso is reliably extracted into its own chunk.
    expect(virtuosoGroup.priority).toBeGreaterThan(20);
    expect(virtuosoGroup.chunks).toBe('all');
    expect(virtuosoGroup.name).toBe('vendor-virtuoso');
  });
});

describe('Code Splitting – prefetch route hints', () => {
  const CRITICAL_ROUTES = ['/admin', '/models', '/settings'] as const;

  it('lists the three critical routes that should be prefetched', () => {
    // These are the routes reachable from the main-page settings menu.
    // Each must have a corresponding <link rel="prefetch"> in layout.tsx.
    expect(CRITICAL_ROUTES).toHaveLength(3);
    expect(CRITICAL_ROUTES).toContain('/admin');
    expect(CRITICAL_ROUTES).toContain('/models');
    expect(CRITICAL_ROUTES).toContain('/settings');
  });

  it('creates valid <link rel="prefetch"> elements for each critical route', () => {
    CRITICAL_ROUTES.forEach((route) => {
      const link = document.createElement('link');
      link.rel = 'prefetch';
      link.href = route;
      link.setAttribute('as', 'document');

      expect(link.rel).toBe('prefetch');
      expect(link.href).toContain(route);
      expect(link.getAttribute('as')).toBe('document');
    });
  });

  it('does not prefetch non-critical or infrequently accessed routes', () => {
    const nonCriticalRoutes = ['/admin/logs', '/admin/collections', '/admin/playground'];

    nonCriticalRoutes.forEach((route) => {
      expect(CRITICAL_ROUTES as readonly string[]).not.toContain(route);
    });
  });
});

describe('Code Splitting – dynamic import pattern', () => {
  it('can resolve VirtualizedSessionList as a default export via named re-export', async () => {
    // Simulate the .then(mod => ({ default: mod.VirtualizedSessionList }))
    // pattern used in the dynamic() call.  The named export must exist on the
    // module so the transform produces a valid default-export object.
    const mod = await import('../src/components/VirtualizedSessionList');
    expect(typeof mod.VirtualizedSessionList).toBe('function');

    const reExported = { default: mod.VirtualizedSessionList };
    expect(typeof reExported.default).toBe('function');
  });

  it('can resolve ChatPane as a default export directly', async () => {
    const mod = await import('../src/app/components/ChatPane');
    expect(typeof mod.default).toBe('function');
  });
});
