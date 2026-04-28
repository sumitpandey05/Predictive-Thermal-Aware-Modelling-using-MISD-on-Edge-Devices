# Project Manifest: Predictive MISD Thermal Management

## Objective
[cite_start]Build and benchmark a proactive thermal management system for Edge AI on a Raspberry Pi 4 Model B (BCM2711 / Cortex-A72)[cite: 1092].

## Core Hypothesis
[cite_start]Sustained SLM inference eliminates idle cooling gaps, causing reactive OS governors to panic-throttle[cite: 893, 894]. [cite_start]By tracking Mathematical Instruction Set Density (MISD)—the ratio of NEON vector instructions to total instructions retired—we can predict thermal spikes 3-5 seconds before hardware sensors detect them[cite: 885, 886, 929].

## Tech Stack & Environment
* [cite_start]**Target Hardware:** Raspberry Pi 4B (Cortex-A72, 8GB)[cite: 836].
* **Host Environment:** WSL2 Ubuntu (Windows host) for development; native RPi4 for compilation and deployment.
* **Compilation:** Native on RPi4 using `linux-headers-$(uname -r)` (kernel `6.12.47+rpt-rpi-v8`). Cross-compilation via `aarch64-linux-gnu-gcc` from WSL2 also supported (see `lkm/Makefile` vs `lkm/Makefile.rpi`).
* [cite_start]**Kernel Integration:** Custom Linux Loadable Kernel Module (LKM) replacing `step_wise` governor[cite: 1359].
* [cite_start]**Scheduler Hook:** `arch_set_thermal_pressure` to feed capacity constraints to EAS[cite: 1347].

## Sprint Phases
1.  [cite_start]**Day 1:** Firestarter empirical calibration (10ms PMU & sysfs polling)[cite: 1166, 1169]. ✅ Complete
2.  [cite_start]**Day 2:** LKM Governor development, native RPi4 compilation, and EAS integration[cite: 1359, 1361, 1363]. ✅ LKM compiles and loads successfully.
3.  [cite_start]**Day 3:** 600-second continuous `llama.cpp` benchmarking (4-Arm A/B Test)[cite: 988, 1283].

---

## Day 1 Findings & Outcomes

### Kernel Selection
K5 (all-core FMLA) was selected as the primary thermal stress kernel. It runs
K1's FMLA loop simultaneously on all 4 Cortex-A72 cores, producing MISD=0.667
across all cores and generating the highest sustained heat load of any kernel.

**Why MISD ceiling is 0.667, not 1.0:**
Every loop requires SUBS + BNE (2 control instructions) alongside the FMLA
instructions. For K1's 4-FMLA loop: `MISD = 4 / (4+2) = 0.667`. This is the
mathematical maximum for a 4-instruction FMLA loop — not an efficiency gap.
MISD=0.667 means 2/3 of all retired instructions are vector operations; the
execution units are fully saturated.

### Firmware vs Kernel Frequency — Critical Discovery
Initial K5 runs showed `scaling_cur_freq` reporting 1800MHz even at 85.7°C.
Investigation revealed two separate throttle layers on the BCM2711:

| Layer | Mechanism | Trip point | Visible to kernel? |
|---|---|---|---|
| Firmware (VideoCore) | vcgencmd / mailbox | ~80°C soft, ~85°C hard | No |
| Kernel thermal framework | cpufreq + thermal_zone | 110°C (trip_point_0) | Yes |

`vcgencmd get_throttled` returning `0xe0000` (bits 17–19 set) confirmed the
firmware had throttled during the run while the kernel saw nothing. The Linux
thermal framework only intervenes at 110°C on this board.

### Telemetry Evolution (src/firestarter.c)

**Telemetry v1 (initial):**
- Added `Kernel_Freq_MHz` (renamed from `CPU_Freq_MHz`) from `scaling_cur_freq`
- Added `Actual_Freq_MHz` via `vcgencmd measure_clock arm` popen — 1s polling
- Added `Throttle_Bits` via `vcgencmd get_throttled` — 1s polling
- `cpuinfo_cur_freq` evaluated but unavailable on this board's kernel config

**Telemetry v2 (final):**
- `Actual_Freq_MHz` moved to PMU hardware cycle counter
  (`PERF_COUNT_HW_CPU_CYCLES` via `PERF_TYPE_HARDWARE` perf_event):
  `actual_freq_mhz = hw_cycles_per_10ms / 10000`
- Resolution improved from **1s → 10ms** — consistent with all other columns
- Zero additional syscall overhead — uses existing PMU infrastructure
- `Throttle_Bits` retained at 1s via `vcgencmd get_throttled` (state changes
  slowly; popen cost is acceptable at 1s)

**Final CSV columns (13 total, all at 10ms except Throttle_Bits):**
```
Time_ms, INST_RET, ASE_SPEC, VFP_SPEC, STALL_BACKEND,
MISD, ASE_Ratio, VFP_Ratio, Stall_Ratio, Temp_C,
Kernel_Freq_MHz, Actual_Freq_MHz, Throttle_Bits
```

