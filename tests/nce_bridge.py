"""Native boundary checks under Unicorn: the assembled bridge (runtime/nce_aarch64.S) keeps the full register state.
No game data."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'tools')]
from nce_check import assemble
import prepare_nce as n
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm64_const as r


def linked_bridge():
    with tempfile.TemporaryDirectory(dir=ROOT / 'build/nce-linux') as tmp:
        p = Path(tmp)
        (p / 'support.S').write_text('.text\n.global rt_nce_service\nrt_nce_service: ret\n'
                                    '.section .tbss,"awT",%nobits\n.global rt_nce_current\n'
                                    '.p2align 3\nrt_nce_current: .skip 8\n'
                                    # the small-copy target of rt_direct_memcpy (runtime/nce.c)
                                    '.data\n.global rt_direct_memmove\n.p2align 3\nrt_direct_memmove: .quad 0\n')
        for source, target in ((ROOT / 'runtime/nce_aarch64.S', p / 'bridge.o'), (p / 'support.S', p / 'support.o')):
            subprocess.run(['clang', '--target=aarch64-linux-gnu', '-DRT_NCE=1', '-c', str(source), '-o', str(target)], check=True)
        subprocess.run(['ld.lld', '--image-base=0x0', '-Ttext=0x100000', '--entry=rt_nce_run', str(p / 'bridge.o'), str(p / 'support.o'), '-o', str(p / 'bridge')], check=True)
        subprocess.run(['llvm-objcopy', '-O', 'binary', '--only-section=.text', str(p / 'bridge'), str(p / 'bridge.bin')], check=True)
        symbols = subprocess.check_output(['llvm-nm', str(p / 'bridge')], text=True)
        addresses = {fields[2]: int(fields[0], 16) for line in symbols.splitlines() if len(fields := line.split()) == 3}
        return (p / 'bridge.bin').read_bytes(), addresses


class BridgeChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary, cls.symbols = linked_bridge()

    def boundary(self, call_boundary):
        u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        code, island, sentinel, cpu, frame, tls = 0x200000, 0x210000, 0x220000, 0x300000, 0x301000, 0x310000
        guest_stack, host_stack, service_stack, finish = 0x400000, 0x500000, 0x600000, 0x700000
        for a, size in ((0x100000, 0x10000), (code, 0x30000), (cpu, 0x20000), (guest_stack, 0x10000),
                        (host_stack, 0x10000), (service_stack, 0x10000), (finish, 0x1000)):
            u.mem_map(a, size)
        u.mem_write(0x100000, self.binary)
        u.mem_write(code, struct.pack('<III', 0x58000110, n.branch(code + 4, island), n.branch(code + 8, sentinel)))
        seed16 = 0xfedcba9876543210
        u.mem_write(code + 32, struct.pack('<Q', seed16))
        for addr, pc, continuation in ((island, code + 4, code + 8), (sentinel, sentinel, sentinel)):
            words = n.stub(addr, pc, continuation)
            words[8], words[9] = self.symbols['rt_nce_boundary'] & 0xffffffff, self.symbols['rt_nce_boundary'] >> 32
            u.mem_write(addr, struct.pack('<' + 'I' * len(words), *words))
        initial = [0x1010101000000000 + i for i in range(32)]
        initial[16] = seed16
        initial[30] = code + 8
        vectors = [(i + 1) * 0x0102030405060708090a0b0c0d0e0f for i in range(32)]
        u.mem_write(cpu, struct.pack('<32Q', *initial))
        sp = guest_stack + 0x8000
        u.mem_write(cpu + 256, struct.pack('<7Q', sp, code, 0xa0000000, 0x1234, 0x5678, 0, 0))
        u.mem_write(cpu + 320, struct.pack('<QQ', 0x1c00000, 0x800009f))
        u.mem_write(cpu + 512, b''.join(v.to_bytes(16, 'little') for v in vectors))
        u.mem_write(frame, struct.pack('<8Q', cpu, 0, 0, service_stack + 0x8000, 0, 0, code, 0))
        u.mem_write(tls + 16, struct.pack('<Q', frame))
        u.reg_write(r.UC_ARM64_REG_TPIDR_EL0, tls)
        u.reg_write(r.UC_ARM64_REG_SP, host_stack + 0x8000)
        u.reg_write(r.UC_ARM64_REG_X0, frame)
        u.reg_write(r.UC_ARM64_REG_X30, finish)
        u.reg_write(r.UC_ARM64_REG_FPCR, 0x800000)
        u.reg_write(r.UC_ARM64_REG_FPSR, 1)
        host_saved = {}
        for i in range(19, 30):
            reg = getattr(r, f'UC_ARM64_REG_X{i}')
            host_saved[reg] = 0x99880000 + i
            u.reg_write(reg, host_saved[reg])
        host_vectors = {}
        for i in range(8, 16):
            reg = getattr(r, f'UC_ARM64_REG_Q{i}')
            host_vectors[reg] = (0xabcdef1234 << 64) | (0x1234000000000000 + i)
            u.reg_write(reg, host_vectors[reg])
        changed = [0xabcde00000000000 + i for i in range(32)]
        changed_vectors = [v ^ ((1 << 127) | 123) for v in vectors]
        calls = []
        def service(uc, pc, size, _):
            if pc != self.symbols['rt_nce_service']:
                return
            self.assertEqual(uc.reg_read(r.UC_ARM64_REG_FPCR), 0)
            self.assertEqual(uc.reg_read(r.UC_ARM64_REG_FPSR), 0)
            self.assertEqual(uc.reg_read(r.UC_ARM64_REG_TPIDR_EL0), tls)
            self.assertTrue(service_stack <= uc.reg_read(r.UC_ARM64_REG_SP) < service_stack + 0x10000)
            calls.append(uc.reg_read(r.UC_ARM64_REG_X1))
            state = list(struct.unpack('<32Q', uc.mem_read(cpu, 256)))
            if len(calls) == 1:
                self.assertEqual(state[:31], initial[:31])
                self.assertEqual(uc.reg_read(r.UC_ARM64_REG_X2), sp)
                self.assertEqual(uc.reg_read(r.UC_ARM64_REG_X3), initial[30])
                self.assertEqual(bytes(uc.mem_read(cpu + 512, 512)), b''.join(v.to_bytes(16, 'little') for v in vectors))
                uc.mem_write(cpu, struct.pack('<32Q', *changed))
                uc.mem_write(cpu + 256, struct.pack('<QQQ', sp + 0x100, 0xdead0000, 0x50000000))
                uc.mem_write(cpu + 320, struct.pack('<QQ', 0xc00000, 0x800001f))
                uc.mem_write(cpu + 512, b''.join(v.to_bytes(16, 'little') for v in changed_vectors))
                uc.reg_write(r.UC_ARM64_REG_X0, code + 8 if call_boundary else island + 40)
                uc.reg_write(r.UC_ARM64_REG_X1, 0 if call_boundary else 1)
            else:
                expected = changed[:31]
                if call_boundary:
                    expected[16] = code + 8
                self.assertEqual(state[:31], expected)
                self.assertEqual(struct.unpack('<Q', uc.mem_read(cpu + 256, 8))[0], sp + 0x100)
                self.assertEqual(struct.unpack('<Q', uc.mem_read(cpu + 272, 8))[0], 0x50000000)
                self.assertEqual(struct.unpack('<QQ', uc.mem_read(cpu + 320, 16)), (0xc00000, 0x800001f))
                self.assertEqual(bytes(uc.mem_read(cpu + 512, 512)), b''.join(v.to_bytes(16, 'little') for v in changed_vectors))
                uc.reg_write(r.UC_ARM64_REG_X0, 0)
                uc.reg_write(r.UC_ARM64_REG_X1, 2)
            uc.reg_write(r.UC_ARM64_REG_PC, uc.reg_read(r.UC_ARM64_REG_X30))
        u.hook_add(UC_HOOK_CODE, service)
        u.emu_start(self.symbols['rt_nce_run'], finish, count=2000)
        self.assertEqual(calls, [code + 4, sentinel])
        self.assertEqual(u.reg_read(r.UC_ARM64_REG_SP), host_stack + 0x8000)
        self.assertEqual(u.reg_read(r.UC_ARM64_REG_FPCR), 0x800000)
        self.assertEqual(u.reg_read(r.UC_ARM64_REG_FPSR), 1)
        for reg, value in host_saved.items():
            self.assertEqual(u.reg_read(reg), value)
        for reg, value in host_vectors.items():
            self.assertEqual(u.reg_read(reg) & ((1 << 64) - 1), value & ((1 << 64) - 1))

    def test_instruction_preserves_every_output_and_private_control(self):
        self.boundary(False)

    def test_function_call_ip0_exception_is_explicit(self):
        self.boundary(True)


if __name__ == '__main__':
    unittest.main()
