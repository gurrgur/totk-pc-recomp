// The FSR 3.1 upscaler's shader blobs for the vendored AMD FidelityFX SDK (runtime/third_party/ffx).
//
// The SDK's Vulkan backend asks its interface's fpGetPermutationBlobByIndex (ffx_shader_blobs.h) for the SPIR-V module
// of a pass and permutation and builds the pipeline's descriptor set layout from the returned FfxShaderBlob's binding
// numbers, counts and names, so that reflection comes from the generated table
// (third_party/ffx/ffx_fsr3upscaler_spv.h, tools/build_ffx_shaders.py) rather than from a shader compiler or a
// reflection library at run time. Upstream fills the table from blob_accessors/, which the SDK's CMake generates;
// this repository generates it in build_ffx_shaders.py, which also keeps it in step with the vendored shaders.
//
// The table's permutation key is Fs3UpscalerShaderPermutationOptions (the SDK component's own flags, in
// sdk/src/components/fsr3upscaler/ffx_fsr3upscaler_private.h) limited to the bits the vendored shaders encode:
// HDR colour input, depth inverted, sharpening. LOW_RESOLUTION_MOTION_VECTORS is compiled on and
// JITTERED_MOTION_VECTORS off: the shared path provides unjittered render-pixel motion.
// USE_LANCZOS_TYPE, FORCE_WAVE64 and ALLOW_FP16 are not part of a key: build_ffx_shaders.py compiles one variant of
// every pass for all three, the approximate/reference Lanczos samplers of ffx_fsr3upscaler_upsample.h and
// ffx_fsr3upscaler_reproject.h, 32 lanes and FP32, so a request differing only in them gets the same module.
#include <string.h>

#include <FidelityFX/host/ffx_error.h>
#include <FidelityFX/host/ffx_fsr3upscaler.h>
#include <ffx_shader_blobs.h>

extern "C" {
#include "host.h"   // rt_logf
}
#include "third_party/ffx/ffx_fsr3upscaler_spv.h"

// The permutation bits a table key holds, Fs3UpscalerShaderPermutationOptions:
// HDR_COLOR_INPUT | LOW_RES_MOTION_VECTORS | JITTER_MOTION_VECTORS | DEPTH_INVERTED | ENABLE_SHARPENING.
#define FFX_PERMUTATION_KEY_MASK 0x3eu

static const FfxFsr3UpscalerSpvBlob *find_blob(uint32_t effect, FfxFsr3UpscalerPass pass, uint32_t permutation) {
    uint32_t key = permutation & FFX_PERMUTATION_KEY_MASK;
    for (size_t i = 0; i < sizeof ffx_fsr3upscaler_spv_blobs / sizeof ffx_fsr3upscaler_spv_blobs[0]; i++) {
        const FfxFsr3UpscalerSpvBlob *b = &ffx_fsr3upscaler_spv_blobs[i];
        if (b->pass == (uint32_t)pass && b->permutation == key) return b;
    }
    rt_logf("ffx: no shader blob for %s pass %d, permutation %#x (key %#x); tools/build_ffx_shaders.py generates the "
            "table, and the context's flags must match the permutations it compiles\n",
            effect == FFX_EFFECT_FSR3UPSCALER ? "FSR3 upscaler" : "unknown", (int)pass, permutation, key);
    return NULL;
}

