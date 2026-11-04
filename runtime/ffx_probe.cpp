// totk --probe-ffx: FSR 3.1 upscaling on this machine's Vulkan device through the vendored AMD FidelityFX SDK
// (runtime/third_party/ffx). Needs no game files.
//
// It creates the normal device (vk_init), makes a FfxFsr3UpscalerContext over the vendored Vulkan backend, allocates
// the three shared resources ffxFsr3UpscalerGetSharedResourceDescriptions asks the application for, and renders
// 960x540 colour, synthetic R32 depth and unjittered render-pixel motion, plus a 1x1 exposure into a
// 1920x1080 output. The same dynamic context tests Native AA and smaller render extents. Four dispatches with a jitter
// sequence follow; the output is copied back and checked to be finite and not constant.
//
// What the frame integration has to repeat is visible here: the context flags that select the shader permutation, the
// dispatch fields, the resource states handed to ffxGetResourceVK (they are the layouts the backend's barriers start
// from), the image layout the backend records in, and one submission per dispatch.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <FidelityFX/host/ffx_fsr3upscaler.h>
#include <FidelityFX/host/backends/vk/ffx_vk.h>

extern "C" {
#include "host.h"
#include "platform/platform.h"
#include "vk.h"
}

enum { RENDER_W = 960, RENDER_H = 540, OUTPUT_W = 1920, OUTPUT_H = 1080, FRAMES = 4 };
// The images the probe owns.
enum { IMG_COLOUR, IMG_DEPTH, IMG_MOTION, IMG_EXPOSURE, IMG_OUTPUT, IMG_DILATED_DEPTH, IMG_DILATED_MOTION,
       IMG_PREV_DEPTH, IMG_COUNT };

// The jitter sequence of an FSR example integration, in render-resolution pixels.
static const float kJitter[FRAMES][2] = {{0.0f, 0.0f}, {0.5f, -0.5f}, {-0.5f, 0.5f}, {0.25f, 0.25f}};

struct Probe {
    VkCtx *c;
    FfxFsr3UpscalerContext *context;
    int contextValid;   // failed SDK creation cannot be passed to its public destroy API
    void *scratch;
    size_t scratchSize;
    VkBuffer staging, readback;
    VmaAllocation stagingAlloc, readbackAlloc;
    VkCommandPool pool;
    VkCommandBuffer cb;
    VkFence fence;
    struct {
        VkImage image;
        VmaAllocation alloc;
        VkImageCreateInfo createInfo;
        FfxResourceDescription desc;
    } images[IMG_COUNT];
};

static VkFormat vk_format_of(FfxSurfaceFormat format) {
    switch (format) {
        case FFX_SURFACE_FORMAT_R32G32B32A32_FLOAT: return VK_FORMAT_R32G32B32A32_SFLOAT;
        case FFX_SURFACE_FORMAT_R16G16B16A16_FLOAT: return VK_FORMAT_R16G16B16A16_SFLOAT;
        case FFX_SURFACE_FORMAT_R32_FLOAT: return VK_FORMAT_R32_SFLOAT;
        case FFX_SURFACE_FORMAT_R16G16_FLOAT: return VK_FORMAT_R16G16_SFLOAT;
        case FFX_SURFACE_FORMAT_R32_UINT: return VK_FORMAT_R32_UINT;
        default: return VK_FORMAT_UNDEFINED;
    }
}

