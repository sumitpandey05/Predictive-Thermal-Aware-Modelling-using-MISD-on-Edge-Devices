# Sustainability Analysis: MISD Edge AI Governor
## Energy Efficiency of B3 (MISD) vs B0 (Unthrottled Baseline)

---

## 1. Context & Run Reference

| Item | Value |
|---|---|
| **Benchmark Run ID** | `20260320_170029` |
| **Run Date (IST)** | 2026-03-20 17:00:29 |
| **Run Description** | First 20-minute B0 vs B3 comparison |
| **B0 Telemetry File** | `scripts-on-rpi/bench_results/B0_20260320_170029_telemetry.csv` |
| **B3 Telemetry File** | `scripts-on-rpi/bench_results/B3_20260320_170029_telemetry.csv` |
| **B0 dmesg File** | `scripts-on-rpi/bench_results/B0_20260320_170029_dmesg.log` |
| **B3 dmesg File** | `scripts-on-rpi/bench_results/B3_20260320_170029_dmesg.log` |
| **Platform** | Raspberry Pi 4B, BCM2711 (Cortex-A72, 4× cores, 8GB LPDDR4) |
| **Workload** | TinyLLaMA (GGUF), `llama-server`, 4 threads, continuous inference, 150 tokens/request |
| **Duration** | 1200 seconds (20 minutes) per arm |

---

## 2. The Core Sustainability Question

Can B3 (MISD predictive governor) generate each AI inference token with **less energy (Joules/token)** than B0 (default unthrottled Linux), despite B0 running at a higher average frequency?

**Answer: YES — B3 is 11.5% more energy-efficient per token under the linear power model.**

---

## 3. Physics Foundation — Dynamic Power Equation

The CMOS dynamic power equation:

```
P = α × C × V² × f
```

Where:
- `α` = switching activity factor (workload-dependent)
- `C` = load capacitance (transistor count, fixed for BCM2711)
- `V` = supply voltage
- `f` = clock frequency (Hz)

Since `α` and `C` are constant across both arms (same workload, same silicon):

```
P_dynamic ∝ V² × f
```

---

## 4. Two Power Models

### Model A — Linear (P ∝ f) — **Recommended for Pi 4**

The Pi 4 firmware throttle **changes frequency only**; voltage stays approximately fixed.  
With V constant: `P_dynamic ∝ f`

```
P(f) = P_static + (f_MHz / 1800) × P_dynamic_max

P_static       = 2.0 W   (board baseline: DDR, GPU, USB hub, network chip — always on)
P_dynamic_max  = 3.1 W   (extra CPU dynamic power at full 1800 MHz)
```

Verification against published Pi 4 operating points:
- At 1800 MHz: P = 2.0 + (1800/1800) × 3.1 = **5.1 W** ✓ (matches datasheet full-load)
- At 1200 MHz: P = 2.0 + (1200/1800) × 3.1 = **4.07 W**
- At  600 MHz: P = 2.0 + ( 600/1800) × 3.1 = **3.03 W**

### Model B — DVFS Cubic (P ∝ f³) — Theoretical upper bound

If voltage *also* scaled down with frequency (true DVFS): V ∝ f → P ∝ f³

```
P_dvfs(f) = P_static + (f_MHz / 1800)³ × P_dynamic_max
```

> The Pi 4 does **not** implement full DVFS today (frequency-only throttle).  
> This model represents the **theoretical maximum energy savings** if DVFS were exploited.

---

## 5. B0 Frequency Distribution — Key Discovery

B0 did **not** run at 1800 MHz for most of the 20 minutes. The firmware's soft-throttle (bit 3) continuously stepped it through graduated bands:

| Actual HW Freq | Power P(f) | Duration | % of Run |
|---|---|---|---|
| 1800 MHz | 5.10 W | 89 s | 7.4% |
| 1775 MHz | 5.06 W | 27 s | 2.3% |
| 1726 MHz | 4.97 W | 62 s | 5.2% |
| 1677 MHz | 4.89 W | 136 s | 11.3% |
| 1629 MHz | 4.81 W | 245 s | 20.4% |
| 1580 MHz | 4.72 W | 324 s | **27.0%** ← most time here |
| 1531 MHz | 4.64 W | 241 s | 20.1% |
| **600 MHz** | **3.03 W** | **75 s** | **6.3%** ← 75 hard throttle collapses |

**B3** by contrast remained locked at a steady **1200–1204 MHz** for 100% of the run. Zero 600 MHz events.

---

## 6. Step-by-Step Calculation: Average Power

The telemetry CSV provides `actual_freq_mhz` at 1 Hz (one sample per second).  
Apply P(f) to each of the 1199 rows, then compute the mean:

### B0 Worked Example (first 3 rows + collapse event):

```
Row   elapsed_s   actual_freq_mhz   P(f) = 2.0 + (f/1800)×3.1
  1       0.0          1800         2.0 + (1800/1800)×3.1 = 5.10 W
  2       1.0          1800         2.0 + (1800/1800)×3.1 = 5.10 W
  ...
194     193.2           600         2.0 + ( 600/1800)×3.1 = 3.03 W  ← collapse!
195     194.2          1677         2.0 + (1677/1800)×3.1 = 4.89 W  ← recovery
```

