"""Compile the vendored FSR 3.1 upscaler's GLSL compute shaders (AMD FidelityFX SDK 1.1.4, runtime/third_party/ffx)
into runtime/third_party/ffx/ffx_fsr3upscaler_spv.h.

The header holds one `static const uint32_t` array per distinct SPIR-V module, that module's resource bindings as
spirv-dis reports them, and the table that maps a pass and a shader permutation to a module. runtime/ffx_blobs.c turns
a table row into the FfxShaderBlob the SDK's Vulkan back end asks for: its CreatePipelineVK builds the pipeline's
descriptor set layout and its resource bindings from the blob's counts, binding numbers and names, not from the SPIR-V
alone.

Every pass is compiled for the eight permutations in VARIANTS; only one Lanczos type, wave-32 and FP32 variant exists,
so the table's keys leave the SDK's USE_LANCZOS_TYPE, FORCE_WAVE64 and ALLOW_FP16 flags out. Identical modules (most
passes do not read the HDR, depth or sharpening option) are stored once, which keeps the header near a megabyte
instead of five.

The header is committed, so building totk.exe needs no shader compiler; tools/build_game.py refuses to build when it no
longer matches the sources or these flags. Needs glslangValidator, spirv-val and spirv-dis (Debian package
glslang-tools and spirv-tools, MSYS2 package mingw-w64-ucrt-x86_64-glslang and spirv-tools).

    python tools/build_ffx_shaders.py [--check]
"""

import argparse
import hashlib
import itertools
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(r'C:\msys64\ucrt64\bin')   # Windows; elsewhere the tools come from PATH
WIN = os.name == 'nt'
FFX = Path('third_party') / 'ffx'     # below runtime/
SHADERS = Path('sdk/src/backends/vk/shaders/fsr3upscaler')
HOST_HEADER = Path('sdk/include/FidelityFX/host/ffx_fsr3upscaler.h')
PRIVATE_HEADER = Path('sdk/src/components/fsr3upscaler/ffx_fsr3upscaler_private.h')

# Every pass of sdk/src/backends/vk/shaders/fsr3upscaler as the FfxFsr3UpscalerPass member it fills. ACCUMULATE and
# ACCUMULATE_SHARPEN come from one source: the sharpening bit of the permutation picks the module.
PASS_OF = {
    'ffx_fsr3upscaler_prepare_inputs_pass': 'PREPARE_INPUTS',
    'ffx_fsr3upscaler_luma_pyramid_pass': 'LUMA_PYRAMID',
    'ffx_fsr3upscaler_shading_change_pyramid_pass': 'SHADING_CHANGE_PYRAMID',
    'ffx_fsr3upscaler_shading_change_pass': 'SHADING_CHANGE',
    'ffx_fsr3upscaler_prepare_reactivity_pass': 'PREPARE_REACTIVITY',
    'ffx_fsr3upscaler_luma_instability_pass': 'LUMA_INSTABILITY',
    'ffx_fsr3upscaler_accumulate_pass': 'ACCUMULATE',
    'ffx_fsr3upscaler_rcas_pass': 'RCAS',
    'ffx_fsr3upscaler_debug_view_pass': 'DEBUG_VIEW',
    'ffx_fsr3upscaler_autogen_reactive_pass': 'GENERATE_REACTIVE',
}

HALF_OPTIONS = ('UPSAMPLE_SAMPLERS_USE_DATA_HALF=0', 'ACCUMULATE_SAMPLERS_USE_DATA_HALF=0',
                'REPROJECT_SAMPLERS_USE_DATA_HALF=1', 'POSTPROCESSLOCKSTATUS_SAMPLERS_USE_DATA_HALF=0')
# Fixed for every permutation: the approximate/reference Lanczos samplers of ffx_fsr3upscaler_upsample.h and
# ffx_fsr3upscaler_reproject.h, render-resolution unjittered motion vectors.
FIXED = ['FFX_GLSL=1', 'FFX_GPU=1'] + [f'FFX_FSR3UPSCALER_OPTION_{o}' for o in HALF_OPTIONS] + [
    'FFX_FSR3UPSCALER_OPTION_UPSAMPLE_USE_LANCZOS_TYPE=2', 'FFX_FSR3UPSCALER_OPTION_REPROJECT_USE_LANCZOS_TYPE=0',
    'FFX_FSR3UPSCALER_OPTION_LOW_RESOLUTION_MOTION_VECTORS=1',
    'FFX_FSR3UPSCALER_OPTION_JITTERED_MOTION_VECTORS=0',
]
VARIANTS = list(itertools.product((0, 1), (0, 1), (0, 1)))   # HDR_COLOR_INPUT, INVERTED_DEPTH, APPLY_SHARPENING
TARGET = 'vulkan1.2'
# The FfxShaderBlob groups of ffx_types.h the Vulkan backend reads, in the order the header emits them. Samplers are
# not among them: CreatePipelineVK takes their binding numbers from the sampler index it is given.
GROUPS = ('srvTexture', 'uavTexture', 'srvBuffer', 'uavBuffer', 'cbv')


