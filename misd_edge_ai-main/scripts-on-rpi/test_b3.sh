#!/bin/bash
# test_b3.sh — Single test for B3 (MISD Governor) to verify setup
#
# What it does:
#   1. Runs B3 (MISD LKM loaded) for 30s — continuous llama inference + telemetry + dmesg
#
# Results written to: scripts-on-rpi/bench_results/

set -e

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/bench_results"
TELEMETRY_SCRIPT="$SCRIPT_DIR/tps_telemetry.py"

LLAMA_SERVER="${HOME}/llama.cpp/build/bin/llama-server"
MODEL="${HOME}/models/tinyllama.gguf"
LKM_PATH="${HOME}/misd_gov/lkm/misd_gov.ko"

# ── Timing ────────────────────────────────────────────────────────────────────

WARMUP_S=5        # short pre-heat
RUN_S=30          # inference duration for test
SERVER_WAIT_S=15  # time to allow model to load

# ── Inference ─────────────────────────────────────────────────────────────────

SERVER_PORT=8080
THREADS=4
N_PREDICT=150     # ~7s per request at 20 TPS

# ── Helpers ───────────────────────────────────────────────────────────────────

log() { echo "[$(date '+%H:%M:%S')] $*"; }

read_temp() {
    awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp
}

prewarm() {
    log "Pre-warming board for ${WARMUP_S}s..."
    for i in 1 2 3 4; do
        yes > /dev/null &
    done
    WARMUP_PIDS=$(jobs -p)

    sleep "$WARMUP_S"

    # shellcheck disable=SC2086
    kill $WARMUP_PIDS 2>/dev/null || true
    # shellcheck disable=SC2086
    wait $WARMUP_PIDS 2>/dev/null || true

    sleep 1
    log "Pre-warm done — board at $(read_temp)°C"
}

start_server() {
    local ARM=$1
    log "Starting llama-server with --metrics (model: $MODEL)"
    "$LLAMA_SERVER"         \
        -m "$MODEL"         \
        --port "$SERVER_PORT" \
        --threads "$THREADS"  \
        --metrics             \
        > "$RESULTS_DIR/${ARM}_test_server.log" 2>&1 &
    SERVER_PID=$!

    log "Waiting ${SERVER_WAIT_S}s for model to load..."
    sleep "$SERVER_WAIT_S"

    # Verify server is up before starting the run
    if ! curl -sf "http://localhost:${SERVER_PORT}/health" > /dev/null; then
        log "ERROR: Server did not start. Check ${RESULTS_DIR}/${ARM}_test_server.log"
        exit 1
    fi
    log "Server ready (PID=$SERVER_PID)"
}

stop_server() {
    if [ -n "${SERVER_PID:-}" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=""
    fi
}

run_inference_loop() {
    local END_TIME=$(( $(date +%s) + RUN_S ))
    local COUNT=0

    log "Continuous inference test started (${RUN_S}s, ${N_PREDICT} tokens/req)"

    while [ "$(date +%s)" -lt "$END_TIME" ]; do
        curl -sf -X POST "http://localhost:${SERVER_PORT}/completion" \
             -H "Content-Type: application/json" \
             -d "{
                   \"prompt\":      \"Explain how neural networks work:\",
                   \"n_predict\":   ${N_PREDICT},
                   \"temperature\": 0.7
                 }" > /dev/null || true
        COUNT=$(( COUNT + 1 ))
    done

    log "Inference loop done — $COUNT requests completed"
}

# ── Main arm runner ───────────────────────────────────────────────────────────

run_arm() {
    local NAME=$1
    local GOV_LOAD=$2
    local GOV_UNLOAD=$3

    log "════════════════════════════════════════"
    log "TEST ARM: $NAME"
    log "════════════════════════════════════════"

    prewarm

    # Load governor if specified
    if [ -n "$GOV_LOAD" ]; then
        log "Loading governor: $GOV_LOAD"
        eval "$GOV_LOAD"
        sleep 2
        # Confirm LKM loaded
        if ! lsmod | grep -q misd_gov; then
            log "ERROR: misd_gov not in lsmod after insmod"
            exit 1
        fi
        # Ensure debugfs is mounted (needed for dynamic_debug)
        if [ ! -f "/sys/kernel/debug/dynamic_debug/control" ]; then
            log "Attempting to mount debugfs..."
            sudo mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
        fi

        # Enable pr_debug if dynamic_debug is available
        if [ -f "/sys/kernel/debug/dynamic_debug/control" ]; then
            log "misd_gov loaded — enabling pr_debug output"
            echo "module misd_gov +p" | sudo tee /sys/kernel/debug/dynamic_debug/control > /dev/null
        else
            log "WARNING: dynamic_debug not found at /sys/kernel/debug/dynamic_debug/control"
            log "LKM debug prints (throttle/restore) will not be visible in dmesg."
        fi
    fi

    start_server "$NAME"

    # Clear kernel ring buffer then start live capture
    sudo dmesg -c > /dev/null 2>&1 || true
    dmesg -w --time-format iso > "$RESULTS_DIR/${NAME}_test_dmesg.log" 2>&1 &
    DMESG_PID=$!

    # Start Python telemetry
    python3 "$TELEMETRY_SCRIPT" \
        "$RESULTS_DIR/${NAME}_test_telemetry.csv" \
        "${NAME}_TEST" "$RUN_S" &
    TELEMETRY_PID=$!

    # Run continuous inference for full RUN_S duration (blocks here)
    run_inference_loop

    # ── Cleanup ───────────────────────────────────────────────────────────────
    log "Stopping telemetry and dmesg capture..."
    kill "$TELEMETRY_PID" "$DMESG_PID" 2>/dev/null || true
    wait "$TELEMETRY_PID" "$DMESG_PID" 2>/dev/null || true

    stop_server

    if [ -n "$GOV_UNLOAD" ]; then
        log "Unloading governor: $GOV_UNLOAD"
        eval "$GOV_UNLOAD"
        sleep 2
    fi

    log "ARM $NAME test complete — board at $(read_temp)°C"
}

# ── Pre-flight checks ─────────────────────────────────────────────────────────

log "Initialization: B3 Test Script"
log "Results directory: $RESULTS_DIR"

for dep in "$LLAMA_SERVER" "$MODEL" "$TELEMETRY_SCRIPT"; do
    if [ ! -f "$dep" ]; then
        log "ERROR: Required file not found: $dep"
        exit 1
    fi
done

if [ ! -f "$LKM_PATH" ]; then
    log "ERROR: LKM not found at $LKM_PATH — build it first with 'make' on the Pi"
    exit 1
fi

if ! sudo -n true 2>/dev/null; then
    log "ERROR: passwordless sudo required (for insmod/rmmod/dmesg -c)"
    exit 1
fi

mkdir -p "$RESULTS_DIR"

log "Pre-flight checks passed. Starting test run."

# ── Run arm ───────────────────────────────────────────────────────────────────

# B3: MISD Governor
run_arm "B3" \
    "sudo rmmod misd_gov 2>/dev/null || true; sudo insmod ${LKM_PATH}" \
    "sudo rmmod misd_gov 2>/dev/null || true"

log "════════════════════════════════════════"
log "Test run complete."
log "════════════════════════════════════════"
