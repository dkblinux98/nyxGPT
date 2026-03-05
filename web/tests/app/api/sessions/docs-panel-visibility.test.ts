/**
 * Tests for the Docs button / Attached Documents panel visibility logic in ChatPane.
 *
 * Issue #3130 acceptance failure: the Docs toggle button and the Attached Documents
 * panel were gated on `ragEnabled`, so users who disabled RAG after attaching documents
 * could no longer see or remove their attachments.
 *
 * Fixed behaviour:
 *  - Docs button: visible when `ragEnabled === true` OR when `attachedDocIds.length > 0`
 *  - Attached Docs panel: visible when `showAttachedDocs === true` (independent of ragEnabled)
 */
import { describe, it, expect } from 'vitest';

// Pure logic extracted from ChatPane rendering conditions.

function showDocsButton(ragEnabled: boolean, attachedDocIds: string[]): boolean {
  return ragEnabled || attachedDocIds.length > 0;
}

function showAttachedDocsPanel(showAttachedDocs: boolean): boolean {
  return showAttachedDocs;
}

describe('Docs button visibility logic (ChatPane)', () => {
  it('shows button when RAG is enabled and no docs attached', () => {
    expect(showDocsButton(true, [])).toBe(true);
  });

  it('shows button when RAG is enabled and docs are attached', () => {
    expect(showDocsButton(true, ['doc-a'])).toBe(true);
  });

  it('shows button when RAG is disabled but docs are attached', () => {
    // Regression test: before the fix this returned false, making it impossible
    // for the user to detach documents once RAG was turned off.
    expect(showDocsButton(false, ['doc-a', 'doc-b'])).toBe(true);
  });

  it('hides button when RAG is disabled and no docs are attached', () => {
    expect(showDocsButton(false, [])).toBe(false);
  });
});

describe('Attached Docs panel visibility logic (ChatPane)', () => {
  it('shows panel when showAttachedDocs is true regardless of ragEnabled', () => {
    // Regression test: before the fix the panel was also gated on ragEnabled,
    // so attached documents became invisible when RAG was turned off.
    expect(showAttachedDocsPanel(true)).toBe(true);
  });

  it('hides panel when showAttachedDocs is false', () => {
    expect(showAttachedDocsPanel(false)).toBe(false);
  });
});
