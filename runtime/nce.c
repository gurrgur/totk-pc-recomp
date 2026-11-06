#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "nce.h"
#if RT_NCE
#include "host.h"
#include "platform/platform.h"
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
#include <sys/ucontext.h>
#include <asm/sigcontext.h>

#define NCE_STACK (16ul << 20)
#define HOST_BRIDGE (RT_HOSTFN_BASE + RT_HOSTFN_SIZE - 32)
_Thread_local NceFrame *rt_nce_current __attribute__((tls_model("local-exec")));
static unsigned loaded;
static int published;
static uint8_t module_loaded[16];
static uint64_t host_addresses[8192];
static unsigned host_count;
static void qualify_host(void) {
    unsigned widths = 0;
    for (unsigned i = 0; i < rt_nce_module_count; i++)
        for (uint64_t j = 0; j < rt_nce_modules[i].site_count; j++) {
            const NceSite *s = &rt_nce_modules[i].sites[j];
            if (s->operation == NCE_CAPSULE) widths |= 1u << (s->original >> 30);
        }
    for (unsigned width = 2; width <= 3; width++) if (widths & (1u << width)) {
        unsigned progress = 0;
        for (unsigned i = 0; i < 1024; i++) {
            uint64_t memory __attribute__((aligned(64))) = 7, result[2];
            uint64_t value = width == 3 ? rt_nce_host_probe64(&memory, 7, 11, result) :
                                         rt_nce_host_probe32(&memory, 7, 11, result);
            if ((value != 0 && value != 7) || result[0] != 1)
                rt_fatal("nce: unsupported host outcome for original constrained exclusive encoding");
            progress += value == 0;
        }
        if (!progress) rt_fatal("nce: unsupported host: original constrained exclusive encoding made no progress");
    }
}

_Static_assert(offsetof(NceFrame, cpu) == 0, "native leaf CPU");
_Static_assert(offsetof(Cpu, tpidr) == 280, "native leaf virtual TP");
_Static_assert(offsetof(Cpu, tpidrro) == 288, "native leaf virtual TPRO");
_Static_assert(offsetof(NceFrame, return_sp) == 16, "native host SP");
_Static_assert(offsetof(NceFrame, service_sp) == 24, "native service SP");
_Static_assert(offsetof(NceFrame, host_fpcr) == 32, "native host FPCR");
_Static_assert(offsetof(NceFrame, target) == 48, "native call target");
_Static_assert(offsetof(NceFrame, site) == 56, "native boundary site");
_Static_assert(offsetof(NceFrame, reservation) == 80, "native reservation state");
_Static_assert(offsetof(Cpu, nce_frame) == 344, "native activation chain");

static uint32_t branch(uint64_t pc, uint64_t target) {
    int64_t d = (int64_t)target - (int64_t)pc;
    if ((d & 3) || d < -(1ll << 27) || d >= (1ll << 27)) rt_fatal("nce: branch out of range");
    return 0x14000000u | ((uint64_t)(d >> 2) & 0x3ffffff);
}

void rt_nce_register_host(uint64_t address) {
    if (__atomic_load_n(&published, __ATOMIC_ACQUIRE)) rt_fatal("nce: registration after native publication");
    if (address - RT_HOSTFN_BASE >= RT_HOSTFN_SIZE) return;
    if ((address & 15) || address >= HOST_BRIDGE || host_count == 8192) rt_fatal("nce: invalid host entry");
    host_addresses[host_count++] = address;
}

