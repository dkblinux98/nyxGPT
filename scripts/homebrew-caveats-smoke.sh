#!/usr/bin/env bash
#
# What `brew install` tells the operator to run, asserted (#3854).
#
# A formula that declares `service` and no `caveats` gets exactly one
# post-install instruction, and Homebrew writes it, not us:
#
#   To start nyxgpt-api now and restart at login:
#     brew services start nyxgpt-api
#
# The owner followed it, because it was the only thing they were told. `brew
# services start` starts the one service it names and never reaches
# `ops.install()`, so the machine came up with no Ollama, no Cassandra and no
# observability, and the web UI answered HTTP 500 on the session list. The
# requirement that observability runs on every install path was implemented and
# simultaneously unreachable by anyone following the printed guidance.
#
# The assertion is therefore on the OUTPUT, not on the recipe. Grepping the
# formulas for a `caveats` block would pass just as happily on a keg whose
# caveats Homebrew never prints -- the same shape of mistake as #3850, where
# every check that reached into the keg's venv was green while the operator's
# `nyxgpt up` answered `command not found`. This runs against the real captured
# `brew install` transcript instead.
#
# Two modes:
#
#   homebrew-caveats-smoke.sh <formula-ref> <install-log>
#       Assert the transcript carries the guidance, then INJECT the condition:
#       strip `caveats` back out of the tap's formula file, ask Homebrew what it
#       would print now, and require the same function to fail. A check that has
#       only ever run against a good formula cannot be told apart from no check
#       at all. Restores the formula on the way out, including on failure.
#
#   homebrew-caveats-smoke.sh --check-only <file>
#       Run just the checker over a file. This is the mode
#       tests/unit/test_homebrew_caveats_smoke.py drives, so the checker itself
#       is exercised on every PR on Linux, where there is no Homebrew.
#
# Usage on a real Mac:
#   brew install --verbose dkblinux98/nyxgpt/nyxgpt-api@3.0.0rc 2>&1 | tee install.log
#   ./scripts/homebrew-caveats-smoke.sh dkblinux98/nyxgpt/nyxgpt-api@3.0.0rc install.log

set -uo pipefail

CAVEATS_HEADER='==> Caveats'

# Homebrew prints caveats under its own header. Everything before it is
# download/build noise, and a `--verbose` install log carries a great deal of
# it -- enough that a bare `grep Cassandra` over the whole transcript could be
# satisfied by a vendored source path rather than by anything the operator is
# told. Narrow to the section first, and fall back to the whole file when the
# header is absent, so a transcript with no caveats at all still reaches the
# checks below and fails them.
caveats_section() {
  local file="$1"
  local section
  section="$(awk -v header="$CAVEATS_HEADER" 'index($0, header) {found = 1} found' "$file")"
  if [ -z "$section" ]; then
    cat "$file"
  else
    printf '%s\n' "$section"
  fi
}

# The single definition of "the operator was told the right thing". Both the
# real assertion and the negative control call this, deliberately: a check
# re-typed for the injection proves nothing about the check that ships.
guidance_is_present() {
  local file="$1"
  local section
  local rc=0
  section="$(caveats_section "$file")"

  if ! printf '%s\n' "$section" | grep -q 'nyxgpt up'; then
    echo "  missing: the \`nyxgpt up\` instruction"
    rc=1
  fi
  if ! printf '%s\n' "$section" | grep -q 'brew services start'; then
    echo "  missing: any mention of \`brew services start\`"
    rc=1
  fi
  # The three subsystems `brew services start` silently leaves out. Naming
  # `nyxgpt up` without saying what the alternative fails to do would leave the
  # operator free to keep using the command Homebrew handed them.
  local subsystem
  for subsystem in Ollama Cassandra observability; do
    if ! printf '%s\n' "$section" | grep -qi -- "$subsystem"; then
      echo "  missing: what \`brew services start\` leaves undone for $subsystem"
      rc=1
    fi
  done

  return "$rc"
}

if [ "${1:-}" = "--check-only" ]; then
  FILE="${2:-}"
  if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    echo "usage: $0 --check-only <file>" >&2
    exit 2
  fi
  if guidance_is_present "$FILE"; then
    echo "PASS: $FILE carries the \`nyxgpt up\` guidance"
    exit 0
  fi
  echo "FAIL: $FILE does not carry the \`nyxgpt up\` guidance"
  exit 1
