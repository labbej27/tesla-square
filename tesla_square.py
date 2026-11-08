#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tesla_square.py — convertit de la musique en audio adapté à une bobine Tesla
musicale Bluetooth (SSTC de bureau).

Ces bobines ne rendent correctement qu'un signal carré, idéalement une seule
note à la fois. Quatre modes :

  clip     rapide : passe-bande, compression, puis écrêtage en carré (np.sign).
  melodie  recommandé : extrait la mélodie (pYIN) et resynthétise un carré pur.
  midi     synthétise un fichier .mid (ou l'audio converti par basic-pitch).
  instru   transcrit l'audio en MIDI, garde la plage de l'instrumentale et mélange
           plusieurs voix carrées : le plus proche du morceau.

Sortie : WAV mono 44,1 kHz 16 bits dans sortie/<original>_<mode>.wav

Exemple :
    python tesla_square.py ma_chanson.mp3 --mode melodie --quantize --play
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage, signal
from tqdm import tqdm

# --- Constantes -------------------------------------------------------------

SR = 44100  # fréquence d'échantillonnage de sortie (Hz)
VOLUME_MAX = 0.95  # plafond absolu du niveau de sortie
FONDU_S = 0.020  # fondu d'entrée/sortie (s)
EPS = 1e-9  # marge d'arrondi sur la phase (en cycles)

CHEMINS_FFMPEG = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
AFCONVERT = "/usr/bin/afconvert"

EXT_AUDIO = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
EXT_MIDI = {".mid", ".midi"}

# Analyse de la mélodie : pYIN travaille à 22 050 Hz, c'est suffisant jusqu'à
# 1500 Hz et bien plus rapide qu'à 44,1 kHz.
SR_ANALYSE = 22050
HOP_ANALYSE = 256  # environ 11,6 ms par trame
FMIN = 80.0
FMAX = 1500.0
HYSTERESIS_NOTE = 0.75  # écart (demi-tons) à partir duquel on change de note
SILENCE_REEL_S = 0.3  # en son continu, seul un vrai silence aussi long coupe le son

# Compression du mode clip
COMP_SEUIL_DB = -24.0
COMP_RATIO = 4.0
BLOC_ENVELOPPE_S = 0.005  # l'enveloppe est calculée par blocs de 5 ms
HYSTERESIS_GATE_DB = 6.0  # le gate se referme 6 dB sous son seuil d'ouverture


class ErreurTesla(Exception):
    """Erreur prévue : son message est affiché tel quel, sans trace Python."""


# --- Entrées / sorties ------------------------------------------------------


def trouver_ffmpeg() -> str | None:
    """Cherche ffmpeg, y compris là où Homebrew l'installe.

    Une application lancée depuis le Finder ne voit pas /opt/homebrew/bin dans
    son PATH : shutil.which seul ne le trouverait pas.
    """
    trouve = shutil.which("ffmpeg")
    if trouve:
        return trouve
    return next((c for c in CHEMINS_FFMPEG if Path(c).exists()), None)


def decoder_avec_ffmpeg(ffmpeg: str, chemin: Path, preview: float | None) -> np.ndarray:
    """Décode en mono 44,1 kHz avec ffmpeg, directement en mémoire."""
    commande = [ffmpeg, "-nostdin", "-v", "error", "-i", str(chemin)]
    if preview is not None:
        commande += ["-t", str(preview)]
    commande += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]

    resultat = subprocess.run(commande, capture_output=True)
    if resultat.returncode != 0:
        detail = resultat.stderr.decode("utf-8", "replace").strip()
        raise ErreurTesla(f"Impossible de décoder « {chemin.name} » : {detail}")
    return np.frombuffer(resultat.stdout, dtype=np.float32).copy()


def decoder_avec_afconvert(afconvert: str, chemin: Path, preview: float | None) -> np.ndarray:
    """Décode en mono 44,1 kHz avec afconvert, l'outil fourni avec macOS."""
    with tempfile.TemporaryDirectory() as dossier:
        temporaire = Path(dossier) / "decode.wav"
        commande = [afconvert, "-f", "WAVE", "-d", f"LEF32@{SR}", "-c", "1", str(chemin.resolve()), str(temporaire)]
        resultat = subprocess.run(commande, capture_output=True)
        if resultat.returncode != 0 or not temporaire.exists():
            detail = resultat.stderr.decode("utf-8", "replace").strip()
            raise ErreurTesla(f"Impossible de décoder « {chemin.name} » : {detail}")
        audio, _ = sf.read(str(temporaire), dtype="float32")
    return audio if preview is None else audio[: int(preview * SR)]


