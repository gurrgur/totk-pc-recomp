// Linux write watch without userfaultfd (runtime/platform/linux.c): page tracking, races, failures and file reads
// into watched pages. Synthetic memory only, no game data.
#define _GNU_SOURCE
#include <unistd.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
static ssize_t checked_pread(int fd, void *buf, size_t n, off_t off);
static int checked_memalign(void **p, size_t align, size_t n);
static int checked_fclose(FILE *f);
#define pread checked_pread
#define posix_memalign checked_memalign
#define fclose checked_fclose
#include "../runtime/platform/linux.c"
#undef pread
#undef posix_memalign
#undef fclose

static const uint64_t base = 0x6200000000ull;
static void *original;
static int io_mode, io_calls, fail_allocation;
static int mixed_check, native_checked, protector_attempted, protector_done;
static int checked_memalign(void **p, size_t align, size_t n) {
    return fail_allocation ? ENOMEM : posix_memalign(p, align, n);
}
static int checked_fclose(FILE *f) {
    int rc = fclose(f);
    if (mixed_check) {
        assert(__atomic_load_n(&vm_gate, __ATOMIC_RELAXED));
        __atomic_store_n(&native_checked, 1, __ATOMIC_RELEASE);
        while (!__atomic_load_n(&protector_attempted, __ATOMIC_ACQUIRE)) plat_pause();
        assert(!__atomic_load_n(&protector_done, __ATOMIC_ACQUIRE));
    }
    return rc;
}
static ssize_t checked_pread(int fd, void *buf, size_t n, off_t off) {
    io_calls++;
    if (original && n) {
        assert(buf != original);
        assert(((uintptr_t)buf & 4095) == ((uintptr_t)original & 4095));
    }
    if (io_mode == 1) { io_mode = 0; errno = EINTR; return -1; }
    if (io_mode == 2 && n > 37) n = 37;
    if (io_mode == 3) { errno = EIO; return -1; }
    if (io_mode == 4) { errno = EFAULT; return -1; }
    return pread(fd, buf, n, off);
}

static int frozen, thaws;
static int thaw_test(uint64_t a) {
    if (!frozen || a < base || a >= base + 2 * PG) return 0;
    frozen = 0;
    thaws++;
    return plat_mem_protect(base, 2 * PG, PLAT_PROT_RW);
}

static uint64_t query(uint64_t a, uint64_t n, int reset) {
    uint64_t pages[32];
    return plat_mem_written(a, n, pages, 32, reset);
}

static void pages_check(void) {
    uint64_t p[8], end;
    assert(plat_mem_watch_faults());
    assert(!plat_mem_reserve(UINT64_MAX - PG + 1, PG, 1));
    assert(!plat_mem_reserve(base + 1, PG, 1));
    assert(plat_mem_reserve(base, 8 * PG, 1));
    assert(plat_mem_commit(base, 8 * PG));
    assert(!query(base, 8 * PG, 0));
    assert(plat_mem_state(base, base + 8 * PG, &end) == PLAT_PROT_RW && end == base + 8 * PG);
    volatile uint8_t *b = (void *)(uintptr_t)base;
    errno = EDOM;
    b[2 * PG + 4] = 3;
    assert(errno == EDOM);
    b[7] = 1; b[7 * PG] = 7;
    assert(plat_mem_written(base, 8 * PG, p, 1, 1) == 1 && p[0] == base);
    assert(plat_mem_written(base, 8 * PG, p, 8, 0) == 2 && p[0] == base + 2 * PG && p[1] == base + 7 * PG);
    assert(query(base, 8 * PG, 1) == 2 && !query(base, 8 * PG, 0));
    assert(plat_mem_written(base + 1, PG, p, 8, 1) == UINT64_MAX);
    assert(plat_mem_written(base, UINT64_MAX, p, 8, 1) == UINT64_MAX);
    assert(plat_mem_reserve(base + 16 * PG, PG, 0));
    assert(query(base + 16 * PG, PG, 0) == UINT64_MAX);

    plat_mem_fault_hook(thaw_test);
    frozen = 1;
    assert(plat_mem_protect(base, 2 * PG, PLAT_PROT_READ));
    assert(plat_mem_state(base, base + 2 * PG, &end) == PLAT_PROT_READ);
    b[0] = 9;
    assert(thaws == 1 && !frozen && query(base, 2 * PG, 1) == 1);
    assert(plat_mem_decommit(base, 2 * PG));
    assert(!watch_fault(base) && !query(base, 2 * PG, 1));
    assert(plat_mem_state(base, base + 2 * PG, &end) == PLAT_MEM_RESERVED);
    assert(plat_mem_commit(base, 2 * PG) && b[0] == 0 && !query(base, 2 * PG, 0));
    b[0] = 1;
    assert(query(base, PG, 1) == 1);
    for (int prot = PLAT_PROT_NONE; prot <= PLAT_PROT_RX; prot++) {
        if (prot == PLAT_PROT_RW) continue;
        assert(plat_mem_protect(base, PG, prot));
        assert(!watch_fault(base));
    }
    assert(plat_mem_protect(base, PG, PLAT_PROT_RW));
}

