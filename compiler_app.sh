#!/bin/bash
# Compile "Tesla Square.app" (Apple Silicon), passe les tests et l'installe dans /Applications.
#
# Tout est construit hors du projet : sur un Bureau synchronisé avec iCloud, les
# fichiers de .venv peuvent recevoir le drapeau « hidden », et Qt ne trouve plus
# ses modules. iCloud peut aussi décharger les fichiers d'une application.
set -euo pipefail
cd "$(dirname "$0")"

TRAVAIL="$HOME/Library/Caches/TeslaSquare/compilation"
VENV="$TRAVAIL/venv"
VENV_BP="$TRAVAIL/venv-transcripteur"  # basic-pitch : Python 3.9, celui de macOS
export PYTHONDONTWRITEBYTECODE=1

[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --only-binary=:all: -r requirements.txt -r requirements-app.txt
[ -x "$VENV_BP/bin/python" ] || /usr/bin/python3 -m venv "$VENV_BP"
"$VENV_BP/bin/pip" install -q --only-binary=:all: basic-pitch pyinstaller

"$VENV/bin/python" -m pytest -q -p no:cacheprovider

rm -rf "$TRAVAIL/dist" "$TRAVAIL/work" "$TRAVAIL/icone" "$TRAVAIL/dist-transcripteur" "$TRAVAIL/work-transcripteur"
mkdir -p "$TRAVAIL/icone"
"$VENV/bin/python" creer_icone.py "$TRAVAIL/icone/TeslaSquare.iconset"
export TESLA_ICNS="$TRAVAIL/icone/TeslaSquare.icns"
iconutil -c icns "$TRAVAIL/icone/TeslaSquare.iconset" -o "$TESLA_ICNS"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
    --distpath "$TRAVAIL/dist" --workpath "$TRAVAIL/work" tesla_square.spec

# Le transcripteur est compilé à part, rangé dans l'application, et l'ensemble est re-signé.
"$VENV_BP/bin/python" -m PyInstaller --noconfirm --clean \
    --distpath "$TRAVAIL/dist-transcripteur" --workpath "$TRAVAIL/work-transcripteur" transcripteur.spec
ditto "$TRAVAIL/dist-transcripteur/transcripteur" "$TRAVAIL/dist/Tesla Square.app/Contents/Resources/transcripteur"
codesign --force --sign - "$TRAVAIL/dist/Tesla Square.app"
codesign --verify --strict "$TRAVAIL/dist/Tesla Square.app"

# Vérifie l'application compilée, avec un environnement aussi pauvre que celui du Finder.
# Sans cache numba, pour exercer la compilation LLVM, celle d'un premier lancement.
rm -rf "$HOME/Library/Caches/TeslaSquare/numba"
ESSAI="$TRAVAIL/essai"
rm -rf "$ESSAI"
mkdir -p "$ESSAI"
"$VENV/bin/python" -c "
import numpy as np, soundfile as sf, sys
t = np.arange(88200) / 44100
sf.write(sys.argv[1], 0.5 * np.sin(2 * np.pi * 440 * t), 44100)" "$ESSAI/la.wav"
env -i HOME="$HOME" PATH=/usr/bin:/bin "$TRAVAIL/dist/Tesla Square.app/Contents/MacOS/Tesla Square" \
    --auto-test "$ESSAI/la.wav" "$ESSAI/sortie"
[ -s "$ESSAI/sortie/la_melodie.wav" ]
"$VENV/bin/python" -c "
import numpy as np, soundfile as sf, sys
t = np.arange(132300) / 44100
sf.write(sys.argv[1], sum(0.3 / k * np.sin(2 * np.pi * 196 * k * t) for k in range(1, 6)), 44100)" "$ESSAI/sol.wav"
env -i HOME="$HOME" PATH=/usr/bin:/bin "$TRAVAIL/dist/Tesla Square.app/Contents/MacOS/Tesla Square" \
    --cli "$ESSAI/sol.wav" --mode instru --sortie "$ESSAI/sortie"
[ -s "$ESSAI/sortie/sol_instru.wav" ]

rm -rf "/Applications/Tesla Square.app"
ditto "$TRAVAIL/dist/Tesla Square.app" "/Applications/Tesla Square.app"
rm -rf "$TRAVAIL/dist" "$TRAVAIL/work" "$TRAVAIL/icone" "$ESSAI" "$TRAVAIL/dist-transcripteur" "$TRAVAIL/work-transcripteur"
echo "Terminé : /Applications/Tesla Square.app"