def decoder_audio(chemin: Path, preview: float | None = None) -> np.ndarray:
    """Décode n'importe quel format audio en mono 44,1 kHz (float32).

    On utilise ffmpeg s'il est installé, sinon afconvert : l'application n'a ainsi
    besoin d'aucune installation sur le Mac. On n'utilise pas pydub, qui ne
    fonctionne plus sans le module audioop, retiré de Python à partir de 3.13.
    """
    ffmpeg = trouver_ffmpeg()
    afconvert = shutil.which("afconvert") or (AFCONVERT if Path(AFCONVERT).exists() else None)
    if ffmpeg:
        audio = decoder_avec_ffmpeg(ffmpeg, chemin, preview)
    elif afconvert:
        audio = decoder_avec_afconvert(afconvert, chemin, preview)
    else:
        raise ErreurTesla("ffmpeg est introuvable. Installe-le avec : brew install ffmpeg")

    if audio.size == 0:
        raise ErreurTesla(f"« {chemin.name} » ne contient aucun audio.")
    # Un fichier abîmé peut contenir des NaN, qui contamineraient tous les filtres.
    return np.nan_to_num(audio)


def ecrire_wav(chemin: Path, audio: np.ndarray) -> None:
    """Écrit un WAV mono 44,1 kHz 16 bits.

    La conversion en entiers est faite ici pour que les niveaux du carré soient
    exactement reproductibles dans le fichier.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    entiers = np.round(np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    sf.write(str(chemin), entiers, SR, subtype="PCM_16")


# --- Outils communs ---------------------------------------------------------


def niveaux_carre(duty: float) -> tuple[float, float]:
    """Renvoie les niveaux (haut, bas) d'un carré de moyenne nulle, crête à 1.

    Avec ±1 et un rapport cyclique différent de 0,5, le signal aurait une
    composante continue : les étages audio la filtrent et cela produit un
    « toc » à chaque départ de note. On ajuste donc le niveau du plateau le plus
    long. À duty = 0,5, on retrouve exactement ±1.
    """
    if duty <= 0.5:
        return 1.0, -duty / (1.0 - duty)
    return (1.0 - duty) / duty, -1.0


def synthese_carre(frequences: np.ndarray, duty: float) -> np.ndarray:
    """Synthétise un carré à partir d'une fréquence par échantillon (0 = silence).

    La phase est accumulée sur toute la durée d'un passage sonore : elle reste
    continue quand la note change, donc pas de clic. Chaque passage commence au
    début d'un cycle et s'arrête à la fin du dernier cycle complet, sans
    impulsion tronquée. Le carré reste volontairement non filtré : deux niveaux
    francs valent mieux pour la bobine qu'un signal adouci.
    """
    sortie = np.zeros(frequences.size, dtype=np.float64)
    haut, bas = niveaux_carre(duty)

    # Repère les débuts et fins de chaque passage sonore.
    actif = np.concatenate(([0], (frequences > 0).astype(np.int8), [0]))
    bords = np.diff(actif)
    debuts = np.flatnonzero(bords == 1)
    fins = np.flatnonzero(bords == -1)

    for debut, fin in zip(debuts, fins):
        # La phase vaut 0 sur le premier échantillon du passage. La marge EPS
        # absorbe les erreurs d'arrondi de la somme, pour qu'une frontière de
        # cycle tombe toujours du bon côté.
        phase = np.concatenate(([0.0], np.cumsum(frequences[debut:fin] / SR))) + EPS
        cycles_complets = np.floor(phase[-1])
        if cycles_complets < 1:
            continue  # moins d'un cycle : trop court pour être une note
        n = int(np.searchsorted(phase, cycles_complets))
        sortie[debut:debut + n] = np.where(phase[:n] % 1.0 < duty, haut, bas)
    return sortie


def appliquer_fondus(audio: np.ndarray, duree: float = FONDU_S) -> np.ndarray:
    """Applique un fondu linéaire d'entrée et de sortie pour éviter les claquements."""
    n = min(int(round(duree * SR)), audio.size // 2)
    if n == 0:
        return audio
    audio = audio.copy()
    rampe = np.arange(n, dtype=np.float64) / n
    audio[:n] *= rampe
    audio[-n:] *= rampe[::-1]
    return audio


def midi_vers_hz(notes: np.ndarray) -> np.ndarray:
    """Convertit des numéros de note MIDI (éventuellement fractionnaires) en Hz."""
    return 440.0 * 2.0 ** ((notes - 69.0) / 12.0)


# --- Mode clip --------------------------------------------------------------


def enveloppe_db(audio: np.ndarray, taille_bloc: int) -> np.ndarray:
    """Enveloppe RMS par blocs, en dB, lissée sur 15 ms pour ignorer les pics isolés."""
    n_blocs = max(1, audio.size // taille_bloc)
    if audio.size < taille_bloc:
        blocs = audio[None, :]
    else:
        blocs = audio[: n_blocs * taille_bloc].reshape(n_blocs, taille_bloc)
    puissance = np.mean(blocs.astype(np.float64) ** 2, axis=1)
    puissance = ndimage.uniform_filter1d(puissance, size=3, mode="nearest")
    return 10.0 * np.log10(puissance + 1e-12)


def compresser(audio: np.ndarray) -> np.ndarray:
    """Normalise le signal et le comprime pour rapprocher passages forts et faibles.

    Le gate agit ensuite sur ce signal : les passages calmes d'un morceau restent
    au-dessus du seuil alors que le bruit de fond reste en dessous.
    """
    crete = np.max(np.abs(audio))
    if crete == 0:
        return audio
    audio = audio / crete

    taille_bloc = int(BLOC_ENVELOPPE_S * SR)
    niveau = enveloppe_db(audio, taille_bloc)
    gain_db = -(1.0 - 1.0 / COMP_RATIO) * np.maximum(0.0, niveau - COMP_SEUIL_DB)
    # Gain interpolé entre les centres de blocs : aucune marche audible.
    centres = (np.arange(niveau.size) + 0.5) * taille_bloc
    gain = 10.0 ** (np.interp(np.arange(audio.size), centres, gain_db) / 20.0)
    audio = audio * gain
    return audio / np.max(np.abs(audio))


def masque_gate(audio: np.ndarray, gate_db: float) -> np.ndarray:
    """Calcule le masque du noise gate, avec hystérésis pour éviter qu'il ne batte.

    Le gate s'ouvre au-dessus de gate_db et ne se referme que 6 dB plus bas.
    """
    taille_bloc = int(BLOC_ENVELOPPE_S * SR)
    niveau = enveloppe_db(audio, taille_bloc)
    ouverture = niveau > gate_db
    maintien = niveau > gate_db - HYSTERESIS_GATE_DB
    if not ouverture.any():
        return np.zeros(audio.size, dtype=bool)
    blocs = ndimage.binary_propagation(ouverture, mask=maintien)
    masque = np.repeat(blocs, taille_bloc)
    if masque.size < audio.size:  # fin de fichier plus courte qu'un bloc
        masque = np.concatenate((masque, np.full(audio.size - masque.size, masque[-1])))
    return masque[: audio.size]


def carre_rapport_cyclique(audio: np.ndarray, duty: float) -> np.ndarray:
    """Écrête en carré en imposant le rapport cyclique, cycle par cycle.

    On repère les passages à zéro montants du signal : chaque cycle garde sa
    durée (donc la hauteur de la note), seule la largeur de l'impulsion change.
    """
    negatif = audio < 0
    montants = np.flatnonzero(negatif[:-1] & ~negatif[1:]) + 1
    sortie = np.zeros(audio.size, dtype=np.float64)
    if montants.size < 2:
        return sortie
    haut, bas = niveaux_carre(duty)
    # Position dans le cycle courant : la partie entière numérote le cycle, la
    # partie fractionnaire est la phase. Avant le premier et après le dernier
    # passage à zéro, il n'y a pas de cycle complet : on laisse le silence.
    indices = np.arange(montants[0], montants[-1])
    phase = np.interp(indices, montants, np.arange(montants.size)) % 1.0
    sortie[indices] = np.where(phase < duty, haut, bas)
    return sortie


def mode_clip(audio: np.ndarray, low: float, high: float, gate_db: float, duty: float) -> np.ndarray:
    """Mode clip : mono → passe-bande → compression → carré avec noise gate."""
    filtre = signal.butter(4, [low, high], btype="bandpass", fs=SR, output="sos")
    filtre_audio = signal.sosfilt(filtre, audio.astype(np.float64))
    filtre_audio = compresser(filtre_audio)

    if duty == 0.5:
        carre = np.sign(filtre_audio)
        carre[carre == 0] = 1.0  # un échantillon pile à zéro ne doit pas créer un 3e niveau
    else:
        carre = carre_rapport_cyclique(filtre_audio, duty)
    return carre * masque_gate(filtre_audio, gate_db)


# --- Mode melodie -----------------------------------------------------------


def lisser_mediane(notes: np.ndarray, taille: int = 5) -> np.ndarray:
    """Filtre médian appliqué séparément à chaque zone voisée, sans franchir les silences."""
    sortie = notes.copy()
    voise = ~np.isnan(notes)
    etiquettes, nb = ndimage.label(voise)
    for tranche in ndimage.find_objects(etiquettes, nb):
        zone = tranche[0]
        sortie[zone] = ndimage.median_filter(notes[zone], size=taille, mode="nearest")
    return sortie


def combler_trous(notes: np.ndarray, max_trames: int) -> np.ndarray:
    """Comble les très courts décrochages de détection au milieu d'une note."""
    sortie = notes.copy()
    n = 0
    for i, valeur in enumerate(notes):
        if np.isnan(valeur):
            n += 1
            continue
        if 0 < n <= max_trames and i - n > 0:
            sortie[i - n:i] = notes[i - n - 1]
        n = 0
    return sortie


def segmenter_notes(notes: np.ndarray) -> list[tuple[int, int]]:
    """Découpe la courbe de hauteur en notes [début, fin[.

    Une nouvelle note commence quand la hauteur s'éloigne de plus de
    HYSTERESIS_NOTE demi-ton du début de la note courante : un vibrato ne
    découpe donc pas la note, contrairement à un arrondi trame par trame.
    """
    segments = []
    debut = None
    for i, valeur in enumerate(notes):
        if np.isnan(valeur):
            if debut is not None:
                segments.append((debut, i))
                debut = None
        elif debut is None:
            debut = i
        elif abs(valeur - notes[debut]) > HYSTERESIS_NOTE:
            segments.append((debut, i))
            debut = i
    if debut is not None:
        segments.append((debut, notes.size))
    return segments


def nettoyer_notes(notes: np.ndarray, min_trames: int, quantize: bool) -> np.ndarray:
    """Applique la durée minimale de note et la quantification au demi-ton.

    Une note trop courte est absorbée par une note qu'elle touche. Isolée, elle
    est considérée comme une fausse détection et devient du silence.
    """
    segments = segmenter_notes(notes)
    sortie = notes.copy()

    for rang, (debut, fin) in enumerate(segments):
        if quantize:
            sortie[debut:fin] = np.round(np.median(notes[debut:fin]))
        if fin - debut >= min_trames:
            continue
        colle_avant = rang > 0 and segments[rang - 1][1] == debut
        colle_apres = rang + 1 < len(segments) and segments[rang + 1][0] == fin
        if colle_avant:
            sortie[debut:fin] = sortie[debut - 1]  # déjà traité, donc quantifié
        elif colle_apres:
            voisin_debut, voisin_fin = segments[rang + 1]
            voisin = np.median(notes[voisin_debut:voisin_fin])
            sortie[debut:fin] = np.round(voisin) if quantize else notes[voisin_debut]
        else:
            sortie[debut:fin] = np.nan
    return sortie


def tenir_notes(notes: np.ndarray, silence: np.ndarray, min_silence_trames: int) -> np.ndarray:
    """Tient chaque note jusqu'à la suivante, comme un synthé joué legato.

    La détection perd souvent la note au milieu d'un morceau chargé : ces trous ne
    sont pas de la musique. Seul un vrai silence du morceau, au moins aussi long
    que min_silence_trames, coupe encore le son. Après une coupure, ce qui précède
    la première note détectée prend déjà cette note.
    """
    etiquettes, nb = ndimage.label(silence)
    coupure = np.zeros(notes.size, dtype=bool)
    for tranche in ndimage.find_objects(etiquettes, nb):
        zone = tranche[0]
        if zone.stop - zone.start >= min_silence_trames:
            coupure[zone] = True

    sortie = notes.copy()
    sortie[coupure] = np.nan
    passages, nb = ndimage.label(~coupure)
    for tranche in ndimage.find_objects(passages, nb):
        zone = sortie[tranche[0]]  # vue : modifie sortie directement
        connues = np.flatnonzero(~np.isnan(zone))
        if connues.size == 0:
            continue
        # Indice de la dernière note connue à chaque trame ; avant la première, la première.
        derniere = np.zeros(zone.size, dtype=np.int64)
        derniere[connues] = connues
        derniere = np.maximum.accumulate(derniere)
        derniere[: connues[0]] = connues[0]
        zone[:] = zone[derniere]
    return sortie


def extraire_melodie(
    audio: np.ndarray, min_note_ms: float, quantize: bool, gate_db: float, hp_analyse: float = 200.0,
    continu: bool = False,
) -> np.ndarray:
    """Extrait la mélodie sous forme de numéros MIDI par trame (NaN = silence).

    pYIN suit une seule hauteur : sur un morceau complet, il se cale facilement
    sur la basse ou un accord. Le passe-haut hp_analyse retire surtout ces graves
    avant l'analyse. La hauteur d'une note reste trouvable grâce à ses
    harmoniques, même si sa fondamentale tombe sous la coupure.
    """
    import librosa  # import local : long à charger et inutile aux autres modes

    analyse = librosa.resample(audio, orig_sr=SR, target_sr=SR_ANALYSE)
    if hp_analyse > 0:
        filtre = signal.butter(4, hp_analyse, btype="highpass", fs=SR_ANALYSE, output="sos")
        analyse = signal.sosfilt(filtre, analyse).astype(np.float32)
    f0, voise, _ = librosa.pyin(
        analyse, fmin=FMIN, fmax=FMAX, sr=SR_ANALYSE,
        frame_length=2048, hop_length=HOP_ANALYSE, fill_na=np.nan,
    )
    f0 = np.where(voise, f0, np.nan)

    # pYIN peut « entendre » une note dans un bruit très faible : on impose aussi
    # un niveau minimal, relatif à la trame la plus forte du morceau.
    rms = librosa.feature.rms(y=analyse, frame_length=2048, hop_length=HOP_ANALYSE)[0]
    n = min(rms.size, f0.size)
    f0 = f0[:n]
    niveau = 20.0 * np.log10(rms[:n] / (np.max(rms) + 1e-12) + 1e-12)
    f0[niveau < gate_db] = np.nan

    with np.errstate(invalid="ignore"):
        notes = 69.0 + 12.0 * np.log2(f0 / 440.0)
    duree_trame_ms = 1000.0 * HOP_ANALYSE / SR_ANALYSE
    min_trames = max(1, int(round(min_note_ms / duree_trame_ms)))

    notes = lisser_mediane(notes)
    notes = combler_trous(notes, min_trames // 2)
    notes = nettoyer_notes(notes, min_trames, quantize)
    if continu:
        min_silence = int(round(SILENCE_REEL_S * SR_ANALYSE / HOP_ANALYSE))
        notes = tenir_notes(notes, niveau < gate_db, min_silence)
    return notes


def extraire_riff(
    audio: np.ndarray, min_note_ms: float, gate_db: float, bas_hz: float, haut_hz: float, continu: bool = True
) -> np.ndarray:
    """Suit un riff : à chaque instant, la note la plus forte entre bas_hz et haut_hz.

    pYIN n'annonce une note que s'il est sûr de lui, ce qui arrive rarement dans un
    mix chargé. Ici, il y a toujours une note : sur un morceau dont le riff domine
    sa plage, le suivi est plus régulier. Choisir une plage qui s'arrête sous la
    voix l'empêche d'être suivie. Renvoie des numéros MIDI entiers par trame.
    """
    import librosa  # import local : long à charger et inutile aux autres modes

    analyse = librosa.resample(audio, orig_sr=SR, target_sr=SR_ANALYSE)
    grave = int(np.round(librosa.hz_to_midi(bas_hz)))
    aigu = int(np.round(librosa.hz_to_midi(haut_hz)))
    spectre = np.abs(librosa.cqt(
        analyse, sr=SR_ANALYSE, hop_length=HOP_ANALYSE, fmin=librosa.midi_to_hz(grave),
        n_bins=aigu - grave + 1, bins_per_octave=12,
    ))
    spectre = ndimage.median_filter(spectre, size=(1, 5))  # lissage dans le temps seulement
    notes = (np.argmax(spectre, axis=0) + grave).astype(np.float64)

    rms = librosa.feature.rms(y=analyse, frame_length=2048, hop_length=HOP_ANALYSE)[0]
    n = min(rms.size, notes.size)
    notes = notes[:n]
    silence = 20.0 * np.log10(rms[:n] / (np.max(rms) + 1e-12) + 1e-12) < gate_db
    notes[silence] = np.nan

    duree_trame_ms = 1000.0 * HOP_ANALYSE / SR_ANALYSE
    notes = nettoyer_notes(notes, max(1, int(round(min_note_ms / duree_trame_ms))), True)
    if continu:
        notes = tenir_notes(notes, silence, int(round(SILENCE_REEL_S * SR_ANALYSE / HOP_ANALYSE)))
    return notes


def mode_melodie(
    audio: np.ndarray, min_note_ms: float, quantize: bool, octave: int, gate_db: float, duty: float,
    hp_analyse: float = 200.0, continu: bool = False, plage_riff: tuple[float, float] | None = None,
) -> np.ndarray:
    """Mode melodie : extrait la mélodie principale (ou le riff) puis resynthétise un carré pur."""
    if plage_riff is None:
        notes = extraire_melodie(audio, min_note_ms, quantize, gate_db, hp_analyse, continu)
    else:
        notes = extraire_riff(audio, min_note_ms, gate_db, plage_riff[0], plage_riff[1], continu)
    if not np.any(~np.isnan(notes)):
        raise ErreurTesla(
            "Aucune mélodie détectée. Essaie --gate plus bas, --min-note plus court, "
            "ou le mode clip si le morceau n'a pas de voix principale claire."
        )

    # Chaque échantillon prend la note de sa trame : pas de glissando parasite.
    trames = np.round(np.arange(audio.size) * SR_ANALYSE / (SR * HOP_ANALYSE)).astype(np.int64)
    par_echantillon = notes[np.minimum(trames, notes.size - 1)]
    frequences = np.nan_to_num(midi_vers_hz(par_echantillon) * 2.0 ** octave, nan=0.0)
    return synthese_carre(frequences, duty)


# --- Mode midi --------------------------------------------------------------


def charger_midi(chemin: Path):
    """Charge un fichier .mid et renvoie un objet PrettyMIDI."""
    import pretty_midi

    try:
        return pretty_midi.PrettyMIDI(str(chemin))
    except Exception as erreur:  # pretty_midi lève des types très variés
        raise ErreurTesla(f"Fichier MIDI illisible « {chemin.name} » : {erreur}") from erreur


def chemin_transcripteur() -> Path | None:
    """Renvoie le transcripteur rangé dans l'application compilée, s'il y en a un."""
    if not getattr(sys, "frozen", False):
        return None
    chemin = Path(sys.executable).resolve().parent.parent / "Resources" / "transcripteur" / "transcripteur"
    return chemin if chemin.exists() else None


def transcrire_a_part(transcripteur: Path, chemin: Path, preview: float | None):
    """Transcrit l'audio avec le programme compilé à part (voir transcripteur.py)."""
    with tempfile.TemporaryDirectory() as dossier:
        midi = Path(dossier) / "transcription.mid"
        decode = Path(dossier) / "decode.wav"

        def lancer(source: Path) -> subprocess.CompletedProcess:
            return subprocess.run([str(transcripteur), str(source), str(midi)], capture_output=True)

        if preview is None:
            resultat = lancer(chemin)
        if preview is not None or resultat.returncode != 0:
            # Avec un extrait, ou un format que le transcripteur ne décode pas lui-même.
            sf.write(str(decode), decoder_audio(chemin, preview), SR, subtype="PCM_16")
            resultat = lancer(decode)
        if resultat.returncode != 0 or not midi.exists():
            lignes = resultat.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ErreurTesla(f"Transcription impossible de « {chemin.name} » : {lignes[-1] if lignes else 'erreur inconnue'}")
        return charger_midi(midi)


def audio_vers_midi(chemin: Path, preview: float | None):
    """Transcrit un fichier audio en MIDI avec basic-pitch (dépendance optionnelle)."""
    try:
        from basic_pitch.inference import predict
    except ImportError as erreur:
        transcripteur = chemin_transcripteur()
        if transcripteur is not None:
            return transcrire_a_part(transcripteur, chemin, preview)
        raise ErreurTesla(
            f"Le mode midi a besoin d'un fichier .mid : « {chemin.name} » est un fichier audio "
            "et basic-pitch n'est pas installé (voir la section basic-pitch du README). "
            "Sinon, utilise --mode melodie."
        ) from erreur

    if preview is None:
        return predict(str(chemin))[1]
    # Avec --preview, on ne transcrit que le début du morceau.
    with tempfile.TemporaryDirectory() as dossier:
        extrait = Path(dossier) / "extrait.wav"
        sf.write(str(extrait), decoder_audio(chemin, preview), SR, subtype="PCM_16")
        return predict(str(extrait))[1]


def lister_notes(pm, preview: float | None, octave: int) -> list[tuple[int, int, int]]:
    """Renvoie les notes (début, fin, hauteur) en échantillons, batteries exclues."""
    notes = []
    for instrument in pm.instruments:
        if instrument.is_drum:
            continue
        for note in instrument.notes:
            fin = min(note.end, preview) if preview is not None else note.end
            debut, fin = int(round(note.start * SR)), int(round(fin * SR))
            if fin > debut:
                notes.append((debut, fin, note.pitch + 12 * octave))
    return notes


def mode_midi(pm, duty: float, octave: int, poly: bool, preview: float | None) -> np.ndarray:
    """Mode midi : synthèse carrée, monophonique sauf si --poly est demandé."""
    notes = lister_notes(pm, preview, octave)
    if not notes:
        raise ErreurTesla("Aucune note exploitable dans le MIDI (hors batterie).")
    longueur = max(fin for _, fin, _ in notes)

    if poly:
        # Une voix carrée par note. Le mélange n'est plus binaire : c'est le prix
        # à payer, et le rendu dépendra beaucoup de la bobine.
        sortie = np.zeros(longueur, dtype=np.float64)
        for debut, fin, hauteur in notes:
            frequences = np.full(fin - debut, float(midi_vers_hz(np.float64(hauteur))))
            sortie[debut:fin] += synthese_carre(frequences, duty)
        crete = np.max(np.abs(sortie))
        return sortie / crete if crete > 0 else sortie

    # Monophonique : on écrit les notes de la plus grave à la plus aiguë, chacune
    # écrasant les précédentes. Il reste la plus aiguë à chaque instant.
    hauteurs = np.full(longueur, np.nan)
    for debut, fin, hauteur in sorted(notes, key=lambda n: n[2]):
        hauteurs[debut:fin] = hauteur
    return synthese_carre(np.nan_to_num(midi_vers_hz(hauteurs), nan=0.0), duty)


def garder_instru(pm, bas_hz: float, haut_hz: float) -> None:
    """Ne garde que les notes de la plage, puis comble les silences entre elles.

    Couper au-dessus de la plage retire le gros de la voix, qui domine les aigus. Un
    silence restant prolonge la note qui se termine en dernier : le son ne s'arrête
    pas tant qu'une note doit encore venir.
    """
    grave, aigu = (int(round(69.0 + 12.0 * np.log2(f / 440.0))) for f in (bas_hz, haut_hz))
    notes = []
    for instrument in pm.instruments:
        if not instrument.is_drum:
            instrument.notes = [n for n in instrument.notes if grave <= n.pitch <= aigu]
            notes += instrument.notes
    notes.sort(key=lambda n: n.start)
    if not notes:
        return
    derniere = notes[0]
    for note in notes[1:]:
        if note.start > derniere.end:
            derniere.end = note.start
        if note.end >= derniere.end:
            derniere = note


# --- Traitement d'un fichier ------------------------------------------------


def resoudre_mode(chemin: Path, mode: str | None) -> str:
    """Choisit le mode : midi pour un .mid, melodie par défaut pour un fichier audio."""
    est_midi = chemin.suffix.lower() in EXT_MIDI
    if mode is None:
        return "midi" if est_midi else "melodie"
    if est_midi and mode != "midi":
        raise ErreurTesla(f"« {chemin.name} » est un fichier MIDI : utilise --mode midi.")
    return mode


def traiter_fichier(chemin: Path, mode: str, sortie: Path, args: argparse.Namespace) -> str | None:
    """Convertit un fichier et écrit le WAV `sortie`. Renvoie un avertissement éventuel."""
    if mode == "instru":
        pm = audio_vers_midi(chemin, args.preview)
        garder_instru(pm, args.instru_bas, args.instru_haut)
        audio = mode_midi(pm, args.duty, args.octave, True, args.preview)
    elif mode == "midi":
        if chemin.suffix.lower() in EXT_MIDI:
            pm = charger_midi(chemin)
        else:
            pm = audio_vers_midi(chemin, args.preview)
        audio = mode_midi(pm, args.duty, args.octave, args.poly, args.preview)
    else:
        source = decoder_audio(chemin, args.preview)
        if mode == "clip":
            audio = mode_clip(source, args.low, args.high, args.gate, args.duty)
        else:
            audio = mode_melodie(
                source, args.min_note, args.quantize, args.octave, args.gate, args.duty, args.hp_analyse,
                args.continu, (args.riff_bas, args.riff_haut) if args.riff else None,
            )

    ecrire_wav(sortie, appliquer_fondus(audio) * args.volume)
    if not np.any(audio):
        return f"« {chemin.name} » donne une sortie silencieuse : le seuil de bruit est peut-être trop haut."
    return None


def lister_entrees(entree: Path, mode: str | None) -> list[Path]:
    """Renvoie les fichiers à traiter (le contenu direct du dossier, sans ses sous-dossiers)."""
    if not entree.exists():
        raise ErreurTesla(f"Fichier ou dossier introuvable : {entree}")
    if entree.is_file():
        extensions = EXT_AUDIO | EXT_MIDI
        if entree.suffix.lower() not in extensions:
            raise ErreurTesla(f"Format non pris en charge : « {entree.name} ». Formats : {formats_lisibles()}")
        return [entree]

    extensions = EXT_AUDIO | EXT_MIDI if mode in (None, "midi") else EXT_AUDIO
    fichiers = sorted(
        f for f in entree.iterdir()
        if f.is_file() and f.suffix.lower() in extensions and not f.name.startswith(".")
    )
    if not fichiers:
        raise ErreurTesla(f"Aucun fichier compatible dans « {entree} ». Formats : {formats_lisibles()}")
    return fichiers


def formats_lisibles() -> str:
    """Liste les extensions acceptées, pour les messages d'erreur."""
    return ", ".join(sorted(e.lstrip(".") for e in EXT_AUDIO | EXT_MIDI))


def chemin_afplay() -> str:
    """Renvoie le chemin d'afplay, le lecteur audio fourni avec macOS."""
    afplay = shutil.which("afplay") or ("/usr/bin/afplay" if Path("/usr/bin/afplay").exists() else None)
    if afplay is None:
        raise ErreurTesla("afplay est introuvable : la lecture ne fonctionne que sur macOS.")
    return afplay


def lire_avec_afplay(fichiers: list[Path]) -> None:
    """Lit les fichiers produits avec afplay, l'un après l'autre."""
    afplay = chemin_afplay()
    try:
        for fichier in fichiers:
            print(f"Lecture : {fichier}")
            subprocess.run([afplay, str(fichier)], check=False)
    except KeyboardInterrupt:
        print("\nLecture interrompue.")


# --- Ligne de commande ------------------------------------------------------


def creer_parser() -> argparse.ArgumentParser:
    """Déclare les options de la ligne de commande."""
    p = argparse.ArgumentParser(
        prog="tesla_square.py",
        description="Convertit de la musique en signal carré pour bobine Tesla musicale.",
    )
    p.add_argument("entree", type=Path, help="fichier (mp3, wav, flac, m4a, ogg, mid) ou dossier")
    p.add_argument("--mode", choices=["clip", "melodie", "midi", "instru"],
                   help="mode de conversion (défaut : melodie, ou midi pour un fichier .mid)")
    p.add_argument("--sortie", default="sortie", help="dossier de sortie (défaut : sortie)")

    communs = p.add_argument_group("options communes")
    communs.add_argument("--duty", type=float, default=0.5,
                         help="rapport cyclique (défaut 0.5 ; 0.1–0.3 donne un son plus fin)")
    communs.add_argument("--volume", type=float, default=0.8,
                         help=f"niveau de sortie (défaut 0.8, plafonné à {VOLUME_MAX})")
    communs.add_argument("--preview", type=float, metavar="N", help="ne traite que les N premières secondes")
    communs.add_argument("--play", action="store_true", help="lit le résultat avec afplay à la fin")
    communs.add_argument("--gate", type=float, default=-40.0,
                         help="seuil de bruit en dB, modes clip et melodie (défaut -40)")

    clip = p.add_argument_group("mode clip")
    clip.add_argument("--low", type=float, default=300.0, help="coupure basse en Hz (défaut 300)")
    clip.add_argument("--high", type=float, default=3000.0, help="coupure haute en Hz (défaut 3000)")

    melodie = p.add_argument_group("mode melodie")
    melodie.add_argument("--min-note", type=float, default=60.0, help="durée minimale d'une note en ms (défaut 60)")
    melodie.add_argument("--quantize", action="store_true", help="cale les notes sur le demi-ton le plus proche")
    melodie.add_argument("--hp-analyse", type=float, default=200.0,
                         help="passe-haut avant détection en Hz, 0 pour le couper (défaut 200 ; "
                              "400–500 si l'outil suit les accords au lieu d'une mélodie aiguë)")

    melodie.add_argument("--continu", action=argparse.BooleanOptionalAction, default=True,
                         help="tient chaque note jusqu'à la suivante, sans trous ; seul un vrai silence "
                              "du morceau coupe le son (défaut : activé ; --no-continu pour le couper)")

    melodie.add_argument("--riff", action="store_true",
                         help="suit la note la plus forte de la plage --riff-bas/--riff-haut au lieu de la "
                              "mélodie ; pour un riff répété dans un morceau chargé. Ignore --hp-analyse "
                              "et cale toujours sur les demi-tons")
    melodie.add_argument("--riff-bas", type=float, default=98.0, help="bas de la plage du riff en Hz (défaut 98)")
    melodie.add_argument("--riff-haut", type=float, default=262.0, help="haut de la plage du riff en Hz (défaut 262)")

    midi = p.add_argument_group("mode midi")
    midi.add_argument("--poly", action="store_true", help="mélange plusieurs voix carrées au lieu d'une seule note")

    instru = p.add_argument_group("mode instru")
    instru.add_argument("--instru-bas", type=float, default=65.0, help="note la plus grave gardée, en Hz (défaut 65)")
    instru.add_argument("--instru-haut", type=float, default=262.0, help="note la plus aiguë gardée, en Hz (défaut 262)")

    p.add_argument("--octave", type=int, default=0, help="transposition en octaves, modes melodie et midi (+1, -1…)")
    return p


def valider_options(args: argparse.Namespace) -> None:
    """Vérifie la cohérence des options avant de lancer un traitement parfois long."""
    if not 0.01 <= args.duty <= 0.99:
        raise ErreurTesla("--duty doit être compris entre 0.01 et 0.99.")
    if args.volume <= 0:
        raise ErreurTesla("--volume doit être strictement positif.")
    if args.volume > VOLUME_MAX:
        print(f"Attention : --volume ramené à {VOLUME_MAX} (maximum autorisé).", file=sys.stderr)
        args.volume = VOLUME_MAX
    if args.preview is not None and args.preview <= 0:
        raise ErreurTesla("--preview doit être strictement positif.")
    if not 0 < args.low < args.high < SR / 2:
        raise ErreurTesla(f"Il faut 0 < --low < --high < {SR // 2} Hz.")
    if args.gate > 0:
        raise ErreurTesla("--gate s'exprime en dB et doit être négatif ou nul (ex. -40).")
    if not 0 <= args.hp_analyse < SR_ANALYSE / 2:
        raise ErreurTesla(f"--hp-analyse doit être compris entre 0 et {SR_ANALYSE // 2} Hz.")
    if not 30 <= args.riff_bas < args.riff_haut <= 3000:
        raise ErreurTesla("Il faut 30 ≤ --riff-bas < --riff-haut ≤ 3000 Hz.")
    if not 20 <= args.instru_bas < args.instru_haut <= 8000:
        raise ErreurTesla("Il faut 20 ≤ --instru-bas < --instru-haut ≤ 8000 Hz.")
    if args.min_note < 0:
        raise ErreurTesla("--min-note ne peut pas être négatif.")
    if abs(args.octave) > 3:
        raise ErreurTesla("--octave doit rester entre -3 et +3.")


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée. Renvoie 0 si tout a réussi, 1 sinon."""
    args = creer_parser().parse_args(argv)
    try:
        valider_options(args)
        fichiers = lister_entrees(args.entree, args.mode)
    except ErreurTesla as erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 1

    produits: list[Path] = []
    echecs: list[tuple[Path, str]] = []
    # En lot, une erreur sur un fichier n'arrête pas les suivants.
    for chemin in tqdm(fichiers, desc="Conversion", unit="fichier", disable=len(fichiers) == 1):
        try:
            mode = resoudre_mode(chemin, args.mode)
            sortie = Path(args.sortie) / f"{chemin.stem}_{mode}.wav"
            if sortie in produits:  # ex. morceau.mp3 et morceau.flac dans le même dossier
                raise ErreurTesla(f"« {sortie.name} » existe déjà pour un autre fichier de ce lot : fichier ignoré.")
            avertissement = traiter_fichier(chemin, mode, sortie, args)
            if avertissement:
                print(f"Attention : {avertissement}", file=sys.stderr)
            produits.append(sortie)
            tqdm.write(f"OK : {chemin.name} → {sortie}")
        except ErreurTesla as erreur:
            echecs.append((chemin, str(erreur)))

    for chemin, message in echecs:
        print(f"Erreur ({chemin.name}) : {message}", file=sys.stderr)

    if args.play and produits:
        try:
            lire_avec_afplay(produits)
        except ErreurTesla as erreur:
            print(f"Erreur : {erreur}", file=sys.stderr)
            return 1
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
