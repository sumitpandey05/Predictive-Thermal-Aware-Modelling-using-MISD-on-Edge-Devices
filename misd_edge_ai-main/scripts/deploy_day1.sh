#!/bin/bash
# =============================================================================
# deploy_day1.sh — Firestarter Full Calibration Suite (Day 1)
# Target: Raspberry Pi 4B (BCM2711 / Cortex-A72, 8GB) @ 192.168.68.124
# Host:   WSL2 Ubuntu (cross-compiled via aarch64-linux-gnu-gcc)
#
# Purpose:
#   Runs the complete K1–K5 kernel suite, expanded frequency sweep, and K5
#   pre-warm throttle capture in a single orchestrated pass. Produces the full
#   calibration dataset for training the MISD-based thermal regression model.
#
# Execution order:
#   1. Standard single-core runs  — K1, K2, K3, K4 at default governor (60s each)
#   2. Standard all-core run      — K5 at default governor (120s)
#   3. Frequency sweep            — K1, K3, K4, K5 at 4 fixed frequencies (60s each)
#   4. K5 throttle capture        — pre-warm (60s) + sustained (300s)
#
# Estimated total runtime: ~75 minutes (including cool-downs)
#
# Kernel summary:
#   K1 — 4x FMLA NEON  (MISD=0.667, maximum vector heat)
#   K2 — 4x FADD NEON  (MISD=0.667, vector add)
#   K3 — DUP/MOV NEON  (MISD=0.333, intermediate — added to sweep for
#                        regression coverage between K4 baseline and K1 ceiling)
#   K4 — 4x ADD scalar (MISD=0.000, integer ALU baseline)
#   K5 — K1 on all 4 cores (MISD=0.667, all-core — added to sweep as the
#                        most realistic proxy for inference workload thermals)
#
# Frequency sweep kernels and rationale:
#   K1 + K4 — original endpoints: max SIMD vs max integer across 4 frequencies
#   K3      — intermediate MISD=0.333 point; separates MISD and V2f coefficients
#              in the regression by providing 3 MISD levels × 4 frequencies
#   K5      — all-core behaviour at fixed frequencies; critical for LKM training
#              since inference runs all cores simultaneously
#   K2 excluded — same MISD as K1 (0.667); adds no unique regression signal
#
# Telemetry columns (per-10ms sample, 13 total):
#   Time_ms, INST_RET, ASE_SPEC, VFP_SPEC, STALL_BACKEND,
#   MISD, ASE_Ratio, VFP_Ratio, Stall_Ratio, Temp_C,
#   Kernel_Freq_MHz  — governor-requested freq (scaling_cur_freq, every 10ms)
#   Actual_Freq_MHz  — true hardware clock derived from PMU cycle counter
#                      (PERF_COUNT_HW_CPU_CYCLES / 10000), every 10ms.
#                      Replaces vcgencmd popen approach; zero syscall overhead,
#                      reflects firmware throttle steps at full 10ms resolution.
#   Throttle_Bits    — firmware throttle state (bits 0-3, vcgencmd, every 1s):
#                        bit 0: under-voltage  bit 1: freq capped
#                        bit 2: throttled      bit 3: soft-temp-limit active
#
# Key finding from Day 1 testing:
#   BCM2711 firmware throttles the actual clock independently of the kernel
#   cpufreq layer. Kernel thermal trip is 110°C — all throttling below that
#   is firmware-only. Actual_Freq_MHz (cycle counter) makes it visible at
#   10ms resolution vs the 1s resolution of the prior vcgencmd approach.
#
# Standalone throttle re-run:
#   run_k5_prewarm.sh can be used independently to redo just the throttle
#   capture without running the full calibration suite.
#
# Prerequisites (run once on the Pi):
#   echo 'pi-sree ALL=(ALL) NOPASSWD: /usr/bin/tee /sys/devices/system/cpu/*/cpufreq/*' \
#     | sudo tee /etc/sudoers.d/cpufreq
# =============================================================================

PI_USER="pi-sree"
PI_IP="192.168.68.124"
PI_PASS="pi-sree"

echo "==> Cross-compiling Firestarter for AARCH64..."
aarch64-linux-gnu-gcc -O3 -pthread src/firestarter.c -o firestarter_arm64

echo "==> Transferring binary to Raspberry Pi 4..."
sshpass -p $PI_PASS ssh $PI_USER@$PI_IP 'mkdir -p ~/misd_calibration/logs'
sshpass -p $PI_PASS scp firestarter_arm64 $PI_USER@$PI_IP:~/misd_calibration/

