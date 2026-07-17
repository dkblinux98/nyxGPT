import { describe, expect, it } from 'vitest';
import { extractSseEvents, safeJsonParse } from '../../../src/app/lib/sse';

/**
 * Regression tests for #3112: "SyntaxError: Unexpected end of JSON input"
 * in ChatPane SSE parsing. These import the real shared parser used by
 * ChatPane — reverting the implementation breaks these tests.
 */

describe('extractSseEvents', () => {
  it('parses a complete message event and empties the buffer', () => {
    const { events, rest } = extractSseEvents(
      'event: text\ndata: {"content":"hello"}\n\n'
    );
    expect(events).toEqual([{ event: 'text', data: '{"content":"hello"}' }]);
    expect(rest).toBe('');
  });

  it('keeps an incomplete frame in the buffer instead of emitting it (#3112 fragmentation)', () => {
    // JSON split mid-token across network chunks: first chunk has no
    // terminating blank line, so nothing must be emitted yet.
    const chunk1 = 'event: text\ndata: {"content":"hel';
    const first = extractSseEvents(chunk1);
    expect(first.events).toEqual([]);
    expect(first.rest).toBe(chunk1);

    // Second chunk completes the frame; parsing now succeeds.
    const second = extractSseEvents(first.rest + 'lo"}\n\n');
    expect(second.events).toEqual([{ event: 'text', data: '{"content":"hello"}' }]);
    expect(second.rest).toBe('');
  });

  it('skips comment-only keep-alive frames instead of emitting an empty default message (#3112 empty data)', () => {
    const { events, rest } = extractSseEvents(': ping\n\n: keep-alive\n\n');
    expect(events).toEqual([]);
    expect(rest).toBe('');
  });

  it('emits typed events with empty data rather than dropping them', () => {
    const { events } = extractSseEvents('event: heartbeat\n\n');
    expect(events).toEqual([{ event: 'heartbeat', data: '' }]);
  });

  it('accumulates multi-line data fields per the SSE spec', () => {
    const { events } = extractSseEvents(
      'event: text\ndata: {"content":\ndata: "hello"}\n\n'
    );
    expect(events).toHaveLength(1);
    expect(JSON.parse(events[0].data)).toEqual({ content: 'hello' });
  });

  it('handles CRLF line endings for framing and fields', () => {
    const { events, rest } = extractSseEvents(
      'event: text\r\ndata: {"content":"crlf"}\r\n\r\n'
    );
    expect(events).toEqual([{ event: 'text', data: '{"content":"crlf"}' }]);
    expect(rest).toBe('');
  });

  it('parses multiple events from one buffer and retains the trailing partial', () => {
    const { events, rest } = extractSseEvents(
      'data: {"content":"a"}\n\ndata: {"content":"b"}\n\ndata: {"content":"c'
    );
    expect(events.map((e) => e.event)).toEqual(['message', 'message']);
    expect(rest).toBe('data: {"content":"c');
  });

  it('defaults the event type to "message" when only data is present', () => {
    const { events } = extractSseEvents('data: {"content":"x"}\n\n');
    expect(events).toEqual([{ event: 'message', data: '{"content":"x"}' }]);
  });

  it('skips a whitespace-only frame between two real events', () => {
    const { events } = extractSseEvents(
      'data: {"content":"a"}\n\n   \n\ndata: {"content":"b"}\n\n'
    );
    expect(events).toHaveLength(2);
    expect(events.map((e) => e.data)).toEqual([
      '{"content":"a"}',
      '{"content":"b"}',
    ]);
  });

  it('ignores id: and retry: fields, which are not yet supported', () => {
    const { events } = extractSseEvents('id: 5\nretry: 1000\ndata: hi\n\n');
    expect(events).toEqual([{ event: 'message', data: 'hi' }]);
  });

  it('strips a stray trailing CR that is not part of the frame-ending CRLFCRLF', () => {
    // "data: hi" + a bare CR, then a genuine CRLFCRLF frame terminator. The
    // frame-delimiter split only consumes the CR immediately adjacent to
    // the terminating LFs, so this one lands mid-line and must still be
    // stripped by the per-line CR handling.
    const { events } = extractSseEvents('data: hi\r\r\n\r\n');
    expect(events).toEqual([{ event: 'message', data: 'hi' }]);
  });

  it('does not strip a non-space character following the data: field name', () => {
    const { events } = extractSseEvents('data:{"content":"nospace"}\n\n');
    expect(events).toEqual([{ event: 'message', data: '{"content":"nospace"}' }]);
  });
});

describe('safeJsonParse', () => {
  it('returns null for empty input instead of throwing (#3112 crash case)', () => {
    expect(safeJsonParse('')).toBeNull();
  });

  it('returns null for truncated JSON instead of throwing', () => {
    expect(safeJsonParse('{"content":"hel')).toBeNull();
  });

  it('parses valid JSON', () => {
    expect(safeJsonParse<{ content: string }>('{"content":"ok"}')).toEqual({
      content: 'ok',
    });
  });
});
