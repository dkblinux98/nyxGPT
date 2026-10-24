import type { MetadataRoute } from 'next';

import { manifestIcons } from '../lib/appIcons';

/**
 * The W3C web app manifest, generated rather than hand-written (#4235).
 *
 * This replaced a static `web/public/manifest.json` whose `icons` array was a
 * third hand-maintained copy of the icon list -- one that could drift away
 * from the `<link rel="icon">` tags in `app/layout.tsx` without any single
 * edit looking wrong. The icons now come from `lib/appIcons.ts`, which is the
 * only place in the app an icon URL is written down.
 *
 * Next serves this convention at **`/manifest.webmanifest`** (the registered
 * extension for `application/manifest+json`) and emits the
 * `<link rel="manifest">` itself, which is why `layout.tsx` no longer sets
 * `metadata.manifest`: doing both would put two manifest links in the head.
 * The old `/manifest.json` URL is gone, and nothing referenced it but that
 * one `<link>` -- browsers and installed PWAs re-read the manifest from
 * whatever the page's link element points at.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'nyxGPT',
    short_name: 'nyxGPT',
    description: 'An intelligent chat application with RAG capabilities',
    start_url: '/',
    display: 'standalone',
    background_color: '#ffffff',
    theme_color: '#0070f3',
    orientation: 'portrait-primary',
    icons: manifestIcons().map((icon) => ({ ...icon })),
  };
}