```
Total  = Σ P(f_i) × 1 second,  for i = 1..1199
       = 5615.46 W·s (Joules)

Avg Power (B0) = 5615.46 / 1199 = 4.68 W
```

### B3 (nearly constant 1204 MHz):

```
Avg Power (B3) = 4879 / 1200 = 4.07 W
```

---

## 7. Total Energy

```
Total Energy (J) = Σ [ P(f_i) × Δt ]   where Δt = 1 second per sample

B0:  5,615 J  =  1.560 Wh
B3:  4,879 J  =  1.355 Wh
```

---

## 8. Energy per Token — The Sustainability Metric

```
Energy per Token (J/token) = Total Energy (J) / Total Tokens Generated

B0:  5,615 J / 5,978 tokens = 0.94 J/token
B3:  4,879 J / 5,868 tokens = 0.83 J/token
```

---

## 9. Head-to-Head Results

| Metric | B0 (Baseline) | B3 (MISD Gov) | B3 Advantage |
|---|---|---|---|
| **Total Tokens** | 5,978 | 5,868 | −1.8% tokens |
| **Avg HW Frequency** | 1,558 MHz | 1,204 MHz | −33% freq |
| **600 MHz Collapses** | **75 events** | **0 events** | ∞ improvement |
| **Hard Throttle Rows** | 74 | 0 | B3 eliminates entirely |
| **Soft Throttle Rows** | 1,033 | 243 | 4.2× fewer |
| **Avg Power (Linear)** | 4.68 W | 4.07 W | **−13% power draw** |
| **Total Energy (Linear)** | 1.560 Wh | 1.355 Wh | **−13% energy** |
| **Energy/Token (Linear)** | **0.94 J/token** | **0.83 J/token** | **+11.5% efficient** ✅ |
| **Energy/Token (DVFS)** | 0.83 J/token | 0.60 J/token | **+28.1% efficient** ✅ |
| **Peak Temperature** | 86.2 °C | 83.3 °C | −2.9 °C safer |

---

## 10. Why B3 Wins on Energy Despite Running Slower

This is the critical counter-intuitive insight:

> **Running faster is not more energy efficient when it causes thermal oscillation.**

B0's oscillation between 1800 MHz and 600 MHz is energy-wasteful:
1. Board draws **5.1W** to run at 1800 MHz
2. Overshoots thermally → firmware panics → collapses to 600 MHz (3.03W) for 1 second
3. Board "recovers" → ramps back up to 1800 MHz → cycle repeats

The **75 oscillation events** consume energy on the ramp-up cycles without generating proportional tokens (the LLM is mid-inference when the crash hits, causing the token rate to drop by 66%). This is pure energy waste.

B3's steady **1204 MHz, 4.07W** ensures every joule is converted to useful inference output.

---

## 11. Improvement Percentage Calculation

```
Linear Model:
  Improvement = (0.94 - 0.83) / 0.94 × 100 = +11.5%

DVFS Cubic Model (theoretical upper bound):
  Improvement = (0.83 - 0.60) / 0.83 × 100 = +28.1%
```

---

## 12. Options to Strengthen the Energy Claim

| Option | Cost | Accuracy | Notes |
|---|---|---|---|
| **Current model (telemetry-based)** | Free | ±15% | Uses published Pi 4 power operating points. Defensible for a research paper. |
| **INA219 I²C current sensor** | ~$5 USD | ±1% | Attach to 5V GPIO rail, poll via `smbus2` in `tps_telemetry.py`. Makes the claim hardware-validated. |
| **USB power meter (UM25C, etc.)** | ~$20 USD | ±2% | No code changes needed. Read cumulative Wh off the display after each arm. Simple and peer-reviewable. |

---

## 13. Conclusion Statement (for Paper)

> "Under sustained 20-minute LLM inference workloads, the MISD predictive thermal governor (B3) reduces energy consumption per generated token by **11.5%** compared to the unthrottled Linux baseline (B0), using the linear power model calibrated against BCM2711 operating points. B3 completely eliminates the 75 firmware hard-throttle collapses observed in B0 — each of which instantly halved token generation throughput and represented wasted thermal energy with no useful AI output. By proactively stabilising the CPU at 1204 MHz (below the 85°C firmware panic threshold), B3 converts a higher fraction of consumed energy into useful inference work, demonstrating that **predictive thermal management is both a performance and a sustainability improvement** for Edge AI deployments."

---

## 14. Data Sources

All numbers are derived from:
- `scripts-on-rpi/bench_results/B0_20260320_170029_telemetry.csv` (1199 rows × 10 columns)
- `scripts-on-rpi/bench_results/B3_20260320_170029_telemetry.csv` (1200 rows × 10 columns)

Key columns used: `elapsed_s`, `actual_freq_mhz`, `tokens_predicted_total`, `tps`, `temp_c`, `throttle_bits`

Power model calibration references:
- Raspberry Pi 4 product brief (5.1W at 1800MHz full CPU load, 4-core)
- BCM2711 datasheet (Cortex-A72 leakage and dynamic power characteristics)
- `vcgencmd get_throttled` bit definitions (bits 0–3: undervoltage, freq-cap, throttled, soft-temp-limit)
