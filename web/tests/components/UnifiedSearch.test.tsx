import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import { createRef } from 'react';
import {
  UnifiedSearch,
  UnifiedSearchRef,
  Session,
  MessageSearchResult,
} from '../../src/components/UnifiedSearch';

const sessions: Session[] = [
  { name: 'alpha', title: 'Alpha Session', summary: 'first summary', pinned: true },
  { name: 'beta', title: 'Beta Notes', summary: 'second summary' },
  { name: 'gamma' }, // no title/summary — exercises fallback branches
  { name: 'delta-1', title: 'Delta One' },
  { name: 'delta-2', title: 'Delta Two' },
  { name: 'delta-3', title: 'Delta Three' },
  { name: 'delta-4', title: 'Delta Four' },
];

const messageResult: MessageSearchResult = {
  session_name: 'alpha',
  session_title: 'Alpha Session',
  message_index: 3,
  role: 'user',
  content_preview: 'hello delta world',
  timestamp: null,
  matches: 2,
};

function makeFetch(results: MessageSearchResult[] = []) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ results }),
  }) as unknown as typeof fetch;
}

function renderSearch(overrides: Partial<Parameters<typeof UnifiedSearch>[0]> = {}) {
  const onSelectSession = vi.fn();
  const onMessageClick = vi.fn();
  const utils = render(
    <UnifiedSearch
      sessions={sessions}
      onSelectSession={onSelectSession}
      onMessageClick={onMessageClick}
      modKey="Ctrl"
      {...overrides}
    />
  );
  const input = screen.getByPlaceholderText(/Search sessions & messages/i);
  return { ...utils, input, onSelectSession, onMessageClick };
}

/** Find the deepest element whose full textContent equals t (survives
 * highlightMatches splitting text across <mark>/<span> nodes). */
function fullText(t: string): HTMLElement {
  const matches = screen.getAllByText((_, el) => el?.textContent === t);
  return matches[matches.length - 1] as HTMLElement;
}

function queryFullText(t: string): HTMLElement | null {
  const matches = screen.queryAllByText((_, el) => el?.textContent === t);
  return (matches[matches.length - 1] as HTMLElement) ?? null;
}

async function typeAndOpen(input: HTMLElement, value: string) {
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(300);
  });
}

