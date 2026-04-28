#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/bench_results"
TELEMETRY_SCRIPT="$SCRIPT_DIR/tps_telemetry.py"
LLAMA_SERVER="${HOME}/llama.cpp/build/bin/llama-server"
MODEL="${HOME}/models/tinyllama.gguf"

# RUN_ID is passed by the deploy script (IST timestamp). Default fallback for manual runs.
RUN_ID="${1:-$(date +%Y%m%d_%H%M%S)}"

WARMUP_S=5
RUN_S=1200
SERVER_WAIT_S=15
SERVER_PORT=8080
THREADS=4
N_PREDICT=150

log() { echo "[$(date '+%H:%M:%S')] $*"; }
read_temp() { awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp; }

prewarm() {
    log "Pre-warming board for ${WARMUP_S}s..."
    for i in 1 2 3 4; do yes > /dev/null & done
    WARMUP_PIDS=$(jobs -p)
    sleep "$WARMUP_S"
    kill $WARMUP_PIDS 2>/dev/null || true
    wait $WARMUP_PIDS 2>/dev/null || true
    sleep 1
}

start_server() {
    log "Starting llama-server"
    "$LLAMA_SERVER" -m "$MODEL" --port "$SERVER_PORT" --threads "$THREADS" --metrics > "$RESULTS_DIR/B0_torture_${RUN_ID}_server.log" 2>&1 &
    SERVER_PID=$!
    sleep "$SERVER_WAIT_S"
    if ! curl -sf "http://localhost:${SERVER_PORT}/health" > /dev/null; then exit 1; fi
}

stop_server() { kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; }

run_inference_loop() {
    local END_TIME=$(( $(date +%s) + RUN_S ))
    log "Continuous inference test started (${RUN_S}s)"
    while [ "$(date +%s)" -lt "$END_TIME" ]; do
        curl -sf -X POST "http://localhost:${SERVER_PORT}/completion" -H "Content-Type: application/json" -d "{
            \"prompt\": \"Explain how neural networks work:\",
            \"n_predict\": ${N_PREDICT},
            \"temperature\": 0.7
        }" > /dev/null || true
    done
}

log "=== B0 Torture Test | Run ID: ${RUN_ID} ==="
mkdir -p "$RESULTS_DIR"

# Ensure MISD is NOT loaded
sudo rmmod misd_gov 2>/dev/null || true

prewarm
start_server

sudo dmesg -c > /dev/null 2>&1 || true
dmesg -w --time-format iso > "$RESULTS_DIR/B0_torture_${RUN_ID}_dmesg.log" 2>&1 &
DMESG_PID=$!

python3 "$TELEMETRY_SCRIPT" "$RESULTS_DIR/B0_torture_${RUN_ID}_telemetry.csv" "B0_TORTURE" "$RUN_S" &
TELEMETRY_PID=$!

run_inference_loop

kill "$TELEMETRY_PID" "$DMESG_PID" 2>/dev/null || true
wait "$TELEMETRY_PID" "$DMESG_PID" 2>/dev/null || true
stop_server

log "B0 Torture test complete — board at $(read_temp)°C"
log "Results at: $RESULTS_DIR/B0_torture_${RUN_ID}_*.{csv,log}"
