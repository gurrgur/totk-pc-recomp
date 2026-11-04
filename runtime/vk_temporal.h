// Shared temporal scene selection, jitter and camera/object motion.
#pragma once
#include "vk.h"

// Ultra Performance at 1080p renders 640 wide; smaller windows render less.
#define TEMPORAL_SCENE_MIN_W 320u

void vk_temporal_init(void);
void vk_temporal_shutdown(void);   // drains recorder/GPU, before device/VMA destruction; safe to repeat
int vk_temporal_available(void);

typedef struct VkExec VkExec;
// After target resolution, before the pass barrier: arm the scene or resolve before its first consumer.
void vk_temporal_pass(VkExec *ex);
void vk_temporal_frame(void);
void vk_temporal_draw(VkExec *ex);   // capture scene camera before fixed draw state
void vk_temporal_viewport(VkExec *ex, VkViewport *v);
int vk_temporal_prev_camera(VkDescriptorBufferInfo *out, float jitter[2]);
void vk_temporal_sampled(void);   // retain a substituted output through this recording's submission
// Keep the game's FXAA control and skip only scaling already replaced by a successful resolve.
int vk_temporal_skip_draw(VkExec *ex, const GfxShader *fs);

void vk_mvsurvey_draw(VkExec *ex);
void vk_mvsurvey_frame(void);
void vk_mvprev_draw(VkExec *ex);
// Draw last-frame vertex variants over camera motion, restricted to the accepted scene camera.
int vk_mvobj_execute(VkExec *ex, uint64_t cam, VkImageView mv, VkImageView depth, VkFormat depth_fmt,
                     uint32_t w, uint32_t h);
void vk_mvcheck_camera(VkCommandBuffer cb, VkImage mv, uint32_t w, uint32_t h, float jx, float jy);
void vk_mvcheck_final(VkCommandBuffer cb, uint64_t fence, VkImage mv, VkImage cimg, VkFormat cfmt, uint32_t level,
                      uint32_t layer, uint32_t w, uint32_t h, float jx, float jy);
const char *vk_mvobj_frame_masks(void);
void vk_mvcheck_output(VkCommandBuffer cb, VkImage out, uint32_t ow, uint32_t oh);
