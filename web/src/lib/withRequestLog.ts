// Wraps a Next.js route handler so every log call inside it (via
// `logger.ts`) is automatically tagged with a per-request id, and every
// response gets an `X-Request-Id` header -- without the handler body having
// to thread a request id through itself (#3430). Route handlers only need
// to change `console.error(...)` to `logger.error(...)` and wrap their
// export: `export const GET = withRequestLog(async (request) => {...})`.
import { logger } from "./logger";
import { resolveRequestId, withRequestId } from "./requestContext";

type RouteHandler<Context = unknown> = (
  request: Request,
  context: Context
) => Promise<Response> | Response;

export function withRequestLog<Context = unknown>(
  handler: RouteHandler<Context>
): RouteHandler<Context> {
  return async (request: Request, context: Context) => {
    const requestId = resolveRequestId(request);
    return withRequestId(requestId, async () => {
      try {
        const response = await handler(request, context);
        response.headers.set("X-Request-Id", requestId);
        return response;
      } catch (error) {
        const where = request ? `${request.method} ${new URL(request.url).pathname}` : "route handler";
        logger.error(`Unhandled error in ${where}`, error);
        throw error;
      }
    });
  };
}
