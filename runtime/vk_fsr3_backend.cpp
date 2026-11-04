// FSR 3.1 consumes the shared path's GENERAL-layout HDR/depth and unjittered render-pixel motion.
#include <FidelityFX/host/ffx_fsr3upscaler.h>
#include <FidelityFX/host/backends/vk/ffx_vk.h>
#include <math.h>
#include <stdlib.h>
extern "C" {
#include "host.h"
#include "vk.h"
#include "vk_rec.h"
#include "vk_temporal_backend.h"
}

struct FsrContext {
    FfxFsr3UpscalerContext sdk;
    FfxInterface backend;
    VkDeviceContext device;
    void *scratch;
    uint32_t width, height, slot;
    uint64_t fences[FFX_MAX_QUEUED_FRAMES], last_fence;
    bool valid, initialized, reset;
    struct Shared {
        VkImage image;
        VmaAllocation allocation;
        FfxResourceDescription description;
    } shared[3];
    // Creation rollback: SDK ContextCreate does not unwind its successful allocations on failure.
    FfxCreateBackendContextFunc create_backend;
    FfxCreatePipelineFunc create_pipeline;
    FfxRegisterResourceFunc register_resource;
    FfxExecuteGpuJobsFunc execute_jobs;
    FfxErrorCode dispatch_error;
    bool backend_created;
    uint32_t effect_id, pipeline_count;
    FfxPipelineState *pipelines[FFX_FSR3UPSCALER_PASS_COUNT];
};
// Backend API is confined to one renderer thread and one dispatch at a time.
static FsrContext *active, *creating, *dispatching;
static bool failed;

static FfxErrorCode track_backend(FfxInterface *b, FfxEffect effect, FfxEffectBindlessConfig *config, FfxUInt32 *id) {
    FfxErrorCode e = creating->create_backend(b, effect, config, id);
    if (e == FFX_OK) creating->backend_created = true, creating->effect_id = *id;
    return e;
}
static FfxErrorCode track_pipeline(FfxInterface *b, FfxEffect effect, FfxPass pass, uint32_t permutation,
                                  const FfxPipelineDescription *desc, FfxUInt32 id, FfxPipelineState *out) {
    FfxErrorCode e = creating->create_pipeline(b, effect, pass, permutation, desc, id, out);
    if (e == FFX_OK) creating->pipelines[creating->pipeline_count++] = out;
    return e;
}

static FfxErrorCode track_register(FfxInterface *b, const FfxResource *resource, FfxUInt32 id,
                                   FfxResourceInternal *out) {
    FfxErrorCode e = dispatching->register_resource(b, resource, id, out);
    if (dispatching->dispatch_error == FFX_OK) dispatching->dispatch_error = e;
    return e;
}
static FfxErrorCode track_execute(FfxInterface *b, FfxCommandList commands, FfxUInt32 id) {
    // The SDK ignores registration errors and still schedules jobs and returns FFX_OK.
    if (dispatching->dispatch_error != FFX_OK) return dispatching->dispatch_error;
    return dispatching->dispatch_error = dispatching->execute_jobs(b, commands, id);
}

static void destroy_context(void *ptr) {
    FsrContext *c = (FsrContext *)ptr;
    if (c->valid) ffxFsr3UpscalerContextDestroy(&c->sdk);
    else if (c->backend_created) {
        for (uint32_t i = 0; i < c->pipeline_count; ++i)
            c->backend.fpDestroyPipeline(&c->backend, c->pipelines[i], c->effect_id);
        // DestroyResourceVK range-checks the index. Sweep this context's bounded static range, including
        // upload copies and partially created resources, before DestroyBackendContextVK's leak assertion.
        for (uint32_t i = 0; i < FFX_MAX_RESOURCE_COUNT; ++i) {
            FfxResourceInternal resource = {(int32_t)(c->effect_id * FFX_MAX_RESOURCE_COUNT + i)};
            c->backend.fpDestroyResource(&c->backend, resource, c->effect_id);
        }
        c->backend.fpDestroyBackendContext(&c->backend, c->effect_id);
    }
    for (auto &image : c->shared)
        if (image.image) vmaDestroyImage(vk_ctx()->vma, image.image, image.allocation);
    free(c->scratch);
    free(c);
}

static void retire(FsrContext *c) {
    if (!c) return;
    // The reserved fence includes recorded-but-unsubmitted SDK commands. DeviceWaitIdle cannot cover them.
    if (c->last_fence > vk_sync_fence_done()) vk_completion_add(c->last_fence, destroy_context, c);
    else destroy_context(c);
}

static VkFormat shared_format(FfxSurfaceFormat f) {
    switch (f) {
    case FFX_SURFACE_FORMAT_R32_FLOAT: return VK_FORMAT_R32_SFLOAT;
    case FFX_SURFACE_FORMAT_R16G16_FLOAT: return VK_FORMAT_R16G16_SFLOAT;
    case FFX_SURFACE_FORMAT_R32_UINT: return VK_FORMAT_R32_UINT;
    default: return VK_FORMAT_UNDEFINED;
    }
}

