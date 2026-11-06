#define _GNU_SOURCE
#include "host.h"
#include "nce.h"
#include "platform/platform.h"
#include "nce_fixture.h"
#include <assert.h>
#include <setjmp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sys/ucontext.h>
#include <asm/sigcontext.h>

const RtLayoutEntry rt_layout[] = {{"main", FIXTURE_BASE, 0x3000}};
const int rt_layout_count = 1;
static char fixture_hash[] = FIXTURE_SHA256;
const RtFileHash rt_exefs_sha256[] = {{"main", fixture_hash}};
const int rt_exefs_sha256_count = 1;
static _Thread_local unsigned libc_tls = 0x12345678;
static int patch_on, calls, svcs;
static _Thread_local unsigned spin_calls, spin_mismatch_calls;
static _Thread_local int spin_reset;
static jmp_buf exit_target;
static PlatFiber *base_fiber, *guest_fiber;
static Cpu fiber_cpu;
static volatile int fiber_stage;
static uint64_t contention_word __attribute__((aligned(64)));
static volatile sig_atomic_t other_signals;
static int stop_signals;

static void other_signal(int signal, siginfo_t *info, void *context) {
    (void)signal; (void)context;
    if (info->si_code > 0) {
        static const char message[] = "unsupported host constrained instruction\n";
        write(STDERR_FILENO, message, sizeof message - 1);
        _Exit(70);
    }
    __atomic_fetch_add(&other_signals, 1, __ATOMIC_RELAXED);
}
static void send_other_signals(void *argument) {
    (void)argument;
    while (!__atomic_load_n(&stop_signals, __ATOMIC_ACQUIRE)) raise(SIGILL);
}

static void capsule_trials(Cpu *c, uint64_t *memory, unsigned width, unsigned count) {
    unsigned raw_progress = 0, rewritten_progress = 0;
    for (unsigned i = 0; i < count; i++) for (unsigned match = 0; match < 2; match++) {
        uint64_t raw_memory __attribute__((aligned(64))) = 7, result[2];
        uint64_t expected = match ? 7 : 8;
        uint64_t raw = width == 64 ? rt_nce_host_probe64(&raw_memory, expected, 11, result) :
                                     rt_nce_host_probe32(&raw_memory, expected, 11, result);
        assert(result[0] <= 1 && result[1] == (match ? 0x60000000 : 0x80000000));
        assert(raw == 7 || raw == (match ? 0 : 11));
        raw_progress += raw != 7;
        __atomic_store_n(memory, 7, __ATOMIC_SEQ_CST);
        c->x[8] = (uint64_t)(uintptr_t)memory;
        c->x[19] = expected;
        c->x[0] = 11;
        uint64_t saved18 = c->x[18] = 0x987654321;
        __uint128_t saved_q15 = c->v[15] = ((__uint128_t)0xfeed << 100) | 17;
        rt_call(c, FIXTURE_BASE + (width == 64 ? 0x200 : 0x240), 0);
        uint64_t got = __atomic_load_n(memory, __ATOMIC_SEQ_CST);
        assert(got == 7 || got == 0 || got == 11);
        uint64_t loaded = c->x[9], difference = loaded - expected;
        uint64_t flags = (difference >> 63) << 31 | (uint64_t)(loaded == expected) << 30 |
                         (uint64_t)(loaded >= expected) << 29;
        assert(c->x[10] <= 1 && c->nzcv == flags);
        assert(c->x[18] == saved18 && c->v[15] == saved_q15);
        rewritten_progress += got != 7;
    }
    assert(raw_progress && rewritten_progress);
}

typedef struct { uint64_t *memory; uint32_t *flag; } Aba;
static void aba_writer(void *p) {
    Aba *a = p;
    while (__atomic_load_n(a->flag, __ATOMIC_ACQUIRE) != 1) plat_yield();
    __atomic_store_n(a->memory, 8, __ATOMIC_SEQ_CST);
    __atomic_store_n(a->memory, 7, __ATOMIC_SEQ_CST);
    __atomic_store_n(a->flag, 2, __ATOMIC_RELEASE);
}

