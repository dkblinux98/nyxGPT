#!/usr/bin/env bash
# Reconstruct a pre-fix Kubernetes sizing in a COPY of `k8s/`, for the
# fault-injection halves of `.github/workflows/k8s-capacity-smoke.yml`.
#
# Usage: scripts/k8s-inject-pre-fix-sizing.sh <dir> <memory|cpu>
#
#   memory -- the pre-#3825 memory requests (api 512Mi, web 256Mi)
#   cpu    -- the pre-#3825 cpu requests (api 250m, web 100m), memory left at
#             the shipped sizing so a failure is unambiguously about cpu
#
# BOTH modes also restore the pre-#3833 standing replica pool (`replicas: 4`
# on the stable Deployments). That is the point of this script existing rather
# than a `sed` per workflow step: the sizing #3825 measured was the old
# requests times a FOUR-wide standing pool, and since #3833 the shipped
# manifests rest at 1 replica and grow only for a rollout. Injecting the old
# requests alone reconstructs a quarter of the defect, fits the node, and
# leaves the gate asserting nothing -- the V-022 failure mode. Keeping the
# reconstruction in one file is also what stops one injection being fixed and
# its twin left behind.
#
# Every substitution is verified to have actually matched: a `sed` that
# silently finds nothing (a request renamed, a manifest split) would otherwise
# be indistinguishable from a defect that has been fixed.
set -euo pipefail

DIR="${1:?usage: k8s-inject-pre-fix-sizing.sh <dir> <memory|cpu>}"
MODE="${2:?usage: k8s-inject-pre-fix-sizing.sh <dir> <memory|cpu>}"

# Pre-#3833: both stable Deployments shipped a standing 4-replica pool, which
# is what made a 25% weight expressible by replica ratio (#2692).
PRE_3833_POOL_REPLICAS=4

must_sed() {
  local file="$1" expr="$2"
  [ -f "$file" ] || {
    echo "::error::injection target $file does not exist" >&2
    exit 1
  }
  local before
  before="$(cat "$file")"
  sed -i "$expr" "$file"
  if [ "$before" = "$(cat "$file")" ]; then
    echo "::error::injection no-op: '$expr' matched nothing in $file -- the" \
      "manifest changed shape, so this fault injection would prove nothing" >&2
    exit 1
  fi
}

restore_standing_pool() {
  local file
  for file in "$DIR/deployment-stable.yaml" "$DIR/deployment-web-stable.yaml"; do
    must_sed "$file" "s/^  replicas: 1$/  replicas: ${PRE_3833_POOL_REPLICAS}/"
  done
  echo "restored the pre-#3833 standing pool: ${PRE_3833_POOL_REPLICAS} replicas on both stable Deployments"
}

case "$MODE" in
  memory)
    restore_standing_pool
    must_sed "$DIR/deployment-stable.yaml" 's/memory: 256Mi/memory: 512Mi/'
    must_sed "$DIR/deployment-web-stable.yaml" 's/memory: 192Mi/memory: 256Mi/'
    echo "restored the pre-#3825 memory requests: api 512Mi, web 256Mi"
    ;;
  cpu)
    restore_standing_pool
    must_sed "$DIR/deployment-stable.yaml" 's/cpu: 100m/cpu: 250m/'
    must_sed "$DIR/deployment-canary.yaml" 's/cpu: 100m/cpu: 250m/'
    must_sed "$DIR/deployment-web-stable.yaml" 's/cpu: 50m/cpu: 100m/'
    must_sed "$DIR/deployment-web-canary.yaml" 's/cpu: 50m/cpu: 100m/'
    echo "restored the pre-#3825 cpu requests: api 250m, web 100m"
    ;;
  *)
    echo "::error::unknown mode '$MODE' -- expected 'memory' or 'cpu'" >&2
    exit 1
    ;;
esac

echo "--- injected sizing in $DIR ---"
grep -nE '^  replicas:|cpu:|memory:' \
  "$DIR/deployment-stable.yaml" "$DIR/deployment-web-stable.yaml"