static FsrContext *create_context(uint32_t width, uint32_t height) {
    const VkCtx *v = vk_ctx();
    FsrContext *c = (FsrContext *)calloc(1, sizeof *c);
    if (!c) return nullptr;
    c->width = width, c->height = height, c->reset = true;
    c->device = {v->device, v->phys, vkGetDeviceProcAddr};
    size_t bytes = ffxGetScratchMemorySizeVK(v->phys, FFX_FSR3UPSCALER_CONTEXT_COUNT);
    c->scratch = calloc(1, bytes);
    if (!c->scratch || ffxGetInterfaceVK(&c->backend, ffxGetDeviceVK(&c->device), c->scratch, bytes,
                                        FFX_FSR3UPSCALER_CONTEXT_COUNT) != FFX_OK) {
        destroy_context(c);
        return nullptr;
    }
    c->create_backend = c->backend.fpCreateBackendContext;
    c->create_pipeline = c->backend.fpCreatePipeline;
    c->backend.fpCreateBackendContext = track_backend;
    c->backend.fpCreatePipeline = track_pipeline;
    c->register_resource = c->backend.fpRegisterResource;
    c->execute_jobs = c->backend.fpExecuteGpuJobs;
    c->backend.fpRegisterResource = track_register;
    c->backend.fpExecuteGpuJobs = track_execute;
    FfxFsr3UpscalerContextDescription desc = {};
    desc.flags = FFX_FSR3UPSCALER_ENABLE_HIGH_DYNAMIC_RANGE | FFX_FSR3UPSCALER_ENABLE_DEPTH_INVERTED |
                 FFX_FSR3UPSCALER_ENABLE_DYNAMIC_RESOLUTION;
    // No DISPLAY_RESOLUTION_MOTION_VECTORS or JITTER_CANCELLATION: upstream supplies render-grid unjittered motion.
    desc.maxRenderSize = desc.maxUpscaleSize = {width, height};
    desc.backendInterface = c->backend;
    creating = c;
    FfxErrorCode e = ffxFsr3UpscalerContextCreate(&c->sdk, &desc);
    creating = nullptr;
    if (e != FFX_OK) {
        rt_log("fsr3: context creation failed (%#x); original scene retained", (unsigned)e);
        destroy_context(c);
        return nullptr;
    }
    c->valid = true;
    FfxFsr3UpscalerSharedResourceDescriptions shared = {};
    if (ffxFsr3UpscalerGetSharedResourceDescriptions(&c->sdk, &shared) != FFX_OK) {
        destroy_context(c);
        return nullptr;
    }
    const FfxCreateResourceDescription *resources[] = {&shared.dilatedDepth, &shared.dilatedMotionVectors,
                                                      &shared.reconstructedPrevNearestDepth};
    for (int i = 0; i < 3; ++i) {
        auto &image = c->shared[i];
        image.description = resources[i]->resourceDescription;
        VkImageCreateInfo ci = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
        ci.imageType = VK_IMAGE_TYPE_2D;
        ci.format = shared_format(image.description.format);
        ci.extent = {image.description.width, image.description.height, 1};
        ci.mipLevels = ci.arrayLayers = 1;
        ci.samples = VK_SAMPLE_COUNT_1_BIT;
        ci.tiling = VK_IMAGE_TILING_OPTIMAL;
        ci.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        VmaAllocationCreateInfo ai = {};
        ai.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
        if (ci.format == VK_FORMAT_UNDEFINED ||
            vmaCreateImage(v->vma, &ci, &ai, &image.image, &image.allocation, nullptr) != VK_SUCCESS) {
            destroy_context(c);
            return nullptr;
        }
    }
    return c;
}

static FfxResource resource(VkImage image, VkFormat format, uint32_t width, uint32_t height, bool output = false) {
    if (!image) return {};
    VkImageCreateInfo ci = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = format;
    ci.extent = {width, height, 1};
    ci.mipLevels = ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_OPTIMAL;
    ci.usage = output ? VK_IMAGE_USAGE_STORAGE_BIT : VK_IMAGE_USAGE_SAMPLED_BIT;
    if (format == VK_FORMAT_D32_SFLOAT) ci.usage |= VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    auto desc = ffxGetImageResourceDescriptionVK(image, ci, output ? FFX_RESOURCE_USAGE_UAV : FFX_RESOURCE_USAGE_READ_ONLY);
    return ffxGetResourceVK((void *)image, desc, nullptr,
                            output ? FFX_RESOURCE_STATE_UNORDERED_ACCESS : FFX_RESOURCE_STATE_GENERIC_READ);
}

int vk_fsr3_available(void) { return !failed && vk_ctx() && vk_ctx()->device != VK_NULL_HANDLE; }

