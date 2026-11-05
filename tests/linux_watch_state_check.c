// Linux page-state metadata (runtime/platform/linux.c plat_mem_state): concurrent queries during protection changes,
// run ends, failed publications and reentry. Synthetic memory only, no game data.
#include "../runtime/platform/linux.c"
#include <assert.h>

static const uint64_t base = 0x6300000000ull;
#define SPAN (128 * PG)
static int ready, stop, allowed_free;
static uint64_t query_count;

static void expect(uint64_t a, uint64_t limit, int state, uint64_t end) {
    uint64_t got;
    assert(plat_mem_state(a, limit, &got) == state && got == end);
}

static void reader(void *arg) {
    uint64_t a = (uintptr_t)arg, end, count = 0;
    __atomic_add_fetch(&ready, 1, __ATOMIC_RELEASE);
    while (!__atomic_load_n(&stop, __ATOMIC_ACQUIRE)) {
        int state = plat_mem_state(a, a + SPAN, &end);
        assert(end == a + SPAN);
        assert(state == PLAT_PROT_RW || state == PLAT_PROT_READ || state == PLAT_PROT_NONE ||
               state == PLAT_PROT_RX || state == PLAT_MEM_RESERVED || (allowed_free && state == PLAT_MEM_FREE));
        state = plat_mem_state(a + PG + 17, a + SPAN - 73, &end);
        assert(end == a + SPAN - 73);
        assert(state == PLAT_PROT_RW || state == PLAT_PROT_READ || state == PLAT_PROT_NONE ||
               state == PLAT_PROT_RX || state == PLAT_MEM_RESERVED || (allowed_free && state == PLAT_MEM_FREE));
        count += 2;
    }
    __atomic_add_fetch(&query_count, count, __ATOMIC_RELAXED);
}

static void start_readers(PlatThread **threads, uint64_t addr) {
    ready = stop = 0;
    for (int i = 0; i < 4; i++) {
        threads[i] = plat_thread_new(reader, (void *)(uintptr_t)addr, "state-reader");
        assert(threads[i]);
    }
    while (__atomic_load_n(&ready, __ATOMIC_ACQUIRE) != 4) plat_pause();
}

static void end_readers(PlatThread **threads) {
    __atomic_store_n(&stop, 1, __ATOMIC_RELEASE);
    for (int i = 0; i < 4; i++) plat_thread_join(threads[i]);
}

static void publication_check(void) {
    // Must run before the first reservation initializes/publishes platform metadata.
    assert(!__atomic_load_n(&pstate, __ATOMIC_ACQUIRE));
    expect(base, base + SPAN, PLAT_MEM_FREE, base + SPAN);
    PlatThread *threads[4];
    allowed_free = 1;
    start_readers(threads, base);
    assert(plat_mem_reserve(base, SPAN, 1));
    expect(base, base + SPAN, PLAT_MEM_RESERVED, base + SPAN);
    assert(plat_mem_commit(base, SPAN));
    expect(base, base + SPAN, PLAT_PROT_RW, base + SPAN);
    end_readers(threads);
    uint64_t a = base + 2 * SPAN;
    start_readers(threads, a);
    assert(plat_mem_reserve(a, SPAN, 0));
    expect(a, a + SPAN, PLAT_MEM_RESERVED, a + SPAN);
    assert(plat_mem_commit(a, SPAN));
    expect(a, a + SPAN, PLAT_PROT_RW, a + SPAN);
    end_readers(threads);
    allowed_free = 0;
}

static int epoch, acknowledged, observed;
static void handshake_reader(void *arg) {
    (void)arg;
    const int states[] = {PLAT_PROT_READ, PLAT_PROT_RW, PLAT_MEM_RESERVED, PLAT_PROT_RW};
    for (int i = 1; i <= 400; i++) {
        while (__atomic_load_n(&epoch, __ATOMIC_ACQUIRE) != i) plat_pause();
        expect(base, base + SPAN, states[(i - 1) % 4], base + SPAN);
        __atomic_store_n(&acknowledged, i, __ATOMIC_RELEASE);
    }
    observed = 1;
}

