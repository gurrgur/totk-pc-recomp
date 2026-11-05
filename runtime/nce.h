#pragma once
#include "cpu.h"

#if RT_NCE
#if !defined(__linux__) || !defined(__aarch64__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error RT_NCE requires little-endian Linux AArch64
#endif

enum { NCE_SVC = 1, NCE_READ_TP, NCE_WRITE_TP, NCE_READ_TPRO, NCE_TICKS, NCE_FREQUENCY,
       NCE_CONSTANT, NCE_PATCH, NCE_CALL, NCE_SPIN, NCE_BRK, NCE_UDF, NCE_WORD, NCE_CACHE, NCE_ZERO,
       NCE_EXLOAD, NCE_EXSTORE, NCE_CLREX, NCE_CAPSULE, NCE_INTERIOR };
typedef struct { uint32_t set; uint64_t entry; } NceChoice;
typedef struct {
    uint64_t pc, resume, entry;
    uint32_t original, operation;
    uint64_t argument, link;
    const NceChoice *choices;
    uint64_t choice_count;
} NceSite;
typedef struct {
    const char *name, *sha256;
    uint64_t base, size, text_offset, text_size, arena, arena_size;
    const NceSite *sites;
    uint64_t site_count;
    const uint32_t *words;
    uint64_t word_count;
} NceModule;
extern const NceModule rt_nce_modules[];
extern const unsigned rt_nce_module_count;

typedef struct NceFrame {
    Cpu *cpu;
    struct NceFrame *previous;
    uint64_t return_sp, service_sp, host_fpcr, host_fpsr, target, site;
    void *mapping;
    size_t mapping_size;
    uint32_t reservation; // 0 consumed, 1 original load, 2 crossed host code; callbacks cannot revive parents.
    uint32_t last_operation;
    uint64_t last_boundary, last_lr;
} NceFrame;
extern _Thread_local NceFrame *rt_nce_current;

uint64_t rt_nce_host_probe64(uint64_t *memory, uint64_t expected, uint64_t value, uint64_t result[2]);
uint64_t rt_nce_host_probe32(uint64_t *memory, uint64_t expected, uint64_t value, uint64_t result[2]);
uint64_t rt_nce_tls_offset(void);
void kern_ap_yield(void);
void rt_nce_run(NceFrame *frame);
void rt_nce_boundary(void);
void rt_nce_call(Cpu *cpu);
void rt_nce_bind(Cpu *cpu);
void rt_nce_cleanup(Cpu *cpu);
void rt_nce_reserve(uint64_t reserved_end);
void rt_nce_register_host(uint64_t address);
void rt_nce_load(const char *name, uint64_t base, uint64_t size);
int rt_nce_check_text(const char *name, uint64_t address, const void *original, size_t size);
void rt_nce_publish(void);
int rt_nce_original_pc(uint64_t pc);
int rt_nce_constrained_pc(uint64_t pc);
void rt_nce_preempt(void *context, void *argument);

typedef struct { uint64_t target, mode; } NceResume;
NceResume rt_nce_service(NceFrame *frame, uint64_t site, uint64_t original_sp, uint64_t original_lr);
#endif
