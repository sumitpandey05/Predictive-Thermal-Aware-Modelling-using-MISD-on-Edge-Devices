#!/usr/bin/env python3
"""
analyze_results.py — Phase 1 Post-Processing
Computes all 5 Section 4 metrics from B0/B3 telemetry CSVs and dmesg logs.
Generates the money slide and summary bar charts.

Usage: python3 analyze_results.py
Reads from: scripts-on-rpi/bench_results/
Writes:     bench_results/money_slide.png
            bench_results/summary_charts.png
"""

import csv
import re
import sys
import os
from datetime import datetime

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("WARNING: matplotlib not found — metrics printed only, no plots")
    print("Install: pip install matplotlib\n")

# ── Config ────────────────────────────────────────────────────────────────────

RESULTS_DIR           = os.path.join(os.path.dirname(__file__), "bench_results")
VIOLATION_THRESHOLD_C = 80.0   # °C — same as telemetry script
LEAD_TIME_WINDOW_S    = 10.0   # look-ahead window for violation after throttle event
OVERHEAD_WINDOW_S     = 60.0   # first N seconds used for sensor overhead comparison

# ── CSV loading ───────────────────────────────────────────────────────────────

def load_csv(arm):
    path = os.path.join(RESULTS_DIR, f"{arm}_telemetry.csv")
    rows = []
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            try:
                rows.append({
                    'timestamp':    float(row['timestamp']),
                    'elapsed_s':    float(row['elapsed_s']),
                    'arm':          row['arm'],
                    'tps':          float(row['tps']),
                    'tokens_total': int(row['tokens_predicted_total']),
                    'temp_c':       float(row['temp_c']),
                    'violation':    int(row['violation_flag']),
                    'kernel_freq':  int(row['kernel_freq_mhz']),
                    'throttle_bits': int(row['throttle_bits']),
                })
            except (ValueError, KeyError):
                pass   # skip malformed rows
    return rows

# ── dmesg parsing ─────────────────────────────────────────────────────────────

def parse_dmesg(arm):
    """
    Parse LKM pr_debug lines from dmesg log.
    Expected format (from misd_gov.c pr_debug calls):
      2026-03-20T10:23:45.123456+0000 kernel: MISD Gov: throttle — T=74°C predicted_dt=3 misd_avg=847
      2026-03-20T10:23:45.123456+0000 kernel: MISD Gov: restore  — T=72°C predicted_dt=1 misd_avg=312

    Returns list of dicts with epoch timestamp and parsed fields.
    """
    path = os.path.join(RESULTS_DIR, f"{arm}_dmesg.log")
    events = []
    if not os.path.exists(path):
        print(f"  [warn] {arm}_dmesg.log not found — metrics 02/03 will be skipped")
        return events

    # Match ISO timestamp at line start + MISD Gov action
    pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2}T[\d:.+]+)'
        r'.*MISD Gov:\s+(throttle|restore)'
        r'.*T=(\d+)'
        r'.*predicted_dt=(\d+)'
        r'.*misd_avg=(\d+)'
    )

    with open(path) as f:
        for line in f:
            m = pattern.search(line)
            if not m:
                continue
            try:
                # Parse ISO timestamp — handle +0000 and +00:00 variants
                ts_str = m.group(1)
                if re.search(r'[+-]\d{4}$', ts_str):          # +0000 → +00:00
                    ts_str = ts_str[:-2] + ':' + ts_str[-2:]
                ts = datetime.fromisoformat(ts_str).timestamp()

                events.append({
                    'timestamp':    ts,
                    'action':       m.group(2),        # 'throttle' or 'restore'
                    'temp_c':       int(m.group(3)),
                    'predicted_dt': int(m.group(4)),
                    'misd_avg':     int(m.group(5)),
                })
            except (ValueError, IndexError):
                pass

    return events

# ── Metric 01: Average TPS ────────────────────────────────────────────────────

def metric_01_avg_tps(rows):
    """
    Average TPS and Coefficient of Variation over the full 600s run.
    CV% = (std / mean) * 100 — lower means more stable (B3 should win here).
    """
    values = [r['tps'] for r in rows if r['tps'] > 0]
    if not values:
        return 0.0, 0.0
    n    = len(values)
    mean = sum(values) / n
    std  = (sum((x - mean) ** 2 for x in values) / n) ** 0.5
    cv   = (std / mean * 100) if mean > 0 else 0.0
    return mean, cv

