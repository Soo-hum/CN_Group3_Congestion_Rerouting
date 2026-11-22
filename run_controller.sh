#!/usr/bin/env bash
# Start the POX SDN controller (for the live demo with topology.py).
#   ./run_controller.sh                       # app_aware = full system (default)
#   ./run_controller.sh adaptive              # rerouting from switch statistics only
#   ./run_controller.sh static                # baseline: detects, never reroutes
#   ./run_controller.sh app_aware --threshold=0.7 --interval=0.5
# POX location: $POX_DIR (default: ~/pox). Logs: $LOG_DIR (default: logs/live_<mode>)
# (experiment.py and tests/run_tests.py start their own controller - no need to run this first)
set -euo pipefail

MODE="${1:-app_aware}"
shift || true
case "$MODE" in
  static|adaptive|app_aware) ;;
  *) echo "mode must be static, adaptive or app_aware"; exit 1 ;;
esac
PROJECT="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
POX_DIR="${POX_DIR:-$HOME_DIR/pox}"
LOG_DIR="${LOG_DIR:-$PROJECT/logs/live_$MODE}"

if [ ! -f "$POX_DIR/pox.py" ]; then
  echo "POX not found in $POX_DIR"
  echo "Install it with:  git clone https://github.com/noxrepo/pox.git ~/pox"
  echo "or point to it:   POX_DIR=/path/to/pox ./run_controller.sh $MODE"
  exit 1
fi

if ! python3 -c "import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', 6653))" 2>/dev/null; then
  echo "Port 6653 is already in use - another OpenFlow controller is running."
  echo "  POX from an earlier run:      sudo pkill -f pox.py"
  echo "  Ubuntu's test controller:     sudo systemctl stop openvswitch-testcontroller"
  exit 1
fi

# The UDP servers report to the controller through a UNIX socket file. If an earlier
# "sudo" run (tests, experiments) created it, it belongs to root and this controller,
# running as you, cannot replace it - remove it first.
SOCK="$(cd "$PROJECT" && python3 -c 'import netconfig; print(netconfig.FEEDBACK_SOCKET)' 2>/dev/null)"
SOCK="${SOCK:-/tmp/sdn_app_feedback.sock}"
if [ -e "$SOCK" ] && [ ! -O "$SOCK" ] && [ "$(id -u)" -ne 0 ]; then
  echo "Removing $SOCK left behind by an earlier sudo run (sudo may ask for your password)"
  sudo rm -f "$SOCK" || echo "Could not remove it: the application feedback channel will be off"
fi

mkdir -p "$LOG_DIR"
echo "Starting POX controller: mode=$MODE, logs -> $LOG_DIR  (Ctrl+C to stop)"
cd "$POX_DIR"
PYTHONPATH="$PROJECT/controller${PYTHONPATH:+:$PYTHONPATH}" exec python3 pox.py \
  log.level --INFO \
  openflow.of_01 --port=6653 \
  congestion_controller --mode="$MODE" --log_dir="$LOG_DIR" "$@"
