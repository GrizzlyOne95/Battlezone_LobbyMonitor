# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['bzr_monitor.py'],
    pathex=[],
    binaries=[],
    # Never bundle bzr_monitor_config.json: it holds the local Discord bot token.
    datas=[
        ('branding/app_icon.ico', 'branding'),
        ('branding/app_icon.png', 'branding'),
    ],
    hiddenimports=['socks', 'sockshandler', 'python_socks'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['branding/pyinstaller_icon_hook.py'],
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
    name='BZLobbyMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='branding/app_icon.ico',
)