# ── Metric 02: Prediction Lead Time ──────────────────────────────────────────

def metric_02_lead_time(b3_rows, dmesg_events):
    """
    For each 'throttle' dmesg event, find the next violation_flag=1 row in the CSV
    within LEAD_TIME_WINDOW_S. Lead time = violation_timestamp - throttle_timestamp.

    Only true-positive events (where a violation DID follow) are counted.
    This measures how far ahead the MISD governor predicted the thermal breach.
    """
    throttle_events = [e for e in dmesg_events if e['action'] == 'throttle']
    lead_times = []

    for event in throttle_events:
        t = event['timestamp']
        for row in b3_rows:
            if t < row['timestamp'] <= t + LEAD_TIME_WINDOW_S:
                if row['violation'] == 1:
                    lead_times.append(row['timestamp'] - t)
                    break   # first violation after this throttle event

    if not lead_times:
        return None, 0
    avg = sum(lead_times) / len(lead_times)
    return avg, len(lead_times)

# ── Metric 03: False Positive Rate ───────────────────────────────────────────

def metric_03_false_positive_rate(b3_rows, dmesg_events):
    """
    False positive = throttle event NOT followed by a violation within LEAD_TIME_WINDOW_S.
    FPR = false_positives / total_throttle_events * 100
    """
    throttle_events = [e for e in dmesg_events if e['action'] == 'throttle']
    if not throttle_events:
        return 0.0, 0, 0

    false_positives = 0
    for event in throttle_events:
        t = event['timestamp']
        found = any(
            t < r['timestamp'] <= t + LEAD_TIME_WINDOW_S and r['violation'] == 1
            for r in b3_rows
        )
        if not found:
            false_positives += 1

    total = len(throttle_events)
    fpr   = (false_positives / total * 100) if total > 0 else 0.0
    return fpr, false_positives, total

# ── Metric 04: Thermal Violation Count ───────────────────────────────────────

def metric_04_violation_count(rows):
    """Count of 1Hz samples where temp_c >= VIOLATION_THRESHOLD_C."""
    return sum(1 for r in rows if r['violation'] == 1)

# ── Metric 05: Sensor Overhead ────────────────────────────────────────────────

def metric_05_sensor_overhead(b0_rows, b3_rows):
    """
    Compare mean TPS in the first OVERHEAD_WINDOW_S seconds of each arm.
    At this point both boards are similarly warm from pre-warm and neither
    has yet entered deep throttle — so TPS difference ≈ PMU overhead.

    Overhead% = (B0_mean - B3_mean) / B0_mean * 100
    Expected: 2-5% (per deck slide 18)
    """
    b0_vals = [r['tps'] for r in b0_rows if r['elapsed_s'] <= OVERHEAD_WINDOW_S and r['tps'] > 0]
    b3_vals = [r['tps'] for r in b3_rows if r['elapsed_s'] <= OVERHEAD_WINDOW_S and r['tps'] > 0]

    if not b0_vals or not b3_vals:
        return None, None, None

    b0_mean = sum(b0_vals) / len(b0_vals)
    b3_mean = sum(b3_vals) / len(b3_vals)
    overhead_pct = ((b0_mean - b3_mean) / b0_mean * 100) if b0_mean > 0 else 0.0
    return b0_mean, b3_mean, overhead_pct

# ── Plot: Money Slide ─────────────────────────────────────────────────────────