def defines(variant):
    """The -D options of one permutation, in a fixed order so the recipe hash is stable."""
    hdr, inverted_depth, sharpening = variant
    return FIXED + [f'FFX_FSR3UPSCALER_OPTION_HDR_COLOR_INPUT={hdr}',
                    f'FFX_FSR3UPSCALER_OPTION_INVERTED_DEPTH={inverted_depth}',
                    f'FFX_FSR3UPSCALER_OPTION_APPLY_SHARPENING={sharpening}']


def key_of(flags, variant):
    """The permutation key of one variant: the Fs3UpscalerShaderPermutationOptions bits the defines select. Without
    USE_LANCZOS_TYPE and JITTER_MOTION_VECTORS, which FIXED turns off for every permutation, and without
    FORCE_WAVE64 and ALLOW_FP16, which select nothing here."""
    hdr, inverted_depth, sharpening = variant
    return (flags['LOW_RES_MOTION_VECTORS'] | (flags['HDR_COLOR_INPUT'] if hdr else 0)
            | (flags['DEPTH_INVERTED'] if inverted_depth else 0) | (flags['ENABLE_SHARPENING'] if sharpening else 0))


def recipe_hash():
    return hashlib.sha256('\n'.join(' '.join(defines(v)) for v in VARIANTS).encode()).hexdigest()


def enum_members(text, name, prefix=''):
    """{member without prefix: value} of the C enum `name` in text; its members are names or integer expressions."""
    body = re.search(r'enum\s+' + name + r'\s*{(.*?)}', text, re.S).group(1)
    out, value = {}, -1
    for member, expr in re.findall(r'(\w+)\s*(?:=\s*([^,]+))?,', body):
        value = eval(expr, {}, {**out}) if expr else value + 1
        out[member.removeprefix(prefix)] = value
    return out


def tool(name):
    return str(BIN / f'{name}.exe') if WIN else name


def shader_sources(runtime=ROOT / 'runtime'):
    """Track entry points and included GPU headers, not only the top-level GLSL."""
    ffx = runtime / FFX
    return sorted([*(ffx / SHADERS).glob('*.glsl'),
                   *(ffx / 'sdk/include/FidelityFX/gpu').rglob('*.h')])


def source_hash(src):
    return hashlib.sha256(src.read_bytes().replace(b'\r\n', b'\n')).hexdigest()   # the same on a CRLF checkout


def bindings(spv):
    """The resource bindings of one module, as {group: [(binding, count, set, name)]} for the groups of GROUPS.

    Read from spirv-dis: a variable's type (image with sampled = 1 or 2, struct in the Uniform or StorageBuffer
    storage class) and the Binding, DescriptorSet and NonWritable decorations say what it is bound as."""
    dis = subprocess.run([tool('spirv-dis'), str(spv)], capture_output=True, text=True)
    if dis.returncode:
        sys.exit(f'spirv-dis failed on {spv}\n{dis.stdout}{dis.stderr}')
    names, binding, space, readonly = {}, {}, {}, set()
    image, array, struct, pointer, constant, variable = {}, {}, {}, {}, {}, {}
    for line in dis.stdout.splitlines():
        s = line.strip()
        if (m := re.match(r'OpName %(\w+) "(.*)"$', s)):
            names[m.group(1)] = m.group(2)
        elif (m := re.match(r'OpDecorate %(\w+) (Binding|DescriptorSet) (\d+)', s)):
            (binding if m.group(2) == 'Binding' else space)[m.group(1)] = int(m.group(3))
        elif (m := re.match(r'OpDecorate %(\w+) NonWritable$', s)):
            readonly.add(m.group(1))
        elif (m := re.match(r'%(\w+) = OpTypeImage %\w+ \w+ \d \d \d (\d) ', s)):   # Sampled: 1 sampled, 2 storage
            image[m.group(1)] = int(m.group(2))
        elif (m := re.match(r'%(\w+) = OpTypeSampler$', s)):
            struct[m.group(1)] = 'sampler'
        elif (m := re.match(r'%(\w+) = OpTypeArray %(\w+) %(\w+)', s)):
            array[m.group(1)] = (m.group(2), m.group(3))
        elif (m := re.match(r'%(\w+) = OpTypeStruct', s)):
            struct[m.group(1)] = 'struct'
        elif (m := re.match(r'%(\w+) = OpTypePointer (\w+) %(\w+)', s)):
            pointer[m.group(1)] = (m.group(2), m.group(3))
        elif (m := re.match(r'%(\w+) = OpConstant %\w+ (\d+)', s)):
            constant[m.group(1)] = int(m.group(2))
        elif (m := re.match(r'%(\w+) = OpVariable %(\w+) (\w+)', s)):
            variable[m.group(1)] = (m.group(2), m.group(3))
    out = {g: [] for g in GROUPS}
    for var, (ptr, storage) in variable.items():
        if var not in binding or ptr not in pointer:
            continue
        base, count = pointer[ptr][1], 1
        if base in array:
            base, count = array[base][0], constant.get(array[base][1], 1)
        if base in image:
            group = 'srvTexture' if image[base] == 1 else 'uavTexture'
        elif struct.get(base) == 'sampler':
            continue   # samplers are bound by index, see GROUPS
        elif storage == 'Uniform':
            group = 'cbv'
        elif storage in ('StorageBuffer', 'UniformConstant'):
            group = 'srvBuffer' if var in readonly else 'uavBuffer'
        else:
            sys.exit(f'{spv}: {names.get(var, var)} is a {storage} binding the header cannot describe')
        out[group].append((binding[var], count, space.get(var, 0), names.get(var, var)))
    for g in GROUPS:
        out[g].sort()
    return out


