#!/usr/bin/env bash
# Install the Playwright driver + browsers that the google-maps-scraper binary
# needs for -grid-bbox (JS mode). The upstream binary compiled from
# github.com/gosom/google-maps-scraper @ 0ef302e ("Fixes 404 playwright") uses
# mxschmitt/playwright-go v0.6100.0 (driver v1.60.0 identifier). PLAYWRIGHT_INSTALL_ONLY=1
# actually downloads v1.61.1 — see workaround below.
#
# Usage:
#   ./scripts/setup_scraper_playwright.sh
#
# Idempotent. Skips download work already done.

set -eu

BINARY_DIR="$(cd "$(dirname "$0")/../app/scraper" && pwd)"
BINARY="$BINARY_DIR/google-maps-scraper"
CACHE_ROOT="$HOME/Library/Caches/ms-playwright-go"
TARGET_VERSION="1.60.0"  # what the binary looks for
INSTALLED_VERSION="1.61.1"  # what PLAYWRIGHT_INSTALL_ONLY actually writes

if [ ! -x "$BINARY" ]; then
  echo "ERROR: scraper binary not found at $BINARY" >&2
  exit 1
fi

echo "Installing Playwright driver + browsers (Chromium + FFmpeg, ~265 MB)..."
PLAYWRIGHT_INSTALL_ONLY=1 "$BINARY"

if [ ! -d "$CACHE_ROOT/$INSTALLED_VERSION" ]; then
  echo "ERROR: expected driver at $CACHE_ROOT/$INSTALLED_VERSION not found." >&2
  echo "Check $BINARY output above for the actual install path." >&2
  exit 1
fi

# Version-mismatch bridge: binary's Run() reads driver at 1.60.0, but
# Install() writes to 1.61.1 (upstream mxschmitt/playwright-go v0.6100.0 bug).
# Copy the installed dir to the expected path and patch the version so the
# binary's version check passes. Idempotent.
if [ ! -d "$CACHE_ROOT/$TARGET_VERSION" ]; then
  echo "Bridging installed $INSTALLED_VERSION -> expected $TARGET_VERSION..."
  cp -R "$CACHE_ROOT/$INSTALLED_VERSION" "$CACHE_ROOT/$TARGET_VERSION"
fi

PKG_JSON="$CACHE_ROOT/$TARGET_VERSION/package/package.json"
if [ -f "$PKG_JSON" ]; then
  current_version="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$PKG_JSON")"
  if [ "$current_version" != "$TARGET_VERSION" ]; then
    echo "Patching package.json version $current_version -> $TARGET_VERSION..."
    python3 - "$PKG_JSON" "$TARGET_VERSION" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
d = json.load(open(path))
d["version"] = version
json.dump(d, open(path, "w"), indent=2)
PY
  fi
fi

echo "Playwright setup complete."
echo "Verify with: $BINARY -input <(echo 'test') -grid-bbox '37.3,-122.0,37.4,-121.9' -grid-cell 5.0 -depth 1 -json -results /tmp/verify.json -exit-on-inactivity 30s"
