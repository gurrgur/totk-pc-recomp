// Standalone Linux platform checks; no game data or runtime startup required.
#include "../runtime/platform/linux.c"
#include "../runtime/sha256.c"
#include "../runtime/simd.h"
#include <assert.h>

static PlatFiber *back, *fiber;
static int visits;
static unsigned fiber_rc;

#if defined(__aarch64__)
#define GPRS(X) X(19) X(20) X(21) X(22) X(23) X(24) X(25) X(26) X(27) X(28) X(29)
#define FPRS(X) X(8) X(9) X(10) X(11) X(12) X(13) X(14) X(15)
#define BIND_G(n) register uint64_t g##n __asm__("x" #n) = seed + n;
#define TOUCH_G(n) __asm__ volatile("" : "+r"(g##n));
#define CHECK_G(n) assert(g##n == seed + n);
#define BIND_F(n) register double f##n __asm__("d" #n) = (double)(seed + n);
#define TOUCH_F(n) __asm__ volatile("" : "+w"(f##n));
#define CHECK_F(n) assert(f##n == (double)(seed + n));
#else
#define GPRS(X) X(12) X(13) X(14) X(15)
#define FPRS(X)
#define BIND_G(n) register uint64_t g##n __asm__("r" #n) = seed + n;
#define TOUCH_G(n) __asm__ volatile("" : "+r"(g##n));
#define CHECK_G(n) assert(g##n == seed + n);
#endif

static __attribute__((noinline)) void switch_checked(PlatFiber *to) {
    volatile uint64_t seed = to == fiber ? 0x12340000u : 0x56780000u;
    GPRS(BIND_G)
    FPRS(BIND_F)
    GPRS(TOUCH_G)
    FPRS(TOUCH_F)
    unsigned fp = plat_fp_get();
#if defined(__aarch64__)
    uint64_t fpsr;
    __asm__ volatile("mrs %0, fpsr" : "=r"(fpsr));
#endif
    plat_fiber_switch(to);
    GPRS(TOUCH_G)
    FPRS(TOUCH_F)
    GPRS(CHECK_G)
    FPRS(CHECK_F)
    assert(plat_fp_get() == fp);
#if defined(__aarch64__)
    uint64_t after;
    __asm__ volatile("mrs %0, fpsr" : "=r"(after));
    assert(after == fpsr);
#endif
}

static void fiber_main(void *arg) {
    assert(arg == &visits);
    assert(!(plat_fp_get() & PLAT_FP_RC_MASK));
    plat_fp_set((plat_fp_get() & ~PLAT_FP_RC_MASK) | fiber_rc);
#if defined(__aarch64__)
    __asm__ volatile("msr fpsr, %0" :: "r"(0x10ull));
#endif
    for (;;) {
        visits++;
        switch_checked(back);
    }
}

static void migrate(void *arg) {
    (void)arg;
    back = plat_fiber_self();
    switch_checked(fiber);
    plat_fiber_unself();
}

static void check_fibers(void) {
    unsigned fp = plat_fp_get();
    fiber_rc = SIMD_RC_MASK;
    back = plat_fiber_self();
    fiber = plat_fiber_new(1 << 20, fiber_main, &visits);
    assert(fiber);
    for (int i = 1; i <= 1000; i++) {
        switch_checked(fiber);
        assert(visits == i);
    }
    PlatThread *t = plat_thread_new(migrate, NULL, "fiber-check");
    assert(t);
    plat_thread_join(t);
    assert(visits == 1001);
    plat_fiber_free(fiber);
    plat_fiber_unself();
    plat_fp_set(fp);
}

static PlatIntr *interrupt;
static int intr_ready, intr_done, intr_block, intr_blocked;
static uint32_t interrupted_tid;
typedef struct { uint64_t pc, sp; int called; } CallbackCheck;
static PlatMutex callback_lock = PLAT_MUTEX_INIT;
static void interrupt_callback(void *context, void *arg) {
    CallbackCheck *c = arg;
    plat_mutex_lock(&callback_lock);
    assert(plat_thread_id() == interrupted_tid);
    assert(context_pc(context) == c->pc && context_sp(context) == c->sp);
    plat_fp_set(plat_fp_get() ^ PLAT_FP_RC_MASK);
    __atomic_add_fetch(&c->called, 1, __ATOMIC_RELEASE);
    plat_mutex_unlock(&callback_lock);
}

