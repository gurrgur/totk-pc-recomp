// AArch64 host FP control: FPCR rounding mode and flush-to-zero for inputs and results.
#pragma once
#include <stdint.h>

#define PLAT_FP_RC_MASK (3u << 22)
#define PLAT_FP_FTZ (1u << 24)
#define PLAT_FP_DAZ PLAT_FP_FTZ

static inline uint32_t plat_fp_get(void) {
    uint64_t v;
    __asm__ volatile("mrs %0, fpcr" : "=r"(v));
    return (uint32_t)v;
}
static inline void plat_fp_set(uint32_t v) {
    __asm__ volatile("msr fpcr, %0" :: "r"((uint64_t)v) : "memory");
}
