// Backend boundary for the shared scene selection, jitter and camera/object motion path.
#pragma once
#include <stdint.h>
#include <vulkan/vulkan.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VkTemporalDispatch {
    VkCommandBuffer cb;
    uint64_t recording_fence;   // reserved replay submission fence, not vk_sync_issued()+1
    uint32_t width, height, output_width, output_height;
    VkImage color, depth, motion, mask, output;
    VkImageView color_view, depth_view, motion_view, mask_view, output_view;
    VkFormat color_format, depth_format;
    // Previous minus current, unjittered render-pixel motion. Positive jitter moves the viewport right/down.
    float jitter_x, jitter_y;
    float delta_ms, near_plane, far_plane, vertical_fov, sharpness;
    int reset, preset;   // preset: NVIDIA model (0 automatic); ignored by FSR
} VkTemporalDispatch;

// Dispatch: 1 success, 0 transient deferral (retry with reset), -1 permanent failure for this run.
// FSR owns one dynamic-resolution context; callers supply images and retain them through the recording fence.
int vk_fsr3_available(void);
int vk_fsr3_dispatch(const VkTemporalDispatch *frame);
void vk_fsr3_shutdown(void);   // after submissions and recorder have drained, before the Vulkan device is destroyed

// NGX extensions are requested only for the selected NVIDIA method, before device creation.
int vk_dlss_wanted(void);
void vk_dlss_instance_exts(const char **ext, uint32_t *n, uint32_t max);
void vk_dlss_device_exts(const VkExtensionProperties *have, uint32_t nhave, const char **ext, uint32_t *n, uint32_t max);
void vk_dlss_init(void);
int vk_dlss_available(void);
// One feature per cached scene/output size, owned by the shared renderer. Failure never replaces the game image.
int vk_dlss_dispatch(void **feature, const VkTemporalDispatch *frame);
void vk_dlss_free(void *feature);   // caller has retired all GPU/recorder references
void vk_dlss_shutdown(void);

#ifdef __cplusplus
}
#endif
