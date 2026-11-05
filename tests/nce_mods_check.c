#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include "../runtime/mods.c"

static const char *fixture_root;
static unsigned served;
const char *cfg_get(const char *name) {
    if (!strcmp(name, "TOTK_MODS_DIR")) return fixture_root;
    if (!strcmp(name, "TOTK_MODS_DISABLED")) return "DisabledRomfs";
    return NULL;
}
const RtConfig *rt_config(void) { static RtConfig config = {.exefs = "unused"}; return &config; }
void rt_log(const char *fmt, ...) { (void)fmt; }
_Noreturn void rt_fatal(const char *fmt, ...) { (void)fmt; abort(); }

static void mod_walk(int k, int priority, const char *path, const char *relative) {
    (void)priority;
    assert(!*relative);
    const ModInfo *m = mods_at(k);
    assert(m && m->enabled && m->problem == MOD_OK);
    char asset[4096];
    snprintf(asset, sizeof asset, "%s/asset.bin", path);
    struct stat st;
    assert(!stat(asset, &st) && S_ISREG(st.st_mode));
    served++;
}
#include "romfs_gate.inc"

int main(int argc, char **argv) {
    assert(argc == 2);
    fixture_root = argv[1];
    mods_scan();
    assert(mods_count() == 11);
    unsigned expected_served = 0, replacements = 0;
    for (int i = 0; i < mods_count(); i++) {
        const ModInfo *m = mods_at(i);
        int dependency = !strcmp(m->name, "SdkMix") || !strcmp(m->name, "RtldMix") || !strcmp(m->name, "CapsSdkMix");
        int existing = !strcmp(m->name, "ExistingPlugin") || !strcmp(m->name, "MainMix");
        if (dependency || !strcmp(m->name, "ExeOnlySdk")) {
#if RT_NCE
            assert((m->kind & MOD_SUBSDK) && m->problem == MOD_PLUGIN);
#else
            assert(!(m->kind & MOD_SUBSDK));
            assert(m->problem == (dependency ? MOD_OK : MOD_EMPTY));
#endif
            replacements++;
        } else if (existing) {
            assert((m->kind & MOD_SUBSDK) && m->problem == MOD_PLUGIN);
        } else {
            assert(!(m->kind & MOD_SUBSDK) && m->problem == MOD_OK);
        }
        expected_served += m->enabled && (m->kind & MOD_ROMFS) && m->problem == MOD_OK;
    }
    assert(replacements == 4);
    mods_overlay_init();
    assert(served == expected_served && served == (RT_NCE ? 4u : 7u));
    puts(RT_NCE ? "Native whole-mod dependency rejection passed" : "Legacy mod classification preserved");
    return 0;
}
