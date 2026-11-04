"""Exercise real motion descriptor replay with typed Vulkan mocks; no game or GPU needed."""
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
#include "vk_exec_draw.c"
static VkCtx context;
static VkPipelineLayout push_layout;
static int pushes, binds, draws, resets, creations;
static uint32_t expected_count;

VkCtx *vk_ctx(void) { return &context; }
int vk_pipeline_vertex_input_dynamic(void) { return 0; }
PFN_vkCmdPushDescriptorSetKHR vk_pipeline_push_fn(void) { return NULL; }
uint32_t vk_pipeline_set_dynamic(VkCommandBuffer cb, const VkPipeState *s) { return 0; }
void rc_vkCmdBindPipeline(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipeline p) {}
void rc_vkCmdSetViewportWithCount(VkCommandBuffer cb, uint32_t n, const VkViewport *v) {}
void rc_vkCmdSetScissorWithCount(VkCommandBuffer cb, uint32_t n, const VkRect2D *r) {}
void rc_vkCmdSetDepthBias(VkCommandBuffer cb, float c, float cl, float s) {}
void rc_vkCmdBindVertexBuffers2(VkCommandBuffer cb, uint32_t first, uint32_t n, const VkBuffer *b,
                               const VkDeviceSize *o, const VkDeviceSize *sz, const VkDeviceSize *st) {}
void rc_vkCmdBindIndexBuffer(VkCommandBuffer cb, VkBuffer b, VkDeviceSize o, VkIndexType t) {}
void rc_vkCmdDrawIndexed(VkCommandBuffer cb, uint32_t n, uint32_t i, uint32_t f, int32_t v, uint32_t fi) { draws++; }
void rc_vkCmdBindDescriptorSets(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipelineLayout layout,
                               uint32_t first, uint32_t n, const VkDescriptorSet *sets,
                               uint32_t nd, const uint32_t *offsets) {
    assert(n == 1 && sets[0]);
    if (layout == push_reset.layout) {
        assert(context.validation && first == 0 && sets[0] == push_reset.set);
        push_layout = VK_NULL_HANDLE; // ordinary set explicitly replaces stale push metadata
        resets++;
    } else {
        assert(first <= 1);
        // Model observed stale metadata: set 1 does not reliably disturb the old push set.
        binds++;
    }
}
void rec_push(PFN_vkCmdPushDescriptorSetKHR fn, VkCommandBuffer cb, VkPipelineBindPoint bp,
              VkPipelineLayout layout, uint32_t set, uint32_t n, const VkWriteDescriptorSet *w) {
    assert(set <= 1 && n == 1);
    if (context.validation) {
        assert(!push_layout || push_layout == layout);
        push_layout = layout;
    }
    assert(w[0].dstBinding == (set ? 1 : layout == (VkPipelineLayout)4 ? 7 : 3));
    assert(w[0].descriptorCount == expected_count);
    assert(w[0].descriptorType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER);
    assert(!w[0].pNext && !w[0].dstSet && !w[0].pImageInfo && !w[0].pTexelBufferView);
    for (uint32_t i = 0; i < expected_count; i++) {
        assert(w[0].pBufferInfo[i].buffer == (VkBuffer)(uintptr_t)((set ? 22 : 20) + i));
        assert(w[0].pBufferInfo[i].range == 256);
    }
    pushes++;
}

VkResult vkCreateDescriptorSetLayout(VkDevice device, const VkDescriptorSetLayoutCreateInfo *info,
                                      const VkAllocationCallbacks *allocator, VkDescriptorSetLayout *out) {
    assert(info->sType == VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO && !info->flags && !info->bindingCount);
    *out = (VkDescriptorSetLayout)101; creations++; return VK_SUCCESS;
}
VkResult vkCreatePipelineLayout(VkDevice device, const VkPipelineLayoutCreateInfo *info,
                                 const VkAllocationCallbacks *allocator, VkPipelineLayout *out) {
    assert(!info->flags && info->setLayoutCount == 1 && info->pSetLayouts[0] == push_reset.set_layout);
    *out = (VkPipelineLayout)102; creations++; return VK_SUCCESS;
}
VkResult vkCreateDescriptorPool(VkDevice device, const VkDescriptorPoolCreateInfo *info,
                                 const VkAllocationCallbacks *allocator, VkDescriptorPool *out) {
    assert(info->maxSets == 1 && !info->poolSizeCount);
    *out = (VkDescriptorPool)103; creations++; return VK_SUCCESS;
}
VkResult vkAllocateDescriptorSets(VkDevice device, const VkDescriptorSetAllocateInfo *info, VkDescriptorSet *out) {
    assert(info->descriptorPool == push_reset.pool && info->descriptorSetCount == 1);
    assert(info->pSetLayouts[0] == push_reset.set_layout);
    *out = (VkDescriptorSet)104; creations++; return VK_SUCCESS;
}
void rt_fatal(const char *format, ...) { abort(); }

