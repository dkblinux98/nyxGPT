/**
 * Format the running app version for display.
 *
 * The API reports the installed package version (e.g. `3.0.0`); the UI shows
 * it with a leading `v`. Any value that already carries the prefix is left
 * alone, so a tagged version (`v3.0.0`) never renders as `vv3.0.0`.
 */
export function formatVersion(version?: string | null): string {
  const trimmed = (version ?? '').trim();
  if (!trimmed) return '';
  return /^v/i.test(trimmed) ? trimmed : `v${trimmed}`;
}
