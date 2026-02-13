#!/usr/bin/env bash
# Validate that web proxy routes exist for all frontend API calls

set -euo pipefail

echo "Checking for missing web API proxy routes..."

# Extract all fetch('/api/v1/...') calls from frontend
FRONTEND_CALLS=$(grep -rh "fetch(['\"]\/api\/v1\/" web/src --include="*.tsx" --include="*.ts" | \
  sed -n "s/.*fetch(['\"]\/api\/v1\/\([^'\"]*\)['\"].*/\1/p" | \
  cut -d'?' -f1 | \
  sort -u)

MISSING_ROUTES=0

for route in $FRONTEND_CALLS; do
  # Skip dynamic routes with ${...}
  if echo "$route" | grep -q '\${'; then
    continue
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
  else
    echo "✅ Found route: /api/v1/$route"
  fi
done

if [[ $MISSING_ROUTES -gt 0 ]]; then
  echo ""
  echo "⚠️  Found $MISSING_ROUTES missing web proxy route(s)"
  echo "   Create the route.ts files or remove the frontend calls"
  exit 1
else
  echo ""
  echo "✅ All web API routes exist"
fi