### Pre-warm Methodology
A cold K5 start reaches ~84.7°C in ~116s but stalls below the firmware throttle
threshold. A two-phase pre-warm approach was adopted:
- **Phase 1 (60s):** Brings board from idle to ~80°C.
- **Phase 2 (300s):** Sustained all-core FMLA from ~80°C; enters throttle zone
  within ~30s of Phase 2 start.

### Throttle Run Results (logs/firestarter_k5_throttle.csv)
| Metric | Value |
|---|---|
| Peak temp | 86.2°C |
| Throttle_Bits=0x8 (soft-temp-limit) | 94% of run |
| Throttle_Bits=0x6 (actively throttled) | ~8s |
| Actual_Freq_MHz range | 600MHz – 1800MHz |
| Average Actual_Freq_MHz | 1582MHz (vs 1800MHz requested) — 12% reduction |
| Kernel_Freq_MHz | 1800MHz throughout — never stepped down |

Firmware frequency staircase observed:
`1800→1775→1726→1677→1629→1580→1531→600MHz`
The 600MHz panic-drop is the cliff-edge throttle the LKM must prevent.

### Calibration Script
`deploy_day1.sh` was consolidated into a single orchestrated script (4 sections,
~75 min total) incorporating the pre-warm throttle capture. SSH keepalive
(`ServerAliveInterval=60`) added for stability over long runs. `run_k5_prewarm.sh`
retained as a standalone utility for targeted throttle re-runs.

### Frequency Sweep Expansion
Sweep expanded from **K1+K4** (2 kernels × 4 frequencies = 8 runs) to
**K1+K3+K4+K5** (4 kernels × 4 frequencies = 16 runs):

| Kernel | MISD | Added to sweep? | Reason |
|---|---|---|---|
| K1 | 0.667 | Original | Maximum single-core SIMD ceiling |
| K3 | 0.333 | Added | Intermediate MISD point — separates MISD and V2f coefficients |
| K4 | 0.000 | Original | Integer baseline — zero vector load |
| K5 | 0.667 | Added | All-core realistic inference proxy |
| K2 | 0.667 | Excluded | Same MISD as K1; no unique regression signal |

3 MISD levels × 4 frequencies = 12 combinations enable the regression to
properly isolate α (MISD) from V²×f (dynamic power) contributions.

---

## Day 1 Regression Model — Final Results

### Model Design Decisions

**Target: delta-T (not absolute temperature)**
Initial model predicted absolute `T(t+3s)`, giving `w_temp=0.984` (thermal
inertia dominance) and `w_freq≈0` (frequency effect swamped). Changed target to
`T(t+3s) - T(t)` (temperature rise), forcing the model to learn what *drives*
temperature change. Result: `w_temp` became a small negative value (Newton's
Law of Cooling) and MISD/V2f weights became physically meaningful.

**Feature: V²×f (not raw frequency)**
Dynamic power `P = α × C × V² × f`. Raw frequency is linear but V²×f captures
the nonlinear voltage-frequency relationship. V is not independently measurable
on BCM2711 — it is deterministically set by the DVFS table:

| Freq | Voltage |
|---|---|
| 600 MHz | 825 mV |
| 1000 MHz | 900 mV |
| 1500 MHz | 975 mV |
| 1800 MHz | 1050 mV |

V2f normalized to [0,1] by `V2F_MAX = 1.05² × 1800 = 1984.5` to prevent
fixed-point precision loss (raw V²×f ≈ 400–2000 caused `W_V2F` to round to 0
at ×1024 scaling).

**Features excluded:**
- `Stall_Ratio`: STALL_BACKEND was 0 in all runs — zero variance, zero weight
- `Actual_Freq_MHz` (raw): replaced by V2f which is the physically correct
  nonlinear power proxy

### Trained Weights (155,211 samples)

| Feature | Float weight | Fixed-point (×1024) | Physical meaning |
|---|---|---|---|
| MISD | 0.802 | W_MISD = 821 | °C rise per unit MISD (α proxy) |
| V2f (normalized) | 0.441 | W_V2F = 451 | °C rise per unit V²×f (dynamic power) |
| Temp_C | -0.019 | W_TEMP = -20 | Newton's Law of Cooling — hotter board rises less |
| bias | 0.853 | B_OFFSET = 874 | Baseline thermal drift |
| V2F_MAX_INT | — | 1984 | LKM normalization constant |

### LKM Inference Formula
```c
// Fixed-point arithmetic (all weights ×1024, recover with >>10)
v_mv     = dvfs_lookup(actual_freq_mhz);               // DVFS table lookup
v2f_raw  = (v_mv * v_mv * freq_mhz) / 1000000;        // multiply before divide
v2f_norm = v2f_raw * 1000 / V2F_MAX_INT;               // normalize to [0,1000]
misd_int = (ase_spec + vfp_spec) * 1000 / inst_ret;   // MISD ×1000

delta_C_scaled = (W_MISD * misd_int  / 1000)
               + (W_V2F  * v2f_norm  / 1000)
               + (W_TEMP * temp_C)
               + B_OFFSET;
delta_C = delta_C_scaled >> 10;
T_pred  = temp_C + delta_C;
```

