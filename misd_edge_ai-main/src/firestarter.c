#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <unistd.h>
#include <string.h>
#include <fcntl.h>
#include <pthread.h>
#include <time.h>
#include <stdatomic.h>
#include <linux/perf_event.h>
#include <sys/syscall.h>
#include <sys/ioctl.h>

// PMUv3 Event Codes for Cortex-A72
#define EV_INST_RETIRED    0x0008
#define EV_ASE_SPEC        0x0074
#define EV_VFP_SPEC        0x0075
#define EV_STALL_BACKEND   0x0024  // Cycles stalled waiting for execution units

// Hardware cycle counter — used to derive Actual_Freq_MHz at 10ms resolution.
// actual_freq_mhz = hw_cycles_in_10ms / 10000  (cycles/μs = MHz)
// Uses PERF_TYPE_HARDWARE so the kernel maps it to the correct PMU cycle
// counter (PMCCNTR on Cortex-A72) without needing the raw event code.
#define EV_HW_CPU_CYCLES   PERF_COUNT_HW_CPU_CYCLES

#define CHUNK_ITERATIONS 1000000L  // ~1ms of work per chunk at 1800MHz
#define NUM_CORES        4

_Atomic int running = 1;
// Arrays sized for NUM_CORES; K1-K4 use index 0 only, K5 uses all 4
int fd_inst[NUM_CORES], fd_ase[NUM_CORES], fd_vfp[NUM_CORES], fd_stall[NUM_CORES];
int fd_cycles[NUM_CORES];  // hardware cycle counter — derives Actual_Freq_MHz
int num_cores;
FILE *csv_file;

long perf_event_open(struct perf_event_attr *hw_event, pid_t pid, int cpu, int group_fd, unsigned long flags) {
    return syscall(__NR_perf_event_open, hw_event, pid, cpu, group_fd, flags);
}

int setup_pmu_counter(uint64_t config, pid_t pid, int cpu) {
    struct perf_event_attr pe;
    memset(&pe, 0, sizeof(struct perf_event_attr));
    pe.type = PERF_TYPE_RAW;
    pe.size = sizeof(struct perf_event_attr);
    pe.config = config;
    pe.disabled = 1;
    pe.exclude_kernel = 1;
    pe.exclude_hv = 1;

    int fd = perf_event_open(&pe, pid, cpu, -1, 0);
    if (fd == -1) {
        fprintf(stderr, "Error opening PMU counter %lx (pid=%d cpu=%d). Run as root or set perf_event_paranoid to -1.\n", config, pid, cpu);
        exit(EXIT_FAILURE);
    }
    return fd;
}

// Sets up the hardware CPU cycle counter (PERF_TYPE_HARDWARE).
// exclude_kernel=0 so all cycles on the monitored pid/cpu are counted,
// giving true wall-clock frequency rather than just userspace cycles.
int setup_cycle_counter(pid_t pid, int cpu) {
    struct perf_event_attr pe;
    memset(&pe, 0, sizeof(struct perf_event_attr));
    pe.type     = PERF_TYPE_HARDWARE;
    pe.size     = sizeof(struct perf_event_attr);
    pe.config   = EV_HW_CPU_CYCLES;
    pe.disabled = 1;
    pe.exclude_kernel = 0;
    pe.exclude_hv     = 1;

    int fd = perf_event_open(&pe, pid, cpu, -1, 0);
    if (fd == -1) {
        fprintf(stderr, "Error opening cycle counter (pid=%d cpu=%d). Run as root or set perf_event_paranoid to -1.\n", pid, cpu);
        exit(EXIT_FAILURE);
    }
    return fd;
}

double read_sysfs_temp() {
    char buf[16];
    int fd = open("/sys/class/thermal/thermal_zone0/temp", O_RDONLY);
    if (fd < 0) return 0.0;
    (void)read(fd, buf, sizeof(buf));
    close(fd);
    return atof(buf) / 1000.0;
}

double read_sysfs_freq() {
    char buf[16];
    int fd = open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", O_RDONLY);
    if (fd < 0) return 0.0;
    (void)read(fd, buf, sizeof(buf));
    close(fd);
    return atof(buf) / 1000.0;  // kHz -> MHz
}

// Returns current firmware throttle state (bits 0-3 of vcgencmd get_throttled).
// Bit 0: under-voltage  Bit 1: freq capped  Bit 2: throttled  Bit 3: soft-temp-limit
// Called every 1s (every 100 telemetry ticks) to avoid fork overhead.
unsigned int read_throttle_bits() {
    char buf[32];
    FILE *f = popen("vcgencmd get_throttled", "r");
    if (!f) return 0;
    if (!fgets(buf, sizeof(buf), f)) { pclose(f); return 0; }
    pclose(f);
    char *eq = strchr(buf, '=');
    if (!eq) return 0;
    return (unsigned int)strtoul(eq + 1, NULL, 16) & 0xf;
}

