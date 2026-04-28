#!/usr/bin/env python3
"""
tps_telemetry.py — Phase 1 Telemetry Collector
Polls llama-server /metrics + sysfs at 1Hz for 600s.

Usage: python3 tps_telemetry.py <output_csv> <arm_name>
  arm_name: B0, B3, etc.

Columns:
  timestamp, elapsed_s, arm, tps, tokens_predicted_total,
  temp_c, violation_flag, kernel_freq_mhz, throttle_bits
"""

import requests
import time
import csv
import sys
import subprocess
import re

# ── Configuration ─────────────────────────────────────────────────────────────

METRICS_URL           = "http://localhost:8080/metrics"
VIOLATION_THRESHOLD_C = 80.0   # °C — firmware soft throttle begins here (Day 1 finding)
SAMPLE_INTERVAL_S     = 1.0    # 1Hz
REQUEST_TIMEOUT_S     = 0.5    # don't block the 1Hz loop if server is slow

# ── Args ──────────────────────────────────────────────────────────────────────

if len(sys.argv) < 4:
    print("Usage: tps_telemetry.py <output_csv> <arm_name> <duration_s>")
    sys.exit(1)

OUTPUT_FILE = sys.argv[1]
ARM         = sys.argv[2]
DURATION_S  = int(sys.argv[3])

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_prometheus(text):
    """
    Parse Prometheus text format into {metric_name: float}.
    Strips label sets so metric_name is just the base name.
    Handles both 'llamacpp:' prefix (newer llama.cpp) and 'llama_' prefix.
    """
    metrics = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9eE+\-.]+)', line)
        if m:
            try:
                metrics[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return metrics

def get_tps_from_metrics(metrics, prev_tokens, prev_time):
    """
    Derive instantaneous TPS from the predicted tokens counter delta.
    More reliable than a gauge — works across llama.cpp versions.
    Returns (tps, tokens_total).
    """
    # Try multiple metric names across llama.cpp versions
    tokens_total = int(metrics.get(
        'llamacpp:tokens_predicted_total',
        metrics.get('llama_tokens_predicted_total',
        metrics.get('llamacpp:tokens_predicted',
        0))
    ))

    now = time.time()
    tps = 0.0
    if prev_tokens is not None and prev_time is not None:
        dt    = now - prev_time
        delta = tokens_total - prev_tokens
        if dt > 0 and delta >= 0:
            tps = delta / dt

    return tps, tokens_total, now

def read_temp_c():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read().strip()) / 1000.0

def read_kernel_freq_mhz():
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
            return int(f.read().strip()) // 1000   # kHz → MHz
    except OSError:
        return 0

def read_actual_freq_mhz():
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_clock", "arm"],
            capture_output=True, text=True, timeout=1
        )
        m = re.search(r'frequency\(\d+\)=(\d+)', result.stdout)
        if m:
            return int(m.group(1)) // 1000000
    except Exception:
        return 0

def read_throttle_bits():
    """
    Read vcgencmd get_throttled, return current-state bits (bits 0-3 only).
    Bit 0: under-voltage  Bit 1: freq capped  Bit 2: throttled  Bit 3: soft-temp-limit
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2
        )
        m = re.search(r'0x([0-9a-fA-F]+)', result.stdout)
        if m:
            return int(m.group(1), 16) & 0xF   # mask to current-state bits only
    except Exception:
        pass
    return 0

# ── Main loop ─────────────────────────────────────────────────────────────────

print(f"[telemetry] ARM={ARM}  output={OUTPUT_FILE}  duration={DURATION_S}s",
      flush=True)
print(f"[telemetry] Violation threshold: {VIOLATION_THRESHOLD_C}°C  "
      f"endpoint: {METRICS_URL}", flush=True)

prev_tokens = None
prev_time   = None
rows_written = 0

with open(OUTPUT_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow([
        "timestamp", "elapsed_s", "arm",
        "tps", "tokens_predicted_total",
        "temp_c", "violation_flag",
        "kernel_freq_mhz", "actual_freq_mhz", "throttle_bits",
    ])
    f.flush()

    start_time = time.time()

    while True:
        loop_start = time.time()
        elapsed    = loop_start - start_time

        if elapsed >= DURATION_S:
            break

        # ── 1. llama-server /metrics ──────────────────────────────────────────
        tps          = 0.0
        tokens_total = 0

        try:
            resp    = requests.get(METRICS_URL, timeout=REQUEST_TIMEOUT_S)
            metrics = parse_prometheus(resp.text)
            tps, tokens_total, prev_time = get_tps_from_metrics(
                metrics, prev_tokens, prev_time
            )
            prev_tokens = tokens_total

        except requests.exceptions.Timeout:
            print(f"[telemetry] WARN: /metrics timeout at t={elapsed:.1f}s", flush=True)
        except requests.exceptions.ConnectionError:
            print(f"[telemetry] WARN: /metrics connection error at t={elapsed:.1f}s "
                  f"(server starting?)", flush=True)
        except Exception as e:
            print(f"[telemetry] WARN: metrics error at t={elapsed:.1f}s — {e}", flush=True)

        # ── 2. Sysfs / vcgencmd ───────────────────────────────────────────────
        try:
            temp_c = read_temp_c()
        except Exception as e:
            print(f"[telemetry] WARN: temp read failed — {e}", flush=True)
            temp_c = 0.0

        kernel_freq   = read_kernel_freq_mhz()
        actual_freq   = read_actual_freq_mhz()
        throttle_bits = read_throttle_bits()
        violation     = 1 if temp_c >= VIOLATION_THRESHOLD_C else 0

        # ── 3. Write row ──────────────────────────────────────────────────────
        writer.writerow([
            f"{loop_start:.3f}",
            f"{elapsed:.3f}",
            ARM,
            f"{tps:.4f}",
            tokens_total,
            f"{temp_c:.1f}",
            violation,
            kernel_freq,
            actual_freq,
            throttle_bits,
        ])
        f.flush()
        rows_written += 1

        # ── 4. Sleep remainder of 1s interval ────────────────────────────────
        used = time.time() - loop_start
        remaining = SAMPLE_INTERVAL_S - used
        if remaining > 0:
            time.sleep(remaining)

print(f"[telemetry] Complete — {rows_written} rows written to {OUTPUT_FILE}", flush=True)
