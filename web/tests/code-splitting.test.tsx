import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render } from '@testing-library/react';
import React from 'react';
import nextDynamic from 'next/dynamic';

/**
 * Code Splitting Tests
 *
 * Validates the code splitting implementation for issue #2678:
 *   - Component lazy loading: ChatPane and VirtualizedSessionList use
 *     next/dynamic with loading fallbacks.
 *   - Chunk optimization: react-virtuoso is split into its own vendor chunk
 *     via webpack splitChunks config in next.config.ts.
 *   - Preloading critical paths: /admin, /models, and /settings are prefetched
 *     via <link rel="prefetch"> in layout.tsx.
 *
 * All three suites exercise the REAL implementation files so that removing
 * or breaking the code splitting features causes test failures.
 */

// ─── Module-level mocks (hoisted by vitest before any imports) ────────────────

// Intercept next/dynamic so we can assert it is called with loading options.
vi.mock('next/dynamic', () => ({
  default: vi.fn((_loader: unknown, _options: unknown) => {
    // Return a stub functional component so the module-level const assignments
    // in page.tsx succeed without importing the real lazy components.
    return function DynamicStub() { return null; };
  }),
}));

// Bypass the next-pwa service-worker setup so next.config.ts can be imported
// directly.  The passthrough (() => (cfg) => cfg) means the exported config is
// exactly the nextConfig object defined in next.config.ts, including the
// custom webpack function.
vi.mock('@ducanh2912/next-pwa', () => ({
  default: () => (config: unknown) => config,
}));

// Replace Next.js font loader with a minimal stub so layout.tsx can be
// imported without the build-time font pipeline.
vi.mock('next/font/google', () => ({
  Geist: (_opts: unknown) => ({ variable: '--font-geist-sans', className: 'mock-geist' }),
  Geist_Mono: (_opts: unknown) => ({ variable: '--font-geist-mono', className: 'mock-geist-mono' }),
}));

// ─── Import page.tsx once to trigger module-level dynamic() calls ─────────────
// Must happen after the vi.mock declarations above so the mock is in place
// when page.tsx executes `const ChatPane = dynamic(...)`.
beforeAll(async () => {
  await import('../src/app/page');
});

// ─── Suite 1: Loading fallbacks ───────────────────────────────────────────────
// These tests verify that the dynamic() calls in page.tsx actually supply a
// loading option.  They fail if the dynamic() calls are reverted to static
// imports or if the loading option is removed.

