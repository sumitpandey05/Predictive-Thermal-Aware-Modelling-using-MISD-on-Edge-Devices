# Architecture Manifest: The MISD Control Loop

## 1. The Sensor Pipeline (10ms Polling)

### PMU Counters (Cortex-A72 PMUv3)
* [cite_start]`INST_RETIRED` (0x0008): Total architectural instructions[cite: 1134].
* [cite_start]`ASE_SPEC` (0x0074): Advanced SIMD/NEON instructions[cite: 1134].
* [cite_start]`VFP_SPEC` (0x0075): Scalar FP instructions[cite: 1134].
* `STALL_BACKEND` (0x0024): Cycles stalled waiting for execution units.
* [cite_start]**Equation:** `MISD = (ASE_SPEC + VFP_SPEC) / INST_RETIRED`[cite: 1140].
* K1–K4 monitor the calling process on any CPU (`pid=0, cpu=-1`).
* K5 monitors all 4 cores per-core (`pid=-1, cpu=N`) to capture all worker threads.

### Frequency Telemetry (two-channel, updated after Day 1 findings)
Both channels are logged per sample. The gap between them is the firmware throttle signal.

| Column | Source | Polling | Purpose |
|---|---|---|---|
| `Kernel_Freq_MHz` | `/sys/.../scaling_cur_freq` | Every 10ms | Governor-requested freq — what the kernel intends |
| `Actual_Freq_MHz` | `PERF_COUNT_HW_CPU_CYCLES` PMU counter | Every 10ms | True hardware clock — `hw_cycles / num_cores / 10000` |

**Why two channels:** The BCM2711 VideoCore firmware throttles the actual clock
independently of the Linux cpufreq subsystem. `scaling_cur_freq` remains at
1800MHz even when the firmware drops the hardware clock to 600MHz. The kernel
thermal trip point is 110°C, so the Linux thermal framework never intervenes
below that — all reactive throttling on this board is firmware-only.

**Actual_Freq_MHz implementation (Telemetry v2):** Derived from the hardware
PMU cycle counter (`PERF_TYPE_HARDWARE`, `PERF_COUNT_HW_CPU_CYCLES`) read at
every 10ms polling interval. For K1–K4 (single-core), one fd per process
(`pid=0, cpu=-1`). For K5 (all-core), one fd per core (`pid=-1, cpu=N`);
total cycles summed and divided by `num_cores` to get per-core average.
Resolution improved from 1s → 10ms. Zero additional syscall overhead vs the
prior `vcgencmd measure_clock arm` popen approach.

```c
// K5: sum cycles across all core fds, compute average
uint64_t total_cycles = 0;
for (int c = 0; c < num_cores; c++)
    read(fd_cycles[c], &cnt, sizeof(cnt));
    total_cycles += cnt;
double actual_freq = (double)total_cycles / num_cores / 10000.0;
```

### Firmware Throttle State
| Column | Source | Polling | Bits (current state, 0–3) |
|---|---|---|---|
| `Throttle_Bits` | `vcgencmd get_throttled` (masked to bits 0–3) | Every 1s | 0=under-voltage, 1=freq capped, 2=throttled, 3=soft-temp-limit |

`Throttle_Bits` is read via `popen()` once per second. Throttle state changes
slowly — popen cost is acceptable at 1s. `Actual_Freq_MHz` is now independent
(PMU counter, 10ms) and no longer shares a polling cycle with `Throttle_Bits`.

### Temperature
* `Temp_C`: read from `/sys/class/thermal/thermal_zone0/temp` every 10ms.

---

## 2. The Predictive Model (Linear Regression)

### Features
| Feature | Source | Physical role |
|---|---|---|
| `MISD` | `(ASE_SPEC + VFP_SPEC) / INST_RETIRED` | Activity factor α proxy — vector instruction density |
| `V2f` (normalized) | Derived from `Actual_Freq_MHz` via DVFS table | Dynamic power proxy: P ∝ α × C × V² × f |
| `Temp_C` | `/sys/class/thermal/thermal_zone0/temp` | Thermal state — Newton's cooling headroom |

**Excluded features:**
- `Stall_Ratio` — `STALL_BACKEND` was 0 in all runs; zero variance, zero weight
- Raw `Actual_Freq_MHz` — replaced by `V2f` which captures the nonlinear V²×f
  power relationship that raw frequency alone cannot represent linearly

**V2f derivation:**
```python
# BCM2711 DVFS table: voltage is deterministically set by firmware
DVFS_FREQ_MHZ = [600,   1000,  1500,  1800]
DVFS_VOLT_V   = [0.825, 0.900, 0.975, 1.050]
V2F_MAX = 1.05² × 1800 = 1984.5  # physical maximum on this board

V2f = interp(Actual_Freq_MHz, DVFS table) ** 2 * Actual_Freq_MHz / V2F_MAX
```
Normalizing to [0,1] by dividing by `V2F_MAX` ensures `W_V2F` survives ×1024
fixed-point scaling without rounding to 0 (raw V²×f ≈ 400–2000 made W_V2F=0).