static int make_image(Probe *p, int id, FfxSurfaceFormat format, uint32_t w, uint32_t h, VkImageUsageFlags usage) {
    VkImageCreateInfo ci = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = vk_format_of(format);
    ci.extent = {w, h, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_OPTIMAL;
    ci.usage = usage | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    ci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (ci.format == VK_FORMAT_UNDEFINED) {
        rt_logf("probe-ffx: no VkFormat for surface format %d\n", (int)format);
        return 0;
    }
    VmaAllocationCreateInfo aci = {};
    aci.usage = VMA_MEMORY_USAGE_AUTO;
    if (vmaCreateImage(p->c->vma, &ci, &aci, &p->images[id].image, &p->images[id].alloc, nullptr) != VK_SUCCESS) {
        rt_logf("probe-ffx: vmaCreateImage failed for a %ux%u format %d image\n", w, h, (int)format);
        return 0;
    }
    p->images[id].createInfo = ci;
    // The description tells the backend a resource's format, size and image aspect, and that it can be a UAV.
    p->images[id].desc = ffxGetImageResourceDescriptionVK(p->images[id].image, ci, FFX_RESOURCE_USAGE_READ_ONLY);
    return 1;
}

// Every image is left in VK_IMAGE_LAYOUT_GENERAL: that is what the backend's own barriers use for both the states the
// probe declares (GENERIC_READ for the inputs, UNORDERED_ACCESS for the outputs and shared resources).
static void image_barrier(VkCommandBuffer cb, VkImage image, VkImageAspectFlags aspect, VkImageLayout old,
                          VkImageLayout new_, VkAccessFlags src, VkAccessFlags dst) {
    VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = src;
    b.dstAccessMask = dst;
    b.oldLayout = old;
    b.newLayout = new_;
    b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = image;
    b.subresourceRange = {aspect, 0, 1, 0, 1};
    vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                         VK_DEPENDENCY_BY_REGION_BIT, 0, nullptr, 0, nullptr, 1, &b);
}

static float half_to_float(uint16_t h) {
    uint32_t sign = (uint32_t)(h >> 15) << 31, exp = (h >> 10) & 0x1f, man = h & 0x3ff, bits;
    if (exp == 0) {
        if (!man) return 0.0f * (sign ? -1.0f : 1.0f);
        exp = 127 - 15 + 1;
        while (!(man & 0x400)) man <<= 1, exp--;
        bits = sign | (exp << 23) | ((man & 0x3ff) << 13);
    } else if (exp == 0x1f) {
        bits = sign | 0x7f800000u | (man << 13);   // an infinity or a NaN
    } else {
        bits = sign | ((exp + 127 - 15) << 23) | (man << 13);
    }
    float f;
    memcpy(&f, &bits, sizeof f);
    return f;
}

// A float in (0, 1) as an IEEE half; the gradient needs nothing else.
static uint16_t float_to_half(float v) {
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    return (uint16_t)(((bits >> 16) & 0x8000) | ((((bits >> 23) & 0xff) - 127 + 15) << 10) | ((bits >> 13) & 0x3ff));
}

// A gradient in RGBA16F: a constant colour would make the output constant as well, which the check rejects.
static uint16_t *gradient_pixels(void) {
    uint16_t *p = (uint16_t *)malloc((size_t)RENDER_W * RENDER_H * 4 * sizeof *p);
    if (!p) return NULL;
    for (uint32_t y = 0; y < RENDER_H; y++) {
        for (uint32_t x = 0; x < RENDER_W; x++) {
            uint16_t *texel = &p[((size_t)y * RENDER_W + x) * 4];
            texel[0] = float_to_half(0.1f + 0.8f * x / (float)RENDER_W);
            texel[1] = float_to_half(0.1f + 0.8f * y / (float)RENDER_H);
            texel[2] = float_to_half(0.5f);
            texel[3] = float_to_half(1.0f);
        }
    }
    return p;
}

static int create_pool(Probe *p) {
    VkCommandPoolCreateInfo pci = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = p->c->gfx_family;
    if (vkCreateCommandPool(p->c->device, &pci, NULL, &p->pool) != VK_SUCCESS) return 0;
    VkCommandBufferAllocateInfo cbi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbi.commandPool = p->pool;
    cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbi.commandBufferCount = 1;
    if (vkAllocateCommandBuffers(p->c->device, &cbi, &p->cb) != VK_SUCCESS) return 0;
    VkFenceCreateInfo fci = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    return vkCreateFence(p->c->device, &fci, NULL, &p->fence) == VK_SUCCESS;
}

