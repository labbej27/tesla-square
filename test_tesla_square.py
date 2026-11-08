# -*- coding: utf-8 -*-
"""Tests de tesla_square.py : un accord et une mélodie connus, passés dans chaque mode.

Lancement : .venv/bin/python -m pytest -v
"""

import shutil
import subprocess
import sys
import types

import numpy as np
import pretty_midi
import pytest
import soundfile as sf
from scipy import ndimage

import tesla_square as ts

SR = 44100
VOLUME = 0.8
NIVEAU = int(round(VOLUME * 32767))
FONDU = int(0.020 * SR)

# La mélodie connue : (note MIDI, durée en s). La4=69 → 440 Hz.
MELODIE = [(69, 0.4), (72, 0.4), (76, 0.4), (79, 0.4), (81, 0.4), (57, 0.4), (64, 0.4)]
ACCORD = [57, 61, 64]  # la majeur : la3, do#4, mi4


def hz(note):
    return 440.0 * 2.0 ** ((note - 69) / 12)


def ton_riche(frequence, duree):
    """Un son harmonique avec attaque douce, plus proche d'un instrument qu'un sinus."""
    t = np.arange(int(duree * SR)) / SR
    son = sum(np.sin(2 * np.pi * frequence * k * t) / k for k in range(1, 5))
    enveloppe = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
    return son * enveloppe


def signal_melodie():
    return 0.5 * np.concatenate([ton_riche(hz(n), d) for n, d in MELODIE])


def signal_accord(duree=1.5):
    return 0.2 * sum(ton_riche(hz(n), duree) for n in ACCORD)


def ecrire(chemin, audio):
    sf.write(str(chemin), audio, SR, subtype="PCM_16")
    return chemin


def lire_entiers(chemin):
    audio, sr = sf.read(str(chemin), dtype="int16")
    return audio, sr


def frequence_mesuree(audio):
    """Fréquence d'un extrait de carré, déduite des fronts montants."""
    negatif = audio <= 0
    fronts = np.flatnonzero(negatif[:-1] & ~negatif[1:])
    assert fronts.size >= 3, "pas assez de cycles dans l'extrait"
    return SR * (fronts.size - 1) / (fronts[-1] - fronts[0])


def cents(mesure, attendu):
    return 1200.0 * abs(np.log2(mesure / attendu))


def verifier_notes(audio, notes, tolerance_cents):
    """Vérifie la hauteur au milieu de chaque note de la séquence."""
    debut = 0.0
    for note, duree in notes:
        a = int((debut + 0.25 * duree) * SR)
        b = int((debut + 0.75 * duree) * SR)
        mesure = frequence_mesuree(audio[a:b])
        assert cents(mesure, hz(note)) < tolerance_cents, f"note {note} : {mesure:.1f} Hz, attendu {hz(note):.1f} Hz"
        debut += duree


def lancer(tmp_path, *arguments):
    """Lance la CLI et vérifie qu'elle réussit."""
    assert ts.main([str(a) for a in arguments] + ["--sortie", str(tmp_path / "sortie")]) == 0


@pytest.fixture
def wav_melodie(tmp_path):
    return ecrire(tmp_path / "melodie.wav", signal_melodie())


@pytest.fixture
def wav_accord(tmp_path):
    return ecrire(tmp_path / "accord.wav", signal_accord())


@pytest.fixture
def mid_accord_melodie(tmp_path):
    """MIDI : la mélodie au-dessus d'un accord tenu très grave, plus une batterie."""
    pm = pretty_midi.PrettyMIDI()
    melodie = pretty_midi.Instrument(0)
    t = 0.0
    for note, duree in MELODIE[:5]:
        melodie.notes.append(pretty_midi.Note(100, note, t, t + duree))
        t += duree
    accord = pretty_midi.Instrument(0)
    accord.notes += [pretty_midi.Note(100, n - 12, 0.0, t) for n in ACCORD]
    batterie = pretty_midi.Instrument(0, is_drum=True)
    batterie.notes.append(pretty_midi.Note(100, 100, 0.0, t + 3.0))  # ne doit jamais sonner
    pm.instruments += [accord, melodie, batterie]
    chemin = tmp_path / "morceau.mid"
    pm.write(str(chemin))
    return chemin


# --- Mode clip --------------------------------------------------------------


@pytest.mark.parametrize("source", [signal_melodie, signal_accord])
def test_clip_sortie_binaire(tmp_path, source):
    entree = ecrire(tmp_path / "e.wav", source())
    lancer(tmp_path, entree, "--mode", "clip")
    audio, sr = lire_entiers(tmp_path / "sortie" / "e_clip.wav")
    assert sr == SR and audio.ndim == 1 and audio.dtype == np.int16
    # Hors fondus, seuls +V, -V et 0 (gate fermé) sont permis.
    assert set(np.unique(audio[FONDU:-FONDU])) <= {-NIVEAU, 0, NIVEAU}
    assert {-NIVEAU, NIVEAU} <= set(np.unique(audio))