describe('UnifiedSearch', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    global.fetch = makeFetch();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('renders the input with the modKey in the aria-label and title', () => {
    const { input } = renderSearch();
    expect(input).toHaveAttribute('aria-label', 'Search sessions and messages (Ctrl+F)');
    expect(input).toHaveAttribute('title', 'Search sessions and messages (Ctrl+F)');
  });

  it('exposes an imperative focus() via ref', () => {
    const ref = createRef<UnifiedSearchRef>();
    render(
      <UnifiedSearch
        ref={ref}
        sessions={sessions}
        onSelectSession={vi.fn()}
        onMessageClick={vi.fn()}
        modKey="Cmd"
      />
    );
    const input = screen.getByPlaceholderText(/Search sessions & messages/i);
    expect(document.activeElement).not.toBe(input);
    act(() => ref.current!.focus());
    expect(document.activeElement).toBe(input);
  });

  it('shows no dropdown until focused with a non-empty query', () => {
    const { input } = renderSearch();
    fireEvent.change(input, { target: { value: 'alpha' } });
    // not focused → isOpen false → no dropdown
    expect(screen.queryByText('Sessions')).not.toBeInTheDocument();
  });

  it('filters sessions by name, title, and summary (max 5 results)', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'delta');
    // 4 delta titles + 'hello delta world'? messages empty (fetch returns []),
    // name/title matches: delta-1..4 → 4 sessions shown
    expect(screen.getByText('Sessions')).toBeInTheDocument();
    expect(fullText('Delta One')).toBeInTheDocument();
    expect(fullText('Delta Four')).toBeInTheDocument();

    // summary match branch
    fireEvent.change(input, { target: { value: 'second summary' } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(screen.getByText('Beta Notes')).toBeInTheDocument();
  });

  it('caps the session list at five entries', async () => {
    const many = Array.from({ length: 8 }, (_, i) => ({
      name: `proj-${i}`,
      title: `Project ${i}`,
    }));
    const { input } = renderSearch({ sessions: many });
    await typeAndOpen(input, 'proj');
    expect(fullText('Project 0')).toBeInTheDocument();
    expect(fullText('Project 4')).toBeInTheDocument();
    expect(queryFullText('Project 5')).toBeNull();
    expect(queryFullText('Project 7')).toBeNull();
  });

  it('renders the pinned marker and highlights matches', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'alpha');
    expect(screen.getByText('📌')).toBeInTheDocument();
    const marks = document.querySelectorAll('mark');
    expect(marks.length).toBeGreaterThan(0);
    expect(marks[0].textContent!.toLowerCase()).toBe('alpha');
  });

  it('falls back to session name when there is no title', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'gamma');
    expect(screen.getByText('gamma')).toBeInTheDocument();
  });

  it('invokes onSelectSession and resets on session click', async () => {
    const { input, onSelectSession } = renderSearch();
    await typeAndOpen(input, 'beta');
    fireEvent.click(fullText('Beta Notes'));
    expect(onSelectSession).toHaveBeenCalledWith('beta');
    expect((input as HTMLInputElement).value).toBe('');
    expect(screen.queryByText('Sessions')).not.toBeInTheDocument();
  });

  it('debounces the message search API call', async () => {
    const { input } = renderSearch();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'he' } });
    fireEvent.change(input, { target: { value: 'hel' } });
    expect(global.fetch).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(String((global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0])).toContain(
      'query=hel'
    );
  });

  it('renders message results with role icon, title, badge, and preview highlight', async () => {
    global.fetch = makeFetch([
      messageResult,
      { ...messageResult, role: 'assistant', message_index: 4, matches: 1 },
      { ...messageResult, role: 'system', message_index: 5, matches: 1, session_title: null },
    ]);
    const { input } = renderSearch();
    await typeAndOpen(input, 'hello');
    expect(screen.getByText('Messages')).toBeInTheDocument();
    expect(screen.getByText('👤')).toBeInTheDocument();
    expect(screen.getByText('🤖')).toBeInTheDocument();
    expect(screen.getByText('⚙️')).toBeInTheDocument();
    // matches badge only for matches > 1
    expect(screen.getByText('2')).toBeInTheDocument();
    // session_title null → falls back to session_name
    expect(screen.getAllByText('alpha').length).toBeGreaterThan(0);
  });

  it('invokes onMessageClick and resets on message click', async () => {
    global.fetch = makeFetch([messageResult]);
    const { input, onMessageClick } = renderSearch();
    await typeAndOpen(input, 'hello');
    fireEvent.click(screen.getByText(/hello/i));
    expect(onMessageClick).toHaveBeenCalledWith('alpha', 3);
    expect((input as HTMLInputElement).value).toBe('');
  });

  it('shows "No results found" when nothing matches', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'zzz-no-match');
    expect(fullText('No results found for "zzz-no-match"')).toBeInTheDocument();
  });

  it('clears message results and skips the API for a blank query', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'alpha');
    fireEvent.change(input, { target: { value: '   ' } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    // only the first query hit the API
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('handles a non-OK response by logging and clearing results', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 }) as unknown as typeof fetch;
    const { input } = renderSearch();
    await typeAndOpen(input, 'boom');
    expect(consoleSpy).toHaveBeenCalledWith('Message search failed:', expect.any(Error));
    expect(screen.queryByText('Messages')).not.toBeInTheDocument();
  });

  it('handles a rejected fetch (network error)', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    global.fetch = vi.fn().mockRejectedValue(new Error('offline')) as unknown as typeof fetch;
    const { input } = renderSearch();
    await typeAndOpen(input, 'net');
    expect(consoleSpy).toHaveBeenCalled();
  });

  it('treats a missing results field as an empty list', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    }) as unknown as typeof fetch;
    const { input } = renderSearch();
    await typeAndOpen(input, 'gamma');
    // sessions section still renders; messages section absent
    expect(screen.getByText('Sessions')).toBeInTheDocument();
    expect(screen.queryByText('Messages')).not.toBeInTheDocument();
  });

  it('closes the dropdown on outside mousedown but not on inside mousedown', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'alpha');
    expect(screen.getByText('Sessions')).toBeInTheDocument();
    fireEvent.mouseDown(input); // inside → stays open
    expect(screen.getByText('Sessions')).toBeInTheDocument();
    fireEvent.mouseDown(document.body); // outside → closes
    expect(screen.queryByText('Sessions')).not.toBeInTheDocument();
  });

  it('closes the dropdown and blurs the input on Escape', async () => {
    const { input } = renderSearch();
    await typeAndOpen(input, 'alpha');
    act(() => input.focus());
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByText('Sessions')).not.toBeInTheDocument();
    expect(document.activeElement).not.toBe(input);
  });

  it('applies and clears hover styles on session and message rows', async () => {
    global.fetch = makeFetch([{ ...messageResult, content_preview: 'plain preview text' }]);
    const { input } = renderSearch();
    await typeAndOpen(input, 'delta');

    // happy-dom neither propagates mouseover/mouseout through React's
    // delegated listeners nor accepts `background = 'transparent'` in its
    // style parser, so run the rows' real handlers via their React props and
    // assert the out-handler's effect on a stub target.
    const propsOf = (el: HTMLElement) => {
      const key = Object.keys(el).find((k) => k.startsWith('__reactProps$'));
      return key
        ? (el as unknown as Record<string, Record<string, (e: unknown) => void>>)[key]
        : undefined;
    };
    const rowOf = (el: HTMLElement): HTMLElement => {
      let n: HTMLElement | null = el;
      while (n && typeof propsOf(n)?.onMouseOver !== 'function') n = n.parentElement;
      if (!n) throw new Error('no ancestor with hover handlers');
      return n;
    };
    const checkHover = (start: HTMLElement) => {
      const el = rowOf(start);
      propsOf(el)!.onMouseOver({ currentTarget: el });
      expect(el.style.background).toContain('button-hover');
      const stub = { style: { background: '' } };
      propsOf(el)!.onMouseOut({ currentTarget: stub });
      expect(stub.style.background).toBe('transparent');
    };

    checkHover(fullText('Delta One'));
    checkHover(screen.getByText('plain preview text'));
  });

  it('sets and clears the focus outline styles', () => {
    const { input } = renderSearch();
    fireEvent.focus(input);
    expect((input as HTMLInputElement).style.outline).toContain('var(--success)');
    fireEvent.blur(input);
    expect((input as HTMLInputElement).style.outline).toContain('transparent');
  });

  it('shows the searching indicator while a message search is in flight', async () => {
    vi.useRealTimers();
    let resolveFetch: (v: unknown) => void = () => {};
    global.fetch = vi.fn().mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      })
    ) as unknown as typeof fetch;
    const { input } = renderSearch();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'slow' } });
    await waitFor(() => expect(global.fetch).toHaveBeenCalled(), { timeout: 2000 });
    expect(screen.getByText('searching...')).toBeInTheDocument();
    await act(async () => {
      resolveFetch({ ok: true, json: async () => ({ results: [] }) });
    });
    await waitFor(() => expect(screen.queryByText('searching...')).not.toBeInTheDocument());
  });
});
