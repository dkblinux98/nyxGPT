/**
 * The version of the *web tier itself* -- the Next.js server answering this
 * request -- resolved at runtime on the server (#3982).
 *
 * Why this exists at all: until now the header rendered `release_version`
 * from `GET /api/v1/info`, which is the **API process's** installed package
 * version. One number was shown for two independently installed services.
 * During acceptance a 2.1.0 `nyxgpt-web` keg served :3000 against a
 * 3.0.0-line `nyxgpt-api` on :8000 and the header read `v3.0.0` -- it
 * described neither the page rendering it nor, usefully, the install. A
 * version indicator that can silently describe a different build than the
 * one running is worse than none during an incident.
 *
 * Why resolution is *derived from where the server is running* rather than
 * injected at build time: the web tier is built four different ways (the
 * Homebrew keg's own `npm run build`, the Linux native systemd build,
 * `web/Dockerfile`, and `npm run dev` from a checkout), and only one of them
 * -- the Dockerfile -- has anywhere natural to thread a build argument
 * through. Adding a stamp step to the other three means editing the install
 * paths, which is exactly the "build-argument plumbing to keep in sync" that
 * `buildFingerprint.ts` (#3857) already declined for the same reason. Both
 * native installs already write the version into the directory they serve
 * from, so it is there to be read:
 *
 *   macOS keg    /opt/homebrew/Cellar/nyxgpt-web/3.0.0rc13/libexec
 *   macOS rc keg /opt/homebrew/Cellar/nyxgpt-web@3.0.0rc/3.0.0rc13/libexec
 *   Linux native ~/.nyxGPT/... /build/nyxgpt-web-3.0.0rc13
 *
 * `NYXGPT_WEB_VERSION` stays as the explicit override for the paths that
 * have no such directory (containers, Kubernetes, Compose), and takes
 * precedence so an operator can always assert the truth by hand.
 *
 * `web/package.json`'s own `version` is deliberately NOT a fallback: it is a
 * fixed `0.1.0` that no release path updates, so returning it would report a
 * confident wrong answer -- the exact failure mode being fixed. When nothing
 * identifies the build, this returns null and the UI says "unknown".
 */

/** How the reported web version was established, for the UI to explain. */
export type WebVersionSource = 'env' | 'homebrew-keg' | 'native-build' | 'unknown';

export type WebVersion = {
  /** The web tier's own version, or null when nothing identifies it. */
  version: string | null;
  source: WebVersionSource;
};

/**
 * `/opt/homebrew/Cellar/<formula>/<version>/...`, where `<formula>` is
 * `nyxgpt-web` or a versioned rc formula such as `nyxgpt-web@3.0.0rc`
 * (#3853). The keg *directory* holds the full version including the rc
 * suffix, which is the number acceptance testing actually needs.
 */
const KEG_PATH = /[/\\]Cellar[/\\]nyxgpt-web(?:@[^/\\]+)?[/\\]([^/\\]+)/;

/**
 * The Linux native install's build directory, named `nyxgpt-web-<version>`
 * by `_install_native_web_systemd` when it extracts the service tarball.
 */
const NATIVE_BUILD_PATH = /[/\\]nyxgpt-web-([^/\\]+)(?:[/\\]|$)/;

/**
 * Resolve the running web tier's version.
 *
 * Pure in its inputs so it can be tested without a real install tree: the
 * environment and working directory are parameters, defaulted to the live
 * process only at the call site's convenience.
 */
export function resolveWebVersion(
  env: NodeJS.ProcessEnv = process.env,
  cwd: string = typeof process !== 'undefined' ? process.cwd() : '',
): WebVersion {
  const declared = (env.NYXGPT_WEB_VERSION ?? '').trim();
  if (declared) return { version: declared, source: 'env' };

  const path = cwd ?? '';

  const keg = KEG_PATH.exec(path);
  if (keg) return { version: keg[1], source: 'homebrew-keg' };

  const native = NATIVE_BUILD_PATH.exec(path);
  if (native) return { version: native[1], source: 'native-build' };

  return { version: null, source: 'unknown' };
}
