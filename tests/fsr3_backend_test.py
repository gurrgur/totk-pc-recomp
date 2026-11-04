"""FSR backend contract/lifetime checks; --gpu also builds/runs the isolated real-SDK Vulkan probe.

No game build or files. Test products stay in build/fsr3-backend-test.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/fsr3-backend-test'
SDK = ROOT / 'runtime/third_party/ffx/sdk'

UNIT = r'''
#include <assert.h>
#include <stdarg.h>
#include <vector>
#include "vk_fsr3_backend.cpp"
static VkCtx device;
static uint64_t done;
static int images, contexts, backends, pipelines, drains, calls, fail_create, fail_images, fail_dispatch;
static bool queued_barrier;
static int fail_register, fail_execute, registrations, executions, unregisters;
static FfxInterface sdk_interface;
static uintptr_t handle = 100;
static FfxFsr3UpscalerDispatchDescription last;
struct Pending { uint64_t fence; void (*fn)(void*); void *arg; };
static std::vector<Pending> pending;
extern "C" VkCtx *vk_ctx(void) { return &device; }
extern "C" void rt_log(const char *, ...) {}
extern "C" void rec_drain(void) { ++drains; queued_barrier=false; }
extern "C" uint64_t vk_sync_fence_done(void) { return done; }
extern "C" void vk_completion_add(uint64_t fence, void (*fn)(void*), void *arg) {
    if (fence <= done) fn(arg); else pending.push_back({fence,fn,arg});
}
static void complete(uint64_t fence) {
    done = fence;
    for (size_t i = 0; i < pending.size();) {
        if (pending[i].fence <= done) {
            auto p = pending[i]; pending.erase(pending.begin()+i); p.fn(p.arg);
        } else ++i;
    }
}
extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice, const char *) { return nullptr; }
extern "C" VKAPI_ATTR void VKAPI_CALL vkCmdPipelineBarrier(VkCommandBuffer, VkPipelineStageFlags, VkPipelineStageFlags,
    VkDependencyFlags, uint32_t, const VkMemoryBarrier*, uint32_t, const VkBufferMemoryBarrier*, uint32_t n,
    const VkImageMemoryBarrier *b) {
    assert(drains && n == 3); queued_barrier=true;
    for (uint32_t i=0;i<n;++i) assert(b[i].oldLayout==VK_IMAGE_LAYOUT_UNDEFINED && b[i].newLayout==VK_IMAGE_LAYOUT_GENERAL);
}
extern "C" VkResult vmaCreateImage(VmaAllocator, const VkImageCreateInfo*, const VmaAllocationCreateInfo*,
                                   VkImage *image, VmaAllocation *allocation, VmaAllocationInfo*) {
    if (fail_images) return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    *image=(VkImage)++handle; *allocation=(VmaAllocation)++handle; ++images; return VK_SUCCESS;
}
extern "C" void vmaDestroyImage(VmaAllocator, VkImage, VmaAllocation) { assert(images>0); --images; }
static FfxErrorCode make_backend(FfxInterface*, FfxEffect, FfxEffectBindlessConfig*, FfxUInt32 *id) {
    *id=7; ++backends; return FFX_OK;
}
static FfxErrorCode free_backend(FfxInterface*, FfxUInt32 id) { assert(id==7); --backends; return FFX_OK; }
static FfxErrorCode free_resource(FfxInterface*, FfxResourceInternal, FfxUInt32 id) { assert(id==7); return FFX_OK; }
static FfxErrorCode make_pipeline(FfxInterface*, FfxEffect, FfxPass, uint32_t, const FfxPipelineDescription*, FfxUInt32, FfxPipelineState*) {
    ++pipelines; return FFX_OK;
}
static FfxErrorCode free_pipeline(FfxInterface*, FfxPipelineState*, FfxUInt32) { --pipelines; return FFX_OK; }
static FfxErrorCode register_resource(FfxInterface*, const FfxResource*, FfxUInt32, FfxResourceInternal*) {
    ++registrations;
    return fail_register && registrations % 2 == 1 ? FFX_ERROR_OUT_OF_MEMORY : FFX_OK;
}
static FfxErrorCode execute_jobs(FfxInterface*, FfxCommandList, FfxUInt32) {
    ++executions; return fail_execute ? FFX_ERROR_BACKEND_API_ERROR : FFX_OK;
}
static FfxErrorCode unregister_resources(FfxInterface*, FfxCommandList, FfxUInt32) {
    ++unregisters; return FFX_OK;
}
size_t ffxGetScratchMemorySizeVK(VkPhysicalDevice, size_t) { return 128; }
FfxDevice ffxGetDeviceVK(VkDeviceContext *d) { return d; }
FfxCommandList ffxGetCommandListVK(VkCommandBuffer cb) { return cb; }
FfxErrorCode ffxGetInterfaceVK(FfxInterface *b,FfxDevice,void*,size_t,size_t) {
    b->fpCreateBackendContext=make_backend; b->fpDestroyBackendContext=free_backend; b->fpDestroyResource=free_resource;
    b->fpRegisterResource=register_resource; b->fpExecuteGpuJobs=execute_jobs; b->fpUnregisterResources=unregister_resources;
    b->fpCreatePipeline=make_pipeline; b->fpDestroyPipeline=free_pipeline; return FFX_OK;
}
FfxResourceDescription ffxGetImageResourceDescriptionVK(VkImage, const VkImageCreateInfo ci, FfxResourceUsage usage) {
    FfxResourceDescription d={}; d.width=ci.extent.width; d.height=ci.extent.height; d.usage=usage;
    if (ci.format==VK_FORMAT_D32_SFLOAT) d.format=FFX_SURFACE_FORMAT_R32_FLOAT;
    else if (ci.format==VK_FORMAT_R16G16_SFLOAT) d.format=FFX_SURFACE_FORMAT_R16G16_FLOAT;
    else if (ci.format==VK_FORMAT_R8_UNORM) d.format=FFX_SURFACE_FORMAT_R8_UNORM;
    else d.format=FFX_SURFACE_FORMAT_R16G16B16A16_FLOAT;
    return d;
}
FfxResource ffxGetResourceVK(void *im,FfxResourceDescription desc,const wchar_t*,FfxResourceStates state) {
    FfxResource r={}; r.resource=im; r.description=desc; r.state=state; return r;
}
FfxErrorCode ffxFsr3UpscalerContextCreate(FfxFsr3UpscalerContext*, const FfxFsr3UpscalerContextDescription *d) {
    assert(d->flags==(FFX_FSR3UPSCALER_ENABLE_HIGH_DYNAMIC_RANGE|FFX_FSR3UPSCALER_ENABLE_DEPTH_INVERTED|FFX_FSR3UPSCALER_ENABLE_DYNAMIC_RESOLUTION));
    assert(d->maxRenderSize.width==d->maxUpscaleSize.width && d->maxRenderSize.height==d->maxUpscaleSize.height);
    sdk_interface=d->backendInterface;
    FfxInterface b=d->backendInterface; FfxUInt32 id; FfxPipelineState p={};
    b.fpCreateBackendContext(&b,FFX_EFFECT_FSR3UPSCALER,nullptr,&id);
    b.fpCreatePipeline(&b,FFX_EFFECT_FSR3UPSCALER,0,0,nullptr,id,&p);
    if (fail_create) return FFX_ERROR_BACKEND_API_ERROR;
    ++contexts; return FFX_OK;
}
FfxErrorCode ffxFsr3UpscalerContextDestroy(FfxFsr3UpscalerContext*) {
    --contexts; --backends; --pipelines; return FFX_OK;
}
FfxErrorCode ffxFsr3UpscalerGetSharedResourceDescriptions(FfxFsr3UpscalerContext*,FfxFsr3UpscalerSharedResourceDescriptions *s) {
    FfxCreateResourceDescription *r[]={&s->dilatedDepth,&s->dilatedMotionVectors,&s->reconstructedPrevNearestDepth};
    FfxSurfaceFormat formats[]={FFX_SURFACE_FORMAT_R32_FLOAT,FFX_SURFACE_FORMAT_R16G16_FLOAT,FFX_SURFACE_FORMAT_R32_UINT};
    for (int i=0;i<3;++i) {r[i]->resourceDescription.format=formats[i];r[i]->resourceDescription.width=64;r[i]->resourceDescription.height=64;}
    return FFX_OK;
}
FfxErrorCode ffxFsr3UpscalerContextDispatch(FfxFsr3UpscalerContext*,const FfxFsr3UpscalerDispatchDescription *d) {
    assert(drains>calls && !queued_barrier); ++calls; last=*d;
    assert(d->cameraNear==1000 && d->cameraFar==0.1f);
    assert(d->motionVectorScale.x==1 && d->motionVectorScale.y==1);
    assert(d->jitterOffset.x==0.25f && d->jitterOffset.y==-0.125f);
    assert(d->depth.description.format==FFX_SURFACE_FORMAT_R32_FLOAT);
    assert(d->motionVectors.description.format==FFX_SURFACE_FORMAT_R16G16_FLOAT);
    assert(d->reactive.resource==nullptr || d->reactive.description.format==FFX_SURFACE_FORMAT_R8_UNORM);
    // Deliberately ignore callback errors, as the real SDK does.
    FfxResourceInternal resource={};
    sdk_interface.fpRegisterResource(&sdk_interface,&d->color,7,&resource);
    sdk_interface.fpRegisterResource(&sdk_interface,&d->depth,7,&resource);
    sdk_interface.fpExecuteGpuJobs(&sdk_interface,d->commandList,7);
    sdk_interface.fpExecuteGpuJobs(&sdk_interface,d->commandList,7);
    sdk_interface.fpUnregisterResources(&sdk_interface,d->commandList,7);
    return fail_dispatch ? FFX_ERROR_BACKEND_API_ERROR : FFX_OK;
}
static VkTemporalDispatch frame() {
    VkTemporalDispatch f={}; f.cb=(VkCommandBuffer)1; f.recording_fence=10;
    f.width=f.height=f.output_width=f.output_height=64;
    f.color=(VkImage)2;f.depth=(VkImage)3;f.motion=(VkImage)4;f.output=(VkImage)5;
    f.color_format=VK_FORMAT_R16G16B16A16_SFLOAT;f.depth_format=VK_FORMAT_D32_SFLOAT;
    f.near_plane=0.1f;f.far_plane=1000;f.vertical_fov=1;f.delta_ms=16;
    f.jitter_x=0.25f;f.jitter_y=-0.125f;return f;
}
int main() {
    device.device=(VkDevice)1;
    assert(vk_fsr3_available());
    auto f=frame(); f.recording_fence=0;
    assert(vk_fsr3_dispatch(&f)==0 && contexts==0); f=frame();
    f.near_plane=NAN; assert(vk_fsr3_dispatch(&f)==0 && calls==0); f=frame();
    f.width=65;assert(vk_fsr3_dispatch(&f)==0 && calls==0);f=frame();
    assert(vk_fsr3_dispatch(&f)==1 && last.reset && images==3);
    f.width=f.height=32;f.mask=(VkImage)6;
    assert(vk_fsr3_dispatch(&f)==1 && !last.reset && contexts==1 && last.renderSize.width==32);
    f.reset=1;assert(vk_fsr3_dispatch(&f)==1 && last.reset);
    // Fourth dispatch must not destroy views referenced by the same unsubmitted reserved fence.
    f.reset=0;assert(vk_fsr3_dispatch(&f)==0 && calls==3);
    f.output_width=f.output_height=128;
    assert(vk_fsr3_dispatch(&f)==1 && last.reset && contexts==2 && images==6 && pending.size()==1);
    // Replacement and shutdown also preserve recorded-but-unsubmitted references.
    vk_fsr3_shutdown();assert(contexts==2 && pending.size()==2);
    complete(10);assert(contexts==0 && images==0 && pending.empty());
    f=frame();f.recording_fence=11;
    assert(vk_fsr3_dispatch(&f)==1);
    assert(vk_fsr3_dispatch(&f)==1);assert(vk_fsr3_dispatch(&f)==1);
    assert(vk_fsr3_dispatch(&f)==0);
    complete(11);f.recording_fence=12;
    assert(vk_fsr3_dispatch(&f)==1 && last.reset);
    fail_dispatch=1;assert(vk_fsr3_dispatch(&f)==-1 && !vk_fsr3_available());
    vk_fsr3_shutdown();assert(contexts==1);complete(12);assert(!contexts && !images);
    fail_dispatch=0;fail_create=1;assert(vk_fsr3_dispatch(&f)==-1);
    assert(!contexts && !images && !backends && !pipelines);vk_fsr3_shutdown();
    fail_create=0;fail_images=1;assert(vk_fsr3_dispatch(&f)==-1);
    assert(!contexts && !images && !backends && !pipelines);vk_fsr3_shutdown();
    fail_images=0;fail_register=1;f.recording_fence=13;
    int executed=executions, unregistered=unregisters;
    assert(vk_fsr3_dispatch(&f)==-1 && !vk_fsr3_available());
    assert(active->dispatch_error==FFX_ERROR_OUT_OF_MEMORY && executions==executed && unregisters==unregistered+1);
    assert(vk_fsr3_dispatch(&f)==-1 && executions==executed);
    vk_fsr3_shutdown();assert(contexts==1 && pending.size()==1);
    complete(13);assert(!contexts && !images && !backends && !pipelines);
    fail_register=0;fail_execute=1;f.recording_fence=14;
    assert(vk_fsr3_dispatch(&f)==-1);vk_fsr3_shutdown();complete(14);
    fail_execute=0;f.recording_fence=15;
    assert(vk_fsr3_dispatch(&f)==1);vk_fsr3_shutdown();complete(15);
    assert(!contexts && !images && !backends && !pipelines);
    puts("FSR backend lifecycle, reserved-fence retirement, failure, extent and reset checks passed");
    puts("Ignored SDK registration/execution errors cannot report success; jobs blocked, unregister retained, restart succeeds");
}
'''


GPU = r'''
#define VK_REC_IMPL
#include <assert.h>
#include <stdarg.h>
#include <chrono>
#include <vector>
#include "ffx_probe.cpp"
static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL fault_proc(VkDevice, const char*);
#define vkGetDeviceProcAddr fault_proc
#include "vk_fsr3_backend.cpp"
#undef vkGetDeviceProcAddr
#include "third_party/ffx/sdk/src/components/fsr3upscaler/ffx_fsr3upscaler_private.h"
static VkCtx device;
static uint64_t completed;
struct Pending { uint64_t fence; void (*fn)(void*); void *arg; };
static std::vector<Pending> callbacks;
extern "C" VkCtx *vk_ctx(void) { return device.device ? &device : nullptr; }
extern "C" void rt_logf(const char *fmt, ...) { va_list a;va_start(a,fmt);vfprintf(stderr,fmt,a);va_end(a); }
extern "C" void rt_log(const char *fmt, ...) { va_list a;va_start(a,fmt);vfprintf(stderr,fmt,a);va_end(a);fputc('\n',stderr); }
extern "C" uint64_t plat_now_ns(void) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
extern "C" void rec_drain(void) {}
extern "C" uint64_t vk_sync_fence_done(void) { return completed; }
extern "C" void vk_completion_add(uint64_t fence,void(*fn)(void*),void *arg) {
    if(fence<=completed) fn(arg); else callbacks.push_back({fence,fn,arg});
}
static void complete(uint64_t fence) {
    completed=fence;
    for(size_t i=0;i<callbacks.size();) {
        if(callbacks[i].fence<=completed) {auto p=callbacks[i];callbacks.erase(callbacks.begin()+i);p.fn(p.arg);} else ++i;
    }
}
extern "C" int vk_init(void) {
    if(device.device) return 1;
    VkApplicationInfo app={VK_STRUCTURE_TYPE_APPLICATION_INFO};app.apiVersion=VK_API_VERSION_1_3;
    VkInstanceCreateInfo ici={VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};ici.pApplicationInfo=&app;
    if(vkCreateInstance(&ici,nullptr,&device.instance)!=VK_SUCCESS) return 0;
    uint32_t n=0;vkEnumeratePhysicalDevices(device.instance,&n,nullptr);if(!n)return 0;
    std::vector<VkPhysicalDevice> phys(n);vkEnumeratePhysicalDevices(device.instance,&n,phys.data());device.phys=phys[0];
    vkGetPhysicalDeviceProperties(device.phys,&device.props);
    vkGetPhysicalDeviceMemoryProperties(device.phys,&device.mem_props);
    vkGetPhysicalDeviceQueueFamilyProperties(device.phys,&n,nullptr);
    std::vector<VkQueueFamilyProperties> families(n);vkGetPhysicalDeviceQueueFamilyProperties(device.phys,&n,families.data());
    for(uint32_t i=0;i<n;++i) if((families[i].queueFlags&(VK_QUEUE_GRAPHICS_BIT|VK_QUEUE_COMPUTE_BIT))==(VK_QUEUE_GRAPHICS_BIT|VK_QUEUE_COMPUTE_BIT)) {device.gfx_family=i;break;}
    float priority=1;VkDeviceQueueCreateInfo q={VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    q.queueFamilyIndex=device.gfx_family;q.queueCount=1;q.pQueuePriorities=&priority;
    VkPhysicalDeviceFeatures features={};vkGetPhysicalDeviceFeatures(device.phys,&features);
    VkDeviceCreateInfo d={VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};d.queueCreateInfoCount=1;d.pQueueCreateInfos=&q;
    d.pEnabledFeatures=&features; // Vulkan 1.3 core entry points, no promoted KHR extension enabled.
    if(vkCreateDevice(device.phys,&d,nullptr,&device.device)!=VK_SUCCESS)return 0;
    vkGetDeviceQueue(device.device,device.gfx_family,0,&device.gfx_queue);
    VmaAllocatorCreateInfo ai={};ai.instance=device.instance;ai.physicalDevice=device.phys;ai.device=device.device;ai.vulkanApiVersion=VK_API_VERSION_1_3;
    return vmaCreateAllocator(&ai,&device.vma)==VK_SUCCESS;
}
extern "C" void vk_shutdown(void) {
    vmaDestroyAllocator(device.vma);vkDestroyDevice(device.device,nullptr);vkDestroyInstance(device.instance,nullptr);device={};
}
int main() {
    assert(vk_init());
    // Exercise the real SDK probe (including finite nonconstant readback) independently of the wrapper.
    Probe p={};p.c=&device;assert(probe_run(&p));
    // Replace only the synthetic depth with a real scene-style D32F image for the wrapper path.
    vmaDestroyImage(device.vma,p.images[IMG_DEPTH].image,p.images[IMG_DEPTH].alloc);
    VkImageCreateInfo ci={VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};ci.imageType=VK_IMAGE_TYPE_2D;ci.format=VK_FORMAT_D32_SFLOAT;
    ci.extent={RENDER_W,RENDER_H,1};ci.mipLevels=ci.arrayLayers=1;ci.samples=VK_SAMPLE_COUNT_1_BIT;
    ci.tiling=VK_IMAGE_TILING_OPTIMAL;ci.usage=VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    VmaAllocationCreateInfo ai={};ai.usage=VMA_MEMORY_USAGE_AUTO;
    assert(vmaCreateImage(device.vma,&ci,&ai,&p.images[IMG_DEPTH].image,&p.images[IMG_DEPTH].alloc,nullptr)==VK_SUCCESS);
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    assert(vkResetCommandBuffer(p.cb,0)==VK_SUCCESS);assert(vkBeginCommandBuffer(p.cb,&begin)==VK_SUCCESS);
    image_barrier(p.cb,p.images[IMG_DEPTH].image,VK_IMAGE_ASPECT_DEPTH_BIT,VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_GENERAL,0,VK_ACCESS_TRANSFER_WRITE_BIT);
    VkImageSubresourceRange range={VK_IMAGE_ASPECT_DEPTH_BIT,0,1,0,1};VkClearDepthStencilValue clear={0.5f,0};
    vkCmdClearDepthStencilImage(p.cb,p.images[IMG_DEPTH].image,VK_IMAGE_LAYOUT_GENERAL,&clear,1,&range);
    image_barrier(p.cb,p.images[IMG_DEPTH].image,VK_IMAGE_ASPECT_DEPTH_BIT,VK_IMAGE_LAYOUT_GENERAL,VK_IMAGE_LAYOUT_GENERAL,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT);
    VkTemporalDispatch f={};f.cb=p.cb;f.recording_fence=1;
    f.width=f.output_width=RENDER_W;f.height=f.output_height=RENDER_H;
    f.color=p.images[IMG_COLOUR].image;f.depth=p.images[IMG_DEPTH].image;f.motion=p.images[IMG_MOTION].image;f.output=p.images[IMG_OUTPUT].image;
    f.color_format=VK_FORMAT_R16G16B16A16_SFLOAT;f.depth_format=VK_FORMAT_D32_SFLOAT;
    f.near_plane=0.1f;f.far_plane=1000;f.vertical_fov=1;f.delta_ms=16;f.reset=1;
    f.jitter_x=0.25f;f.jitter_y=-0.125f;
    assert(vk_fsr3_dispatch(&f)==1); // Native AA
    auto &constants=((FfxFsr3UpscalerContext_Private*)&active->sdk)->constants;
    assert(constants.jitterOffset[0]==f.jitter_x && constants.jitterOffset[1]==f.jitter_y);
    assert(fabs(constants.motionVectorScale[0]-1.0f/RENDER_W)<1e-8f);
    assert(fabs(constants.motionVectorScale[1]-1.0f/RENDER_H)<1e-8f);
    // OpenGL perspective z, followed by replay's reversed depth mapping: rawDepth = (1-ndcZ)/2.
    // SDK GetViewSpaceDepth must recover distance directly, with no depth conversion pass.
    for(float distance: {0.1f,1.0f,100.0f,1000.0f}) {
        double n=f.near_plane,far=f.far_plane;
        double ndc=(far+n)/(far-n)-2*far*n/((far-n)*distance);
        double raw=(1-ndc)/2;
        double recovered=constants.deviceToViewDepth[1]/(raw-constants.deviceToViewDepth[0]);
        assert(fabs(recovered-distance)<0.002*distance);
    }
    // Accumulate samples current UV + jitter/renderSize; Reproject adds previous-current motion.
    float current=200,previous=190;
    assert(fabs((current/RENDER_W + (previous-current)*constants.motionVectorScale[0])-previous/RENDER_W)<1e-6f);
    // Whole-frame barrier: common renderer supplies this between invocations, not the backend.
    VkMemoryBarrier memory={VK_STRUCTURE_TYPE_MEMORY_BARRIER};memory.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT;memory.dstAccessMask=VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT;
    vkCmdPipelineBarrier(p.cb,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,1,&memory,0,nullptr,0,nullptr);
    f.output_width=OUTPUT_W;f.output_height=OUTPUT_H;
    assert(vk_fsr3_dispatch(&f)==1 && callbacks.size()==1); // replacement while commands remain unsubmitted
    vkCmdPipelineBarrier(p.cb,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,1,&memory,0,nullptr,0,nullptr);
    f.reset=0;assert(vk_fsr3_dispatch(&f)==1);
    vkCmdPipelineBarrier(p.cb,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,1,&memory,0,nullptr,0,nullptr);
    f.reset=1;assert(vk_fsr3_dispatch(&f)==1);
    assert(vk_fsr3_dispatch(&f)==0); // view recycling would destroy views still referenced by this same CB
    image_barrier(p.cb,f.output,VK_IMAGE_ASPECT_COLOR_BIT,VK_IMAGE_LAYOUT_GENERAL,VK_IMAGE_LAYOUT_GENERAL,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copy={};copy.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};copy.imageExtent={OUTPUT_W,OUTPUT_H,1};
    vkCmdCopyImageToBuffer(p.cb,f.output,VK_IMAGE_LAYOUT_GENERAL,p.readback,1,&copy);
    assert(vkEndCommandBuffer(p.cb)==VK_SUCCESS);assert(submit_and_wait(&p));complete(1);
    vmaInvalidateAllocation(device.vma,p.readbackAlloc,0,VK_WHOLE_SIZE);assert(check_output(&p));
    assert(vkResetCommandBuffer(p.cb,0)==VK_SUCCESS);assert(vkBeginCommandBuffer(p.cb,&begin)==VK_SUCCESS);
    f.recording_fence=2;f.reset=0;assert(vk_fsr3_dispatch(&f)==1);
    assert(vkEndCommandBuffer(p.cb)==VK_SUCCESS);assert(submit_and_wait(&p));complete(2);
    vk_fsr3_shutdown();assert(callbacks.empty());
    assert(live.empty() && mapped.empty() && sets.empty());
    dispatch_fault_checks(&p,f);
    ffxFsr3UpscalerContextDestroy(p.context);free(p.context);free(p.scratch);
    vmaDestroyBuffer(device.vma,p.staging,p.stagingAlloc);vmaDestroyBuffer(device.vma,p.readback,p.readbackAlloc);
    for(auto &im:p.images)vmaDestroyImage(device.vma,im.image,im.alloc);
    vkDestroyCommandPool(device.device,p.pool,nullptr);vkDestroyFence(device.device,p.fence,nullptr);
    puts("FSR real-SDK GPU probe and wrapper NativeAA/upscale/reset/reserved-fence lifecycle passed");vk_shutdown();
}
'''


FAULTS = r'''
#include <set>
#include <map>
#include <string>
static std::set<std::pair<std::string,uint64_t>> live;
static std::map<VkDescriptorSet,VkDescriptorPool> sets;
static std::set<VkDeviceMemory> mapped;
static std::string fail_name;
static int fail_at, seen, gpu_dispatches, descriptor_updates;
static bool inject(const char *name) { return fail_name==name && ++seen==fail_at; }
static void add(const char *kind,uint64_t h) { assert(h && live.insert({kind,h}).second); }
static void drop(const char *kind,uint64_t h) { assert(h && live.erase({kind,h})==1); }
#define CREATE(Name,Info,Handle) \
static VKAPI_ATTR VkResult VKAPI_CALL test##Name(VkDevice d,const Info *i,const VkAllocationCallbacks *a,Handle *h) { \
    *h=VK_NULL_HANDLE; if(inject(#Name)) return VK_ERROR_OUT_OF_DEVICE_MEMORY; \
    VkResult r=vk##Name(d,i,a,h);if(r==VK_SUCCESS)add(#Handle,(uint64_t)*h);return r; }
#define DESTROY(Name,Handle) \
static VKAPI_ATTR void VKAPI_CALL test##Name(VkDevice d,Handle h,const VkAllocationCallbacks *a) { \
    drop(#Handle,(uint64_t)h);vk##Name(d,h,a); }
CREATE(CreateDescriptorPool,VkDescriptorPoolCreateInfo,VkDescriptorPool)
CREATE(CreateBuffer,VkBufferCreateInfo,VkBuffer)
CREATE(AllocateMemory,VkMemoryAllocateInfo,VkDeviceMemory)
CREATE(CreateSampler,VkSamplerCreateInfo,VkSampler)
CREATE(CreateDescriptorSetLayout,VkDescriptorSetLayoutCreateInfo,VkDescriptorSetLayout)
CREATE(CreatePipelineLayout,VkPipelineLayoutCreateInfo,VkPipelineLayout)
CREATE(CreateShaderModule,VkShaderModuleCreateInfo,VkShaderModule)
static VKAPI_ATTR VkResult VKAPI_CALL testCreateImageView(VkDevice d,const VkImageViewCreateInfo *i,const VkAllocationCallbacks *a,VkImageView *h) {
    *h=VK_NULL_HANDLE;
    // Arm only dynamic registration, not context creation's static image views.
    if(dispatching && inject("CreateImageView"))return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    VkResult r=vkCreateImageView(d,i,a,h);if(r==VK_SUCCESS)add("VkImageView",(uint64_t)*h);return r;
}
static VKAPI_ATTR void VKAPI_CALL testDestroyImageView(VkDevice d,VkImageView h,const VkAllocationCallbacks *a) {
    if(h)drop("VkImageView",(uint64_t)h);vkDestroyImageView(d,h,a);
}
static VKAPI_ATTR void VKAPI_CALL testCmdDispatch(VkCommandBuffer cb,uint32_t x,uint32_t y,uint32_t z) {
    ++gpu_dispatches;vkCmdDispatch(cb,x,y,z);
}
static VKAPI_ATTR void VKAPI_CALL testUpdateDescriptorSets(VkDevice d,uint32_t n,const VkWriteDescriptorSet *w,uint32_t c,const VkCopyDescriptorSet *copies) {
    ++descriptor_updates;vkUpdateDescriptorSets(d,n,w,c,copies);
}
DESTROY(DestroyBuffer,VkBuffer)
DESTROY(FreeMemory,VkDeviceMemory)
DESTROY(DestroySampler,VkSampler)
DESTROY(DestroyDescriptorSetLayout,VkDescriptorSetLayout)
DESTROY(DestroyPipelineLayout,VkPipelineLayout)
DESTROY(DestroyShaderModule,VkShaderModule)
DESTROY(DestroyPipeline,VkPipeline)
static VKAPI_ATTR void VKAPI_CALL testDestroyDescriptorPool(VkDevice d,VkDescriptorPool p,const VkAllocationCallbacks *a) {
    for(auto i=sets.begin();i!=sets.end();)if(i->second==p)i=sets.erase(i);else ++i;
    drop("VkDescriptorPool",(uint64_t)p);vkDestroyDescriptorPool(d,p,a);
}
static VKAPI_ATTR VkResult VKAPI_CALL testAllocateDescriptorSets(VkDevice d,const VkDescriptorSetAllocateInfo *i,VkDescriptorSet *s) {
    *s=VK_NULL_HANDLE;if(inject("AllocateDescriptorSets"))return VK_ERROR_OUT_OF_POOL_MEMORY;
    VkResult r=vkAllocateDescriptorSets(d,i,s);if(r==VK_SUCCESS)for(uint32_t n=0;n<i->descriptorSetCount;++n)assert(sets.emplace(s[n],i->descriptorPool).second);return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL testFreeDescriptorSets(VkDevice d,VkDescriptorPool p,uint32_t n,const VkDescriptorSet *s) {
    for(uint32_t i=0;i<n;++i)assert(sets.erase(s[i])==1);return vkFreeDescriptorSets(d,p,n,s);
}
static VKAPI_ATTR VkResult VKAPI_CALL testMapMemory(VkDevice d,VkDeviceMemory m,VkDeviceSize o,VkDeviceSize z,VkMemoryMapFlags f,void **p) {
    *p=nullptr;if(inject("MapMemory"))return VK_ERROR_MEMORY_MAP_FAILED;
    VkResult r=vkMapMemory(d,m,o,z,f,p);if(r==VK_SUCCESS)assert(mapped.insert(m).second);return r;
}
static VKAPI_ATTR void VKAPI_CALL testUnmapMemory(VkDevice d,VkDeviceMemory m) { assert(mapped.erase(m)==1);vkUnmapMemory(d,m); }
static VKAPI_ATTR VkResult VKAPI_CALL testBindBufferMemory(VkDevice d,VkBuffer b,VkDeviceMemory m,VkDeviceSize o) {
    return inject("BindBufferMemory") ? VK_ERROR_OUT_OF_DEVICE_MEMORY : vkBindBufferMemory(d,b,m,o);
}
static VKAPI_ATTR VkResult VKAPI_CALL testCreateComputePipelines(VkDevice d,VkPipelineCache cache,uint32_t n,const VkComputePipelineCreateInfo *i,const VkAllocationCallbacks *a,VkPipeline *p) {
    // Vulkan may return a pipeline handle alongside an error; rollback must destroy that handle too.
    VkResult r=vkCreateComputePipelines(d,cache,n,i,a,p);if(r==VK_SUCCESS)for(uint32_t x=0;x<n;++x)add("VkPipeline",(uint64_t)p[x]);
    return inject("CreateComputePipelines") ? VK_ERROR_OUT_OF_DEVICE_MEMORY : r;
}
static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL fault_proc(VkDevice d,const char *name) {
#define HOOK(Name) if(!strcmp(name,"vk" #Name))return (PFN_vkVoidFunction)test##Name;
    HOOK(CreateDescriptorPool) HOOK(DestroyDescriptorPool) HOOK(CreateBuffer) HOOK(DestroyBuffer)
    HOOK(AllocateMemory) HOOK(FreeMemory) HOOK(MapMemory) HOOK(UnmapMemory) HOOK(BindBufferMemory)
    HOOK(CreateSampler) HOOK(DestroySampler) HOOK(CreateDescriptorSetLayout) HOOK(DestroyDescriptorSetLayout)
    HOOK(AllocateDescriptorSets) HOOK(FreeDescriptorSets) HOOK(CreatePipelineLayout) HOOK(DestroyPipelineLayout)
    HOOK(CreateShaderModule) HOOK(DestroyShaderModule) HOOK(CreateComputePipelines) HOOK(DestroyPipeline)
    HOOK(CreateImageView) HOOK(DestroyImageView) HOOK(CmdDispatch) HOOK(UpdateDescriptorSets)
#undef HOOK
    return vkGetDeviceProcAddr(d,name);
}
static void fault_checks() {
    size_t size=ffxGetScratchMemorySizeVK(device.phys,1);void *scratch=calloc(1,size);
    VkDeviceContext dc={device.device,device.phys,fault_proc};FfxInterface b={};
    assert(ffxGetInterfaceVK(&b,ffxGetDeviceVK(&dc),scratch,size,1)==FFX_OK);
    uint32_t id;
    for(const char *name: {"CreateDescriptorPool","CreateBuffer","AllocateMemory","MapMemory","BindBufferMemory"}) {
        fail_name=name;fail_at=1;seen=0;
        assert(b.fpCreateBackendContext(&b,FFX_EFFECT_FSR3UPSCALER,nullptr,&id)!=FFX_OK);
        assert(live.empty() && mapped.empty() && sets.empty());
        fail_name.clear();assert(b.fpCreateBackendContext(&b,FFX_EFFECT_FSR3UPSCALER,nullptr,&id)==FFX_OK && id==0);
        assert(b.fpDestroyBackendContext(&b,id)==FFX_OK);
        assert(live.empty() && mapped.empty() && sets.empty());
    }
    assert(b.fpCreateBackendContext(&b,FFX_EFFECT_FSR3UPSCALER,nullptr,&id)==FFX_OK);
    auto baseline=live;auto baseline_mapped=mapped;
    uint32_t overflow_id=99;
    assert(b.fpCreateBackendContext(&b,FFX_EFFECT_FSR3UPSCALER,nullptr,&overflow_id)!=FFX_OK);
    assert(overflow_id==99 && live==baseline && mapped==baseline_mapped);
    FfxSamplerDescription samplers[2]={};samplers[0].filter=FFX_FILTER_TYPE_MINMAGMIP_POINT;samplers[1].filter=FFX_FILTER_TYPE_MINMAGMIP_LINEAR;
    FfxPipelineDescription desc={};desc.samplers=samplers;desc.samplerCount=2;
    FfxPipelineState pipeline={};
    for(auto point: {std::pair<const char*,int>{"CreateSampler",1},{"CreateSampler",2},
        {"CreateDescriptorSetLayout",1},{"AllocateDescriptorSets",1},{"AllocateDescriptorSets",5},
        {"CreatePipelineLayout",1},{"CreateShaderModule",1},{"CreateComputePipelines",1}}) {
        fail_name=point.first;fail_at=point.second;seen=0;
        assert(b.fpCreatePipeline(&b,FFX_EFFECT_FSR3UPSCALER,FFX_FSR3UPSCALER_PASS_PREPARE_INPUTS,0x16,&desc,id,&pipeline)!=FFX_OK);
        assert(!pipeline.rootSignature && !pipeline.pipeline && live==baseline && mapped==baseline_mapped && sets.empty());
        fail_name.clear();assert(b.fpCreatePipeline(&b,FFX_EFFECT_FSR3UPSCALER,FFX_FSR3UPSCALER_PASS_PREPARE_INPUTS,0x16,&desc,id,&pipeline)==FFX_OK);
        assert(b.fpDestroyPipeline(&b,&pipeline,id)==FFX_OK);
        assert(live==baseline && mapped==baseline_mapped && sets.empty());
    }
    assert(b.fpDestroyBackendContext(&b,id)==FFX_OK);
    assert(live.empty() && mapped.empty() && sets.empty());free(scratch);
    puts("SDK fault injection: backend/pipeline partial-driver failures clean up, do not double-destroy, and retry successfully");
}
static std::vector<uint16_t> snapshot(Probe *p,VkImage image,uint32_t w,uint32_t h) {
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    assert(vkResetCommandBuffer(p->cb,0)==VK_SUCCESS);assert(vkBeginCommandBuffer(p->cb,&begin)==VK_SUCCESS);
    image_barrier(p->cb,image,VK_IMAGE_ASPECT_COLOR_BIT,VK_IMAGE_LAYOUT_GENERAL,VK_IMAGE_LAYOUT_GENERAL,
                  VK_ACCESS_MEMORY_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copy={};copy.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};copy.imageExtent={w,h,1};
    vkCmdCopyImageToBuffer(p->cb,image,VK_IMAGE_LAYOUT_GENERAL,p->readback,1,&copy);
    assert(vkEndCommandBuffer(p->cb)==VK_SUCCESS);assert(submit_and_wait(p));
    vmaInvalidateAllocation(device.vma,p->readbackAlloc,0,VK_WHOLE_SIZE);
    VmaAllocationInfo info={};vmaGetAllocationInfo(device.vma,p->readbackAlloc,&info);
    const uint16_t *data=(const uint16_t*)info.pMappedData;
    return {data,data+(size_t)w*h*4};
}
static void dispatch_fault_checks(Probe *p,VkTemporalDispatch f) {
    auto scene=snapshot(p,f.color,RENDER_W,RENDER_H);
    auto output=snapshot(p,f.output,OUTPUT_W,OUTPUT_H);
    // First SRV, shared UAV, output SRV and output UAV: exercise early and late partial registration.
    for(int point: {1,5,10,11}) {
        VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        assert(vkResetCommandBuffer(p->cb,0)==VK_SUCCESS);assert(vkBeginCommandBuffer(p->cb,&begin)==VK_SUCCESS);
        f.recording_fence=completed+1;
        fail_name="CreateImageView";fail_at=point;seen=0;
        int jobs=gpu_dispatches, updates=descriptor_updates;
        int result=vk_fsr3_dispatch(&f);
        assert(result==-1 && !vk_fsr3_available() && seen>=point);
        assert(active->dispatch_error==FFX_ERROR_BACKEND_API_ERROR);
        // No descriptors or dispatches from incomplete resources; caller must not consume output.
        assert(gpu_dispatches==jobs && descriptor_updates==updates);
        assert(vk_fsr3_dispatch(&f)==-1 && gpu_dispatches==jobs);
        auto &sdk=*(FfxFsr3UpscalerContext_Private*)&active->sdk;
        assert(sdk.resourceFrameIndex==1 && active->slot==1);
        vk_fsr3_shutdown();assert(callbacks.size()==1 && !live.empty());
        assert(vkEndCommandBuffer(p->cb)==VK_SUCCESS);assert(submit_and_wait(p));complete(f.recording_fence);
        assert(callbacks.empty() && live.empty() && mapped.empty() && sets.empty());
        assert(snapshot(p,f.color,RENDER_W,RENDER_H)==scene);
        assert(snapshot(p,f.output,OUTPUT_W,OUTPUT_H)==output);
        // Restart is explicit; the failed context and its queued jobs are never reused.
        fail_name.clear();f.recording_fence=completed+1;
        assert(vkResetCommandBuffer(p->cb,0)==VK_SUCCESS);assert(vkBeginCommandBuffer(p->cb,&begin)==VK_SUCCESS);
        assert(vk_fsr3_dispatch(&f)==1 && gpu_dispatches>jobs);
        assert(vkEndCommandBuffer(p->cb)==VK_SUCCESS);assert(submit_and_wait(p));complete(f.recording_fence);
        vk_fsr3_shutdown();assert(callbacks.empty() && live.empty() && mapped.empty() && sets.empty());
        assert(snapshot(p,f.color,RENDER_W,RENDER_H)==scene);
        output=snapshot(p,f.output,OUTPUT_W,OUTPUT_H);
    }
    puts("Dispatch vkCreateImageView faults (1,5,10,11): scene/output unchanged, no invalid GPU jobs, reserved-fence cleanup without double-free, restart retry passed");
}
'''


def gpu_test():
    src = OUT / 'gpu.cpp'
    code = GPU.replace('int main() {', FAULTS + '\nint main() {').replace('assert(vk_init());', 'assert(vk_init());fault_checks();')
    src.write_text(code, encoding='utf-8')
    sources = [SDK / 'src/backends/vk/ffx_vk.cpp',
               SDK / 'src/components/fsr3upscaler/ffx_fsr3upscaler.cpp',
               *sorted((SDK / 'src/shared').glob('*.cpp')),
               ROOT / 'runtime/ffx_blobs.cpp', ROOT / 'runtime/vk_vma.cpp', src]
    objects = []
    for source in sources:
        obj = OUT / (source.stem + '.o')
        run([compiler(), *flags(), '-c', source, '-o', obj])
        objects.append(obj)
    exe = OUT / ('gpu.exe' if os.name == 'nt' else 'gpu')
    run([compiler(), *objects, '-lvulkan-1' if os.name == 'nt' else '-lvulkan', '-o', exe])
    run([exe])


def compiler():
    return shutil.which('clang++') or shutil.which('g++') or 'clang++'


def flags():
    vma = ROOT / 'toolchain/vma'
    return ['-std=c++17', '-O1', '-w', '-DFFX_GCC', '-DFFX_FSR3UPSCALER',
            '-include', str(ROOT / 'runtime/ffx_compat.h'), '-I' + str(ROOT / 'runtime'),
            '-I' + str(vma), '-I' + str(SDK / 'include'), '-I' + str(SDK / 'src/backends/shared'),
            '-I' + str(SDK / 'src/shared')]


def run(cmd):
    subprocess.run([str(x) for x in cmd], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    src = OUT / 'unit.cpp'
    src.write_text(UNIT, encoding='utf-8')
    exe = OUT / ('unit.exe' if os.name == 'nt' else 'unit')
    run([compiler(), *flags(), src, '-o', exe])
    run([exe])
    if args.gpu:
        gpu_test()


if __name__ == '__main__':
    main()
