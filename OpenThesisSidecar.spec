# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH)
resources = project_root / "src" / "openthesis" / "resources"

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
)

bundle = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="openthesis-sidecar",
)