static int request, completed;
static void racing_writer(void *arg) {
    (void)arg;
    for (int seq = 1; seq <= 2000; seq++) {
        while (__atomic_load_n(&request, __ATOMIC_ACQUIRE) != seq) plat_pause();
        __atomic_store_n((uint32_t *)(uintptr_t)base, (uint32_t)seq, __ATOMIC_RELEASE);
        __atomic_store_n(&completed, seq, __ATOMIC_RELEASE);
    }
}

static int delayed_started, delayed_result;
static void delayed_fault(void *arg) {
    (void)arg;
    __atomic_store_n(&delayed_started, 1, __ATOMIC_RELEASE);
    delayed_result = watch_fault(base);
}

static void races_check(void) {
    PlatThread *t = plat_thread_new(racing_writer, NULL, "watch-writer");
    assert(t);
    for (int seq = 1; seq <= 2000; seq++) {
        assert(!query(base, PG, 1));
        __atomic_store_n(&request, seq, __ATOMIC_RELEASE);
        uint64_t first = query(base, PG, 1);
        while (__atomic_load_n(&completed, __ATOMIC_ACQUIRE) != seq) plat_pause();
        uint64_t second = query(base, PG, 1);
        assert(first <= 1 && second <= 1 && first + second >= 1);
        assert(*(uint32_t *)(uintptr_t)base == (uint32_t)seq);
    }
    plat_thread_join(t);
    // A fault queued behind the gate must inspect the new logical state, never restore stale RW permissions.
    for (int prot = PLAT_PROT_NONE; prot <= PLAT_PROT_RX; prot++) {
        if (prot == PLAT_PROT_RW) continue;
        sigset_t old;
        delayed_started = 0;
        vm_enter(&old);
        t = plat_thread_new(delayed_fault, NULL, "watch-delayed");
        assert(t);
        while (!__atomic_load_n(&delayed_started, __ATOMIC_ACQUIRE)) plat_pause();
        assert(vm_protect(base, PG, prot));
        vm_leave(&old);
        plat_thread_join(t);
        assert(!delayed_result);
    }
    sigset_t old;
    delayed_started = 0;
    vm_enter(&old);
    t = plat_thread_new(delayed_fault, NULL, "watch-decommit");
    assert(t);
    while (!__atomic_load_n(&delayed_started, __ATOMIC_ACQUIRE)) plat_pause();
    assert(!mprotect((void *)(uintptr_t)base, PG, PROT_NONE));
    ps_begin(base);
    ps_set(base, PG, PS_RESERVED);
    ps_end(base, PG);
    vm_leave(&old);
    plat_thread_join(t);
    assert(!delayed_result);
    assert(plat_mem_decommit(base, PG) && plat_mem_commit(base, PG));
}

