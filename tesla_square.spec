# -*- mode: python ; coding: utf-8 -*-
# Recette PyInstaller de l'application. À lancer via compiler_app.sh.
import os

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
# librosa charge ses modules à la demande : l'analyse statique n'en voit pas tous.
for paquet in ("librosa", "pretty_midi", "soxr"):
    d, b, h = collect_all(paquet)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["tesla_square_gui.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "tkinter", "IPython", "pytest", "PyQt5", "PyQt6", "basic_pitch", "tensorflow"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Tesla Square",
    console=False,
    target_arch="arm64",
)
coll = COLLECT(exe, a.binaries, a.datas, name="Tesla Square")
app = BUNDLE(
    coll,
    name="Tesla Square.app",
    icon=os.environ["TESLA_ICNS"],
    bundle_identifier="local.tesla-square.app",
    info_plist={
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHumanReadableCopyright": "",
    },
)
