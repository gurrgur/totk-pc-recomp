"""Compile actual config/SR/resolution functions; no game files or Vulkan device needed.

Run: python tests/super_resolution_config_test.py (requires Clang and zstd headers on Linux).
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
#include <assert.h>
#include <stdarg.h>
#include "cfg.c"
#include "super_resolution.c"
#include "resolution.c"
#include "ui_text.c"

const int rt_game_version = TEST_GAME_VERSION;
static unsigned char page[128];
static char exe_path[1024], last_log[1024];
static unsigned recreations, game_w, game_h;
volatile int g_fnprof;
void rt_log(const char *fmt, ...) {
    va_list args; va_start(args, fmt);
    vsnprintf(last_log, sizeof last_log, fmt, args); va_end(args);
}
uint64_t rt_call(Cpu *c, uint64_t fn, int n, ...) {
    (void)c;
    assert(RT_V143 && fn == SET_RENDER_SIZE && n == 2);
    va_list args; va_start(args, n);
    assert(va_arg(args, uint64_t) == renderer);
    assert(va_arg(args, uint64_t) == 1); va_end(args);
    float sc[2]; memcpy(sc, page + 16, sizeof sc);
    game_w = (unsigned)lround(sc[0] * R.ow / BASE_W);
    game_h = (unsigned)lround(sc[1] * R.ow / BASE_W);
    ++recreations;
    return 0;
}
void rt_fatal(const char *fmt, ...) { (void)fmt; abort(); }
void plat_mutex_lock(PlatMutex *m) { (void)m; }
void plat_mutex_unlock(PlatMutex *m) { (void)m; }
int plat_exe_path(char *out, size_t cap) { snprintf(out, cap, "%s", exe_path); return 1; }
int plat_file_exists(const char *path) { FILE *f = fopen(path, "rb"); if (!f) return 0; fclose(f); return 1; }
uint8_t *plat_file_read(const char *path, uint64_t *size) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END); *size = (uint64_t)ftell(f); rewind(f);
    uint8_t *p = calloc(1, (size_t)*size + 1);
    assert(fread(p, 1, (size_t)*size, f) == *size); fclose(f); return p;
}
void *rt_patch_page(void) { return page; }
int rt_patch_set(const char *name) { (void)name; return 0; }
int romfs_shaped_ready(unsigned w, unsigned h) { (void)w; (void)h; return 1; }
static void reset(void) {
    clear(); memset(&R, 0, sizeof R); renderer = 0; size_pending = 0;
    memset(page, 0, sizeof page); last_log[0] = 0; recreations = 0; g_fnprof = 0;
}
static void apply_size(void) {
    Cpu c = {0};
    unsigned before = recreations;
    assert(size_pending);
    res_present(&c);
    assert(!size_pending && recreations == before + 1);
    assert(game_w == R.sw && game_h == R.sh);
}
static void unchanged(const char *m, int fallback_log) {
    unsigned char saved[sizeof page]; memcpy(saved, page, sizeof page);
    unsigned w = R.sw, h = R.sh, calls = recreations;
    double share = R.dlss_k;
    int pending = size_pending;
    assert(!res_dlss_mode(m));
    assert(R.sw == w && R.sh == h && R.dlss_k == share && size_pending == pending);
    assert(!memcmp(page, saved, sizeof page));
    Cpu c = {0}; res_present(&c);
    assert(recreations == calls);
    if (fallback_log) {
        assert(strstr(last_log, "game's scaler") && strstr(last_log, "until restart"));
    }
}
static void mode(const char *m, double share) {
    cfg_set("TOTK_SUPER_RESOLUTION_MODE", m);
    assert(!strcmp(sr_mode(), m));
    assert(fabs(dlss_share(sr_mode()) - share) < 1e-9);
}
int main(int argc, char **argv) {
    assert(argc == 3); snprintf(exe_path, sizeof exe_path, "%s", argv[0]);
    if (!strcmp(argv[1], "config")) {
        assert(sr_method() == SR_OFF && !strcmp(sr_mode(), "native"));
        assert(!strcmp(cfg_meta("SUPER_RESOLUTION")->def, "off"));
        assert(!cfg_meta("SUPER_RESOLUTION")->live);
        assert(cfg_meta("SUPER_RESOLUTION_MODE")->live);
        const char *languages[] = {"en", "ru", "pt-BR", "zh-Hans"};
        for (unsigned l = 0; l < sizeof languages / sizeof *languages; l++)
            for (int id = UI_MENU_SR_FSR3; id <= UI_MENU_FXAA_DESC; id++) {
                assert(ui_text_defined(languages[l], (UiTextId)id));
                assert(strlen(ui_text_in(languages[l], (UiTextId)id)) < UI_DESC_BYTES);
            }
        FILE *f = fopen(argv[2], "w"); assert(f);
        fputs("[display]\ndlaa = 1\ndlss = balanced\n", f); fclose(f);
        assert(cfg_load(argv[2]));
        assert(sr_method() == SR_DLSS && !strcmp(sr_mode(), "balanced"));
        assert(!strcmp(cfg_value("TOTK_SUPER_RESOLUTION"), "dlss"));
        cfg_set("TOTK_DLSS", "dlaa"); assert(!strcmp(sr_mode(), "native"));
        cfg_set("TOTK_SUPER_RESOLUTION", "off"); assert(sr_method() == SR_OFF);
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "quality");
        cfg_set("TOTK_DLSS", "performance"); assert(!strcmp(sr_mode(), "quality"));
        setenv("TOTK_SUPER_RESOLUTION", "fsr3", 1); assert(sr_method() == SR_FSR3);
        unsetenv("TOTK_SUPER_RESOLUTION"); assert(sr_method() == SR_OFF);
        setenv("TOTK_SUPER_RESOLUTION_MODE", "native", 1); assert(!strcmp(sr_mode(), "native"));
        unsetenv("TOTK_SUPER_RESOLUTION_MODE"); assert(!strcmp(sr_mode(), "quality"));
        const char *keys[] = {"SUPER_RESOLUTION", "SUPER_RESOLUTION_MODE"};
        assert(cfg_reset(keys, 2) == 2); assert(sr_method() == SR_DLSS);
        assert(!strcmp(sr_mode(), "performance"));
        cfg_set("TOTK_SUPER_RESOLUTION", "bad"); assert(sr_method() == SR_OFF);
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "bad"); assert(!strcmp(sr_mode(), "native"));
    } else if (!strcmp(argv[1], "fsr3")) {
        cfg_set("TOTK_SUPER_RESOLUTION", "fsr3"); decide();
        mode("native", 1); mode("quality", 2.0/3); mode("balanced", .58);
        mode("performance", .5); mode("ultra_performance", 1.0/3);
        mode("native", 1); decide();
        assert(R.sw == 1920 && R.sh == 1080 && R.scene_set);
        assert(R.live == RT_V143); assert(R.hw >= R.sw && R.hh >= R.sh);
        R.on = 1; renderer = 1;
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "quality");
        assert(res_dlss_mode(sr_mode()) == RT_V143);
        if (RT_V143) {
            assert(R.sw == 1280 && R.sh == 720 && size_pending);
            apply_size();
            assert(fabs(res_dlss_lod_bias() - log2(2.0/3)) < 1e-6);
            cfg_set("TOTK_SUPER_RESOLUTION", "off");
            assert(sr_method() == SR_OFF);   // UI sees restart-only change; running resolution keeps its backend
            assert(res_dlss_mode("native") && R.sw == 1920 && R.sh == 1080);
            apply_size();
            assert(res_dlss_mode("off") && R.sw == 1600 && R.sh == 900);
            apply_size();
            assert(res_dlss_lod_bias() == 0);
        } else {
            assert(R.sw == 1920 && R.sh == 1080);
            unchanged("off", 1);
        }
        reset(); cfg_set("TOTK_SUPER_RESOLUTION", "fsr3");
        cfg_set("TOTK_RENDER_SCALE", "150"); decide();
        assert(R.sw == 1920 && R.sh == 1080);
        assert(R.hw == 2880 && R.hh == 1620);
        // res_init owns startup sizing; runtime hooks must not rewrite it without recreation.
        unchanged("off", 0);
        R.on = 1;
        unchanged("quality", 0);
        unchanged("off", 1);
        reset(); cfg_set("TOTK_SUPER_RESOLUTION", "fsr3");
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "quality"); decide();
        R.on = 1;
        assert(R.sw == 1280 && R.sh == 720);
        unchanged("off", 1);   // backend failure preserves the actual reduced scene, not bookkeeping restoration
    } else if (!strcmp(argv[1], "large-output")) {
        cfg_set("TOTK_OUTPUT_RES", "5120x2880");
        cfg_set("TOTK_SUPER_RESOLUTION", "fsr3"); decide();
        assert(R.sw == 5120 && R.sh == 2880);
        assert(R.hw == 5120 && R.hh == 2880);
        uint64_t native_heap = res_heap_extra_kib();
        reset(); cfg_set("TOTK_OUTPUT_RES", "5120x2880");
        cfg_set("TOTK_SUPER_RESOLUTION", "off"); decide();
        assert(R.sw == 4096 && R.sh == 2304);
        assert(R.hw == 4096 && R.hh == 2304);
        assert(res_heap_extra_kib() < native_heap);
        reset(); cfg_set("TOTK_OUTPUT_RES", "5120x2880");
        cfg_set("TOTK_SUPER_RESOLUTION", "fsr3");
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "quality"); decide();
        assert(R.sw == 3413 && R.sh == 1920);
        assert(R.hw == 5120 && R.hh == 2880);
        assert(res_heap_extra_kib() == native_heap);
        R.on = 1; renderer = 1;
        if (RT_V143) {
            assert(res_dlss_mode("native")); apply_size();
            assert(game_w == 5120 && game_h == 2880);
            assert(res_heap_extra_kib() == native_heap);
        } else unchanged("native", 0);
        reset(); cfg_set("TOTK_OUTPUT_RES", "5120x2880");
        cfg_set("TOTK_SUPER_RESOLUTION", "fsr3");
        cfg_set("TOTK_RENDER_SCALE", "150");
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "quality"); decide();
        unsigned fallback_w, fallback_h;
        scene_for(R.base_k, &fallback_w, &fallback_h);
        assert(R.hw >= 5120 && R.hh >= 2880);
        assert(R.hw >= fallback_w && R.hh >= fallback_h);
        assert(res_heap_extra_kib() >= native_heap);
    } else {
        int present = !strcmp(argv[1], "dlss-present");
        cfg_set("TOTK_SUPER_RESOLUTION", "dlss");
        cfg_set("TOTK_SUPER_RESOLUTION_MODE", "quality"); decide();
        assert(R.live == (RT_V143 && present));
        assert(R.sw == (present ? 1280 : 1600));
        assert(R.sh == (present ? 720 : 900));
    }
    return 0;
}
'''


class SuperResolutionConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        source = cls.directory / 'test.c'
        source.write_text(HARNESS)
        compiler = os.environ.get('CC') or shutil.which('clang')
        if not compiler:
            raise RuntimeError('Clang required by runtime CPU headers')
        cls.binaries = []
        for version in (121, 143):
            binary = cls.directory / str(version) / 'test'
            binary.parent.mkdir()
            subprocess.run([compiler, '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections',
                            f'-DTEST_GAME_VERSION={version}', '-I', str(ROOT / 'runtime'),
                            str(source), '-lm', '-o', str(binary)], check=True)
            cls.binaries.append(binary)

    def run_case(self, case):
        env = {k: v for k, v in os.environ.items() if not k.startswith('TOTK_')}
        env['TOTK_DEVICE_PROFILE'] = 'desktop'
        for binary in self.binaries:
            with self.subTest(game=binary.parent.name, case=case):
                subprocess.run([str(binary), case, str(self.directory / 'test.ini')], env=env, check=True)

    def test_legacy_precedence_and_live_config(self):
        self.run_case('config')

    def test_fsr_ratios_native_and_live_fallback(self):
        self.run_case('fsr3')

    def test_native_full_output_and_heap_capacity(self):
        self.run_case('large-output')

    def test_dlss_model_required(self):
        self.run_case('dlss-missing')
        for binary in self.binaries:
            (binary.parent / 'libnvidia-ngx-dlss.so.test').touch()
        try:
            self.run_case('dlss-present')
        finally:
            for binary in self.binaries:
                (binary.parent / 'libnvidia-ngx-dlss.so.test').unlink()


if __name__ == '__main__':
    unittest.main()
