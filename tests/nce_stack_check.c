// runtime/nce.c valid_sp: a guest stack pointer is accepted only when the 32 bytes below it are runtime-owned and
// read-write. Synthetic memory only, no game data.
#define _GNU_SOURCE
#include "host.h"
#include "platform/platform.h"
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <sys/mman.h>

static jmp_buf refusal;
static unsigned checks, probes;

_Noreturn void rt_fatal(const char *fmt, ...) {
    assert(!strcmp(fmt, "nce: invalid allocated guest stack"));
    longjmp(refusal, 1);
}

int __real_plat_mem_readable(uint64_t addr, uint64_t n);
int __wrap_plat_mem_readable(uint64_t addr, uint64_t n) {
    probes++;
    return __real_plat_mem_readable(addr, n);
}

#include "nce_stack.inc"   // runtime/nce.c valid_sp, copied by tests/nce_check.py

static void check(uint64_t sp, int accepted) {
    int rejected = setjmp(refusal);
    if (!rejected) valid_sp(sp);
    assert((!rejected) == accepted);
    checks++;
}

static void state(uint64_t page, int prot) {
    assert(prot == PLAT_MEM_RESERVED ? plat_mem_decommit(page, 4096) : plat_mem_protect(page, 4096, prot));
}

static void pages(uint64_t base, int watch) {
    static const int states[] = {PLAT_MEM_RESERVED, PLAT_PROT_NONE, PLAT_PROT_READ, PLAT_PROT_RW, PLAT_PROT_RX};
    assert(plat_mem_reserve(base, 8192, watch));
    check(base + 32, 0);
    for (unsigned i = 0; i < sizeof states / sizeof *states; i++) {
        state(base, states[i]);
        for (unsigned j = 0; j < sizeof states / sizeof *states; j++) {
            state(base + 4096, states[j]);
            int first_rw = states[i] == PLAT_PROT_RW, second_rw = states[j] == PLAT_PROT_RW;
            check(base + 32, first_rw);
            check(base + 4096, first_rw); // End is exclusive: next page must not matter.
            check(base + 4112, first_rw && second_rw); // Both halves of the 32-byte spill span.
            check(base + 4128, second_rw);
            check(base + 8192, second_rw);
        }
    }
    assert(plat_mem_commit(base, 8192));
    for (unsigned offset = 32; offset <= 8192; offset += 16) check(base + offset, 1);
    check(base + 16, 0); // First half of spill is free, second half is RW.
    check(base + 8208, 0); // First half is RW, second half is free.
    for (unsigned offset = 1; offset < 16; offset++) check(base + 4096 + offset, 0);
    assert(plat_mem_decommit(base, 8192));
    check(base + 4112, 0);
    assert(plat_mem_commit(base, 8192));
    check(base + 4112, 1); // Permission/lifetime changes must not leave a cached result.
    if (watch) {
        uint64_t dirty[2];
        assert(plat_mem_written(base, 8192, dirty, 2, 1) == 0);
        check(base + 4112, 1); // Logical RW remains valid under temporary write-watch protection.
        assert(plat_mem_written(base, 8192, dirty, 2, 0) == 0);
    }
}

int main(void) {
    check(0, 0);
    check(16, 0);
    check(32, 0);
    check(RT_ADDRESS_LIMIT, 0);
    check(RT_ADDRESS_LIMIT + 16, 0);
    check(UINT64_MAX & ~15ull, 0);
    // OS-readable but not runtime-owned memory is not a guest stack.
    uint64_t foreign = 0x5000000000ull;
    void *p = mmap((void *)(uintptr_t)foreign, 4096, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    assert(p == (void *)(uintptr_t)foreign);
    check(foreign + 32, 0); // Also before the platform's ownership table exists.
    pages(0x6000000000ull, 0);
    check(foreign + 32, 0);
    pages(0x6100000000ull, 1);
    uint64_t top = RT_ADDRESS_LIMIT - 4096;
    assert(plat_mem_reserve(top, 4096, 0) && plat_mem_commit(top, 4096));
    check(top + 32, 1);
    check(RT_ADDRESS_LIMIT - 16, 1);
    check(RT_ADDRESS_LIMIT, 0);
    assert(!probes);
    assert(!munmap(p, 4096));
    printf("Native stack checks passed: %u\n", checks);
    return 0;
}
