"""Build the shader translator: build/xlate/totk_xlate.exe (Ryujinx 1.3.2 + patches, NativeAOT, SPIR-V for Vulkan).

Copies tools/vendor/ryujinx to build/xlate/ryujinx, applies tools/xlate/patches/*.patch there (tools/vendor stays
byte-identical; 0001-0004 are MNG's, 0005-0013, 0015 and 0016 are ours), then runs `dotnet publish`. Needs the .NET 10 SDK, the MSVC build
tools and vswhere.exe (docs/building.md). Run with .venv. On Linux it builds totk_xlate for linux-x64 or linux-arm64, which
NativeAOT links with the system's clang (no MSVC); NativeAOT builds for the OS it runs on only.

Next to the exe it writes totk_xlate.id: the translator identity, a sha256 over everything the build reads (this script,
Program.cs, the csproj, the patches, the vendor tree). runtime/gfx_shader.c puts it into every shader cache key, so a
translator change re-keys the cache without a hand-made KEY_VERSION bump. --id-only writes it for the exe already built
(the caller vouches that the exe matches the sources).

  python tools/build_xlate.py [--id-only] [--out DIR]

--out builds into DIR instead of build/xlate (a task build dir: totk.exe takes the translator next to it).
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "tools" / "vendor" / "ryujinx"
MNG_VENDOR = ROOT / "refs" / "MacNativeGames" / "vendor" / "ryujinx"
XLATE = ROOT / "tools" / "xlate"
BUILD = ROOT / "build" / "xlate"
WIN = os.name == "nt"
EXE = "totk_xlate.exe" if WIN else "totk_xlate"
VSWHERE_DIR = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer"


def default_target() -> str:
    if WIN:
        return "x86_64"
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    sys.exit(f"unsupported host architecture: {machine}")


def build_dir(target: str) -> Path:
    return ROOT / "build" / ("xlate-aarch64" if target == "aarch64" else "xlate")


def runtime_id(target: str) -> str:
    if WIN:
        if target != "x86_64":
            sys.exit("Windows supports only --target x86_64")
        return "win-x64"
    return "linux-arm64" if target == "aarch64" else "linux-x64"


def check_architecture(exe: Path, target: str) -> None:
    if not exe.is_file():
        sys.exit(f"{exe}: translator missing; rebuild it")
    with exe.open("rb") as f:
        head = f.read(64)
        if WIN and head[:2] == b"MZ":
            f.seek(int.from_bytes(head[60:64], "little"))
            pe = f.read(6)
            valid = pe[:4] == b"PE\0\0" and pe[4:6] == b"\x64\x86"
        else:
            machine = 183 if target == "aarch64" else 62
            valid = (not WIN and head[:6] == b"\x7fELF\x02\x01" and
                     int.from_bytes(head[18:20], "little") == machine)
    if not valid:
        sys.exit(f"{exe}: not a {runtime_id(target)} translator; rebuild it")


def tree_hashes(base: Path) -> dict[str, str]:
    """Hash every source file; src/*/obj and src/*/bin are build output (an IDE restore writes them, the vendor
    .gitignore ignores them and prepare_sources() drops them), so they are left out."""
    def build_output(rel: Path) -> bool:
        return len(rel.parts) > 2 and rel.parts[0] == "src" and rel.parts[2] in ("obj", "bin")

    return {
        p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(base.rglob("*"))
        if p.is_file() and not build_output(p.relative_to(base))
    }


def mng_pin() -> str:
    """The MacNativeGames commit tools/fetch_refs.sh pins."""
    for line in (ROOT / "tools" / "fetch_refs.sh").read_text(encoding="utf-8").splitlines():
        f = line.split()
        if len(f) >= 4 and f[:2] == ["fetch", "MacNativeGames"]:   # a fifth word asks for the history
            return f[3]
    sys.exit("tools/fetch_refs.sh pins no MacNativeGames commit")


def pinned_blobs() -> dict[str, str]:
    """Git blob ids of MNG's vendor/ryujinx at the pinned commit. The checkout itself may be newer than the pin or
    carry CRLF line ends (core.autocrlf), so its files are not compared."""
    out = subprocess.run(["git", "-C", str(MNG_VENDOR.parent.parent), "ls-tree", "-r", mng_pin(), "vendor/ryujinx"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"git ls-tree of the MacNativeGames pin failed: {out.stderr.strip()}")
    blobs = {}
    for line in out.stdout.splitlines():
        meta, path = line.split("\t", 1)
        blobs[path.removeprefix("vendor/ryujinx/")] = meta.split()[2]
    return blobs


def blob_ids(base: Path) -> dict[str, str]:
    """Git blob ids of the source files tree_hashes() hashes."""
    ids = {}
    for rel in tree_hashes(base):
        data = (base / rel).read_bytes()
        ids[rel] = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return ids


def verify_vendor() -> None:
    """tools/vendor/ryujinx must be byte-identical to MNG's copy at the pinned commit (when refs/ is present)."""
    if not MNG_VENDOR.is_dir():
        print("refs/MacNativeGames missing: vendor comparison skipped")
        return
    ours, theirs = blob_ids(VENDOR), pinned_blobs()
    if ours != theirs:
        diff = sorted(k for k in ours.keys() | theirs.keys() if ours.get(k) != theirs.get(k))
        sys.exit(f"tools/vendor/ryujinx differs from MNG in {len(diff)} files, first: {diff[:5]}")
    print(f"vendor identical to MNG: {len(ours)} files")