fi

FORMULA="${1:-}"
LOG="${2:-}"
if [ -z "$FORMULA" ] || [ -z "$LOG" ]; then
  echo "usage: $0 <formula-ref> <install-log>" >&2
  echo "       $0 --check-only <file>" >&2
  exit 2
fi
if [ ! -f "$LOG" ]; then
  echo "::error::no install transcript at $LOG -- nothing to assert against" >&2
  exit 2
fi

echo "=== what the install told the operator ==="
caveats_section "$LOG"

echo
echo "=== assert: the install names \`nyxgpt up\` ==="
if ! guidance_is_present "$LOG"; then
  echo "::error::brew install for $FORMULA does not tell the operator to run \`nyxgpt up\`, or does not say what \`brew services start\` leaves out (#3854)"
  exit 1
fi
echo "good: the install output names \`nyxgpt up\` and what \`brew services start\` does not do"

FORMULA_FILE="$(brew formula "$FORMULA" 2>/dev/null)"
if [ -z "$FORMULA_FILE" ] || [ ! -f "$FORMULA_FILE" ]; then
  echo "::error::cannot locate the formula file for $FORMULA, so the condition cannot be injected" >&2
  exit 1
fi

WORK="$(mktemp -d)"
BACKUP="$WORK/formula.rb.orig"
cp "$FORMULA_FILE" "$BACKUP"
restore_the_formula() {
  cp "$BACKUP" "$FORMULA_FILE"
}
trap restore_the_formula EXIT

# Positive control on the vehicle itself. The injection below reads what
# Homebrew *would* print via `brew info` rather than reinstalling the keg, so
# `brew info` has to satisfy the same checker while the caveats are still there
# -- otherwise a failure after the strip could just as well mean `brew info`
# never carries caveats, and the control would prove nothing.
echo
echo "=== control: \`brew info\` carries the same guidance ==="
if ! brew info "$FORMULA" > "$WORK/info-before.log" 2>&1; then
  echo "::error::brew info $FORMULA failed" >&2
  cat "$WORK/info-before.log" >&2
  exit 1
fi
if ! guidance_is_present "$WORK/info-before.log"; then
  echo "::error::brew info does not carry the caveats, so it cannot be used to inject the condition" >&2
  exit 1
fi
echo "good: brew info reports the same guidance the install printed"

echo
echo "=== negative control: the formula #3854 shipped, with no caveats block ==="
if ! python3 - "$FORMULA_FILE" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
# The heredoc body indents further than the method, so the first line that is
# exactly `  end` is the method's own -- non-greedy is safe here.
stripped, count = re.subn(r"\n  def caveats\n.*?\n  end\n", "\n", text, flags=re.S)
if count != 1:
    sys.exit(f"expected exactly one caveats block in {path}, found {count}")
path.write_text(stripped, encoding="utf-8")
PY
then
  echo "::error::could not strip the caveats block, so the check was never exercised against a bad formula" >&2
  exit 1
fi

if ! brew info "$FORMULA" > "$WORK/info-after.log" 2>&1; then
  echo "::error::brew info $FORMULA failed after the caveats block was removed" >&2
  cat "$WORK/info-after.log" >&2
  exit 1
fi
if guidance_is_present "$WORK/info-after.log"; then
  echo "::error::the check passes against a formula with no caveats -- it tests nothing" >&2
  exit 1
fi
echo "good: the check fails when the formula carries no caveats"

echo
echo "=== restore, and prove the shipped formula is whole again ==="
restore_the_formula
trap - EXIT
if ! brew info "$FORMULA" > "$WORK/info-restored.log" 2>&1; then
  echo "::error::brew info $FORMULA failed after the formula was restored" >&2
  cat "$WORK/info-restored.log" >&2
  exit 1
fi
if ! guidance_is_present "$WORK/info-restored.log"; then
  echo "::error::the formula was not restored -- later steps would run against a mutated tap" >&2
  exit 1
fi
echo "good: $FORMULA still tells the operator to run \`nyxgpt up\`"