static void denied_fault(const PlatCrash *c) {
    _exit(c->access == 1 && c->addr == base ? 0 : 9);
}
static void failures_check(void) {
    for (int mode = 0; mode < 4; mode++) {
        pid_t pid = fork(); assert(pid >= 0);
        if (!pid) {
            plat_crash_hook(denied_fault);
            if (mode == 3) {
                assert(!munmap((void *)(uintptr_t)base, PG));
                watch_fault(base); // owned but broken mapping: deterministic tracking fatal, not a retry loop
                _exit(8);
            }
            if (mode == 2) assert(plat_mem_decommit(base, PG));
            else assert(plat_mem_protect(base, PG, mode ? PLAT_PROT_RX : PLAT_PROT_READ));
            *(volatile uint8_t *)(uintptr_t)base = 1;
            _exit(8);
        }
        int status; assert(waitpid(pid, &status, 0) == pid);
        assert(WIFEXITED(status) && WEXITSTATUS(status) == (mode == 3 ? 70 : 0));
    }
}

static void files_check(void) {
    char path[] = "/tmp/linux_watch_XXXXXX";
    int fd = mkstemp(path); assert(fd >= 0);
    uint8_t data[8192];
    for (size_t i = 0; i < sizeof data; i++) data[i] = (uint8_t)(i * 17 + 3);
    assert(write(fd, data, sizeof data) == sizeof data);
    close(fd);
    PlatFile *f = plat_file_open(path); assert(f);
    uint8_t *b = (void *)(uintptr_t)base;
    memset(b, 0xa5, 3 * PG); query(base, 3 * PG, 1);
    original = b + 13; io_mode = 1; io_calls = 0;
    assert(plat_file_pread(f, b + 13, 200, 9) == 200 && io_calls == 2);
    assert(!memcmp(b + 13, data + 9, 200) && b[12] == 0xa5 && b[213] == 0xa5);
    assert(query(base, 3 * PG, 1) == 1);
    io_mode = 2;
    assert(plat_file_pread(f, b + 13, 200, 0) == 37);
    assert(!memcmp(b + 13, data, 37) && b[50] == data[46]);
    io_mode = 0; original = b;
    assert(plat_file_pread(f, b, 200, sizeof data - 19) == 19);
    assert(!memcmp(b, data + sizeof data - 19, 19));
    query(base, 3 * PG, 1);
    assert(!plat_file_pread(f, b, 200, sizeof data) && !query(base, 3 * PG, 0));
    assert(!plat_file_pread(f, b, 0, 0));
    io_mode = 3;
    assert(plat_file_pread(f, b, 200, 0) == -1 && errno == EIO);
    io_mode = 4;
    assert(plat_file_pread(f, b, 200, 0) == -2 && errno == EFAULT);
    io_mode = 0;
    fail_allocation = 1;
    assert(plat_file_pread(f, b, UINT32_MAX, 0) == -1 && errno == ENOMEM);
    fail_allocation = 0;
    assert(plat_file_pread(f, b, 200, UINT64_MAX) == -1);
    assert(plat_file_pread(f, (void *)(UINTPTR_MAX - 2), 200, 0) == -2);
    frozen = 1;
    assert(plat_mem_protect(base, 2 * PG, PLAT_PROT_READ));
    int before = thaws;
    assert(plat_file_pread(f, b, 2 * PG, 0) == 2 * PG && thaws == before + 1);
    assert(query(base, 3 * PG, 1) == 2 && !memcmp(b, data, sizeof data));
    // Whole returned span checked before any bytes are copied, including a trailing reserved or genuine READ page.
    original = b + PG - 16;
    uint8_t saved[16]; memcpy(saved, b + PG - 16, 16);
    for (int mode = 0; mode < 3; mode++) {
        if (mode == 2) assert(plat_mem_decommit(base + PG, PG));
        else assert(plat_mem_protect(base + PG, PG, mode ? PLAT_PROT_RX : PLAT_PROT_READ));
        assert(plat_file_pread(f, b + PG - 16, 32, 0) == -2 && errno == EFAULT);
        assert(!memcmp(saved, b + PG - 16, 16) && !query(base, 3 * PG, 0));
    }
    // EOF/short reads do not validate or thaw bytes beyond the actual returned span.
    original = b + PG - 16;
    before = thaws;
    assert(plat_file_pread(f, original, 32, sizeof data) == 0 && thaws == before);
    assert(plat_file_pread(f, original, 32, sizeof data - 8) == 8 && thaws == before);
    assert(!memcmp(b + PG - 16, data + sizeof data - 8, 8));
    assert(plat_mem_commit(base + PG, PG));
    original = NULL;
    assert(plat_file_pread(f, (void *)1, 10, 0) == -2);
    plat_file_close(f);
    f = plat_file_open_unbuffered(path);
    if (f) {
        original = b;
        assert(plat_file_pread(f, b, PG, 0) == PG && !memcmp(b, data, PG));
        uint8_t *native = plat_aligned_alloc(2 * PG, PG); assert(native);
        // Some filesystems ignore O_DIRECT alignment; match their native result rather than imposing a new rule.
        const uint32_t sizes[] = {PG, PG - 1, PG};
        const uint64_t offsets[] = {0, 0, 1};
        for (int i = 0; i < 3; i++) {
            int delta = i == 0;
            ssize_t want = pread(fd_of(f), native + delta, sizes[i], offsets[i]);
            int err = errno;
            original = b + delta;
            assert(plat_file_pread(f, b + delta, sizes[i], offsets[i]) == want);
            if (want < 0) assert(errno == err);
        }
        plat_aligned_free(native);
        plat_file_close(f);
    }
    unlink(path);
}

