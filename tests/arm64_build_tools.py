"""No-game checks for build targets, runtime object isolation and translator RIDs."""
import importlib
import json
import os
from contextlib import ExitStack
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import build_game
import build_xlate


class BuildToolsTest(unittest.TestCase):
    def setUp(self):
        self.game = importlib.reload(build_game)
        self.xlate = importlib.reload(build_xlate)
        (ROOT / 'build').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_host_defaults(self):
        for mod in (self.game, self.xlate):
            for machine, target in [('aarch64', 'aarch64'), ('arm64', 'aarch64'),
                                    ('x86_64', 'x86_64'), ('AMD64', 'x86_64')]:
                with mock.patch.object(mod, 'WIN', False), mock.patch.object(mod.platform, 'machine', return_value=machine):
                    self.assertEqual(mod.default_target(), target)
            with mock.patch.object(mod, 'WIN', True):
                self.assertEqual(mod.default_target(), 'x86_64')
            with mock.patch.object(mod, 'WIN', False), mock.patch.object(mod.platform, 'machine', return_value='riscv64'):
                with self.assertRaisesRegex(SystemExit, 'unsupported host architecture'):
                    mod.default_target()

    def test_translator_directories(self):
        x = self.xlate
        self.assertEqual(x.build_dir('aarch64'), x.ROOT / 'build' / 'xlate-aarch64')
        self.assertEqual(x.build_dir('x86_64'), x.ROOT / 'build' / 'xlate')

    def test_x86_flags_and_pgo(self):
        g = self.game
        with mock.patch.object(g, 'WIN', False), mock.patch.object(g, 'default_target', return_value='x86_64'):
            g.set_target('x86_64', 'v2')
        self.assertIn('-march=x86-64-v2', g.GEN_FLAGS)
        self.assertIn('-march=x86-64-v2', g.RT_FLAGS)
        self.assertIn('-DRT_CPU_LEVEL=2', g.RT_FLAGS)
        self.assertIn('-DRT_NCE=0', g.RT_FLAGS)
        self.assertEqual(g.LIB_NAME, 'libtotkrt.a')
        g.set_pgo('gen', self.base)
        self.assertIn('-O2', g.GEN_FLAGS)
        self.assertIn('-DTOTK_PGO_GEN', g.RT_FLAGS)
        self.assertIn('-fprofile-generate', g.LINK_FLAGS)

    def test_arm_flags_objects_and_assembly(self):
        g = self.game
        rt = self.base / 'runtime'
        rt.mkdir()
        for name in ('main.c', 'entry.S', 'window_win.c', 'window_sdl.c'):
            (rt / name).touch()
        with mock.patch.object(g, 'WIN', False), mock.patch.object(g, 'RUNTIME', rt), \
                mock.patch.object(g, 'default_target', return_value='x86_64'):
            x86_object = g.runtime_object(self.base, rt / 'main.c')
            self.assertNotIn(rt / 'entry.S', g.runtime_sources())
            g.set_target('aarch64')
            arm_object = g.runtime_object(self.base, rt / 'main.c')
            self.assertNotEqual(arm_object, x86_object)
            self.assertIn(rt / 'entry.S', g.runtime_sources())
            self.assertNotIn(rt / 'window_win.c', g.runtime_sources())
            with mock.patch.object(g, 'vma_include', return_value=self.base):
                text, objs, exe = g.ninja_file(self.base, self.base, {}, 1)
            self.assertIn('entry.S', text)
            self.assertIn('-DRT_NCE=1', text)
            self.assertNotIn('march=x86', text)
            self.assertNotIn('ffixed-x18', text)
            self.assertNotIn('call-saved-x18', text)
            self.assertIn('--target=aarch64-linux-gnu', text)
            self.assertIn('nce_generated.c', text)
            self.assertEqual(exe.name, g.EXE)
            self.assertTrue(all('nce-aarch64' in o.parts for o in objs['runtime']))
            self.assertIn(g.GENERATED_MARKER, text)

    def test_native_arm_uses_compiler_default_triple(self):
        g = self.game
        with mock.patch.object(g, 'WIN', False), mock.patch.object(g, 'default_target', return_value='aarch64'):
            g.set_target('aarch64')
        for flags in (g.GEN_FLAGS, g.RT_FLAGS, g.LINK_FLAGS):
            self.assertFalse(any(flag.startswith('--target=') for flag in flags))
        self.assertIn('-DRT_NCE=1', g.RT_FLAGS)

    def test_invalid_arm_cpu_and_windows(self):
        with mock.patch.object(self.game, 'WIN', False):
            with self.assertRaisesRegex(SystemExit, '--cpu is only valid'):
                self.game.set_target('aarch64', 'v3')
        with mock.patch.object(self.game, 'WIN', True):
            with self.assertRaisesRegex(SystemExit, 'Windows supports only'):
                self.game.set_target('aarch64')
        with mock.patch.object(self.xlate, 'WIN', True):
            with self.assertRaisesRegex(SystemExit, 'Windows supports only'):
                self.xlate.runtime_id('aarch64')
            self.assertEqual(self.xlate.runtime_id('x86_64'), 'win-x64')

    def test_translator_rids_and_wrong_arch(self):
        x = self.xlate
        exe = self.base / 'totk_xlate'
        with mock.patch.object(x, 'WIN', False):
            for target, rid, machine in [('aarch64', 'linux-arm64', 183), ('x86_64', 'linux-x64', 62)]:
                self.assertEqual(x.runtime_id(target), rid)
                head = bytearray(64)
                head[:6] = b'\x7fELF\x02\x01'
                head[18:20] = machine.to_bytes(2, 'little')
                exe.write_bytes(head)
                x.check_architecture(exe, target)
                other = 'x86_64' if target == 'aarch64' else 'aarch64'
                with self.assertRaisesRegex(SystemExit, 'translator; rebuild it'):
                    x.check_architecture(exe, other)
            exe.write_bytes(b'invalid')
            with self.assertRaises(SystemExit):
                x.check_architecture(exe, 'aarch64')

    def test_native_rejects_incompatible_modes_before_inputs(self):
        for option in (['--pgo', 'gen'], ['--modules', 'main'], ['--runtime-lib', 'lib.a'],
                       ['--make-runtime-lib'], ['--cpu', 'v2']):
            g = importlib.reload(build_game)
            with mock.patch.object(g, 'WIN', False), mock.patch.object(sys, 'argv', ['build_game', '--target', 'aarch64', *option]):
                with self.assertRaises(SystemExit):
                    g.main()

    def test_native_cli_metadata_only_and_profile_glue(self):
        g = self.game
        gen = self.base / 'generated' / 'v121-nce'
        gen.mkdir(parents=True)
        (gen / 'layout.json').write_text(json.dumps([{'name': 'main', 'base': '0x8004000', 'size': '0x1000'}]))
        (gen / 'nce_generated.c').write_text('/* fixture */')
        expected = self.base / 'expected.json'
        expected.write_text(json.dumps({'exefs_sha256': {'main': 'fixture-hash'}}))
        tool = self.base / 'clang'
        tool.touch()
        profile = self.base / 'custom-profile'
        out = self.base / 'build' / 'nce-linux' / 'v121'

        def compile_objects(*args):
            text = (out / 'build.ninja').read_text()
            for line in text.splitlines():
                if line.startswith('build ') and ': cc_rt ' in line:
                    obj = g.ROOT / line[6:].split(': cc_rt ')[0]
                    obj.parent.mkdir(parents=True, exist_ok=True)
                    obj.write_bytes(b'fixture object')
            return 0, 0, 0, 0

        with ExitStack() as stack:
            for name, value in [('ROOT', self.base), ('DEFAULT_DEPS', self.base / 'deps'),
                                ('WIN', False), ('CLANG', tool), ('NINJA', tool)]:
                stack.enter_context(mock.patch.object(g, name, value))
            stack.enter_context(mock.patch.object(g, 'chunk_files', side_effect=AssertionError('lifted chunks read')))
            stack.enter_context(mock.patch.object(g, 'main_objects', side_effect=AssertionError('lifted objects reused')))
            stack.enter_context(mock.patch.object(g, 'runtime_sources', return_value=[]))
            stack.enter_context(mock.patch.object(g, 'stale_shaders', return_value=[]))
            stack.enter_context(mock.patch.object(g, 'vma_include', return_value=self.base))
            stack.enter_context(mock.patch.object(g, 'run_measured', side_effect=compile_objects))
            stack.enter_context(mock.patch.object(sys, 'argv', ['build_game', '--target', 'aarch64', '--expected',
                                        str(expected), '--profile', str(profile), '--compile-only']))
            g.main()
        text = (out / 'build.ninja').read_text()
        glue = (out / 'modules.c').read_text()
        self.assertIn('nce_generated.c', text)
        self.assertNotIn(': cc_gen ', text)
        self.assertIn('NULL, NULL, 0', glue)
        self.assertNotIn('main_fn_', glue)
        self.assertIn(profile.as_posix(), glue)
        self.assertIn('fixture-hash', glue)
        self.assertTrue((out / 'obj' / g.MANIFEST).is_file())

    def test_x86_glue_and_split_runtime_stay_lifted(self):
        g = self.game
        paths = {name: self.base / name for name in ('exefs', 'romfs', 'profile')}
        glue = g.module_glue([('main', 0x1000, 0x1000)], ['main'], {'main': 'hash'}, paths, 'v121')
        self.assertIn('main_fn_addrs', glue)
        self.assertIn('main_fn_ptrs', glue)
        self.assertIn('main_fn_count', glue)
        sources = [g.RUNTIME / 'main.c', g.RUNTIME / 'romfs.c']
        with mock.patch.object(g, 'runtime_sources', return_value=sources), mock.patch.object(g, 'vma_include', return_value=self.base):
            library, objs, archive = g.ninja_file(self.base, self.base, {}, 1, make_lib=True)
            self.assertEqual(archive.name, 'libtotkrt.a')
            self.assertIn('main.c', library)
            self.assertNotIn('romfs.c', library)
            split, _, _ = g.ninja_file(self.base, self.base, {}, 1, runtime_lib=archive)
            self.assertIn('--whole-archive', split)
            self.assertIn('romfs.c', split)
            self.assertNotIn(': cc_rt ' + g.ninja_path(g.RUNTIME / 'main.c'), split)

    def test_publish_uses_selected_rid_and_separate_default_output(self):
        for target, rid in [('aarch64', 'linux-arm64'), ('x86_64', 'linux-x64')]:
            x = importlib.reload(build_xlate)
            source = self.base / 'xlate-input'
            source.mkdir(exist_ok=True)
            for name in ('Program.cs', 'totk_xlate.csproj'):
                (source / name).touch()
            calls = []

            def publish(cmd, **kwargs):
                calls.append(cmd)
                out = Path(cmd[cmd.index('-o') + 1])
                out.mkdir()
                head = bytearray(64)
                head[:6] = b'\x7fELF\x02\x01'
                head[18:20] = (183 if target == 'aarch64' else 62).to_bytes(2, 'little')
                (out / x.EXE).write_bytes(head)
                return subprocess_result

            subprocess_result = mock.Mock(returncode=0)
            with ExitStack() as stack:
                for name, value in [('ROOT', self.base), ('WIN', False), ('XLATE', source)]:
                    stack.enter_context(mock.patch.object(x, name, value))
                stack.enter_context(mock.patch.object(x, 'default_target', return_value=target))
                stack.enter_context(mock.patch.object(x, 'verify_vendor'))
                stack.enter_context(mock.patch.object(x, 'prepare_sources', return_value=source))
                stack.enter_context(mock.patch.object(x, 'identity', return_value='fixture'))
                stack.enter_context(mock.patch.object(x.shutil, 'which', return_value='/fixture/dotnet'))
                stack.enter_context(mock.patch.object(x.subprocess, 'run', side_effect=publish))
                stack.enter_context(mock.patch.object(sys, 'argv', ['build_xlate']))
                x.main()
            self.assertEqual(calls[0][calls[0].index('-r') + 1], rid)
            self.assertEqual(x.BUILD.name, 'xlate-aarch64' if target == 'aarch64' else 'xlate')
            self.assertTrue((x.BUILD / x.EXE).is_file())

    @unittest.skipUnless(shutil.which('clang'), 'clang not installed')
    def test_native_flags_compile_c_and_assembly_without_game(self):
        g = self.game
        with mock.patch.object(g, 'WIN', False):
            g.set_target('aarch64')
        c = self.base / 'fixture.c'
        assembly = self.base / 'fixture.S'
        c.write_text('void fixture_c(void) { __asm__ volatile("mov x18, #0" : : : "x18"); }\n')
        assembly.write_text('.text\n.global fixture_asm\nfixture_asm:\nret\n')
        for source in (c, assembly):
            obj = source.with_suffix(source.suffix + '.o')
            subprocess.run([shutil.which('clang'), *g.RT_FLAGS, '-c', str(source), '-o', str(obj)], check=True)
            head = obj.read_bytes()[:64]
            self.assertEqual(head[:6], b'\x7fELF\x02\x01')
            self.assertEqual(int.from_bytes(head[18:20], 'little'), 183)

    def test_manifest_verification_in_native_object_directory(self):
        g = self.game
        with mock.patch.object(g, 'WIN', False), mock.patch.object(g, 'RUNTIME', self.base):
            g.set_target('aarch64')
            obj = g.runtime_object(self.base, self.base / 'main.c')
        obj.parent.mkdir(parents=True)
        obj.write_bytes(b'fixture')
        manifest = {}
        self.assertEqual(g.check_objects(self.base, [obj], manifest, True), (1, [], []))
        path = self.base / 'obj' / g.MANIFEST
        g.write_manifest(path, manifest)
        self.assertEqual(g.read_manifest(path), manifest)
        stat = obj.stat()
        obj.write_bytes(b'corrupt')
        os.utime(obj, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(g.check_objects(self.base, [obj], manifest, True), (0, [obj], []))


if __name__ == '__main__':
    unittest.main()