def test_clip_conserve_la_hauteur(tmp_path):
    t = np.arange(SR) / SR
    entree = ecrire(tmp_path / "la.wav", 0.5 * np.sin(2 * np.pi * 440 * t))
    lancer(tmp_path, entree, "--mode", "clip")
    audio, _ = lire_entiers(tmp_path / "sortie" / "la_clip.wav")
    assert cents(frequence_mesuree(audio[SR // 4:3 * SR // 4]), 440) < 5


def test_clip_gate_silence_reste_silencieux(tmp_path):
    """Du bruit très faible entre deux notes ne doit surtout pas produire de buzz."""
    rng = np.random.default_rng(0)
    note = 0.5 * ton_riche(440, 0.5)
    bruit = 10 ** (-80 / 20) * rng.standard_normal(SR)
    entree = ecrire(tmp_path / "g.wav", np.concatenate([note, bruit, note]))
    lancer(tmp_path, entree, "--mode", "clip", "--gate", "-40")
    audio, _ = lire_entiers(tmp_path / "sortie" / "g_clip.wav")
    # Marge de 50 ms : le passe-bande laisse traîner un peu la note précédente.
    centre = audio[int(0.55 * SR):int(1.45 * SR)]
    assert np.all(centre == 0)
    assert np.any(audio[int(0.1 * SR):int(0.4 * SR)] != 0)


def test_clip_filtre_passe_bande(tmp_path):
    """Un fort ronflement à 50 Hz, hors bande, ne doit pas dicter les fronts du carré."""
    t = np.arange(SR) / SR
    melange = 0.5 * np.sin(2 * np.pi * 50 * t) + 0.1 * np.sin(2 * np.pi * 1000 * t)
    lancer(tmp_path, ecrire(tmp_path / "b.wav", melange), "--mode", "clip")
    audio, _ = lire_entiers(tmp_path / "sortie" / "b_clip.wav")
    assert cents(frequence_mesuree(audio[SR // 4:3 * SR // 4]), 1000) < 5


def test_clip_low_high_reglables(tmp_path):
    """--low et --high déplacent bien la bande : c'est le 2 kHz qui passe ici."""
    t = np.arange(SR) / SR
    melange = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 2000 * t)
    entree = ecrire(tmp_path / "lh.wav", melange)
    lancer(tmp_path, entree, "--mode", "clip", "--low", "1500", "--high", "4000")
    audio, _ = lire_entiers(tmp_path / "sortie" / "lh_clip.wav")
    assert cents(frequence_mesuree(audio[SR // 4:3 * SR // 4]), 2000) < 5


def test_clip_rapport_cyclique(tmp_path):
    t = np.arange(SR) / SR
    entree = ecrire(tmp_path / "d.wav", 0.5 * np.sin(2 * np.pi * 440 * t))
    lancer(tmp_path, entree, "--mode", "clip", "--duty", "0.2")
    audio, _ = lire_entiers(tmp_path / "sortie" / "d_clip.wav")
    extrait = audio[SR // 4:3 * SR // 4].astype(float)
    assert np.mean(extrait > 0) == pytest.approx(0.2, abs=0.02)
    assert cents(frequence_mesuree(extrait), 440) < 5  # la hauteur ne bouge pas
    assert abs(extrait.mean()) < 0.02 * NIVEAU  # pas de composante continue
    assert np.unique(extrait).size == 2


# --- Mode melodie -----------------------------------------------------------


@pytest.mark.parametrize("options,tolerance", [([], 15), (["--quantize"], 1)])
def test_melodie_frequences(tmp_path, wav_melodie, options, tolerance):
    lancer(tmp_path, wav_melodie, "--mode", "melodie", *options)
    audio, sr = lire_entiers(tmp_path / "sortie" / "melodie_melodie.wav")
    assert sr == SR
    verifier_notes(audio, MELODIE, tolerance)


def test_melodie_carre_pur(tmp_path, wav_melodie):
    lancer(tmp_path, wav_melodie, "--mode", "melodie", "--quantize")
    audio, _ = lire_entiers(tmp_path / "sortie" / "melodie_melodie.wav")
    assert set(np.unique(audio[FONDU:-FONDU])) <= {-NIVEAU, 0, NIVEAU}


def test_melodie_phase_continue(tmp_path, wav_melodie):
    """Aucun cycle anormal : chaque période correspond à une note de la mélodie.

    Un saut de phase au changement de note créerait un cycle plus court que le
    plus court des cycles légitimes.
    """
    lancer(tmp_path, wav_melodie, "--mode", "melodie", "--quantize")
    audio, _ = lire_entiers(tmp_path / "sortie" / "melodie_melodie.wav")
    negatif = audio <= 0
    fronts = np.flatnonzero(negatif[:-1] & ~negatif[1:])
    plus_courte = SR / max(hz(n) for n, _ in MELODIE)
    assert np.min(np.diff(fronts)) >= plus_courte - 1.5


def test_melodie_octave(tmp_path, wav_melodie):
    lancer(tmp_path, wav_melodie, "--mode", "melodie", "--quantize", "--octave", "-1")
    audio, _ = lire_entiers(tmp_path / "sortie" / "melodie_melodie.wav")
    verifier_notes(audio, [(n - 12, d) for n, d in MELODIE], 1)


def test_tenir_notes_comble_les_trous():
    """Un trou prend la note précédente ; le début d'un passage prend la première note."""
    nan = np.nan
    notes = np.array([nan, 60.0, nan, nan, 62.0, nan])
    sortie = ts.tenir_notes(notes, np.zeros(6, dtype=bool), 3)
    assert sortie.tolist() == [60.0, 60.0, 60.0, 60.0, 62.0, 62.0]


def test_tenir_notes_respecte_un_vrai_silence():
    """Un silence assez long coupe le son, un silence bref non."""
    nan = np.nan
    notes = np.array([60.0, nan, nan, nan, nan, 62.0, nan, 64.0])
    silence = np.array([0, 1, 1, 1, 0, 0, 1, 0], dtype=bool)
    sortie = ts.tenir_notes(notes, silence, 3)
    attendu = np.array([60.0, nan, nan, nan, 62.0, 62.0, 62.0, 64.0])
    assert np.array_equal(sortie, attendu, equal_nan=True)


def test_tenir_notes_sans_aucune_note():
    notes = np.full(5, np.nan)
    assert np.isnan(ts.tenir_notes(notes, np.zeros(5, dtype=bool), 3)).all()


def morceau_avec_bruit():
    """Deux notes séparées par du bruit, assez fort pour ne pas être un silence, mais sans hauteur."""
    bruit = 0.3 * np.random.default_rng(1).standard_normal(SR // 2)
    return np.concatenate([0.5 * ton_riche(440, 0.5), bruit, 0.5 * ton_riche(660, 0.5)]).astype(np.float32)


def test_melodie_continue_sans_trou(tmp_path):
    """Par défaut, le carré ne s'arrête jamais entre deux notes, même sans hauteur détectée."""
    lancer(tmp_path, ecrire(tmp_path / "b.wav", morceau_avec_bruit()), "--mode", "melodie", "--quantize")
    audio, _ = lire_entiers(tmp_path / "sortie" / "b_melodie.wav")
    # Un cycle à 440 Hz ne dure pas 100 échantillons : aucune plage nulle plus longue.
    nuls = audio == 0
    etiquettes, nb = ndimage.label(nuls[SR // 10:-SR // 10])
    assert nb == 0 or np.bincount(etiquettes)[1:].max() < 100


def test_melodie_no_continu_garde_les_trous(tmp_path):
    lancer(tmp_path, ecrire(tmp_path / "b.wav", morceau_avec_bruit()), "--mode", "melodie", "--quantize", "--no-continu")
    audio, _ = lire_entiers(tmp_path / "sortie" / "b_melodie.wav")
    assert np.all(audio[int(0.7 * SR):int(0.8 * SR)] == 0)


def test_riff_suit_les_notes_de_sa_plage_et_ignore_la_voix():
    """Une « voix » forte au-dessus de la plage n'est pas suivie ; les notes du riff, si."""
    hauteurs = [110.0, 146.83, 196.0, 130.81]  # A2 D3 G3 C3
    riff = np.concatenate([np.sin(2 * np.pi * f * np.arange(SR // 2) / SR) for f in hauteurs])
    voix = 0.8 * np.sin(2 * np.pi * 660.0 * np.arange(riff.size) / SR)
    notes = ts.extraire_riff((0.4 * riff + voix).astype(np.float32), 60, -40, 98.0, 262.0)
    par_note = notes.size // 4
    trouvees = [np.median(notes[i * par_note + 8:(i + 1) * par_note - 8]) for i in range(4)]
    assert trouvees == [45, 50, 55, 48]
    assert not np.isnan(notes[:-8]).any()


def test_riff_garde_les_vrais_silences(tmp_path):
    sequence = np.concatenate([0.5 * ton_riche(220, 0.5), np.zeros(SR), 0.5 * ton_riche(220, 0.5)])
    lancer(tmp_path, ecrire(tmp_path / "r.wav", sequence), "--mode", "melodie", "--riff")
    audio, _ = lire_entiers(tmp_path / "sortie" / "r_melodie.wav")
    assert np.all(audio[int(0.8 * SR):int(1.2 * SR)] == 0)
    assert np.any(audio[:SR // 2] != 0) and np.any(audio[int(1.6 * SR):] != 0)


def pm_de_notes(*notes):
    pm = pretty_midi.PrettyMIDI()
    instrument = pretty_midi.Instrument(0)
    instrument.notes = [pretty_midi.Note(100, h, d, f) for h, d, f in notes]
    pm.instruments.append(instrument)
    return pm


def test_garder_instru_filtre_la_plage_et_comble_les_silences():
    pm = pm_de_notes((48, 0.0, 1.0), (75, 0.5, 3.0), (30, 0.5, 3.0), (50, 2.0, 2.5), (52, 2.2, 3.0))
    ts.garder_instru(pm, 65.0, 262.0)
    notes = [(n.pitch, n.start, n.end) for n in pm.instruments[0].notes]
    # 75 (voix) et 30 sortent ; le silence de 1 à 2 s prolonge la note 48.
    assert notes == [(48, 0.0, 2.0), (50, 2.0, 2.5), (52, 2.2, 3.0)]


def test_garder_instru_laisse_les_notes_qui_se_chevauchent():
    """Une note courte recouverte ne doit pas être prise pour la dernière."""
    pm = pm_de_notes((48, 0.0, 2.0), (50, 0.5, 1.0), (52, 3.0, 4.0))
    ts.garder_instru(pm, 65.0, 262.0)
    assert [(n.pitch, n.end) for n in pm.instruments[0].notes] == [(48, 3.0), (50, 1.0), (52, 4.0)]


def test_instru_de_bout_en_bout(tmp_path, wav_melodie, monkeypatch):
    """L'audio devient plusieurs voix : deux notes simultanées, sans la note hors plage."""
    pm = pm_de_notes((48, 0.0, 1.0), (55, 0.0, 1.0), (90, 0.0, 1.0))
    monkeypatch.setattr(ts, "audio_vers_midi", lambda chemin, preview: pm)
    lancer(tmp_path, wav_melodie, "--mode", "instru", "--octave", "1")
    audio, _ = lire_entiers(tmp_path / "sortie" / f"{wav_melodie.stem}_instru.wav")
    assert np.unique(audio[SR // 10:SR // 2]).size > 2  # plus de deux niveaux : c'est un mélange
    spectre = np.abs(np.fft.rfft(audio[SR // 10:SR // 2 + SR // 10]))
    frequences = np.fft.rfftfreq(SR // 2, 1 / SR)
    pic = frequences[np.argmax(spectre)]
    assert min(abs(pic - 261.63), abs(pic - 392.0)) < 5  # C4 et G4, après l'octave +1


def test_melodie_silence_entre_notes(tmp_path):
    """Là où aucune note n'est voisée, la sortie doit être strictement nulle."""
    sequence = np.concatenate([0.5 * ton_riche(440, 0.5), np.zeros(SR), 0.5 * ton_riche(660, 0.5)])
    lancer(tmp_path, ecrire(tmp_path / "s.wav", sequence), "--mode", "melodie")
    audio, _ = lire_entiers(tmp_path / "sortie" / "s_melodie.wav")
    assert np.all(audio[int(0.7 * SR):int(1.3 * SR)] == 0)
    assert np.any(audio[:SR // 2] != 0) and np.any(audio[int(1.5 * SR):] != 0)


def test_melodie_note_trop_courte_supprimee():
    """Une note isolée sous --min-note disparaît ; une note longue reste intacte."""
    nan = np.nan
    notes = np.array([nan] * 5 + [69.0] * 3 + [nan] * 5 + [72.0] * 20 + [nan] * 2)
    sortie = ts.nettoyer_notes(notes, min_trames=5, quantize=False)
    assert np.all(np.isnan(sortie[:13]))
    assert np.all(sortie[13:33] == 72.0)


def test_melodie_note_courte_absorbee_et_vibrato():
    """Une note courte collée à une autre est absorbée ; un vibrato ne découpe pas."""
    notes = np.array([69.0] * 10 + [76.0] * 2 + [69.0] * 10)
    assert np.all(ts.nettoyer_notes(notes, 5, False) == 69.0)
    vibrato = 69.0 + 0.4 * np.sin(np.linspace(0, 12, 200))
    assert np.all(ts.nettoyer_notes(vibrato, 5, True) == 69.0)


def test_melodie_sur_accord_ne_plante_pas(tmp_path, wav_accord):
    """Sur un accord, on attend un carré propre, quelle que soit la note choisie."""
    code = ts.main([str(wav_accord), "--mode", "melodie", "--sortie", str(tmp_path / "sortie")])
    sortie = tmp_path / "sortie" / "accord_melodie.wav"
    if code == 0:  # pYIN peut aussi légitimement refuser cet accord
        audio, _ = lire_entiers(sortie)
        assert set(np.unique(audio[FONDU:-FONDU])) <= {-NIVEAU, 0, NIVEAU}
    else:
        assert not sortie.exists()


def mix_complet(transposition, accords, basse):
    """Mélodie + accords + basse + batterie : renvoie le signal et les notes attendues."""
    rng = np.random.default_rng(1)
    temps = 0.3
    motif = [76, 74, 72, 74, 76, 76, 76, 74, 74, 74, 76, 79, 79, 76, 74, 72]
    notes = [n + transposition for n in motif] * 2
    grille = [[60, 64, 67], [67, 71, 74], [69, 72, 76], [65, 69, 72]]
    audio = np.zeros(int((len(notes) + 4) * temps * SR))

    def ajouter(debut, son, gain=1.0):
        i = int(debut * SR)
        audio[i:i + son.size] += gain * son[: audio.size - i]

    for i, note in enumerate(notes):
        ajouter(i * temps, ton_riche(hz(note), temps * 0.95))
        duree = int(0.1 * SR)
        decroissance = np.exp(-np.arange(duree) / SR * 35)
        ajouter(i * temps, rng.standard_normal(duree) * decroissance, 0.5)  # batterie
    for i in range(len(notes) // 4):
        accord = grille[i % 4]
        ajouter(i * temps * 4, ton_riche(hz(accord[0] - 24), temps * 4), basse)
        for note in accord:
            ajouter(i * temps * 4, ton_riche(hz(note), temps * 4), accords)
    return (audio / np.max(np.abs(audio)) * 0.9).astype(np.float32), notes, temps


def justesse(detecte, notes, temps):
    """Part des trames, au cœur de chaque note, où la bonne note a été trouvée."""
    trame = ts.HOP_ANALYSE / ts.SR_ANALYSE
    bons = total = 0
    for i, note in enumerate(notes):
        extrait = detecte[int((i * temps + 0.05) / trame):int((i * temps + 0.8 * temps) / trame)]
        total += extrait.size
        bons += np.sum(np.abs(extrait - note) < 0.5)  # NaN compte comme faux
    return bons / total


def test_melodie_retrouvee_dans_un_mix():
    """Le cas réel : une mélodie médium au-dessus d'un accompagnement discret."""
    audio, notes, temps = mix_complet(-12, accords=0.2, basse=0.25)
    assert justesse(ts.extraire_melodie(audio, 60, True, -40), notes, temps) > 0.9


def test_melodie_passe_haut_aide_sur_mix_charge():
    """Sans le passe-haut, pYIN se cale sur l'accompagnement ; --hp-analyse corrige."""
    audio, notes, temps = mix_complet(0, accords=0.3, basse=0.35)
    sans = justesse(ts.extraire_melodie(audio, 60, True, -40, hp_analyse=0), notes, temps)
    avec = justesse(ts.extraire_melodie(audio, 60, True, -40, hp_analyse=500), notes, temps)
    assert avec > 0.9 and avec > sans + 0.3


def test_melodie_grave_sous_la_coupure(tmp_path):
    """Une note de 110 Hz, sous le passe-haut de 200 Hz, reste bien détectée."""
    entree = ecrire(tmp_path / "grave.wav", 0.5 * ton_riche(110, 1.0))
    lancer(tmp_path, entree, "--mode", "melodie", "--quantize")
    audio, _ = lire_entiers(tmp_path / "sortie" / "grave_melodie.wav")
    assert cents(frequence_mesuree(audio[SR // 4:3 * SR // 4]), 110) < 1


def test_melodie_aucune_note(tmp_path, capsys):
    entree = ecrire(tmp_path / "vide.wav", np.zeros(SR))
    assert ts.main([str(entree), "--mode", "melodie", "--sortie", str(tmp_path)]) == 1
    assert "Aucune mélodie détectée" in capsys.readouterr().err


# --- Mode midi --------------------------------------------------------------


def test_midi_mono_garde_la_plus_aigue(tmp_path, mid_accord_melodie):
    lancer(tmp_path, mid_accord_melodie, "--mode", "midi")
    audio, sr = lire_entiers(tmp_path / "sortie" / "morceau_midi.wav")
    assert sr == SR
    assert set(np.unique(audio[FONDU:-FONDU])) <= {-NIVEAU, 0, NIVEAU}
    verifier_notes(audio, MELODIE[:5], 3)
    # La batterie dure 3 s de plus mais ne doit pas allonger le fichier.
    assert audio.size == pytest.approx(2.0 * SR, abs=10)


def test_midi_poly_melange_les_voix(tmp_path, mid_accord_melodie):
    lancer(tmp_path, mid_accord_melodie, "--mode", "midi", "--poly")
    audio, _ = lire_entiers(tmp_path / "sortie" / "morceau_midi.wav")
    assert np.unique(audio).size > 3  # mélange : plus de deux niveaux
    assert np.max(np.abs(audio)) == NIVEAU  # mais toujours au volume demandé
    # Les quatre voix du premier temps sont bien présentes dans le spectre.
    extrait = audio[int(0.05 * SR):int(0.35 * SR)].astype(float)
    spectre = np.abs(np.fft.rfft(extrait * np.hanning(extrait.size)))
    freqs = np.fft.rfftfreq(extrait.size, 1 / SR)
    for note in [69, 57 - 12, 61 - 12, 64 - 12]:
        zone = np.abs(freqs - hz(note)) < 4
        assert spectre[zone].max() > 0.1 * spectre.max(), f"voix {note} absente"


def test_midi_sur_audio_sans_basic_pitch(tmp_path, wav_accord, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "basic_pitch.inference", None)
    assert ts.main([str(wav_accord), "--mode", "midi", "--sortie", str(tmp_path)]) == 1
    assert "basic-pitch n'est pas installé" in capsys.readouterr().err


@pytest.mark.parametrize("preview", [None, 1.0])
def test_midi_sur_audio_avec_basic_pitch_simule(tmp_path, wav_melodie, mid_accord_melodie, monkeypatch, preview):
    """Le branchement avec basic-pitch, testé avec un faux module compatible."""
    recus = []

    def predict(chemin):
        recus.append(sf.info(chemin).duration)
        return None, pretty_midi.PrettyMIDI(str(mid_accord_melodie)), None

    module = types.ModuleType("basic_pitch.inference")
    module.predict = predict
    monkeypatch.setitem(sys.modules, "basic_pitch", types.ModuleType("basic_pitch"))
    monkeypatch.setitem(sys.modules, "basic_pitch.inference", module)

    options = ["--preview", preview] if preview else []
    lancer(tmp_path, wav_melodie, "--mode", "midi", *options)
    audio, _ = lire_entiers(tmp_path / "sortie" / "melodie_midi.wav")
    attendu = preview or sf.info(str(wav_melodie)).duration
    assert recus[0] == pytest.approx(attendu, abs=0.01)
    assert audio.size == pytest.approx(min(attendu, 2.0) * SR, abs=10)


def test_midi_illisible(tmp_path, capsys):
    faux = tmp_path / "faux.mid"
    faux.write_bytes(b"pas du midi")
    assert ts.main([str(faux), "--sortie", str(tmp_path)]) == 1
    assert "MIDI illisible" in capsys.readouterr().err


def test_fichier_mid_detecte_sans_mode(tmp_path, mid_accord_melodie):
    lancer(tmp_path, mid_accord_melodie)
    assert (tmp_path / "sortie" / "morceau_midi.wav").exists()


def test_mid_refuse_en_mode_clip(tmp_path, mid_accord_melodie, capsys):
    assert ts.main([str(mid_accord_melodie), "--mode", "clip", "--sortie", str(tmp_path)]) == 1
    assert "utilise --mode midi" in capsys.readouterr().err


# --- Options communes -------------------------------------------------------


@pytest.mark.parametrize("mode", ["clip", "melodie"])
def test_fondus(tmp_path, mode):
    """Le carré monte progressivement en 20 ms, sans démarrer à pleine amplitude."""
    t = np.arange(SR) / SR
    entree = ecrire(tmp_path / "f.wav", 0.5 * np.sin(2 * np.pi * 440 * t))
    lancer(tmp_path, entree, "--mode", mode)
    audio, _ = lire_entiers(tmp_path / "sortie" / f"f_{mode}.wav")
    assert audio[0] == 0 and abs(int(audio[-1])) <= NIVEAU / FONDU + 1
    assert np.max(np.abs(audio[:FONDU // 4])) < NIVEAU / 3


@pytest.mark.parametrize("demande,attendu", [(0.5, 0.5), (0.95, 0.95), (2.0, 0.95)])
def test_volume_plafonne(tmp_path, wav_melodie, demande, attendu):
    lancer(tmp_path, wav_melodie, "--mode", "clip", "--volume", demande)
    audio, _ = lire_entiers(tmp_path / "sortie" / "melodie_clip.wav")
    assert np.max(np.abs(audio)) == int(round(attendu * 32767))


@pytest.mark.parametrize("mode", ["clip", "melodie"])
def test_preview(tmp_path, wav_melodie, mode):
    lancer(tmp_path, wav_melodie, "--mode", mode, "--preview", "1")
    audio, _ = lire_entiers(tmp_path / "sortie" / f"melodie_{mode}.wav")
    assert audio.size == SR


def test_preview_midi(tmp_path, mid_accord_melodie):
    lancer(tmp_path, mid_accord_melodie, "--preview", "1")
    audio, _ = lire_entiers(tmp_path / "sortie" / "morceau_midi.wav")
    assert audio.size == SR


@pytest.mark.parametrize("duty", [0.1, 0.3, 0.7])
def test_duty_melodie_et_moyenne_nulle(duty):
    audio = ts.synthese_carre(np.full(SR, 441.0), duty)
    actif = audio[: 44100]
    assert np.mean(actif > 0) == pytest.approx(duty, abs=0.01)
    assert abs(actif.mean()) < 0.01
    assert np.max(np.abs(actif)) == 1.0


def test_synthese_finit_sur_un_cycle_complet():
    """Un passage ne s'arrête jamais au milieu d'un cycle, et ne dure pas moins d'un cycle."""
    audio = ts.synthese_carre(np.full(1000, 441.0), 0.5)
    assert audio[0] > 0
    fin = np.flatnonzero(audio)[-1]
    assert fin == 999 and audio[fin] < 0  # 1000 échantillons = 10 cycles exacts
    fin = np.flatnonzero(ts.synthese_carre(np.full(1050, 441.0), 0.5))[-1]
    assert fin == 999  # le demi-cycle restant n'est pas joué
    assert not np.any(ts.synthese_carre(np.full(50, 441.0), 0.5))


@pytest.mark.parametrize("extension", ["mp3", "flac", "m4a", "ogg", "wav"])
def test_formats_entree(tmp_path, wav_melodie, extension):
    """Chaque format annoncé est décodé, avec la bonne mélodie à l'arrivée."""
    entree = tmp_path / f"chanson.{extension}"
    if extension == "wav":
        shutil.copy(wav_melodie, entree)
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav_melodie), str(entree)], check=True)
    lancer(tmp_path, entree, "--mode", "melodie", "--quantize")
    audio, sr = lire_entiers(tmp_path / "sortie" / "chanson_melodie.wav")
    assert sr == SR and audio.ndim == 1
    # Les encodeurs compressés décalent un peu le début : on ne vérifie que
    # l'intérieur des notes, à une petite marge près.
    verifier_notes(audio, MELODIE, 1)


def test_stereo_devient_mono(tmp_path):
    t = np.arange(SR) / SR
    stereo = np.column_stack([0.5 * np.sin(2 * np.pi * 440 * t)] * 2)
    entree = tmp_path / "st.wav"
    sf.write(str(entree), stereo, SR)
    assert ts.decoder_audio(entree).ndim == 1


def test_reechantillonne_vers_44100(tmp_path):
    t = np.arange(48000) / 48000
    entree = tmp_path / "r48.wav"
    sf.write(str(entree), 0.5 * np.sin(2 * np.pi * 440 * t), 48000)
    lancer(tmp_path, entree, "--mode", "clip")
    audio, sr = lire_entiers(tmp_path / "sortie" / "r48_clip.wav")
    assert sr == SR and audio.size == pytest.approx(SR, abs=2)
    assert cents(frequence_mesuree(audio[SR // 4:3 * SR // 4]), 440) < 5


# --- Lot, lecture et erreurs ------------------------------------------------


def test_lot_dossier(tmp_path):
    dossier = tmp_path / "musique"
    dossier.mkdir()
    ecrire(dossier / "a.wav", signal_melodie())
    ecrire(dossier / "b.wav", signal_accord())
    (dossier / "notes.txt").write_text("à ignorer")
    (dossier / "._a.wav").write_bytes(b"fichier fantome macOS")
    lancer(tmp_path, dossier, "--mode", "clip")
    assert sorted(f.name for f in (tmp_path / "sortie").iterdir()) == ["a_clip.wav", "b_clip.wav"]


def test_lot_continue_apres_une_erreur(tmp_path, capsys):
    dossier = tmp_path / "musique"
    dossier.mkdir()
    (dossier / "a_casse.wav").write_bytes(b"ceci n'est pas un wav")
    ecrire(dossier / "b.wav", signal_melodie())
    assert ts.main([str(dossier), "--mode", "clip", "--sortie", str(tmp_path / "sortie")]) == 1
    assert (tmp_path / "sortie" / "b_clip.wav").exists()
    assert "a_casse.wav" in capsys.readouterr().err


def test_lot_conflit_de_noms(tmp_path, capsys):
    """Deux fichiers de même nom ne doivent pas s'écraser en silence."""
    dossier = tmp_path / "musique"
    dossier.mkdir()
    ecrire(dossier / "m.flac", signal_melodie())
    ecrire(dossier / "m.wav", signal_accord())
    assert ts.main([str(dossier), "--mode", "clip", "--sortie", str(tmp_path / "sortie")]) == 1
    assert "m_clip.wav" in capsys.readouterr().err
    # C'est bien le premier fichier (m.flac, la mélodie, plus longue) qui reste.
    assert sf.info(str(tmp_path / "sortie" / "m_clip.wav")).duration == pytest.approx(2.8, abs=0.01)


def test_play_appelle_afplay(tmp_path, wav_melodie, monkeypatch):
    """--play est testé sans son : aucun signal ne doit partir vers une vraie bobine."""
    appels = []
    monkeypatch.setattr(ts.subprocess, "run", lambda cmd, **k: appels.append(cmd))
    monkeypatch.setattr(ts, "decoder_audio", lambda *a: signal_melodie().astype(np.float32))
    lancer(tmp_path, wav_melodie, "--mode", "clip", "--play")
    assert appels == [[ts.chemin_afplay(), str(tmp_path / "sortie" / "melodie_clip.wav")]]


def test_fichier_introuvable(tmp_path, capsys):
    assert ts.main([str(tmp_path / "absent.mp3")]) == 1
    assert "introuvable" in capsys.readouterr().err


def test_format_non_pris_en_charge(tmp_path, capsys):
    (tmp_path / "x.txt").write_text("texte")
    assert ts.main([str(tmp_path / "x.txt")]) == 1
    assert "non pris en charge" in capsys.readouterr().err


@pytest.fixture
def sans_ffmpeg(monkeypatch):
    """Simule un Mac sans ffmpeg : seul afconvert, fourni par macOS, reste disponible."""
    vrai_which = shutil.which
    monkeypatch.setattr(ts.shutil, "which", lambda nom: None if nom == "ffmpeg" else vrai_which(nom))
    monkeypatch.setattr(ts, "CHEMINS_FFMPEG", ())


def test_ffmpeg_trouve_hors_du_path(monkeypatch):
    """Depuis le Finder, le PATH ne contient pas Homebrew : on doit quand même le trouver."""
    monkeypatch.setattr(ts.shutil, "which", lambda nom: None)
    monkeypatch.setattr(ts, "CHEMINS_FFMPEG", ("/bin/ls",))
    assert ts.trouver_ffmpeg() == "/bin/ls"


@pytest.mark.parametrize("extension,options", [
    ("mp3", []), ("flac", []), ("m4a", []), ("wav", []),
    ("ogg", ["-c:a", "libvorbis"]), ("ogg", ["-c:a", "vorbis", "-strict", "-2", "-ac", "2"]),
    ("ogg", ["-c:a", "libopus"]),
])
def test_decodage_sans_ffmpeg(tmp_path, wav_melodie, sans_ffmpeg, extension, options):
    """Sans ffmpeg, chaque format doit donner la même mélodie grâce à afconvert."""
    entree = tmp_path / f"chanson.{extension}"
    if extension == "wav":
        shutil.copy(wav_melodie, entree)
    else:
        encodage = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(wav_melodie), *options, str(entree)], capture_output=True)
        if encodage.returncode != 0:
            pytest.skip(f"encodeur absent de ce ffmpeg : {options}")
    lancer(tmp_path, entree, "--mode", "melodie", "--quantize")
    audio, sr = lire_entiers(tmp_path / "sortie" / "chanson_melodie.wav")
    assert sr == SR and audio.ndim == 1
    verifier_notes(audio, MELODIE, 1)


def test_afconvert_preview_stereo_et_48k(tmp_path, sans_ffmpeg):
    t = np.arange(2 * 48000) / 48000
    onde = 0.5 * np.sin(2 * np.pi * 440 * t)
    entree = tmp_path / "st48.wav"
    sf.write(str(entree), np.column_stack([onde, onde]), 48000)
    audio = ts.decoder_audio(entree, preview=1.0)
    assert audio.ndim == 1 and audio.size == SR
    assert cents(frequence_mesuree(audio[SR // 4:3 * SR // 4]), 440) < 5


def test_afconvert_fichier_invalide(tmp_path, sans_ffmpeg):
    faux = tmp_path / "faux.mp3"
    faux.write_bytes(b"pas de l'audio")
    with pytest.raises(ts.ErreurTesla, match="Impossible de décoder"):
        ts.decoder_audio(faux)


def test_aucun_decodeur(tmp_path, wav_melodie, capsys, monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda nom: None)
    monkeypatch.setattr(ts, "CHEMINS_FFMPEG", ())
    monkeypatch.setattr(ts, "AFCONVERT", "/nulle/part/afconvert")
    assert ts.main([str(wav_melodie), "--mode", "clip", "--sortie", str(tmp_path / "s")]) == 1
    assert "brew install ffmpeg" in capsys.readouterr().err


@pytest.mark.parametrize("options", [
    ["--duty", "0"], ["--duty", "1.5"], ["--volume", "0"], ["--preview", "-1"],
    ["--low", "3000", "--high", "300"], ["--high", "30000"], ["--gate", "10"], ["--octave", "9"], ["--hp-analyse", "-5"], ["--riff-bas", "300", "--riff-haut", "200"], ["--instru-bas", "300", "--instru-haut", "200"],
])
def test_options_invalides(wav_melodie, capsys, options):
    assert ts.main([str(wav_melodie)] + options) == 1
    assert "Erreur" in capsys.readouterr().err
