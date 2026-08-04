#!/usr/bin/env bash
# Weekly pull + rebuild for the vendored google-maps-scraper binary.
#
# ../google-maps-scraper is a clean clone tracking gosom/google-maps-scraper
# upstream directly (no fork). This script pulls it, rebuilds, smoke-tests
# the new binary with one minimal live scrape, and only swaps it into
# app/scraper/google-maps-scraper if the smoke test's output still parses
# the way run_scraper.py expects (see _scraper_binary_path() and the
# JSON/JSONL parsing around line 332 there).
#
# Usage: ./scripts/update_scraper.sh
# Idempotent: no-ops (exit 0) when already up to date. Never touches the
# live binary unless the new build passes its smoke test.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRAPER_SRC_DIR="$(cd "$REPO_ROOT/../google-maps-scraper" && pwd)"
TARGET_BINARY="$REPO_ROOT/app/scraper/google-maps-scraper"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/update_scraper.log"

mkdir -p "$LOG_DIR"

log() {
  echo "[update_scraper] $*" | tee -a "$LOG_FILE"
}

BUILD_TMP=""
SMOKE_QUERY_FILE=""
SMOKE_RESULTS_FILE=""
cleanup() {
  [ -n "$BUILD_TMP" ] && rm -f "$BUILD_TMP"
  [ -n "$SMOKE_QUERY_FILE" ] && rm -f "$SMOKE_QUERY_FILE"
  [ -n "$SMOKE_RESULTS_FILE" ] && rm -f "$SMOKE_RESULTS_FILE"
  return 0
}
trap cleanup EXIT

log "=== run started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cd "$SCRAPER_SRC_DIR"

if [ -n "$(git status --porcelain)" ]; then
  log "ERROR: $SCRAPER_SRC_DIR has local changes — refusing to touch a dirty clone."
  exit 1
fi

OLD_SHA="$(git rev-parse HEAD)"
git fetch origin --quiet

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
REMOTE_SHA="$(git rev-parse "origin/$CURRENT_BRANCH")"

if [ "$OLD_SHA" = "$REMOTE_SHA" ]; then
  log "already up to date ($OLD_SHA)"
  exit 0
fi

log "new commits available: $OLD_SHA -> $REMOTE_SHA. Pulling..."
git pull --ff-only --quiet origin "$CURRENT_BRANCH"

NEW_SHA="$(git rev-parse HEAD)"
log "pulled $NEW_SHA. Building..."

# Build inside app/scraper/ so the final `mv` into TARGET_BINARY is a same-
# filesystem atomic rename, not a cross-device copy.
BUILD_TMP="$REPO_ROOT/app/scraper/.google-maps-scraper.new.$$"
if ! go build -o "$BUILD_TMP" . >>"$LOG_FILE" 2>&1; then
  log "ERROR: go build failed at $NEW_SHA. Leaving existing binary ($OLD_SHA) in place."
  exit 1
fi
chmod +x "$BUILD_TMP"

log "build OK. Smoke-testing..."

SMOKE_QUERY_FILE="$(mktemp "${TMPDIR:-/tmp}/gms-smoke-query.XXXXXX")"
SMOKE_RESULTS_FILE="$(mktemp "${TMPDIR:-/tmp}/gms-smoke-results.XXXXXX")"
echo "plumber in San Jose, CA" > "$SMOKE_QUERY_FILE"

if ! "$BUILD_TMP" \
    -input "$SMOKE_QUERY_FILE" \
    -results "$SMOKE_RESULTS_FILE" \
    -json -depth 1 -pages-per-browser 1 -lang en -email -fast-mode \
    -geo "37.3382,-121.8863" \
    >>"$LOG_FILE" 2>&1; then
  log "ERROR: smoke-test scrape exited non-zero at $NEW_SHA. Leaving existing binary ($OLD_SHA) in place."
  exit 1
fi

if [ ! -s "$SMOKE_RESULTS_FILE" ]; then
  log "ERROR: smoke-test produced an empty results file at $NEW_SHA. Leaving existing binary ($OLD_SHA) in place."
  exit 1
fi

if ! python3 - "$SMOKE_RESULTS_FILE" <<'PY'
import json, sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read().strip()
try:
    items = json.loads(text) if text.startswith("[") else [
        json.loads(line) for line in text.splitlines() if line.strip()
    ]
except json.JSONDecodeError as e:
    print(f"parse error: {e}", file=sys.stderr)
    sys.exit(1)

if not items or not any(isinstance(i, dict) and i.get("title") for i in items):
    print("no entries with a 'title' key found", file=sys.stderr)
    sys.exit(1)
PY
then
  log "ERROR: smoke-test output at $NEW_SHA doesn't match the schema run_scraper.py expects (no 'title' fields). Leaving existing binary ($OLD_SHA) in place."
  exit 1
fi

log "smoke test passed ($(wc -l < "$SMOKE_RESULTS_FILE" | tr -d ' ') result line(s)). Swapping in new binary."
mv "$BUILD_TMP" "$TARGET_BINARY"
chmod +x "$TARGET_BINARY"

log "done: $OLD_SHA -> $NEW_SHA"