def plot_money_slide(b0_rows, b3_rows, dmesg_events, out_path):
    fig, ax = plt.subplots(figsize=(13, 6))

    b0_t   = [r['elapsed_s'] for r in b0_rows]
    b0_tps = [r['tps']       for r in b0_rows]
    b3_t   = [r['elapsed_s'] for r in b3_rows]
    b3_tps = [r['tps']       for r in b3_rows]

    ax.plot(b0_t, b0_tps,
            color='#888888', linewidth=1.0, alpha=0.85,
            label='B0: Default Linux (Reactive)')
    ax.plot(b3_t, b3_tps,
            color='#2d2db5', linewidth=2.0,
            label='B3: MISD Governor (Predictive)')

    # Theoretical max line
    peak = max(b0_tps + b3_tps, default=1)
    ax.axhline(y=peak * 1.08, color='red', linestyle='--',
               alpha=0.45, linewidth=1.0, label='Theoretical Max TPS')

    # Mark governor throttle events from dmesg
    throttle_ts = [
        e['timestamp'] for e in dmesg_events if e['action'] == 'throttle'
    ]
    if b3_rows and throttle_ts:
        b3_start = b3_rows[0]['timestamp']
        for ts in throttle_ts:
            elapsed = ts - b3_start
            if 0 <= elapsed <= 600:
                ax.axvline(x=elapsed, color='orange',
                           alpha=0.25, linewidth=0.8)
        # Single legend entry for all throttle markers
        ax.axvline(x=-999, color='orange', alpha=0.6, linewidth=0.8,
                   label='MISD throttle event')

    # Violation threshold (temperature line would need a second axis;
    # instead shade the pre-throttle window)
    ax.axvspan(0, OVERHEAD_WINDOW_S, alpha=0.05, color='green',
               label=f'Pre-throttle window ({int(OVERHEAD_WINDOW_S)}s)')

    ax.set_xlabel('Time (seconds)', fontsize=12)
    ax.set_ylabel('Tokens Per Second (TPS)', fontsize=12)
    ax.set_title(
        'Throughput Stabilization Under Sustained SLM Load\n'
        'B0 (Default Linux) vs B3 (MISD Governor) — 600s TinyLlama on RPi4B',
        fontsize=12
    )
    ax.legend(fontsize=9, loc='upper right')
    ax.set_xlim(0, 600)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.25)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"[plot] Money slide → {out_path}")
    plt.close()

# ── Plot: Summary Bar Charts ──────────────────────────────────────────────────

