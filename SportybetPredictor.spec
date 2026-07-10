# -*- mode: python ; coding: utf-8 -*-
import os
import playwright

# Playwright's Node driver must ship inside the exe (no PyInstaller hook exists)
playwright_driver = os.path.join(os.path.dirname(playwright.__file__), 'driver')

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        (playwright_driver, 'playwright/driver'),
    ],
    # These are imported dynamically via importlib at runtime, so PyInstaller
    # cannot detect them — without this the exe cannot scrape at all
    hiddenimports=[
        'sportybet_virtual_scraper_v1',
        'sportybet_virtual_odds_v1',
        'sportybet_virtual_racing_scraper_v2',
    ],
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
    a.binaries,
    a.datas,
    [],
    name='SportybetPredictor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
