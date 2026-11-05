#define _GNU_SOURCE
#include "host.h"
#include "platform/platform.h"
#include "sha256.h"
#include "nce_types.inc"
#include "nce_fixture.h"
#include <assert.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

typedef struct {
    RtLoaded pub;
    uint32_t seg_off[3], seg_size[3];
} Mod;
static Mod mods[1];
static int nmods = 1, failures;
static unsigned loaded;
static int published;
static uint8_t module_loaded[16];
const RtFileHash rt_exefs_sha256[] = {{"main", FIXTURE_SHA256}};
const int rt_exefs_sha256_count = 1;
#define CHECK(condition, ...) do { if (!(condition)) failures++; } while (0)

_Noreturn void rt_fatal(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    _Exit(70);
}
void rt_sha256(const void *p, size_t n, uint8_t out[32]) { sha256(p, n, out); }
uint64_t rt_nce_tls_offset(void) { return 16; }
void rt_nce_boundary(void) {}
#include "loader_parts.inc"

static void probe(const char *directory, int valid) {
    failures = 0;
    check_segments(directory);
    assert((failures == 0) == valid);
}

static void corrupt_word(const char *directory, uint64_t address, int protection) {
    uint64_t page = address & ~0xfffull;
    assert(plat_mem_protect(page, 0x1000, PLAT_PROT_RW));
    uint32_t word = LD32(address);
    ST32(address, word ^ 1);
    assert(plat_mem_protect(page, 0x1000, protection));
    probe(directory, 0);
    assert(plat_mem_protect(page, 0x1000, PLAT_PROT_RW));
    ST32(address, word);
    assert(plat_mem_protect(page, 0x1000, protection));
    probe(directory, 1);
}

int main(int argc, char **argv) {
    assert(argc == 2);
#if RT_NCE
    assert(!rt_nce_check_text("main", FIXTURE_BASE, (const void *)1, 0x1000));
#endif
    size_t length;
    uint8_t *raw = ldr_read_verified(argv[1], "main", &length);
    Seg seg[3];
    uint64_t size = nso_segments(raw, length, "main", seg);
    assert(plat_mem_reserve(FIXTURE_BASE, size + 0x1000, 0));
    assert(plat_mem_commit(FIXTURE_BASE, size));
    for (unsigned i = 0; i < 3; i++) {
        nso_segment(raw, &seg[i], (uint8_t *)(uintptr_t)(FIXTURE_BASE + seg[i].mem_off), "main", i);
        mods[0].seg_off[i] = seg[i].mem_off;
        mods[0].seg_size[i] = seg[i].size;
    }
    mods[0].pub = (RtLoaded){.name = "main", .base = FIXTURE_BASE, .size = size,
                            .text_end = seg[0].mem_off + seg[0].size, .data_off = seg[2].mem_off};
#if RT_NCE
    const NceModule *m = &rt_nce_modules[0];
    assert(plat_mem_reserve(m->arena, m->arena_size, 0));
    rt_nce_load("main", FIXTURE_BASE, size);
    assert(!published && module_loaded[0]);
#endif
    seal(&mods[0]);
    probe(argv[1], 1);
    int text_protection = RT_NCE ? PLAT_PROT_RX : PLAT_PROT_READ;
    corrupt_word(argv[1], FIXTURE_BASE + 0xe00, text_protection);
    corrupt_word(argv[1], FIXTURE_BASE + 0x1000, PLAT_PROT_READ);
#if RT_NCE
    int branch_checked = 0, word_checked = 0;
    for (uint64_t i = 0; i < m->site_count; i++) {
        const NceSite *s = &m->sites[i];
        if (s->pc - FIXTURE_BASE >= seg[0].size) continue;
        int *checked = s->operation == NCE_WORD ? &word_checked : &branch_checked;
        if (*checked) continue;
        corrupt_word(argv[1], s->pc, PLAT_PROT_RX);
        *checked = 1;
    }
    assert(branch_checked && word_checked);
#endif
    assert(plat_mem_protect(FIXTURE_BASE, 0x1000, RT_NCE ? PLAT_PROT_READ : PLAT_PROT_RX));
    probe(argv[1], 0);
    assert(plat_mem_protect(FIXTURE_BASE, 0x1000, text_protection));
    assert(plat_mem_protect(FIXTURE_BASE + 0x1000, 0x1000, PLAT_PROT_RW));
    probe(argv[1], 0);
    assert(plat_mem_protect(FIXTURE_BASE + 0x1000, 0x1000, PLAT_PROT_READ));
    probe(argv[1], 1);
    pid_t child = fork();
    assert(child >= 0);
    if (!child) {
        char path[4096];
        snprintf(path, sizeof path, "%s/main", argv[1]);
        int fd = open(path, O_WRONLY);
        assert(fd >= 0);
        uint8_t changed = raw[length - 1] ^ 1;
        assert(pwrite(fd, &changed, 1, length - 1) == 1);
        close(fd);
        check_segments(argv[1]);
        _Exit(99);
    }
    int status;
    assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 70);
    char path[4096];
    snprintf(path, sizeof path, "%s/main", argv[1]);
    assert(plat_file_write(path, raw, length));
    probe(argv[1], 1);
    free(raw);
    puts(RT_NCE ? "Native installed-image/protection probe passed" : "Legacy original-image/read-only probe preserved");
    return 0;
}