static void interrupted(void *arg) {
    (void)arg;
    volatile uint64_t seed = 0x98760000u;
    GPRS(BIND_G)
    FPRS(BIND_F)
    GPRS(TOUCH_G)
    FPRS(TOUCH_F)
    unsigned fp = plat_fp_get();
    sigset_t original_mask;
    stack_t original_stack;
    assert(!pthread_sigmask(SIG_SETMASK, NULL, &original_mask));
    assert(!sigaltstack(NULL, &original_stack));
    interrupted_tid = plat_thread_id();
    interrupt = plat_intr_self();
    assert(interrupt);
    __atomic_store_n(&intr_ready, 1, __ATOMIC_RELEASE);
    while (!__atomic_load_n(&intr_done, __ATOMIC_ACQUIRE)) {
        if (__atomic_load_n(&intr_block, __ATOMIC_ACQUIRE)) {
            sigset_t mask;
            sigemptyset(&mask);
            sigaddset(&mask, intr_sig);
            assert(!pthread_sigmask(SIG_BLOCK, &mask, NULL));
            __atomic_store_n(&intr_blocked, 1, __ATOMIC_RELEASE);
            while (__atomic_load_n(&intr_block, __ATOMIC_ACQUIRE)) plat_pause();
            assert(!pthread_sigmask(SIG_SETMASK, &original_mask, NULL));
            __atomic_store_n(&intr_blocked, 0, __ATOMIC_RELEASE);
        }
        plat_pause();
    }
    GPRS(TOUCH_G)
    FPRS(TOUCH_F)
    GPRS(CHECK_G)
    FPRS(CHECK_F)
    assert(plat_fp_get() == fp);
    sigset_t after_mask;
    stack_t after_stack;
    assert(!pthread_sigmask(SIG_SETMASK, NULL, &after_mask));
    assert(!sigaltstack(NULL, &after_stack));
    for (int sig = 1; sig <= SIGRTMAX; sig++)
        assert(sigismember(&original_mask, sig) == sigismember(&after_mask, sig));
    assert(original_stack.ss_sp == after_stack.ss_sp && original_stack.ss_size == after_stack.ss_size &&
           original_stack.ss_flags == after_stack.ss_flags);
    plat_intr_free(interrupt);
}

static void check_interrupt(void) {
    PlatThread *t = plat_thread_new(interrupted, NULL, "intr-check");
    assert(t);
    while (!__atomic_load_n(&intr_ready, __ATOMIC_ACQUIRE)) plat_yield();
    int stops = 0;
    CallbackCheck cb[10] = {0};
    for (int i = 0; i < 100 && stops < 10; i++) {
        uint64_t pc, sp;
        if (!plat_intr_stop(interrupt, &pc, &sp)) continue;
        assert(pc && sp);
        if (stops % 3 == 0) {
            cb[stops].pc = pc, cb[stops].sp = sp;
            plat_mutex_lock(&callback_lock);
            assert(plat_intr_resume_callback(interrupt, interrupt_callback, &cb[stops]));
            plat_mutex_unlock(&callback_lock);
        } else {
            assert(plat_intr_resume(interrupt, stops & 1 ? pc : 0));
        }
        stops++;
        plat_sleep_ms(1);
    }
    assert(stops);
    __atomic_store_n(&intr_block, 1, __ATOMIC_RELEASE);
    while (!__atomic_load_n(&intr_blocked, __ATOMIC_ACQUIRE)) plat_yield();
    uint64_t pc = 123, sp = 456;
    assert(!plat_intr_stop(interrupt, &pc, &sp));
    assert(pc == 123 && sp == 456);
    __atomic_store_n(&intr_block, 0, __ATOMIC_RELEASE);
    while (__atomic_load_n(&intr_blocked, __ATOMIC_ACQUIRE)) plat_yield();
    __atomic_store_n(&intr_done, 1, __ATOMIC_RELEASE);
    plat_thread_join(t);
    for (int i = 0; i < stops; i += 3) assert(__atomic_load_n(&cb[i].called, __ATOMIC_ACQUIRE) == 1);
}

