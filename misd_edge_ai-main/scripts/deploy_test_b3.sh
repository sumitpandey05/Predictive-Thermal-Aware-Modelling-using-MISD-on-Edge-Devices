#!/bin/bash
# =============================================================================
# deploy_test_b3.sh — Deploy, build, and run B3 test on Raspberry Pi
# Target: Raspberry Pi 4B (BCM2711 / Cortex-A72, 8GB) @ 192.168.68.124
# Host:   WSL2 Ubuntu
#
# Purpose:
#   Automatically synchronizes the modified LKM and inference test scripts 
#   to the Raspberry Pi, builds the governor locally on the Pi, runs 
#   test_b3.sh, and pulls back the telemetry & dmesg logs to WSL2.
# =============================================================================

set -e

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
sshpass -p $PI_PASS scp scripts-on-rpi/test_b3.sh scripts-on-rpi/tps_telemetry.py $PI_USER@$PI_IP:~/$RPI_SCRIPTS_DIR/

echo "==> Building LKM and executing test_b3.sh on bare-metal..."
sshpass -p $PI_PASS ssh -T \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=10 \
    $PI_USER@$PI_IP << EOF
    set -e

    echo "  [Pi] Compiling misd_gov LKM..."
    cd ~/$RPI_LKM_DIR
    make clean > /dev/null
    make > /dev/null

    echo "  [Pi] Running B3 Test..."
    cd ~/$RPI_SCRIPTS_DIR
    chmod +x test_b3.sh tps_telemetry.py
    ./test_b3.sh
EOF

echo "==> Retrieving B3 test results back to WSL2 workspace..."
mkdir -p scripts-on-rpi/bench_results
# Use quotes around the remote path so the wildcard is expanded by the remote shell
sshpass -p $PI_PASS scp "$PI_USER@$PI_IP:~/$RPI_SCRIPTS_DIR/bench_results/B3_test_*" ./scripts-on-rpi/bench_results/ || true

echo "==> Deployment and test complete."
echo "    Check host logs in: ./scripts-on-rpi/bench_results/"