static void native_increment(void *p) {
    Cpu c = {0};
    c.sp = (uint64_t)(uintptr_t)p;
    c.x[8] = FIXTURE_BASE + 0x2100;
    for (unsigned i = 0; i < 1000; i++) rt_call(&c, FIXTURE_BASE + 0x480, 0);
}

static void native_variants(Cpu *c) {
    static const unsigned bits[] = {8, 8, 16, 16, 32, 32, 64, 64, 32, 32, 64, 64};
    unsigned char *memory = (void *)(uintptr_t)(FIXTURE_BASE + 0x2000);
    __uint128_t vectors[32];
    memcpy(vectors, c->v, sizeof vectors);
    for (unsigned i = 0; i < 12; i++) {
        int success = 0;
        for (unsigned retry = 0; retry < 1000 && !success; retry++) {
            memset(memory, 0xa5, 16);
            c->x[8] = (uint64_t)(uintptr_t)memory;
            c->x[0] = 0x1122334455667788;
            c->x[1] = 0x8877665544332211;
            unsigned char expected[16];
            memset(expected, 0xa5, sizeof expected);
            unsigned bytes = bits[i] / 8;
            for (unsigned lane = 0; lane < (i >= 8 ? 2u : 1u); lane++)
                for (unsigned b = 0; b < bytes; b++) expected[lane * bytes + b] = c->x[lane] >> (8 * b);
            rt_call(c, FIXTURE_BASE + 0x500 + i * 0x20, 0);
            assert(c->x[10] <= 1 && !memcmp(vectors, c->v, sizeof vectors));
            if (!c->x[10]) { assert(!memcmp(memory, expected, 16)); success = 1; }
        }
        assert(success);
    }
    for (unsigned status = 0; status < 31; status++) {
        int success = 0;
        for (unsigned retry = 0; retry < 1000 && !success; retry++) {
            unsigned base = status == 8 ? 7 : 8;
            c->x[base] = (uint64_t)(uintptr_t)memory;
            c->x[0] = c->x[1] = 0x12345678;
            c->nzcv = 0xa0000000;
            rt_call(c, FIXTURE_BASE + 0x700 + status * 0x20, 0);
            uint64_t result = c->x[status == 30 ? 10 : status];
            assert(result <= 1 && c->nzcv == 0xa0000000 && !memcmp(vectors, c->v, sizeof vectors));
            success = result == 0;
        }
        assert(success);
    }
}

static void contend(void *p) {
    Cpu c = {0};
    c.sp = (uint64_t)(uintptr_t)p;
    capsule_trials(&c, &contention_word, 64, 1000);
}

_Noreturn void rt_fatal(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    _Exit(70);
}
_Noreturn void rt_brk(Cpu *c, uint32_t imm, uint64_t pc) { (void)c; (void)imm; (void)pc; rt_fatal("fixture BRK"); }
_Noreturn void rt_udf(Cpu *c, uint64_t pc) { (void)c; (void)pc; rt_fatal("fixture UDF"); }
uint64_t rt_ticks(void) { return 19200001; }
void rt_spin_gate(Cpu *c, uint64_t head) {
    (void)head;
    spin_calls++;
    if (c->pc == FIXTURE_BASE + 0xb48) spin_mismatch_calls++;
    if (spin_reset) {
        ST64(c->x[8], c->x[1]);
        c->nzcv = 0x40000000;
        spin_reset = 0;
    }
}
int rt_patch_enabled(int set) { return set == 0 && patch_on; }
void kern_ap_yield(void) {}
void kern_checkpoint(void) {}

static void check_host(void) {
    assert(libc_tls == 0x12345678);
    assert(rt_get_fpcr() == 0 && rt_get_fpsr() == 0);
}

