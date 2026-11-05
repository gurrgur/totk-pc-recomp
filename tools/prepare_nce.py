"""Prepare the original AArch64 game images for native execution: rewrite the instructions that need the runtime and
emit the native metadata (sites, arenas, hashes) the loader checks."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'recomp'))
from check_content import GENERATED_MARKER
import layout
import patches

SVC, READ_TP, WRITE_TP, READ_TPRO, TICKS, FREQUENCY, CONSTANT, PATCH, CALL, SPIN, BRK, UDF, WORD, CACHE, ZERO, EXLOAD, EXSTORE, CLEAR, CAPSULE, INTERIOR = range(1, 21)
NOP = 0xd503201f


def signed(v, bits):
    return (v ^ (1 << (bits - 1))) - (1 << (bits - 1))


def branch(pc, target, bits=26, shift=0, opcode=0x14000000):
    delta = target - pc
    if delta & 3 or not -(1 << (bits + 1)) <= delta < (1 << (bits + 1)):
        raise ValueError(f'{pc:#x}: native branch out of range')
    return opcode | ((delta >> 2) & ((1 << bits) - 1)) << shift


def direct_target(w, pc):
    if w & 0x7c000000 == 0x14000000:
        return pc + 4 * signed(w & 0x3ffffff, 26)
    if w & 0xff000010 == 0x54000000 or w & 0x7e000000 == 0x34000000:
        return pc + 4 * signed((w >> 5) & 0x7ffff, 19)
    if w & 0x7e000000 == 0x36000000:
        return pc + 4 * signed((w >> 5) & 0x3fff, 14)
    return None


def operation(w):
    if w & 0xffe0001f == 0xd4000001:
        return SVC, (w >> 5) & 0xffff
    if w & 0xffe0001f == 0xd4200000:
        return BRK, (w >> 5) & 0xffff
    if w & 0xff000000 == 0xd4000000:
        raise ValueError('unsupported exception instruction')
    if w & 0xfff00000 in (0xd5300000, 0xd5100000):
        read = bool(w & (1 << 21))
        reg = (w >> 5) & 0xffff
        if reg == 0xde82:
            return (READ_TP if read else WRITE_TP), 0
        if reg == 0xde83 and read:
            return READ_TPRO, 0
        if read and reg in (0xdf01, 0xdf02):
            return TICKS, 0
        if read and reg == 0xdf00:
            return FREQUENCY, 19200000
        if reg in (0xda10, 0xda20, 0xda21):  # NZCV, FPCR, FPSR are real native state.
            return None
        if read and reg == 0xd807:  # DCZID_EL0: 64-byte guest zeroing.
            return CONSTANT, 4
        if read and reg == 0xd801:  # CTR_EL0: Cortex-A57 cache geometry.
            return CONSTANT, 0x8444c004
        raise ValueError('unsupported system register or direction')
    if w >> 24 == 0xd5:
        if w == 0xd503205f:  # Horizon's wait hint must not sleep on Linux's unrelated event state.
            return WORD, 0xd503203f
        if w in (0xd50320ff, 0xd503219f, 0xd50321df):  # A57 has no pointer authentication.
            return WORD, NOP
        if w & 0xffffffe0 in (0xd50b7e20, 0xd50b7520):
            return CACHE, 0
        if w & 0xffffffe0 == 0xd50b7420:
            return ZERO, 0
        if w in (NOP, 0xd503203f, 0xd503209f, 0xd50320bf, 0xd5033fdf, 0xd503229f):
            return None
        if w & 0xfffff0ff == 0xd503305f:
            return CLEAR, 0
        if w & 0xfffff0ff in (0xd50330bf, 0xd503309f) and (w >> 8) & 15 in (1, 2, 3, 5, 6, 7, 9, 10, 11, 13, 14, 15):
            return None
        raise ValueError('unsupported system operation')
    if w & 0xfe000000 == 0xd6000000 and w & 0xfffffc1f not in (0xd61f0000, 0xd63f0000, 0xd65f0000):
        raise ValueError('unsupported indirect control operation')
    kind = exclusive(w)
    if kind:
        return (EXLOAD if kind == 'load' else EXSTORE), 0
    return None


def exclusive(w):
    if w & 0x3ffffc00 in (0x085f7c00, 0x085ffc00) or w & 0xbfff8000 in (0x887f0000, 0x887f8000):
        return 'load'
    if w & 0x3fe0fc00 in (0x08007c00, 0x0800fc00) or w & 0xbfe08000 in (0x88200000, 0x88208000):
        return 'store'
    return None


def validate_exclusive(w, kind):
    rt, rn, rt2, rs = w & 31, (w >> 5) & 31, (w >> 10) & 31, (w >> 16) & 31
    pair = bool(w & (1 << 21))
    if kind == 'load' and pair and rt == rt2:
        raise ValueError('unpredictable exclusive pair destinations')
    if kind == 'store' and (rs == 31 or rs == rt or (pair and rs == rt2) or (rn != 31 and rs == rn)):
        raise ValueError('exclusive status register unavailable for store-free guard')


def relocate(w, original_pc, native_pc):
    target = direct_target(w, original_pc)
    if target is not None:
        if w & 0xfc000000 == 0x94000000:
            raise ValueError('BL requires an explicit native call gate')
        if w & 0xfc000000 == 0x14000000:
            return branch(native_pc, target)
        if w & 0x7e000000 == 0x36000000:
            return branch(native_pc, target, 14, 5, w & ~0x7ffe0)
        return branch(native_pc, target, 19, 5, w & ~0xffffe0)
    if w & 0x1f000000 == 0x10000000:
        imm = signed(((w >> 5) & 0x7ffff) * 4 + ((w >> 29) & 3), 21)
        page = bool(w & (1 << 31))
        target = ((original_pc & ~4095) + (imm << 12)) if page else original_pc + imm
        delta = (target >> 12) - (native_pc >> 12) if page else target - native_pc
        if not -(1 << 20) <= delta < (1 << 20):
            raise ValueError('ADR/ADRP relocation out of range')
        return (w & 0x9f00001f) | ((delta & 3) << 29) | (((delta >> 2) & 0x7ffff) << 5)
    if w & 0x3b000000 == 0x18000000:
        target = original_pc + 4 * signed((w >> 5) & 0x7ffff, 19)
        return branch(native_pc, target, 19, 5, w & ~0xffffe0)
    if w & 0xfe000000 == 0xd6000000:
        raise ValueError('indirect control in instruction patch')
    if operation(w) is not None:
        raise ValueError('system/exception operation in instruction patch')
    return w


def leaf_stub(address, pc, w, kind):
    mrs16, ldr16 = 0xd53bd050, 0xf9400210
    addhi16, addlo16 = 0x91400210, 0x91000210
    bookkeeping = [0xd10083ff, 0xa90047f0, mrs16, addhi16, addlo16, ldr16,
                   0x52800011 | ((1 if kind == EXLOAD else 0) << 5), 0xb9005211, 0xa8c247f0]
    if kind == EXLOAD:
        return bookkeeping + [w, branch(address + 40, pc + 4)] + [NOP] * 5
    if kind == CLEAR:
        return [w] + bookkeeping + [branch(address + 40, pc + 4)] + [NOP] * 5
    rs = (w >> 16) & 31
    guard = [0xd53bd040 | rs, 0x91400000 | (rs << 5) | rs, 0x91000000 | (rs << 5) | rs,
             0xf9400000 | (rs << 5) | rs, 0xb9405000 | (rs << 5) | rs,
             branch(address + 20, address + 80, 14, 5, 0x36000000 | rs), w]
    body = guard + bookkeeping + [branch(address + 64, pc + 4)] + [NOP] * 3
    return body + stub(address + 80, pc, pc + 4)


def stub(address, pc, continuation):
    return [0xd10083ff, 0xa90047f0, 0x58000091, 0x580000b0, 0xd61f0200, NOP,
            pc & 0xffffffff, pc >> 32, 0, 0,
            0xa8c247f0, branch(address + 44, continuation), NOP, NOP, NOP, NOP]


def atomic_capsules(words):
    out, interiors = {}, set()
    for pc, store in words.items():
        if exclusive(store) != 'store' or (store >> 16) & 31 != 31:
            continue
        load, cmp, cond = (words.get(pc - i, 0) for i in (12, 8, 4))
        rt, rn = load & 31, (load >> 5) & 31
        if (exclusive(load) != 'load' or load & (1 << 21) or store & (1 << 21) or
                load >> 30 not in (2, 3) or store >> 30 != load >> 30 or
                rt == 31 or rt == rn or store & 31 != 31 or (store >> 5) & 31 != rn or
                cmp & 0x7fe0fc1f != 0x6b00001f or (cmp >> 5) & 31 != rt or
                cmp >> 31 != (load >> 30) - 2 or
                cond & 0xff000010 != 0x54000000 or cond & 15 not in (0, 1)):
            raise ValueError(f'{pc:#x}: WZR exclusive store lacks a closed atomic capsule pattern')
        inside = {pc - 8, pc - 4, pc}
        target = direct_target(cond, pc - 4)
        if target not in words or target in inside or inside & interiors:
            raise ValueError('overlapping/self-entering atomic capsule')
        out[pc - 12] = (load, cmp, cond, store)
        interiors.update(inside)
    for pc, w in words.items():
        if direct_target(w, pc) in interiors:
            raise ValueError(f'{pc:#x}: direct control enters atomic capsule interior')
    return out, interiors


def plan_module(name, base, nso, table):
    off, length = nso.text
    if (off | length) & 3 or off + length > nso.size:
        raise ValueError(f'{name}: invalid executable range')
    words = {base + off + i: struct.unpack_from('<I', nso.image, off + i)[0] for i in range(0, length, 4)}
    capsules, interiors = atomic_capsules(words)
    for _, _, shndx, value, _ in nso.syms:
        if shndx and base + value in interiors:
            raise ValueError('exported entry enters atomic capsule interior')
    sites = {}
    for pc, w in words.items():
        try:
            op = (CAPSULE, 0) if pc in capsules else (INTERIOR, 0) if pc in interiors else operation(w)
            if exclusive(w) and pc not in interiors and pc not in capsules:
                validate_exclusive(w, exclusive(w))
        except ValueError as e:
            raise ValueError(f'{name}+{pc-base:#x}: {e}') from e
        if op:
            sites[pc] = dict(pc=pc, original=w, operation=op[0], argument=op[1], link=0, choices=[],
                             capsule=capsules.get(pc))
    for offset, alts in table.sites.get(name, {}).items():
        pc = base + offset
        if pc not in words or any(orig != words[pc] for _, _, orig in alts):
            raise ValueError(f'{name}+{offset:#x}: original patch word mismatch/outside text')
        if pc in sites:
            raise ValueError(f'{name}+{offset:#x}: patch collides with system rewrite')
        sites[pc] = dict(pc=pc, original=words[pc], operation=PATCH, argument=0, link=0, choices=alts)
    for offset, head in table.spins.get(name, {}).items():
        pc, target = base + offset, base + head
        if pc not in words or target not in words or patches.branch_target(words[pc], pc) != target or pc in sites:
            raise ValueError(f'{name}+{offset:#x}: invalid/colliding spin site')
        sites[pc] = dict(pc=pc, original=words[pc], operation=SPIN, argument=target, link=0, choices=[])
    return words, sites


def emit_module(name, base, nso, sites, arena, patch_page, interiors):
    output, result, choices = {}, [], []
    cursor = arena

    def put(addr, values):
        for i, w in enumerate(values):
            if addr + i * 4 in output:
                raise ValueError('native arena collision')
            output[addr + i * 4] = w

    def gate(site, continuation):
        nonlocal cursor
        addr = cursor
        cursor += 64
        put(addr, stub(addr, site['pc'], continuation))
        site['resume'] = addr + 40
        site['entry'] = addr
        result.append(site)
        return addr

    for pc, site in sorted(sites.items()):
        if site['operation'] == WORD:
            site.update(resume=0, entry=0)
            result.append(site)
            continue
        if site['operation'] == CAPSULE:
            load, cmp, cond, store = site['capsule']
            body = leaf_stub(cursor, pc, load, EXLOAD)[:10]
            body += [cmp, branch(cursor + 44, cursor + 52, 19, 5, (cond ^ 1) & ~0xffffe0),
                     branch(cursor + 48, direct_target(cond, pc + 8)), store]
            # Constrained WZR forms may be NOPs: keep monitor/state live after the unchanged word.
            body += [branch(cursor + 56, pc + 16)]
            site.update(entry=cursor, resume=0)
            put(cursor, body)
            cursor += len(body) * 4
            cursor = layout.align_up(cursor, 16)
            result.append(site)
            continue
        if site['operation'] in (EXLOAD, EXSTORE, CLEAR):
            kind = site['operation']
            body = leaf_stub(cursor, pc, site['original'], kind)
            site.update(entry=cursor, resume=cursor + 120 if kind == EXSTORE else 0)
            put(cursor, body)
            cursor += len(body) * 4
            result.append(site)
            continue
        if site['operation'] == SPIN:
            decision = cursor
            w = site['original']
            conditional = w & 0xfc000000 != 0x14000000
            if conditional:
                bits = 14 if w & 0x7e000000 == 0x36000000 else 19
                mask = 0x7ffe0 if bits == 14 else 0xffffe0
                put(cursor, [branch(cursor, cursor + 16, bits, 5, w & ~mask),
                             branch(cursor + 4, pc + 4), NOP, NOP])
                cursor += 16
            gate(site, site['argument'])
            site['entry'] = decision
            continue
        gate(site, pc + 4)
        alternatives = site['choices']
        site['choices'] = []
        if site['operation'] != PATCH:
            continue
        alternatives = alternatives + [(0xffffffff, [site['original']], site['original'])]
        for index, repl, _ in alternatives:
            body = cursor
            site['choices'].append((index, body))
            put(cursor, [0xa8c247f0])
            cursor += 4
            for k, w in enumerate(repl):
                w = patches.adrp(pc, patch_page, w[1]) if isinstance(w, tuple) else w
                if direct_target(w, pc) in interiors:
                    raise ValueError('patch control enters atomic capsule interior')
                if direct_target(w, pc) is not None and k != len(repl) - 1:
                    raise ValueError('branch before end of native patch')
                if w & 0xfc000000 == 0x94000000:
                    extra_pc = cursor
                    cursor += 4
                    extra = dict(pc=extra_pc, original=w, operation=CALL, argument=direct_target(w, pc),
                                 link=pc + 4, choices=[])
                    dest = gate(extra, pc + 4)
                    put(extra_pc, [w])
                    # rt_nce_load replaces this verified generated BL with its gate branch.
                    continue
                if exclusive(w):
                    raise ValueError('exclusive operation introduced by instruction patch')
                try:
                    relocated = [relocate(w, pc, cursor)]
                except ValueError:
                    target = direct_target(w, pc)
                    if target is None or w & 0x7c000000 == 0x14000000:
                        raise
                    if w & 0xff000010 == 0x54000000 and w & 15 >= 14:
                        relocated = [branch(cursor, target)]
                    else:
                        inverse = w ^ (1 if w & 0xff000010 == 0x54000000 else 1 << 24)
                        bits = 14 if w & 0x7e000000 == 0x36000000 else 19
                        mask = 0x7ffe0 if bits == 14 else 0xffffe0
                        relocated = [branch(cursor, cursor + 8, bits, 5, inverse & ~mask), branch(cursor + 4, target)]
                put(cursor, relocated)
                cursor += 4 * len(relocated)
            if w & 0xfc000000 != 0x94000000:
                put(cursor, [branch(cursor, pc + 4)])
                cursor += 4
            cursor = layout.align_up(cursor, 16)
    return sorted(result, key=lambda s: s['pc']), sorted(output.items()), cursor - arena


def validate_nso(raw, name):
    if len(raw) < 256 or raw[:4] != b'NSO0':
        raise ValueError(f'{name}: invalid NSO header')
    flags = struct.unpack_from('<I', raw, 12)[0]
    end = 0
    for i in range(3):
        file_offset, offset, size = struct.unpack_from('<III', raw, 16 + 16 * i)
        compressed_size = struct.unpack_from('<I', raw, 96 + 4 * i)[0]
        stored = compressed_size if flags & (1 << i) else size
        if file_offset < 256 or stored > len(raw) or file_offset > len(raw) - stored or offset < end:
            raise ValueError(f'{name}: invalid/overlapping NSO segments')
        end = offset + size
    bss = struct.unpack_from('<I', raw, 60)[0]
    if end + bss >= (1 << 32):
        raise ValueError(f'{name}: oversized NSO image')


def generate(exefs, out, expected, patch_path=None):
    from nso import Nso
    out = Path(out).resolve()
    if not any(out.is_relative_to(ROOT / root) for root in ('generated', 'build')):
        raise ValueError('native output must stay under ignored generated/ or build/')
    if subprocess.run(['git', 'check-ignore', '-q', str(out / 'nce_generated.c')], cwd=ROOT).returncode:
        raise ValueError('native output is not ignored')
    hashes = json.loads(Path(expected).read_text())['exefs_sha256']
    images = {}
    for name in layout.ORDER:
        p = Path(exefs) / name
        if not p.exists():
            continue
        raw = p.read_bytes()
        if name not in hashes or hashlib.sha256(raw).hexdigest() != hashes[name]:
            raise ValueError(f'{name}: original NSO SHA-256 mismatch')
        validate_nso(raw, name)
        images[name] = Nso(str(p))
    if 'main' not in images:
        raise ValueError('main module missing')
    mods = layout.layout(exefs)
    table = patches.load(patch_path, mods) if patch_path else patches.Table()
    all_words, all_sites, plans = {}, {}, {}
    for name, base, _ in mods:
        words, sites = plan_module(name, base, images[name], table)
        all_words.update(words)
        all_sites.update(sites)
        plans[name] = sites
    for pc, w in all_words.items():
        if w & 0x3b000000 == 0x18000000:
            opc, vector = w >> 30, bool(w & (1 << 26))
            if opc != 3:
                width = (4 << opc) if vector else (8 if opc == 1 else 4)
                target = pc + signed((w >> 5) & 0x7ffff, 19) * 4
                if any(target + i in all_sites for i in range(0, width, 4)):
                    raise ValueError('rewrite collides with referenced original literal data')
        if exclusive(w) and pc not in all_sites:
            raise ValueError(f'{pc:#x}: unguarded original exclusive operation')
    interiors = {pc for pc, s in all_sites.items() if s['operation'] == INTERIOR}
    for pc, w in all_words.items():
        if direct_target(w, pc) in interiors:
            raise ValueError('cross-module direct control enters atomic capsule interior')
    sizes = {n: sum((0 if s['operation'] == WORD else 144 if s['operation'] == EXSTORE else
                      96 if s['operation'] == CAPSULE else 64) +
                     (160 + sum(160 + len(r) * 8 for _, r, _ in s['choices']) if s['operation'] in (PATCH, SPIN) else 0)
                    for s in plans[n].values()) for n, _, _ in mods}
    arenas = layout.nce_arenas(mods, sizes)
    lines = [f'// {GENERATED_MARKER}: original native rewrite metadata. Do not edit or commit.', '#include "nce.h"', '']
    descriptions = []
    for (name, base, size), (_, arena, arena_size) in zip(mods, arenas):
        sites, words, used = emit_module(name, base, images[name], plans[name], arena, table.page, interiors)
        if used > arena_size:
            raise ValueError(f'{name}: native arena size estimate exceeded')
        for i, s in enumerate(sites):
            if s['choices']:
                lines.append(f'static const NceChoice {name}_choices_{i}[] = {{')
                lines += [f' {{{k}u, {a:#x}ull}},' for k, a in s['choices']]
                lines.append('};')
        lines.append(f'static const NceSite {name}_sites[] = {{')
        for i, s in enumerate(sites):
            cp = f'{name}_choices_{i}' if s['choices'] else 'NULL'
            lines.append(f' {{{s["pc"]:#x}ull, {s["resume"]:#x}ull, {s["entry"]:#x}ull, {s["original"]:#x}u, '
                         f'{s["operation"]}, {s["argument"]:#x}ull, {s["link"]:#x}ull, {cp}, {len(s["choices"])} }},')
        lines += ['};', f'static const uint32_t {name}_words[] = {{']
        sparse = dict(words)
        packed = [sparse.get(pc, 0xd4200000) for pc in range(arena, arena + used, 4)]
        lines += [' ' + ','.join(f'{w:#x}u' for w in packed[i:i + 8]) + ',' for i in range(0, len(packed), 8)]
        lines += ['};', '']
        off, length = images[name].text
        counts = {op: sum(s['operation'] == op for s in sites) for op in (EXLOAD, EXSTORE, CLEAR, CAPSULE, INTERIOR)}
        print(f'{name}: sites={len(sites)}, arena={arena_size} bytes, leaf exclusives={counts}, '
              f'C gates={sum(s["operation"] not in (EXLOAD, EXSTORE, CLEAR, CAPSULE, WORD) for s in sites)}')
        descriptions.append(f' {{"{name}", "{hashes[name]}", {base:#x}ull, {size:#x}ull, {off:#x}, {length:#x}, {arena:#x}ull, '
                            f'{arena_size:#x}, {name}_sites, {len(sites)}, {name}_words, {len(packed)} }},')
    lines += ['const NceModule rt_nce_modules[] = {', *descriptions, '};',
              f'const unsigned rt_nce_module_count = {len(mods)};', '']
    out.mkdir(parents=True, exist_ok=True)
    layout.write(mods, out)
    metadata = json.loads((out / 'layout.json').read_text())
    for m, (_, arena, size) in zip(metadata, arenas):
        m.update(_generated=GENERATED_MARKER, nce_arena=hex(arena), nce_arena_size=hex(size))
    (out / 'layout.json').write_text(json.dumps(metadata, indent=1) + '\n')
    header = (out / 'layout.h').read_text()
    (out / 'layout.h').write_text(f'// {GENERATED_MARKER}\n' + header)
    (out / 'patches.h').write_text(patches.header_text(table, GENERATED_MARKER))
    (out / 'nce_generated.c').write_text('\n'.join(lines))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--exefs', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--expected', required=True)
    p.add_argument('--patches')
    a = p.parse_args()
    try:
        generate(a.exefs, a.out, a.expected, a.patches)
    except (ValueError, AssertionError, KeyError, OSError) as e:
        p.exit(1, f'nce: {e}\n')


if __name__ == '__main__':
    main()