static int submit_and_wait(Probe *p) {
    VkSubmitInfo submit = {VK_STRUCTURE_TYPE_SUBMIT_INFO};
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &p->cb;
    if (vkQueueSubmit(p->c->gfx_queue, 1, &submit, p->fence) != VK_SUCCESS ||
        vkWaitForFences(p->c->device, 1, &p->fence, VK_TRUE, UINT64_MAX) != VK_SUCCESS)
        return 0;
    return vkResetFences(p->c->device, 1, &p->fence) == VK_SUCCESS;
}

// Transitions every image out of UNDEFINED into GENERAL and fills the inputs. The fills happen before the transition
// out of UNDEFINED is complete in no other submission, so both are recorded here.
static int setup_images(Probe *p) {
    static const VkImageAspectFlags aspects[IMG_COUNT] = {
        VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_ASPECT_COLOR_BIT,
        VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_ASPECT_COLOR_BIT};
    VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (vkBeginCommandBuffer(p->cb, &begin) != VK_SUCCESS) return 0;
    for (int i = 0; i < IMG_COUNT; i++)
        image_barrier(p->cb, p->images[i].image, aspects[i], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                      VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT);
    VkBufferImageCopy region = {};
    region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    region.imageExtent = {RENDER_W, RENDER_H, 1};
    vkCmdCopyBufferToImage(p->cb, p->staging, p->images[IMG_COLOUR].image, VK_IMAGE_LAYOUT_GENERAL, 1, &region);
    VkClearColorValue black = {{0.0f, 0.0f, 0.0f, 0.0f}}, one = {{1.0f, 1.0f, 1.0f, 1.0f}};
    VkImageSubresourceRange colourRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    vkCmdClearColorImage(p->cb, p->images[IMG_MOTION].image, VK_IMAGE_LAYOUT_GENERAL, &black, 1, &colourRange);
    vkCmdClearColorImage(p->cb, p->images[IMG_EXPOSURE].image, VK_IMAGE_LAYOUT_GENERAL, &one, 1, &colourRange);
    // The synthetic depth input is R32_SFLOAT, not a depth/stencil image.
    VkClearColorValue mid = {{0.5f, 0.0f, 0.0f, 0.0f}};
    vkCmdClearColorImage(p->cb, p->images[IMG_DEPTH].image, VK_IMAGE_LAYOUT_GENERAL, &mid, 1, &colourRange);
    if (vkEndCommandBuffer(p->cb) != VK_SUCCESS) return 0;
    return submit_and_wait(p);
}

static FfxResource resource_of(Probe *p, int id, FfxResourceStates state) {
    return ffxGetResourceVK((void *)p->images[id].image, p->images[id].desc, NULL, state);
}

