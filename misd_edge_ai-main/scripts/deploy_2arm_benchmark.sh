#!/bin/bash
# =============================================================================
# deploy_2arm_benchmark.sh — Deploy, build, and run Full Benchmark on RPi
# Target: Raspberry Pi 4B (BCM2711 / Cortex-A72, 8GB) @ 192.168.68.124
# Host:   WSL2 Ubuntu
#
# Purpose:
#   Automatically synchronizes the modified LKM and inference benchmark 
#   scripts to the Raspberry Pi, builds the governor locally on the Pi, runs 
#   run_2arm_benchmark.sh (B0 vs B3 execution), and pulls back all 
#   telemetry & dmesg logs to WSL2.
# =============================================================================

set -e

if [ -z "$1" ]; then
    echo "Usage: ./scripts/deploy_2arm_benchmark.sh \"Description of this run\""
    echo "Example: ./scripts/deploy_2arm_benchmark.sh \"20 min baseline vs B3\""
    exit 1
fi
RUN_COMMENT="$1"
export TZ=Asia/Kolkata
RUN_ID=$(date +%Y%m%d_%H%M%S)

PI_USER="pi-sree"
PI_IP="192.168.68.124"
PI_PASS="pi-sree"
RPI_LKM_DIR="misd_gov/lkm"
RPI_SCRIPTS_DIR="misd_gov/scripts-on-rpi"

echo "==> Transferring modified LKM source and scripts to Raspberry Pi..."
sshpass -p $PI_PASS ssh $PI_USER@$PI_IP "mkdir -p ~/$RPI_LKM_DIR ~/$RPI_SCRIPTS_DIR"

# Copy C source to Pi
sshpass -p $PI_PASS scp lkm/misd_gov.c $PI_USER@$PI_IP:~/$RPI_LKM_DIR/

# Copy scripts to Pi
sshpass -p $PI_PASS scp scripts-on-rpi/run_2arm_benchmark.sh scripts-on-rpi/tps_telemetry.py $PI_USER@$PI_IP:~/$RPI_SCRIPTS_DIR/

echo "==> Building LKM and executing run_2arm_benchmark.sh on bare-metal..."
sshpass -p $PI_PASS ssh -T \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=10 \
    $PI_USER@$PI_IP << EOF
    set -e

    echo "  [Pi] Compiling misd_gov LKM..."
    cd ~/$RPI_LKM_DIR
    make clean > /dev/null
    make > /dev/null

    echo "  [Pi] Running Full 2-Arm Benchmark..."
    cd ~/$RPI_SCRIPTS_DIR
    chmod +x run_2arm_benchmark.sh tps_telemetry.py
    ./run_2arm_benchmark.sh "${RUN_ID}"
EOF

echo "==> Retrieving full benchmark results back to WSL2 workspace..."
mkdir -p scripts-on-rpi/bench_results

HISTORY_FILE="scripts-on-rpi/bench_results/run_history.md"
if [ ! -f "$HISTORY_FILE" ]; then
    echo "# Benchmark Run History" > "$HISTORY_FILE"
    echo "| Run ID | Timestamp (IST) | Description / Notes |" >> "$HISTORY_FILE"
    echo "|---|---|---|" >> "$HISTORY_FILE"
fi
IST_TIME=$(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M:%S')
echo "| \`${RUN_ID}\` | ${IST_TIME} | ${RUN_COMMENT} |" >> "$HISTORY_FILE"

# Use quotes around the remote path so the wildcard is expanded by the remote shell
sshpass -p $PI_PASS scp "$PI_USER@$PI_IP:~/$RPI_SCRIPTS_DIR/bench_results/*${RUN_ID}*" ./scripts-on-rpi/bench_results/ || true

echo "==> Deployment and benchmark complete."
echo "    Check host logs in: ./scripts-on-rpi/bench_results/"
echo "    Run analysis via:   python3 scripts-on-rpi/analyze_results.py"
