#!/usr/bin/env bash
# Stop and remove the local Reacher container started by
# start_local_verifier.sh. No-op if it isn't running.
#
# Usage: ./scripts/stop_local_verifier.sh

set -euo pipefail

CONTAINER_NAME=reacher-backend

if ! command -v docker &> /dev/null; then
  echo "Docker not found — nothing to stop here (if you started the Cargo"
  echo "fallback, Ctrl-C the terminal it's running in)."
  exit 0
fi

if ! docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  echo "No '$CONTAINER_NAME' container found — nothing to do."
  exit 0
fi

docker stop "$CONTAINER_NAME" > /dev/null
docker rm "$CONTAINER_NAME" > /dev/null
echo "Stopped and removed '$CONTAINER_NAME'."