static void check_context(void) {
    ucontext_t u = {0};
    context_set_pc(&u, 0x12345678);
    assert(context_pc(&u) == 0x12345678);
#if defined(__aarch64__)
    u.uc_mcontext.sp = 0x98765430;
    assert(context_access(&u) == -1);
    struct _aarch64_ctx *skip = (void *)u.uc_mcontext.__reserved;
    *skip = (struct _aarch64_ctx){0x12345678, 16};
    struct esr_context *e = (void *)(u.uc_mcontext.__reserved + 16);
    e->head = (struct _aarch64_ctx){ESR_MAGIC, sizeof *e};
    e->esr = 0x24ull << 26;
    assert(context_access(&u) == 0);
    e->esr |= 1 << 6;
    assert(context_access(&u) == 1);
    e->esr = 0x20ull << 26;
    assert(context_access(&u) == 8);
    e->head.size = 0x10000;
    assert(context_access(&u) == -1);
    e->head.size = 8;
    assert(context_access(&u) == -1);
#else
    u.uc_mcontext.gregs[REG_RSP] = 0x98765430;
    assert(context_access(&u) == 0);
    u.uc_mcontext.gregs[REG_ERR] = 2;
    assert(context_access(&u) == 1);
    u.uc_mcontext.gregs[REG_ERR] = 16;
    assert(context_access(&u) == 8);
#endif
    assert(context_sp(&u) == 0x98765430);
}

static uint8_t *fault_page;
static int thawed, expected_access;
static int thaw(uint64_t addr) {
    if (addr != (uintptr_t)fault_page) return 0;
    thawed++;
    return !mprotect(fault_page, 4096, PROT_READ | PROT_WRITE);
}
static void crash_check(const PlatCrash *c) {
    _exit(c->access == expected_access && c->rip && c->addr == (uintptr_t)fault_page ? 0 : 1);
}

static void check_faults(void) {
    fault_page = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(fault_page != MAP_FAILED);
    plat_mem_fault_hook(thaw);
    assert(!mprotect(fault_page, 4096, PROT_READ));
    *(volatile uint8_t *)fault_page = 42;
    assert(thawed == 1 && *fault_page == 42);
    plat_mem_fault_hook(NULL);
    for (int mode = 0; mode < 3; mode++) {
        pid_t p = fork();
        assert(p >= 0);
        if (!p) {
            expected_access = mode == 2 ? 8 : mode;
            plat_crash_hook(crash_check);
            assert(!mprotect(fault_page, 4096, mode == 0 ? PROT_NONE : PROT_READ));
            if (mode == 0) (void)*(volatile uint8_t *)fault_page;
            if (mode == 1) *(volatile uint8_t *)fault_page = 1;
            if (mode == 2) ((void (*)(void))fault_page)();
            _exit(2);
        }
        int status;
        assert(waitpid(p, &status, 0) == p);
        assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    }
    munmap(fault_page, 4096);
}

static void digest_is(const uint8_t *digest, const char *hex) {
    char out[65];
    for (int i = 0; i < 32; i++) snprintf(out + 2 * i, 3, "%02x", digest[i]);
    assert(!strcmp(out, hex));
}

