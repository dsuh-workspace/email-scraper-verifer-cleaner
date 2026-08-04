#!/usr/bin/env bash
# Start a local Reacher (check-if-email-exists) backend on port 8080.
#
# Mirrors production's docker invocation from ../email-verifier's
# deploy_kamatera.sh (`docker run -d --name email-verifier --restart always
# -p 8080:8080 reacherhq/backend`), minus --restart always — this is a local
# dev instance, not a server. Falls back to building from source via Cargo
# (bin `reacher_backend`, see ../email-verifier/backend/Cargo.toml) if Docker
# isn't available.
#
# Idempotent: no-ops if the verifier is already reachable, and restarts an
# existing-but-stopped container instead of erroring on "name already in use".
#
# Usage: ./scripts/start_local_verifier.sh

set -euo pipefail

PORT=8080
CONTAINER_NAME=reacher-backend
VERSION_URL="http://127.0.0.1:${PORT}/version"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EMAIL_VERIFIER_DIR="$(cd "$SCRIPT_DIR/../../email-verifier" && pwd)"

is_up() {
  curl -s -o /dev/null -m 2 "$VERSION_URL"
}

wait_for_health() {
  local tries=30
  while [ "$tries" -gt 0 ]; do
    if is_up; then
      return 0
    fi
    tries=$((tries - 1))
    sleep 1
  done
  return 1
}

if is_up; then
  echo "Verifier already responding at http://127.0.0.1:${PORT}"
  exit 0
fi

if command -v docker &> /dev/null; then
  echo "Docker found."
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "Container '$CONTAINER_NAME' already exists — starting it."
    docker start "$CONTAINER_NAME" > /dev/null
  else
    echo "Running reacherhq/backend:latest as '$CONTAINER_NAME'..."
    docker run -d -p "${PORT}:8080" --name "$CONTAINER_NAME" reacherhq/backend:latest > /dev/null
  fi

  echo "Waiting for the verifier to come up..."
  if wait_for_health; then
    echo "Verifier is running at http://127.0.0.1:${PORT}"
    echo "To stop: ./scripts/stop_local_verifier.sh"
  else
    echo "ERROR: container started but ${VERSION_URL} never responded. Check: docker logs $CONTAINER_NAME"
    exit 1
  fi
else
  echo "Docker not found. Falling back to compiling from source..."
  if [ ! -d "$EMAIL_VERIFIER_DIR/backend" ]; then
    echo "ERROR: expected sibling repo at $EMAIL_VERIFIER_DIR/backend, not found."
    exit 1
  fi
  if ! command -v cargo &> /dev/null; then
    echo "ERROR: neither Docker nor Cargo (Rust) is installed."
    exit 1
  fi
  echo "Cargo found. Building and running reacher_backend in the foreground (Ctrl-C to stop)..."
  cd "$EMAIL_VERIFIER_DIR/backend"
  cargo run --release --bin reacher_backend
fi
