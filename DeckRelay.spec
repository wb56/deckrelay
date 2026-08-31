from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


project_dir = Path(SPECPATH)
sys.path.insert(0, str(project_dir / "src"))

from party_player.release_artifact import is_forbidden_dependency_path
from party_player import __version__
from party_player.release_version import windows_version


numeric_version = windows_version(__version__)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=numeric_version,
        prodvers=numeric_version,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040704B0",
                    [
                        StringStruct("CompanyName", "DeckRelay"),
                        StringStruct("FileDescription", "DeckRelay"),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", "DeckRelay"),
                        StringStruct("OriginalFilename", "DeckRelay.exe"),
                        StringStruct("ProductName", "DeckRelay"),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1031, 1200])]),
    ],
)

python_root = Path(sys.base_prefix)
tcl_root = python_root / "tcl"
datas = collect_data_files("customtkinter") + [
    (str(tcl_root / "tcl8.6"), "_tcl_data"),
    (str(tcl_root / "tk8.6"), "_tk_data"),
]
tk_binaries = [
    (str(python_root / "DLLs" / "tcl86t.dll"), "."),
    (str(python_root / "DLLs" / "tk86t.dll"), "."),
]

a = Analysis(
    [str(project_dir / "src" / "party_player" / "__main__.py")],
    pathex=[str(project_dir / "src")],
    binaries=tk_binaries,
    datas=datas,
    hiddenimports=["vlc", "_tkinter", *collect_submodules("tkinter")],
    hookspath=[str(project_dir / "hooks")],
    runtime_hooks=[str(project_dir / "scripts" / "pyi_rth_deckrelay_tkinter.py")],
)
# The python-vlc PyInstaller hook may discover a locally installed VLC runtime.
# DeckRelay intentionally ships only the Python binding and requires an external
# user-installed VLC/FFmpeg runtime.
a.binaries = TOC(
    entry for entry in a.binaries if not is_forbidden_dependency_path(entry[0])
)
a.datas = TOC(entry for entry in a.datas if not is_forbidden_dependency_path(entry[0]))
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DeckRelay",
    console=False,
    version=version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="DeckRelay",
    contents_directory=".",
)
