/**
 * A stamp identifying which build of the web client this document loaded
 * (#3857).
 *
 * The stamp is read out of the document rather than injected at build time,
 * so it works identically for every way nyxGPT's web tier is built (the
 * native `npm run build` in `nyxgpt ops install`, `web/Dockerfile`, the
 * Compose and Terraform paths) with no build-argument plumbing to keep in
 * sync -- and it is inert, not wrong, anywhere the markers are absent.
 *
 * The markers are Next.js's `webpack-*.js` and `main-app-*.js` bootstrap
 * chunks, which are emitted into the initial HTML of every App Router
 * document and whose content hashes change whenever the build changes (the
 * webpack runtime embeds the chunk-hash map). Dynamically injected chunk
 * scripts are deliberately excluded: they appear only after something lazy
 * has loaded, so including them would make the stamp depend on what the user
 * happened to do rather than on which build is running.
 */

const BUILD_SCRIPT_PATTERN = /\/_next\/static\/chunks\/(?:webpack|main-app)-[^/]+\.js(?:\?.*)?$/;

/** djb2, so the stamp stored in localStorage stays short. */
function hash(input: string): string {
  let h = 5381;
  for (let i = 0; i < input.length; i += 1) {
    h = ((h << 5) + h + input.charCodeAt(i)) | 0;
  }
  return (h >>> 0).toString(36);
}

/**
 * The running build's stamp, or `undefined` when this document carries no
 * recognisable build markers (dev mode, tests, a future asset layout) -- in
 * which case callers must do nothing rather than assume a change.
 */
export function buildFingerprint(doc: Document = document): string | undefined {
  const sources = Array.from(doc.querySelectorAll('script[src]'), (script) =>
    String(script.getAttribute('src')),
  )
    .filter((src) => BUILD_SCRIPT_PATTERN.test(src))
    .sort();
  if (sources.length === 0) return undefined;
  return hash(sources.join('|'));
}