void rt_svc(Cpu *c, uint32_t imm, uint64_t pc) {
    (void)pc;
    check_host();
    svcs++;
    if (imm == 0x77) {
        assert(c->x[18] == c->tpidr);
        for (unsigned i = 0; i < 32; i++) c->v[i] = ((__uint128_t)(i + 1) << 100) | (1234 + i);
        c->tpidr = 0x9988;
        c->tpidrro = 0x8877665544332211ull;
        c->fpcr = 0x1c00000;
        c->fpsr = 0x800009f;
        c->nzcv = 0xa0000000;
    } else if (imm == 0x78) {
        c->x[0] = 0xbeef;
        c->v[31] ^= 0x1234;
    } else if (imm == 0x79) {
        if (guest_fiber) {
            fiber_stage = 1;
            plat_fiber_switch(base_fiber);
            check_host();
            rt_nce_bind(c);
            assert(libc_tls == 0x12345678 && c->nce_frame == rt_nce_current);
            c->tpidr = 0x123456789abcdef0ull;
            c->tpidrro = 0xfedcba9876543210ull;
            return; // Resume native reads on the migrated thread before its next C boundary.
        }
        longjmp(exit_target, 1);
    } else if (imm == 0x7b) {
        assert(guest_fiber && fiber_stage == 1);
        assert(c->x[16] == c->tpidr && c->x[17] == c->tpidrro && c->x[18] == 19200000);
        assert(c->nce_frame == rt_nce_current && libc_tls == 0x12345678);
        fiber_stage = 2;
        plat_fiber_switch(base_fiber);
        rt_fatal("finished fixture fiber resumed");
    } else {
        assert(imm == 0x7a);
    }
}

static void leaf_values(Cpu *c) {
    uint64_t tp = c->tpidr, tpro = c->tpidrro;
    NceFrame *outer = c->nce_frame;
    assert(rt_call(c, FIXTURE_BASE + 0xc40, 0) == tp);
    assert(c->x[1] == tpro && c->x[16] == 19200000 && c->x[17] == 0x8444c004 && c->x[18] == 4);
    assert(rt_call(c, FIXTURE_BASE + 0xcc0, 0) == tp && c->x[1] == tpro);
    assert(c->nce_frame == outer && rt_nce_current == outer);
    check_host();
}

static void host(Cpu *c) {
    check_host();
    calls++;
    NceFrame *outer = c->nce_frame;
    assert(outer && outer->reservation == 2);
    c->x[8] = FIXTURE_BASE + 0x2000;
    assert(rt_call(c, FIXTURE_BASE + 0x100, 0) == 0xbeef);
    assert(c->nce_frame == outer && rt_nce_current == outer && outer->reservation == 2);
    uint64_t tp = c->tpidr, tpro = c->tpidrro;
    c->tpidr = 0xabcdef0123456789ull;
    c->tpidrro = 0x9876543210fedcbaull;
    leaf_values(c); // A new callback activation must read the current fields, not an outer snapshot.
    c->tpidr = tp;
    c->tpidrro = tpro;
    c->x[17] = 0x1717;
    c->x[18] = 0x1818;
    c->x[30] = 0xbad0;
    c->pc = 0xbad4;
}

static void sentinel(Cpu *c) { (void)c; rt_fatal("fixture sentinel dispatched as host C"); }
// runtime/nce.c pairs its direct-call stubs with these C versions and reads TOTK_NCE_DIRECT; the fixture turns direct
// calls off, so none of the C versions runs.
const char *cfg_get(const char *name) { return strcmp(name, "TOTK_NCE_DIRECT") ? NULL : "0"; }
int cfg_bool(const char *name) { (void)name; return 0; }
GuestFn hostfn_fn(uint64_t addr) { (void)addr; return NULL; }
void rt_log(const char *fmt, ...) { (void)fmt; }
#define NOT_CALLED(name) void name(Cpu *c) { (void)c; rt_fatal(#name " called with direct calls off"); }
NOT_CALLED(ov_memcpy) NOT_CALLED(nvnp_nvnBufferGetAddress) NOT_CALLED(nvnp_nvnDeviceGetTextureHandle)
NOT_CALLED(nvnp_nvnMemoryPoolGetFlags) NOT_CALLED(nvnp_nvnChannelMaskStateSetChannelMask)
NOT_CALLED(nvnp_nvnBufferBuilderSetDevice) NOT_CALLED(nvnp_nvnBufferBuilderSetDefaults)
NOT_CALLED(nvnp_nvnBufferBuilderSetStorage) NOT_CALLED(nvnp_nvnBufferInitialize) NOT_CALLED(nvnp_nvnBufferFinalize)
const char *hostfn_name(uint64_t pc) { return pc == RT_HOSTFN_BASE + 0x40 ? "fixture host" : NULL; }
GuestFn rt_dispatch_lookup(uint64_t pc) {
    if (pc == RT_HOSTFN_BASE + 0x40) return host;
    if (pc == RT_RET_HOST) return sentinel;
    return rt_nce_original_pc(pc) ? rt_nce_call : NULL;
}
uint64_t rt_call(Cpu *c, uint64_t fn, int n, ...) {
    assert(n == 0);
    uint64_t lr = c->x[30], pc = c->pc;
    c->x[30] = RT_RET_HOST;
    c->pc = fn;
    GuestFn f = rt_dispatch_lookup(fn);
    assert(f);
    f(c);
    c->x[30] = lr;
    c->pc = pc;
    return c->x[0];
}