void* telemetry_thread(void* arg) {
    struct timespec req = {0, 10000000}; // 10ms polling
    int ms_elapsed = 0;
    int iter = 0;
    uint64_t val;
    unsigned int throttle_bits = 0;

    for (int i = 0; i < num_cores; i++) {
        ioctl(fd_inst[i],   PERF_EVENT_IOC_ENABLE, 0);
        ioctl(fd_ase[i],    PERF_EVENT_IOC_ENABLE, 0);
        ioctl(fd_vfp[i],    PERF_EVENT_IOC_ENABLE, 0);
        ioctl(fd_stall[i],  PERF_EVENT_IOC_ENABLE, 0);
        ioctl(fd_cycles[i], PERF_EVENT_IOC_ENABLE, 0);
    }

    while (running) {
        nanosleep(&req, NULL);

        // Sum counts across all monitored cores
        uint64_t total_inst = 0, total_ase = 0, total_vfp = 0, total_stall = 0;
        uint64_t total_cycles = 0;
        for (int i = 0; i < num_cores; i++) {
            (void)read(fd_inst[i],   &val, sizeof(uint64_t)); total_inst   += val;
            (void)read(fd_ase[i],    &val, sizeof(uint64_t)); total_ase    += val;
            (void)read(fd_vfp[i],    &val, sizeof(uint64_t)); total_vfp    += val;
            (void)read(fd_stall[i],  &val, sizeof(uint64_t)); total_stall  += val;
            (void)read(fd_cycles[i], &val, sizeof(uint64_t)); total_cycles += val;
        }

        // Derive actual execution frequency from hardware cycle count.
        // Average per-core cycles over the 10ms window: cycles/μs = MHz.
        // Reflects true clock including firmware throttle steps, at 10ms resolution.
        double actual_freq = (double)total_cycles / num_cores / 10000.0;

        double temp        = read_sysfs_temp();
        double freq        = read_sysfs_freq();

        // Throttle state is slow-moving — poll firmware once per second.
        if (iter % 100 == 0)
            throttle_bits = read_throttle_bits();
        iter++;

        double misd        = (total_inst > 0) ? ((double)(total_ase + total_vfp) / total_inst) : 0.0;
        double ase_ratio   = (total_inst > 0) ? ((double)total_ase   / total_inst) : 0.0;
        double vfp_ratio   = (total_inst > 0) ? ((double)total_vfp   / total_inst) : 0.0;
        double stall_ratio = (total_inst > 0) ? ((double)total_stall / total_inst) : 0.0;

        fprintf(csv_file, "%d,%lu,%lu,%lu,%lu,%.3f,%.3f,%.3f,%.3f,%.1f,%.0f,%.0f,%u\n",
                ms_elapsed, total_inst, total_ase, total_vfp, total_stall,
                misd, ase_ratio, vfp_ratio, stall_ratio, temp, freq, actual_freq, throttle_bits);

        ms_elapsed += 10;

        // Reset counters for next 10ms window
        for (int i = 0; i < num_cores; i++) {
            ioctl(fd_inst[i],   PERF_EVENT_IOC_RESET, 0);
            ioctl(fd_ase[i],    PERF_EVENT_IOC_RESET, 0);
            ioctl(fd_vfp[i],    PERF_EVENT_IOC_RESET, 0);
            ioctl(fd_stall[i],  PERF_EVENT_IOC_RESET, 0);
            ioctl(fd_cycles[i], PERF_EVENT_IOC_RESET, 0);
        }
    }
    return NULL;
}

// --- Chunk runners (fixed small iteration count, called in a duration loop) ---

void run_k1_chunk() {
    long iterations = CHUNK_ITERATIONS;
    asm volatile(
        "1: \n"
        " fmla v0.4s, v1.4s, v2.4s \n"
        " fmla v3.4s, v4.4s, v5.4s \n"
        " fmla v6.4s, v7.4s, v8.4s \n"
        " fmla v9.4s, v10.4s, v11.4s \n"
        " subs %0, %0, #1 \n"
        " bne 1b \n"
        : "+r"(iterations) :: "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "cc"
    );
}

void run_k2_chunk() {
    long iterations = CHUNK_ITERATIONS;
    asm volatile(
        "1: \n"
        " fadd v0.4s, v1.4s, v2.4s \n"
        " fadd v3.4s, v4.4s, v5.4s \n"
        " fadd v6.4s, v7.4s, v8.4s \n"
        " fadd v9.4s, v10.4s, v11.4s \n"
        " subs %0, %0, #1 \n"
        " bne 1b \n"
        : "+r"(iterations) :: "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11", "cc"
    );
}

