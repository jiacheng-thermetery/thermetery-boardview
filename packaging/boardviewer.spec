from pathlib import Path
import importlib.util
import os


ROOT = Path(SPECPATH).parent
NATIVE_BUILD = Path(os.environ["BOARDVIEW_NATIVE_BUILD_DIR"])
ICON = os.environ["BOARDVIEW_ICON_PATH"]
VERSION_FILE = os.environ["BOARDVIEW_VERSION_FILE"]

native_names = ("tvw_native.dll", "xzz_native.dll", "rc6_native.dll")
native_binaries = [
    (str(NATIVE_BUILD / name), "src/parsers/native") for name in native_names
]

skia_spec = importlib.util.find_spec("skia")
if skia_spec is None or skia_spec.origin is None:
    raise RuntimeError("skia-python is required for the Windows release")
skia_icu = Path(skia_spec.origin).with_name("icudtl.dat")
if not skia_icu.is_file():
    raise RuntimeError(f"skia ICU data is missing: {skia_icu}")

hidden_imports = [
    "src.ratsnest",
    "src.runtime_paths",
    "src.tvw_compal",
    "src.tvw_topology",
    "src.parsers.tvw_native",
    "src.parsers.xzz_native",
    "pyopengltk.win32",
    "OpenGL.GL",
    "OpenGL.WGL",
    "tkinterdnd2",
]

excluded_modules = [
    "src.walker",
    "src.linker",
    "src.signal_match",
    "src.schematic_text",
    "src.convert_rules",
    "anthropic",
    "openai",
    "keyring",
    "openpyxl",
    "yaml",
    "fitz",
    "pymupdf",
]

a = Analysis(
    [str(ROOT / "packaging" / "boardviewer_entry.py")],
    pathex=[str(ROOT)],
    binaries=native_binaries,
    datas=[(str(skia_icu), ".")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=1,
)

# PyOpenGL ships optional VC9-era GLUT/GLE helper DLLs. The viewer uses the
# Windows system OpenGL implementation directly and never imports GLUT/GLE;
# omitting these avoids dead binaries whose obsolete MSVCR90 dependency is not
# present on a clean Windows 10/11 machine.
def _is_unused_pyopengl_helper(entry):
    destination = entry[0].replace("\\", "/").lower()
    return destination.startswith("opengl/dlls/")


a.binaries = [entry for entry in a.binaries if not _is_unused_pyopengl_helper(entry)]
a.datas = [entry for entry in a.datas if not _is_unused_pyopengl_helper(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ThermeteryBoardviewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=ICON,
    version=VERSION_FILE,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ThermeteryBoardviewer",
)