static void native_protector(void *arg) {
    (void)arg;
    while (!__atomic_load_n(&native_checked, __ATOMIC_ACQUIRE)) plat_pause();
    __atomic_store_n(&protector_attempted, 1, __ATOMIC_RELEASE);
    assert(plat_mem_protect(PS_LIMIT, PG, PLAT_PROT_READ));
    __atomic_store_n(&protector_done, 1, __ATOMIC_RELEASE);
}

static void mixed_check_run(void) {
    assert(plat_mem_reserve(PS_LIMIT - PG, PG, 1));
    assert(plat_mem_commit(PS_LIMIT - PG, PG));
    void *native = mmap((void *)(uintptr_t)PS_LIMIT, PG, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    assert(native == (void *)(uintptr_t)PS_LIMIT);
    int fd = memfd_create("linux_watch_mixed", MFD_CLOEXEC); assert(fd >= 0);
    uint8_t bytes[64]; memset(bytes, 0x47, sizeof bytes);
    assert(write(fd, bytes, sizeof bytes) == sizeof bytes);
    PlatFile *f = (PlatFile *)(intptr_t)(fd + 1);
    original = (void *)(uintptr_t)(PS_LIMIT - 32);
    PlatThread *t = plat_thread_new(native_protector, NULL, "watch-native"); assert(t);
    mixed_check = 1;
    assert(plat_file_pread(f, original, sizeof bytes, 0) == sizeof bytes);
    mixed_check = 0;
    plat_thread_join(t);
    assert(protector_done && !memcmp(original, bytes, sizeof bytes));
    // The other serialization order must refuse the whole span without changing its leading watched bytes.
    memset(bytes, 0x59, sizeof bytes);
    assert(pwrite(fd, bytes, sizeof bytes, 0) == sizeof bytes);
    assert(plat_file_pread(f, original, sizeof bytes, 0) == -2);
    assert(*(uint8_t *)original == 0x47);
    plat_file_close(f);
}

int main(void) {
    struct rlimit no_core = {0, 0}; setrlimit(RLIMIT_CORE, &no_core);
    // The protection-based write watch, whatever the host's userfaultfd support.
    ww_ready = 1;
    pages_check();
    races_check();
    failures_check();
    files_check();
    mixed_check_run();
    puts("Linux write-watch checks passed");
}
