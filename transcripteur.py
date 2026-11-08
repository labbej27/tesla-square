#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transcrit un fichier audio en MIDI avec basic-pitch : transcripteur.py entree sortie.mid

basic-pitch ne s'installe pas avec le Python 3.14 de l'application. Ce petit programme
est donc compilé à part, avec Python 3.9, et rangé dans l'application, qui le lance.
"""

import multiprocessing
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    multiprocessing.freeze_support()
    if len(sys.argv) != 3:
        print("Usage : transcripteur entree sortie.mid", file=sys.stderr)
        return 2
    # Le dossier de numba à l'intérieur de l'application n'est pas inscriptible.
    cache = Path.home() / "Library" / "Caches" / "TeslaSquare" / "numba"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = Path(tempfile.mkdtemp())
    os.environ.setdefault("NUMBA_CACHE_DIR", str(cache))

    from basic_pitch.inference import predict

    predict(sys.argv[1])[1].write(sys.argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main())
