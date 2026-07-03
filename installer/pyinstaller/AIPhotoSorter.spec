# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
TARGET_ARCH = "universal2" if sys.platform == "darwin" else None

datas = [
    (str(ROOT / "ai_photo_sorter" / "templates"), "ai_photo_sorter/templates"),
    (str(ROOT / "ai_photo_sorter" / "static"), "ai_photo_sorter/static"),
]

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AIPhotoSorter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="AI Photo Sorter.app",
        icon=None,
        bundle_identifier="com.local.aiphotosorter",
        info_plist={
            "CFBundleDisplayName": "AI Photo Sorter",
            "CFBundleName": "AI Photo Sorter",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
        },
    )
else:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="AIPhotoSorter",
    )