**Note:** `delta_C` values < 1°C truncate to 0 after `>>10`. Acceptable for
threshold detection — `T_pred = current_temp` still triggers intervention when
the board is already in the danger zone.

### LKM Intervention Point
Issue `arch_set_thermal_pressure` when `Actual_Freq_MHz` enters the 1531MHz
band (last graduated step before the 600MHz panic floor), giving EAS time to
shed load before the firmware cliff fires.

**Output:** `lkm/misd_weights.h` — auto-generated fixed-point constants ready
for Day 2 LKM development.

---

## Day 2 — LKM Compilation & Deployment on RPi4

### Native Compilation Approach
The LKM is compiled directly on the RPi4 against the running kernel's headers.
This avoids cross-compilation toolchain mismatches and guarantees the `.ko` is
built against the exact kernel ABI in use.

**Prerequisites (already installed on the Pi):**
```bash
sudo apt install -y linux-headers-$(uname -r) build-essential
# Kernel headers install to: /lib/modules/$(uname -r)/build
# Verified: /usr/src/linux-headers-6.12.47+rpt-rpi-v8
```

**Files to transfer from WSL2 to RPi4:**
```bash
scp lkm/misd_gov.c lkm/misd_weights.h lkm/Makefile.rpi \
    pi-sree@<rpi-ip>:~/misd_gov/lkm/
# Rename Makefile.rpi → Makefile on the Pi, or scp directly as Makefile
```

**Native Makefile (`lkm/Makefile.rpi`) — no cross-compile flags needed:**
```makefile
obj-m += misd_gov.o
KDIR := /lib/modules/$(shell uname -r)/build

all:
    $(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
    $(MAKE) -C $(KDIR) M=$(PWD) clean
```

**Build:**
```bash
cd ~/misd_gov/lkm && make
# Produces: misd_gov.ko
```

**Load / verify / unload:**
```bash
sudo insmod misd_gov.ko
dmesg | tail -20          # expect: "MISD Gov: PMU sampling every 10ms..."
lsmod | grep misd         # verify module is live
sudo rmmod misd_gov       # clean unload
```

**Enable debug logging (throttle/restore decisions):**
```bash
sudo bash -c 'echo "module misd_gov +p" > /sys/kernel/debug/dynamic_debug/control'
sudo dmesg -w
```

### Day 2 LKM Load — Confirmed Output (2026-03-20)
```
[ 2463.804394] misd_gov: loading out-of-tree module taints kernel.
[ 2463.806876] MISD Gov: Loading Hardened Predictive Governor...
[ 2463.807149] MISD Gov: PMU sampling every 10ms, freq decisions every 100ms.
```
Module loaded successfully on kernel `6.12.47+rpt-rpi-v8` on RPi4B.

---

## Day 3 Findings & Outcomes

### The 110°C Critical-Only Trip Point Discovery
During baseline (B0) benchmarking, testing revealed that the Raspberry Pi 4's default thermal framework under specific firmware configurations consists of a *single* `critical` trip point at 110°C (`/sys/class/thermal/thermal_zone0/trip_point_0_temp = 110000`). Because it lacks an `active` or `passive` soft-throttle (typically ~85°C), the default B0 run completely ignored climbing temperatures, maxing out at 1.8GHz up to 85.2°C without throttling. This makes the proactive 75°C MISD LKM absolutely essential to prevent hardware damage during continuous edge LLM inference.

### Fixed-Point Arithmetic & Telemetry Bugfixes
- Fixed a scaling error in the 1024-based fixed-point predictive math where unscaled `delta_C` variables were being added directly to actual temperature millidegrees, resulting in false/constant throttling. The `misd_gov.c` prediction logic now properly converts the offset to millidegrees before the threshold comparison.
- Implemented a custom decimal formatting approach for kernel logs (`%d.%03d`) to safely report float-like telemetry natively in `pr_info` since the Linux kernel fundamentally prohibits floating-point calculation.

### Automating the Edge AI Pipeline
- **WSL2 Deployment Framework**: Created `deploy_test_b3.sh` and `deploy_2arm_benchmark.sh` mimicking the Day 1 architecture to completely automate LKM source transfer, Pi-side compilation, `llama.cpp` inference execution, and `scp` log retrieval over `sshpass` directly from the development host.
- **Unified Visualization Dashboard**: Developed `scripts/dashboard.py`, a Streamlit Plotly web-app that dynamically parses both the 1Hz `*telemetry.csv` and 10Hz `*dmesg.log`. It calculates a dynamic 0-600s timeline, rendering a perfect red-background overlay of the sub-second micro-throttle interventions exactly beneath the TPS and Temperature macro-curves.
