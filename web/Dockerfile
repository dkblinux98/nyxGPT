FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* variables are inlined into the client bundle at build time,
# so these must be build ARGs rather than runtime container env vars.
ARG NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
# Browser-side OTel correlation backbone (#3430, src/instrumentation-client.ts).
# Defaults to the otel-collector's host-published port -- matches
# [tracing] otlp_endpoint's default in example.config.ini -- so no override
# is needed for the common local/Compose deployment.
ARG NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT=http://localhost:4318/v1/traces
ENV NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT=${NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT}
ARG NEXT_PUBLIC_NYXGPT_TRACING_ENABLED=true
ENV NEXT_PUBLIC_NYXGPT_TRACING_ENABLED=${NEXT_PUBLIC_NYXGPT_TRACING_ENABLED}
# Browser error tracking DSN (#3430, src/instrumentation-client.ts) -- opt-in,
# empty by default (Sentry init is skipped with no DSN). Sentry DSNs are not
# secrets (designed to be embedded in client code), unlike NYXGPT_AUTH_API_KEY.
# Set to match [error_tracking] dsn in config.ini to enable browser error
# capture to the local GlitchTip instance -- see docs/docker-compose.md.
ARG NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN=""
ENV NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN=${NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN}
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# Which web build this image is (#3982). Server-side only, so unlike the
# NEXT_PUBLIC_* vars above it belongs on the *runner* stage and needs no
# rebuild to correct: `/api/info` reads it at request time and reports it
# beside the API's own version, which is how a mixed web/API stack becomes
# visible instead of silent. A container has no install directory to derive
# it from -- the native paths do (Homebrew Cellar, the systemd build dir,
# see web/src/lib/webVersion.ts) -- so here it must be declared. Left empty
# by default rather than guessed: the UI says "unknown", which is true,
# where a wrong number would not be.
ARG NYXGPT_WEB_VERSION=""
ENV NYXGPT_WEB_VERSION=${NYXGPT_WEB_VERSION}
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

EXPOSE 3000
CMD ["npm", "start"]
