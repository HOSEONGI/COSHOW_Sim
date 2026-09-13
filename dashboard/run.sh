#!/usr/bin/env bash
set -eo pipefail
DASHBOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DASHBOARD_DIR/.." && pwd)"
source "$PROJECT_DIR/setup_env.sh"
source "$PROJECT_DIR/ros2_ws/install/setup.bash"
mkdir -p "$DASHBOARD_DIR/logs" "$DASHBOARD_DIR/run"
cd "$DASHBOARD_DIR"
# Process substitution preserves Python as this script's PID and signal target.
# A pipeline with Python on its left would make shutdown ownership ambiguous.
exec python3 -u server.py --config config/dashboard.yaml "$@" \
  > >(tee -a "$DASHBOARD_DIR/logs/dashboard.log") 2>&1