def prepare_sources() -> Path:
    src = BUILD / "ryujinx"
    if BUILD.exists():
        shutil.rmtree(BUILD / "ryujinx", ignore_errors=True)
    shutil.copytree(VENDOR, src)
    for junk in list(src.glob("src/*/obj")) + list(src.glob("src/*/bin")):
        shutil.rmtree(junk)
    patches = sorted((XLATE / "patches").glob("*.patch"))
    if len(patches) != 15:
        sys.exit(f"expected 15 patches, found {len(patches)}")
    for patch in patches:
        # git apply works outside a repository when given a directory; --check first so a failure names the patch.
        for extra in (["--check"], []):
            r = subprocess.run(
                ["git", "apply", *extra, "--whitespace=nowarn", "--directory", str(src.relative_to(BUILD)), str(patch)],
                cwd=BUILD, capture_output=True, text=True,
            )
            if r.returncode != 0:
                sys.exit(f"patch does not apply: {patch.name}\n{r.stdout}{r.stderr}")
        print(f"applied {patch.name}")
    return src


def identity() -> str:
    """sha256 over the translator's inputs, in a fixed order: path and content hash of each file."""
    h = hashlib.sha256()
    files = [Path(__file__).resolve(), XLATE / "Program.cs", XLATE / "totk_xlate.csproj",
             *sorted((XLATE / "patches").glob("*.patch"))]
    for f in files:
        h.update(f"{f.name}\0{hashlib.sha256(f.read_bytes()).hexdigest()}\n".encode())
    for rel, digest in tree_hashes(VENDOR).items():
        h.update(f"vendor/{rel}\0{digest}\n".encode())
    return h.hexdigest()


def write_identity(exe: Path) -> None:
    ident = identity()
    exe.with_suffix(".id").write_text(ident + "\n", encoding="ascii")
    print(f"translator identity {ident[:16]} -> {exe.with_suffix('.id')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--id-only", action="store_true", help="write totk_xlate.id for the exe already built")
    parser.add_argument("--out", type=Path, help="build directory instead of build/xlate (build/xlate-aarch64 on ARM)")
    parser.add_argument("--target", choices=("x86_64", "aarch64"),
                        help="target architecture (default: Linux host architecture, x86_64 on Windows)")
    parser.add_argument("--llvm", type=Path, help="Linux: an LLVM directory whose bin/clang and lld link the exe")
    parser.add_argument("--sysroot", type=Path, help="Linux: link against this sysroot's glibc instead of the host's "
                                                     "(tools/make_release_linux.py)")
    a = parser.parse_args()
    target = a.target or default_target()
    rid = runtime_id(target)
    global BUILD
    BUILD = a.out.resolve() if a.out else build_dir(target)
    if a.id_only:
        check_architecture(BUILD / EXE, target)
        write_identity(BUILD / EXE)
        return
    started = time.time()
    dotnet = shutil.which("dotnet")
    if not dotnet:
        sys.exit("missing: dotnet (.NET 10 SDK)")
    env = dict(os.environ)
    if WIN:
        if not (VSWHERE_DIR / "vswhere.exe").is_file() and not shutil.which("vswhere"):
            sys.exit(f"missing: vswhere.exe (expected in {VSWHERE_DIR}); NativeAOT cannot link without it")
        env["PATH"] = str(VSWHERE_DIR) + os.pathsep + env["PATH"]

    verify_vendor()
    src = prepare_sources()
    work = BUILD / "xlate"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for name in ("Program.cs", "totk_xlate.csproj"):
        shutil.copy2(XLATE / name, work / name)
    out = BUILD / "publish"
    shutil.rmtree(out, ignore_errors=True)
    cmd = [
        dotnet, "publish", str(work / "totk_xlate.csproj"), "-c", "Release", "-r", rid,
        "-p:PublishAot=true", "-p:InvariantGlobalization=true", f"-p:RyuSrc={src}", "-o", str(out),
    ]
    if a.llvm:
        env["PATH"] = str(a.llvm.resolve() / "bin") + os.pathsep + env["PATH"]
        cmd += ["-p:CppCompilerAndLinker=clang", "-p:LinkerFlavor=lld"]
    if a.sysroot:
        cmd.append(f"-p:SysRoot={a.sysroot.resolve()}")
    if subprocess.run(cmd, env=env).returncode != 0:
        sys.exit("dotnet publish failed")
    exe = BUILD / EXE
    check_architecture(out / EXE, target)
    shutil.copy2(out / EXE, exe)
    write_identity(exe)
    print(f"built {exe} ({exe.stat().st_size / 1e6:.1f} MB) in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
