'use client';

// Support -> Docs -> one document (#3745).
//
// The HTML comes from the backend, which renders the packaged Markdown and
// strips active content from it (`nyxgpt.support._sanitize`) before it is
// injected here. Links between packaged documents were rewritten server-side
// onto `/support/docs/...`, so the tree browses as a unit with no checkout
// and no network.

import Link from 'next/link';
import { use, useEffect, useState } from 'react';

interface RenderedDocument {
  slug: string;
  title: string;
  html: string;
}

export default function SupportDocumentPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = use(params);
  const [document, setDocument] = useState<RenderedDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const res = await fetch(`/api/v1/support/docs/${encodeURIComponent(slug)}`);
        if (res.status === 404) {
          setError(`No document named "${slug}" ships with this version of nyxGPT.`);
          return;
        }
        if (!res.ok) {
          setError(`Could not load this document (HTTP ${res.status}).`);
          return;
        }
        setDocument(await res.json());
      } catch {
        setError('Could not reach the nyxGPT API to load this document.');
      } finally {
        setLoading(false);
      }
    }
    void load();
  }, [slug]);

  return (
    <div
      style={{
        minHeight: '100vh',
        background: 'var(--background)',
        color: 'var(--foreground)',
        padding: 24,
      }}
    >
      <div style={{ maxWidth: 900, margin: '0 auto' }}>
        <div style={{ marginBottom: 24 }}>
          <Link href="/support/docs" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← All documentation
          </Link>
        </div>

        {loading && <p style={{ opacity: 0.7 }}>Loading…</p>}

        {error && (
          <p style={{ color: '#d32f2f' }} role="alert">
            {error}
          </p>
        )}

        {document && (
          <article
            className="support-doc"
            // Safe by construction: this HTML is rendered from Markdown that
            // ships inside the wheel, and the backend strips scripts, event
            // handlers and javascript: URLs from it before returning it.
            dangerouslySetInnerHTML={{ __html: document.html }}
          />
        )}
      </div>
    </div>
  );
}