int vk_fsr3_dispatch(const VkTemporalDispatch *f) {
    if (failed) return -1;
    if (!vk_fsr3_available()) return 0;
    // Reject before recording anything; caller keeps the original scene, including on SDK failure.
    if (!f || !f->cb || !f->recording_fence || !f->width || !f->height || !f->output_width || !f->output_height ||
        f->width > f->output_width || f->height > f->output_height ||
        !f->color || !f->depth || !f->motion || !f->output || f->output == f->color ||
        f->depth_format != VK_FORMAT_D32_SFLOAT ||
        (f->color_format != VK_FORMAT_R16G16B16A16_SFLOAT && f->color_format != VK_FORMAT_B10G11R11_UFLOAT_PACK32) ||
        !isfinite(f->near_plane) || !isfinite(f->far_plane) || !(f->near_plane > 0) || !(f->far_plane > f->near_plane) ||
        !isfinite(f->vertical_fov) || !(f->vertical_fov > 0 && f->vertical_fov < 3.14159265f) ||
        !isfinite(f->delta_ms) || !(f->delta_ms > 0) || !isfinite(f->sharpness) ||
        f->sharpness < 0 || f->sharpness > 1 || !isfinite(f->jitter_x) || !isfinite(f->jitter_y)) return 0;
    if (!active || active->width != f->output_width || active->height != f->output_height) {
        FsrContext *next = create_context(f->output_width, f->output_height);
        if (!next) { failed = true; return -1; }
        retire(active);
        active = next;
    }
    FsrContext *c = active;
    // UnregisterResourcesVK destroys views in the NEXT slot immediately on the CPU, not when the GPU reaches it.
    uint32_t next = (c->slot + 1) % FFX_MAX_QUEUED_FRAMES;
    if (c->fences[next] > vk_sync_fence_done()) { c->reset = true; return 0; }
    rec_drain();
    if (!c->initialized) {
        VkImageMemoryBarrier barriers[3] = {};
        for (int i = 0; i < 3; ++i) {
            auto &b = barriers[i];
            b.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            b.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT;
            b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
            b.newLayout = VK_IMAGE_LAYOUT_GENERAL;
            b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            b.image = c->shared[i].image;
            b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        }
        vkCmdPipelineBarrier(f->cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             0, 0, nullptr, 0, nullptr, 3, barriers);
        c->initialized = true;
    }
    c->last_fence = f->recording_fence; // even a failed dispatch may have recorded commands
    c->fences[c->slot] = f->recording_fence;
    c->slot = next;
    FfxFsr3UpscalerDispatchDescription d = {};
    d.commandList = ffxGetCommandListVK(f->cb);
    d.color = resource(f->color, f->color_format, f->width, f->height);
    d.depth = resource(f->depth, f->depth_format, f->width, f->height);
    d.motionVectors = resource(f->motion, VK_FORMAT_R16G16_SFLOAT, f->width, f->height);
    d.reactive = resource(f->mask, VK_FORMAT_R8_UNORM, f->width, f->height);
    d.output = resource(f->output, VK_FORMAT_R16G16B16A16_SFLOAT, f->output_width, f->output_height, true);
    FfxResource *shared[] = {&d.dilatedDepth, &d.dilatedMotionVectors, &d.reconstructedPrevNearestDepth};
    for (int i = 0; i < 3; ++i)
        *shared[i] = ffxGetResourceVK((void *)c->shared[i].image, c->shared[i].description, nullptr,
                                     FFX_RESOURCE_STATE_UNORDERED_ACCESS);
    // SDK Accumulate samples low-resolution UV + Jitter()/RenderSize(): positive viewport right/down is positive here.
    d.jitterOffset = {f->jitter_x, f->jitter_y};
    d.motionVectorScale = {1, 1}; // SDK divides previous-minus-current pixel displacement by render size
    d.renderSize = {f->width, f->height};
    d.upscaleSize = {f->output_width, f->output_height};
    d.enableSharpening = f->sharpness > 0;
    d.sharpness = f->sharpness;
    d.frameTimeDelta = f->delta_ms;
    d.preExposure = 1;
    d.reset = f->reset || c->reset;
    // SDK inverted-depth convention swaps the physical planes; raw D32F is near=1, far=0.
    d.cameraNear = f->far_plane;
    d.cameraFar = f->near_plane;
    d.cameraFovAngleVertical = f->vertical_fov;
    d.viewSpaceToMetersFactor = 1;
    rec_drain(); // first-use barriers must reach Vulkan before the SDK records directly, even with recorder wrappers
    c->dispatch_error = FFX_OK;
    dispatching = c;
    FfxErrorCode e = ffxFsr3UpscalerContextDispatch(&c->sdk, &d);
    dispatching = nullptr;
    if (c->dispatch_error != FFX_OK) e = c->dispatch_error;
    if (e != FFX_OK) {
        rt_log("fsr3: dispatch failed (%#x); original scene retained", (unsigned)e);
        failed = true;
        return -1;
    }
    c->reset = false;
    return 1;
}

void vk_fsr3_shutdown(void) {
    // Caller has drained submissions and the recorder; completion owns any replaced contexts.
    retire(active);
    active = nullptr;
    failed = false;
}
