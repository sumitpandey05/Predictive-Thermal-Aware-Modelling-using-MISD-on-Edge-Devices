import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import re
import numpy as np
from datetime import datetime

st.set_page_config(page_title="MISD Edge AI Dashboard", layout="wide")

st.title("Edge AI Benchmark Visualization: B0 vs B3")

# Setup paths
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts-on-rpi", "bench_results")

def parse_run_history(results_dir):
    """Parse run_history.md to extract a list of (run_id, timestamp, test_type, description) tuples."""
    history_path = os.path.join(results_dir, "run_history.md")
    runs = []
    if not os.path.exists(history_path):
        return runs
    with open(history_path, "r") as f:
        for line in f:
            m = re.match(r"\|\s*`(\S+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
            if m:
                run_id, timestamp, test_type, desc = m.group(1), m.group(2).strip(), m.group(3).strip(), m.group(4).strip()
                runs.append({"run_id": run_id, "timestamp": timestamp, "test_type": test_type, "desc": desc})
    return runs

def parse_dmesg_throttle_events(dmesg_path, t0_dt):
    events = []
    if not os.path.exists(dmesg_path):
        return events
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},\d{6}\+\d{2}:\d{2})\s+.*MISD Gov:\s+(throttle|restore)")
    current_throttle_start_s = None
    with open(dmesg_path, 'r') as f:
        for line in f:
            match = pattern.search(line.strip())
            if match:
                dt_str = match.group(1).replace(',', '.')
                try:
                    dt = datetime.fromisoformat(dt_str)
                    relative_s = (dt - t0_dt).total_seconds()
                except ValueError:
                    continue
                action = match.group(2)
                if action == "throttle" and current_throttle_start_s is None:
                    current_throttle_start_s = relative_s
                elif action == "restore" and current_throttle_start_s is not None:
                    events.append((current_throttle_start_s, relative_s))
                    current_throttle_start_s = None
    if current_throttle_start_s is not None:
        events.append((current_throttle_start_s, relative_s))
    return events

def load_telemetry(csv_path):
    if not os.path.exists(csv_path):
        return None
    return pd.read_csv(csv_path)

def find_files_for_run(results_dir, run_id, test_type):
    """Find telemetry CSV and dmesg log for a given run ID and test type."""
    # Support both old naming (B0_telemetry.csv) and new (B0_<RUN_ID>_telemetry.csv)
    csv = os.path.join(results_dir, f"{test_type}_{run_id}_telemetry.csv")
    dmesg = os.path.join(results_dir, f"{test_type}_{run_id}_dmesg.log")
    return csv, dmesg

# ── Sidebar / Controls ────────────────────────────────────────────────────────
col1, col2 = st.columns([1, 3])

with col1:
    st.subheader("Selection")

    mode_options = ["B3 MISD Governor", "B0 Baseline (Unthrottled)", "B0 Torture Test (20m)"]
    mode = st.radio("Benchmark Phase:", mode_options)

    if mode == "B3 MISD Governor":
        test_type = "B3"
    elif mode == "B0 Baseline (Unthrottled)":
        test_type = "B0"
    else:
        test_type = "B0_torture"

    # ── Run History Selector ──────────────────────────────────────────────────
    st.divider()
    st.subheader("Run History")
    all_runs = parse_run_history(RESULTS_DIR)
    
    # Filter runs matching the current test_type
    matching_runs = [r for r in all_runs if r["test_type"].lower() == test_type.lower()]
    
    if matching_runs:
        run_options = {f"{r['run_id']}  |  {r['timestamp']}  |  {r['desc']}": r["run_id"] for r in matching_runs}
        selected_label = st.selectbox("Select Run:", list(run_options.keys()), index=0)
        run_id = run_options[selected_label]
        csv_file, dmesg_file = find_files_for_run(RESULTS_DIR, run_id, test_type)
        st.caption(f"Run ID: `{run_id}`")
    else:
        st.info("No runs found in history for this test type. Falling back to latest legacy files.")
        run_id = None
        # Fallback to old legacy naming convention
        csv_file = os.path.join(RESULTS_DIR, f"{test_type}_telemetry.csv")
        dmesg_file = os.path.join(RESULTS_DIR, f"{test_type}_dmesg.log")

# ── Plotting ──────────────────────────────────────────────────────────────────
df = load_telemetry(csv_file)

