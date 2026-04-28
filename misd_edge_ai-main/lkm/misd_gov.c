#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/workqueue.h>
#include <linux/perf_event.h>
#include <linux/smp.h>
#include <linux/cpu.h>
#include <linux/thermal.h>
#include <linux/pm_qos.h>
#include <linux/cpufreq.h>
#include "misd_weights.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Senior ARM Systems Engineer");
MODULE_DESCRIPTION("Hardened Predictive MISD Thermal Governor for RPi4");

#define POLL_INTERVAL_MS     10
#define DECISION_INTERVAL_MS 100
#define DECISION_TICKS       (DECISION_INTERVAL_MS / POLL_INTERVAL_MS)
#define FIXED_POINT_SHIFT    10

// ARMv8 PMU Event Codes
#define ARMV8_PMUV3_PERFCTR_INST_RETIRED 0x08
#define ARMV8_PMUV3_PERFCTR_ASE_SPEC     0x74
#define ARMV8_PMUV3_PERFCTR_VFP_SPEC     0x75

static struct delayed_work        misd_work;
static struct freq_qos_request    misd_qos_req;
static struct thermal_zone_device *misd_tz;

static struct perf_event *event_inst_ret[NR_CPUS];
static struct perf_event *event_ase_spec[NR_CPUS];
static struct perf_event *event_vfp_spec[NR_CPUS];

// Per-CPU previous values for delta computation
static u64 prev_inst[NR_CPUS];
static u64 prev_ase[NR_CPUS];
static u64 prev_vfp[NR_CPUS];

// Accumulator state for 10ms -> 100ms decoupling
static u64 misd_accumulator;
static int tick_count;

static struct perf_event *create_pmu_event(int cpu, u64 config)
{
    struct perf_event_attr attr;
    struct perf_event *event;

    memset(&attr, 0, sizeof(attr));
    attr.type     = PERF_TYPE_RAW;
    attr.size     = sizeof(attr);
    attr.config   = config;
    attr.disabled = 0;
    attr.pinned   = 1;

    event = perf_event_create_kernel_counter(&attr, cpu, NULL, NULL, NULL);
    return event;
}

static void release_pmu_counters(void)
{
    int cpu;
    for_each_possible_cpu(cpu) {
        if (!IS_ERR_OR_NULL(event_inst_ret[cpu])) {
            perf_event_release_kernel(event_inst_ret[cpu]);
            event_inst_ret[cpu] = NULL;
        }
        if (!IS_ERR_OR_NULL(event_ase_spec[cpu])) {
            perf_event_release_kernel(event_ase_spec[cpu]);
            event_ase_spec[cpu] = NULL;
        }
        if (!IS_ERR_OR_NULL(event_vfp_spec[cpu])) {
            perf_event_release_kernel(event_vfp_spec[cpu]);
            event_vfp_spec[cpu] = NULL;
        }
    }
}

static int init_pmu_counters(void)
{
    int cpu;
    for_each_online_cpu(cpu) {
        event_inst_ret[cpu] = create_pmu_event(cpu, ARMV8_PMUV3_PERFCTR_INST_RETIRED);
        if (IS_ERR_OR_NULL(event_inst_ret[cpu])) goto fail;

        event_ase_spec[cpu] = create_pmu_event(cpu, ARMV8_PMUV3_PERFCTR_ASE_SPEC);
        if (IS_ERR_OR_NULL(event_ase_spec[cpu])) goto fail;

        event_vfp_spec[cpu] = create_pmu_event(cpu, ARMV8_PMUV3_PERFCTR_VFP_SPEC);
        if (IS_ERR_OR_NULL(event_vfp_spec[cpu])) goto fail;
    }
    return 0;

fail:
    release_pmu_counters();
    return -ENODEV;
}

