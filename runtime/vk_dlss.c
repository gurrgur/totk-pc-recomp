// Driver NGX ABI, without NVIDIA SDK headers. Scene selection and motion live in vk_temporal.c.
// The parameter overloads have opposite vtable order under MSVC and the Itanium C++ ABI;
// vtable_ok verifies the four overloads we use before querying capabilities.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include "host.h"
#include "platform/platform.h"
#include "super_resolution.h"
#include "vk.h"
#include "vk_temporal_backend.h"

typedef int NgxResult;
#define NGX_OK(r) (((unsigned)(r) & 0xFFF00000u) != 0xBAD00000u)
#define NGX_VERSION_API 0x15
#define NGX_FEATURE_SUPERSAMPLING 1
#define NGX_PERFQ_DLAA 5

typedef struct NgxParam NgxParam;
typedef struct NgxHandle NgxHandle;
typedef struct { void *slot[17]; } NgxParamVtbl;
struct NgxParam { const NgxParamVtbl *vt; };
typedef struct {
    VkImageView view;
    VkImage image;
    VkImageSubresourceRange range;
    VkFormat format;
    unsigned width, height;
} NgxImageView;
typedef struct {
    union { NgxImageView img; struct { VkBuffer buffer; unsigned bytes; } buf; } r;
    int type;
    _Bool rw;
} NgxResourceVK;

typedef NgxResult (*PFN_Init)(unsigned long long, const wchar_t *, VkInstance, VkPhysicalDevice, VkDevice,
                              PFN_vkGetInstanceProcAddr, PFN_vkGetDeviceProcAddr, int, const void *);
typedef NgxResult (*PFN_ReqExt)(unsigned *, const char ***, unsigned *, const char ***);
typedef NgxResult (*PFN_Params)(NgxParam **);
typedef NgxResult (*PFN_DestroyParams)(NgxParam *);
typedef NgxResult (*PFN_Create)(VkCommandBuffer, int, NgxParam *, NgxHandle **);
typedef NgxResult (*PFN_Evaluate)(VkCommandBuffer, const NgxHandle *, const NgxParam *, void *);
typedef NgxResult (*PFN_Release)(NgxHandle *);
typedef NgxResult (*PFN_Shutdown)(void);
typedef NgxResult (*PFN_Shutdown1)(VkDevice);

static struct {
    void *core;
    PFN_Init init;
    PFN_ReqExt req_ext;
    PFN_Params caps, alloc_params;
    PFN_DestroyParams destroy_params;
    PFN_Create create;
    PFN_Evaluate evaluate;
    PFN_Release release;
    PFN_Shutdown shutdown;
    PFN_Shutdown1 shutdown1;
    VkDevice device;
    int tried, loaded, initialized, available, failed;
    int wanted;
} N = {.wanted = -1};

typedef void (*SetI)(NgxParam *, const char *, int);
typedef void (*SetU)(NgxParam *, const char *, unsigned);
typedef void (*SetF)(NgxParam *, const char *, float);
typedef void (*SetP)(NgxParam *, const char *, void *);
typedef NgxResult (*GetI)(const NgxParam *, const char *, int *);
typedef NgxResult (*GetU)(const NgxParam *, const char *, unsigned *);
typedef NgxResult (*GetF)(const NgxParam *, const char *, float *);
typedef NgxResult (*GetP)(const NgxParam *, const char *, void **);
#ifdef _WIN32
// MSVC reverses each group of overloaded methods.
enum { S_P = 0, S_D12, S_D11, S_I, S_U32, S_D, S_F, S_U64, G_P, G_D12, G_D11, G_I, G_U32, G_D, G_F, G_U64 };
#else
enum { S_U64 = 0, S_F, S_D, S_U32, S_I, S_D11, S_D12, S_P, G_U64, G_F, G_D, G_U32, G_I, G_D11, G_D12, G_P };
#endif
static void p_seti(NgxParam *p, const char *n, int v) { ((SetI)p->vt->slot[S_I])(p, n, v); }
static void p_setu(NgxParam *p, const char *n, unsigned v) { ((SetU)p->vt->slot[S_U32])(p, n, v); }
static void p_setf(NgxParam *p, const char *n, float v) { ((SetF)p->vt->slot[S_F])(p, n, v); }
static void p_setp(NgxParam *p, const char *n, void *v) { ((SetP)p->vt->slot[S_P])(p, n, v); }
static int p_geti(const NgxParam *p, const char *n, int *v) { return NGX_OK(((GetI)p->vt->slot[G_I])(p, n, v)); }
static int p_getu(const NgxParam *p, const char *n, unsigned *v) { return NGX_OK(((GetU)p->vt->slot[G_U32])(p, n, v)); }
static int p_getf(const NgxParam *p, const char *n, float *v) { return NGX_OK(((GetF)p->vt->slot[G_F])(p, n, v)); }
static int p_getp(const NgxParam *p, const char *n, void **v) { return NGX_OK(((GetP)p->vt->slot[G_P])(p, n, v)); }

