#!/usr/bin/env bash
# check_denylist.sh — fail if any literal from the gitignored .secret-denylist
# appears in staged content (or, with --all, in every tracked file).
#
# Prints only file name and line number — NEVER the matched value.
#
# Usage:
#   tools/check_denylist.sh            # scan `git diff --cached` (pre-commit)
#   tools/check_denylist.sh --all      # scan all tracked files (pre-push)
#   tools/check_denylist.sh --history  # scan every blob in history (pre-push)
#
# .secret-denylist format:
#   <literal>        a string that must never appear in committed content
#   !<path>          exempt one repo-relative path from scanning (e.g. a LICENSE
#                    file the repo owner deliberately published with their name);
#                    a trailing "/" exempts a whole directory (e.g. !ai-instructions/)
#   # comment
#
# Fails closed: a missing or literal-free denylist exits non-zero.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
DENYLIST="${DENYLIST_FILE:-$REPO_ROOT/.secret-denylist}"
MODE="${1:-staged}"

if [[ ! -f "$DENYLIST" ]]; then
  cat > "$DENYLIST" <<'TEMPLATE'
# .secret-denylist — gitignored; one literal per line; lines starting with # are comments.
# tools/check_denylist.sh fails a commit if any entry below appears in staged content.
# Add here, one per line:
#   - your VM's public IP address
#   - your email address
#   - any real LiveTrack session IDs / tokens you have captured
TEMPLATE
  chmod 600 "$DENYLIST"
  echo "check_denylist: FAILED — $(basename "$DENYLIST") was missing; created a template. Fill it in." >&2
  exit 1
fi

# Load entries (strip comments and blank lines, trim whitespace).
mapfile -t ENTRIES < <(sed -e 's/#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$DENYLIST" | grep -v '^$' || true)
LITERALS=()
EXEMPT_PATHS=()
for e in "${ENTRIES[@]}"; do
  if [[ "$e" == !* ]]; then EXEMPT_PATHS+=("${e#!}"); else LITERALS+=("$e"); fi
done

is_exempt() {
  local p
  for p in "${EXEMPT_PATHS[@]+"${EXEMPT_PATHS[@]}"}"; do
    if [[ "$p" == */ ]]; then
      [[ "$1" == "$p"* ]] && return 0
    else
      [[ "$1" == "$p" ]] && return 0
    fi
  done
  return 1
}

if [[ ${#LITERALS[@]} -eq 0 ]]; then
  echo "check_denylist: FAILED — $(basename "$DENYLIST") has no literals (fail closed). Populate it." >&2
  exit 1
fi

# Build a fixed-string pattern file for grep -F.
PATTERN_FILE="$(mktemp)"
trap 'rm -f "$PATTERN_FILE"' EXIT
printf '%s\n' "${LITERALS[@]}" > "$PATTERN_FILE"

found=0

scan_stream() {
  # $1 = label for the stream; stdin = content. Reports "label:line" only.
  local label="$1"
  local hits
  hits="$(grep -n -F -f "$PATTERN_FILE" -- - 2>/dev/null | cut -d: -f1 || true)"
  if [[ -n "$hits" ]]; then
    while IFS= read -r ln; do
      echo "DENYLIST HIT: $label:$ln" >&2
    done <<< "$hits"
    return 1
  fi
  return 0
}

case "$MODE" in
  staged)
    # Scan each staged file's *staged* content.
    while IFS= read -r -d '' path; do
      is_exempt "$path" && continue
      if ! git show ":$path" 2>/dev/null | scan_stream "$path"; then found=1; fi
    done < <(git diff --cached --name-only --diff-filter=ACMR -z)
    # Also check file names themselves.
    if git diff --cached --name-only --diff-filter=ACMR | grep -q -F -f "$PATTERN_FILE"; then
      echo "DENYLIST HIT: a staged file *name* contains a denylisted literal" >&2
      found=1
    fi
    ;;
  --all|all)
    while IFS= read -r -d '' path; do
      is_exempt "$path" && continue
      if ! git show "HEAD:$path" 2>/dev/null | scan_stream "$path"; then found=1; fi
    done < <(git ls-files -z)
    ;;
  --history|history)
    # Every blob reachable from any ref.
    while read -r sha type path; do
      [[ "$type" == "blob" ]] || continue
      is_exempt "$path" && continue
      if ! git cat-file -p "$sha" | scan_stream "blob:${sha:0:12}:$path"; then found=1; fi
    done < <(git rev-list --all --objects | git cat-file --batch-check='%(objectname) %(objecttype) %(rest)')
    ;;
  *)
    echo "usage: $0 [staged|--all|--history]" >&2
    exit 2
    ;;
esac

if [[ $found -ne 0 ]]; then
  echo "check_denylist: FAILED — denylisted literal(s) found (locations above; values not shown)." >&2
  exit 1
fi
echo "check_denylist: OK ($MODE)"