void rt_nce_reserve(uint64_t reserved_end) {
    if (!rt_nce_module_count || rt_nce_module_count > 16) rt_fatal("nce: invalid module count");
    for (unsigned i = 0; i < rt_nce_module_count; i++) {
        const NceModule *m = &rt_nce_modules[i];
        if ((m->arena | m->arena_size) & 0xfff || m->arena_size > RT_KERNEL_BASE ||
            m->arena > RT_KERNEL_BASE - m->arena_size)
            rt_fatal("nce: invalid arena layout for %s", m->name);
        uint64_t end = m->arena + m->arena_size;
        for (unsigned j = 0; j < rt_nce_module_count; j++) {
            const NceModule *n = &rt_nce_modules[j];
            uint64_t image_end = n->base + n->size + (!strcmp(n->name, "main") ? 0x1000 : 0);
            if (m->arena < image_end && end > n->base) rt_fatal("nce: arena/image overlap");
            if (j != i && m->arena < n->arena + n->arena_size && end > n->arena) rt_fatal("nce: arena/arena overlap");
        }
        uint64_t reserved_start = rt_layout[0].base;
        uint64_t prefix_end = end < reserved_start ? end : reserved_start;
        if (m->arena < prefix_end && !plat_mem_reserve(m->arena, prefix_end - m->arena, 0))
            rt_fatal("nce: arena collision for %s", m->name);
        uint64_t first = m->arena < reserved_end ? reserved_end : m->arena;
        if (end > first && !plat_mem_reserve(first, end - first, 0)) rt_fatal("nce: arena collision for %s", m->name);
    }
}

// Trusted immutable original text is callable; private RX islands are not guest entry points.
int rt_nce_original_pc(uint64_t pc) {
    if (!__atomic_load_n(&published, __ATOMIC_ACQUIRE) || (pc & 3)) return 0;
    for (unsigned i = 0; i < rt_nce_module_count; i++) {
        const NceModule *m = &rt_nce_modules[i];
        if (pc - (m->base + m->text_offset) < m->text_size) return 1;
    }
    return 0;
}

int rt_nce_constrained_pc(uint64_t pc) {
    if (pc == (uintptr_t)rt_nce_host_probe64 + 28 || pc == (uintptr_t)rt_nce_host_probe32 + 28) return 1;
    if (!__atomic_load_n(&published, __ATOMIC_ACQUIRE)) return 0;
    for (unsigned i = 0; i < rt_nce_module_count; i++)
        for (uint64_t j = 0; j < rt_nce_modules[i].site_count; j++) {
            const NceSite *s = &rt_nce_modules[i].sites[j];
            if (s->operation == NCE_CAPSULE && pc == s->entry + 52) return 1;
        }
    return 0;
}

