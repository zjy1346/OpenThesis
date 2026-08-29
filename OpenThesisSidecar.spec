# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH)
resources = project_root / "src" / "openthesis" / "resources"
version_file = project_root / "OpenThesisSidecar.version.txt"

a = Analysis(
    [str(project_root / "sidecar_launcher.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[(str(resources), "openthesis/resources")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinterweb", "tkinterweb_tkhtml"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Universal CRT is a Windows system component on every supported OpenThesis
# release target. Some development environments expose a third-party copy on
# PATH; collecting it risks version skew and can trip real-time protection.
a.binaries = [entry for entry in a.binaries if entry[0].lower() != "ucrtbase.dll"]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="openthesis-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    hide_console="hide-early",
    version=str(version_file),
)

bundle = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="openthesis-sidecar",
)
