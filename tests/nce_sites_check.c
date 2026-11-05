// runtime/nce.c site_at against synthetic site tables: every key is found, its neighbours and keys outside the
// modules are not. No game data.
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "nce_sites.inc"   // runtime/nce.c site_at, copied by tests/nce_check.py

static unsigned checks;

static void check(uint64_t pc, const NceSite *expected) {
    assert(site_at(pc) == expected);
    checks++;
}

int main(void) {
    // Includes empty NULL tables, singleton endpoints, overlapping min/max envelopes and arena-style keys.
    for (unsigned m = 0; m < rt_nce_module_count; m++) {
        const NceModule *module = &rt_nce_modules[m];
        for (uint64_t i = 0; i < module->site_count; i++) {
            uint64_t pc = module->sites[i].pc;
            check(pc, &module->sites[i]);
            check(pc - 1, NULL);
            check(pc + 1, NULL);
        }
    }
    check(8, NULL);
    check(0x2008, NULL);
    check(0x8000, NULL);
    check(0x90fc + 4, NULL);
    check(0x9ffc + 4, NULL);
    check(0x20000000, NULL);
    check(UINT64_MAX, NULL);
    printf("Native site lookup checks passed: %u in %u tables\n", checks, rt_nce_module_count);
    return 0;
}
