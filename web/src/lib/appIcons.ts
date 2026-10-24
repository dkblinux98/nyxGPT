/**
 * The one place nyxGPT's icon images are written down (#4235).
 *
 * Browsers learn an app's icons through two unrelated standards, and nyxGPT
 * has to satisfy both:
 *
 * * HTML `<link rel="icon">` / `rel="apple-touch-icon"`, which Next emits from
 *   `metadata.icons` in `app/layout.tsx`; and
 * * the W3C web app manifest's `icons` array, which Next serves from
 *   `app/manifest.ts`.
 *
 * Neither can be expressed through the other, so there are necessarily two
 * *derivations* -- but there must be only one *decision*. Before this module
 * the list existed three times over: in `metadata.icons`, in a hand-written
 * `web/public/manifest.json`, and in Next's `app/icon.png` /
 * `app/apple-icon.png` file convention. Three hand-maintained copies of one
 * list is a list that drifts, and the third copy was worse than redundant: the
 * file convention publishes `/icon.png` and `/apple-icon.png` and emits its
 * own `<link>` tags by mere presence, so which images the browser actually
 * used came down to Next's precedence rules rather than to anything written in
 * this repository. Editing those two PNGs changed nothing; trimming
 * `metadata.icons` would have handed the icons back to them with no edit at
 * the point of change.
 *
 * Both derivations now read from here, and
 * `tests/unit/test_web_icon_declaration_is_single_sourced.py` fails the build
 * if an icon URL is written down anywhere else, if a file-convention icon
 * reappears under `web/src/app/`, or if either derivation stops importing this
 * module.
 *
 * Every `url` below is a path under `web/public/`, which Next serves verbatim
 * at the same path. To add an icon, put the image there and add it here -- not
 * through `app/icon.*`, which is the mechanism this module replaced.
 */

/** One icon image, in the shape the two standards happen to agree on. */
export type AppIcon = {
  /** Rooted URL path, as the browser requests it. Lives in `web/public/`. */
  readonly url: string;
  /** Pixel dimensions, or `'any'` for a multi-resolution `.ico`. */
  readonly sizes: string;
  /**
   * MIME type. Omitted for `favicon.ico`, whose two historical spellings
   * (`image/x-icon`, `image/vnd.microsoft.icon`) are both less reliable than
   * letting the browser sniff a format it has handled since 1999.
   */
  readonly type?: string;
};

/** An `AppIcon` whose MIME type is known, which the manifest requires. */
export type TypedAppIcon = AppIcon & { readonly type: string };

/**
 * Favicons offered for the tab, the bookmark bar and the history list.
 *
 * Ordered small-to-large with the `.ico` last: a browser that understands
 * `sizes` picks the right PNG, and one that does not falls through to the
 * `.ico`, which carries every size in a single file.
 */
export const FAVICONS: readonly AppIcon[] = [
  { url: '/favicon-16x16.png', sizes: '16x16', type: 'image/png' },
  { url: '/favicon-32x32.png', sizes: '32x32', type: 'image/png' },
  { url: '/favicon.ico', sizes: 'any' },
];

/**
 * The `rel="shortcut icon"` target.
 *
 * `rel="shortcut icon"` is in no current specification; it is kept for old
 * crawlers and feed readers that look for nothing else, which is why it points
 * at the one format all of them can read.
 */
export const SHORTCUT_ICON = '/favicon.ico';

/** The iOS home-screen icon, at the size Safari asks for. */
export const APPLE_TOUCH_ICON: TypedAppIcon = {
  url: '/apple-touch-icon.png',
  sizes: '180x180',
  type: 'image/png',
};

/** An icon the installed-app surfaces use, with its manifest `purpose`. */
export type InstallIcon = TypedAppIcon & {
  /**
   * `'maskable'` lets a platform that crops icons to its own shape (Android's
   * adaptive icons) use the image rather than fall back to a screenshot of the
   * page. An image usable both ways is listed twice, once per purpose, which
   * is what Chrome's own guidance recommends over the single
   * `purpose: 'any maskable'` entry this manifest used to carry.
   */
  readonly purpose: 'any' | 'maskable';
};

/**
 * The launcher and splash-screen icons an install uses. These are larger than
 * any favicon, so they are their own images rather than a rescale of one.
 */
export const INSTALL_ICONS: readonly InstallIcon[] = [
  { url: '/nyxGPT-logo-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
  { url: '/nyxGPT-logo-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
  { url: '/nyxGPT-logo-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
];

/** A manifest `icons[]` entry: the same image, under the manifest's key names. */
export type ManifestIcon = {
  readonly src: string;
  readonly sizes: string;
  readonly type: string;
  readonly purpose?: 'any' | 'maskable';
};

/**
 * `metadata.icons` for `app/layout.tsx`, which is what emits the `<link>`
 * tags. Returned as fresh mutable objects because Next's `Metadata` type takes
 * ownership of what it is given.
 */
export function linkIcons(): {
  icon: Array<{ url: string; sizes: string; type?: string }>;
  shortcut: string;
  apple: string;
} {
  return {
    icon: FAVICONS.map((icon) => ({ ...icon })),
    shortcut: SHORTCUT_ICON,
    apple: APPLE_TOUCH_ICON.url,
  };
}

/**
 * The web app manifest's `icons` array, derived from the groups above.
 *
 * `favicon.ico` is dropped: a manifest entry is expected to declare a concrete
 * `type`, and a multi-resolution `.ico` has no useful one. Everything else is
 * offered smallest-first so an install can choose by size.
 */
export function manifestIcons(): ManifestIcon[] {
  const typedFavicons = FAVICONS.filter(
    (icon): icon is TypedAppIcon => icon.type !== undefined,
  );
  return [
    ...typedFavicons.map((icon) => ({ src: icon.url, sizes: icon.sizes, type: icon.type })),
    {
      src: APPLE_TOUCH_ICON.url,
      sizes: APPLE_TOUCH_ICON.sizes,
      type: APPLE_TOUCH_ICON.type,
    },
    ...INSTALL_ICONS.map((icon) => ({
      src: icon.url,
      sizes: icon.sizes,
      type: icon.type,
      purpose: icon.purpose,
    })),
  ];
}