void rt_nce_load(const char *name, uint64_t base, uint64_t size) {
    unsigned i;
    for (i = 0; i < rt_nce_module_count; i++) if (!strcmp(name, rt_nce_modules[i].name)) break;
    if (i == rt_nce_module_count || module_loaded[i] || __atomic_load_n(&published, __ATOMIC_ACQUIRE))
        rt_fatal("nce: invalid module lifecycle for %s", name);
    const NceModule *m = &rt_nce_modules[i];
    int identity = 0;
    for (int j = 0; j < rt_exefs_sha256_count; j++)
        if (!strcmp(rt_exefs_sha256[j].file, name)) identity = !strcmp(rt_exefs_sha256[j].sha256, m->sha256);
    if (!identity) rt_fatal("nce: rewrite metadata differs from loader-verified NSO identity for %s", name);
    if (m->base != base || m->size != size || m->text_offset > size || m->text_size > size - m->text_offset)
        rt_fatal("nce: module identity/layout mismatch for %s", name);
    for (uint64_t k = 0; k < m->site_count; k++) {
        const NceSite *s = &m->sites[k];
        int original = s->pc - (base + m->text_offset) < m->text_size;
        int generated = s->pc - m->arena < m->arena_size;
        if ((s->pc & 3) || (!original && !generated) || (k && s->pc <= m->sites[k - 1].pc) ||
            (original && LD32(s->pc) != s->original))
            rt_fatal("nce: original word/site mismatch for %s", name);
    }
    if (m->arena_size && !plat_mem_commit(m->arena, m->arena_size)) rt_fatal("nce: cannot commit %s arena", name);
    if (m->word_count > m->arena_size / 4) rt_fatal("nce: immutable arena words exceed layout");
    if (m->word_count) memcpy((void *)(uintptr_t)m->arena, m->words, m->word_count * 4);
    uint64_t tls = rt_nce_tls_offset();
    if (tls >= (1u << 24)) rt_fatal("nce: static TLS offset out of range");
    for (uint64_t k = 0; k < m->site_count; k++) {
        const NceSite *s = &m->sites[k];
        if (LD32(s->pc) != s->original) rt_fatal("nce: generated original word mismatch");
        if (s->operation == NCE_WORD) {
            ST32(s->pc, s->argument);
            continue;
        }
        if ((s->entry & 3) || s->entry - m->arena >= m->arena_size) rt_fatal("nce: invalid gate entry");
        if (s->resume) {
            if (s->resume - m->arena >= m->arena_size || s->resume < m->arena + 40 ||
                m->arena_size - (s->resume - m->arena) < 8)
                rt_fatal("nce: invalid site epilogue");
            if (s->operation == NCE_SPIN &&
                (s->resume != s->entry + ((s->original & 0xfc000000u) == 0x14000000u ? 40 : 56) ||
                 LD32(s->resume + 4) != branch(s->resume + 4, s->argument)))
                rt_fatal("nce: stale spin metadata; regenerate with tools/prepare_nce.py");
            ST64(s->resume - 8, (uint64_t)(uintptr_t)rt_nce_boundary);
        }
        unsigned offsets[4], n = 0;
        if (s->operation == NCE_EXLOAD) { offsets[0] = 12; n = 1; }
        if (s->operation == NCE_CLREX) { offsets[0] = 16; n = 1; }
        if (s->operation == NCE_EXSTORE) { offsets[0] = 4; offsets[1] = 40; n = 2; }
        if (s->operation == NCE_CAPSULE) { offsets[0] = 12; n = 1; }
        if ((s->operation == NCE_READ_TP || s->operation == NCE_READ_TPRO) && !s->resume) {
            offsets[0] = 4; n = 1;
        }
        for (unsigned j = 0; j < n; j++) {
            uint64_t a = s->entry + offsets[j];
            ST32(a, LD32(a) | ((uint32_t)(tls >> 12) << 10));
            ST32(a + 4, LD32(a + 4) | ((uint32_t)(tls & 4095) << 10));
        }
        ST32(s->pc, branch(s->pc, s->entry));
    }
    if (m->arena_size) {
        plat_icache_flush((void *)(uintptr_t)m->arena, m->arena_size);
        if (!plat_mem_protect(m->arena, m->arena_size, PLAT_PROT_RX)) rt_fatal("nce: cannot seal arena");
    }
    plat_icache_flush((void *)(uintptr_t)(base + m->text_offset), m->text_size);
    module_loaded[i] = 1;
    loaded++;
}

int rt_nce_check_text(const char *name, uint64_t address, const void *original, size_t size) {
    unsigned i;
    for (i = 0; i < rt_nce_module_count; i++) if (!strcmp(name, rt_nce_modules[i].name)) break;
    if (i == rt_nce_module_count || !module_loaded[i]) return 0;
    const NceModule *m = &rt_nce_modules[i];
    if (address != m->base + m->text_offset || size != m->text_size || !original) return 0;
    const uint8_t *source = original;
    uint64_t cursor = 0;
    for (uint64_t k = 0; k < m->site_count; k++) {
        const NceSite *s = &m->sites[k];
        uint64_t offset = s->pc - address;
        if (offset >= size) continue;
        if (offset < cursor || size - offset < 4 || LD32((uintptr_t)source + offset) != s->original) return 0;
        if (memcmp(source + cursor, (const void *)(uintptr_t)(address + cursor), offset - cursor)) return 0;
        uint32_t expected = s->operation == NCE_WORD ? (uint32_t)s->argument : branch(s->pc, s->entry);
        if (LD32(s->pc) != expected) return 0;
        cursor = offset + 4;
    }
    return !memcmp(source + cursor, (const void *)(uintptr_t)(address + cursor), size - cursor);
}

