#!/usr/bin/env bash
# Validate that web proxy routes exist for all frontend API calls, AND that
# each one forwards the query parameters its caller sends.
#
# The second check exists because the first one was not enough (#4136). This
# script stripped the query string before looking anything up, so a page could
# ask for `?probe_health=true&verify_host=true` while its proxy forwarded
# `probe_health` alone and every gate stayed green: the page tests mock the
# proxy, the backend tests call FastAPI directly, and nothing sat on the seam
# between them. The visible result was a dashboard that asked AWS to confirm a
# billing Dedicated Host, never did, and reported "never confirmed" forever.
#
# A route satisfies the check either by forwarding the whole incoming query
# string (`new URL(request.url).search`) or by naming the parameter, which is
# the allowlist shape -- a proxy that hand-picks parameters is fine, but each
# one it is sent has to be a deliberate decision rather than an omission.

set -euo pipefail

echo "Checking for missing web API proxy routes..."

# Extract all fetch('/api/v1/...') calls from frontend, query string included
FRONTEND_CALLS=$(grep -rh "fetch(['\"]\/api\/v1\/" web/src --include="*.tsx" --include="*.ts" | \
  sed -n "s/.*fetch(['\"]\/api\/v1\/\([^'\"]*\)['\"].*/\1/p" | \
  sort -u)

MISSING_ROUTES=0
DROPPED_PARAMS=0

for call in $FRONTEND_CALLS; do
  # Skip dynamic routes with ${...}
  if echo "$call" | grep -q '\${'; then
    continue
  fi

  route="${call%%\?*}"
  query=""
  if [[ "$call" == *\?* ]]; then
    query="${call#*\?}"
  fi

  # Convert route to file path (handle both /path and /path/route patterns)
  # Remove trailing slashes and convert to directory structure
  route_clean=$(echo "$route" | sed 's:/$::')

  # Check if route.ts exists at the expected location
  route_file="web/src/app/api/v1/${route_clean}/route.ts"

  if [[ ! -f "$route_file" ]]; then
    echo "❌ Missing route: /api/v1/$route"
    echo "   Expected file: $route_file"
    MISSING_ROUTES=$((MISSING_ROUTES + 1))
    continue
  fi

  echo "✅ Found route: /api/v1/$route"

  [[ -n "$query" ]] || continue

  # A route that forwards the whole incoming query string carries every
  # parameter by construction; there is nothing to enumerate.
  if grep -q 'request\.url)\.search\b\|{ *search *}' "$route_file"; then
    echo "   ↳ forwards the whole query string"
    continue
  fi

  for pair in ${query//&/ }; do
    param="${pair%%=*}"
    [[ -n "$param" ]] || continue
    if grep -q "\"$param\"\|'$param'\|\`$param\`\|$param=" "$route_file"; then
      echo "   ↳ forwards ?$param"
    else
      echo "❌ Dropped query parameter: /api/v1/$route?$param"
      echo "   Caller sends it; $route_file never mentions it, so the backend"
      echo "   never sees it and the request silently does less than it asked."
      DROPPED_PARAMS=$((DROPPED_PARAMS + 1))
    fi
  done
done

if [[ $MISSING_ROUTES -gt 0 || $DROPPED_PARAMS -gt 0 ]]; then
  echo ""
  if [[ $MISSING_ROUTES -gt 0 ]]; then
    echo "⚠️  Found $MISSING_ROUTES missing web proxy route(s)"
    echo "   Create the route.ts files or remove the frontend calls"
  fi
  if [[ $DROPPED_PARAMS -gt 0 ]]; then
    echo "⚠️  Found $DROPPED_PARAMS query parameter(s) dropped by a proxy route"
    echo "   Forward them in route.ts, or stop sending them from the page"
  fi
  exit 1
else
  echo ""
  echo "✅ All web API routes exist and forward the parameters they are sent"
fi
