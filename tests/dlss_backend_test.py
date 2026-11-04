"""Compile the production NGX boundary with a mock parameter vtable and entry points.

No game data, NVIDIA driver or Vulkan device needed. Requires a C compiler and Vulkan headers.
"""
from pathlib import Path
import os
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

STUBS = r'''
#include <vulkan/vulkan.h>
#include <stdarg.h>
#include <wchar.h>
static int loader_on, path_fail, dir_fail, opened, closed, load_errors;
static int core_token;
static const char *missing_symbol;
#define PLAT_PATH_MAX 4096
typedef struct {
    VkInstance instance;
    VkPhysicalDevice phys;
    VkDevice device;
    VkPhysicalDeviceProperties props;
} VkCtx;
static VkCtx ctx;
static VkCtx *vk_ctx(void) { return &ctx; }
static int method = 2;
static int sr_method(void) { return method; }
enum { SR_OFF, SR_FSR3, SR_DLSS };
static void rt_log(const char *fmt, ...) { (void)fmt; }
static int plat_ngx_core_path(char *p, size_t n) {
    snprintf(p, n, "mock-ngx-core"); return !path_fail;
}
static void *plat_lib_open(const char *p) {
    assert(!strcmp(p, "mock-ngx-core"));
    if (!loader_on) return NULL;
    opened++; return &core_token;
}
static void plat_lib_close(void *h) { assert(h == &core_token); closed++; }
static void plat_lib_error(char *p, size_t n) { load_errors++; snprintf(p, n, "mock load failure"); }
static void *plat_lib_sym(void *h, const char *p);
static int plat_exe_dir_wide(wchar_t *p, size_t n) {
    assert(n >= 6); wcscpy(p, L"/tmp/"); return !dir_fail;
}
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance i, const char *n) {
    (void)i; (void)n; return NULL;
}
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice d, const char *n) {
    (void)d; (void)n; return NULL;
}
'''