static void expect_failure(Cpu *c, uint64_t entry) {
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        rt_call(c, entry, 0);
        _Exit(99);
    }
    int status;
    assert(waitpid(pid, &status, 0) == pid);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 70);
}

static void expect_load_failure(int identity) {
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        if (identity) fixture_hash[0] ^= 1;
        else ST32(rt_nce_modules[0].sites[0].pc, LD32(rt_nce_modules[0].sites[0].pc) ^ 1);
        rt_nce_load("main", FIXTURE_BASE, 0x3000);
        _Exit(99);
    }
    int status;
    assert(waitpid(pid, &status, 0) == pid);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 70);
}

static void check_installed_text(void) {
    uint8_t original[0x1000];
    memcpy(original, fixture_image, sizeof original);
    assert(rt_nce_check_text("main", FIXTURE_BASE, original, sizeof original));
    assert(!rt_nce_check_text("missing", FIXTURE_BASE, original, sizeof original));
    assert(!rt_nce_check_text("main", FIXTURE_BASE + 4, original, sizeof original));
    assert(!rt_nce_check_text("main", FIXTURE_BASE, original, sizeof original - 4));
    assert(!rt_nce_check_text("main", FIXTURE_BASE, NULL, sizeof original));
    uint64_t unchanged = FIXTURE_BASE + 0xe00;
    uint32_t saved = LD32(unchanged);
    ST32(unchanged, saved ^ 1);
    assert(!rt_nce_check_text("main", FIXTURE_BASE, original, sizeof original));
    ST32(unchanged, saved);
    int branch_checked = 0, word_checked = 0;
    const NceModule *m = &rt_nce_modules[0];
    for (uint64_t i = 0; i < m->site_count; i++) {
        const NceSite *s = &m->sites[i];
        if (s->pc - FIXTURE_BASE >= sizeof original) continue;
        int *checked = s->operation == NCE_WORD ? &word_checked : &branch_checked;
        if (*checked) continue;
        uint32_t installed = LD32(s->pc);
        ST32(s->pc, installed ^ 1);
        assert(!rt_nce_check_text("main", FIXTURE_BASE, original, sizeof original));
        ST32(s->pc, installed);
        original[s->pc - FIXTURE_BASE] ^= 1;
        assert(!rt_nce_check_text("main", FIXTURE_BASE, original, sizeof original));
        original[s->pc - FIXTURE_BASE] ^= 1;
        *checked = 1;
    }
    assert(branch_checked && word_checked && rt_nce_check_text("main", FIXTURE_BASE, original, sizeof original));
    plat_icache_flush((void *)(uintptr_t)FIXTURE_BASE, sizeof original);
}

static void readonly_fault(int signal) { _Exit(signal == SIGSEGV ? 101 : 102); }

static void expect_read_only(uint64_t address) {
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        signal(SIGSEGV, readonly_fault);
        signal(SIGBUS, readonly_fault);
        *(volatile uint32_t *)(uintptr_t)address = 0;
        _Exit(99);
    }
    int status;
    assert(waitpid(pid, &status, 0) == pid);
    assert(WIFEXITED(status) && (WEXITSTATUS(status) == 101 || WEXITSTATUS(status) == 102));
}

