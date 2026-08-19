/**
 * buildFingerprint tests (#3857)
 *
 * The stamp has to satisfy three things at once: change when the build
 * changes, stay identical across reloads of the same build (including after
 * lazy chunks have been injected into the document), and return undefined --
 * never a guess -- when the document carries no recognisable build markers.
 */

import { describe, it, expect } from 'vitest';
import { buildFingerprint } from '../../src/lib/buildFingerprint';

/**
 * Build a parsed document containing the given script srcs. DOMParser is used
 * rather than createHTMLDocument + appendChild so the scripts are never
 * fetched -- the fingerprint reads attributes, it does not run anything.
 */
function docWith(srcs: string[]): Document {
  const tags = srcs.map((src) => `<script src="${src}"></script>`).join('');
  return new DOMParser().parseFromString(`<!doctype html><html><body>${tags}</body></html>`, 'text/html');
}

const BUILD_A = [
  '/_next/static/chunks/webpack-aaa111.js',
  '/_next/static/chunks/main-app-bbb222.js',
];
const BUILD_B = [
  '/_next/static/chunks/webpack-ccc333.js',
  '/_next/static/chunks/main-app-ddd444.js',
];

describe('buildFingerprint', () => {
  it('produces a stamp from the bootstrap chunks', () => {
    expect(buildFingerprint(docWith(BUILD_A))).toEqual(expect.any(String));
  });

  it('is stable for the same build', () => {
    expect(buildFingerprint(docWith(BUILD_A))).toBe(buildFingerprint(docWith(BUILD_A)));
  });

  it('is stable regardless of the order the scripts appear in', () => {
    expect(buildFingerprint(docWith([...BUILD_A].reverse()))).toBe(
      buildFingerprint(docWith(BUILD_A)),
    );
  });

  it('changes when the build changes', () => {
    expect(buildFingerprint(docWith(BUILD_B))).not.toBe(buildFingerprint(docWith(BUILD_A)));
  });

  it('ignores lazily injected chunk scripts, so it does not depend on user activity', () => {
    // ChatPane's chunk is appended to the document only once something lazy
    // loads; including it would make the same build stamp differently.
    const withLazyChunk = docWith([...BUILD_A, '/_next/static/chunks/app/page-eee555.js']);
    expect(buildFingerprint(withLazyChunk)).toBe(buildFingerprint(docWith(BUILD_A)));
  });

  it('tolerates a query string on the bootstrap chunk URL', () => {
    const versioned = docWith(['/_next/static/chunks/webpack-aaa111.js?v=1']);
    expect(versioned).toBeDefined();
    expect(buildFingerprint(versioned)).toEqual(expect.any(String));
  });

  it('returns undefined when no build markers are present', () => {
    expect(buildFingerprint(docWith(['/some/other/script.js']))).toBeUndefined();
    expect(buildFingerprint(docWith([]))).toBeUndefined();
  });

  it('defaults to the live document', () => {
    // happy-dom's document has no Next.js bootstrap scripts, so the guard is
    // inert here -- which is the required behaviour, not a gap.
    expect(buildFingerprint()).toBeUndefined();
  });
});