#ifdef _WIN32
#define DLSS_MODEL "nvngx_dlss.dll"
#else
#define DLSS_MODEL "libnvidia-ngx-dlss.so.<version>"
#endif

int vk_dlss_wanted(void) {
    if (N.wanted < 0) N.wanted = sr_method() == SR_DLSS;
    return N.wanted;
}

static int core_load(void) {
    if (N.tried) return N.loaded;
    N.tried = 1;
    char path[PLAT_PATH_MAX];
    if (!plat_ngx_core_path(path, sizeof path)) {
        rt_log("dlss: the driver has no NGX core (registry NGXCore missing): DLSS off");
        return 0;
    }
    N.core = plat_lib_open(path);
    if (!N.core) {
        char why[256];
        plat_lib_error(why, sizeof why);
        rt_log("dlss: cannot load %s (%s; not an NVIDIA driver, or one without NGX): DLSS off", path, why);
        return 0;
    }
#define FN(f, name) N.f = plat_lib_sym(N.core, name)
    FN(init, "NVSDK_NGX_VULKAN_Init_Ext2");
    FN(req_ext, "NVSDK_NGX_VULKAN_RequiredExtensions");
    FN(caps, "NVSDK_NGX_VULKAN_GetCapabilityParameters");
    FN(alloc_params, "NVSDK_NGX_VULKAN_AllocateParameters");
    FN(destroy_params, "NVSDK_NGX_VULKAN_DestroyParameters");
    FN(create, "NVSDK_NGX_VULKAN_CreateFeature");
    FN(evaluate, "NVSDK_NGX_VULKAN_EvaluateFeature");
    FN(release, "NVSDK_NGX_VULKAN_ReleaseFeature");
    FN(shutdown1, "NVSDK_NGX_VULKAN_Shutdown1");
    FN(shutdown, "NVSDK_NGX_VULKAN_Shutdown");
#undef FN
    if (!N.init || !N.req_ext || !N.caps || !N.alloc_params || !N.destroy_params || !N.create || !N.evaluate || !N.release) {
        rt_log("dlss: the NGX core %s lacks Vulkan entry points: DLSS off", path);
        return 0;
    }
    N.loaded = 1;
    rt_log("dlss: NGX core %s", path);
    return 1;
}

static int listed(const char *const *list, uint32_t n, const char *s) {
    for (uint32_t i = 0; i < n; i++)
        if (!strcmp(list[i], s)) return 1;
    return 0;
}

void vk_dlss_instance_exts(const char **ext, uint32_t *n, uint32_t max) {
    unsigned ni = 0, nd = 0;
    const char **ie = NULL, **de = NULL;
    if (!ext || !n || *n > max || !vk_dlss_wanted() || !core_load() ||
        !NGX_OK(N.req_ext(&ni, &ie, &nd, &de)) || (ni && !ie)) return;
    for (unsigned i = 0; i < ni; i++)
        if (ie[i] && !listed(ext, *n, ie[i]) && *n < max) ext[(*n)++] = ie[i];
}

void vk_dlss_device_exts(const VkExtensionProperties *have, uint32_t nhave, const char **ext, uint32_t *n,
                         uint32_t max) {
    unsigned ni = 0, nd = 0;
    const char **ie = NULL, **de = NULL;
    if (!ext || !n || *n > max || (nhave && !have) || !vk_dlss_wanted() || !core_load() ||
        !NGX_OK(N.req_ext(&ni, &ie, &nd, &de)) || (nd && !de)) return;
    for (unsigned i = 0; i < nd; i++) {
        if (!de[i]) continue;
        int ok = 0;
        for (uint32_t k = 0; k < nhave && !ok; k++) ok = !strcmp(have[k].extensionName, de[i]);
        if (!ok) rt_log("dlss: the device lacks %s, which NGX asks for", de[i]);
        else if (!listed(ext, *n, de[i]) && *n < max) ext[(*n)++] = de[i];
    }
}

