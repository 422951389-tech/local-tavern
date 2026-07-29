from pathlib import Path


# PyInstaller 把 SPECPATH 设为 spec 所在目录（本项目为 packaging/）。
ROOT = Path(SPECPATH).resolve().parent


a = Analysis(
    [str(ROOT / "desktop" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "web"), "web"),
        (str(ROOT / "prompts"), "prompts"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "httptools",
        "tkinter",
        "uvicorn",
        "watchfiles",
        "websockets",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalTavern",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalTavern",
)
