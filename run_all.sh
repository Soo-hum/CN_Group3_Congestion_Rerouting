#!/usr/bin/env bash
# Runs every experiment and then the analysis.
#   sudo ./run_all.sh              # everything (~12 minutes)
#   sudo ./run_all.sh --quick      # only the main congestion comparison (~4 minutes)
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
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    *) EXTRA+=("$arg") ;;
  esac
done

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
for MODE in static adaptive app_aware; do run congestion "$MODE"; done

if [ $QUICK -eq 0 ]; then
  # 2) link failure on path P1
  for MODE in static app_aware; do run linkfail "$MODE"; done
  # 3) load sweep: different offered loads on P1
  for RATE in 3 4 5 6 7; do
    for MODE in static app_aware; do run sweep "$MODE" --rate "$RATE"; done
  done
fi

echo
echo "================ analysis ================"
python3 "$PROJECT/analyze.py"
chown -R "$REAL_USER":"$REAL_USER" "$PROJECT/results" 2>/dev/null || true