static int vtable_ok(void) {
    NgxParam *p = NULL;
    NgxResult r = N.alloc_params(&p);
    if (!NGX_OK(r) || !p) {
        if (p) N.destroy_params(p);
        rt_log("dlss: NGX layout-check parameter allocation failed (%08x)", (unsigned)r);
        return 0;
    }
    int i = 0;
    unsigned u = 0;
    float f = 0;
    void *v = NULL;
    p_seti(p, "TotK.Test.I", -12345);
    p_setu(p, "TotK.Test.U", 4000000000u);
    p_setf(p, "TotK.Test.F", 1.25f);
    p_setp(p, "TotK.Test.P", p);
    int ok = p_geti(p, "TotK.Test.I", &i) && i == -12345 && p_getu(p, "TotK.Test.U", &u) && u == 4000000000u &&
             p_getf(p, "TotK.Test.F", &f) && f == 1.25f && p_getp(p, "TotK.Test.P", &v) && v == p;
    N.destroy_params(p);
    if (!ok) rt_log("dlss: NGX parameter layout check failed (i=%d u=%u f=%g p=%p): DLSS off", i, u, f, v);
    return ok;
}

void vk_dlss_init(void) {
    if (!vk_dlss_wanted() || !core_load() || N.initialized || N.failed) return;
    const VkCtx *c = vk_ctx();
    if (!c || !c->instance || !c->phys || !c->device) return;
    wchar_t wdir[PLAT_PATH_MAX];
    if (!plat_exe_dir_wide(wdir, PLAT_PATH_MAX)) {
        rt_log("dlss: cannot name the executable's folder for NGX: DLSS off");
        return;
    }
    // NVIDIA's sample application id for builds without an issued id.
    NgxResult r = N.init(231313132ull, wdir, c->instance, c->phys, c->device, vkGetInstanceProcAddr,
                         vkGetDeviceProcAddr, NGX_VERSION_API, NULL);
    if (!NGX_OK(r)) {
        rt_log("dlss: NGX init failed (%08x): DLSS off", (unsigned)r);
        N.failed = 1;
        return;
    }
    N.initialized = 1;
    N.device = c->device;
    if (!vtable_ok()) { N.failed = 1; return; }
    NgxParam *caps = NULL;
    int avail = 0, driver = 0;
    r = N.caps(&caps);
    if (NGX_OK(r) && caps) {
        p_geti(caps, "SuperSampling.Available", &avail);
        p_geti(caps, "SuperSampling.NeedsUpdatedDriver", &driver);
    } else rt_log("dlss: NGX capability query failed (%08x)", (unsigned)r);
    if (caps) N.destroy_params(caps);
    N.available = avail != 0;
    rt_log("dlss: NGX up; DLSS %s%s", avail ? "available" : "NOT available (is " DLSS_MODEL " next to the executable?)",
           driver ? "; the driver needs an update" : "");
}

int vk_dlss_available(void) { return vk_dlss_wanted() && N.available && !N.failed; }

static int dl_fail(const char *what, NgxResult r) {
    rt_log("dlss: %s failed (%08x): DLSS off for this run", what, (unsigned)r);
    N.failed = 1;
    return -1;
}

typedef struct {
    NgxHandle *handle;
    NgxParam *params;
    uint32_t w, h, ow, oh;
    int preset;
} DlssFeature;

static NgxResourceVK res_image(VkImageView v, VkImage img, VkFormat f, uint32_t w, uint32_t h, VkImageAspectFlags a,
                               int rw) {
    NgxResourceVK r = {0};
    r.r.img = (NgxImageView){v, img, {a, 0, 1, 0, 1}, f, w, h};
    r.rw = rw;
    return r;
}

static int perf_quality(uint32_t w, uint32_t ow) {
    if (w >= ow) return NGX_PERFQ_DLAA;
    double k = (double)w / ow;
    return k >= 0.66 ? 2 : k >= 0.57 ? 1 : k >= 0.49 ? 0 : 3;
}