static void transitions_check(void) {
    PlatThread *threads[4];
    start_readers(threads, base);
    PlatThread *handoff = plat_thread_new(handshake_reader, NULL, "state-handoff"); assert(handoff);
    for (int i = 1; i <= 400; i++) {
        switch ((i - 1) % 4) {
        case 0: assert(plat_mem_protect(base, SPAN, PLAT_PROT_READ)); break;
        case 1: assert(plat_mem_protect(base, SPAN, PLAT_PROT_RW)); break;
        case 2: assert(plat_mem_decommit(base, SPAN)); break;
        case 3: assert(plat_mem_commit(base, SPAN)); break;
        }
        __atomic_store_n(&epoch, i, __ATOMIC_RELEASE);
        while (__atomic_load_n(&acknowledged, __ATOMIC_ACQUIRE) != i) plat_pause();
    }
    plat_thread_join(handoff);
    assert(observed && *(uint8_t *)(uintptr_t)base == 0);
    for (int i = 0; i < 100; i++) {
        assert(plat_mem_protect(base, SPAN, PLAT_PROT_RX));
        assert(plat_mem_protect(base, SPAN, PLAT_PROT_NONE));
        assert(plat_mem_protect(base, SPAN, PLAT_PROT_RW));
    }
    uint64_t pages[8];
    for (int i = 0; i < 400; i++) {
        *(volatile uint32_t *)(uintptr_t)base = (uint32_t)i;
        assert(plat_mem_written(base, SPAN, pages, 8, 1) == 1);
    }
    end_readers(threads);
}

static void mixed_check(void) {
    // One protect spans both tracking types and therefore more than one physical mprotect run.
    uint64_t a = base + 4 * SPAN;
    assert(plat_mem_reserve(a, SPAN / 2, 1));
    assert(plat_mem_reserve(a + SPAN / 2, SPAN / 2, 0));
    assert(plat_mem_commit(a, SPAN));
    PlatThread *threads[4];
    start_readers(threads, a);
    for (int i = 0; i < 300; i++) {
        assert(plat_mem_protect(a, SPAN, PLAT_PROT_READ));
        assert(plat_mem_protect(a, SPAN, PLAT_PROT_RW));
        assert(plat_mem_decommit(a, SPAN));
        assert(plat_mem_commit(a, SPAN));
    }
    end_readers(threads);
    expect(a + 3, a + SPAN, PLAT_PROT_RW, a + SPAN);
}

static void run_ends_check(void) {
    uint64_t a = base + 8 * SPAN;
    assert(plat_mem_reserve(a, 8 * PG, 1));
    assert(plat_mem_commit(a + PG, 6 * PG));
    assert(plat_mem_protect(a + PG, PG, PLAT_PROT_NONE));
    assert(plat_mem_protect(a + 2 * PG, 2 * PG, PLAT_PROT_READ));
    assert(plat_mem_protect(a + 6 * PG, PG, PLAT_PROT_RX));
    const int states[] = {PLAT_MEM_RESERVED, PLAT_PROT_NONE, PLAT_PROT_READ, PLAT_PROT_READ,
                          PLAT_PROT_RW, PLAT_PROT_RW, PLAT_PROT_RX, PLAT_MEM_RESERVED};
    const int ends[] = {1, 2, 4, 4, 6, 6, 7, 8};
    for (int i = 0; i < 8; i++) {
        uint64_t at = a + i * PG + 7, end = a + ends[i] * PG;
        expect(at, a + 8 * PG, states[i], end);
        expect(at, at + 1, states[i], at + 1);
        expect(at, end - 1, states[i], end - 1);
        expect(at, end, states[i], end);
    }
    expect(a - PG + 1, a + 8 * PG, PLAT_MEM_FREE, a);
    expect(a + 8 * PG, a + 10 * PG + 19, PLAT_MEM_FREE, a + 10 * PG + 19);
    expect(PS_LIMIT - PG, PS_LIMIT + 99, PLAT_MEM_FREE, PS_LIMIT);
    expect(PS_LIMIT, PS_LIMIT + 99, PLAT_MEM_FREE, PS_LIMIT + 99);
    expect(a + 4 * PG, a + 4 * PG, PLAT_PROT_RW, a + 4 * PG);
    expect(a + 4 * PG, a, PLAT_PROT_RW, a); // retain existing reversed-limit behavior
    *(volatile uint8_t *)(uintptr_t)(a + 4 * PG) = 1;
    expect(a + 4 * PG, a + 6 * PG, PLAT_PROT_RW, a + 6 * PG);
    uint64_t pages[8];
    assert(plat_mem_written(a, 8 * PG, pages, 8, 1) == 1);
    expect(a + 4 * PG, a + 6 * PG, PLAT_PROT_RW, a + 6 * PG);
}

