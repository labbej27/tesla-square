# -*- mode: python ; coding: utf-8 -*-
# Recette PyInstaller du transcripteur (Python 3.9). À lancer via compiler_app.sh.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for paquet in ("basic_pitch", "coremltools", "librosa", "pretty_midi", "soxr"):
    d, b, h = collect_all(paquet)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["transcripteur.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "tkinter", "IPython", "pytest", "tensorflow", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="transcripteur", console=True, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="transcripteur")