void rt_nce_publish(void) {
    if (__atomic_load_n(&published, __ATOMIC_ACQUIRE) || loaded != rt_nce_module_count) return;
    if (!plat_mem_commit(RT_HOSTFN_BASE, RT_HOSTFN_SIZE)) rt_fatal("nce: cannot commit native host gates");
    for (uint64_t a = RT_HOSTFN_BASE; a < RT_HOSTFN_BASE + RT_HOSTFN_SIZE; a += 4) ST32(a, 0xd4200000);
    ST32(HOST_BRIDGE, 0x58000050); // ldr x16, literal at +8
    ST32(HOST_BRIDGE + 4, 0xd61f0200); // br x16
    ST64(HOST_BRIDGE + 8, (uint64_t)(uintptr_t)rt_nce_boundary);
    for (unsigned i = 0; i < host_count; i++) {
        uint64_t a = host_addresses[i];
        ST32(a, 0xd10083ff); // sub sp, sp, #32
        ST32(a + 4, 0xa90047f0); // stp x16, x17, [sp]
        ST32(a + 8, 0x10ffffd1); // adr x17, .-8
        ST32(a + 12, branch(a + 12, HOST_BRIDGE));
    }
    plat_icache_flush((void *)(uintptr_t)RT_HOSTFN_BASE, RT_HOSTFN_SIZE);
    if (!plat_mem_protect(RT_HOSTFN_BASE, RT_HOSTFN_SIZE, PLAT_PROT_RX)) rt_fatal("nce: cannot seal native host gates");
    qualify_host();
    __atomic_store_n(&published, 1, __ATOMIC_RELEASE);
}

void rt_nce_bind(Cpu *c) { rt_nce_current = c ? c->nce_frame : NULL; }

static void free_frames(NceFrame *f) {
    while (f) {
        NceFrame *p = f->previous;
        munmap(f->mapping, f->mapping_size);
        free(f);
        f = p;
    }
}

void rt_nce_cleanup(Cpu *c) {
    free_frames(c->nce_frame);
    free_frames(c->nce_spare);
    c->nce_frame = c->nce_spare = NULL;
    rt_nce_current = NULL;
}

