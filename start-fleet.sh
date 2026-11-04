#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
FLEET_PYTHON="${FLEET_PYTHON:-python3}"
"$FLEET_PYTHON" -m fleet.setup_admin "$@"
exec "$FLEET_PYTHON" -m fleet.app "$@"