// The SDK declares the fsr3Upscaler* pair with C linkage in blob_accessors/ffx_fsr3upscaler_shaderblobs.h, which this
// tree does not vendor; ffx_shader_blobs.h does the same for ffxGetPermutationBlobByIndex and ffxIsWave64.
extern "C" FfxErrorCode fsr3UpscalerGetPermutationBlobByIndex(FfxFsr3UpscalerPass passId, uint32_t permutationOptions,
                                                             FfxShaderBlob *outBlob) {
    const FfxFsr3UpscalerSpvBlob *b = find_blob(FFX_EFFECT_FSR3UPSCALER, passId, permutationOptions);
    if (!b || !outBlob) return FFX_ERROR_INVALID_ARGUMENT;
    const FfxFsr3UpscalerSpvModule *m = b->module;
    // A local FfxShaderBlob and memcpy, like the SDK's own accessors: its members are const.
    const FfxShaderBlob blob = {
        .data = (const uint8_t *)m->data,
        .size = m->size,
        .cbvCount = m->cbvCount,
        .srvTextureCount = m->srvTextureCount,
        .uavTextureCount = m->uavTextureCount,
        .srvBufferCount = m->srvBufferCount,
        .uavBufferCount = m->uavBufferCount,
        .samplerCount = 0,   // CreatePipelineVK binds its two samplers at SAMPLER_BINDING_SHIFT + index itself
        .rtAccelStructCount = 0,
        .boundConstantBufferNames = (const char **)m->cbvNames,
        .boundConstantBuffers = m->cbvBindings,
        .boundConstantBufferCounts = m->cbvCounts,
        .boundConstantBufferSpaces = m->cbvSpaces,
        .boundSRVTextureNames = (const char **)m->srvTextureNames,
        .boundSRVTextures = m->srvTextureBindings,
        .boundSRVTextureCounts = m->srvTextureCounts,
        .boundSRVTextureSpaces = m->srvTextureSpaces,
        .boundUAVTextureNames = (const char **)m->uavTextureNames,
        .boundUAVTextures = m->uavTextureBindings,
        .boundUAVTextureCounts = m->uavTextureCounts,
        .boundUAVTextureSpaces = m->uavTextureSpaces,
        .boundSRVBufferNames = (const char **)m->srvBufferNames,
        .boundSRVBuffers = m->srvBufferBindings,
        .boundSRVBufferCounts = m->srvBufferCounts,
        .boundSRVBufferSpaces = m->srvBufferSpaces,
        .boundUAVBufferNames = (const char **)m->uavBufferNames,
        .boundUAVBuffers = m->uavBufferBindings,
        .boundUAVBufferCounts = m->uavBufferCounts,
        .boundUAVBufferSpaces = m->uavBufferSpaces,
    };
    memcpy(outBlob, &blob, sizeof blob);
    return FFX_OK;
}

FfxErrorCode ffxGetPermutationBlobByIndex(FfxEffect effectId, FfxPass passId, FfxBindStage bindStage,
                                          uint32_t permutationOptions, FfxShaderBlob *outBlob) {
    (void)bindStage;   // every vendored pass is a compute shader
    if (effectId != FFX_EFFECT_FSR3UPSCALER) {
        rt_logf("ffx: no effect %d in this tree; only FSR3 upscaling is vendored\n", (int)effectId);
        return FFX_ERROR_INVALID_ENUM;
    }
    return fsr3UpscalerGetPermutationBlobByIndex((FfxFsr3UpscalerPass)passId, permutationOptions, outBlob);
}

// No permutation asks for wave64: the table holds one 32-lane module per pass, and false leaves CreatePipelineVK's
// requiredSubgroupSize unset.
extern "C" FfxErrorCode fsr3UpscalerIsWave64(uint32_t permutationOptions, bool &isWave64) {
    (void)permutationOptions;
    isWave64 = false;
    return FFX_OK;
}

FfxErrorCode ffxIsWave64(FfxEffect effectId, uint32_t permutationOptions, bool &isWave64) {
    (void)effectId;
    return fsr3UpscalerIsWave64(permutationOptions, isWave64);
}

// CreateBackendContextVK stores this in the interface's fpSwapChainConfigureFrameGeneration. Setting it up needs the
// frame interpolation swapchain (sdk/src/backends/vk/FrameInterpolationSwapchain), whose sources include <Windows.h>
// and dwmapi.h and are not vendored, and upscaling alone never calls it.
extern "C" FFX_API FfxErrorCode ffxSetFrameGenerationConfigToSwapchainVK(const FfxFrameGenerationConfig *config) {
    (void)config;
    rt_logf("ffx: frame generation is not part of this build; only FSR3 upscaling is vendored\n");
    return FFX_ERROR_INCOMPLETE_INTERFACE;
}