void vk_dlss_free(void *feature) {
    DlssFeature *e = feature;
    if (!e) return;
    if (e->handle) {
        NgxResult r = N.release(e->handle);
        if (!NGX_OK(r)) rt_log("dlss: feature release failed (%08x)", (unsigned)r);
    }
    if (e->params) N.destroy_params(e->params);
    free(e);
}

static int valid_frame(const VkTemporalDispatch *f) {
    const VkCtx *c = vk_ctx();
    if (!f || !c || c->device != N.device || !f->cb || !f->width || !f->height ||
        f->output_width < f->width || f->output_height < f->height ||
        f->output_width > c->props.limits.maxImageDimension2D || f->output_height > c->props.limits.maxImageDimension2D ||
        !f->color || !f->depth || !f->motion || !f->output || !f->color_view || !f->depth_view ||
        !f->motion_view || !f->output_view || (!!f->mask != !!f->mask_view) ||
        f->output == f->color || f->output == f->depth || f->output == f->motion ||
        (f->mask && f->output == f->mask) || f->preset < 0 || f->preset > 13 ||
        !isfinite(f->jitter_x) || !isfinite(f->jitter_y) || !isfinite(f->delta_ms) || f->delta_ms <= 0 ||
        !isfinite(f->sharpness)) return 0;
    if (f->color_format != VK_FORMAT_B10G11R11_UFLOAT_PACK32 && f->color_format != VK_FORMAT_R16G16B16A16_SFLOAT) return 0;
    return f->depth_format == VK_FORMAT_D32_SFLOAT || f->depth_format == VK_FORMAT_D16_UNORM ||
           f->depth_format == VK_FORMAT_D24_UNORM_S8_UINT || f->depth_format == VK_FORMAT_D32_SFLOAT_S8_UINT;
}

