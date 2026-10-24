/**
 * The app's icons come from one source, and both consumers derive from it (#4235).
 *
 * This tree used to declare its icons three times over: `metadata.icons` in
 * `layout.tsx`, a hand-written `web/public/manifest.json`, and Next's
 * `app/icon.png` / `app/apple-icon.png` file convention -- which published
 * `/icon.png` and `/apple-icon.png` and emitted its own `<link>` tags while no
 * `<link>` in the rendered HTML named either. Whichever mechanism won was a
 * framework precedence rule, not a decision written down anywhere.
 *
 * `web/src/lib/appIcons.ts` is now that decision, and the assertions here are
 * about *derivation* rather than about values: each one compares a consumer's
 * output to the inventory, so changing the inventory moves both consumers and
 * keeps this suite green, while hardcoding a URL back into either consumer
 * turns it red. The complementary structural guard --
 * no file-convention icon, no icon URL written down anywhere else -- is
 * `tests/unit/test_web_icon_declaration_is_single_sourced.py`.
 *
 * What the rendered `<head>` actually contains is not assertable here: Next
 * processes the `metadata` export in the build, not in `RootLayout`'s JSX, so
 * nothing this test renders would carry the tags. That claim is verified by
 * running `npm run build` and reading `web/.next/server/app/*.html`, cited in
 * the PR.
 */
import { describe, expect, it, vi } from 'vitest';

import {
  APPLE_TOUCH_ICON,
  FAVICONS,
  INSTALL_ICONS,
  SHORTCUT_ICON,
  linkIcons,
  manifestIcons,
} from '../../src/lib/appIcons';

// Replace Next.js font loader with a minimal stub so layout.tsx can be
// imported without the build-time font pipeline.
vi.mock('next/font/google', () => ({
  Geist: (_opts: unknown) => ({ variable: '--font-geist-sans', className: 'mock-geist' }),
  Geist_Mono: (_opts: unknown) => ({ variable: '--font-geist-mono', className: 'mock-geist-mono' }),
}));

describe('the icon inventory', () => {
  it('declares every icon as a rooted path under web/public/', () => {
    const urls = [
      ...FAVICONS.map((icon) => icon.url),
      SHORTCUT_ICON,
      APPLE_TOUCH_ICON.url,
      ...INSTALL_ICONS.map((icon) => icon.url),
    ];
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) {
      expect(url).toMatch(/^\/[A-Za-z0-9_.-]+\.(png|ico)$/);
    }
  });

  it('offers a multi-resolution .ico as well as sized PNGs', () => {
    // The `.ico` is the fallback for browsers that ignore `sizes`, and it is
    // also the only entry the manifest deliberately drops. Both behaviours
    // below depend on the inventory still containing one.
    expect(FAVICONS.some((icon) => icon.type === undefined && icon.sizes === 'any')).toBe(true);
    expect(FAVICONS.some((icon) => icon.type === 'image/png')).toBe(true);
  });
});

describe('metadata.icons in layout.tsx', () => {
  it('derives its <link rel="icon"> list from the inventory, in order', async () => {
    const { metadata } = await import('../../src/app/layout');
    expect(metadata.icons).toEqual(linkIcons());
    const icons = linkIcons().icon;
    expect(icons.map((icon) => icon.url)).toEqual(FAVICONS.map((icon) => icon.url));
    expect(icons.map((icon) => icon.sizes)).toEqual(FAVICONS.map((icon) => icon.sizes));
  });

  it('derives the shortcut and apple-touch icons from the inventory', () => {
    expect(linkIcons().shortcut).toBe(SHORTCUT_ICON);
    expect(linkIcons().apple).toBe(APPLE_TOUCH_ICON.url);
  });

  it('returns fresh objects, so Next cannot mutate the inventory', () => {
    const first = linkIcons();
    const second = linkIcons();
    expect(first.icon).not.toBe(second.icon);
    expect(first.icon[0]).not.toBe(FAVICONS[0]);
    expect(first).toEqual(second);
  });

  it('does not set metadata.manifest, which would duplicate the manifest link', async () => {
    // `app/manifest.ts` emits its own `<link rel="manifest">`. Setting
    // `metadata.manifest` as well puts two in the head, pointing at two URLs.
    const { metadata } = await import('../../src/app/layout');
    expect(metadata.manifest).toBeUndefined();
  });
});

describe('the generated web app manifest', () => {
  it('derives its icons from the inventory', async () => {
    const { default: manifest } = await import('../../src/app/manifest');
    expect(manifest().icons).toEqual(manifestIcons());
  });

  it('offers the sized favicons, the apple-touch icon and the install logos', () => {
    const sources = manifestIcons().map((icon) => icon.src);
    for (const icon of FAVICONS.filter((candidate) => candidate.type !== undefined)) {
      expect(sources).toContain(icon.url);
    }
    expect(sources).toContain(APPLE_TOUCH_ICON.url);
    for (const icon of INSTALL_ICONS) {
      expect(sources).toContain(icon.url);
    }
  });

  it('drops favicon.ico, which has no useful manifest type', () => {
    // A manifest entry is expected to declare a concrete `type`, and a
    // multi-resolution `.ico` has none -- so it is the one inventory entry
    // that is deliberately absent here.
    expect(manifestIcons().map((icon) => icon.src)).not.toContain(SHORTCUT_ICON);
    expect(manifestIcons().every((icon) => icon.type.length > 0)).toBe(true);
  });

  it('carries both an "any" and a "maskable" purpose for the install icons', () => {
    const purposes = manifestIcons()
      .filter((icon) => icon.purpose !== undefined)
      .map((icon) => icon.purpose);
    expect(purposes).toContain('any');
    expect(purposes).toContain('maskable');
  });

  it('keeps the app identity the installed PWA shows', async () => {
    const { default: manifest } = await import('../../src/app/manifest');
    expect(manifest()).toMatchObject({
      name: 'nyxGPT',
      short_name: 'nyxGPT',
      start_url: '/',
      display: 'standalone',
      theme_color: '#0070f3',
      orientation: 'portrait-primary',
    });
  });
});
