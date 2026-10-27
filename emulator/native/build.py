"""Build ds5aec.dll, the echo canceller's native core, from source.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe native\\build.py

Output: emulator/ds5emu/_native/ds5aec.dll (git-ignored; built here, in CI, and by
app/packaging/build.ps1 before PyInstaller bundles it).

Sources: `ds5aec.c` (this repo) and the MDF echo canceller and resampler from speexdsp, vendored
in `speexdsp/` (BSD-3-Clause, see speexdsp/COPYING) from the upstream release
tag SpeexDSP-1.2.1 -- https://github.com/xiph/speexdsp/archive/refs/tags/
SpeexDSP-1.2.1.tar.gz, sha256 d17ca363654556a4ff1d02cc13d9eb1fc5a8642c90b40bd54ce266c3807b91a7.
The files are byte-identical to the release except speexdsp_config_types.h,
which upstream generates at configure time and is written out here.

The compiler is found the way setuptools finds it for C extensions (vswhere ->
MSVC), so neither a developer prompt nor any download is needed: a Visual
Studio 2022 (or Build Tools) install with the C++ workload is enough, and
GitHub's windows-latest runners have one. Built float, KISS FFT, /O2, with the
static C runtime (/MT), so the DLL depends on nothing but kernel32.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPX = os.path.join(HERE, "speexdsp")
OUT_DIR = os.path.join(os.path.dirname(HERE), "ds5emu", "_native")
NAME = "ds5aec"
SOURCES = [os.path.join(HERE, "ds5aec.c")] + [
    os.path.join(SPX, f) for f in ("mdf.c", "fftwrap.c", "kiss_fft.c", "kiss_fftr.c",
                                   "resample.c")]
MACROS = [("FLOATING_POINT", None), ("USE_KISS_FFT", None), ("EXPORT", ""),
          ("_CRT_SECURE_NO_WARNINGS", None)]


def build(out_dir: str = OUT_DIR, verbose: bool = False) -> str:
    try:
        from setuptools._distutils.ccompiler import new_compiler
    except ImportError:                       # older setuptools
        from distutils.ccompiler import new_compiler
    cc = new_compiler(verbose=verbose)
    if sys.platform == "win32":
        cc.initialize()
        cc.compile_options = [o for o in cc.compile_options if o not in ("/MD",)] + ["/MT"]
    tmp = tempfile.mkdtemp(prefix="ds5aec-")
    try:
        objs = cc.compile(SOURCES, output_dir=tmp, macros=MACROS,
                          include_dirs=[SPX, os.path.join(SPX, "include")],
                          extra_preargs=["/O2"] if sys.platform == "win32" else ["-O2", "-fPIC"])
        os.makedirs(out_dir, exist_ok=True)
        target = NAME + (".dll" if sys.platform == "win32" else ".so")
        cc.link_shared_object(objs, target, output_dir=tmp,
                              libraries=[] if sys.platform == "win32" else ["m"])
        dst = os.path.join(out_dir, os.path.basename(target))
        shutil.copy2(os.path.join(tmp, target), dst)
        return dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    path = build(verbose="-v" in sys.argv)
    print(f"built {path} ({os.path.getsize(path)} bytes)")
