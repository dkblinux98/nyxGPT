// Search-term highlighter for the session list.
//
// Lives outside page.tsx because Next.js page files may only export a default
// component plus a fixed set of framework exports -- a named export here fails
// the Next.js Page type check and breaks `next build`. Extracted so both
// page.tsx and its test can import it.

export function highlightText(text: string, search: string) {
  if (!search) return text;
  const parts = text.split(new RegExp(`(${search})`, 'gi'));
  return parts.map((part, i) =>
    part.toLowerCase() === search.toLowerCase() ? (
      <mark key={i} style={{ background: 'var(--highlight)', padding: '0 2px' }}>
        {part}
      </mark>
    ) : (
      part
    )
  );
}
