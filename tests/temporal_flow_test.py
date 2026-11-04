"""Compile the real shared renderer with typed Vulkan/backend mocks; no SDK or GPU required."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CC = shutil.which("clang")

HARNESS = r'''
#include <assert.h>
#include "vk_temporal.c"
#include "vk_mvprev.c"
static VkCtx context;
static int setting, fsr_available = 1, dlss_available, dispatch_result = 1;
static int fsr_calls, dlss_calls, drains, invalidations, blits, clears, allocations, frees;
static int feature_frees, shutdowns, fail_alloc, image_attempts;
static int vk_allocations, vk_frees, fail_view;
static uint64_t next_handle = 100, reserved_fence = 77, completed_fence = 76;
static float camera[64], sampler_bias;
static unsigned char mapped[65536];
static int have_camera = 1;
static char applied_mode[32];
static const char *quality_setting = "quality";
static int idle_calls;
static VkTemporalDispatch dispatched;
static struct { uint64_t fence; void (*fn)(void *); void *arg; } pending[64];
static unsigned pending_count;
static void complete(uint64_t fence) {
    for (unsigned i = 0; i < pending_count;) {
        if (pending[i].fence > fence) { i++; continue; }
        pending[i].fn(pending[i].arg);
        pending[i] = pending[--pending_count];
    }
}

static void make_camera(void) {
    memset(camera, 0, sizeof camera);
    camera[0] = camera[5] = camera[10] = 1;
    for (int r = 0; r < 4; r++) camera[12 + r * 4 + r] = 1;
    float *p = camera + 28;
    p[0] = 1; p[5] = 2; p[10] = -1.002002f; p[11] = -0.2002002f; p[14] = -1;
}

static VkExec scene(void) {
    VkExec ex = {0};
    ex.cb = (VkCommandBuffer)1;
    ex.fence = reserved_fence;
    ex.pass_ncolor = 3;
    ex.pass_w = 1280; ex.pass_h = 720;
    ex.depth = (VkExecTarget){.image = (VkImage)2, .width = 1280, .height = 720,
        .format = VK_FORMAT_D32_SFLOAT, .img = (const VkTexImage *)2};
    ex.color[0] = (VkExecTarget){.image = (VkImage)3, .width = 1280, .height = 720,
        .format = VK_FORMAT_R8G8B8A8_UNORM, .img = (const VkTexImage *)3};
    ex.ubo[0][1] = (p_bind_buf){.addr = 0x10000, .size = CAM_BLOCK};
    return ex;
}

static void arm(VkExec *ex) {
    vk_temporal_pass(ex);
    assert(D.gbuf && !D.armed);
    ex->pass_ncolor = 1;
    ex->color[0].format = VK_FORMAT_R16G16B16A16_SFLOAT;
    vk_temporal_pass(ex);
    vk_temporal_draw(ex);
    assert(D.armed);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    setting = atoi(argv[1]);
    make_camera();
    dlss_available = setting == SR_DLSS;
    vk_temporal_init();
    assert(method == setting);
    setting = setting == SR_FSR3 ? SR_DLSS : SR_FSR3;
    assert(selected_method() == method); // method is restart-only
    if (method == SR_OFF) {
        VkExec ex = scene();
        vk_temporal_pass(&ex);
        assert(!D.gbuf && !D.armed && !fsr_calls && !dlss_calls);
        assert(!strcmp(applied_mode, "off"));
        vk_temporal_shutdown();
        return 0;
    }
    dlss_available = method == SR_DLSS;
    D.on = vk_temporal_available();
    assert(D.on); // FSR works with no NVIDIA backend
    assert(!cfg_bool("TOTK_DLAA"));
    vk_mvprev_frame();
    assert(vk_mvprev_on()); // new FSR selection, legacy DLAA unset, still captures object history
    VkExec unsupported = scene();
    unsupported.depth.level = 1;
    vk_temporal_pass(&unsupported);
    assert(!D.gbuf && !D.armed);
    unsupported = scene();
    vk_temporal_pass(&unsupported);
    unsupported.pass_ncolor = 1;
    unsupported.color[0].format = VK_FORMAT_R16G16B16A16_SFLOAT;
    unsupported.color[0].base_layer = 1;
    vk_temporal_pass(&unsupported);
    assert(!D.armed); // NGX/FSR must not receive mip-0 metadata for another scene subresource
    D.gbuf = 0;
    VkExec ex = scene();
    ex.ubo[0][BONES] = (p_bind_buf){.addr = 0x11000, .size = 0x100};
    vk_mvprev_draw(&ex);
    vk_mvprev_frame();
    uint64_t previous;
    int how;
    assert(vk_mvprev_find(BONES, 0x11000, 0x100, &previous, &how) && previous == 0x11000);
    arm(&ex);
    ex.pass_w = 320;
    ex.depth.image = VK_NULL_HANDLE;
    ex.color[0].width = 320;
    vk_temporal_pass(&ex); // actual admission -> dispatch -> common blit
    assert(D.done && !D.up && blits == 1 && D.evals == 1);
    assert(dispatched.recording_fence == reserved_fence);
    assert(dispatched.reset && dispatched.width == 1280 && dispatched.output_width == 1280);
    assert(dispatched.mask == VK_NULL_HANDLE); // cold camera cannot publish old classes
    assert(fabsf(dispatched.near_plane - 0.1f) < 0.001f);
    assert(fabsf(dispatched.far_plane - 100.0f) < 0.01f);
    assert(fabsf(dispatched.vertical_fov - 2 * atanf(0.5f)) < 1e-6f);
    assert(invalidations && drains);
    assert((method == SR_FSR3 ? fsr_calls : dlss_calls) == 1);
    assert((method == SR_FSR3 ? dlss_calls : fsr_calls) == 0);

    ex = scene();
    ex.color[0].format = VK_FORMAT_R16G16B16A16_SFLOAT;
    // Continuous camera creates object/camera motion and a character mask.
    D.done = 0;
    evaluate(&ex, NULL);
    assert(!dispatched.reset && dispatched.mask != VK_NULL_HANDLE);
    assert(D.last->chars_ok);

    // Camera cut restarts motion and masks before the backend sees them.
    camera[3] = 200;
    D.done = 0;
    evaluate(&ex, NULL);
    assert(dispatched.reset && dispatched.mask == VK_NULL_HANDLE);
    assert(!D.last->chars_ok);

    // FSR rejects unknown projections instead of inventing a permanent FOV.
    if (method == SR_FSR3) {
        float saved = camera[33];
        camera[33] = 0;
        int before = fsr_calls;
        D.done = 0;
        evaluate(&ex, NULL);
        assert(!D.done && fsr_calls == before && !D.camera_valid && D.reset);
        camera[33] = saved;
        evaluate(&ex, NULL);
        assert(D.done && dispatched.reset);
    }

    // DRS changes size even when it reuses a cached resource: no stale history.
    D.color.width = D.depth.width = 960;
    D.color.height = D.depth.height = 540;
    D.done = 0;
    evaluate(&ex, NULL);
    assert(dispatched.reset && dispatched.width == 960 && !D.last->chars_ok);
    D.color.width = D.depth.width = 1280;
    D.color.height = D.depth.height = 720;
    D.done = 0;
    evaluate(&ex, NULL);
    assert(dispatched.reset && !D.last->chars_ok);

    // Upscale path publishes substitution only after successful dispatch/blit.
    VkExecTarget output = D.color;
    output.width = 1920; output.height = 1080; output.image = (VkImage)8;
    D.done = 0;
    evaluate(&ex, &output);
    assert(D.done && D.up && vk_temporal_swap_img == D.color.img && vk_temporal_swap_view);
    assert(dispatched.output_width == 1920);
    reserved_fence++;
    vk_temporal_sampled();
    assert(D.last->fence == reserved_fence);
    vk_temporal_frame();
    assert(!D.done && !D.up && !vk_temporal_swap_view);
    quality_setting = "balanced";
    vk_temporal_frame();
    assert(!strcmp(applied_mode, "balanced") && D.reset && !D.cam_prev_ok);

    // Missing snapshot: FSR rejects unknown camera; DLSS can resolve zero motion with reset.
    D.color = ex.color[0]; D.depth = ex.depth;
    D.cam_addr = 0x10000; D.cam_depth = D.depth.image;
    have_camera = 0;
    int calls = fsr_calls + dlss_calls, before = blits;
    evaluate(&ex, NULL);
    if (method == SR_FSR3) assert(!D.done && fsr_calls + dlss_calls == calls && blits == before);
    else assert(D.done && dispatched.reset && !dispatched.mask);
    assert(!D.jit_ok || D.done);
    have_camera = 1;

    // Transient backend deferral keeps scale, original image and retry ability.
    D.done = D.up = 0;
    dispatch_result = 0;
    D.jit_used = D.jit_ok = 1; D.ux = 0.25f; D.uy = -0.125f;
    before = blits;
    evaluate(&ex, &output);
    assert(!D.done && !D.up && !D.failed && !D.jit_ok && blits == before);
    assert(strcmp(applied_mode, "off"));
    dispatch_result = 1;
    evaluate(&ex, &output);
    assert(D.done && D.up && dispatched.reset);
    assert(dispatched.jitter_x == 0.25f && dispatched.jitter_y == -0.125f); // recorded scene was not unjittered by deferral

    if (method == SR_DLSS) {
        // Live preset changes retire every cached feature, including recordings not submitted yet.
        D.done = D.up = 0;
        D.preset_dirty = 1;
        int before_free = frees;
        evaluate(&ex, &output);
        assert(D.done && pending_count && frees == before_free);
    }

    // Permanent failure never blits/substitutes and restores base render scale/bias.
    D.done = D.up = 0;
    vk_temporal_swap_img = NULL; vk_temporal_swap_view = VK_NULL_HANDLE;
    dispatch_result = -1;
    before = blits;
    evaluate(&ex, &output);
    assert(D.failed && !D.done && !D.up && !D.jit_ok && blits == before);
    assert(!vk_temporal_swap_view && !strcmp(applied_mode, "off") && sampler_bias == 0);
    vk_mvprev_frame();
    assert(!vk_mvprev_on());
    int failed_calls = fsr_calls + dlss_calls;
    vk_temporal_pass(&ex);
    assert(fsr_calls + dlss_calls == failed_calls);

    // Shutdown owns all image/buffer/pipeline resources; repeated calls are harmless.
    vk_temporal_shutdown();
    assert(allocations == frees && vk_allocations == vk_frees && shutdowns == 2 && idle_calls);
    int released = feature_frees;
    vk_temporal_shutdown();
    assert(shutdowns == 2 && feature_frees == released);

    // Eviction copies the entire resource payload; no old recording loses its feature/images.
    for (unsigned i = 0; i < 5; i++)
        assert(size_for((VkCommandBuffer)1, reserved_fence, 640 + i, 360, 640 + i, 360));
    int before_free = frees;
    assert(size_for((VkCommandBuffer)1, reserved_fence, 800, 450, 800, 450));
    assert(frees == before_free && pending_count == 1);
    complete(reserved_fence - 1);
    assert(frees == before_free);
    complete(reserved_fence);
    assert(frees > before_free);
    for (unsigned i = 0; i < 5; i++) free_size(&D.sizes[i]);
    assert(allocations == frees);
    for (unsigned i = 0; i < SNAPS; i++)
        assert(snap_for((VkCommandBuffer)1, reserved_fence, 640 + i, 360, VK_FORMAT_R16G16B16A16_SFLOAT));
    before_free = frees;
    assert(snap_for((VkCommandBuffer)1, reserved_fence, 800, 450, VK_FORMAT_R16G16B16A16_SFLOAT));
    assert(frees == before_free && pending_count == 1);
    complete(reserved_fence);
    for (unsigned i = 0; i < SNAPS; i++) free_snap(&R.s[i]);
    assert(allocations == frees);

    // Partial image creation is rolled back, not reused as a cache hit.
    fail_alloc = 3; image_attempts = 0;
    assert(!size_for((VkCommandBuffer)1, reserved_fence, 640, 360, 640, 360));
    assert(allocations == frees && !D.sizes[0].out && !D.sizes[0].w);
    fail_alloc = 0;
    struct Size *entry = size_for((VkCommandBuffer)1, reserved_fence, 640, 360, 640, 360);
    assert(entry && entry->w == 640);
    free_size(entry);
    assert(allocations == frees);
    fail_view = 1;
    assert(!size_for((VkCommandBuffer)1, reserved_fence, 640, 360, 640, 360));
    assert(allocations == frees && vk_allocations == vk_frees);
    free(P.t[0]); free(P.t[1]); free(P.bytes[0]); free(P.bytes[1]);
    return 0;
}
'''

# Bodies use names from project/Vulkan declarations; every linked dependency gets its exact C signature.
BODIES = {
    "sr_method": "return setting;",
    "sr_mode": "return quality_setting;",
    "vkDeviceWaitIdle": "idle_calls++; return VK_SUCCESS;",
    "vk_ctx": "return &context;",
    "vk_fsr3_available": "return fsr_available;",
    "vk_dlss_available": "return dlss_available;",
    "vk_exec_recording_fence": "return reserved_fence;",
    "vk_sync_fence_done": "return completed_fence;",
    "rec_drain": "drains++;",
    "vk_exec_draw_new_cb": "invalidations++;",
    "vk_fsr3_dispatch": "fsr_calls++; dispatched = *frame; assert(drains); return dispatch_result;",
    "vk_dlss_dispatch": "dlss_calls++; *feature = (void *)1; dispatched = *frame; assert(drains); return dispatch_result;",
    "vk_dlss_free": "assert(drains); feature_frees++;",
    "vk_completion_add": "assert(pending_count < 64); pending[pending_count].fence = fence; pending[pending_count].fn = fn; pending[pending_count++].arg = arg;",
    "vk_sync_fence_wait": "complete(value); return 1;",
    "vk_fsr3_shutdown": "assert(!pending_count); shutdowns++;",
    "vk_dlss_shutdown": "assert(!pending_count); shutdowns++;",
    "vk_usnap_bytes": "return have_camera ? (const uint8_t *)camera + (addr - 0x10000) : NULL;",
    "vk_usnap_find": "return have_camera;",
    "vk_exec_target_view": "return (VkImageView)10;",
    "rt_knob": "return atoi(def);",
    "cfg_get": "return NULL;",
    "cfg_int": "return 0;",
    "cfg_bool": "return strcmp(name, \"TOTK_DLAA\") != 0;",
    "cfg_version": "return 1;",
    "res_dlss_lod_bias": "return -0.5f;",
    "res_dlss_mode": "snprintf(applied_mode, sizeof applied_mode, \"%s\", mode); return 1;",
    "vk_sampler_dlss_bias": "sampler_bias = bias;",
    "res_sizes": "out[0] = scene[0] = 1280; out[1] = scene[1] = 720;",
    "rc_vkCmdBlitImage": "blits++;",
    "rc_vkCmdClearColorImage": "clears++;",
    "vmaCreateImage": "if (++image_attempts == fail_alloc) return VK_ERROR_OUT_OF_DEVICE_MEMORY; *pImage = (VkImage)++next_handle; *pAllocation = (VmaAllocation)1; allocations++; return VK_SUCCESS;",
    "vmaDestroyImage": "frees++;",
    "vmaCreateBuffer": "*pBuffer = (VkBuffer)++next_handle; *pAllocation = (VmaAllocation)1; if (pAllocationInfo) pAllocationInfo->pMappedData = mapped; allocations++; return VK_SUCCESS;",
    "vmaDestroyBuffer": "frees++;",
}


@unittest.skipUnless(CC, "requires Clang and Vulkan development headers")
class TemporalFlow(unittest.TestCase):
    def test_real_shared_flow(self):
        with tempfile.TemporaryDirectory(prefix="temporal-flow-") as tmp:
            tmp = Path(tmp)
            obj = tmp / "renderer.o"
            includes = ["-I" + str(ROOT / p) for p in ("runtime", "include", "toolchain/vma")]
            flags = [CC, "-std=gnu11", *includes]
            source = ROOT / "runtime/vk_temporal.c"
            subprocess.run([*flags, "-c", str(source), "-o", str(obj)], check=True)
            undefined = {line.split()[-1] for line in subprocess.check_output(["nm", "-u", str(obj)], text=True).splitlines()}
            ast = json.loads(subprocess.check_output([*flags, "-Xclang", "-ast-dump=json", "-fsyntax-only", str(source)]))
            declarations = {}

            def visit(node):
                if node.get("kind") == "FunctionDecl" and node.get("name") in undefined:
                    declarations[node["name"]] = node
                for child in node.get("inner", []):
                    visit(child)

            visit(ast)
            mocks = []
            for name, node in sorted(declarations.items()):
                # libc/math calls remain real; only runtime, Vulkan and allocator calls are mocked.
                if name not in BODIES and not name.startswith(("vk", "rc_", "vma", "rec_", "gfx_", "rt_", "kern_", "plat_")):
                    continue
                ret = node["type"]["qualType"].split("(")[0].strip()
                params = [p for p in node.get("inner", []) if p.get("kind") == "ParmVarDecl"]
                signature = ", ".join(
                    p["type"]["qualType"].replace("(*)", "(*" + p["name"] + ")")
                    if "(*)" in p["type"]["qualType"] else p["type"]["qualType"] + " " + p["name"]
                    for p in params
                )
                if node.get("variadic"):
                    signature += ", ..."
                body = BODIES.get(name)
                if body is None and name.startswith("vkCreate"):
                    output = params[-1]
                    handle = output["type"]["qualType"].removesuffix(" *")
                    body = f'*{output["name"]} = ({handle})++next_handle; vk_allocations++; return VK_SUCCESS;'
                    if name == "vkCreateImageView":
                        body = "if (fail_view) return VK_ERROR_OUT_OF_DEVICE_MEMORY; " + body
                if body is None and name.startswith("vkDestroy"):
                    body = "vk_frees++;"
                if body is None:
                    body = "" if ret == "void" else f"return ({ret})0;"
                mocks.append(f"{ret} {name}({signature or 'void'}) {{ {body} }}")
            harness = tmp / "flow.c"
            harness.write_text(HARNESS + "\n" + "\n".join(mocks) + "\n__typeof__(gfx_counters) gfx_counters;\n")
            binary = tmp / "flow"
            subprocess.run([*flags, "-fsanitize=address,undefined", str(harness), "-lm", "-o", str(binary)], check=True)
            for method in (0, 1, 2):
                subprocess.run([str(binary), str(method)], check=True, env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1"})


if __name__ == "__main__":
    unittest.main()