with col2:
    if df is not None and not df.empty:
        t0_dt = pd.to_datetime(df['timestamp'].iloc[0], unit='s', utc=True)
        throttle_events = parse_dmesg_throttle_events(dmesg_file, t0_dt)

        fig = make_subplots(
            rows=3, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.06,
            subplot_titles=[
                "① Clock Frequency (Firmware vs Linux OS)",
                "② Core Temperature",
                "③ Throughput & Latency Analytics"
            ],
            specs=[[{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": True}]]
        )

        # ── ROW 1: Firmware HW Freq + Linux OS Freq ───────────────────────────
        if 'actual_freq_mhz' in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=df['elapsed_s'], y=df['actual_freq_mhz'],
                    name="Firmware HW Freq (MHz)",
                    line=dict(color='#0077FF', width=2.5),
                    fill='tozeroy', fillcolor='rgba(0,119,255,0.08)'
                ),
                row=1, col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df['elapsed_s'], y=df['kernel_freq_mhz'],
                    name="Linux OS Freq (MHz)",
                    line=dict(color='#FF6600', width=2, dash='dash'),
                ),
                row=1, col=1,
            )

        # ── ROW 2: Temperature ────────────────────────────────────────────────
        fig.add_trace(
            go.Scatter(
                x=df['elapsed_s'], y=df['temp_c'],
                name="Temperature (°C)",
                line=dict(color='firebrick', width=3),
                fill='tozeroy', fillcolor='rgba(178,34,34,0.07)'
            ),
            row=2, col=1,
        )

        # ── ROW 3: Inst TPS + Sustained TPS + Worst-Case Latency ─────────────
        # Pre-compute derived columns before plotting
        df['sustained_tps'] = np.where(df['elapsed_s'] > 0, df['tokens_predicted_total'] / df['elapsed_s'], 0)
        df['latency_ms_per_token'] = np.where(df['tps'] > 0, 1000 / df['tps'], np.nan)
        df['worst_case_latency_ms'] = df['latency_ms_per_token'].rolling(window=5, min_periods=1).max()

        fig.add_trace(
            go.Scatter(
                x=df['elapsed_s'], y=df['tps'],
                name="Inst. TPS",
                line=dict(color='#00AACC', width=1.5),
                opacity=0.7
            ),
            row=3, col=1, secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=df['elapsed_s'], y=df['sustained_tps'],
                name="Sustained Avg TPS",
                line=dict(color='#8800CC', width=3),
            ),
            row=3, col=1, secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=df['elapsed_s'], y=df['worst_case_latency_ms'],
                name="Worst-Case Latency (ms/token)",
                line=dict(color='#FF8800', width=2),
                opacity=0.85
            ),
            row=3, col=1, secondary_y=True,
        )

        # Throttle V-Rects for B3
        if mode == "B3 MISD Governor":
            for idx, (start_s, end_s) in enumerate(throttle_events):
                fig.add_vrect(
                    x0=start_s, x1=end_s,
                    fillcolor="red", opacity=0.2,
                    layer="below", line_width=0,
                    row="all", col=1,
                    annotation_text="1.2GHz" if idx == 0 else ""
                )

        run_label = run_id or "legacy"
        fig.update_layout(
            title=f"{mode}  |  Run: {run_label}",
            height=1000,
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )

        fig.update_xaxes(title_text="Elapsed Time (Seconds)", row=3, col=1)
        fig.update_yaxes(title_text="<b>Frequency (MHz)</b>", row=1, col=1)
        fig.update_yaxes(title_text="<b>Temperature (°C)</b>", row=2, col=1)
        fig.update_yaxes(title_text="<b>TPS</b>", row=3, col=1, secondary_y=False)
        fig.update_yaxes(title_text="<b>Max Latency (ms)</b>", row=3, col=1, secondary_y=True)

        st.plotly_chart(fig, use_container_width=True)

        if mode == "B3 MISD Governor":
            st.info(f"Detected **{len(throttle_events)}** distinct 100ms micro-throttle interventions (background shaded red).")

        colA, colB, colC, colD = st.columns(4)
        colA.metric("Max Temperature", f"{df['temp_c'].max()} °C")
        colB.metric("Total Tokens", f"{int(df['tokens_predicted_total'].max())}")
        colC.metric("Avg Sustained TPS", f"{df['sustained_tps'].iloc[-1]:.1f}")
        colD.metric("Duration", f"{df['elapsed_s'].max():.0f} s")

    else:
        st.error(f"Could not find telemetry data at `{csv_file}`.")
        st.info("Run a benchmark first, or select a different Run ID.")