int main(void) {
    VkPipeBinding binding[2] = {
        {.binding = 3, .type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, .count = 2, .set = 0},
        {.binding = 1, .type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, .count = 1, .set = 1}
    };
    VkPipeInfo info = {.layout = (VkPipelineLayout)1, .n_sets = 2, .push = 1,
                       .n_bindings = 2, .bindings = binding};
    VkPipeState state = {0};
    MoVb vb = {0};
    VkDescSlot descriptors[6] = {0};
    for (int i = 0; i < 6; i++) descriptors[i].buffer = (VkDescriptorBufferInfo){(VkBuffer)(uintptr_t)(20 + i % 3), 0, 256};
    MoDraw list[3] = {0};
    for (int i = 0; i < 3; i++) {
        list[i].pipe = (VkPipeline)1;
        list[i].info = &info;
        list[i].set[1] = (VkDescriptorSet)2;
        list[i].desc = i ? 3 : 0; // same descriptors at different packed indices
    }
    MQ.d = list; MQ.n = 3; MQ.st = &state; MQ.vb = &vb; MQ.desc = descriptors;
    expected_count = 2;
    for (int validation = 0; validation <= 1; validation++) {
        context.validation = validation;
        push_layout = (VkPipelineLayout)99; // prior game layout, not in mo_emit's cache
        pushes = binds = draws = 0;
        mo_emit((VkCommandBuffer)1);
        assert(pushes == 1 && binds == 1 && draws == 3); // unchanged draws retain the cache
        assert(resets == validation && creations == validation * 4);
    }
    // A second independent layout needs a fresh push, even when descriptor bytes match.
    VkPipeBinding other_binding[2];
    memcpy(other_binding, binding, sizeof binding);
    other_binding[0].binding = 7;
    VkPipeInfo other = info;
    other.layout = (VkPipelineLayout)4; other.bindings = other_binding;
    list[1].info = &other;
    pushes = binds = draws = 0;
    mo_emit((VkCommandBuffer)1); // A -> B -> A, including a return to an earlier layout
    assert(pushes == 3 && binds == 3 && draws == 3);
    // Empty lists leave the camera-only fallback untouched.
    MQ.n = 0;
    mo_emit((VkCommandBuffer)1);
    assert(pushes == 3 && binds == 3 && draws == 3);
    // Fragment-stage pushes use the same reset and keep descriptor offsets intact.
    other.push = 0; other.push1 = 1;
    list[0].info = &other; list[0].set[0] = (VkDescriptorSet)2;
    MQ.n = 1; expected_count = 1;
    mo_emit((VkCommandBuffer)1);
    assert(pushes == 4 && binds == 4 && draws == 4);
    // Single-set layouts also reset under validation, without needing set 1.
    int previous_resets = resets;
    other.n_sets = 1; other.n_bindings = 1; other.push = 1; other.push1 = 0;
    expected_count = 2;
    mo_emit((VkCommandBuffer)1);
    assert(pushes == 5 && binds == 4 && draws == 5);
    assert(resets == previous_resets + 1 && creations == 4);
    // Allocated-set fallback works without touching push metadata.
    other.push = 0; previous_resets = resets;
    mo_emit((VkCommandBuffer)1);
    assert(pushes == 5 && binds == 5 && draws == 6 && resets == previous_resets);
    return 0;
}
'''


@unittest.skipUnless(CC, "clang required")
class MotionDescriptorTest(unittest.TestCase):
    def test_motion_replay(self):
        (ROOT / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "build", prefix="motion-descriptor-") as directory:
            tmp = Path(directory)
            source = tmp / "motion.c"
            source.write_text(HARNESS)
            binary = tmp / "motion"
            subprocess.run([CC, "-std=gnu11", "-O1", "-ffunction-sections", "-fdata-sections",
                            "-I", str(ROOT / "runtime"), "-I", str(ROOT / "toolchain/vma"),
                            "-Wl,--gc-sections", "-fsanitize=address,undefined", str(source),
                            "-lm", "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True,
                           env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1"})


if __name__ == "__main__":
    unittest.main()