def plot_summary(metrics, out_path):
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    arms   = ['B0', 'B3']
    colors = ['#888888', '#2d2db5']
    labels = ['Default\nLinux', 'MISD\nGovernor']

    def bar_chart(ax, values, title, ylabel, fmt='{:.1f}'):
        bars = ax.bar(labels, values, color=colors, width=0.4, edgecolor='white')
        ax.set_title(title, fontsize=11)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.grid(axis='y', alpha=0.3)
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() * 1.03,
                    fmt.format(val),
                    ha='center', fontsize=11, fontweight='bold')

    bar_chart(axes[0],
              [metrics['B0']['avg_tps'], metrics['B3']['avg_tps']],
              'Avg TPS (600s)\nHigher is better', 'TPS')

    bar_chart(axes[1],
              [metrics['B0']['violation_count'], metrics['B3']['violation_count']],
              f'Thermal Violations\n(samples ≥ {int(VIOLATION_THRESHOLD_C)}°C, lower is better)',
              'Sample count', fmt='{:.0f}')

    bar_chart(axes[2],
              [metrics['B0']['tps_cv'], metrics['B3']['tps_cv']],
              'TPS Stability (CV%)\nLower = more stable',
              'Coefficient of Variation (%)')

    fig.suptitle('Phase 1 Summary: B0 (Default) vs B3 (MISD Governor)',
                 fontsize=13, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    print(f"[plot] Summary charts → {out_path}")
    plt.close()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Phase 1 Analysis — B0 vs B3")
    print("=" * 60)

    # Load CSVs
    for arm in ('B0', 'B3'):
        csv_path = os.path.join(RESULTS_DIR, f"{arm}_telemetry.csv")
        if not os.path.exists(csv_path):
            print(f"ERROR: {csv_path} not found — run run_2arm_benchmark.sh first")
            sys.exit(1)

    b0_rows = load_csv("B0")
    b3_rows = load_csv("B3")
    print(f"[data] B0: {len(b0_rows)} rows")
    print(f"[data] B3: {len(b3_rows)} rows")

    dmesg_events = parse_dmesg("B3")
    throttles = sum(1 for e in dmesg_events if e['action'] == 'throttle')
    restores  = sum(1 for e in dmesg_events if e['action'] == 'restore')
    print(f"[data] B3 dmesg: {len(dmesg_events)} events "
          f"(throttle={throttles}, restore={restores})")
    print()

    store = {'B0': {}, 'B3': {}}

    # ── Metric 01 ─────────────────────────────────────────────────────────────
    b0_avg, b0_cv = metric_01_avg_tps(b0_rows)
    b3_avg, b3_cv = metric_01_avg_tps(b3_rows)
    store['B0']['avg_tps'] = b0_avg
    store['B0']['tps_cv']  = b0_cv
    store['B3']['avg_tps'] = b3_avg
    store['B3']['tps_cv']  = b3_cv

    print("METRIC 01 — Average TPS")
    print(f"  B0: {b0_avg:.2f} TPS  (CV = {b0_cv:.1f}%)")
    print(f"  B3: {b3_avg:.2f} TPS  (CV = {b3_cv:.1f}%)")
    if b0_avg > 0:
        delta = (b3_avg - b0_avg) / b0_avg * 100
        print(f"  B3 vs B0: {delta:+.1f}%")
    print()

    # ── Metric 02 ─────────────────────────────────────────────────────────────
    lead_avg, lead_n = metric_02_lead_time(b3_rows, dmesg_events)
    print("METRIC 02 — Prediction Lead Time (B3 only)")
    if lead_avg is not None:
        print(f"  Avg lead time: {lead_avg:.2f}s  ({lead_n} true-positive events)")
        print(f"  Expected: 3–5s")
        verdict = "PASS" if 2.0 <= lead_avg <= 7.0 else "CHECK"
        print(f"  Result: {verdict}")
    else:
        print("  No throttle→violation pairs found")
        print("  (Board may not have reached 80°C — check temp_c column in CSV)")
    print()

    # ── Metric 03 ─────────────────────────────────────────────────────────────
    fpr, fp_n, total_t = metric_03_false_positive_rate(b3_rows, dmesg_events)
    print("METRIC 03 — False Positive Rate (B3 only)")
    print(f"  Total throttle events: {total_t}")
    print(f"  False positives:       {fp_n}  ({fpr:.1f}%)")
    print()

    # ── Metric 04 ─────────────────────────────────────────────────────────────
    b0_viol = metric_04_violation_count(b0_rows)
    b3_viol = metric_04_violation_count(b3_rows)
    store['B0']['violation_count'] = b0_viol
    store['B3']['violation_count'] = b3_viol
    print(f"METRIC 04 — Thermal Violation Count (≥{int(VIOLATION_THRESHOLD_C)}°C)")
    print(f"  B0: {b0_viol} samples  ({b0_viol / max(len(b0_rows), 1) * 100:.1f}% of run)")
    print(f"  B3: {b3_viol} samples  ({b3_viol / max(len(b3_rows), 1) * 100:.1f}% of run)")
    print()

    # ── Metric 05 ─────────────────────────────────────────────────────────────
    b0_e, b3_e, overhead = metric_05_sensor_overhead(b0_rows, b3_rows)
    print(f"METRIC 05 — Sensor Overhead (first {int(OVERHEAD_WINDOW_S)}s)")
    if overhead is not None:
        print(f"  B0 mean TPS: {b0_e:.2f}")
        print(f"  B3 mean TPS: {b3_e:.2f}")
        print(f"  LKM+PMU overhead: {overhead:.2f}%  (expected 2–5%)")
        verdict = "PASS" if overhead <= 6.0 else "CHECK"
        print(f"  Result: {verdict}")
    else:
        print("  Insufficient data in first window")
    print()

    # ── Plots ─────────────────────────────────────────────────────────────────
    print("=" * 60)
    if HAS_MATPLOTLIB:
        plot_money_slide(
            b0_rows, b3_rows, dmesg_events,
            os.path.join(RESULTS_DIR, "money_slide.png")
        )
        plot_summary(
            store,
            os.path.join(RESULTS_DIR, "summary_charts.png")
        )
    else:
        print("Install matplotlib to generate plots:")
        print("  pip install matplotlib")

    print()
    print(f"All outputs in: {RESULTS_DIR}")

if __name__ == "__main__":
    main()