def permutations():
    """{(pass member, permutation key): module id} and the module table, plus the source hash of every shader."""
    ffx = ROOT / 'runtime' / FFX
    flags = enum_members((ffx / PRIVATE_HEADER).read_text(encoding='utf-8'), 'Fs3UpscalerShaderPermutationOptions',
                         'FSR3UPSCALER_SHADER_PERMUTATION_')
    passes = enum_members((ffx / HOST_HEADER).read_text(encoding='utf-8'), 'FfxFsr3UpscalerPass',
                          'FFX_FSR3UPSCALER_PASS_')
    srcs = sorted((ffx / SHADERS).glob('*.glsl'))
    if len(srcs) != len(PASS_OF):
        sys.exit(f'{ffx / SHADERS}: {len(srcs)} shaders, expected {len(PASS_OF)}')
    keys, modules = {}, {}
    hashes = {src: source_hash(src) for src in shader_sources()}
    with tempfile.TemporaryDirectory() as tmp:
        for src in srcs:
            for variant in VARIANTS:
                spv = Path(tmp) / 'out.spv'
                cmd = [tool('glslangValidator'), '-e', 'CS', '--target-env', TARGET, '-S', 'comp', '-Os'] + \
                      [f'-D{d}' for d in defines(variant)] + \
                      [f'-I{ffx / "sdk/include/FidelityFX/gpu"}', f'-I{ffx / "sdk/include/FidelityFX/gpu/fsr3upscaler"}',
                       '-o', str(spv), str(src)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode or not spv.is_file():
                    sys.exit(f'{src.name} {variant}: glslangValidator failed\n{r.stdout}{r.stderr}')
                v = subprocess.run([tool('spirv-val'), '--target-env', TARGET, str(spv)], capture_output=True, text=True)
                if v.returncode:
                    sys.exit(f'{src.name} {variant}: spirv-val rejected the module\n{v.stdout}{v.stderr}')
                data = spv.read_bytes()
                h = hashlib.sha256(data).hexdigest()
                if h not in modules:
                    modules[h] = (f'ffx_fsr3upscaler_spv_{len(modules):02d}', data, bindings(spv))
                name = PASS_OF[src.stem]
                if name == 'ACCUMULATE':   # one source fills both passes: the sharpening bit picks the module
                    name += '_SHARPEN' if variant[2] else ''
                keys[(name, key_of(flags, variant))] = h
    return keys, modules, hashes, passes


def module_rows(modules):
    """The arrays and the module table: one row per distinct module."""
    arrays, rows = [], []
    for h, (name, data, groups) in modules.items():
        words = [int.from_bytes(data[i:i + 4], 'little') for i in range(0, len(data), 4)]
        arrays.append(f'static const uint32_t {name}[] = {{')
        for i in range(0, len(words), 8):
            # No space between the words: with one, the eight permutations of ten passes pass the 1 MiB file limit of
            # tools/check_content.py.
            arrays.append('    ' + ''.join(f'0x{w:08x},' for w in words[i:i + 8]))
        arrays.append('};')
        fields = [f'.data = {name}', f'.size = {len(data)}']
        for g, entries in groups.items():
            if not entries:   # the field keeps its zero value
                continue
            fields.append(f'.{g}Count = {len(entries)}')
            for what, values in (('Bindings', [b for b, _, _, _ in entries]),
                                 ('Counts', [c for _, c, _, _ in entries]),
                                 ('Spaces', [s for _, _, s, _ in entries])):
                arr = f'{name}_{g}{what}'
                arrays.append(f'static const uint32_t {arr}[] = {{{", ".join(str(x) for x in values)}}};')
                fields.append(f'.{g}{what} = {arr}')
            arr = f'{name}_{g}Names'
            quoted = ', '.join('"' + n + '"' for _, _, _, n in entries)
            arrays.append(f'static const char *const {arr}[] = {{{quoted}}};')
            fields.append(f'.{g}Names = {arr}')
        rows.append(f'    {{' + ', '.join(fields) + '},')
    return arrays, rows


def header_of(keys, modules, hashes, passes):
    """The header text: the modules with their bindings, then the {pass, permutation} table."""
    lines = ['// Generated by tools/build_ffx_shaders.py from runtime/third_party/ffx/sdk/src/backends/vk/shaders/'
             'fsr3upscaler/*.glsl; do not edit.',
             f'// recipe sha256 {recipe_hash()}']
    for src in sorted(hashes):
        lines.append(f'// source sha256 {src.relative_to(ROOT / "runtime" / FFX).as_posix()} {hashes[src]}')
    lines += ['#pragma once', '#include <stdint.h>', '',
              '// One SPIR-V module of the upscaler and the resource bindings its pipeline layout is built from.',
              'typedef struct FfxFsr3UpscalerSpvModule {',
              '    const uint32_t *data;',
              '    uint32_t size;']
    for g in GROUPS:
        lines.append(f'    uint32_t {g}Count;')
        lines.append(f'    const uint32_t *{g}Bindings, *{g}Counts, *{g}Spaces;')
        lines.append(f'    const char *const *{g}Names;')
    lines.append('} FfxFsr3UpscalerSpvModule;')
    lines += ['', '// One row of the table: the FfxFsr3UpscalerPass member and the permutation key runtime/ffx_blobs.c',
              '// explains.', 'typedef struct FfxFsr3UpscalerSpvBlob {',
              '    uint32_t pass, permutation;', '    const FfxFsr3UpscalerSpvModule *module;',
              '} FfxFsr3UpscalerSpvBlob;', '']
    arrays, rows = module_rows(modules)
    lines += arrays
    lines += ['', 'static const FfxFsr3UpscalerSpvModule ffx_fsr3upscaler_spv_modules[] = {']
    lines += rows
    lines.append('};')
    order = lambda k: (k[0] != 'ACCUMULATE_SHARPEN', passes[k[0]], k[1])
    index = {name: i for i, (name, _, _) in enumerate(modules.values())}
    lines += ['', 'static const FfxFsr3UpscalerSpvBlob ffx_fsr3upscaler_spv_blobs[] = {']
    for (pass_name, permutation), h in sorted(keys.items(), key=lambda kv: order(kv[0])):
        lines.append(f'    {{FFX_FSR3UPSCALER_PASS_{pass_name}, {permutation:#04x}, '
                     f'&ffx_fsr3upscaler_spv_modules[{index[modules[h][0]]}]}},')
    lines.append('};')
    return '\n'.join(lines) + '\n'


def header_path(runtime=ROOT / 'runtime'):
    return runtime / FFX / 'ffx_fsr3upscaler_spv.h'


def stale(runtime=ROOT / 'runtime'):
    """Reasons the header no longer matches the shaders, the flags or the tools, or []."""
    h = header_path(runtime)
    if not h.exists():
        return [f'{h}: missing, run python tools/build_ffx_shaders.py']
    text = h.read_text(encoding='utf-8')
    if f'// recipe sha256 {recipe_hash()}' not in text:
        return [f'{h}: its permutation flags changed, run python tools/build_ffx_shaders.py']
    return [f'{h}: {src.name} changed, run python tools/build_ffx_shaders.py'
            for src in shader_sources(runtime)
            if f'// source sha256 {src.relative_to(runtime / FFX).as_posix()} {source_hash(src)}' not in text]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--check', action='store_true', help='only report a header that does not match its sources')
    ap.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    args = ap.parse_args()
    if args.check:
        bad = stale(args.runtime)
        for line in bad:
            print(line)
        return 1 if bad else 0
    keys, modules, hashes, passes = permutations()
    h = header_path(args.runtime)
    h.write_text(header_of(keys, modules, hashes, passes), encoding='utf-8', newline='\n')
    print(f'{h.relative_to(ROOT)}: {len(keys)} permutations from {len(PASS_OF)} shaders in {len(modules)} modules, '
          f'{h.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