HARNESS = r'''
#include <assert.h>
#define FAIL ((int)0xBAD00001u)
typedef struct {
    const char *name;
    union { int i; unsigned u; float f; void *p; } v;
} Entry;
typedef struct { NgxParam base; Entry entries[80]; int count; } Params;
static int allocated, destroyed, created, evaluated, released, stopped;
static int alloc_fail, create_fail, eval_fail, no_handle, bad_roundtrip, caps_fail, init_fail, shutdown_fail;
static NgxParamVtbl vt;
static Entry *entry(const NgxParam *p, const char *n) {
    Params *q = (Params *)p;
    for (int i = 0; i < q->count; i++)
        if (!strcmp(q->entries[i].name, n)) return &q->entries[i];
    assert(q->count < 80);
    Entry *e = &q->entries[q->count++]; e->name = n; return e;
}
static void si(NgxParam *p, const char *n, int v) { entry(p,n)->v.i = v; }
static void su(NgxParam *p, const char *n, unsigned v) { entry(p,n)->v.u = v; }
static void sf(NgxParam *p, const char *n, float v) { entry(p,n)->v.f = v; }
static void sp(NgxParam *p, const char *n, void *v) { entry(p,n)->v.p = v; }
static NgxResult gi(const NgxParam *p, const char *n, int *v) { *v = entry(p,n)->v.i; return bad_roundtrip ? FAIL : 1; }
static NgxResult gu(const NgxParam *p, const char *n, unsigned *v) { *v = entry(p,n)->v.u; return 1; }
static NgxResult gf(const NgxParam *p, const char *n, float *v) { *v = entry(p,n)->v.f; return 1; }
static NgxResult gp(const NgxParam *p, const char *n, void **v) { *v = entry(p,n)->v.p; return 1; }
static NgxResult alloc(NgxParam **p) {
    Params *q = calloc(1,sizeof *q); assert(q); q->base.vt = &vt; *p = &q->base; allocated++;
    return alloc_fail ? FAIL : 1;
}
static NgxResult destroy(NgxParam *p) { destroyed++; free(p); return 1; }
static NgxResult caps(NgxParam **p) {
    alloc(p); si(*p,"SuperSampling.Available",1); return caps_fail ? FAIL : 1;
}
static NgxResult init(unsigned long long app, const wchar_t *path, VkInstance i, VkPhysicalDevice ph, VkDevice d,
    PFN_vkGetInstanceProcAddr ip, PFN_vkGetDeviceProcAddr dp, int version, const void *info) {
    assert(app == 231313132ull && !wcscmp(path,L"/tmp/"));
    assert(i == ctx.instance && ph == ctx.phys && d == ctx.device && ip && dp && version == 0x15 && !info);
    return init_fail ? FAIL : 1;
}
static NgxResult create(VkCommandBuffer cb, int type, NgxParam *p, NgxHandle **h) {
    assert(cb && type == 1); created++;
    assert(entry(p,"DLSS.Feature.Create.Flags")->v.i == 67);
    assert(entry(p,"DLSS.Hint.Render.Preset.Quality")->v.u == 11 ||
           entry(p,"DLSS.Hint.Render.Preset.Quality")->v.u == 5);
    assert(entry(p,"Width")->v.u == 1280 && entry(p,"OutWidth")->v.u == 1920);
    if (!no_handle) *h = (NgxHandle *)(uintptr_t)42;
    return create_fail ? FAIL : 1;
}
static int expect_mask, expect_reset;
static NgxResult evaluate(VkCommandBuffer cb, const NgxHandle *h, const NgxParam *p, void *callback) {
    assert(cb && h && !callback); evaluated++;
    NgxResourceVK *c = entry(p,"Color")->v.p, *z = entry(p,"Depth")->v.p;
    NgxResourceVK *m = entry(p,"MotionVectors")->v.p, *o = entry(p,"Output")->v.p;
    assert(c->r.img.format == VK_FORMAT_B10G11R11_UFLOAT_PACK32 && c->r.img.width == 1280 && !c->rw);
    assert(z->r.img.format == VK_FORMAT_D32_SFLOAT && z->r.img.range.aspectMask == VK_IMAGE_ASPECT_DEPTH_BIT);
    assert(m->r.img.format == VK_FORMAT_R16G16_SFLOAT && m->r.img.height == 720);
    assert(o->r.img.format == VK_FORMAT_R16G16B16A16_SFLOAT && o->r.img.width == 1920 && o->rw);
    NgxResourceVK *mask = entry(p,"DLSS.Input.Bias.Current.Color.Mask")->v.p;
    assert(!!mask == expect_mask);
    if (mask) assert(mask->r.img.format == VK_FORMAT_R8_UNORM);
    assert(entry(p,"Reset")->v.i == expect_reset);
    assert(entry(p,"Jitter.Offset.X")->v.f == 0.25f && entry(p,"Jitter.Offset.Y")->v.f == -0.375f);
    assert(entry(p,"MV.Scale.X")->v.f == 1 && entry(p,"MV.Scale.Y")->v.f == 1);
    assert(entry(p,"FrameTimeDeltaInMsec")->v.f == 20);
    return eval_fail ? FAIL : 1;
}
static NgxResult release(NgxHandle *h) { assert(h); released++; return 1; }
static NgxResult shutdown1(VkDevice d) { assert(d == ctx.device); stopped++; return shutdown_fail ? FAIL : 1; }
static NgxResult shutdown0(void) { stopped++; return 1; }
static NgxResult extensions(unsigned *ni, const char ***ie, unsigned *nd, const char ***de) {
    static const char *inst[] = {"instance", "instance", "extra"};
    static const char *dev[] = {"device", "missing", "device"};
    *ni = 3; *ie = inst; *nd = 3; *de = dev; return 1;
}
static void *plat_lib_sym(void *h, const char *p) {
    assert(h);
    if (missing_symbol && !strcmp(p,missing_symbol)) return NULL;
#define SYM(name, fn) if (!strcmp(p,"NVSDK_NGX_VULKAN_" name)) return (void *)fn
    SYM("Init_Ext2",init); SYM("RequiredExtensions",extensions);
    SYM("GetCapabilityParameters",caps); SYM("AllocateParameters",alloc);
    SYM("DestroyParameters",destroy); SYM("CreateFeature",create);
    SYM("EvaluateFeature",evaluate); SYM("ReleaseFeature",release);
    SYM("Shutdown1",shutdown1); SYM("Shutdown",shutdown0);
#undef SYM
    return NULL;
}
static void setup(void) {
    memset(&N,0,sizeof N); N.wanted = -1; N.tried = N.loaded = 1;
    N.init = init; N.req_ext = extensions; N.alloc_params = alloc; N.destroy_params = destroy;
    N.caps = caps; N.create = create; N.evaluate = evaluate; N.release = release; N.shutdown1 = shutdown1;
#ifdef TEST_MSVC_VTABLE
    vt.slot[3] = (void *)si; vt.slot[4] = (void *)su; vt.slot[6] = (void *)sf; vt.slot[0] = (void *)sp;
    vt.slot[11] = (void *)gi; vt.slot[12] = (void *)gu; vt.slot[14] = (void *)gf; vt.slot[8] = (void *)gp;
#else
    vt.slot[4] = (void *)si; vt.slot[3] = (void *)su; vt.slot[1] = (void *)sf; vt.slot[7] = (void *)sp;
    vt.slot[12] = (void *)gi; vt.slot[11] = (void *)gu; vt.slot[9] = (void *)gf; vt.slot[15] = (void *)gp;
#endif
    vk_dlss_init();
}
int main(void) {
    ctx.instance = (VkInstance)(uintptr_t)1; ctx.phys = (VkPhysicalDevice)(uintptr_t)2;
    ctx.device = (VkDevice)(uintptr_t)3; ctx.props.limits.maxImageDimension2D = 16384;
    setup(); assert(vk_dlss_available()); assert(allocated == destroyed);
    vk_dlss_init(); assert(allocated == destroyed);
    method = SR_OFF; assert(vk_dlss_wanted()); method = SR_DLSS;
    const char *ext[4] = {"instance"}; uint32_t n = 1;
    vk_dlss_instance_exts(ext,&n,2); assert(n == 2 && !strcmp(ext[1],"extra"));
    VkExtensionProperties have = {0}; strcpy(have.extensionName,"device");
    n = 0; vk_dlss_device_exts(&have,1,ext,&n,4); assert(n == 1 && !strcmp(ext[0],"device"));
    VkTemporalDispatch f = {
        .cb = (VkCommandBuffer)(uintptr_t)1, .width = 1280, .height = 720, .output_width = 1920, .output_height = 1080,
        .color = (VkImage)(uintptr_t)1, .depth = (VkImage)(uintptr_t)2, .motion = (VkImage)(uintptr_t)3,
        .output = (VkImage)(uintptr_t)4, .color_view = (VkImageView)(uintptr_t)5, .depth_view = (VkImageView)(uintptr_t)6,
        .motion_view = (VkImageView)(uintptr_t)7, .output_view = (VkImageView)(uintptr_t)8,
        .color_format = VK_FORMAT_B10G11R11_UFLOAT_PACK32, .depth_format = VK_FORMAT_D32_SFLOAT,
        .delta_ms = 20, .jitter_x = 0.25f, .jitter_y = -0.375f,
    };
    void *feature = NULL; expect_reset = 1;
    assert(vk_dlss_dispatch(&feature,&f) == 1 && feature && created == 1);
    DlssFeature *e = feature;
    assert(entry(e->params,"DLSS.Hint.Render.Preset.Performance")->v.u == 13);
    assert(entry(e->params,"DLSS.Hint.Render.Preset.UltraPerformance")->v.u == 12);
    expect_reset = 0; expect_mask = 1; f.mask = (VkImage)(uintptr_t)9; f.mask_view = (VkImageView)(uintptr_t)10;
    int before = allocated;
    assert(vk_dlss_dispatch(&feature,&f) == 1 && allocated == before && created == 1);
    expect_mask = 0; f.mask = VK_NULL_HANDLE; f.mask_view = VK_NULL_HANDLE;
    assert(vk_dlss_dispatch(&feature,&f) == 1);
    f.width = 1279; assert(vk_dlss_dispatch(&feature,&f) == -1); f.width = 1280;
    f.preset = 5; assert(vk_dlss_dispatch(&feature,&f) == -1); f.preset = 0;
    f.output_width = 0; assert(vk_dlss_dispatch(&feature,&f) == -1); f.output_width = 1920;
    f.depth_view = VK_NULL_HANDLE; assert(vk_dlss_dispatch(&feature,&f) == -1); f.depth_view = (VkImageView)(uintptr_t)6;
    f.mask = (VkImage)(uintptr_t)9; assert(vk_dlss_dispatch(&feature,&f) == -1); f.mask = VK_NULL_HANDLE;
    f.delta_ms = NAN; assert(vk_dlss_dispatch(&feature,&f) == -1); f.delta_ms = 20;
    f.output_width = 16385; assert(vk_dlss_dispatch(&feature,&f) == -1); f.output_width = 1920;
    f.output = f.color; assert(vk_dlss_dispatch(&feature,&f) == -1); f.output = (VkImage)(uintptr_t)4;
    assert(vk_dlss_dispatch(NULL,&f) == -1 && vk_dlss_dispatch(&feature,NULL) == -1);
    assert(evaluated == 3);
    eval_fail = 1; assert(vk_dlss_dispatch(&feature,&f) == -1 && !vk_dlss_available() && evaluated == 4);
    vk_dlss_free(feature); feature = NULL; assert(allocated == destroyed && released == 1);
    vk_dlss_shutdown(); assert(stopped == 1); vk_dlss_shutdown(); assert(stopped == 1);
    eval_fail = 0; setup(); create_fail = 1;
    assert(vk_dlss_dispatch(&feature,&f) == -1 && feature); vk_dlss_free(feature); feature = NULL;
    assert(allocated == destroyed); vk_dlss_shutdown();
    create_fail = 0; no_handle = 1; setup();
    assert(vk_dlss_dispatch(&feature,&f) == -1 && feature); vk_dlss_free(feature); feature = NULL;
    assert(allocated == destroyed); vk_dlss_shutdown(); no_handle = 0;
    setup(); alloc_fail = 1;
    assert(vk_dlss_dispatch(&feature,&f) == -1 && !feature && allocated == destroyed);
    vk_dlss_shutdown(); alloc_fail = 0;
    bad_roundtrip = 1; setup(); assert(!vk_dlss_available() && allocated == destroyed);
    vk_dlss_shutdown(); bad_roundtrip = 0;
    caps_fail = 1; setup(); assert(!vk_dlss_available() && allocated == destroyed);
    vk_dlss_shutdown(); caps_fail = 0;
    setup(); N.shutdown1 = NULL; N.shutdown = shutdown0;
    before = stopped; vk_dlss_shutdown(); assert(stopped == before + 1);
    setup(); N.shutdown1 = NULL; N.shutdown = NULL; N.core = &core_token;
    before = closed;
    int stops_before = stopped;
    vk_dlss_shutdown(); assert(stopped == stops_before && closed == before && N.core && N.initialized);
    N.shutdown1 = shutdown1; shutdown_fail = 1;
    vk_dlss_shutdown(); assert(closed == before && N.core && N.initialized);
    shutdown_fail = 0;
    vk_dlss_shutdown(); assert(closed == before + 1 && !N.core && !N.initialized);
    assert(perf_quality(1920,1920) == 5 && perf_quality(1280,1920) == 2 && perf_quality(1120,1920) == 1);
    assert(perf_quality(960,1920) == 0 && perf_quality(640,1920) == 3);
    setup(); f.preset = 5; expect_reset = 1;
    assert(vk_dlss_dispatch(&feature,&f) == 1);
    assert(entry(((DlssFeature *)feature)->params,"DLSS.Hint.Render.Preset.Performance")->v.u == 5);
    vk_dlss_free(feature); feature = NULL; vk_dlss_shutdown(); assert(allocated == destroyed);
    init_fail = 1; setup(); assert(!vk_dlss_available() && !N.initialized); vk_dlss_shutdown(); init_fail = 0;
    memset(&N,0,sizeof N); N.wanted = -1;
    path_fail = 1; before = load_errors;
    assert(!core_load() && load_errors == before && !N.core); vk_dlss_shutdown();
    path_fail = 0;
    assert(!core_load() && load_errors == before + 1 && !N.core); vk_dlss_shutdown();
    loader_on = 1; missing_symbol = "NVSDK_NGX_VULKAN_DestroyParameters";
    before = closed;
    assert(!core_load() && N.core); vk_dlss_shutdown(); assert(closed == before + 1);
    missing_symbol = NULL; dir_fail = 1;
    vk_dlss_init(); assert(N.loaded && !N.initialized && !vk_dlss_available());
    vk_dlss_shutdown(); dir_fail = 0;
    missing_symbol = "NVSDK_NGX_VULKAN_Shutdown1";
    assert(core_load() && !N.shutdown1 && N.shutdown); vk_dlss_init(); assert(vk_dlss_available());
    before = closed; vk_dlss_shutdown(); assert(closed == before + 1); missing_symbol = NULL;
    before = closed;
    vk_dlss_shutdown(); assert(closed == before);
    assert(closed == opened + 1); // Includes the manually supplied live core above.
    method = SR_FSR3; N.wanted = -1; assert(!vk_dlss_wanted());
    puts("DLSS backend mock checks passed");
    return 0;
}
'''


def main():
    source = (ROOT / "runtime/vk_dlss.c").read_text()
    # Stub host/platform dependencies, not the NGX code or public dispatch header.
    for header in ("host.h", "platform/platform.h", "super_resolution.h", "vk.h"):
        source = source.replace(f'#include "{header}"', "")
    # Exercise both documented vtable orders on the host, without pretending the Windows loader ran.
    source = source.replace("#ifdef _WIN32\n// MSVC", "#if defined(_WIN32) || defined(TEST_MSVC_VTABLE)\n// MSVC")
    with tempfile.TemporaryDirectory(prefix="dlss-test-", dir=ROOT / "build") as tmp:
        tmp = Path(tmp)
        cfile = tmp / "test.c"
        # Standard declarations precede the dependency stubs.
        cfile.write_text("#include <stdio.h>\n#include <string.h>\n#include <assert.h>\n" + STUBS + source + HARNESS)
        exe = tmp / "test"
        for defines in ([], ["-DTEST_MSVC_VTABLE"]):
            subprocess.run(shlex.split(os.environ.get("CC", "cc")) + defines + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer", "-I", str(ROOT / "runtime"), str(cfile), "-o", str(exe),
            ], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