### Target
**delta-T (not absolute temperature):** `T(t+3s) - T(t)` over a 3-second
prediction horizon. Predicting absolute T(t+3s) caused thermal inertia
dominance (`w_temp=0.984`), swamping V2f and MISD contributions. Delta-T
forces the model to learn what *drives* temperature change.

### Trained Weights (155,211 samples)
| Feature | Float | Fixed-point (×1024) | Physical meaning |
|---|---|---|---|
| MISD | 0.802 | `W_MISD = 821` | °C rise per unit MISD |
| V2f (normalized) | 0.441 | `W_V2F = 451` | °C rise per unit V²×f |
| Temp_C | -0.019 | `W_TEMP = -20` | Newton's Law of Cooling |
| bias | 0.853 | `B_OFFSET = 874` | Baseline thermal drift |
| V2F_MAX normalization | — | `V2F_MAX_INT = 1984` | LKM normalization constant |

### LKM Inference Formula (fixed-point)
```c
// DVFS lookup returns millivolts for the given frequency
v_mv     = dvfs_lookup(actual_freq_mhz);
// Multiply before divide to avoid integer truncation
v2f_raw  = (v_mv * v_mv * freq_mhz) / 1000000;
v2f_norm = v2f_raw * 1000 / V2F_MAX_INT;
misd_int = (ase_spec + vfp_spec) * 1000 / inst_ret;

delta_C_scaled = (W_MISD * misd_int  / 1000)
               + (W_V2F  * v2f_norm  / 1000)
               + (W_TEMP * temp_C)
               + B_OFFSET;
delta_C = delta_C_scaled >> 10;
T_pred  = temp_C + delta_C;
```
`delta_C` values < 1°C truncate to 0 after `>>10`. Acceptable — `T_pred =
current_temp` still triggers threshold checks when the board is in the
intervention zone.

---

## 3. The Thermal Governor (LKM)
* [cite_start]Replaces standard Linux `thermal_core` subsystem governor[cite: 891].
* [cite_start]Calculates Thermal Urgency Score based on $T_{pred}$[cite: 1345].
* **Intervention trigger (updated):** Issue `arch_set_thermal_pressure` when
  `Actual_Freq_MHz` enters the 1531MHz band. This is the last graduated step
  before the firmware drops to the 600MHz panic floor. Acting here gives EAS
  time to shed load before the cliff-edge throttle fires.
* The LKM does not wait for the kernel thermal framework (trip at 110°C) — it
  acts on the MISD predictor signal before the firmware throttle escalates.

---

## 4. Energy Aware Scheduler (EAS) Integration
* [cite_start]LKM issues `arch_set_thermal_pressure(cpu, pressure)` (0-1024 scale)[cite: 1337].
* [cite_start]EAS autonomously adjusts frequency scaling and task migration based on effective capacity[cite: 1348].
* By feeding thermal pressure proactively, EAS backs off load before the
  firmware clamp occurs — smoothing the response curve instead of reacting
  to the 600MHz cliff after the fact.

---

## 5. Observed Firmware Throttle Staircase (from firestarter_k5_throttle.csv)
The BCM2711 firmware steps the actual clock down in discrete bands before
dropping to the 600MHz panic floor. This is the sequence the LKM must anticipate:

```
1800 MHz  — nominal
1775 MHz  \
1726 MHz   |
1677 MHz   | graduated firmware steps (soft-temp-limit active, Throttle_Bits=0x8)
1629 MHz   |
1580 MHz   |
1531 MHz  /  ← LKM intervention point
 600 MHz  — panic floor (Throttle_Bits=0x6, actively throttled)
```

---

## 6. LKM Build System & File Inventory

### Compilation Approach
The LKM is built natively on the RPi4 against the running kernel's headers.
The kernel build system (`kbuild`) is invoked via the `KDIR` path, which points
to the installed header tree for the exact kernel version in use. This is
essential — a `.ko` built against mismatched headers will be rejected by
`insmod` due to `vermagic` string mismatch.

```
Host (WSL2)                         RPi4
-----------                         ----
misd_gov.c       --scp-->           ~/misd_gov/lkm/
misd_weights.h   --scp-->           ~/misd_gov/lkm/
Makefile.rpi     --scp-->           ~/misd_gov/lkm/Makefile

                                    make
                                    ↓
                              kbuild invokes cc1 (native gcc)
                                    ↓
                              misd_gov.ko  ← loadable module
```

**Makefile.rpi (native build, no cross-compile flags):**
```makefile
obj-m += misd_gov.o
KDIR  := /lib/modules/$(shell uname -r)/build

all:
    $(MAKE) -C $(KDIR) M=$(PWD) modules
clean:
    $(MAKE) -C $(KDIR) M=$(PWD) clean
```
The `$(MAKE) -C $(KDIR)` invocation hands control to the kernel's own build
system, which handles compiler flags, symbol resolution, and ABI stamping
automatically. `M=$(PWD)` tells kbuild where our out-of-tree source lives.

