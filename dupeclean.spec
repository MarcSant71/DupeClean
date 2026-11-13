# -*- mode: python ; coding: utf-8 -*-
# PyInstaller Spec file for DupeClean (GPU-Accelerated Duplicate File Finder)

import os
from pathlib import Path

block_cipher = None

# Hidden imports required by dynamic loaders
hiddenimports = [
    'torch',
    'xxhash',
    'send2trash',
    'PySide6',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PIL',
    'PIL.Image',
    'PIL.ImageOps',
    'sqlite3',
]

# Data files
datas = [
    ('assets/icon.ico', 'assets'),
]

binaries = []

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DupeClean',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # NEVER use UPX on CUDA binaries (corrupts large DLLs)
    console=False,  # Windowed GUI application (no console popup)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    contents_directory='lib',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DupeClean',
)
