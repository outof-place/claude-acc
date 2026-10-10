// What acc-cored reads from the kernel that the SDK doesn't declare: a launchd job's resource
// coalition (<sys/coalition.h> in xnu, not in the macOS SDK). Every job launchd starts gets its own
// coalition, and its counters keep the CPU time, wakeups and I/O of every process that ever ran in
// it, so one read covers a periodic job's short-lived runs and their children without root.
#ifndef CACCCORE_H
#define CACCCORE_H

#include <stdint.h>
#include <stddef.h>

// The leading fields of xnu's struct coalition_resource_usage (bsd/sys/coalition.h); the kernel
// copies min(size, its own size), so a prefix is safe. Times are mach absolute time units.
struct acc_coalition_usage {
    uint64_t tasks_started;
    uint64_t tasks_exited;
    uint64_t time_nonempty;
    uint64_t cpu_time;
    uint64_t interrupt_wakeups;
    uint64_t platform_idle_wakeups;
    uint64_t bytesread;
    uint64_t byteswritten;
    uint64_t gpu_time;
    uint64_t cpu_time_billed_to_me;
    uint64_t cpu_time_billed_to_others;
    uint64_t energy;
    uint64_t logical_writes[8];
    uint64_t energy_billed_to_me;
    uint64_t energy_billed_to_others;
    uint64_t cpu_ptime;
};

// 0 and the counters of coalition `cid`, or -1 with errno (ESRCH: no such coalition).
int acc_coalition_usage(uint64_t cid, struct acc_coalition_usage *out);

#endif
