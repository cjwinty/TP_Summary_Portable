# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


def collect_tree(source, target):
    source_path = Path(source)
    if not source_path.exists():
        raise FileNotFoundError(f"Required build asset is missing: {source_path}")

    entries = []
    for item in source_path.rglob('*'):
        if item.is_file():
            relative_parent = item.relative_to(source_path).parent
            entries.append((str(item), str(Path(target) / relative_parent)))
    if not entries:
        raise FileNotFoundError(f"Required build asset folder is empty: {source_path}")
    return entries


tk_seed = Path('build_tools\\portable-test\\TP Query\\_internal')
required_tk_assets = [
    Path('build_tools\\tkinter_pyc\\__init__.pyc'),
    tk_seed / '_tkinter.pyd',
    tk_seed / 'tcl86t.dll',
    tk_seed / 'tk86t.dll',
    tk_seed / '_tcl_data' / 'init.tcl',
    tk_seed / '_tk_data' / 'tk.tcl',
]
for required_tk_asset in required_tk_assets:
    if not required_tk_asset.exists():
        raise FileNotFoundError(f"Missing required Tk build asset: {required_tk_asset}")


datas = [('build_tools\\tcl-test', 'tcl')]
datas += collect_tree('build_tools\\tkinter_pyc', 'tkinter')
datas += collect_tree('build_tools\\portable-test\\TP Query\\_internal\\_tcl_data', '_tcl_data')
datas += collect_tree('build_tools\\portable-test\\TP Query\\_internal\\_tk_data', '_tk_data')
datas += collect_tree('build_tools\\portable-test\\TP Query\\_internal\\tcl8', 'tcl8')
binaries = [
    ('build_tools\\portable-test\\TP Query\\_internal\\_tkinter.pyd', '.'),
    ('build_tools\\portable-test\\TP Query\\_internal\\tcl86t.dll', '.'),
    ('build_tools\\portable-test\\TP Query\\_internal\\tk86t.dll', '.'),
]
hiddenimports = []
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tkcalendar')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('babel')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['tp_query.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['build_tools\\pyi_rth_portable_tkinter.py'],
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
    name='TP Query',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='TP Query',
)
