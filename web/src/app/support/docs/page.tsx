'use client';

// Support -> Docs: the index of the documentation packaged with this install
// (#3745).
//
// A user who installed nyxGPT from PyPI or Homebrew has no repo checkout, so
// this is their documentation. It is served out of the wheel, which means the
// documents listed here match the version that is running by construction,
// and they render with no network access.

import Link from 'next/link';
import { useEffect, useState } from 'react';

interface DocumentSummary {
  slug: string;
  title: string;
  summary: string;
}

// The index is grouped, and the grouping is data from the backend
// (`support.DOC_SECTIONS`) rather than anything inferred here (#3809): a flat
// alphabetical list of every packaged file is what shipped agent/CI process
// docs to readers looking for product help.
interface DocumentSection {
  title: string;
  documents: DocumentSummary[];
}

interface SupportContext {
  environment: { version: string; platform: string; python: string };
  network_note: string;
}

export default function SupportDocsIndexPage() {
  const [sections, setSections] = useState<DocumentSection[]>([]);
  const [ungrouped, setUngrouped] = useState(false);
  const [context, setContext] = useState<SupportContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const res = await fetch('/api/v1/support/docs');
        if (!res.ok) {
          setError(`Could not load the documentation index (HTTP ${res.status}).`);
          return;
        }
        const data = await res.json();
        // An older backend answers with `documents` only; render that as one
        // unnamed group rather than an empty page -- but say so on the page
        // (`ungrouped`), because an unlabelled flat list is exactly the
        // symptom #3809 fixed and it must not read as that defect returning.
        const grouped: DocumentSection[] = data.sections ?? [];
        if (grouped.length > 0) {
          setSections(grouped);
        } else if (data.documents?.length) {
          setSections([{ title: '', documents: data.documents }]);
          setUngrouped(true);
        } else {
          setSections([]);
        }
      } catch {
        setError('Could not reach the nyxGPT API to load the documentation index.');
      } finally {
        setLoading(false);
      }
    }
    void load();
  }, []);

  // The issue link is a separate, non-blocking fetch: docs must still render
  // when this fails, since filing needs the network and reading does not.
  useEffect(() => {
    async function loadContext() {
      try {
        const res = await fetch('/api/v1/support/context');
        if (res.ok) setContext(await res.json());
      } catch {
        // Leave the link hidden rather than showing a broken one.
      }
    }
    void loadContext();
  }, []);

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
          <h1 style={{ fontSize: 28, fontWeight: 600, margin: 0, marginBottom: 8 }}>
            Documentation
          </h1>
          <Link href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to chat
          </Link>
        </div>

        {context && (
          <div
            style={{
              border: '1px solid var(--border)',
              borderRadius: 8,
              padding: 16,
              marginBottom: 24,
              fontSize: 13,
            }}
          >
            <div style={{ marginBottom: 8 }}>
              Packaged with nyxGPT <strong>{context.environment.version}</strong> — these
              documents match the version you are running and work offline.
            </div>
            {/* nyxGPT's own intake page, not github.com's compose form
                (#3811). This link used to leave the app for GitHub, which is
                the behaviour the owner rejected in acceptance -- the menu
                entry was fixed and this one, on the page a reader lands on
                when the docs did not answer their question, was not. */}
            <Link href="/support/new" style={{ color: '#0066cc', textDecoration: 'none' }}>
              🐛 File an Issue
            </Link>
            <div style={{ opacity: 0.7, marginTop: 6 }}>{context.network_note}</div>
          </div>
        )}

        {loading && <p style={{ opacity: 0.7 }}>Loading documentation…</p>}

        {error && (
          <p style={{ color: '#d32f2f' }} role="alert">
            {error}
          </p>
        )}

        {!loading && !error && sections.length === 0 && (
          <p style={{ opacity: 0.7 }}>No documentation shipped with this install.</p>
        )}

        {ungrouped && (
          <p role="status" style={{ opacity: 0.7, fontSize: 13, marginBottom: 16 }}>
            This nyxGPT API is older than the web interface and did not send the grouped
            index, so every document is listed together below.
          </p>
        )}

        {sections.map((section, index) => (
          <section key={section.title || `section-${index}`} style={{ marginBottom: 32 }}>
            {/*
              A real heading, not a caption (#3809 acceptance): the first
              attempt rendered the section title at 13px, uppercase and at 60%
              opacity -- smaller and fainter than the 15px document titles
              underneath it -- so the page still read as one continuous list.
              A group heading has to outweigh the items it groups, which is
              what the size, full opacity and the rule below it are for.
            */}
            {section.title && (
              <h2
                style={{
                  fontSize: 20,
                  fontWeight: 600,
                  margin: '0 0 8px',
                  paddingBottom: 8,
                  borderBottom: '2px solid var(--border)',
                }}
              >
                {section.title}
              </h2>
            )}
            <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
              {section.documents.map((doc) => (
                <li
                  key={doc.slug}
                  style={{
                    borderBottom: '1px solid var(--border-light)',
                    padding: '12px 0',
                  }}
                >
                  <Link
                    href={`/support/docs/${doc.slug}`}
                    style={{
                      color: 'var(--foreground)',
                      textDecoration: 'none',
                      fontWeight: 600,
                      fontSize: 15,
                    }}
                  >
                    {doc.title}
                  </Link>
                  {doc.summary && (
                    <div style={{ opacity: 0.7, fontSize: 13, marginTop: 4 }}>{doc.summary}</div>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}
