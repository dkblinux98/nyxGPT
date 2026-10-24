# The Node major in every `FROM` below is NOT a free choice (#4194): it is the
# one nyxGPT declares in `.nvmrc` / `src/nyxgpt/node_runtime.py`, and
# `tests/unit/test_node_version_declaration.py` fails the build if this file
# names a different one. Bump the declaration, not this line.
FROM node:24-alpine AS deps
WORKDIR /app
# --- nyxGPT remote-fetch policy (#4205). Generated from
# src/nyxgpt/build_fetch_policy.py, which states the reasoning;
# tests/unit/test_build_fetch_policy.py fails the build if these drift or
# if a new fetching step appears without them. Do not edit by hand.
ENV NPM_CONFIG_FETCH_RETRIES=5
ENV NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=2000
ENV NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=60000
ENV NPM_CONFIG_FETCH_TIMEOUT=300000
ENV PIP_RETRIES=5
ENV PIP_TIMEOUT=30
# --- end nyxGPT remote-fetch policy
# `.npmrc` with the manifests, not left behind: it carries `engine-strict`, so
# without it the one `npm ci` that builds the shipped bundle is the one install
# that would downgrade an `engines` mismatch back to a warning (#4194).
COPY package.json package-lock.json .npmrc ./
# `npm ci` is not trustworthy on its own here: with the registry unreachable it
# prints "npm error Exit handler never called!" and **exits 0**, having created
# 520 empty package directories and no `node_modules/.package-lock.json`. That
# made a registry blip a green build over an empty dependency tree, cached by
# BuildKit for every later build on the machine (#4205). So the step asserts
# npm's own completed-reify receipt; see build_fetch_policy.npm_install_run_step.
RUN npm ci; status=$?; if [ $status -ne 0 ] || ! test -s node_modules/.package-lock.json; then echo "nyxgpt-build: remote fetch did not complete: 'npm ci' left an incomplete node_modules (exit $status) -- the package registry was most likely unreachable" >&2; exit 1; fi

FROM node:24-alpine AS builder
WORKDIR /app
# --- nyxGPT remote-fetch policy (#4205). Generated from
# src/nyxgpt/build_fetch_policy.py, which states the reasoning;
# tests/unit/test_build_fetch_policy.py fails the build if these drift or
# if a new fetching step appears without them. Do not edit by hand.
ENV NPM_CONFIG_FETCH_RETRIES=5
ENV NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=2000
ENV NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=60000
ENV NPM_CONFIG_FETCH_TIMEOUT=300000
ENV PIP_RETRIES=5
ENV PIP_TIMEOUT=30
# --- end nyxGPT remote-fetch policy
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
# Resolves from the node_modules copied above, but still reaches the network
# for the platform `@next/swc` binary and anything `next build` downloads, so
# it runs under the same fetch policy (the ENV block on this stage). No
# completion assertion: unlike `npm ci`, `next build` reports a failed fetch
# through its exit status, so the step already goes red (#4205).
RUN npm run build

FROM node:24-alpine AS runner
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
