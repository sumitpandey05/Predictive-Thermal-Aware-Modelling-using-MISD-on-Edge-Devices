import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os
import re
import numpy as np

st.set_page_config(page_title="MISD Sustainability Dashboard", layout="wide")

# ── Constants ─────────────────────────────────────────────────────────────────
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts-on-rpi", "bench_results")
P_STATIC_W  = 2.0    # Board baseline (DDR, GPU, USB, network — always-on)
P_DYN_MAX_W = 3.1    # Max CPU dynamic power at 1800 MHz

def power_linear(f_mhz):
    """P ∝ f  (Pi 4: frequency-only throttle, voltage held constant)"""
    return P_STATIC_W + (f_mhz / 1800.0) * P_DYN_MAX_W

def power_dvfs(f_mhz):
    """P ∝ f³  (theoretical DVFS: voltage also scales with frequency)"""
    return P_STATIC_W + ((f_mhz / 1800.0) ** 3) * P_DYN_MAX_W

# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_run_history(results_dir):
    history_path = os.path.join(results_dir, "run_history.md")
    runs = []
    if not os.path.exists(history_path):
        return runs
    with open(history_path, "r") as f:
        for line in f:
            m = re.match(r"\|\s*`(\S+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
            if m:
                runs.append({
                    "run_id": m.group(1),
                    "timestamp": m.group(2).strip(),
                    "test_type": m.group(3).strip(),
                    "desc": m.group(4).strip()
                })
    return runs

def load_pair(run_id):
    """Load B0 and B3 CSVs for the same run_id. Returns (df_b0, df_b3)."""
    def read(arm):
        # Try new stamped name first, then fall back to legacy
        p1 = os.path.join(RESULTS_DIR, f"{arm}_{run_id}_telemetry.csv")
        p2 = os.path.join(RESULTS_DIR, f"{arm}_telemetry.csv")
        path = p1 if os.path.exists(p1) else (p2 if os.path.exists(p2) else None)
        return pd.read_csv(path) if path else None
    return read("B0"), read("B3")

def enrich(df, label):
    """Add derived energy columns to a telemetry DataFrame."""
    df = df.copy()
    df['arm'] = label
    df['power_linear_w'] = df['actual_freq_mhz'].apply(power_linear)
    df['power_dvfs_w']   = df['actual_freq_mhz'].apply(power_dvfs)
    # Cumulative energy (Joules), assuming 1-second sample interval
    df['energy_linear_j_cum'] = df['power_linear_w'].cumsum()
    df['energy_dvfs_j_cum']   = df['power_dvfs_w'].cumsum()
    # Energy per token
    df['energy_per_token_linear'] = np.where(
        df['tokens_predicted_total'] > 0,
        df['energy_linear_j_cum'] / df['tokens_predicted_total'], np.nan)
    df['energy_per_token_dvfs']   = np.where(
        df['tokens_predicted_total'] > 0,
        df['energy_dvfs_j_cum'] / df['tokens_predicted_total'], np.nan)
    # Running count of 600 MHz collapse events
    df['panic_event'] = (df['actual_freq_mhz'] == 600).astype(int)
    df['panic_count_cumulative'] = df['panic_event'].cumsum()
    return df

# ── UI Layout ─────────────────────────────────────────────────────────────────
st.title("⚡ Sustainability Dashboard: B0 vs B3 Energy Efficiency")
st.caption("Compares energy-per-token, thermal stability, and firmware panic throttle events across both arms.")

# Sidebar controls
with st.sidebar:
    st.header("Run Selection")
    all_runs = parse_run_history(RESULTS_DIR)
    # Get unique run_ids that have BOTH B0 and B3
    run_ids_b0 = {r["run_id"] for r in all_runs if r["test_type"] in ("B0","2arm")}
    run_ids_b3 = {r["run_id"] for r in all_runs if r["test_type"] in ("B3","2arm")}
    paired_ids = sorted((run_ids_b0 | run_ids_b3), reverse=True)  # union — user may have separate entries

    if not paired_ids:
        st.warning("No paired B0+B3 runs found in run_history.md yet.")
        st.stop()

    run_labels = {}
    for rid in paired_ids:
        # find a description for this run_id
        descs = [r["desc"] for r in all_runs if r["run_id"] == rid]
        desc = descs[0] if descs else ""
        run_labels[f"{rid}  |  {desc}"] = rid

    selected_label = st.selectbox("Select Run ID:", list(run_labels.keys()))
    run_id = run_labels[selected_label]
    st.caption(f"Run ID: `{run_id}`")
    st.divider()
    st.subheader("⚙️ Power Model")
    model_choice = st.radio("Energy model:", ["Linear (P ∝ f)  — Pi 4 realistic", "DVFS Cubic (P ∝ f³)  — theoretical"])
    use_dvfs = "Cubic" in model_choice
    energy_col = 'energy_dvfs_j_cum' if use_dvfs else 'energy_linear_j_cum'
    ept_col    = 'energy_per_token_dvfs' if use_dvfs else 'energy_per_token_linear'
    power_col  = 'power_dvfs_w' if use_dvfs else 'power_linear_w'

# ── Load data ─────────────────────────────────────────────────────────────────
df_b0_raw, df_b3_raw = load_pair(run_id)

if df_b0_raw is None or df_b3_raw is None:
    st.error(f"Could not load both B0 and B3 telemetry for Run ID `{run_id}`.")
    st.info("Check that both `B0_{run_id}_telemetry.csv` and `B3_{run_id}_telemetry.csv` exist in bench_results/")
    st.stop()

b0 = enrich(df_b0_raw, "B0")
b3 = enrich(df_b3_raw, "B3")

# ── Summary KPI row ───────────────────────────────────────────────────────────
st.subheader(f"📊 Run Summary  —  `{run_id}`")
kA, kB, kC, kD, kE = st.columns(5)

b0_total_e   = b0[energy_col].iloc[-1]
b3_total_e   = b3[energy_col].iloc[-1]
b0_ept       = b0_total_e / b0['tokens_predicted_total'].max()
b3_ept       = b3_total_e / b3['tokens_predicted_total'].max()
ept_delta    = ((b0_ept - b3_ept) / b0_ept) * 100

kA.metric("B0 Energy", f"{b0_total_e/3600:.3f} Wh", delta=None)
kB.metric("B3 Energy", f"{b3_total_e/3600:.3f} Wh", delta=f"{((b3_total_e-b0_total_e)/b0_total_e*100):+.1f}%")
kC.metric("B0 J/token", f"{b0_ept:.3f} J", delta=None)
kD.metric("B3 J/token", f"{b3_ept:.3f} J", delta=f"{-ept_delta:+.1f}%")
kE.metric("B3 Efficiency Gain", f"+{ept_delta:.1f}%", delta=None)

st.divider()

# ── Chart 1: Firmware HW Frequency ────────────────────────────────────────────
st.subheader("① Firmware Hardware Clock Frequency — B0 vs B3")
fig1 = go.Figure()
fig1.add_trace(go.Scatter(
    x=b0['elapsed_s'], y=b0['actual_freq_mhz'], name="B0 HW Freq",
    line=dict(color='#FF4444', width=2), fill='tozeroy', fillcolor='rgba(255,68,68,0.07)'
))
fig1.add_trace(go.Scatter(
    x=b3['elapsed_s'], y=b3['actual_freq_mhz'], name="B3 HW Freq",
    line=dict(color='#0077FF', width=2), fill='tozeroy', fillcolor='rgba(0,119,255,0.07)'
))
# Mark 600MHz line
fig1.add_hline(y=600, line_dash="dot", line_color="orange",
               annotation_text="600 MHz panic floor", annotation_position="bottom right")
fig1.update_layout(height=350, hovermode="x unified", xaxis_title="Elapsed Time (s)",
                   yaxis_title="Frequency (MHz)",
                   legend=dict(orientation="h", y=1.1, x=0))
st.plotly_chart(fig1, use_container_width=True)

# ── Chart 2: Temperature ──────────────────────────────────────────────────────
st.subheader("② Core Temperature — B0 vs B3")
fig2 = go.Figure()
fig2.add_trace(go.Scatter(
    x=b0['elapsed_s'], y=b0['temp_c'], name="B0 Temperature",
    line=dict(color='#FF4444', width=2.5)
))
fig2.add_trace(go.Scatter(
    x=b3['elapsed_s'], y=b3['temp_c'], name="B3 Temperature",
    line=dict(color='#0077FF', width=2.5)
))
fig2.add_hline(y=85, line_dash="dash", line_color="darkred",
               annotation_text="85°C firmware hard-throttle threshold",
               annotation_position="top right")
fig2.add_hline(y=80, line_dash="dot", line_color="orange",
               annotation_text="80°C soft-throttle onset",
               annotation_position="bottom right")
fig2.update_layout(height=350, hovermode="x unified", xaxis_title="Elapsed Time (s)",
                   yaxis_title="Temperature (°C)",
                   legend=dict(orientation="h", y=1.1, x=0))
st.plotly_chart(fig2, use_container_width=True)

# ── Chart 3: Cumulative 600MHz Panic Throttle Events ─────────────────────────
b0_panics = int(b0['panic_event'].sum())
b3_panics = int(b3['panic_event'].sum())
st.subheader(f"③ Cumulative 600 MHz Panic Throttle Events  —  B0: {b0_panics} events  |  B3: {b3_panics} events")
fig3 = go.Figure()
fig3.add_trace(go.Scatter(
    x=b0['elapsed_s'], y=b0['panic_count_cumulative'], name=f"B0 Panic Events (total {b0_panics})",
    line=dict(color='#FF4444', width=3), fill='tozeroy', fillcolor='rgba(255,68,68,0.1)'
))
fig3.add_trace(go.Scatter(
    x=b3['elapsed_s'], y=b3['panic_count_cumulative'], name=f"B3 Panic Events (total {b3_panics})",
    line=dict(color='#0077FF', width=3), fill='tozeroy', fillcolor='rgba(0,119,255,0.1)'
))
fig3.update_layout(height=300, hovermode="x unified", xaxis_title="Elapsed Time (s)",
                   yaxis_title="Cumulative 600 MHz Collapses",
                   legend=dict(orientation="h", y=1.1, x=0))
st.plotly_chart(fig3, use_container_width=True)

# ── Chart 4a: Instantaneous Power Draw ───────────────────────────────────────
st.subheader("④ Energy Metrics")
tab_power, tab_cumulative, tab_ept = st.tabs([
    "④a  Instantaneous Power (W)",
    "④b  Cumulative Energy (J)",
    "④c  Energy per Token (J/token)"
])

with tab_power:
    fig4a = go.Figure()
    fig4a.add_trace(go.Scatter(
        x=b0['elapsed_s'], y=b0[power_col], name="B0 Power",
        line=dict(color='#FF4444', width=2)
    ))
    fig4a.add_trace(go.Scatter(
        x=b3['elapsed_s'], y=b3[power_col], name="B3 Power",
        line=dict(color='#0077FF', width=2)
    ))
    fig4a.update_layout(height=350, hovermode="x unified", xaxis_title="Elapsed Time (s)",
                        yaxis_title="Estimated Power (W)",
                        legend=dict(orientation="h", y=1.1, x=0))
    st.plotly_chart(fig4a, use_container_width=True)
    model_label = "DVFS Cubic (P ∝ f³)" if use_dvfs else "Linear (P ∝ f)"
    st.caption(f"Model: **{model_label}**. P_static = {P_STATIC_W}W (board baseline), P_dynamic_max = {P_DYN_MAX_W}W at 1800 MHz.")

with tab_cumulative:
    fig4b = go.Figure()
    fig4b.add_trace(go.Scatter(
        x=b0['elapsed_s'], y=b0[energy_col], name=f"B0 Cumulative Energy",
        line=dict(color='#FF4444', width=2.5)
    ))
    fig4b.add_trace(go.Scatter(
        x=b3['elapsed_s'], y=b3[energy_col], name=f"B3 Cumulative Energy",
        line=dict(color='#0077FF', width=2.5)
    ))
    fig4b.update_layout(height=350, hovermode="x unified", xaxis_title="Elapsed Time (s)",
                        yaxis_title="Cumulative Energy (Joules)",
                        legend=dict(orientation="h", y=1.1, x=0))
    st.plotly_chart(fig4b, use_container_width=True)
    st.caption(f"B0 total: **{b0_total_e:.0f} J** ({b0_total_e/3600:.3f} Wh)  |  B3 total: **{b3_total_e:.0f} J** ({b3_total_e/3600:.3f} Wh)")

with tab_ept:
    fig4c = go.Figure()
    # Filter to rows where tokens are actually being generated
    b0_ept_df = b0[b0['tokens_predicted_total'] > 10]
    b3_ept_df = b3[b3['tokens_predicted_total'] > 10]
    fig4c.add_trace(go.Scatter(
        x=b0_ept_df['elapsed_s'], y=b0_ept_df[ept_col], name="B0 J/token",
        line=dict(color='#FF4444', width=2.5)
    ))
    fig4c.add_trace(go.Scatter(
        x=b3_ept_df['elapsed_s'], y=b3_ept_df[ept_col], name="B3 J/token",
        line=dict(color='#0077FF', width=2.5)
    ))
    fig4c.update_layout(height=350, hovermode="x unified", xaxis_title="Elapsed Time (s)",
                        yaxis_title="Energy per Token (J/token)",
                        legend=dict(orientation="h", y=1.1, x=0))
    st.plotly_chart(fig4c, use_container_width=True)
    st.caption(
        f"Final B0: **{b0_ept:.3f} J/token**  |  Final B3: **{b3_ept:.3f} J/token**  |  "
        f"B3 improvement: **+{ept_delta:.1f}%**"
    )

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Data: `B0_{run_id}_telemetry.csv` & `B3_{run_id}_telemetry.csv`  |  "
    f"Power model: {'DVFS Cubic P∝f³' if use_dvfs else 'Linear P∝f'}  |  "
    f"P_static={P_STATIC_W}W  P_dyn_max={P_DYN_MAX_W}W at 1800MHz  |  "
    f"See `sustainability_manifest.md` for full methodology."
)