static void misd_work_fn(struct work_struct *work)
{
    u64 inst, ase, vfp, enabled, running;
    u64 delta_inst, delta_ase, delta_vfp;
    u64 total_delta_inst = 0, total_delta_ase = 0, total_delta_vfp = 0;
    u64 misd_avg, predicted_delta_t;
    int cpu, temp_milli;

    // Every 10ms: sample PMU deltas across all CPUs and accumulate
    for_each_online_cpu(cpu) {
        if (IS_ERR_OR_NULL(event_inst_ret[cpu]) ||
            IS_ERR_OR_NULL(event_ase_spec[cpu])  ||
            IS_ERR_OR_NULL(event_vfp_spec[cpu]))
            continue;

        inst = perf_event_read_value(event_inst_ret[cpu], &enabled, &running);
        ase  = perf_event_read_value(event_ase_spec[cpu],  &enabled, &running);
        vfp  = perf_event_read_value(event_vfp_spec[cpu],  &enabled, &running);

        delta_inst = inst - prev_inst[cpu];
        delta_ase  = ase  - prev_ase[cpu];
        delta_vfp  = vfp  - prev_vfp[cpu];

        prev_inst[cpu] = inst;
        prev_ase[cpu]  = ase;
        prev_vfp[cpu]  = vfp;

        total_delta_inst += delta_inst;
        total_delta_ase  += delta_ase;
        total_delta_vfp  += delta_vfp;
    }

    if (total_delta_inst > 0)
        misd_accumulator += ((total_delta_ase + total_delta_vfp) << FIXED_POINT_SHIFT)
                            / total_delta_inst;

    tick_count++;

    // Every 100ms: compute averaged MISD, read temp, make freq decision
    if (tick_count >= DECISION_TICKS) {
        misd_avg = misd_accumulator / DECISION_TICKS;

        if (thermal_zone_get_temp(misd_tz, &temp_milli) < 0)
            temp_milli = 0;

        predicted_delta_t = (W_MISD * misd_avg) >> FIXED_POINT_SHIFT;

        // Fix Bug: Convert 1024-scale prediction to 1000-scale (millidegrees) to match temp_milli
        int pred_dt_milli = (predicted_delta_t * 1000) / 1024;
        int future_temp_milli = temp_milli + pred_dt_milli;

        // Format values for human-readable logging without using kernel-banned floating point math
        int dt_int = pred_dt_milli / 1000;
        int dt_frac = pred_dt_milli % 1000;
        int misd_int = misd_avg / 1024;
        int misd_frac = ((misd_avg % 1024) * 1000) / 1024;

        if (future_temp_milli > 75000) {
            freq_qos_update_request(&misd_qos_req, 1200000);
            pr_info("MISD Gov: throttle — T=%d.%03d°C predicted_dt=%d.%03d°C misd_avg=%d.%03d\n",
                     temp_milli / 1000, temp_milli % 1000, dt_int, dt_frac, misd_int, misd_frac);
        } else {
            freq_qos_update_request(&misd_qos_req, 1800000);
            pr_info("MISD Gov: restore  — T=%d.%03d°C predicted_dt=%d.%03d°C misd_avg=%d.%03d\n",
                     temp_milli / 1000, temp_milli % 1000, dt_int, dt_frac, misd_int, misd_frac);
        }

        misd_accumulator = 0;
        tick_count = 0;
    }

    schedule_delayed_work(&misd_work, msecs_to_jiffies(POLL_INTERVAL_MS));
}

static int __init misd_gov_init(void)
{
    int ret;
    struct cpufreq_policy *policy;

    pr_info("MISD Gov: Loading Hardened Predictive Governor...\n");

    ret = init_pmu_counters();
    if (ret < 0)
        return ret;

    misd_tz = thermal_zone_get_zone_by_name("cpu-thermal");
    if (IS_ERR(misd_tz)) {
        pr_err("MISD Gov: Could not find cpu-thermal zone\n");
        release_pmu_counters();
        return -ENODEV;
    }

    policy = cpufreq_cpu_get(0);
    if (!policy) {
        pr_err("MISD Gov: Could not get cpufreq policy\n");
        release_pmu_counters();
        return -ENODEV;
    }
    freq_qos_add_request(&policy->constraints, &misd_qos_req, FREQ_QOS_MAX, 1800000);
    cpufreq_cpu_put(policy);

    misd_accumulator = 0;
    tick_count       = 0;

    INIT_DELAYED_WORK(&misd_work, misd_work_fn);
    schedule_delayed_work(&misd_work, msecs_to_jiffies(POLL_INTERVAL_MS));

    pr_info("MISD Gov: PMU sampling every %dms, freq decisions every %dms.\n",
            POLL_INTERVAL_MS, DECISION_INTERVAL_MS);
    return 0;
}

static void __exit misd_gov_exit(void)
{
    cancel_delayed_work_sync(&misd_work);
    freq_qos_remove_request(&misd_qos_req);
    release_pmu_counters();
    pr_info("MISD Gov: Unloaded safely.\n");
}

module_init(misd_gov_init);
module_exit(misd_gov_exit);
