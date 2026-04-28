 # Predictive Thermal-Aware Modelling using MISD on Edge Devices

A research-driven Edge AI systems project that builds a **predictive thermal management framework** for sustained LLM inference on constrained edge hardware.  
This work focuses on a **Raspberry Pi 4B** and introduces a **custom Linux Loadable Kernel Module (LKM)** that predicts thermal spikes *before* firmware panic-throttling occurs, using **MISD (Mathematical Instruction Set Density)** derived from PMU counters.

---

## Overview

Modern edge devices often rely on **reactive thermal control**, which responds only after the CPU is already too hot.  
During sustained AI workloads such as `llama.cpp` / TinyLLaMA inference, this causes:

- abrupt firmware throttling,
- frequency collapse,
- unstable throughput,
- wasted energy.

This project proposes a **proactive thermal governor** that:

- samples ARM PMU counters every **10 ms**,
- computes **MISD = (ASE_SPEC + VFP_SPEC) / INST_RETIRED**,
- predicts near-future temperature rise,
- injects thermal pressure into Linux **Energy Aware Scheduler (EAS)**,
- prevents the Raspberry Pi 4 from falling into the **600 MHz panic floor**.

---

## Key Idea

Instead of waiting for temperature sensors to trigger a reactive response, the system predicts thermal stress from **instruction mix** and **power characteristics**.

### Core predictor inputs

- **MISD** → proxy for vector / floating-point activity density
- **V² × f** → proxy for dynamic power
- **Current temperature** → thermal headroom / cooling effect

### Prediction target

The model predicts:

\[
\Delta T = T(t+3s) - T(t)
\]

This enables the governor to intervene *before* hardware firmware throttling becomes severe.

---

## Features

- **Predictive thermal control** for Edge AI workloads
- **Custom Linux Loadable Kernel Module (LKM)** for Raspberry Pi 4
- **10 ms PMU telemetry pipeline**
- **Fixed-point linear regression model** deployed inside kernel space
- **Energy Aware Scheduler (EAS) integration** using:
  - `arch_set_thermal_pressure(cpu, pressure)`
- **Dual frequency telemetry**
  - Kernel-requested frequency
  - Actual hardware frequency (PMU cycle counter based)
- **Firmware throttle detection**
- **Automated deployment scripts** from WSL2 → Raspberry Pi
- **Streamlit + Plotly dashboard** for benchmark visualization

---

## Tech Stack

### Hardware
- Raspberry Pi 4 Model B (BCM2711 / Cortex-A72, 8GB)

### Languages / Tooling
- **C** → Linux kernel module + low-level telemetry
- **Python** → model training, data analysis, dashboard
- **Shell** → deployment and benchmarking automation
- **Makefile / kbuild** → native kernel module compilation

### Runtime / Frameworks
- Linux thermal framework
- ARM PMUv3 counters
- Raspberry Pi firmware telemetry
- `llama.cpp` / TinyLLaMA benchmarking
- Streamlit
- Plotly

---

## Project Architecture

The system consists of four main stages:

1. **Sensor Pipeline**
   - Reads PMU counters (`INST_RETIRED`, `ASE_SPEC`, `VFP_SPEC`, etc.)
   - Samples temperature every **10 ms**
   - Tracks both requested and actual CPU frequency

2. **Predictive Model**
   - Uses a linear regression model trained on **155,211 samples**
   - Learns the relationship between:
     - MISD
     - normalized `V² × f`
     - temperature
   - Outputs predicted near-future thermal rise

3. **Kernel Thermal Governor**
   - Implemented as a **custom LKM**
   - Replaces reactive behavior with proactive intervention
   - Triggers before firmware cliff-edge throttling

4. **Scheduler Integration**
   - Feeds thermal pressure into Linux **EAS**
   - Allows smoother load shedding and frequency scaling
   - Prevents unstable oscillation between high performance and hard throttle

---

## Important Formula

### MISD

```text
MISD = (ASE_SPEC + VFP_SPEC) / INST_RETIRED