#include "CAccCore.h"
#include <string.h>

// libsystem_kernel's wrapper of the coalition_info syscall (COALITION_INFO_RESOURCE_USAGE)
extern int coalition_info_resource_usage(uint64_t cid, void *cru, size_t sz);

int acc_coalition_usage(uint64_t cid, struct acc_coalition_usage *out) {
    // room for a kernel struct bigger than our prefix: it copies min(sz, its size)
    uint64_t buf[256];
    memset(buf, 0, sizeof buf);
    if (coalition_info_resource_usage(cid, buf, sizeof buf) != 0) return -1;
    memcpy(out, buf, sizeof *out);
    return 0;
}
