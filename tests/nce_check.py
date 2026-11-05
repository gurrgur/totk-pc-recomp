"""Native execution checks without game data: instruction rewrites under Unicorn, prepare_nce.py output, and the
mod and loader checks compiled from runtime sources. Run with .venv/bin/python tests/nce_check.py."""
from pathlib import Path
import struct
import contextlib
import hashlib
import io
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'recomp')]
import prepare_nce as n
import patches
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r

TEXT, ARENA, TLS, FRAME, STACK, DATA, BRIDGE = 0x100000, 0x200000, 0x300000, 0x301000, 0x500000, 0x600000, 0x700000


def assemble(text):
    directory = ROOT / 'build/nce-linux'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        p = Path(tmp)
        (p / 'test.S').write_text('.text\n' + text + '\n')
        subprocess.run(['clang', '--target=aarch64-linux-gnu', '-c', str(p / 'test.S'), '-o', str(p / 'test.o')], check=True, capture_output=True)
        subprocess.run(['llvm-objcopy', '-O', 'binary', '--only-section=.text', str(p / 'test.o'), str(p / 'test.bin')], check=True)
        raw = (p / 'test.bin').read_bytes()
        return list(struct.unpack('<' + 'I' * (len(raw) // 4), raw))


def capsule_code():
    code = assemble('ldaxr x9, [x8]\ncmp x9, x19\nb.ne 1f\nstlxr w10, xzr, [x8]\n1: nop')
    code[3] = (code[3] & ~(31 << 16)) | (31 << 16)
    return code


def plan(code, table=None):
    image = struct.pack('<' + 'I' * len(code), *code)
    nso = SimpleNamespace(text=(0, len(image)), size=0x1000, image=image, syms=[])
    words, sites = n.plan_module('main', TEXT, nso, table or patches.Table())
    interiors = {pc for pc, s in sites.items() if s['operation'] == n.INTERIOR}
    output, arena, _ = n.emit_module('main', TEXT, nso, sites, ARENA, 0, interiors)
    return output, arena


def machine(code, table=None):
    sites, arena = plan(code, table)
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for address, size in ((TEXT, 0x10000), (ARENA, 0x10000), (TLS, 0x10000), (STACK, 0x10000), (DATA, 0x10000), (BRIDGE, 0x1000)):
        u.mem_map(address, size)
    u.mem_write(TEXT, struct.pack('<' + 'I' * len(code), *code))
    for pc, w in arena:
        u.mem_write(pc, struct.pack('<I', w))
    for s in sites:
        op, entry = s['operation'], s['entry']
        if op == n.WORD:
            u.mem_write(s['pc'], struct.pack('<I', s['argument']))
            continue
        if s['resume']:
            u.mem_write(s['resume'] - 8, struct.pack('<Q', BRIDGE))
        # Independent test relocation: static TLS variable at TP + 16.
        starts = {n.EXLOAD: (12,), n.EXSTORE: (4, 40), n.CLEAR: (16,), n.CAPSULE: (12,)}.get(op, ())
        for offset in starts:
            addr = entry + offset + 4
            value = struct.unpack('<I', u.mem_read(addr, 4))[0]
            u.mem_write(addr, struct.pack('<I', value | (16 << 10)))
        u.mem_write(s['pc'], struct.pack('<I', n.branch(s['pc'], entry)))
    u.mem_write(TLS + 16, struct.pack('<Q', FRAME))
    u.mem_write(FRAME + 80, struct.pack('<I', 2))
    u.reg_write(r.UC_ARM64_REG_TPIDR_EL0, TLS)
    u.reg_write(r.UC_ARM64_REG_SP, STACK + 0x8000)
    for i in range(31):
        u.reg_write(getattr(r, f'UC_ARM64_REG_X{i}'), 0x12340000 + 0x1000 * i + i)
    for i in range(32):
        u.reg_write(getattr(r, f'UC_ARM64_REG_Q{i}'), (i + 1) * 0x123456789abcdef123456789abc)
    u.reg_write(r.UC_ARM64_REG_NZCV, 0xa0000000)
    u.reg_write(r.UC_ARM64_REG_FPCR, 0x1c00000)
    u.reg_write(r.UC_ARM64_REG_FPSR, 0x800009f)
    u.reg_write(r.UC_ARM64_REG_X8, DATA)
    u.reg_write(r.UC_ARM64_REG_X0, 0x1122334455667788)
    u.mem_write(DATA, struct.pack('<QQ', 7, 9))
    return u, sites


def snapshot(u):
    ids = [getattr(r, f'UC_ARM64_REG_X{i}') for i in range(31)]
    ids += [getattr(r, f'UC_ARM64_REG_Q{i}') for i in range(32)]
    ids += [r.UC_ARM64_REG_SP, r.UC_ARM64_REG_NZCV, r.UC_ARM64_REG_FPCR, r.UC_ARM64_REG_FPSR, r.UC_ARM64_REG_TPIDR_EL0]
    return {i: u.reg_read(i) for i in ids}


def source_function(source, signature):
    """The text of one C function definition in source, found by the start of its signature."""
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


def fixture(out):
    """Write a synthetic NSO, its prepared native metadata and nce_fixture.h under build/; returns generated/."""
    out = Path(out).resolve()
    if not out.is_relative_to(ROOT / 'build'):
        raise ValueError('fixture output must stay under build/')
    exefs = out / 'input'
    exefs.mkdir(parents=True, exist_ok=True)
    image = bytearray(struct.pack('<I', n.NOP) * (0x3000 // 4))
    struct.pack_into('<I', image, 4, 0x1000)
    image[0x1000:0x1004] = b'MOD0'
    struct.pack_into('<6i', image, 0x1004, 0x100, 0x2000, 0x2000, 0, 0, 0)
    image[0x1100:0x1110] = bytes(16)
    def put(offset, text):
        words = assemble(text)
        image[offset:offset + len(words) * 4] = struct.pack('<' + 'I' * len(words), *words)
        return words
    entry = put(0x40, 'stp x29, x30, [sp, #-16]!\nmov x29, sp\n'
                'mrs x18, tpidr_el0\nsvc #0x77\nldr x16, 1f\nblr x16\n'
                'mrs x0, tpidr_el0\nmrs x1, cntpct_el0\nldp x29, x30, [sp], #16\nret\n'
                '.p2align 3\n1: .quad 0x7020000040')
    put(0x100, 'svc #0x78\nldaxr x9, [x8]\nstlxr w10, x0, [x8]\nret')
    patch_word = put(0x180, 'mov w0, #3\nret')[0]
    words = put(0x200, 'ldaxr x9, [x8]\ncmp x9, x19\nb.ne 1f\nstlxr w10, xzr, [x8]\n'
                '1: stlxr w10, x0, [x8]\nret')
    words[3] = (words[3] & ~(31 << 16)) | (31 << 16)
    struct.pack_into('<I', image, 0x20c, words[3])
    words32 = put(0x240, 'ldaxr w9, [x8]\ncmp w9, w19\nb.ne 1f\nstlxr w10, wzr, [x8]\n'
                  '1: stlxr w10, w0, [x8]\nret')
    words32[3] = (words32[3] & ~(31 << 16)) | (31 << 16)
    struct.pack_into('<I', image, 0x24c, words32[3])
    put(0x300, 'svc #0x79\nret')
    put(0x340, 'ldaxr x9, [x8]\nsvc #0x7a\nstlxr w10, x0, [x8]\nret')
    put(0x380, 'ldaxr x9, [x8]\nsvc #0x7a\nldaxr x9, [x8]\nstlxr w10, x0, [x8]\nret')
    put(0x400, 'ldaxr x9, [x8]\nmov w10, #1\nstlr w10, [x11]\n'
               '1: ldar w12, [x11]\ncmp w12, #2\nb.ne 1b\nstlxr w10, x0, [x8]\nret')
    put(0x480, '1: ldaxr x9, [x8]\nadd x9, x9, #1\nstlxr w10, x9, [x8]\ncbnz w10, 1b\nret')
    variants = [(s, reg, a, l) for s, reg in (('b', 'w'), ('h', 'w'), ('', 'w'), ('', 'x'))
                for a, l in (('', ''), ('a', 'l'))]
    for i, (suffix, reg, a, l) in enumerate(variants):
        put(0x500 + i * 0x20, f'ld{a}xr{suffix} {reg}9, [x8]\nst{l}xr{suffix} w10, {reg}0, [x8]\nret')
    for i, (reg, a, l) in enumerate((reg, a, l) for reg in ('w', 'x') for a, l in (('', ''), ('a', 'l'))):
        put(0x600 + i * 0x20, f'ld{a}xp {reg}9, {reg}11, [x8]\nst{l}xp w10, {reg}0, {reg}1, [x8]\nret')
    for status in range(31):
        base, data = (7 if status == 8 else 8), (1 if status == 0 else 0)
        body = f'ldaxr x9, [x{base}]\nstlxr w{status}, x{data}, [x{base}]\n'
        if status == 30:
            body = 'stp x29, x30, [sp, #-16]!\n' + body + 'mov w10, w30\nldp x29, x30, [sp], #16\n'
        put(0x700 + status * 0x20, body + 'ret')
    put(0xb40, '1: ldaxr x9, [x8]\ncmp x9, x1\nb.ne 1b\nstlxr w10, x0, [x8]\ncbnz w10, 1b\nret')
    put(0xb80, '1: cbz x1, 2f\nsub x1, x1, #1\nb 1b\n2: ret')
    put(0xc00, 'wfe\nret')
    raw = bytearray(0x100) + image
    raw[:4] = b'NSO0'
    for i in range(3):
        struct.pack_into('<III', raw, 0x10 + i * 16, 0x100 + i * 0x1000, i * 0x1000, 0x1000)
        struct.pack_into('<I', raw, 0x60 + i * 4, 0x1000)
    (exefs / 'main').write_bytes(raw)
    expected = out / 'expected.json'
    expected.write_text(json.dumps({'exefs_sha256': {'main': hashlib.sha256(raw).hexdigest()}}))
    repl = assemble('mov w0, #7\nadd w0, w0, #2')
    patch_file = out / 'patches.txt'
    patch_file.write_text(f'set fixture TEST_PATCH 0\nsite fixture main 180 {patch_word:08x} ' + ' '.join(f'{w:08x}' for w in repl) + '\nspin main b40 b48 b50\nspin main b80 b88\nspin main 480 48c\n')
    target = out / 'generated'
    n.generate(exefs, target, expected, patch_file)
    values = ','.join(str(b) for b in image)
    (target / 'nce_fixture.h').write_text(f'/* {n.GENERATED_MARKER}: synthetic test image, not game data. */\n'
                                      '#define FIXTURE_BASE 0x7000000000ull\n'
                                      f'#define FIXTURE_SHA256 "{hashlib.sha256(raw).hexdigest()}"\n'
                                      f'static const unsigned char fixture_image[] = {{{values}}};\n')
    return target


class NativeChecks(unittest.TestCase):
    def test_single_widths(self):
        for suffix, reg in (('b', 'w'), ('h', 'w'), ('', 'w'), ('', 'x')):
            for acquire, release in (('', ''), ('a', 'l')):
                with self.subTest(suffix=suffix, reg=reg, acquire=acquire):
                    code = assemble(f'ld{acquire}xr{suffix} {reg}9, [x8]\nst{release}xr{suffix} w10, {reg}0, [x8]')
                    u, _ = machine(code)
                    before = snapshot(u)
                    u.emu_start(TEXT, TEXT + 8, count=200)
                    after = snapshot(u)
                    self.assertEqual(u.reg_read(r.UC_ARM64_REG_X10), 0)
                    self.assertEqual(u.reg_read(r.UC_ARM64_REG_X9), 7)
                    for regid in before.keys() - {r.UC_ARM64_REG_X9, r.UC_ARM64_REG_X10}:
                        self.assertEqual(after[regid], before[regid], str(regid))
                    bits = 8 if suffix == 'b' else 16 if suffix == 'h' else 32 if reg == 'w' else 64
                    got = int.from_bytes(u.mem_read(DATA, bits // 8), 'little')
                    self.assertEqual(got, before[r.UC_ARM64_REG_X0] & ((1 << bits) - 1))
                    self.assertEqual(int.from_bytes(u.mem_read(FRAME + 80, 4), 'little'), 0)

    def test_all_borrowable_status_registers(self):
        for status in range(31):
            base = 7 if status == 8 else 8
            data = 1 if status == 0 else 0
            code = assemble(f'ldaxr x9, [x{base}]\nstlxr w{status}, x{data}, [x{base}]')
            u, _ = machine(code)
            u.reg_write(getattr(r, f'UC_ARM64_REG_X{base}'), DATA)
            before = snapshot(u)
            u.emu_start(TEXT, TEXT + 8, count=200)
            self.assertEqual(u.reg_read(getattr(r, f'UC_ARM64_REG_X{status}')), 0)
            for regid, value in before.items():
                if regid not in (r.UC_ARM64_REG_X9, getattr(r, f'UC_ARM64_REG_X{status}')):
                    self.assertEqual(u.reg_read(regid), value)

    def test_pairs(self):
        for reg in ('w', 'x'):
            for a, l in (('', ''), ('a', 'l')):
                code = assemble(f'ld{a}xp {reg}9, {reg}11, [x8]\nst{l}xp w10, {reg}0, {reg}1, [x8]')
                u, _ = machine(code)
                before = snapshot(u)
                u.emu_start(TEXT, TEXT + 8, count=200)
                self.assertEqual(u.reg_read(r.UC_ARM64_REG_X10), 0)
                after = snapshot(u)
                for regid in before.keys() - {r.UC_ARM64_REG_X9, r.UC_ARM64_REG_X10, r.UC_ARM64_REG_X11}:
                    self.assertEqual(after[regid], before[regid])

    def test_lost_and_empty_fail_before_store(self):
        code = assemble('stlxr w10, x0, [x8]')
        for state in (0, 2):
            u, _ = machine(code)
            u.mem_write(FRAME + 80, struct.pack('<I', state))
            reached = []
            def stop(uc, pc, size, _):
                if pc == BRIDGE:
                    reached.append(pc)
                    uc.emu_stop()
            u.hook_add(UC_HOOK_CODE, stop)
            u.emu_start(TEXT, TEXT + 4, count=200)
            self.assertEqual(reached, [BRIDGE])
            self.assertEqual(int.from_bytes(u.mem_read(DATA, 8), 'little'), 7)

    def test_abandoned_call_indirect_then_reload(self):
        code = assemble('ldaxr x9, [x8]\nblr x12\nbr x13\nldaxr x9, [x8]\nstlxr w10, x0, [x8]')
        u, _ = machine(code)
        u.reg_write(r.UC_ARM64_REG_X12, TEXT + 0x100)
        u.reg_write(r.UC_ARM64_REG_X13, TEXT + 12)
        u.mem_write(TEXT + 0x100, struct.pack('<I', 0xd65f03c0))
        def call(uc, pc, size, _):
            if pc == TEXT + 0x100:
                uc.mem_write(FRAME + 80, struct.pack('<I', 2))
        u.hook_add(UC_HOOK_CODE, call)
        u.emu_start(TEXT, TEXT + 20, count=500)
        self.assertEqual(u.reg_read(r.UC_ARM64_REG_X10), 0)

    def test_clrex_consumes_without_corrupting_state(self):
        code = assemble('ldaxr x9, [x8]\nclrex')
        u, _ = machine(code)
        before = snapshot(u)
        u.emu_start(TEXT, TEXT + 8, count=200)
        self.assertEqual(int.from_bytes(u.mem_read(FRAME + 80, 4), 'little'), 0)
        for regid, value in before.items():
            if regid != r.UC_ARM64_REG_X9:
                self.assertEqual(u.reg_read(regid), value)

    def test_native_spurious_failure_then_retry(self):
        code = assemble('ldaxr x9, [x8]\nstlxr w10, x0, [x8]')
        u, _ = machine(code)
        u.emu_start(TEXT, TEXT + 4, count=100)
        u.mem_write(TEXT + 0x100, struct.pack('<I', 0xd5033f5f))
        u.emu_start(TEXT + 0x100, TEXT + 0x104, count=1)
        u.emu_start(TEXT + 4, TEXT + 8, count=100)
        self.assertEqual(u.reg_read(r.UC_ARM64_REG_X10), 1)
        u.emu_start(TEXT, TEXT + 8, count=200)
        self.assertEqual(u.reg_read(r.UC_ARM64_REG_X10), 0)

    def test_closed_capsule_match_mismatch_and_interior_guards(self):
        code = capsule_code()
        for match in (True, False):
            u, sites = machine(code)
            u.reg_write(r.UC_ARM64_REG_X19, 7 if match else 8)
            before = snapshot(u)
            u.emu_start(TEXT, TEXT + 16, count=200)
            self.assertEqual(int.from_bytes(u.mem_read(DATA, 8), 'little'), 0 if match else 7)
            for regid, value in before.items():
                if regid not in (r.UC_ARM64_REG_X9, r.UC_ARM64_REG_NZCV):
                    self.assertEqual(u.reg_read(regid), value)
            self.assertEqual(u.reg_read(r.UC_ARM64_REG_NZCV), 0x60000000 if match else 0x80000000)
        for offset in (4, 8, 12):
            u, _ = machine(code)
            reached = []
            def stop(uc, pc, size, _):
                if pc == BRIDGE:
                    reached.append(pc)
                    uc.emu_stop()
            u.hook_add(UC_HOOK_CODE, stop)
            u.emu_start(TEXT + offset, TEXT + 16, count=100)
            self.assertEqual(reached, [BRIDGE])
            self.assertEqual(int.from_bytes(u.mem_read(DATA, 8), 'little'), 7)

    def test_capsule_negative_shapes_and_entries(self):
        code = capsule_code()
        for position, replacement in ((1, n.NOP), (3, code[3] ^ (1 << 5)), (0, code[0] ^ (1 << 30))):
            changed = code.copy(); changed[position] = replacement
            with self.assertRaises(ValueError): plan(changed)
        for interior in (4, 8, 12):
            with self.assertRaises(ValueError): plan(code + [n.branch(TEXT + 20, TEXT + interior)])
        table = patches.Table([('test', 'TEST', 0)], {'main': {12: [(0, [n.NOP], code[3])]}})
        with self.assertRaises(ValueError): plan(code, table)
        image = struct.pack('<' + 'I' * len(code), *code)
        nso = SimpleNamespace(text=(0, len(image)), size=0x1000, image=image, syms=[('bad', 2, 1, 4, 0)])
        with self.assertRaisesRegex(ValueError, 'exported entry'):
            n.plan_module('main', TEXT, nso, patches.Table())
        with self.assertRaises(ValueError): n.branch(ARENA, ARENA + (1 << 27))

    def test_patch_control_rejects_every_capsule_interior(self):
        code = capsule_code() + [n.NOP]
        pc = TEXT + 20
        forms = ('b TARGET', 'bl TARGET') + tuple(f'b.{cc} TARGET' for cc in
                ('eq', 'ne', 'hs', 'lo', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'al', 'nv')) + (
                'cbz w2, TARGET', 'cbz x2, TARGET', 'cbnz w2, TARGET', 'cbnz x2, TARGET',
                'tbz w2, #5, TARGET', 'tbz x2, #40, TARGET', 'tbnz w2, #5, TARGET', 'tbnz x2, #40, TARGET')
        for form in forms:
            for prefix in ([], [n.NOP]):
                for offset in (4, 8, 12):
                    word = assemble(form.replace('TARGET', f'.{TEXT + offset - pc:+d}'))[0]
                    self.assertEqual(n.direct_target(word, pc), TEXT + offset)
                    table = patches.Table([('first', 'FIRST', 0), ('disabled', 'DISABLED', 0)],
                                          {'main': {20: [(0, [n.NOP], n.NOP), (1, prefix + [word], n.NOP)]}})
                    with self.subTest(form=form, prefix=prefix, interior=offset):
                        with self.assertRaisesRegex(ValueError, 'patch control enters'):
                            plan(code, table)
                for target in (TEXT, TEXT + 16, TEXT + 0x1000):
                    word = assemble(form.replace('TARGET', f'.{target - pc:+d}'))[0]
                    table = patches.Table([('external', 'EXTERNAL', 1)],
                                          {'main': {20: [(0, prefix + [word], n.NOP)]}})
                    sites, arena = plan(code, table)
                    if form.startswith('bl '):
                        call = next(s for s in sites if s['operation'] == n.CALL)
                        self.assertEqual((call['argument'], call['link']), (target, pc + 4))
                    else:
                        site = next(s for s in sites if s['pc'] == pc)
                        address = site['choices'][0][1] + 4 + len(prefix) * 4
                        words = dict(arena)
                        relocated = n.direct_target(words[address], address)
                        if relocated == address + 8:
                            relocated = n.direct_target(words[address + 4], address + 4)
                        self.assertEqual(relocated, target)
                        for flags, value in ((0, 0), (0xf0000000, (1 << 40) | (1 << 5) | 1)):
                            results = []
                            for oracle in (True, False):
                                u, _ = machine(code, table)
                                if oracle:
                                    u.mem_write(pc, struct.pack('<I', word))
                                u.reg_write(r.UC_ARM64_REG_NZCV, flags)
                                u.reg_write(r.UC_ARM64_REG_X2, value)
                                reached = []
                                def stop(uc, destination, size, _):
                                    if destination in (target, pc + 4):
                                        reached.append(destination)
                                        uc.emu_stop()
                                u.hook_add(UC_HOOK_CODE, stop)
                                u.emu_start(pc if oracle else site['choices'][0][1], 0, count=100)
                                self.assertEqual(len(reached), 1)
                                results.append(reached[0])
                            self.assertEqual(results[0], results[1], (form, flags, value))

    def test_spin_decides_before_c_and_keeps_private_target(self):
        forms = ('b TARGET',) + tuple(f'b.{cc} TARGET' for cc in
                ('eq', 'ne', 'hs', 'lo', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'al', 'nv')) + (
                'cbz w2, TARGET', 'cbz x2, TARGET', 'cbnz w2, TARGET', 'cbnz x2, TARGET',
                'tbz w2, #5, TARGET', 'tbz x2, #40, TARGET', 'tbnz w2, #5, TARGET', 'tbnz x2, #40, TARGET')
        for form in forms:
            word = assemble(form.replace('TARGET', '.-4'))[0]
            code = [n.NOP, word, n.NOP]
            table = patches.Table(spins={'main': {4: 0}})
            for flags, value in ((0, 0), (0xf0000000, (1 << 40) | (1 << 5) | 1), (0x20000000, 1 << 40)):
                destinations = []
                for oracle in (True, False):
                    u, sites = machine(code, None if oracle else table)
                    u.reg_write(r.UC_ARM64_REG_NZCV, flags)
                    u.reg_write(r.UC_ARM64_REG_X2, value)
                    u.mem_write(FRAME + 80, struct.pack('<I', 1))
                    before = snapshot(u)
                    reached, calls = [], []
                    spin = None if oracle else next(s for s in sites if s['operation'] == n.SPIN)
                    def stop(uc, pc, size, _):
                        if pc in (TEXT, TEXT + 8):
                            reached.append(pc)
                            uc.emu_stop()
                        elif pc == BRIDGE:
                            self.assertIsNotNone(spin)
                            calls.append(pc)
                            uc.mem_write(FRAME + 80, struct.pack('<I', 2))
                            uc.reg_write(r.UC_ARM64_REG_NZCV, flags ^ 0xf0000000)
                            uc.reg_write(r.UC_ARM64_REG_X2, value ^ ((1 << 40) | (1 << 5) | 1))
                            uc.reg_write(r.UC_ARM64_REG_PC, spin['resume'])
                    u.hook_add(UC_HOOK_CODE, stop)
                    u.emu_start(TEXT + 4, 0, count=200)
                    self.assertEqual(len(reached), 1)
                    destinations.append(reached[0])
                    expected_calls = int(not oracle and reached[0] == TEXT)
                    self.assertEqual(len(calls), expected_calls, form)
                    self.assertEqual(int.from_bytes(u.mem_read(FRAME + 80, 4), 'little'), 2 if calls else 1)
                    expected = before.copy()
                    if calls:
                        expected[r.UC_ARM64_REG_NZCV] ^= 0xf0000000
                        expected[r.UC_ARM64_REG_X2] ^= (1 << 40) | (1 << 5) | 1
                    self.assertEqual(snapshot(u), expected, form)
                self.assertEqual(destinations[0], destinations[1], (form, flags, value))
        bad = assemble('bl .-4')[0]
        with self.assertRaisesRegex(ValueError, 'spin site'):
            plan([n.NOP, bad, n.NOP], patches.Table(spins={'main': {4: 0}}))

    def test_spin_exclusive_fallthrough_taken_and_retry_progress(self):
        code = assemble('1: ldaxr x9, [x8]\ncmp x9, x1\nb.ne 1b\nstlxr w10, x0, [x8]\ncbnz w10, 1b')
        table = patches.Table(spins={'main': {8: 0, 16: 0}})
        for initial, interrupt in ((7, False), (8, False), (7, True)):
            u, sites = machine(code, table)
            u.reg_write(r.UC_ARM64_REG_X0, 11)
            u.reg_write(r.UC_ARM64_REG_X1, 7)
            u.mem_write(DATA, struct.pack('<Q', initial))
            spins = {s['pc']: s for s in sites if s['operation'] == n.SPIN}
            calls, failures = [], []
            def gate(uc, pc, size, _):
                if pc != BRIDGE:
                    return
                site = uc.reg_read(r.UC_ARM64_REG_X17)
                if site not in spins:
                    failures.append(site)
                    uc.emu_stop()
                    return
                calls.append(site)
                uc.mem_write(FRAME + 80, struct.pack('<I', 2))
                uc.mem_write(DATA, struct.pack('<Q', 7))
                uc.reg_write(r.UC_ARM64_REG_NZCV, 0x40000000)
                uc.reg_write(r.UC_ARM64_REG_PC, spins[site]['resume'])
            u.hook_add(UC_HOOK_CODE, gate)
            if interrupt:
                u.emu_start(TEXT, TEXT + 12, count=300)
                self.assertFalse(calls)
                u.mem_write(TEXT + 0x100, struct.pack('<I', 0xd5033f5f))
                u.emu_start(TEXT + 0x100, TEXT + 0x104, count=1)
            u.emu_start(TEXT + 12 if interrupt else TEXT, TEXT + 20, count=1000)
            self.assertFalse(failures)
            self.assertEqual(len(calls), int(initial != 7 or interrupt))
            self.assertEqual(u.reg_read(r.UC_ARM64_REG_X10), 0)
            self.assertEqual(int.from_bytes(u.mem_read(DATA, 8), 'little'), 11)
            self.assertEqual(int.from_bytes(u.mem_read(FRAME + 80, 4), 'little'), 0)

    def test_status_aliases_rejected_and_ordering_not_exclusive(self):
        store = assemble('stlxr w10, x0, [x8]')[0]
        for status in (0, 8, 31):
            with self.assertRaises(ValueError): n.validate_exclusive((store & ~(31 << 16)) | status << 16, 'store')
        for w in assemble('ldar x9, [x8]\nstlr x0, [x8]'):
            self.assertIsNone(n.exclusive(w))

    def test_system_negative_and_register_directions(self):
        for w in assemble('mrs x0, tpidr_el0\nmsr tpidr_el0, x18\nmrs x17, cntvct_el0'):
            self.assertIsNotNone(n.operation(w))
        for w in assemble('svc #0x21\nbrk #2'):
            self.assertIsNotNone(n.operation(w))
        for w in assemble('msr tpidrro_el0, x0\nmsr sctlr_el1, x0\nwfi'):
            with self.assertRaises(ValueError): n.operation(w)


class PreparationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'build/nce-linux')
        cls.path = Path(cls.temporary.name)
        with contextlib.redirect_stdout(io.StringIO()):
            fixture(cls.path)
        cls.raw = (cls.path / 'input/main').read_bytes()
        cls.expected = (cls.path / 'expected.json').read_text()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_output_contract_and_markers(self):
        output = self.path / 'generated'
        for name in ('layout.json', 'layout.h', 'patches.h', 'nce_generated.c', 'nce_fixture.h'):
            self.assertIn(n.GENERATED_MARKER, (output / name).read_text())
        self.assertEqual(n.layout.read(output), [('main', n.layout.FIRST_BASE, 0x3000)])
        self.assertFalse(list(output.glob('main/*.c')))
        self.assertIn(hashlib.sha256(self.raw).hexdigest(), (output / 'nce_generated.c').read_text())

    def test_original_identity_and_output_boundary_reject(self):
        source = self.path / 'input/main'
        try:
            source.write_bytes(self.raw[:-1] + bytes([self.raw[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                n.generate(source.parent, self.path / 'bad-output', self.path / 'expected.json')
        finally:
            source.write_bytes(self.raw)
        with self.assertRaisesRegex(ValueError, 'ignored generated/'):
            n.generate(source.parent, ROOT / 'tests/native-output', self.path / 'expected.json')

    def test_literal_rewrite_collision_fails(self):
        source = self.path / 'input/main'
        changed = bytearray(self.raw)
        struct.pack_into('<II', changed, 0x600, 0x58000020, assemble('svc #0x45')[0])
        expected = self.path / 'expected.json'
        try:
            source.write_bytes(changed)
            expected.write_text(json.dumps({'exefs_sha256': {'main': hashlib.sha256(changed).hexdigest()}}))
            with self.assertRaisesRegex(ValueError, 'literal data'):
                n.generate(source.parent, self.path / 'bad-literal', expected)
        finally:
            source.write_bytes(self.raw)
            expected.write_text(self.expected)

    def test_patch_cannot_enter_another_modules_capsule(self):
        source = self.path / 'input/main'
        sdk = source.parent / 'sdk'
        expected = self.path / 'expected.json'
        patch_file = self.path / 'cross-module.txt'
        sdk_base = n.layout.FIRST_BASE + n.layout.ALIGN
        target = sdk_base + 0x204
        word = n.branch(n.layout.FIRST_BASE + 0xe00, target)
        patch_file.write_text(f'set external EXTERNAL 0\nsite external main e00 {n.NOP:08x} {word:08x}\n')
        try:
            sdk.write_bytes(self.raw)
            hashes = {'main': hashlib.sha256(self.raw).hexdigest(), 'sdk': hashlib.sha256(self.raw).hexdigest()}
            expected.write_text(json.dumps({'exefs_sha256': hashes}))
            with self.assertRaisesRegex(ValueError, 'patch control enters'):
                n.generate(source.parent, self.path / 'cross-output', expected, patch_file)
        finally:
            sdk.unlink()
            expected.write_text(self.expected)

    def test_nso_bounds_and_near_arena_collision(self):
        changed = bytearray(self.raw)
        struct.pack_into('<I', changed, 0x34, 0)
        with self.assertRaises(ValueError): n.validate_nso(changed, 'fixture')
        mods = [('main', n.layout.FIRST_BASE, 0x200000), ('sdk', n.layout.FIRST_BASE + 0x200000, 0x100000)]
        original = mods.copy()
        arenas = n.layout.nce_arenas(mods, {'main': 0x600000, 'sdk': 0x1000})
        self.assertEqual(mods, original)
        self.assertLess(arenas[0][1], mods[0][1])
        with self.assertRaises(ValueError): n.layout.nce_arenas(mods, {'main': 1 << 28})

    def test_patch_cannot_introduce_exclusive_or_unchecked_original(self):
        original = assemble('mov w0, #3')[0]
        atomic = assemble('ldaxr x9, [x8]')[0]
        table = patches.Table([('test', 'TEST', 0)], {'main': {0: [(0, [atomic], original)]}})
        with self.assertRaises(ValueError): plan([original, n.NOP], table)
        table.sites['main'][0][0] = (0, [n.NOP], original ^ 1)
        with self.assertRaises(ValueError): plan([original, n.NOP], table)


LINUX_ARM64 = platform.machine() in ('aarch64', 'arm64') and sys.platform.startswith('linux')


@unittest.skipUnless(LINUX_ARM64, 'needs Linux ARM64')
class RuntimeChecks(unittest.TestCase):
    """tests/nce_runtime.c: runtime/nce.c and runtime/nce_aarch64.S running the fixture image natively."""
    def test_native_runtime(self):
        work = ROOT / 'build/test/nce'
        work.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):
            generated = fixture(work / 'fixture')
        sdl = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'sdl3'], text=True))
        exe = work / 'nce_runtime'
        sources = [ROOT / 'tests/nce_runtime.c', ROOT / 'runtime/nce.c', ROOT / 'runtime/nce_aarch64.S',
                   ROOT / 'runtime/platform/linux.c', generated / 'nce_generated.c', ROOT / 'runtime/sha256.c']
        subprocess.run([os.environ.get('CC', 'clang'), '-O2', '-Wall', '-DRT_NCE=1', '-I', str(ROOT / 'runtime'),
                        '-I', str(generated), *map(str, sources), '-pthread', '-ldl', '-lm', *sdl, '-o', str(exe)],
                       check=True)
        ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)


class HarnessChecks(unittest.TestCase):
    """Mod scanning and the loader's image checks, compiled from runtime sources against the fixture."""
    @classmethod
    def setUpClass(cls):
        directory = ROOT / 'build/nce-linux'
        directory.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(dir=directory)
        cls.work = Path(cls.temporary.name)
        cls.pkg = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'sdl3'], text=True))
        romfs = (ROOT / 'runtime/romfs.c').read_text()
        (cls.work / 'romfs_gate.inc').write_text(source_function(romfs, 'static void mods_overlay_init(void)'))
        with contextlib.redirect_stdout(io.StringIO()):
            fixture(cls.work / 'fixture')
        cls.generated = cls.work / 'fixture/generated'
        header = (ROOT / 'runtime/nce.h').read_text()
        types = '#include "cpu.h"\n' + header[header.index('enum {'):header.index('typedef struct NceFrame')]
        types += next(line for line in header.splitlines() if line.startswith('int rt_nce_check_text(')) + '\n'
        (cls.work / 'nce_types.inc').write_text(types)
        metadata = (cls.generated / 'nce_generated.c').read_text().replace('#include "nce.h"', '#include "nce_types.inc"')
        (cls.work / 'metadata.c').write_text(metadata)
        loader = (ROOT / 'runtime/loader.c').read_text()
        nce = (ROOT / 'runtime/nce.c').read_text()
        parts = next(line for line in loader.splitlines() if line.startswith('typedef struct { uint32_t file_off')) + '\n'
        for signature in ('static uint8_t *read_file(', 'static void hex32(', 'uint8_t *ldr_read_verified(',
                          'static size_t lz4_decompress(', 'static uint64_t nso_segments(', 'static void nso_segment('):
            parts += source_function(loader, signature)
        parts += '#if RT_NCE\n'
        for signature in ('static uint32_t branch(', 'void rt_nce_load(', 'int rt_nce_check_text('):
            parts += source_function(nce, signature)
        parts += '#endif\n' + source_function(loader, 'static void seal(') + source_function(loader, 'static void check_segments(')
        (cls.work / 'loader_parts.inc').write_text(parts)
        cls.mods = cls.work / 'mods'
        names = ('SdkMix', 'RtldMix', 'CapsSdkMix', 'RtldPrefix', 'MainMetadata', 'PureRomfs',
                 'ExistingPlugin', 'MainMix', 'DirSdk', 'DisabledRomfs')
        for name in names:
            asset = cls.mods / name / 'romfs/asset.bin'
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(b'synthetic RomFS asset')
        dependencies = {'SdkMix': 'sdk', 'RtldMix': 'rtld', 'CapsSdkMix': 'SDK', 'RtldPrefix': 'rtld.txt',
                        'MainMetadata': 'main.npdm', 'ExistingPlugin': 'subsdk0', 'MainMix': 'main', 'ExeOnlySdk': 'sdk'}
        for name, file in dependencies.items():
            path = cls.mods / name / 'exefs' / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic dependency, not an executable')
        (cls.mods / 'DirSdk/exefs/sdk').mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def compile_run(self, test, native):
        executable = self.work / f'{test}-{native}'
        sources = [ROOT / f'tests/{test}.c', ROOT / 'runtime/platform/linux.c']
        if test == 'nce_loader_check':
            sources += [ROOT / 'runtime/sha256.c', self.work / 'metadata.c']
        command = ['clang', '-O2', f'-DRT_NCE={native}', '-ffunction-sections', '-fdata-sections',
                   '-I', str(ROOT / 'runtime'), '-I', str(self.work), '-I', str(self.generated),
                   *map(str, sources), '-Wl,--gc-sections', '-pthread', '-ldl', '-lm', *self.pkg, '-o', str(executable)]
        built = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        argument = self.mods if test == 'nce_mods_check' else self.work / 'fixture/input'
        ran = subprocess.run([str(executable), str(argument)], capture_output=True, text=True, timeout=30)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        print(ran.stdout.strip().splitlines()[-1])

    def test_native_mod_dependencies(self):
        self.compile_run('nce_mods_check', 1)

    def test_legacy_mod_dependencies(self):
        self.compile_run('nce_mods_check', 0)

    def test_native_installed_image_probe(self):
        self.compile_run('nce_loader_check', 1)

    def test_legacy_original_image_probe(self):
        self.compile_run('nce_loader_check', 0)


if __name__ == '__main__':
    unittest.main()
