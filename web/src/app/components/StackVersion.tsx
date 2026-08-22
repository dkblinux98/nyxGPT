'use client';

import { describeStackVersions } from '../lib/version';

/**
 * The header's version surface (#3982).
 *
 * Split into two pieces on purpose. `VersionLabel` sits inside the model
 * selector button, where the version has always been shown; the mismatch
 * warning sits *outside* it as `StackMismatchWarning`, because it is the one
 * thing on this row an operator needs to be able to read (and read out) at
 * the moment the stack is wrong, and burying it in a control that opens a
 * model dropdown when clicked would put it behind an interaction.
 *
 * What each says:
 *
 *   stable          nyxGPT v3.0.0
 *   candidate       nyxGPT v3.0.0rc13 [rc]
 *   working tree    nyxGPT v3.0.0.dev1 [dev]
 *   mixed stack     nyxGPT v3.0.0 [mixed]  ⚠ Mixed stack: web v2.1.0 / API v3.0.0
 *
 * The rc suffix is carried through verbatim from package metadata -- it is
 * the entire difference between "the candidate I am testing" and "the
 * release", and truncating it to `v3.0.0` is what made the two
 * indistinguishable in the first place.
 */

type Props = {
  /** The API process's version, from `GET /api/v1/info`. */
  releaseVersion?: string | null;
  /** The web tier's own version, stamped in by the web server. */
  webVersion?: string | null;
};

const badgeStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  lineHeight: 1,
  padding: '2px 5px',
  borderRadius: 4,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
};

export function VersionLabel({ releaseVersion, webVersion }: Props) {
  const stack = describeStackVersions({ apiVersion: releaseVersion, webVersion });

  return (
    <>
      {stack.apiVersion && (
        // `title` carries both versions on every install, not just the
        // broken ones: an operator who suspects a mixed stack can confirm
        // the pair without needing the warning to have fired.
        <span style={{ opacity: 0.6 }} title={stack.detail}>
          {stack.apiVersion}
        </span>
      )}
      {stack.badge && (
        <span
          data-testid="version-channel-badge"
          title={stack.detail}
          style={{
            ...badgeStyle,
            background: stack.mismatch ? 'var(--error-bg)' : 'var(--border)',
            color: stack.mismatch ? 'var(--error-text)' : 'var(--foreground)',
          }}
        >
          {stack.badge}
        </span>
      )}
    </>
  );
}

export function StackMismatchWarning({ releaseVersion, webVersion }: Props) {
  const stack = describeStackVersions({ apiVersion: releaseVersion, webVersion });
  if (!stack.mismatch) return null;

  return (
    <span
      role="status"
      data-testid="version-mismatch-warning"
      style={{
        ...badgeStyle,
        textTransform: 'none',
        fontWeight: 600,
        letterSpacing: 0,
        padding: '4px 8px',
        borderRadius: 12,
        border: '1px solid var(--error-text)',
        background: 'var(--error-bg)',
        color: 'var(--error-text)',
      }}
    >
      ⚠ {stack.detail}
    </span>
  );
}
