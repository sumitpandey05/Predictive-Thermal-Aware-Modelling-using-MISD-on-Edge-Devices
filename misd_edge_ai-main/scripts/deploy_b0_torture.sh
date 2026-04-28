#!/bin/bash
# scripts/deploy_b0_torture.sh — Automates 20m B0 Torture Benchmark
set -e

if [ -z "$1" ]; then
    echo "Usage: ./scripts/deploy_b0_torture.sh \"Description of this run\""
    echo "Example: ./scripts/deploy_b0_torture.sh \"20 min B0 max thermal stress\""
    exit 1
fi
RUN_COMMENT="$1"
export TZ=Asia/Kolkata
RUN_ID=$(date +%Y%m%d_%H%M%S)
IST_TIME=$(date '+%Y-%m-%d %H:%M:%S')

PI_IP="192.168.68.124"
PI_USER="pi-sree"
PI_PASS="pi-sree"
PI_DIR="~/misd_gov"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

log "1. Transferring modified python and torture shell scripts to RPi4..."
sshpass -p "$PI_PASS" scp scripts-on-rpi/test_b0_torture.sh scripts-on-rpi/tps_telemetry.py "$PI_USER@$PI_IP:$PI_DIR/scripts-on-rpi/"
sshpass -p "$PI_PASS" ssh "$PI_USER@$PI_IP" -o ServerAliveInterval=60 "chmod +x $PI_DIR/scripts-on-rpi/test_b0_torture.sh"

log "2. Running 20-minute B0 Torture benchmark on Pi (Run ID: $RUN_ID)..."
sshpass -p "$PI_PASS" ssh "$PI_USER@$PI_IP" \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=20 \
    "cd $PI_DIR && ./scripts-on-rpi/test_b0_torture.sh \"${RUN_ID}\""

log "3. Fetching updated telemetry and logs..."
mkdir -p scripts-on-rpi/bench_results
sshpass -p "$PI_PASS" scp \
    "$PI_USER@$PI_IP:$PI_DIR/scripts-on-rpi/bench_results/B0_torture_${RUN_ID}_telemetry.csv" \
    "$PI_USER@$PI_IP:$PI_DIR/scripts-on-rpi/bench_results/B0_torture_${RUN_ID}_dmesg.log" \
    scripts-on-rpi/bench_results/ 2>/dev/null || true

log "4. Updating local run history..."
HISTORY_FILE="scripts-on-rpi/bench_results/run_history.md"
if [ ! -f "$HISTORY_FILE" ]; then
    echo "# Benchmark Run History" > "$HISTORY_FILE"
    echo "" >> "$HISTORY_FILE"
    echo "| Run ID | Timestamp (IST) | Test Type | Description / Notes |" >> "$HISTORY_FILE"
    echo "|---|---|---|---|" >> "$HISTORY_FILE"
fi
echo "| \`${RUN_ID}\` | ${IST_TIME} | B0_torture | ${RUN_COMMENT} |" >> "$HISTORY_FILE"

log "Done! Run ID: ${RUN_ID}"
log "Log files: scripts-on-rpi/bench_results/B0_torture_${RUN_ID}_*.csv/log"
log "Refresh your Dashboard and select Run ID: ${RUN_ID}"