static void check_sha(void) {
    uint8_t digest[32];
    sha256("", 0, digest);
    digest_is(digest, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    Sha256 s;
    sha256_init(&s);
    sha256_update(&s, "a", 1);
    sha256_update(&s, "bc", 2);
    sha256_final(&s, digest);
    digest_is(digest, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    const char *long_msg = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    sha256(long_msg, strlen(long_msg), digest);
    digest_is(digest, "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
    uint8_t block[64];
    memset(block, 'a', sizeof block);
    sha256_init(&s);
    for (int i = 0; i < 15625; i++) blocks_c(s.h, block, 1);
    s.bytes = 1000000;
    sha256_final(&s, digest);
    digest_is(digest, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
}

static void check_fp(void) {
    unsigned saved = plat_fp_get();
    plat_fp_set(saved & ~(PLAT_FP_RC_MASK | PLAT_FP_FTZ | PLAT_FP_DAZ));
    Cpu c = {0};
    for (unsigned rm = 0; rm < 4; rm++) {
        unsigned before = plat_fp_get();
        unsigned old = simd_rm_enter((uint64_t)rm << 22);
#if defined(__aarch64__)
        assert(fegetround() == simd_fe_mode((uint64_t)rm << 22));
#else
        static const unsigned rc[] = {0, 0x4000, 0x2000, 0x6000};
        assert((plat_fp_get() & SIMD_RC_MASK) == rc[rm]);
#endif
        simd_rm_leave(old);
        assert(plat_fp_get() == before);
        c.fpcr = (uint64_t)rm << 22;
        assert(fb_s(op_s(&c, FOP_ADD, 1.0f, 0x1p-24f, 0)) == (rm == 1 ? 0x3f800001u : 0x3f800000u));
    }
    c.fpcr = 0;
    assert(op_s(&c, FOP_MADD, -1.0f, 1.0f + 0x1p-23f, 1.0f - 0x1p-23f) == -0x1p-46f);
    unsigned before = plat_fp_get();
    plat_fp_set(before | PLAT_FP_FTZ | PLAT_FP_DAZ);
    assert((plat_fp_get() & (PLAT_FP_FTZ | PLAT_FP_DAZ)) == (PLAT_FP_FTZ | PLAT_FP_DAZ));
    vf32 v = {fv_s(1), fv_s(0x80000001u), 1.0f, -1.0f};
    vu32 flushed = (vu32)vflush_s(v);
    assert(flushed[0] == 0 && flushed[1] == 0x80000000u && flushed[2] == 0x3f800000u);
    plat_fp_set(before);
    vf32 r = vop_s(&c, FOP_ADD, (vf32){1, 2, 3, 4}, (vf32){2, 3, 4, 5}, (vf32){});
    assert(r[0] == 3 && r[3] == 9);
    plat_fp_set(saved);
}

static void check_icache(void) {
    uint64_t addr = 0x6100000000ull, end;
    assert(plat_mem_reserve(addr, 4096, 0));
    assert(plat_mem_commit(addr, 4096));
    uint8_t *p = (uint8_t *)(uintptr_t)addr;
    assert(!plat_mem_protect(addr, 4096, -1));
    assert(!plat_mem_protect(addr, 4096, PLAT_PROT_RX + 1));
#if defined(__aarch64__)
    uint32_t code[] = {0x52800540, 0xd65f03c0};   // mov w0, #42; ret
#else
    uint8_t code[] = {0xb8, 42, 0, 0, 0, 0xc3};
#endif
    memcpy(p, code, sizeof code);
    plat_icache_flush(p, sizeof code);
    assert(plat_mem_protect(addr, 4096, PLAT_PROT_RX));
    assert(plat_mem_state(addr, addr + 4096, &end) == PLAT_PROT_RX && end == addr + 4096);
    assert(((int (*)(void))p)() == 42);
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        struct rlimit no_core = {0, 0};
        setrlimit(RLIMIT_CORE, &no_core);
        signal(SIGSEGV, SIG_DFL);
        signal(SIGBUS, SIG_DFL);
        *(volatile uint8_t *)p = 0;
        _exit(1);
    }
    int status;
    assert(waitpid(child, &status, 0) == child);
    assert(WIFSIGNALED(status) && (WTERMSIG(status) == SIGSEGV || WTERMSIG(status) == SIGBUS));
    assert(plat_mem_protect(addr, 4096, PLAT_PROT_RW));
#if defined(__aarch64__)
    code[0] = 0x52800560;   // mov w0, #43
#else
    code[1] = 43;
#endif
    memcpy(p, code, sizeof code);
    plat_icache_flush(p, sizeof code);
    assert(plat_mem_protect(addr, 4096, PLAT_PROT_RX));
    assert(((int (*)(void))p)() == 43);
    assert(plat_mem_decommit(addr, 4096));
    assert(plat_mem_state(addr, addr + 4096, &end) == PLAT_MEM_RESERVED);
    munmap(p, 4096);
}

int main(void) {
    check_context();
    check_fp();
    check_sha();
    check_fibers();
    check_interrupt();
    check_faults();
    check_icache();
    puts("Linux platform/FP/fiber/SHA checks passed");
    return 0;
}