void run_k3_chunk() {
    long iterations = CHUNK_ITERATIONS;
    asm volatile(
        "1: \n"
        " dup v0.4s, w0 \n"
        " mov v1.16b, v2.16b \n"
        " dup v3.4s, w0 \n"
        " mov v4.16b, v5.16b \n"
        " subs %0, %0, #1 \n"
        " bne 1b \n"
        : "+r"(iterations) :: "v0", "v1", "v2", "v3", "v4", "v5", "w0", "cc"
    );
}

void run_k4_chunk() {
    long iterations = CHUNK_ITERATIONS;
    asm volatile(
        "1: \n"
        " add x0, x0, x1 \n"
        " add x2, x2, x3 \n"
        " add x4, x4, x5 \n"
        " add x6, x6, x7 \n"
        " subs %0, %0, #1 \n"
        " bne 1b \n"
        : "+r"(iterations) :: "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7", "cc"
    );
}

// --- Duration-based kernel runners ---

void run_kernel_timed(int kernel_id, int duration_s) {
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);

    while (running) {
        switch (kernel_id) {
            case 1: run_k1_chunk(); break;
            case 2: run_k2_chunk(); break;
            case 3: run_k3_chunk(); break;
            case 4: run_k4_chunk(); break;
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        if ((now.tv_sec - start.tv_sec) >= duration_s) break;
    }
}

// --- K5: all-core worker thread (runs K1 FMLA chunks until running=0) ---

void* k5_worker(void* arg) {
    while (running)
        run_k1_chunk();
    return NULL;
}

void run_k5_allcore(int duration_s) {
    pthread_t workers[NUM_CORES];
    for (int i = 0; i < NUM_CORES; i++)
        pthread_create(&workers[i], NULL, k5_worker, NULL);

    sleep(duration_s);
    running = 0;

    for (int i = 0; i < NUM_CORES; i++)
        pthread_join(workers[i], NULL);
}

int main(int argc, char **argv) {
    if (argc < 3 || argc > 4) {
        printf("Usage: %s <kernel_id 1-5> <duration_seconds> [freq_label]\n", argv[0]);
        return 1;
    }
    int kernel_id  = atoi(argv[1]);
    int duration_s = atoi(argv[2]);

    char filename[64];
    if (argc == 4)
        snprintf(filename, sizeof(filename), "logs/firestarter_k%d_%s.csv", kernel_id, argv[3]);
    else
        snprintf(filename, sizeof(filename), "logs/firestarter_k%d.csv", kernel_id);
    csv_file = fopen(filename, "w");
    if (!csv_file) {
        fprintf(stderr, "Error: could not open %s for writing. Does the logs/ directory exist?\n", filename);
        exit(EXIT_FAILURE);
    }
    fprintf(csv_file, "Time_ms,INST_RET,ASE_SPEC,VFP_SPEC,STALL_BACKEND,MISD,ASE_Ratio,VFP_Ratio,Stall_Ratio,Temp_C,Kernel_Freq_MHz,Actual_Freq_MHz,Throttle_Bits\n");

    // K1-K4: track calling process on any CPU (pid=0, cpu=-1)
    // K5:    track all processes per-core (pid=-1, cpu=N) to capture all 4 worker threads
    if (kernel_id >= 1 && kernel_id <= 4) {
        num_cores = 1;
        fd_inst[0]   = setup_pmu_counter(EV_INST_RETIRED,  0, -1);
        fd_ase[0]    = setup_pmu_counter(EV_ASE_SPEC,      0, -1);
        fd_vfp[0]    = setup_pmu_counter(EV_VFP_SPEC,      0, -1);
        fd_stall[0]  = setup_pmu_counter(EV_STALL_BACKEND, 0, -1);
        fd_cycles[0] = setup_cycle_counter(0, -1);
    } else if (kernel_id == 5) {
        num_cores = NUM_CORES;
        for (int i = 0; i < NUM_CORES; i++) {
            fd_inst[i]   = setup_pmu_counter(EV_INST_RETIRED,  -1, i);
            fd_ase[i]    = setup_pmu_counter(EV_ASE_SPEC,      -1, i);
            fd_vfp[i]    = setup_pmu_counter(EV_VFP_SPEC,      -1, i);
            fd_stall[i]  = setup_pmu_counter(EV_STALL_BACKEND, -1, i);
            fd_cycles[i] = setup_cycle_counter(-1, i);
        }
    } else {
        printf("Invalid kernel ID.\n");
        return 1;
    }

    pthread_t telem_thread;
    pthread_create(&telem_thread, NULL, telemetry_thread, NULL);

    printf("Executing Firestarter Kernel K%d for %d seconds...\n", kernel_id, duration_s);

    if (kernel_id >= 1 && kernel_id <= 4) {
        run_kernel_timed(kernel_id, duration_s);
        running = 0;
    } else {
        run_k5_allcore(duration_s);  // sets running=0 internally
    }

    pthread_join(telem_thread, NULL);
    fclose(csv_file);
    printf("Done. Data saved to %s\n", filename);

    return 0;
}