**Original Makefile (WSL2 cross-compile — retained for reference):**
Sets `ARCH=arm64` and `CROSS_COMPILE=aarch64-linux-gnu-` and points `KDIR`
at the local `rpi-kernel/` source tree clone.

---

### `lkm/` File Inventory

#### Source files (authored — tracked in git)

| File | Role |
|---|---|
| `misd_gov.c` | Main LKM source. Module init/exit, PMU counter lifecycle, the 10ms delayed work function that samples all-CPU PMU deltas, and the 100ms decision loop that reads temperature and issues `freq_qos` requests. |
| `misd_weights.h` | Auto-generated by Day 1 regression (`train_model.py`). Fixed-point constants: `W_MISD=821`, `W_V2F=451`, `W_TEMP=-20`, `B_OFFSET=874`, `V2F_MAX_INT=1984`. Included by `misd_gov.c`. |
| `Makefile` | WSL2 cross-compilation Makefile. Uses `ARCH=arm64`, `CROSS_COMPILE=aarch64-linux-gnu-`, and points `KDIR` at the local `rpi-kernel/` tree. |
| `Makefile.rpi` | Native RPi4 Makefile. No cross-compile flags. `KDIR=/lib/modules/$(uname -r)/build`. Use this on the Pi. |

#### Build artifacts (generated by `make` — not tracked in git)

| File | Role |
|---|---|
| `misd_gov.o` | ARM64 compiled object for `misd_gov.c`. Intermediate — contains the module's machine code before final linking. |
| `misd_gov.mod.c` | Auto-generated by kbuild's `modpost` pass. Contains the module's `vermagic` string (kernel version + compiler ABI marker), `__this_module` struct, and `MODULE_INFO` macros. The kernel checks this at `insmod` time to reject ABI-incompatible modules. |
| `misd_gov.mod.o` | Compiled object from `misd_gov.mod.c`. Linked into the final `.ko`. |
| `.module-common.o` | Kernel-provided stub linked into every out-of-tree module. Contains the init/exit trampoline that the kernel's module loader calls when the module is inserted or removed. |
| `misd_gov.ko` | **The final Loadable Kernel Module.** ELF relocatable produced by linking `misd_gov.o + misd_gov.mod.o + .module-common.o`. This is the file passed to `insmod`. |
| `misd_gov.mod` | Plain-text dependency descriptor. Lists the module name and any other `.ko` files it depends on (none here — self-contained). Used by `modprobe` for dependency graph resolution. |
| `Module.symvers` | Symbol version table. Lists any symbols this module exports (none) and their CRC checksums. Referenced by dependent modules to verify ABI compatibility at link time. |
| `modules.order` | Build order list for multi-module directories. In our single-module case, contains only `misd_gov.ko`. Used by kbuild when installing modules via `make modules_install`. |

#### The `make` build sequence (what happens under the hood)

```
make
 └─ $(MAKE) -C /lib/modules/6.12.47+rpt-rpi-v8/build M=$(PWD) modules
     ├─ CC    misd_gov.o          ← compile misd_gov.c
     ├─ MODPOST Module.symvers    ← run modpost: resolve symbols, check exports
     ├─ CC    misd_gov.mod.o      ← compile auto-generated misd_gov.mod.c
     ├─ CC    .module-common.o    ← compile kernel-provided trampoline stub
     └─ LD    misd_gov.ko         ← link all objects → final .ko
```

#### Load / verify / unload reference

```bash
# Load
sudo insmod ~/misd_gov/lkm/misd_gov.ko

# Verify (expect two MISD Gov lines in dmesg)
dmesg | tail -20
lsmod | grep misd

# Enable pr_info output (throttle/restore decisions at 100ms)
# Note: Upgraded from pr_debug to pr_info to bypass firmware dynamic_debug requirements
sudo dmesg -w

# Unload cleanly
sudo rmmod misd_gov
dmesg | tail -5   # expect: "MISD Gov: Unloaded safely."
```

## 7. Streamlit Visualization Architecture (`dashboard.py`)
To solve the Nyquist sampling discrepancy between macroscopic benchmark TPS intervals (1Hz) and the highly dynamic MISD LKM interventions (10Hz), the architecture employs a unified graphing model:
1. **Datetime Base Alignment**: The dashboard reads `tps_telemetry.csv` and adopts the very first `177xxxxxxx` UNIX epoch timestamp as `T=0`.
2. **Dmesg Parsing**: A regex parser sweeps `B*_dmesg.log`, identifying the exact ISO8601 millisecond timestamps where the LKM executed `throttle` and `restore` operations.
3. **Relative Mapping**: The absolute Python `datetime` objects from the dmesg log are subtracted from `T=0` (`(dt - t0).total_seconds()`).
4. **Plotly Overlay**: The Streamlit interface passes these relative V-spans to Plotly, which paints a dynamic `add_vrect` red background exactly beneath the continuous 1Hz Temperature/TPS traces, rendering the micro-throttles visually.