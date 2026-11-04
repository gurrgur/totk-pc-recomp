"""The Vulkan renderer is C; FidelityFX is C++. Their dispatch layout must agree."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = r'''
#include <stddef.h>
#include <stdio.h>
#include "vk_temporal_backend.h"
int main(void) {
    printf("%zu %zu %zu %zu %zu %zu\n", sizeof(VkTemporalDispatch),
           offsetof(VkTemporalDispatch, recording_fence), offsetof(VkTemporalDispatch, color),
           offsetof(VkTemporalDispatch, color_view), offsetof(VkTemporalDispatch, jitter_x),
           offsetof(VkTemporalDispatch, reset));
    return 0;
}
'''


def main():
    layouts = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for env, default, ext, standard in [('CC', 'cc', 'c', 'c11'), ('CXX', 'c++', 'cpp', 'c++17')]:
            source, binary = root / f'test.{ext}', root / f'test-{ext}'
            source.write_text(SOURCE)
            subprocess.run([*shlex.split(os.environ.get(env, default)), f'-std={standard}',
                            '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'runtime'),
                            str(source), '-o', str(binary)], check=True)
            layouts.append(subprocess.check_output([str(binary)], text=True).strip())
    assert layouts[0] == layouts[1], layouts
    print('C/C++ temporal backend dispatch ABI matched:', layouts[0])


if __name__ == '__main__':
    main()
