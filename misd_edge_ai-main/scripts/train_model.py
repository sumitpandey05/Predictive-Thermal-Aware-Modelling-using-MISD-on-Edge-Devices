import os
import glob
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

# Configuration — paths are anchored to this script's location so the script
# can be run from any working directory (e.g. project root or scripts/).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, '..', 'logs')
PREDICTION_HORIZON_MS = 3000
POLLING_INTERVAL_MS = 10
SHIFT_ROWS = PREDICTION_HORIZON_MS // POLLING_INTERVAL_MS

# BCM2711 DVFS voltage table (MHz -> Volts).
# V is not independently observable on this platform — it is deterministically
# set by the firmware DVFS table for each frequency operating point.
# Source: standard Pi 4 OPP table (stock, no overclock).
DVFS_FREQ_MHZ = np.array([600,   1000,  1500,  1800])
DVFS_VOLT_V   = np.array([0.825, 0.900, 0.975, 1.050])

# Physical maximum V2f on this board: V²×f at max DVFS point (1800MHz, 1.05V).
# Normalizes V2f to [0,1] — same scale as MISD — so the regression weight
# is large enough to survive *1024 fixed-point scaling without rounding to 0.
# LKM must apply the same normalization: v2f_norm = v2f_raw / V2F_MAX_INT.
V2F_MAX = float((DVFS_VOLT_V[-1] ** 2) * DVFS_FREQ_MHZ[-1])  # 1.05² × 1800 = 1984.5

def compute_v2f(freq_mhz_series):
    # Derive V from frequency via linear interpolation of the DVFS table,
    # compute V²×f, then normalize to [0,1] by dividing by V2F_MAX.
    v = np.interp(freq_mhz_series, DVFS_FREQ_MHZ, DVFS_VOLT_V)
    return ((v ** 2) * freq_mhz_series) / V2F_MAX

def load_and_preprocess_data(log_dir):
    all_files = glob.glob(os.path.join(log_dir, "*.csv"))
    df_list = []

    for file in all_files:
        df = pd.read_csv(file)

        # Target: temperature RISE over the next 3 seconds (delta-T).
        # Predicting delta rather than absolute T(t+3s) removes the
        # thermal-inertia dominance of current temperature, forcing the
        # model to learn what drives temperature change: V²×f (dynamic
        # power) and MISD (vector instruction density / activity factor α).
        df['Target_Delta_C'] = df['Temp_C'].shift(-SHIFT_ROWS) - df['Temp_C']

        # Derived feature: V²×f — physical dynamic power proxy.
        # Replaces raw Actual_Freq_MHz with the correctly scaled power term.
        df['V2f'] = compute_v2f(df['Actual_Freq_MHz'])

        # Drop trailing rows that lack future temperature data.
        df = df.dropna(subset=['Target_Delta_C'])
        df_list.append(df)

    if not df_list:
        return pd.DataFrame()

    return pd.concat(df_list, ignore_index=True)

