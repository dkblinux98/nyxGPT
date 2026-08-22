/**
 * Format and interpret the versions the running stack reports.
 *
 * The API reports the installed package version (e.g. `3.0.0`); the UI shows
 * it with a leading `v`. Any value that already carries the prefix is left
 * alone, so a tagged version (`v3.0.0`) never renders as `vv3.0.0`.
 *
 * The `v` is only ever added to something that *is* a version number. A tier
 * can legitimately report a non-numeric build marker instead -- Compose
 * defaults `NYXGPT_WEB_VERSION` to the image tag, which is `local` on a stack
 * built from a tree -- and rendering that as `vlocal` presents a marker as a
 * version and reads as corruption. Such a value is passed through verbatim.
 */
export function formatVersion(version?: string | null): string {
  const trimmed = (version ?? '').trim();
  if (!trimmed) return '';
  if (/^v\d/i.test(trimmed)) return trimmed;
  return /^\d/.test(trimmed) ? `v${trimmed}` : trimmed;
}

/**
 * Which tier a version belongs to (#3982).
 *
 * This mirrors `version_channel()` in `src/nyxgpt/version.py`, and the two
 * must keep agreeing. The duplication is deliberate rather than proxied: the
 * API classifies its own version and sends `release_channel`, but the *web*
 * tier's version is resolved by the web tier itself (see
 * `web/src/lib/webVersion.ts`) and there is no Python in that path to ask.
 * Classifying one of the pair server-side and the other client-side would
 * make a same-tier stack look mixed whenever the two rules drifted, so both
 * rules live in code and both are tested against the same cases.
 */
export type VersionChannel = 'stable' | 'rc' | 'dev' | 'unknown';

/** `0.0.0` is nyxGPT's "could not be determined" sentinel, not a release. */
const UNKNOWN_VERSION = '0.0.0';

const FINAL_RELEASE = /^\d+(?:\.\d+)*$/;
/** PEP 440 `.devN` / `+local`, plus the `:local` image tag built from a tree. */
const DEV_BUILD = /\.dev\d+|\+|(?<![A-Za-z0-9])local(?![A-Za-z0-9])/i;
/** PEP 440 pre-release suffixes: `3.0.0rc13`, `3.0.0b2`, `3.0.0a1`. */
const PRERELEASE = /(?:a|b|rc)\d+$/i;

export function versionChannel(version?: string | null): VersionChannel {
  const text = (version ?? '').trim().replace(/^v/i, '');
  if (!text || text === UNKNOWN_VERSION) return 'unknown';
  // Order matters: `.devN` is itself a pre-release, and a working-tree build
  // reported as `rc` sends an operator hunting for a published candidate
  // that does not exist.
  if (DEV_BUILD.test(text)) return 'dev';
  if (PRERELEASE.test(text)) return 'rc';
  if (FINAL_RELEASE.test(text)) return 'stable';
  return 'dev';
}

/**
 * The short badge text for a channel.
 *
 * `stable` gets none -- it is the norm, and a badge on every normal install
 * would train the operator to ignore the one place a badge matters.
 * `unknown` gets none either: it is the resting state on any path that
 * cannot derive a version (containers without `NYXGPT_WEB_VERSION`, dev
 * servers) and on the first paint before `/api/info` answers, so badging it
 * would be permanent noise. Both are still named in full in the tooltip and
 * on the settings About surface, where an operator goes to look.
 */
export function channelLabel(channel: VersionChannel): string {
  switch (channel) {
    case 'rc':
      return 'rc';
    case 'dev':
      return 'dev';
    default:
      return '';
  }
}

/**
 * Whether a channel's version is a number two tiers can be compared on.
 *
 * `stable` and `rc` carry a released build number. `dev` carries a marker for
 * a build nothing published (`local`, `3.0.0.dev1`, `1.2.3+g0ab1`), and
 * `unknown` carries nothing at all; neither identifies *which* build, so
 * neither can establish that two tiers differ.
 */
function isComparable(channel: VersionChannel): boolean {
  return channel === 'stable' || channel === 'rc';
}

export type StackVersions = {
  /** The API process's installed version, from `GET /api/v1/info`. */
  apiVersion?: string | null;
  /** The web tier's own version, resolved by the web server itself. */
  webVersion?: string | null;
};

export type StackVersionSummary = {
  apiVersion: string;
  webVersion: string;
  apiChannel: VersionChannel;
  webChannel: VersionChannel;
  /** True when the two tiers are known to be running different versions. */
  mismatch: boolean;
  /**
   * True when both tiers reported a version that can be compared with the
   * other -- i.e. both are a real release or candidate number. False is the
   * answer for every stack where one side reports a build marker (`local`) or
   * nothing at all, and it is why `mismatch` stays false there.
   */
  comparable: boolean;
  /** The badge for the header: the channel, or `mixed` when the tiers differ. */
  badge: string;
  /** One line naming both versions -- the tooltip and the warning text. */
  detail: string;
};

/**
 * Compare the two tiers and describe what an operator is actually running.
 *
 * The mismatch flag is the point of the whole function (#3982): the 2.1.0
 * web / 3.0.0 API stack was diagnosed only because a feature was visibly
 * missing, and a false acceptance failure was nearly filed against that
 * feature. Anything the UI cannot establish is reported as `unknown` and
 * never as a mismatch -- claiming a mixed stack on missing data would send
 * an operator chasing a fault that is not there, which is the same class of
 * misdirection in the other direction.
 *
 * "Cannot establish" is stricter than "reported nothing" (#3982 review). A
 * mismatch is claimed only when BOTH tiers report a number that can be
 * compared with the other -- a release or a candidate. A tier reporting a
 * dev marker has said which channel it is on, not which build it is: the
 * default Compose stack sets `NYXGPT_WEB_VERSION` to the image tag `local`
 * against an API reporting `3.0.0`, and comparing those two strings made
 * every default `docker compose` stack display a permanent "Mixed stack"
 * warning about two images built from the same tree. A warning that is
 * always on is the one an operator learns to scroll past -- which would cost
 * exactly the incident this function exists for. Such a stack still shows
 * both versions and both channels; it simply does not allege a fault.
 */
export function describeStackVersions({
  apiVersion,
  webVersion,
}: StackVersions): StackVersionSummary {
  const api = (apiVersion ?? '').trim();
  const web = (webVersion ?? '').trim();
  const apiChannel = versionChannel(api);
  const webChannel = versionChannel(web);

  const comparable = isComparable(apiChannel) && isComparable(webChannel);
  // Compare on the normalised strings so `v3.0.0` and `3.0.0` -- the same
  // build described two ways -- are not reported as a mixed stack.
  const mismatch = comparable && formatVersion(api) !== formatVersion(web);

  const detail = mismatch
    ? `Mixed stack: web ${formatVersion(web)} / API ${formatVersion(api)}`
    : `web ${formatVersion(web) || 'unknown'} / API ${formatVersion(api) || 'unknown'}`;

  return {
    apiVersion: formatVersion(api),
    webVersion: formatVersion(web),
    apiChannel,
    webChannel,
    mismatch,
    comparable,
    badge: mismatch ? 'mixed' : channelLabel(apiChannel),
    detail,
  };
}