static void preempt_context(void) {
    ucontext_t context = {0};
    context.uc_mcontext.pc = FIXTURE_BASE + 0x40;
    context.uc_mcontext.sp = 0x6000010000ull;
    context.uc_mcontext.pstate = 0xa0000000;
    for (unsigned i = 0; i < 31; i++) context.uc_mcontext.regs[i] = 0x1230000 + i;
    struct fpsimd_context *fp = (void *)context.uc_mcontext.__reserved;
    fp->head.magic = FPSIMD_MAGIC;
    fp->head.size = sizeof *fp;
    fp->fpcr = 0x1c00000;
    fp->fpsr = 0x800009f;
    for (unsigned i = 0; i < 32; i++) fp->vregs[i] = ((__uint128_t)(i + 1) << 110) | i;
    struct _aarch64_ctx *unknown = (void *)((char *)fp + sizeof *fp);
    unknown->magic = 0xabcdef01;
    unknown->size = 16;
    ucontext_t unchanged;
    memcpy(&unchanged, &context, sizeof context);
    Cpu c = {0};
    rt_nce_preempt(&context, &c);
    assert(!memcmp(&unchanged, &context, sizeof context));
    assert(!memcmp(c.x, context.uc_mcontext.regs, 31 * sizeof(uint64_t)));
    assert(!memcmp(c.v, fp->vregs, sizeof c.v));
    assert(c.sp == context.uc_mcontext.sp && c.pc == context.uc_mcontext.pc && c.nzcv == 0xa0000000);
    assert(c.fpcr == fp->fpcr && c.fpsr == fp->fpsr);
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) { fp->head.size = 0; rt_nce_preempt(&context, &c); _Exit(99); }
    int status;
    assert(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 70);
}

static void fiber(void *p) {
    Cpu *c = p;
    rt_call(c, FIXTURE_BASE + 0x300, 0);
    rt_fatal("fixture fiber returned");
}
static void migrate(void *p) {
    (void)p;
    assert(libc_tls == 0x12345678);
    base_fiber = plat_fiber_self();
    rt_nce_bind(&fiber_cpu);
    plat_fiber_switch(guest_fiber);
    assert(fiber_stage == 2);
    rt_nce_cleanup(&fiber_cpu);
    rt_nce_bind(NULL);
    plat_fiber_unself();
}