static int dispatch_frames(Probe *p, double *totalMs) {
    VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    for (int frame = 0; frame < FRAMES; frame++) {
        if (vkResetCommandBuffer(p->cb, 0) != VK_SUCCESS || vkBeginCommandBuffer(p->cb, &begin) != VK_SUCCESS)
            return 0;
        // Inputs are read through the compute stage, outputs and the shared resources are read and written.
        FfxFsr3UpscalerDispatchDescription d = {};
        d.commandList = ffxGetCommandListVK(p->cb);
        d.color = resource_of(p, IMG_COLOUR, FFX_RESOURCE_STATE_GENERIC_READ);
        d.depth = resource_of(p, IMG_DEPTH, FFX_RESOURCE_STATE_GENERIC_READ);
        d.motionVectors = resource_of(p, IMG_MOTION, FFX_RESOURCE_STATE_GENERIC_READ);
        d.exposure = resource_of(p, IMG_EXPOSURE, FFX_RESOURCE_STATE_GENERIC_READ);
        d.dilatedDepth = resource_of(p, IMG_DILATED_DEPTH, FFX_RESOURCE_STATE_UNORDERED_ACCESS);
        d.dilatedMotionVectors = resource_of(p, IMG_DILATED_MOTION, FFX_RESOURCE_STATE_UNORDERED_ACCESS);
        d.reconstructedPrevNearestDepth = resource_of(p, IMG_PREV_DEPTH, FFX_RESOURCE_STATE_UNORDERED_ACCESS);
        d.output = resource_of(p, IMG_OUTPUT, FFX_RESOURCE_STATE_UNORDERED_ACCESS);
        d.jitterOffset = {kJitter[frame][0], kJitter[frame][1]};
        d.motionVectorScale = {1.0f, 1.0f};
        d.renderSize = frame == 1 ? FfxDimensions2D{720, 405} : FfxDimensions2D{RENDER_W, RENDER_H};
        d.upscaleSize = frame == 0 ? FfxDimensions2D{RENDER_W, RENDER_H} : FfxDimensions2D{OUTPUT_W, OUTPUT_H};
        d.enableSharpening = true;
        d.sharpness = 0.2f;
        d.frameTimeDelta = 1000.0f / 60.0f;
        d.preExposure = 1.0f;
        d.reset = frame == 0 || frame == 2;   // first frame and simulated camera cut
        d.cameraNear = 1000.0f;   // SDK inverted-depth convention
        d.cameraFar = 0.1f;
        d.cameraFovAngleVertical = 1.0f;
        d.viewSpaceToMetersFactor = 1.0f;
        uint64_t t0 = plat_now_ns();
        FfxErrorCode e = ffxFsr3UpscalerContextDispatch(p->context, &d);
        double recordMs = (double)(plat_now_ns() - t0) / 1e6;
        if (e != FFX_OK) {
            rt_logf("probe-ffx: ffxFsr3UpscalerContextDispatch frame %d failed (%#x)\n", frame, (unsigned)e);
            return 0;
        }
        if (frame == FRAMES - 1) {
            image_barrier(p->cb, p->images[IMG_OUTPUT].image, VK_IMAGE_ASPECT_COLOR_BIT, VK_IMAGE_LAYOUT_GENERAL,
                          VK_IMAGE_LAYOUT_GENERAL, VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
            VkBufferImageCopy copy = {};
            copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
            copy.imageExtent = {OUTPUT_W, OUTPUT_H, 1};
            vkCmdCopyImageToBuffer(p->cb, p->images[IMG_OUTPUT].image, VK_IMAGE_LAYOUT_GENERAL, p->readback, 1, &copy);
        }
        if (vkEndCommandBuffer(p->cb) != VK_SUCCESS || !submit_and_wait(p)) {
            rt_logf("probe-ffx: frame %d submission failed\n", frame);
            return 0;
        }
        double ms = (double)(plat_now_ns() - t0) / 1e6;
        *totalMs += ms;
        printf("probe-ffx: frame %d jitter (%.2f, %.2f): dispatch recorded in %.2f ms, submitted and done in %.2f ms\n",
               frame, kJitter[frame][0], kJitter[frame][1], recordMs, ms);
    }
    return 1;
}

static int check_output(Probe *p) {
    VmaAllocationInfo readbackInfo = {};
    vmaGetAllocationInfo(p->c->vma, p->readbackAlloc, &readbackInfo);
    const uint16_t *px = (const uint16_t *)readbackInfo.pMappedData;
    size_t values = (size_t)OUTPUT_W * OUTPUT_H * 4;
    float lo = INFINITY, hi = -INFINITY;
    double sum = 0;
    int finite = 1, constant = 1;
    for (size_t i = 0; i < values; i++) {
        float v = half_to_float(px[i]);
        if (!isfinite(v)) finite = 0;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
        sum += v;
        if (px[i] != px[0]) constant = 0;
    }
    printf("probe-ffx: output texel 0 %#06x -> %.4f, range %.4f..%.4f, mean %.4f, finite %d, non-constant %d\n",
           (unsigned)px[0], half_to_float(px[0]), lo, hi, sum / (double)values, finite, !constant);
    if (!finite || constant) {
        rt_logf("probe-ffx: the output image is %s\n", !finite ? "not finite" : "constant");
        return 0;
    }
    return 1;
}

// Everything that needs the device. The caller cleans up whatever was created, so a failure is one early return.
static int probe_run(Probe *pp) {
    Probe &p = *pp;
    double totalMs = 0;
    // ---- the FSR3.1 context: scratch memory, the Vulkan backend interface, the component.
    VkDeviceContext vkDeviceContext = {p.c->device, p.c->phys, vkGetDeviceProcAddr};
    p.scratchSize = ffxGetScratchMemorySizeVK(p.c->phys, FFX_FSR3UPSCALER_CONTEXT_COUNT);
    p.scratch = calloc(1, p.scratchSize);
    p.context = (FfxFsr3UpscalerContext *)calloc(1, sizeof *p.context);
    if (!p.scratch || !p.context) {
        rt_logf("probe-ffx: out of memory (scratch %zu bytes, context %zu bytes)\n", p.scratchSize,
                sizeof *p.context);
        return 0;
    }
    FfxInterface backendInterface = {};
    FfxErrorCode e = ffxGetInterfaceVK(&backendInterface, ffxGetDeviceVK(&vkDeviceContext), p.scratch,
                                      p.scratchSize, FFX_FSR3UPSCALER_CONTEXT_COUNT);
    if (e != FFX_OK) {
        rt_logf("probe-ffx: ffxGetInterfaceVK failed (%#x)\n", (unsigned)e);
        return 0;
    }
    FfxFsr3UpscalerContextDescription contextDescription = {};
    // Match the production backend: HDR, reversed depth, dynamic resolution, unjittered render-grid motion.
    contextDescription.flags = FFX_FSR3UPSCALER_ENABLE_HIGH_DYNAMIC_RANGE |
                               FFX_FSR3UPSCALER_ENABLE_DEPTH_INVERTED |
                               FFX_FSR3UPSCALER_ENABLE_DYNAMIC_RESOLUTION;
    contextDescription.maxRenderSize = {OUTPUT_W, OUTPUT_H};
    contextDescription.maxUpscaleSize = {OUTPUT_W, OUTPUT_H};
    contextDescription.backendInterface = backendInterface;
    if ((e = ffxFsr3UpscalerContextCreate(p.context, &contextDescription)) != FFX_OK) {
        rt_logf("probe-ffx: ffxFsr3UpscalerContextCreate failed (%#x)\n", (unsigned)e);
        return 0;
    }

    p.contextValid = 1;

    // ---- the three shared resources the component leaves to the application.
    FfxFsr3UpscalerSharedResourceDescriptions shared = {};
    if ((e = ffxFsr3UpscalerGetSharedResourceDescriptions(p.context, &shared)) != FFX_OK) {
        rt_logf("probe-ffx: ffxFsr3UpscalerGetSharedResourceDescriptions failed (%#x)\n", (unsigned)e);
        return 0;
    }
    const FfxResourceDescription *sharedDesc[3] = {&shared.dilatedDepth.resourceDescription,
                                                  &shared.dilatedMotionVectors.resourceDescription,
                                                  &shared.reconstructedPrevNearestDepth.resourceDescription};
    const char *sharedName[3] = {"dilated depth", "dilated motion vectors", "reconstructed previous nearest depth"};
    const int sharedId[3] = {IMG_DILATED_DEPTH, IMG_DILATED_MOTION, IMG_PREV_DEPTH};
    for (int i = 0; i < 3; i++)
        if (!make_image(&p, sharedId[i], sharedDesc[i]->format, sharedDesc[i]->width, sharedDesc[i]->height,
                        VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT))
            return 0;
    if (!make_image(&p, IMG_COLOUR, FFX_SURFACE_FORMAT_R16G16B16A16_FLOAT, RENDER_W, RENDER_H, VK_IMAGE_USAGE_SAMPLED_BIT) ||
        !make_image(&p, IMG_DEPTH, FFX_SURFACE_FORMAT_R32_FLOAT, RENDER_W, RENDER_H, VK_IMAGE_USAGE_SAMPLED_BIT) ||
        !make_image(&p, IMG_MOTION, FFX_SURFACE_FORMAT_R16G16_FLOAT, RENDER_W, RENDER_H, VK_IMAGE_USAGE_SAMPLED_BIT) ||
        !make_image(&p, IMG_EXPOSURE, FFX_SURFACE_FORMAT_R32_FLOAT, 1, 1, VK_IMAGE_USAGE_SAMPLED_BIT) ||
        !make_image(&p, IMG_OUTPUT, FFX_SURFACE_FORMAT_R16G16B16A16_FLOAT, OUTPUT_W, OUTPUT_H, VK_IMAGE_USAGE_STORAGE_BIT))
        return 0;

    // ---- the colour gradient, uploaded once, and the buffer the output is read back through.
    uint16_t *pixels = gradient_pixels();
    if (!pixels) return 0;
    VmaAllocationInfo allocInfo = {};
    VkBufferCreateInfo bufferInfo = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bufferInfo.size = (VkDeviceSize)RENDER_W * RENDER_H * 4 * sizeof(uint16_t);
    bufferInfo.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    VmaAllocationCreateInfo aci = {};
    aci.usage = VMA_MEMORY_USAGE_AUTO;
    aci.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
    VkResult vr = vmaCreateBuffer(p.c->vma, &bufferInfo, &aci, &p.staging, &p.stagingAlloc, &allocInfo);
    if (vr == VK_SUCCESS) memcpy(allocInfo.pMappedData, pixels, bufferInfo.size);
    free(pixels);
    if (vr != VK_SUCCESS) {
        rt_logf("probe-ffx: vmaCreateBuffer for the colour upload failed (%d)\n", (int)vr);
        return 0;
    }
    bufferInfo.size = (VkDeviceSize)OUTPUT_W * OUTPUT_H * 4 * sizeof(uint16_t);
    bufferInfo.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    aci.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
    if (vmaCreateBuffer(p.c->vma, &bufferInfo, &aci, &p.readback, &p.readbackAlloc, &allocInfo) != VK_SUCCESS) {
        rt_logf("probe-ffx: vmaCreateBuffer for the output readback failed\n");
        return 0;
    }

    if (!create_pool(&p) || !setup_images(&p) || !dispatch_frames(&p, &totalMs) || !check_output(&p)) return 0;

    printf("probe-ffx: device %u \"%s\", %dx%d -> %dx%d, scratch %zu KiB, context %zu KiB, %d frames in %.2f ms\n",
           p.c->device_index, p.c->props.deviceName, RENDER_W, RENDER_H, OUTPUT_W, OUTPUT_H, p.scratchSize >> 10,
           sizeof *p.context >> 10, FRAMES, totalMs);
    for (int i = 0; i < 3; i++)
        printf("probe-ffx: shared resource %-34s format %2d %ux%u\n", sharedName[i], (int)sharedDesc[i]->format,
               sharedDesc[i]->width, sharedDesc[i]->height);
    FfxEffectMemoryUsage vram = {};
    ffxFsr3UpscalerContextGetGpuMemoryUsage(p.context, &vram);
    printf("probe-ffx: context VRAM %llu KiB, aliasable %llu KiB\n",
           (unsigned long long)(vram.totalUsageInBytes >> 10),
           (unsigned long long)(vram.aliasableUsageInBytes >> 10));
    return 1;
}

int probe_ffx(void) {
    Probe p = {};
    if (!vk_init()) {
        printf("probe-ffx: FAILED (no Vulkan device)\n");
        return 1;
    }
    p.c = vk_ctx();
    int rc = probe_run(&p) ? 0 : 1;
    vkDeviceWaitIdle(p.c->device);
    if (p.contextValid) ffxFsr3UpscalerContextDestroy(p.context);
    free(p.context);
    free(p.scratch);
    if (p.staging) vmaDestroyBuffer(p.c->vma, p.staging, p.stagingAlloc);
    if (p.readback) vmaDestroyBuffer(p.c->vma, p.readback, p.readbackAlloc);
    for (int i = 0; i < IMG_COUNT; i++)
        if (p.images[i].image) vmaDestroyImage(p.c->vma, p.images[i].image, p.images[i].alloc);
    if (p.pool) vkDestroyCommandPool(p.c->device, p.pool, NULL);
    if (p.fence) vkDestroyFence(p.c->device, p.fence, NULL);
    printf("probe-ffx: %s\n", rc ? "FAILED" : "passed");
    vk_shutdown();
    return rc;
}
