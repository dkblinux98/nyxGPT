import { apiFetch } from "@/lib/apiProxy";
import { resolveWebVersion } from "@/lib/webVersion";

/**
 * Proxy `GET /api/v1/info`, and stamp the web tier's own version into the
 * answer on the way through (#3982).
 *
 * This route is the one place in the system where both tiers are present at
 * once: it runs inside the Next.js server (so it knows which web build is
 * serving) and it is talking to the API (so it knows which API build
 * answered). Reporting them together here means the client receives a
 * consistent pair from a single request -- it cannot render a web version
 * from one moment against an API version from another, and a mismatch is a
 * property of the payload rather than something the browser has to
 * assemble.
 *
 * The upstream body is otherwise passed through untouched, including on
 * error: a non-JSON or non-object response (an HTML error page, a 502 from
 * something in front of the API) is forwarded exactly as received rather
 * than replaced by a synthesised one, so the status and text an operator
 * needs to debug survive.
 */
export async function GET() {
  const r = await apiFetch(`/api/v1/info`, {
    cache: "no-store",
  });

  const web = resolveWebVersion();
  // Buffered rather than streamed: the payload is a handful of fields, and
  // merging into it requires having read it.
  const body = await r.text();

  let payload: string | null = body || null;
  if (body) {
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = JSON.stringify({
          ...parsed,
          web_version: web.version,
          web_version_source: web.source,
        });
      }
    } catch {
      // Not JSON -- forward it verbatim (see the docstring above).
    }
  }

  return new Response(payload, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
