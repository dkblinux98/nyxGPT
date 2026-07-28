// Model resource-size hints for the pull dialog.
//
// Lives outside page.tsx because Next.js page files may only export a default
// component plus a fixed set of framework exports -- a named export here fails
// the Next.js Page type check and breaks `next build`. Extracted so both
// models/page.tsx and its test can import it.
//
// Rough parameter-count -> RAM guidance, matching docs/performance.md's
// Small/Medium/Large model tiers (and its per-tag table, where llama3.1:8b
// is listed at 8-16 GB, i.e. Large). Best-effort only: quantization, context
// length, and host overhead all shift the real number.

export function estimateModelResourceHint(modelName: string): string | null {
  const match = modelName.match(/(\d+(?:\.\d+)?)b(?:$|[^a-z0-9])/i);
  if (!match) return null;

  const params = parseFloat(match[1]);
  if (params < 1) {
    return 'Small model (~1-2 GB RAM)';
  }
  if (params < 8) {
    return 'Medium model (~4-8 GB RAM)';
  }
  return 'Large model (~8-16+ GB RAM) — make sure this host has enough free memory before pulling, or chat requests may fail once you select it';
}
