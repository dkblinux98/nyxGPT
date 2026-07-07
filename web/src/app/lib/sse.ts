/**
 * Shared SSE (Server-Sent Events) stream parsing utilities.
 *
 * Extracted from ChatPane's inline parsers to fix #3112:
 * "SyntaxError: Unexpected end of JSON input" during streaming.
 *
 * Root causes addressed:
 * 1. Frames with no `data:` line (e.g. comment-only keep-alives like
 *    `: ping`) fell through to the default "message" branch and were fed
 *    to JSON.parse as an empty string, which throws
 *    "Unexpected end of JSON input".
 * 2. Multi-line `data:` fields were overwritten line-by-line instead of
 *    accumulated, so JSON payloads split across data lines (allowed by
 *    the SSE spec) produced truncated, unparseable fragments.
 * 3. CRLF (`\r\n`) line endings broke both event framing and field
 *    parsing, leaving the whole stream buffered and unparsed.
 */

export interface SseEvent {
  /** Event type from the `event:` field; defaults to "message". */
  event: string;
  /** Accumulated payload from all `data:` lines, joined with newlines. */
  data: string;
}

/**
 * Split accumulated stream text into complete SSE events plus the
 * remaining (possibly incomplete) tail to keep buffering.
 *
 * Events are delimited by a blank line (\n\n or \r\n\r\n). Comment lines
 * (starting with ":") are ignored. Multiple `data:` lines accumulate per
 * the SSE specification. Frames that contain no `data:` field yield an
 * event with an empty `data` string — callers must not JSON.parse those.
 */
export function extractSseEvents(buffer: string): {
  events: SseEvent[];
  rest: string;
} {
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() ?? '';
  const events: SseEvent[] = [];

  for (const part of parts) {
    if (!part.trim()) continue;

    let event = 'message';
    let sawData = false;
    const dataLines: string[] = [];

    for (const rawLine of part.split(/\r?\n/)) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      if (line.startsWith(':')) continue; // SSE comment / keep-alive
      if (line.startsWith('event:')) {
        event = line.substring(6).trim();
      } else if (line.startsWith('data:')) {
        sawData = true;
        // Strip the field name and a single optional leading space only —
        // interior whitespace is part of the payload.
        let value = line.substring(5);
        if (value.startsWith(' ')) value = value.substring(1);
        dataLines.push(value);
      }
      // id: and retry: fields are ignored for now
    }

    // Comment-only frames (keep-alives) carry no event or data — skip
    // them entirely rather than emitting a bogus default "message" event.
    if (!sawData && event === 'message') continue;

    events.push({ event, data: dataLines.join('\n').trim() });
  }

  return { events, rest };
}

/**
 * Parse an SSE data payload as JSON, returning null instead of throwing
 * on empty or malformed input. Callers should skip null results.
 */
export function safeJsonParse<T>(data: string): T | null {
  if (!data) return null;
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}
