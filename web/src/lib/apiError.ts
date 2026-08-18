/**
 * Turn an API error payload (or a caught value) into text an operator can read.
 *
 * Every nyxGPT API error arrives in one of a small number of shapes, and only
 * one of them is a bare string:
 *
 *   - `{"error": {"code", "message", "details", "request_id"}}` -- the API's
 *     own envelope, produced for every `HTTPException` by `http_exception_handler`
 *     in `src/nyxgpt/app.py`. `details` carries a non-string `detail` (a dict or
 *     list) and `message` degrades to the generic "Request failed" in that case.
 *   - `{"error": "..."}` -- older hand-rolled payloads.
 *   - `{"detail": "..."}` -- FastAPI's own refusals, raised before the app's
 *     handler runs.
 *   - `{"detail": [{"loc", "msg", ...}]}` -- FastAPI request-validation errors.
 *   - `{"message": "..."}` -- ok/message action responses reused as an error.
 *
 * Interpolating any of the object-shaped ones into a template literal or
 * passing it to `new Error()` renders the card as `[object Object]`, i.e. it
 * hides exactly the sentence the operator needs: #3831 saw a canary rollout
 * blocked by `FailedScheduling: Insufficient memory` reported to the operator
 * as `[object Object]`, making a scheduling failure look like a broken feature.
 *
 * These helpers are the single implementation of that unwrapping; pages must
 * not re-derive it (four near-copies existed before #3831).
 */

/** Render an envelope's `details` field (dict, list or string) as text. */
function detailsText(details: unknown): string {
  if (typeof details === 'string') return details.trim();
  if (typeof details === 'number' || typeof details === 'boolean') return String(details);
  if (Array.isArray(details)) {
    return details
      .map((item) => itemText(item))
      .filter(Boolean)
      .join('; ');
  }
  if (details && typeof details === 'object') {
    const record = details as Record<string, unknown>;
    // FastAPI's validation payload nests the field errors one level down, and
    // the config endpoints use the same `{"errors": [...]}` shape.
    if (Array.isArray(record.errors)) {
      return detailsText(record.errors);
    }
    const nested = record.message ?? record.msg ?? record.error;
    if (typeof nested === 'string') return nested.trim();
    // Unknown object: JSON is ugly but it is information, unlike "[object Object]".
    return safeJson(record);
  }
  return '';
}

/** Render one entry of a `details`/`detail` list, including FastAPI's `{loc, msg}` items. */
function itemText(item: unknown): string {
  if (typeof item === 'string') return item.trim();
  if (item && typeof item === 'object') {
    const record = item as Record<string, unknown>;
    const msg = record.msg ?? record.message;
    if (typeof msg === 'string') {
      const loc = Array.isArray(record.loc) ? record.loc.join('.') : '';
      return loc ? `${loc}: ${msg}` : msg;
    }
    return safeJson(record);
  }
  return item === null || item === undefined ? '' : String(item);
}

/** `JSON.stringify` that never throws (cyclic payloads) and never yields "[object Object]". */
function safeJson(value: unknown): string {
  try {
    // Only ever called with an object, so `stringify` returns a string here;
    // "{}" means the payload said nothing, which is the fallback's job.
    const json = JSON.stringify(value);
    return json === '{}' ? '' : json;
  } catch {
    return '';
  }
}

/** Unwrap the API's error envelope: `message`, enriched with `details` when present. */
function envelopeText(error: unknown): string {
  if (typeof error === 'string') return error.trim();
  if (!error || typeof error !== 'object') return '';
  const record = error as Record<string, unknown>;
  const message = typeof record.message === 'string' ? record.message.trim() : '';
  const details = detailsText(record.details);
  if (message && details) return `${message}: ${details}`;
  return message || details;
}

/**
 * Extract the human-readable message from a parsed JSON error body.
 *
 * Returns `fallback` (e.g. `HTTP 409`) only when the payload carries no text
 * at all -- never `[object Object]`, and never a bare `undefined`.
 */
export function apiErrorText(data: unknown, fallback: string): string {
  // A body that is not an object carries no error field to read: the API
  // never answers that way, so the status line is the honest thing to show.
  if (data && typeof data === 'object') {
    const record = data as Record<string, unknown>;
    const fromError = envelopeText(record.error);
    if (fromError) return fromError;
    const detail = record.detail;
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    // A dict `detail` is the app's own shape when it is one (`message` plus
    // `details`), and an arbitrary payload otherwise -- try both readings.
    const fromDetail = Array.isArray(detail) ? detailsText(detail) : envelopeText(detail);
    if (fromDetail) return fromDetail;
    const rawDetail = detailsText(detail);
    if (rawDetail) return rawDetail;
    if (typeof record.message === 'string' && record.message.trim()) return record.message.trim();
  }
  return fallback;
}

/**
 * Message text for a value caught in a `catch` block.
 *
 * `String(e)` is the usual shorthand, but it renders a thrown plain object --
 * a rejected structured payload, for instance -- as `[object Object]` all over
 * again, so object-shaped throws go through `apiErrorText` instead.
 */
export function errorMessage(e: unknown, fallback = 'Request failed'): string {
  if (e instanceof Error) return e.message.trim() || fallback;
  if (typeof e === 'string') return e.trim() || fallback;
  if (e && typeof e === 'object') return apiErrorText(e, fallback);
  return e === null || e === undefined ? fallback : String(e);
}
