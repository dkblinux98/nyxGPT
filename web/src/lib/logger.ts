// Structured logger for Next.js server-side code (API route handlers,
// instrumentation hooks). Emits the SAME line shape nyxgpt.logging's
// DEFAULT_FMT does -- "<utc timestamp> <LEVEL> [<request id>] <logger>:
// <message>" plus a trailing " trace_id=<hex> span_id=<hex>" suffix when a
// span is active -- so the existing promtail regex (docker/promtail-
// config.yml) and Grafana's Loki->Jaeger derived field (which matches
// `trace_id=` anywhere in the line) both work without any change, whether
// the line came from the Python API or the web tier (#3430).
import { appendFileSync } from "node:fs";
import { join } from "node:path";
import { context as otelContext, trace } from "@opentelemetry/api";

import { getRequestId } from "./requestContext";

export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

const LOGGER_NAME = "nyxgpt.web";

// When set (the Compose deployment's web service, see docker-compose.yml),
// every line is also appended to a file under this directory -- promtail's
// Compose-mode scrape path only tails files, not `docker logs` output, so
// this is what makes web-tier logs reach Loki with label filters in Compose
// mode (#3430). Native mode needs no equivalent: brew's StandardOutPath
// already redirects this process's stdout to ~/.nyxGPT/logs/nyxgpt-web.log,
// which the existing native promtail scrape path already tails.
const LOG_DIR = process.env.NYXGPT_LOG_DIR;
const LOG_FILE = LOG_DIR ? join(LOG_DIR, "nyxgpt-web.log") : null;

function pad(n: number): string {
  return n.toString().padStart(2, "0");
}

function utcTimestamp(): string {
  const d = new Date();
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`
  );
}

function traceSuffix(): string {
  const spanContext = trace.getSpan(otelContext.active())?.spanContext();
  if (!spanContext || !trace.isSpanContextValid(spanContext)) {
    return "";
  }
  return ` trace_id=${spanContext.traceId} span_id=${spanContext.spanId}`;
}

function errorDetail(error: unknown): string {
  if (error instanceof Error) {
    return error.stack ? `${error.message}\n${error.stack}` : error.message;
  }
  if (error === undefined) {
    return "";
  }
  return String(error);
}

function write(level: LogLevel, message: string, error?: unknown): void {
  const requestId = getRequestId() ?? "-";
  const detail = errorDetail(error);
  const fullMessage = detail ? `${message}: ${detail}` : message;
  const line = `${utcTimestamp()} ${level} [${requestId}] ${LOGGER_NAME}: ${fullMessage}${traceSuffix()}`;

  if (level === "ERROR") {
    // eslint-disable-next-line no-console
    console.error(line);
  } else if (level === "WARN") {
    // eslint-disable-next-line no-console
    console.warn(line);
  } else {
    // eslint-disable-next-line no-console
    console.log(line);
  }

  if (LOG_FILE) {
    try {
      appendFileSync(LOG_FILE, line + "\n");
    } catch {
      // Best-effort only -- a full disk or missing directory must never
      // break request handling; the console line above is still emitted.
    }
  }
}

export const logger = {
  debug: (message: string, error?: unknown) => write("DEBUG", message, error),
  info: (message: string, error?: unknown) => write("INFO", message, error),
  warn: (message: string, error?: unknown) => write("WARN", message, error),
  error: (message: string, error?: unknown) => write("ERROR", message, error),
};