describe('Code Splitting – loading fallbacks', () => {
  it('page.tsx calls next/dynamic at least twice (ChatPane + VirtualizedSessionList)', () => {
    const mockedDynamic = vi.mocked(nextDynamic);
    expect(mockedDynamic).toHaveBeenCalled();
    expect(mockedDynamic.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('every dynamic() call in page.tsx includes a loading fallback function', () => {
    const mockedDynamic = vi.mocked(nextDynamic);
    const calls = mockedDynamic.mock.calls;

    // All dynamic() calls must supply a loading option – this test fails if the
    // loading: () => <LoadingSpinner /> or loading: () => <SessionListSkeleton />
    // lines are removed from page.tsx.
    calls.forEach((call, index) => {
      const options = call[1] as { loading?: unknown };
      expect(
        typeof options?.loading,
        `dynamic() call #${index} is missing a loading option`,
      ).toBe('function');
    });
  });

  it('ChatPane loading fallback returns a renderable React element', () => {
    const mockedDynamic = vi.mocked(nextDynamic);

    // ChatPane is the first dynamic() call; it wraps a <LoadingSpinner />
    // inside a flex container.
    const firstCall = mockedDynamic.mock.calls[0];
    expect(firstCall).toBeDefined();

    const options = firstCall[1] as { ssr?: boolean; loading?: () => React.ReactNode };
    expect(typeof options?.loading).toBe('function');

    // Calling loading() must not throw and must return a React element.
    const element = options.loading!();
    expect(element).not.toBeUndefined();
    expect(element).not.toBeNull();
  });

  it('VirtualizedSessionList loading fallback returns a renderable React element', () => {
    const mockedDynamic = vi.mocked(nextDynamic);

    // VirtualizedSessionList is the second dynamic() call; it uses
    // loading: () => <SessionListSkeleton />.
    const secondCall = mockedDynamic.mock.calls[1];
    expect(secondCall).toBeDefined();

    const options = secondCall[1] as { ssr?: boolean; loading?: () => React.ReactNode };
    expect(typeof options?.loading).toBe('function');

    const element = options.loading!();
    expect(element).not.toBeUndefined();
    expect(element).not.toBeNull();
  });
});

// ─── Suite 2: Chunk optimization ──────────────────────────────────────────────
// These tests import the REAL next.config.ts, invoke its webpack() function with
// a mock webpack configuration, and verify that the virtuosoVendor cache group
// is present.  They fail if the webpack block is removed from next.config.ts.

describe('Code Splitting – chunk optimization configuration', () => {
  it('next.config.ts webpack() injects the virtuosoVendor cache group into splitChunks', async () => {
    // @ducanh2912/next-pwa is mocked as a passthrough so the exported default
    // IS nextConfig (with the custom webpack function intact).
    const { default: nextConfig } = await import('../next.config');

    type WebpackFn = (config: {
      optimization: { splitChunks: { cacheGroups: Record<string, unknown> } };
    }) => { optimization: { splitChunks: { cacheGroups: Record<string, unknown> } } };

    const config = nextConfig as { webpack?: WebpackFn };
    expect(config.webpack, 'next.config.ts must export a webpack function').toBeTypeOf('function');

    // Provide the minimal webpack config structure that the function inspects.
    const mockWebpack = {
      optimization: { splitChunks: { cacheGroups: {} as Record<string, unknown> } },
    };

    const result = config.webpack!(mockWebpack);
    const cacheGroups = result.optimization.splitChunks.cacheGroups;

    expect(cacheGroups.virtuosoVendor).toBeDefined();
    expect(cacheGroups.virtuosoVendor).toMatchObject({
      name: 'vendor-virtuoso',
      chunks: 'all',
      priority: 30,
      reuseExistingChunk: true,
    });
  });

  it('virtuosoVendor cache group regex matches react-virtuoso paths and rejects others', async () => {
    const { default: nextConfig } = await import('../next.config');

    type WebpackFn = (config: {
      optimization: { splitChunks: { cacheGroups: Record<string, unknown> } };
    }) => { optimization: { splitChunks: { cacheGroups: Record<string, unknown> } } };

    const mockWebpack = {
      optimization: { splitChunks: { cacheGroups: {} as Record<string, unknown> } },
    };
    const result = (nextConfig as { webpack?: WebpackFn }).webpack!(mockWebpack);
    const { test: regex } = result.optimization.splitChunks.cacheGroups
      .virtuosoVendor as { test: RegExp };

    // Must match react-virtuoso in node_modules (both POSIX and Windows paths).
    expect(regex.test('/home/user/project/node_modules/react-virtuoso/dist/index.js')).toBe(true);
    expect(regex.test('C:\\project\\node_modules\\react-virtuoso\\dist\\index.js')).toBe(true);

    // Must NOT match unrelated packages.
    expect(regex.test('/node_modules/react/index.js')).toBe(false);
    expect(regex.test('/node_modules/react-dom/index.js')).toBe(false);
    expect(regex.test('/node_modules/next/dist/index.js')).toBe(false);
  });

  it('virtuosoVendor chunk priority exceeds the default Next.js framework chunk priority (20)', async () => {
    const { default: nextConfig } = await import('../next.config');

    type WebpackFn = (config: {
      optimization: { splitChunks: { cacheGroups: Record<string, unknown> } };
    }) => { optimization: { splitChunks: { cacheGroups: Record<string, unknown> } } };

    const mockWebpack = {
      optimization: { splitChunks: { cacheGroups: {} as Record<string, unknown> } },
    };
    const result = (nextConfig as { webpack?: WebpackFn }).webpack!(mockWebpack);
    const group = result.optimization.splitChunks.cacheGroups.virtuosoVendor as {
      priority: number;
      chunks: string;
    };

    expect(group.priority).toBeGreaterThan(20);
    expect(group.chunks).toBe('all');
  });
});

// ─── Suite 3: Prefetch route hints ────────────────────────────────────────────
// These tests render the REAL layout.tsx component and assert that the
// <link rel="prefetch"> elements are present in document.head.  In the
// happy-dom test environment, Next.js layout head elements are promoted to
// document.head (not the container div), so we query document.head directly.
// They fail if the prefetch links are removed from layout.tsx.

describe('Code Splitting – prefetch route hints', () => {
  it('layout.tsx renders a <link rel="prefetch" href="/admin" as="document"> element', async () => {
    const { default: RootLayout } = await import('../src/app/layout');
    render(React.createElement(RootLayout, null, React.createElement('span', null, 'test')));

    const adminLink = document.head.querySelector('link[rel="prefetch"][href="/admin"]');
    expect(adminLink, '<link rel="prefetch" href="/admin"> not found in document.head').not.toBeNull();
    expect(adminLink!.getAttribute('as')).toBe('document');
  });

  it('layout.tsx renders <link rel="prefetch"> elements for /models and /settings', async () => {
    const { default: RootLayout } = await import('../src/app/layout');
    render(React.createElement(RootLayout, null, React.createElement('span', null, 'test')));

    const modelsLink = document.head.querySelector('link[rel="prefetch"][href="/models"]');
    const settingsLink = document.head.querySelector('link[rel="prefetch"][href="/settings"]');

    expect(modelsLink, '<link rel="prefetch" href="/models"> not found in document.head').not.toBeNull();
    expect(settingsLink, '<link rel="prefetch" href="/settings"> not found in document.head').not.toBeNull();
    expect(modelsLink!.getAttribute('as')).toBe('document');
    expect(settingsLink!.getAttribute('as')).toBe('document');
  });

  it('layout.tsx renders exactly three <link rel="prefetch" as="document"> hints', async () => {
    const { default: RootLayout } = await import('../src/app/layout');
    render(React.createElement(RootLayout, null, React.createElement('span', null, 'test')));

    const prefetchLinks = document.head.querySelectorAll('link[rel="prefetch"][as="document"]');
    expect(
      prefetchLinks.length,
      'Expected exactly 3 prefetch links (/admin, /models, /settings)',
    ).toBe(3);
  });
});

// ─── Suite 4: Dynamic import pattern ─────────────────────────────────────────
// These tests import the real component modules and verify the export shapes
// that the dynamic() wrappers in page.tsx depend on.

describe('Code Splitting – dynamic import pattern', () => {
  it('can resolve VirtualizedSessionList as a valid React component via named re-export', async () => {
    // Simulate the .then(mod => ({ default: mod.VirtualizedSessionList })) pattern
    // used in the dynamic() call.  The named export must exist.
    const mod = await import('../src/components/VirtualizedSessionList');
    expect(mod.VirtualizedSessionList).toBeDefined();

    // React.memo() returns an object (not a plain function), so we accept both.
    const t = typeof mod.VirtualizedSessionList;
    expect(['function', 'object']).toContain(t);

    const reExported = { default: mod.VirtualizedSessionList };
    expect(reExported.default).toBeDefined();
  });

  it('can resolve ChatPane as a default export directly', async () => {
    const mod = await import('../src/app/components/ChatPane');
    expect(typeof mod.default).toBe('function');
  });
});