echo "==> Executing Firestarter suite on bare-metal..."
sshpass -p $PI_PASS ssh -T \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=10 \
    $PI_USER@$PI_IP << 'EOF'
    cd ~/misd_calibration

    # Sets all CPUs to a fixed frequency via the userspace governor.
    # BCM2711 uses a single cpufreq policy across all 4 Cortex-A72 cores,
    # so setting cpu0 applies to all cores — including K5 worker threads.
    set_freq() {
        echo userspace | sudo tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor > /dev/null
        echo $1       | sudo tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_setspeed  > /dev/null
    }

    # Restores ondemand governor after the frequency sweep.
    restore_freq() {
        echo ondemand | sudo tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor > /dev/null
    }

    # ----------------------------------------------------------------
    # Section 1: Standard single-core runs (K1–K4), default governor.
    # 60s cool-down between runs to reset thermal baseline.
    # K1–K4 monitor calling process on any CPU (pid=0, cpu=-1).
    # ----------------------------------------------------------------
    echo "--- Section 1: Standard single-core runs ---"

    echo "  Running K1 — FMLA NEON (MISD=0.667, 60s)..."
    taskset -c 0 ./firestarter_arm64 1 60
    sleep 60

    echo "  Running K2 — FADD NEON (MISD=0.667, 60s)..."
    taskset -c 0 ./firestarter_arm64 2 60
    sleep 60

    echo "  Running K3 — DUP/MOV NEON (MISD=0.333, 60s)..."
    taskset -c 0 ./firestarter_arm64 3 60
    sleep 60

    echo "  Running K4 — scalar ADD (MISD=0.000, 60s)..."
    taskset -c 0 ./firestarter_arm64 4 60
    sleep 60

    # ----------------------------------------------------------------
    # Section 2: Standard all-core run (K5), default governor, 120s.
    # K5 monitors all 4 cores per-core (pid=-1, cpu=N).
    # ----------------------------------------------------------------
    echo "--- Section 2: Standard all-core run ---"

    echo "  Running K5 — all-core FMLA (MISD=0.667, 120s)..."
    ./firestarter_arm64 5 120
    sleep 120

    # ----------------------------------------------------------------
    # Section 3: Frequency sweep — K1, K3, K4, K5 at 4 fixed frequencies.
    # Provides 4 kernels × 4 frequencies = 16 combinations covering:
    #   - 3 MISD levels (0.000, 0.333, 0.667) to separate MISD and V2f
    #     coefficients in the regression
    #   - all-core (K5) behaviour at fixed clocks for inference realism
    # Governor forced to userspace to prevent ondemand from overriding.
    # ----------------------------------------------------------------
    echo "--- Section 3: Frequency sweep (K1, K3, K4, K5 × 4 frequencies) ---"

    for freq_hz in 600000 1000000 1500000 1800000; do
        freq_label="${freq_hz%000}MHz"  # e.g. 600000 -> 600MHz

        echo "  K1 at ${freq_label} (MISD=0.667, single-core, 60s)..."
        set_freq $freq_hz
        taskset -c 0 ./firestarter_arm64 1 60 $freq_label
        sleep 60

        echo "  K3 at ${freq_label} (MISD=0.333, single-core, 60s)..."
        set_freq $freq_hz
        taskset -c 0 ./firestarter_arm64 3 60 $freq_label
        sleep 60

        echo "  K4 at ${freq_label} (MISD=0.000, single-core, 60s)..."
        set_freq $freq_hz
        taskset -c 0 ./firestarter_arm64 4 60 $freq_label
        sleep 60

        echo "  K5 at ${freq_label} (MISD=0.667, all-core, 60s)..."
        set_freq $freq_hz
        ./firestarter_arm64 5 60 $freq_label
        sleep 60
    done

    restore_freq
    echo "  Frequency sweep complete."

    # ----------------------------------------------------------------
    # Section 4: K5 thermal throttle capture (pre-warm + sustained).
    # Runs immediately after the sweep while the board is warm, so the
    # 60s pre-warm phase reaches ~80°C quickly and reliably.
    # Phase 1 (prewarm, 60s)  → firestarter_k5_prewarm.csv
    # Phase 2 (throttle, 300s)→ firestarter_k5_throttle.csv
    # ----------------------------------------------------------------
    echo "--- Section 4: K5 thermal throttle capture ---"

    echo "  Phase 1: Pre-warm (K5, 60s)..."
    echo "  Start temp: $(cat /sys/class/thermal/thermal_zone0/temp | awk '{printf "%.1f°C\n", $1/1000}')"
    ./firestarter_arm64 5 60 prewarm
    echo "  Post-warm temp: $(cat /sys/class/thermal/thermal_zone0/temp | awk '{printf "%.1f°C\n", $1/1000}')"

    echo "  Phase 2: Sustained throttle run (K5, 300s)..."
    ./firestarter_arm64 5 300 throttle
    echo "  Peak temp: $(sort -t',' -k10 -n logs/firestarter_k5_throttle.csv | tail -1 | cut -d',' -f10)°C"

    echo "All sections complete."
EOF

echo "==> Retrieving all CSV logs back to WSL2 workspace..."
sshpass -p $PI_PASS scp $PI_USER@$PI_IP:~/misd_calibration/logs/*.csv ./logs/

echo "==> Day 1 Complete. Logs available in ./logs/"
