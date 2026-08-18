#!/usr/bin/env bash
# Print the name of every Pod in a namespace that the scheduler could not
# place, one per line (empty output = everything landed on a node).
#
# The #3825 signal, isolated: a Pod whose requests do not fit any node stays
# Pending with an EMPTY `.spec.nodeName` while every other status field looks
# ordinary. "Pending" alone is not it -- a Pod that is scheduled and pulling
# its image is Pending too, and the capacity gates deliberately run without
# built images. `.spec.nodeName` is the field that separates the two, so the
# assertion is written against that and nothing else.
#
# Used by .github/workflows/k8s-capacity-smoke.yml and
# scripts/k8s-local-smoke.sh. Exits 0 whether or not it finds any; the caller
# decides what an empty or non-empty list means (both halves of the
# fault-injection rule need one of each).
set -euo pipefail

NAMESPACE="${1:-nyxgpt}"

kubectl -n "$NAMESPACE" get pods \
    -o jsonpath='{range .items[*]}{.spec.nodeName}{"|"}{.metadata.name}{"\n"}{end}' |
    awk -F'|' 'NF > 1 && $1 == "" { print $2 }'
