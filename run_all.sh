#!/usr/bin/env bash
# Runs every experiment and then the analysis.
#   sudo ./run_all.sh              # everything (~25 minutes)
#   sudo ./run_all.sh --quick      # only the main congestion comparison (~4 minutes)
#   sudo ./run_all.sh --only flows # one scenario: congestion | linkfail | sweep | flows
#   sudo ./run_all.sh --userspace  # add this only if the OVS kernel module is unavailable
# POX location: $POX_DIR (default: ~/pox of the user who ran sudo)
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Mininet needs root:  sudo ./run_all.sh"
  exit 1
fi

PROJECT="$(cd "$(dirname "$0")" && pwd)"
REAL_USER="${SUDO_USER:-$(id -un)}"
QUICK=0
ONLY=""
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) QUICK=1 ;;
    --only) ONLY="${2:-}"; shift ;;
    *) EXTRA+=("$1") ;;
  esac
  shift
done
case "$ONLY" in
  ""|congestion|linkfail|sweep|flows) ;;
  *) echo "--only must be congestion, linkfail, sweep or flows"; exit 1 ;;
esac
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

run() {   # run <scenario> <mode> [extra experiment args]
  local scenario="$1" mode="$2"; shift 2
  echo
  echo "================ $scenario / $mode $* ================"
  mn -c >/dev/null 2>&1 || true
  python3 "$PROJECT/experiment.py" --scenario "$scenario" --mode "$mode" "$@" ${EXTRA[@]+"${EXTRA[@]}"}
  local status=$?
  mn -c >/dev/null 2>&1 || true
  if [ $status -ne 0 ]; then
    echo "!!! $scenario / $mode failed (exit $status)"
    exit $status
  fi
}

# 1) congestion: baseline vs. SDN rerouting vs. SDN rerouting + application feedback
if want congestion; then
  for MODE in static adaptive app_aware; do run congestion "$MODE"; done
fi

if [ $QUICK -eq 0 ]; then
  # 2) link failure on path P1 (also writes per-packet traces: seq + timestamps)
  if want linkfail; then
    for MODE in static app_aware; do run linkfail "$MODE"; done
  fi
  # 3) load sweep: different offered loads on P1
  if want sweep; then
    for RATE in 3 4 5 6 7; do
      for MODE in static app_aware; do run sweep "$MODE" --rate "$RATE"; done
    done
  fi
  # 4) increasing number of flows (2 Mbps each)
  if want flows; then
    for N in 1 2 4 6 8 10 12; do
      for MODE in static app_aware; do run flows "$MODE" --flows "$N"; done
    done
  fi
fi

echo
echo "================ analysis ================"
python3 "$PROJECT/analyze.py"
chown -R "$REAL_USER":"$REAL_USER" "$PROJECT/results" 2>/dev/null || true
