#!/bin/bash
# =============================================================================
# run_k5_prewarm.sh — K5 Pre-warm + Sustained Thermal Throttle Capture
# Target: Raspberry Pi 4B (BCM2711 / Cortex-A72, 8GB) @ 192.168.68.124
#
# Purpose:
#   Reliably push the board into firmware thermal throttle territory and capture
#   the full throttle event in telemetry. A cold K5 start takes ~116s to reach
#   84.7°C but stalls just below the 85°C firmware soft-limit. The pre-warm
#   phase solves this by bringing the board to ~80°C before the main run starts.
#
# Two-phase strategy:
#   Phase 1 (pre-warm, 60s):  All-core FMLA to bring board from idle to ~80°C.
#                              Logged to firestarter_k5_prewarm.csv.
#   Phase 2 (throttle, 300s): All-core FMLA immediately after pre-warm.
#                              Board enters throttle zone within ~30s.
#                              Logged to firestarter_k5_throttle.csv.
#
# Key findings from throttle validation runs:
#   - vcgencmd get_throttled 0xe0000 confirmed firmware throttling occurred but
#     scaling_cur_freq stayed at 1800MHz — the kernel cpufreq layer was blind.
#   - Kernel thermal trip point is 110°C (from thermal_zone0/trip_point_*_temp),
#     so the Linux thermal framework never intervenes below that.
#   - cpuinfo_cur_freq also returned 0 on this board — not available.
#   - Final solution: Actual_Freq_MHz derived from PERF_COUNT_HW_CPU_CYCLES
#     PMU counter (cycles / 10000 per 10ms window) at full 10ms resolution.
#     Throttle_Bits still read via vcgencmd get_throttled at 1s intervals
#     (throttle state changes slowly; popen cost is acceptable at 1s).
#
# What the throttle run revealed (firestarter_k5_throttle.csv):
#   - Throttle_Bits=0x8 (soft-temp-limit active) for 94% of the 287s run.
#   - Throttle_Bits=0x6 (freq capped + actively throttled) for ~8s.
#   - Actual_Freq_MHz stepped: 1800→1775→1726→1677→1629→1580→1531→600MHz.
#   - Average actual clock: 1582MHz vs 1800MHz requested — 12% sustained reduction.
#   - Peak temp: 86.2°C. Kernel_Freq_MHz reported 1800MHz throughout.
#
# LKM implication:
#   The 600MHz panic-drop is preceded by graduated steps into the 1531MHz band.
#   The LKM should issue arch_set_thermal_pressure when Actual_Freq_MHz enters
#   the 1531MHz range, prompting EAS to shed load before the 600MHz cliff fires.
#
# Note: This script is the standalone throttle re-run utility. The full
#   calibration suite (including throttle capture) is in deploy_day1.sh.
#
# Telemetry columns (13 total, all at 10ms polling except Throttle_Bits):
#   Time_ms, INST_RET, ASE_SPEC, VFP_SPEC, STALL_BACKEND,
#   MISD, ASE_Ratio, VFP_Ratio, Stall_Ratio, Temp_C,
#   Kernel_Freq_MHz, Actual_Freq_MHz (PMU cycle counter), Throttle_Bits (1s)
# =============================================================================

PI_USER="pi-sree"
PI_IP="192.168.68.124"
PI_PASS="pi-sree"

echo "==> Cross-compiling updated Firestarter for AARCH64..."
aarch64-linux-gnu-gcc -O3 -pthread src/firestarter.c -o firestarter_arm64

echo "==> Transferring binary to Raspberry Pi 4..."
sshpass -p $PI_PASS scp firestarter_arm64 $PI_USER@$PI_IP:~/misd_calibration/

echo "==> Running K5 pre-warm thermal throttle sequence on Pi..."
sshpass -p $PI_PASS ssh -T $PI_USER@$PI_IP << 'EOF'
    cd ~/misd_calibration

    echo "--- Phase 1: Pre-warm (K5, 60s) ---"
    echo "Start temp: $(cat /sys/class/thermal/thermal_zone0/temp | awk '{printf "%.1f°C\n", $1/1000}')"
    ./firestarter_arm64 5 60 prewarm
    echo "Post-warm temp: $(cat /sys/class/thermal/thermal_zone0/temp | awk '{printf "%.1f°C\n", $1/1000}')"

    echo "--- Phase 2: Sustained throttle run (K5, 300s) ---"
    ./firestarter_arm64 5 300 throttle

    echo "Peak temp reached: $(sort -t',' -k10 -n logs/firestarter_k5_throttle.csv | tail -1 | cut -d',' -f10)°C"
    echo "Done."
EOF

echo "==> Retrieving logs..."
sshpass -p $PI_PASS scp "$PI_USER@$PI_IP:~/misd_calibration/logs/firestarter_k5_prewarm.csv" ./logs/
sshpass -p $PI_PASS scp "$PI_USER@$PI_IP:~/misd_calibration/logs/firestarter_k5_throttle.csv" ./logs/

echo "==> Logs saved: logs/firestarter_k5_prewarm.csv + logs/firestarter_k5_throttle.csv"