int main(void) {
    Cpu fp_helper = {.fpcr = (3u << 22) | (1u << 24) | (1u << 25)};
    rt_fpcr_sync(&fp_helper);
    assert(rt_get_fpcr() == (1u << 24));
    rt_set_fpcr(0);
    rt_set_fpsr(0);
    assert(!rt_nce_original_pc(FIXTURE_BASE + 0x40));
    assert(!rt_nce_check_text("main", FIXTURE_BASE, (const void *)1, 0x1000));
    assert(rt_nce_constrained_pc((uintptr_t)rt_nce_host_probe64 + 28));
    assert(rt_nce_constrained_pc((uintptr_t)rt_nce_host_probe32 + 28));
    rt_nce_register_host(RT_HOSTFN_BASE + 0x40);
    rt_nce_register_host(RT_RET_HOST);
    rt_nce_publish();
    assert(!rt_nce_original_pc(FIXTURE_BASE + 0x40));
    assert(plat_mem_reserve(FIXTURE_BASE, 0x4000, 0));
    assert(plat_mem_commit(FIXTURE_BASE, 0x3000));
    memcpy((void *)(uintptr_t)FIXTURE_BASE, fixture_image, sizeof fixture_image);
    assert(plat_mem_reserve(RT_HOSTFN_BASE, RT_HOSTFN_SIZE, 0));
    rt_nce_reserve(FIXTURE_BASE + 0x4000);
    expect_load_failure(0);
    expect_load_failure(1);
    rt_nce_load("main", FIXTURE_BASE, 0x3000);
    check_installed_text();
    assert(plat_mem_protect(FIXTURE_BASE, 0x1000, PLAT_PROT_RX));
    assert(plat_mem_protect(FIXTURE_BASE + 0x1000, 0x1000, PLAT_PROT_READ));
    struct sigaction old_sigill, other = {.sa_sigaction = other_signal, .sa_flags = SA_SIGINFO};
    sigemptyset(&other.sa_mask);
    assert(!sigaction(SIGILL, &other, &old_sigill));
    PlatThread *signaler = plat_thread_new(send_other_signals, NULL, "unrelated SIGILL");
    assert(signaler);
    while (!__atomic_load_n(&other_signals, __ATOMIC_ACQUIRE)) plat_yield();
    rt_nce_publish();
    __atomic_store_n(&stop_signals, 1, __ATOMIC_RELEASE);
    plat_thread_join(signaler);
    assert(!sigaction(SIGILL, &old_sigill, NULL));
    assert(rt_nce_original_pc(FIXTURE_BASE + 0x40));
    assert(!rt_nce_original_pc(rt_nce_modules[0].arena));
    for (uint64_t i = 0; i < rt_nce_modules[0].site_count; i++) {
        const NceSite *s = &rt_nce_modules[0].sites[i];
        if (s->entry) assert(!rt_nce_original_pc(s->entry)); // Private leaves cannot be captured as original state.
    }
    uint64_t end;
    assert(plat_mem_state(rt_nce_modules[0].arena, rt_nce_modules[0].arena + 1, &end) == PLAT_PROT_RX);
    assert(plat_mem_state(FIXTURE_BASE, FIXTURE_BASE + 1, &end) == PLAT_PROT_RX);
    expect_read_only(FIXTURE_BASE);
    expect_read_only(FIXTURE_BASE + 0x1000);
    expect_read_only(rt_nce_modules[0].arena);
    preempt_context();
    uint64_t stack = 0x6000000000ull;
    assert(plat_mem_reserve(stack, 0x20000, 0) && plat_mem_commit(stack, 0x20000));
    Cpu c = {0};
    c.sp = stack + 0x10000;
    c.tpidr = 0x4455;
    uint64_t old_sp = c.sp;
    assert(rt_call(&c, FIXTURE_BASE + 0x40, 0) == 0x9988);
    assert(c.x[1] == 19200001 && c.x[17] == 0x1717 && c.x[18] == 0x1818);
    assert(c.sp == old_sp && c.v[8] == ((__uint128_t)9 << 100 | 1242));
    assert(c.v[31] == (((__uint128_t)32 << 100 | 1265) ^ 0x1234));
    assert(c.fpcr == 0x1c00000 && c.fpsr == 0x800009f && c.nzcv == 0xa0000000);
    assert(c.nce_frame == NULL && rt_nce_current == NULL && calls == 1 && svcs == 2);
    leaf_values(&c);
    c.x[0] = 0x1234567887654321ull;
    assert(rt_call(&c, FIXTURE_BASE + 0xc80, 0) == 0x1234567887654321ull);
    assert(c.tpidr == c.x[0] && c.x[1] == c.tpidrro);
    leaf_values(&c);
    c.x[8] = FIXTURE_BASE + 0x2000;
    c.x[0] = 11;
    ST64(c.x[8], 7);
    rt_call(&c, FIXTURE_BASE + 0xd00, 0);
    assert(c.x[10] == 0 && LD64(c.x[8]) == 11 && c.x[16] == c.tpidr && c.x[17] == c.tpidrro && c.x[18] == 4);
    for (patch_on = 0; patch_on < 2; patch_on++) assert(rt_call(&c, FIXTURE_BASE + 0x180, 0) == (patch_on ? 9 : 3));
    for (patch_on = 0; patch_on < 2; patch_on++)
        for (unsigned offset = 4; offset <= 12; offset += 4) expect_failure(&c, FIXTURE_BASE + 0x200 + offset);
    patch_on = 0;
    c.x[8] = FIXTURE_BASE + 0x2000;
    c.x[0] = 11;
    *(uint64_t *)(uintptr_t)c.x[8] = 7;
    expect_failure(&c, FIXTURE_BASE + 0x340);
    for (int i = 0; i < 1000; i++) {
        c.x[0] = 11;
        rt_call(&c, FIXTURE_BASE + 0x380, 0);
        assert(*(uint64_t *)(uintptr_t)c.x[8] == 11 || c.x[10] == 1);
    }
    native_variants(&c);
    unsigned mismatch_before = spin_mismatch_calls;
    for (unsigned i = 0; i < 1000; i++) {
        c.x[8] = FIXTURE_BASE + 0x2000;
        c.x[0] = 11;
        c.x[1] = 7;
        ST64(c.x[8], 7);
        rt_call(&c, FIXTURE_BASE + 0xb40, 0);
        assert(c.x[10] == 0 && LD64(c.x[8]) == 11);
    }
    assert(spin_mismatch_calls == mismatch_before);
    c.x[0] = 11;
    c.x[1] = 7;
    ST64(c.x[8], 8);
    spin_reset = 1;
    rt_call(&c, FIXTURE_BASE + 0xb40, 0);
    assert(!spin_reset && spin_mismatch_calls == mismatch_before + 1 && c.x[10] == 0 && LD64(c.x[8]) == 11);
    unsigned spin_before = spin_calls;
    c.x[1] = 1;
    rt_call(&c, FIXTURE_BASE + 0xb80, 0);
    assert(c.x[1] == 0 && spin_calls == spin_before + 1);
    *(uint64_t *)(uintptr_t)(FIXTURE_BASE + 0x2100) = 0;
    PlatThread *incrementer = plat_thread_new(native_increment, (void *)(uintptr_t)(stack + 0x20000), "guarded increment");
    assert(incrementer);
    native_increment((void *)(uintptr_t)(stack + 0x10000));
    plat_thread_join(incrementer);
    assert(*(uint64_t *)(uintptr_t)(FIXTURE_BASE + 0x2100) == 2000);
    Aba aba = {(uint64_t *)(uintptr_t)(FIXTURE_BASE + 0x2000), (uint32_t *)(uintptr_t)(stack + 0x1000)};
    for (unsigned i = 0; i < 32; i++) {
        __atomic_store_n(aba.memory, 7, __ATOMIC_SEQ_CST);
        __atomic_store_n(aba.flag, 0, __ATOMIC_RELEASE);
        PlatThread *writer = plat_thread_new(aba_writer, &aba, "native ABA");
        assert(writer);
        c.x[8] = (uint64_t)(uintptr_t)aba.memory;
        c.x[11] = (uint64_t)(uintptr_t)aba.flag;
        c.x[0] = 11;
        rt_call(&c, FIXTURE_BASE + 0x400, 0);
        plat_thread_join(writer);
        assert(c.x[10] == 1 && *aba.memory == 7);
    }
    capsule_trials(&c, (uint64_t *)(uintptr_t)(FIXTURE_BASE + 0x2000), 64, 1000);
    capsule_trials(&c, (uint64_t *)(uintptr_t)(FIXTURE_BASE + 0x2080), 32, 1000);
    PlatThread *contender = plat_thread_new(contend, (void *)(uintptr_t)(stack + 0x20000), "capsule contention");
    assert(contender);
    capsule_trials(&c, &contention_word, 64, 1000);
    plat_thread_join(contender);
    if (!setjmp(exit_target)) rt_call(&c, FIXTURE_BASE + 0x300, 0);
    assert(c.nce_frame && rt_nce_current);
    rt_nce_cleanup(&c);
    assert(!c.nce_frame && !rt_nce_current);
    fiber_cpu.sp = stack + 0x20000;
    base_fiber = plat_fiber_self();
    guest_fiber = plat_fiber_new(1 << 20, fiber, &fiber_cpu);
    assert(guest_fiber);
    plat_fiber_switch(guest_fiber);
    assert(fiber_stage == 1 && fiber_cpu.nce_frame);
    rt_nce_bind(NULL);
    plat_fiber_unself();
    PlatThread *thread = plat_thread_new(migrate, NULL, "native migration");
    assert(thread);
    plat_thread_join(thread);
    plat_fiber_free(guest_fiber);
    puts("Native lifecycle/TLS/callback/patch/guard/fiber/exit fixtures passed");
    return 0;
}
