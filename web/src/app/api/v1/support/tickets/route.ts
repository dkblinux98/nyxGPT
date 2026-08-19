// The in-app support intake's write path (#3811).
//
// This is the one POST under /support, and it exists because the owner
// rejected the alternative in acceptance: handing a user with a broken
// install to github.com's compose page shows them this repository's
// development metadata and strands them there afterwards. The form lives in
// the chat, the backend files the ticket, and the UI shows the filer their
// own ticket number.
//
// The backend's status codes carry meaning the UI acts on -- 503 is "this
// install has no GitHub credential", 502 is "GitHub said no", 400 is "fix
// your input" -- so the response is passed through with its status intact
// rather than collapsed into a generic failure.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const POST = withRequestLog(async function POST(request: Request) {
  try {
    const body = await request.json();
    const res = await apiFetch("/api/v1/support/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    logger.error("Failed to file the support ticket:", error);
    return new Response(
      JSON.stringify({
        error:
          "nyxGPT could not reach its own API to file the ticket. Check that the " +
          "nyxGPT API is running (`nyxgpt ops status`) and try again.",
      }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