static void failure_publication_check(void) {
    uint64_t a = base + 14 * SPAN;
    assert(plat_mem_reserve(a, PG, 1));
    assert(plat_mem_reserve(a + 2 * PG, PG, 0));
    assert(plat_mem_commit(a, PG));
    // First tracking run succeeds; the run containing an unmapped hole fails. Publish the real partial progress.
    assert(!plat_mem_protect(a, 3 * PG, PLAT_PROT_READ));
    expect(a, a + 3 * PG, PLAT_PROT_READ, a + PG);
    expect(a + PG, a + 3 * PG, PLAT_MEM_FREE, a + 2 * PG);
    expect(a + 2 * PG, a + 3 * PG, PLAT_MEM_RESERVED, a + 3 * PG);
    int fd = open("/dev/null", O_RDONLY | O_CLOEXEC); assert(fd >= 0);
    ww_fd = fd; // synthetic UFFD registration failure must roll the whole logical reservation back to FREE
    assert(!plat_mem_reserve(a + 4 * PG, 4 * PG, 1));
    ww_fd = -1;
    close(fd);
    expect(a + 4 * PG, a + 8 * PG, PLAT_MEM_FREE, a + 8 * PG);
}

static volatile sig_atomic_t signal_queries, signal_expected;
static void query_signal(int sig) {
    (void)sig;
    uint64_t end;
    if (plat_mem_state(base, base + SPAN, &end) != signal_expected || end != base + SPAN) _exit(91);
    signal_queries++;
}

static void reentry_check(void) {
    struct sigaction action = {.sa_handler = query_signal};
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGUSR1, &action, NULL));
    sigset_t old, one;
    vm_enter(&old);
    ps_begin(base);
    // Interrupt a metadata writer paused halfway, with a signal the test sends itself.
    ps_set(base, SPAN / 2, PS_READ);
    sigemptyset(&one); sigaddset(&one, SIGUSR1);
    assert(!sigprocmask(SIG_UNBLOCK, &one, NULL));
    signal_expected = PLAT_PROT_RW;
    assert(!raise(SIGUSR1));
    assert(signal_queries == 1);
    expect(base, base + SPAN, PLAT_PROT_RW, base + SPAN);
    ps_set(base + SPAN / 2, SPAN / 2, PS_READ);
    ps_end(base, SPAN);
    signal_expected = PLAT_PROT_READ;
    assert(!raise(SIGUSR1));
    assert(signal_queries == 2);
    expect(base, base + SPAN, PLAT_PROT_READ, base + SPAN);
    vm_leave(&old);
    assert(plat_mem_protect(base, SPAN, PLAT_PROT_RW));
}

int main(void) {
    struct rlimit no_core = {0, 0}; setrlimit(RLIMIT_CORE, &no_core);
    alarm(60); // catches a same-thread interrupted-writer query spinning forever
    ww_ready = 1; // the protection-based write watch, whatever the host's userfaultfd support
    publication_check();
    transitions_check();
    mixed_check();
    run_ends_check();
    failure_publication_check();
    reentry_check();
    assert(query_count > 0);
    printf("Linux page-state checks passed: %llu concurrent queries\n",
           (unsigned long long)query_count);
    alarm(0);
}
