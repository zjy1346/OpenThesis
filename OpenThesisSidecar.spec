# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH)
resources = project_root / "src" / "openthesis" / "resources"
version_file = project_root / "OpenThesisSidecar.version.txt"

# ``cryptography`` is imported lazily by the financial-compatibility trust
# path.  PyInstaller's static analysis cannot reliably see that import, so
# collect its package data, native backends, and hidden imports explicitly.
cryptography_datas, cryptography_binaries, cryptography_hiddenimports = collect_all("cryptography")
# HTTPS market/FX adapters rely on the Windows system trust store. Collect the
# dependency explicitly so a local build cannot silently fall back to a Python
# bundle that lacks the operating-system certificate bridge.
truststore_datas, truststore_binaries, truststore_hiddenimports = collect_all("truststore")
# OpenCC loads its conversion dictionaries at runtime.  They must travel with
# the sidecar or simplified/traditional canonicalization fails only after the
# desktop package leaves the development checkout.
opencc_datas, opencc_binaries, opencc_hiddenimports = collect_all("opencc")

a = Analysis(
    [str(project_root / "sidecar_launcher.py")],
    pathex=[str(project_root / "src")],
    binaries=cryptography_binaries + truststore_binaries + opencc_binaries,
    datas=cryptography_datas + truststore_datas + opencc_datas + [(str(resources), "openthesis/resources")],
    hiddenimports=cryptography_hiddenimports + truststore_hiddenimports + opencc_hiddenimports,
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
