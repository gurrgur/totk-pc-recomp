"""Check committed FSR shader formats and included-header freshness without a GPU."""
from pathlib import Path
import importlib.util
import re
import shutil
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_ffx_shaders', ROOT / 'tools/build_ffx_shaders.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class FfxShaderTest(unittest.TestCase):
    def test_luma_history_storage_format(self):
        # Follow OpName -> OpVariable -> OpTypePointer -> OpTypeImage in actual blobs.
        header = build.header_path().read_text()
        found = 0
        for body in re.findall(r'static const uint32_t ffx_fsr3upscaler_spv_\d+\[\] = \{(.*?)\};', header, re.S):
            words = [int(word, 16) for word in re.findall(r'0x[0-9a-f]+', body)]
            names, variables, pointers, formats = {}, {}, {}, {}
            pos = 5  # SPIR-V header
            while pos < len(words):
                count, opcode = words[pos] >> 16, words[pos] & 0xffff
                self.assertGreater(count, 0)
                ins = words[pos:pos + count]
                if opcode == 5:  # OpName
                    names[ins[1]] = struct.pack('<' + 'I' * (count - 2), *ins[2:]).split(b'\0')[0].decode()
                elif opcode == 59:  # OpVariable
                    variables[ins[2]] = ins[1]
                elif opcode == 32:  # OpTypePointer
                    pointers[ins[1]] = ins[3]
                elif opcode == 25:  # OpTypeImage
                    formats[ins[1]] = ins[8]
                pos += count
            for ident, name in names.items():
                if name == 'rw_luma_history':
                    self.assertEqual(formats[pointers[variables[ident]]], 2, 'history storage image must be Rgba16f')
                    found += 1
        self.assertGreater(found, 0, 'no luma-history storage binding in shader blobs')
        host = (ROOT / 'runtime' / build.FFX / 'sdk/src/components/fsr3upscaler/ffx_fsr3upscaler.cpp').read_text()
        for index in (1, 2):
            self.assertRegex(host, rf'FFX_FSR3UPSCALER_RESOURCE_IDENTIFIER_LUMA_HISTORY_{index},[^\n]*\n\s*FFX_SURFACE_FORMAT_R16G16B16A16_FLOAT,')

    def test_included_header_freshness(self):
        self.assertEqual(build.stale(), [])
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='ffx-shader-') as directory:
            runtime = Path(directory)
            for src in [*build.shader_sources(), build.header_path()]:
                dst = runtime / src.relative_to(ROOT / 'runtime')
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            self.assertEqual(build.stale(runtime), [])
            callbacks = runtime / build.FFX / 'sdk/include/FidelityFX/gpu/fsr3upscaler/ffx_fsr3upscaler_callbacks_glsl.h'
            callbacks.write_text(callbacks.read_text() + '\n// changed dependency\n')
            stale = build.stale(runtime)
            self.assertEqual(len(stale), 1)
            self.assertIn(callbacks.name, stale[0])


if __name__ == '__main__':
    unittest.main()