int vk_dlss_dispatch(void **feature, const VkTemporalDispatch *f) {
    if (!feature || !vk_dlss_available()) return -1;
    if (!valid_frame(f)) {
        rt_log("dlss: invalid dispatch dimensions, resources or parameters");
        return -1;
    }
    DlssFeature *e = *feature;
    if (e && (e->w != f->width || e->h != f->height || e->ow != f->output_width ||
              e->oh != f->output_height || e->preset != f->preset)) {
        rt_log("dlss: cached feature dimensions or preset changed; retire the feature before replacing it");
        return -1;
    }
    int fresh = !e;
    if (fresh) {
        e = calloc(1, sizeof *e);
        if (!e) return dl_fail("feature allocation", 0);
        NgxResult r = N.alloc_params(&e->params);
        if (!NGX_OK(r) || !e->params) {
            vk_dlss_free(e);
            return dl_fail("parameter allocation", r);
        }
        e->w = f->width, e->h = f->height, e->ow = f->output_width, e->oh = f->output_height, e->preset = f->preset;
        NgxParam *p = e->params;
        p_setu(p, "CreationNodeMask", 1);
        p_setu(p, "VisibilityNodeMask", 1);
        p_setu(p, "Width", e->w);
        p_setu(p, "Height", e->h);
        p_setu(p, "OutWidth", e->ow);
        p_setu(p, "OutHeight", e->oh);
        p_seti(p, "PerfQualityValue", perf_quality(e->w, e->ow));
        // HDR | low-resolution motion | automatic exposure; motion is already unjittered render pixels.
        p_seti(p, "DLSS.Feature.Create.Flags", (1 << 0) | (1 << 1) | (1 << 6));
        p_seti(p, "DLSS.Enable.Output.Subrects", 0);
        // NVIDIA DLSS 4.5 defaults: K for DLAA/Quality/Balanced, M for Performance, L for Ultra Performance.
        unsigned pk = e->preset ? (unsigned)e->preset : 11, pm = e->preset ? (unsigned)e->preset : 13;
        unsigned pl = e->preset ? (unsigned)e->preset : 12;
        p_setu(p, "DLSS.Hint.Render.Preset.DLAA", pk);
        p_setu(p, "DLSS.Hint.Render.Preset.UltraQuality", pk);
        p_setu(p, "DLSS.Hint.Render.Preset.Quality", pk);
        p_setu(p, "DLSS.Hint.Render.Preset.Balanced", pk);
        p_setu(p, "DLSS.Hint.Render.Preset.Performance", pm);
        p_setu(p, "DLSS.Hint.Render.Preset.UltraPerformance", pl);
        r = N.create(f->cb, NGX_FEATURE_SUPERSAMPLING, p, &e->handle);
        if (!NGX_OK(r) || !e->handle) {
            // A failing create may still have recorded GPU work. The caller retires this context too.
            *feature = e;
            return dl_fail("DLSS feature creation", r);
        }
        *feature = e;
        int q = perf_quality(e->w, e->ow), pr = q == 0 ? (int)pm : q == 3 ? (int)pl : (int)pk;
        rt_log("dlss: feature created at %ux%u -> %ux%u, quality value %d, preset %c (scene format %d, depth format %d)",
               e->w, e->h, e->ow, e->oh, q, 'A' + pr - 1, (int)f->color_format, (int)f->depth_format);
    }
    NgxParam *p = e->params;
    NgxResourceVK rc = res_image(f->color_view, f->color, f->color_format, e->w, e->h, VK_IMAGE_ASPECT_COLOR_BIT, 0);
    NgxResourceVK rz = res_image(f->depth_view, f->depth, f->depth_format, e->w, e->h, VK_IMAGE_ASPECT_DEPTH_BIT, 0);
    NgxResourceVK rm = res_image(f->motion_view, f->motion, VK_FORMAT_R16G16_SFLOAT, e->w, e->h, VK_IMAGE_ASPECT_COLOR_BIT, 0);
    NgxResourceVK ro = res_image(f->output_view, f->output, VK_FORMAT_R16G16B16A16_SFLOAT,
                                 e->ow, e->oh, VK_IMAGE_ASPECT_COLOR_BIT, 1);
    NgxResourceVK rk = res_image(f->mask_view, f->mask, VK_FORMAT_R8_UNORM, e->w, e->h, VK_IMAGE_ASPECT_COLOR_BIT, 0);
    p_setp(p, "Color", &rc);
    p_setp(p, "Depth", &rz);
    p_setp(p, "MotionVectors", &rm);
    p_setp(p, "Output", &ro);
    p_setp(p, "DLSS.Input.Bias.Current.Color.Mask", f->mask ? &rk : NULL);
    p_setu(p, "DLSS.Input.Bias.Current.Color.Subrect.Base.X", 0);
    p_setu(p, "DLSS.Input.Bias.Current.Color.Subrect.Base.Y", 0);
    p_setf(p, "Jitter.Offset.X", f->jitter_x);
    p_setf(p, "Jitter.Offset.Y", f->jitter_y);
    p_setf(p, "Sharpness", f->sharpness);
    p_seti(p, "Reset", fresh || f->reset);
    p_setf(p, "MV.Scale.X", 1.0f);
    p_setf(p, "MV.Scale.Y", 1.0f);
    p_setu(p, "DLSS.Render.Subrect.Dimensions.Width", e->w);
    p_setu(p, "DLSS.Render.Subrect.Dimensions.Height", e->h);
    p_setf(p, "DLSS.Pre.Exposure", 1.0f);
    p_setf(p, "DLSS.Exposure.Scale", 1.0f);
    p_setf(p, "FrameTimeDeltaInMsec", f->delta_ms);
    NgxResult r = N.evaluate(f->cb, e->handle, p, NULL);
    if (!NGX_OK(r)) return dl_fail("DLSS evaluation", r);
    return 1;
}

void vk_dlss_shutdown(void) {
    // The shared renderer has drained the recorder/GPU and freed every feature before this call.
    N.available = 0;
    if (N.initialized) {
        // Older cores need not export shutdown. Keep their code resident rather than unload a live NGX runtime.
        if (!N.shutdown1 && !N.shutdown) {
            N.failed = 1;
            return;
        }
        NgxResult r = N.shutdown1 ? N.shutdown1(N.device) : N.shutdown();
        if (!NGX_OK(r)) {
            rt_log("dlss: NGX shutdown failed (%08x); keeping the core loaded", (unsigned)r);
            N.failed = 1;
            return;
        }
    }
    if (N.core) plat_lib_close(N.core);
    memset(&N, 0, sizeof N);
    N.wanted = -1;
}
