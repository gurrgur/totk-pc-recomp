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
static uint64_t tls_offset = 0x123450;
uint64_t rt_nce_tls_offset(void) { return tls_offset; }
void rt_nce_boundary(void) {}
#if RT_NCE
static const NceModule *loader_modules = rt_nce_modules;
#define rt_nce_modules loader_modules
#endif
#include "loader_parts.inc"
#undef rt_nce_modules

#if RT_NCE
static void check_leaf_words(const NceModule *m) {
    unsigned tp = 0, tpro = 0, constants = 0, words = 0;
    for (uint64_t i = 0; i < m->site_count; i++) {
        const NceSite *s = &m->sites[i];
        unsigned reg = s->original & 31;
        if (s->operation == NCE_READ_TP || s->operation == NCE_READ_TPRO) {
            assert(reg != 31 && !s->resume && LD32(s->pc) == branch(s->pc, s->entry));
            assert(LD32(s->entry) == (0xd53bd040u | reg));
            assert(LD32(s->entry + 4) == (0x91400000u | ((tls_offset >> 12) << 10) | (reg << 5) | reg));
            assert(LD32(s->entry + 8) == (0x91000000u | ((tls_offset & 4095) << 10) | (reg << 5) | reg));
            uint32_t load = 0xf9400000u | (reg << 5) | reg;
            unsigned field = s->operation == NCE_READ_TP ? 280 : 288;
            assert(LD32(s->entry + 12) == load && LD32(s->entry + 16) == load);
            assert(LD32(s->entry + 20) == (load | ((field / 8) << 10)));
            assert(LD32(s->entry + 24) == branch(s->entry + 24, s->pc + 4));
            if (s->operation == NCE_READ_TP) tp++; else tpro++;
        } else if (s->operation == NCE_FREQUENCY || s->operation == NCE_CONSTANT) {
            assert(!s->resume && s->argument > 65535 && s->argument <= UINT32_MAX);
            assert(LD32(s->pc) == branch(s->pc, s->entry));
            assert(LD32(s->entry) == (0xd2800000u | ((s->argument & 65535) << 5) | reg));
            assert(LD32(s->entry + 4) == (0xf2a00000u | ((s->argument >> 16) << 5) | reg));
            assert(LD32(s->entry + 8) == branch(s->entry + 8, s->pc + 4));
            constants++;
        } else if (s->operation == NCE_WORD && (s->original & 0xfff00000u) == 0xd5300000u) {
            assert(!s->entry && !s->resume);
            uint32_t expected = reg == 31 ? 0xd503201fu : 0xd2800000u | (4 << 5) | reg;
            assert(reg == 31 || ((s->original >> 5) & 65535) == 0xd807);
            assert(s->argument == expected && LD32(s->pc) == expected);
            words++;
        }
    }
    assert(tp && tpro && constants && words);
}
#endif

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

#if RT_NCE
static void check_legacy_reads(const char *directory, const NceModule *m) {
    const NceSite *gate = NULL;
    for (uint64_t i = 0; i < m->site_count; i++)
        if (m->sites[i].operation == NCE_SVC) { gate = &m->sites[i]; break; }
    assert(gate && gate->resume == gate->entry + 40 && rt_nce_module_count == 1);
    static const unsigned operations[] = {NCE_READ_TP, NCE_READ_TPRO};
    for (unsigned test = 0; test < 2; test++) {
        unsigned op = operations[test];
        const NceSite *read = NULL;
        for (uint64_t i = 0; i < m->site_count; i++)
            if (m->sites[i].operation == op) { read = &m->sites[i]; break; }
        assert(read);
        pid_t child = fork();
        assert(child >= 0);
        if (!child) {
            // Use a full C gate from the fixture, not one of its native leaves.
            uint32_t words[16], expected[16];
            assert(gate->entry >= m->arena && (gate->entry - m->arena) / 4 + 16 <= m->word_count);
            memcpy(words, m->words + (gate->entry - m->arena) / 4, sizeof words);
            NceSite site = *read;
            site.entry = m->arena;
            site.resume = site.entry + 40;
            words[6] = (uint32_t)site.pc;
            words[7] = (uint32_t)(site.pc >> 32);
            words[11] = branch(site.entry + 44, site.pc + 4);
            memcpy(expected, words, sizeof expected);
            uint64_t boundary = (uintptr_t)rt_nce_boundary;
            memcpy((char *)expected + 32, &boundary, sizeof boundary);
            NceModule module = *m;
            module.sites = &site;
            module.site_count = 1;
            module.words = words;
            module.word_count = 16;
            loader_modules = &module;
            loaded = 0;
            module_loaded[0] = 0;
            assert(plat_mem_protect(FIXTURE_BASE, 0x1000, PLAT_PROT_RW));
            memcpy((void *)(uintptr_t)FIXTURE_BASE, fixture_image, 0x1000);
            rt_nce_load("main", FIXTURE_BASE, module.size);
            assert(module_loaded[0] && loaded == 1 && !published);
            assert(LD32(site.pc) == branch(site.pc, site.entry));
            assert(!memcmp((const void *)(uintptr_t)site.entry, expected, sizeof expected));
            // Nonzero high/low TLS immediates must never touch the old gate's words at +4/+8.
            assert(LD32(site.entry + 4) == words[1] && LD32(site.entry + 8) == words[2]);
            assert(LD64(site.resume - 8) == boundary);
            seal(&mods[0]);
            probe(directory, 1);
            corrupt_word(directory, site.pc, PLAT_PROT_RX);
            _Exit(0);
        }
        int status;
        assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    }
}
#endif

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
    pid_t invalid_tls = fork();
    assert(invalid_tls >= 0);
    if (!invalid_tls) { tls_offset = 1u << 24; rt_nce_load("main", FIXTURE_BASE, size); _Exit(99); }
    int tls_status;
    assert(waitpid(invalid_tls, &tls_status, 0) == invalid_tls && WIFEXITED(tls_status) && WEXITSTATUS(tls_status) == 70);
    rt_nce_load("main", FIXTURE_BASE, size);
    assert(!published && module_loaded[0]);
    check_leaf_words(m);
#endif
    seal(&mods[0]);
    probe(argv[1], 1);
#if RT_NCE
    check_legacy_reads(argv[1], m);
#endif
    int text_protection = RT_NCE ? PLAT_PROT_RX : PLAT_PROT_READ;
    corrupt_word(argv[1], FIXTURE_BASE + 0xe00, text_protection);
    corrupt_word(argv[1], FIXTURE_BASE + 0x1000, PLAT_PROT_READ);
#if RT_NCE
    int branch_checked = 0, word_checked = 0, tls_checked = 0, constant_checked = 0, leaf_word_checked = 0;
    for (uint64_t i = 0; i < m->site_count; i++) {
        const NceSite *s = &m->sites[i];
        if (s->pc - FIXTURE_BASE >= seg[0].size) continue;
        int *checked = s->operation == NCE_WORD ?
                           ((s->original & 0xfff00000u) == 0xd5300000u ? &leaf_word_checked : &word_checked) :
                       s->operation == NCE_READ_TP || s->operation == NCE_READ_TPRO ? &tls_checked :
                       s->operation == NCE_FREQUENCY || s->operation == NCE_CONSTANT ? &constant_checked : &branch_checked;
        if (*checked) continue;
        corrupt_word(argv[1], s->pc, PLAT_PROT_RX);
        *checked = 1;
    }
    assert(branch_checked && word_checked && tls_checked && constant_checked && leaf_word_checked);
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
