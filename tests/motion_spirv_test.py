"""Validate the actual object-motion fragment module at every runtime input Location.

Requires a C compiler and spirv-val; no game, Vulkan device or full build needed.
Regeneration and harness products stay in build/motion-spirv-test.
Run: python tests/motion_spirv_test.py
"""
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/motion-spirv-test'

HARNESS = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "vk_mvobj_fs.h"
typedef struct { int unused; } GfxShader;
static struct { const GfxShader *fs[16]; int failed; } MO;
static GfxShader shaders[16];
static unsigned emitted;
static const GfxShader *gfx_shader_own(const uint32_t *words, size_t count, const char *resources) {
    (void)resources;
    unsigned loc = words[MVOBJ_FS_LOCATION_WORD];
    assert(loc == emitted && count == sizeof(mvobj_fs_spv) / sizeof(uint32_t));
    char path[64];
    snprintf(path, sizeof path, "motion-%u.spv", loc);
    FILE *f = fopen(path, "wb");
    assert(f);
    assert(fwrite(words, sizeof(uint32_t), count, f) == count);
    assert(fclose(f) == 0);
    ++emitted;
    return &shaders[loc];
}
static void rt_log(const char *message) { (void)message; }
static const char MO_FS_RES[] = "test: module capture only";
@MO_FS@
int main(void) {
    assert(mo_fs(-1) == NULL && mo_fs(16) == NULL);
    for (int loc = 0; loc < 16; ++loc) {
        assert(mo_fs(loc) == &shaders[loc]);
        assert(mo_fs(loc) == &shaders[loc]);  // cached, not emitted twice
    }
    assert(emitted == 16 && !MO.failed);
    return 0;
}
'''


def main():
    compiler = shutil.which(os.environ.get('CC', 'cc'))
    validator = shutil.which('spirv-val')
    if not compiler or not validator:
        raise SystemExit('motion_spirv_test requires a C compiler (CC) and spirv-val')
    OUT.mkdir(parents=True, exist_ok=True)

    # Run the unmodified generator in a sandbox, not against the source tree.
    sandbox = OUT / 'regenerate'
    (sandbox / 'tools').mkdir(parents=True, exist_ok=True)
    (sandbox / 'runtime').mkdir(exist_ok=True)
    generator = sandbox / 'tools/make_mvobj_fs.py'
    shutil.copyfile(ROOT / 'tools/make_mvobj_fs.py', generator)
    subprocess.run([sys.executable, str(generator)], check=True)
    header = ROOT / 'runtime/vk_mvobj_fs.h'
    assert (sandbox / 'runtime/vk_mvobj_fs.h').read_bytes() == header.read_bytes(), 'regenerate vk_mvobj_fs.h'

    # Compile the actual runtime function, so validation sees its patched output,
    # not a separately assembled test shader. No new runtime test API is needed.
    draw = (ROOT / 'runtime/vk_exec_draw.c').read_text()
    start = draw.index('static const GfxShader *mo_fs(int loc) {')
    end = draw.index('\n}\n', start) + 2
    source = OUT / 'capture.c'
    source.write_text(HARNESS.replace('@MO_FS@', draw[start:end]))
    exe = OUT / ('capture.exe' if os.name == 'nt' else 'capture')
    subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(header.parent),
                    str(source), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], cwd=OUT, check=True)

    location_word = int(re.search(r'#define MVOBJ_FS_LOCATION_WORD (\d+)', header.read_text())[1])
    for loc in range(16):
        module = OUT / f'motion-{loc}.spv'
        data = module.read_bytes()
        words = struct.unpack(f'<{len(data) // 4}I', data)
        assert words[location_word - 3] == (4 << 16 | 71)  # OpDecorate
        assert words[location_word - 1] == 30 and words[location_word] == loc  # Location
        subprocess.run([validator, '--target-env', 'vulkan1.3', str(module)], check=True)

    # Recreate the original scalar-condition defect and prove spirv-val rejects it.
    words = list(words)
    i = 5
    constructs = {}
    while i < len(words):
        length, opcode = words[i] >> 16, words[i] & 0xffff
        assert length > 0
        if opcode == 80:  # OpCompositeConstruct: result id -> component ids
            constructs[words[i + 2]] = words[i + 3:i + length]
        if opcode == 169:  # OpSelect
            condition = constructs[words[i + 3]]
            assert len(condition) == 4 and len(set(condition)) == 1
            words[i + 3] = condition[0]
            break
        i += length
    else:
        raise AssertionError('motion shader must select motion or zero by validity')
    broken = OUT / 'scalar-condition.spv'
    broken.write_bytes(struct.pack(f'<{len(words)}I', *words))
    result = subprocess.run([validator, '--target-env', 'vulkan1.3', str(broken)], capture_output=True, text=True)
    assert result.returncode != 0 and 'Expected vector sizes' in result.stderr, result.stderr
    print('motion SPIR-V: regenerated header matches; runtime Locations 0..15 valid; scalar-condition regression rejected')


if __name__ == '__main__':
    main()