def train_and_generate_header():
    print("==> Loading and preprocessing telemetry data...")
    data = load_and_preprocess_data(LOG_DIR)

    if data.empty:
        print(f"Error: No CSV data found in {LOG_DIR}.")
        return

    # Features:
    #   MISD  — vector instruction density (activity factor α proxy)
    #   V2f   — V²×f dynamic power proxy derived from DVFS table
    #   Temp_C — current temperature (thermal state / cooling headroom)
    # Stall_Ratio excluded: STALL_BACKEND was 0 in all runs (zero variance).
    # Actual_Freq_MHz excluded: replaced by V2f which captures the V²×f
    # relationship that raw frequency alone cannot represent linearly.
    features = ['MISD', 'V2f', 'Temp_C']
    X = data[features]
    y = data['Target_Delta_C']

    print(f"==> Training Linear Regression model on {len(data)} samples...")
    model = LinearRegression()
    model.fit(X, y)

    w_misd, w_v2f, w_temp = model.coef_
    b_offset = model.intercept_

    print("==> Model Weights (Floating Point):")
    print(f"    w_misd:   {w_misd:.6f}   (°C rise per unit MISD)")
    print(f"    w_v2f:    {w_v2f:.6f}   (°C rise per unit V²·MHz)")
    print(f"    w_temp:   {w_temp:.6f}   (°C rise per °C current temp)")
    print(f"    b_offset: {b_offset:.6f}")

    # --- Fixed-Point Math Conversion for the LKM ---
    # All weights scaled by 1024 (10-bit shift). The LKM computes:
    #   delta_C_scaled = (W_MISD * misd) + (W_V2F * v2f_norm) + (W_TEMP * temp) + B_OFFSET
    #   delta_C        = delta_C_scaled >> 10
    #
    # V2f is normalized to [0,1] before training (divided by V2F_MAX=1984.5).
    # LKM must apply the same normalization using the integer constant V2F_MAX_INT:
    #   v_mv      = dvfs_lookup(actual_freq_mhz)                 // millivolts
    #   v2f_raw   = (v_mv * v_mv * freq_mhz) / 1000000          // V²·MHz — multiply before
    #                                                             // dividing to avoid truncation
    #   v2f_norm  = v2f_raw * 1000 / V2F_MAX_INT                // normalized, *1000 to retain precision
    V2F_MAX_INT  = int(round(V2F_MAX))                    # 1985
    W_MISD_INT   = int(round(w_misd   * 1024.0))
    W_V2F_INT    = int(round(w_v2f    * 1024.0))
    W_TEMP_INT   = int(round(w_temp   * 1024.0))
    B_OFFSET_INT = int(round(b_offset * 1024.0))

    # Warn if any weight rounds to zero — indicates precision loss.
    for name, val, scaled in [('W_MISD', w_misd, W_MISD_INT),
                               ('W_V2F',  w_v2f,  W_V2F_INT),
                               ('W_TEMP', w_temp, W_TEMP_INT)]:
        if scaled == 0 and val != 0.0:
            print(f"  WARNING: {name} float={val:.6f} rounds to 0 at *1024 scale")

    header_path = os.path.join(SCRIPT_DIR, '..', 'lkm', 'misd_weights.h')
    os.makedirs(os.path.dirname(header_path), exist_ok=True)

    header_content = f"""#ifndef MISD_WEIGHTS_H
#define MISD_WEIGHTS_H

/* Auto-generated fixed-point weights for Predictive Thermal Governor.
 * Prediction Horizon : {PREDICTION_HORIZON_MS} ms
 * Target             : delta-T (temperature rise over horizon, degrees C)
 *
 * Fixed-Point Scaling (all weights * 1024, recover with >> 10):
 *   delta_C_scaled = (W_MISD * misd) + (W_V2F * v2f)
 *                  + (W_TEMP * temp_C) + B_OFFSET;
 *   delta_C = delta_C_scaled >> 10;
 *   T_pred  = current_temp_C + delta_C;
 *
 * V2f normalization in LKM (V2f is normalized to [0,1] during training):
 *   v_mv     = dvfs_lookup(actual_freq_mhz);           // millivolts, see table
 *   v2f_raw  = (v_mv * v_mv / 1000000) * freq_mhz;    // V²·MHz
 *   v2f_norm = v2f_raw * 1000 / V2F_MAX_INT;           // normalized (*1000 for precision)
 *   then use v2f_norm in: W_V2F * v2f_norm / 1000
 *
 * BCM2711 DVFS table (stock, no overclock):
 *   600 MHz -> 825 mV    1000 MHz -> 900 mV
 *  1500 MHz -> 975 mV    1800 MHz -> 1050 mV
 *  (interpolate linearly for intermediate firmware throttle steps)
 */

#define V2F_MAX_INT  {V2F_MAX_INT}
#define W_MISD       {W_MISD_INT}
#define W_V2F        {W_V2F_INT}
#define W_TEMP       {W_TEMP_INT}
#define B_OFFSET     {B_OFFSET_INT}

#endif // MISD_WEIGHTS_H
"""

    with open(header_path, 'w') as f:
        f.write(header_content)

    print(f"==> Successfully generated {header_path}")

if __name__ == "__main__":
    train_and_generate_header()
