"""Build and run the Linux platform checks in tests/*.c that include runtime/platform/linux.c. No game data.

Usage: platform_linux_check.py
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ['platform_linux_check']


def main():
    if not sys.platform.startswith('linux'):
        print('platform_linux_check: skipped (Linux only)')
        return 0
    work = ROOT / 'build' / 'test' / 'platform_linux'
    work.mkdir(parents=True, exist_ok=True)
    sdl = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'sdl3'], text=True))
    for name in TESTS:
        exe = work / name
        # platform_linux_check.c binds values to x19-x29 to check that an interrupt keeps them, and x29 is the frame
        # pointer unless the build omits it.
        subprocess.check_call([os.environ.get('CC', 'clang'), '-O1', '-g', '-fomit-frame-pointer', '-Wall',
                               '-I', str(ROOT / 'runtime'), str(ROOT / 'tests' / f'{name}.c'),
                               '-pthread', '-ldl', '-lm', *sdl, '-o', str(exe)])
        if subprocess.run([str(exe)]).returncode:
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