// An activation and its guarded host stack. Host code calls native guest code thousands of times a second (dispatch.c
// for callbacks and guest function pointers); a fresh mapping per call cost an mmap, a page fault on the new stack and
// an munmap whose TLB shootdown interrupts every core running the process. Finished activations stay on the Cpu's
// spare list, as deep as its calls nested, until the thread ends (rt_nce_cleanup).
static NceFrame *frame_take(Cpu *c) {
    NceFrame *f = c->nce_spare;
    if (f) {
        c->nce_spare = f->previous;
        void *p = f->mapping;
        size_t size = f->mapping_size;
        memset(f, 0, sizeof *f);
        f->mapping = p, f->mapping_size = size;
        return f;
    }
    size_t guard = (size_t)sysconf(_SC_PAGESIZE), size = NCE_STACK + 2 * guard;
    void *p = mmap(NULL, size, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    f = calloc(1, sizeof *f);
    if (p == MAP_FAILED || !f || mprotect((char *)p + guard, NCE_STACK, PROT_READ | PROT_WRITE))
        rt_fatal("nce: cannot allocate guarded host stack");
    f->mapping = p, f->mapping_size = size;
    return f;
}

void rt_nce_call(Cpu *c) {
    if (!rt_nce_original_pc(c->pc)) rt_fatal("nce: native function entry before load or outside executable text");
    if (c->sp & 15) rt_fatal("nce: unaligned guest SP");
    NceFrame *f = frame_take(c);
    f->cpu = c;
    f->previous = c->nce_frame;
    f->service_sp = (uint64_t)(uintptr_t)f->mapping + f->mapping_size - (size_t)sysconf(_SC_PAGESIZE);
    f->target = c->pc;
    f->reservation = 2;
    f->last_operation = UINT32_MAX;
    f->last_boundary = c->pc;
    f->last_lr = c->x[30];
    c->nce_frame = f;
    rt_nce_bind(c);
    rt_nce_run(f);
    if (c->nce_frame != f) rt_fatal("nce: corrupted activation ownership");
    c->nce_frame = f->previous;
    rt_nce_bind(c);
    f->previous = c->nce_spare;
    c->nce_spare = f;
}

static const NceSite *site_at(uint64_t pc) {
    for (unsigned i = 0; i < rt_nce_module_count; i++) {
        const NceModule *m = &rt_nce_modules[i];
        uint64_t lo = 0, hi = m->site_count;
        if (!hi || pc < m->sites[0].pc || pc > m->sites[hi - 1].pc) continue;
        while (lo < hi) {
            uint64_t mid = lo + (hi - lo) / 2;
            if (m->sites[mid].pc < pc) lo = mid + 1;
            else hi = mid;
        }
        if (lo < m->site_count && m->sites[lo].pc == pc) return &m->sites[lo];
    }
    return NULL;
}

static void valid_sp(uint64_t sp) {
    uint64_t end;
    // Require the entire spill span to be owned logical RW, including a possible page crossing.
    if ((sp & 15) || sp < 32 || sp >= RT_ADDRESS_LIMIT ||
        plat_mem_state(sp - 32, sp, &end) != PLAT_PROT_RW || end != sp)
        rt_fatal("nce: invalid allocated guest stack");
}

NceResume rt_nce_service(NceFrame *f, uint64_t pc, uint64_t original_sp, uint64_t original_lr) {
    Cpu *c = f->cpu;
    uint32_t reservation = f->reservation;
    f->reservation = 2;
    valid_sp(original_sp);
    if (pc == RT_RET_HOST) return (NceResume){0, 2};
    GuestFn host = rt_dispatch_lookup(pc);
    if (host && host != rt_nce_call) {
        f->last_operation = 0;
        f->last_boundary = pc;
        f->last_lr = original_lr;
        if (original_lr != RT_RET_HOST && !rt_nce_original_pc(original_lr)) rt_fatal("nce: invalid private HLE continuation");
        if (c->thread && kern_preempt_pending(c->thread)) kern_checkpoint();
        host(c);
        valid_sp(c->sp);
        if (c->nce_frame != f) rt_fatal("nce: corrupted HLE activation");
        return (NceResume){original_lr, 0}; // IP0 is call-clobbered only at AAPCS64 function boundaries.
    }
    const NceSite *s = site_at(pc);
    if (!s) rt_fatal("nce: unknown instruction boundary");
    if (s->operation != NCE_EXSTORE) {
        f->last_operation = s->operation;
        f->last_boundary = s->operation == NCE_CALL ? s->link - 4 : pc;
        f->last_lr = original_lr;
    }
    unsigned reg = s->original & 31;
    uint64_t value = 0, resume = s->resume;
    switch (s->operation) {
    case NCE_EXSTORE: {
        const char *state = reservation == 0 ? "EMPTY" : reservation == 1 ? "VALID" : reservation == 2 ? "LOST" : "INVALID";
        const char *name = f->last_operation == 0 ? hostfn_name(f->last_boundary) :
                           f->last_operation == UINT32_MAX ? "entry" : NULL;
        rt_fatal("nce: exclusive store guard at original pc %#llx state=%s(%u) observed=%#llx "
                 "entry=%#llx frame=%p parent=%p cpu=%p thread=%p last=%#llx op=%u name=%s "
                 "last_lr=%#llx guard_lr=%#llx sp=%#llx",
                 (unsigned long long)pc, state, reservation, (unsigned long long)c->x[(s->original >> 16) & 31],
                 (unsigned long long)f->target, (void *)f, (void *)f->previous, (void *)c, c->thread,
                 (unsigned long long)f->last_boundary, f->last_operation, name ? name : "instruction",
                 (unsigned long long)f->last_lr, (unsigned long long)original_lr, (unsigned long long)original_sp);
    }
    case NCE_INTERIOR: rt_fatal("nce: illegal entry into closed atomic capsule interior");
    case NCE_CACHE:
        value = reg == 31 ? 0 : c->x[reg];
        if (value >= RT_ADDRESS_LIMIT - 64) rt_fatal("nce: cache maintenance address out of range");
        plat_icache_flush((void *)(uintptr_t)(value & ~63ull), 64);
        break;
    case NCE_ZERO:
        value = reg == 31 ? 0 : c->x[reg];
        memset((void *)(uintptr_t)(value & ~63ull), 0, 64);
        break;
    case NCE_SVC: rt_svc(c, (uint32_t)s->argument, pc); break;
    case NCE_READ_TP: value = c->tpidr; goto read_result;
    case NCE_WRITE_TP: c->tpidr = reg == 31 ? 0 : c->x[reg]; break;
    case NCE_READ_TPRO: value = c->tpidrro; goto read_result;
    case NCE_TICKS: value = rt_ticks(); goto read_result;
    case NCE_FREQUENCY: case NCE_CONSTANT: value = s->argument;
    read_result:
        if (reg != 31) c->x[reg] = value;
        break;
    case NCE_SPIN:
        rt_spin_gate(c, s->argument);
        break;
    case NCE_PATCH:
        for (uint64_t i = 0; i < s->choice_count; i++)
            if (s->choices[i].set == UINT32_MAX || rt_patch_enabled((int)s->choices[i].set)) {
                resume = s->choices[i].entry;
                break;
            }
        break;
    case NCE_CALL:
        c->x[30] = s->link;
        rt_call(c, s->argument, 0);
        break;
    case NCE_BRK: rt_brk(c, (uint32_t)s->argument, pc);
    case NCE_UDF: rt_udf(c, pc);
    default: rt_fatal("nce: unsupported instruction boundary");
    }
    valid_sp(c->sp);
    if (c->nce_frame != f) rt_fatal("nce: corrupted instruction activation");
    return (NceResume){resume, 1};
}

void rt_nce_preempt(void *context, void *argument) {
    // The kernel retains every extension record; never copy/rebuild its signal frame.
    __asm__ volatile("msr fpcr, xzr\nmsr fpsr, xzr" ::: "memory");
    ucontext_t *u = context;
    Cpu *c = argument;
    if (!c || !rt_nce_original_pc(u->uc_mcontext.pc)) _Exit(70);
    memcpy(c->x, u->uc_mcontext.regs, 31 * sizeof(uint64_t));
    c->sp = u->uc_mcontext.sp;
    c->pc = u->uc_mcontext.pc;
    c->nzcv = u->uc_mcontext.pstate & 0xf0000000;
    const unsigned char *p = u->uc_mcontext.__reserved;
    size_t remaining = sizeof u->uc_mcontext.__reserved;
    const struct fpsimd_context *fp = NULL;
    int terminated = 0;
    while (remaining >= sizeof(struct _aarch64_ctx)) {
        const struct _aarch64_ctx *h = (const void *)p;
        if (!h->magic && !h->size) { terminated = 1; break; }
        if (h->size < 16 || (h->size & 15) || h->size > remaining) _Exit(70);
        if (h->magic == FPSIMD_MAGIC) {
            if (fp || h->size != sizeof *fp) _Exit(70);
            fp = (const void *)p;
        }
        p += h->size;
        remaining -= h->size;
    }
    if (!terminated || !fp) _Exit(70);
    memcpy(c->v, fp->vregs, sizeof c->v);
    c->fpcr = fp->fpcr;
    c->fpsr = fp->fpsr;
    kern_ap_yield();
}
#endif
