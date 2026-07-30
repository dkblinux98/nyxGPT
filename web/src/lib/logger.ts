// Structured logger for Next.js server-side code (API route handlers,
// instrumentation hooks). Emits the SAME line shape nyxgpt.logging's
// DEFAULT_FMT does -- "<utc timestamp> <LEVEL> [<request id>] <logger>:
// <message>" plus a trailing " trace_id=<hex> span_id=<hex>" suffix when a
// span is active -- so the existing promtail regex (docker/promtail-
// config.yml) and Grafana's Loki->Jaeger derived field (which matches
// `trace_id=` anywhere in the line) both work without any change, whether
// the line came from the Python API or the web tier (#3430).
import { context as otelContext, trace } from "@opentelemetry/api";

import { getRequestId } from "./requestContext";

export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

const LOGGER_NAME = "nyxgpt.web";

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
}

export const logger = {
  debug: (message: string, error?: unknown) => write("DEBUG", message, error),
  info: (message: string, error?: unknown) => write("INFO", message, error),
  warn: (message: string, error?: unknown) => write("WARN", message, error),
  error: (message: string, error?: unknown) => write("ERROR", message, error),
};
