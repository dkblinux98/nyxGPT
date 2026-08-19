/**
 * The no-hydration case, end to end (#3857).
 *
 * `withChunkTimeout` and `ChunkErrorBoundary` only fire once the client
 * bootstrap has run. The incident this issue records is the case where it does
 * not: `/_next/static/chunks/*` never executes, so the document is left showing
 * its server-rendered loading placeholders -- `/`'s dynamic-import fallbacks,
 * `Loading canary status...` on the canary page -- with every client-side guard
 * asleep.
 *
 * These tests take the markup the server actually ships, run *only* the code
 * that arrives inside it, and assert a failure surface appears anyway. Against
 * the pre-fix layout there is no inline script in that markup at all, so the
 * first test cannot pass.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { render } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  HYDRATION_FLAG,
  HYDRATION_WATCHDOG_ELEMENT_ID,
  HYDRATION_WATCHDOG_TIMEOUT_MS,
} from '../../src/lib/hydrationWatchdog';

// Replace Next.js font loader with a minimal stub so layout.tsx can be
// imported without the build-time font pipeline.
vi.mock('next/font/google', () => ({
  Geist: (_opts: unknown) => ({ variable: '--font-geist-sans', className: 'mock-geist' }),
  Geist_Mono: (_opts: unknown) => ({ variable: '--font-geist-mono', className: 'mock-geist-mono' }),
}));

type Mutable = Record<string, unknown>;

/** The inline scripts the server-rendered document carries, in order. */
function inlineScriptsOf(markup: string): string[] {
  const doc = new DOMParser().parseFromString(markup, 'text/html');
  return Array.from(doc.querySelectorAll('script:not([src])'), (script) => script.textContent ?? '');
}

beforeEach(() => {
  delete (window as unknown as Mutable)[HYDRATION_FLAG];
  delete (window as unknown as Mutable).__nyxgptWatchdogArmed;
  document.body.innerHTML = '';
});

afterEach(() => {
  vi.useRealTimers();
});

describe('a document whose client bundle never runs', () => {
  it('surfaces the failure using only code that shipped with the HTML', async () => {
    vi.useFakeTimers();
    const { default: RootLayout } = await import('../../src/app/layout');

    // What the server sends: the page's markup, with no client JS executed --
    // renderToStaticMarkup runs no effect, mounts no boundary, hydrates
    // nothing, which is precisely the reported state.
    const markup = renderToStaticMarkup(
      React.createElement(
        RootLayout,
        null,
        React.createElement('div', { 'aria-label': 'Loading sessions' }, 'skeleton'),
      ),
    );
    expect(markup).toContain('aria-label="Loading sessions"');

    const watchdog = inlineScriptsOf(markup).find((source) =>
      source.includes(HYDRATION_WATCHDOG_ELEMENT_ID),
    );
    expect(watchdog, 'no hydration watchdog was shipped inside the document').toBeDefined();

    // Now be the browser: parse the body, run the inline script, run nothing
    // else, and let the clock reach the bound.
    document.body.innerHTML = '<div aria-label="Loading sessions">skeleton</div>';
    new Function(watchdog!)();
    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);

    const alert = document.getElementById(HYDRATION_WATCHDOG_ELEMENT_ID);
    expect(alert, 'placeholders persisted with no failure surface').not.toBeNull();
    expect(alert!.getAttribute('role')).toBe('alert');
    expect(alert!.textContent).toContain('Failed to load the interface');
    expect(alert!.querySelector('button')!.textContent).toBe('Reload');
  });

  it('disarms itself as soon as the client does hydrate', async () => {
    const { default: RootLayout } = await import('../../src/app/layout');

    // A real mount: effects run, so HydrationMarker announces the client.
    render(React.createElement(RootLayout, null, React.createElement('span', null, 'ok')));

    expect((window as unknown as Mutable)[HYDRATION_FLAG]).toBe(true);
    expect(document.getElementById(HYDRATION_WATCHDOG_ELEMENT_ID)).toBeNull();
  });
});
