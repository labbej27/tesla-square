# -*- coding: utf-8 -*-
"""Tests de l'interface, sans écran : la fenêtre est réellement pilotée et convertit de vrais fichiers."""

import os
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import numpy as np
import pretty_midi
import pytest
import soundfile as sf

# PySide6 n'est que dans le venv de compilation, hors d'iCloud (voir compiler_app.sh).
pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QProcess, QSettings, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QMessageBox

import tesla_square as ts
import tesla_square_gui as gui

SR = 44100


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def messages(monkeypatch):
    """Capture les boîtes de dialogue, qui bloqueraient les tests."""
    recus = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _f, _t, texte: recus.append(texte))
    return recus


@pytest.fixture(autouse=True)
def memoire_isolee(tmp_path, monkeypatch):
    """Les réglages vont dans un fichier temporaire, jamais dans les préférences de l'utilisateur."""
    fichier = str(tmp_path / "reglages.ini")
    monkeypatch.setattr(gui, "ouvrir_memoire", lambda: QSettings(fichier, QSettings.Format.IniFormat))


@pytest.fixture
def fenetre(app, tmp_path, messages):
    f = gui.Fenetre()
    f.champ_sortie.setText(str(tmp_path / "résultats"))
    yield f
    f.close()


def attendre(f, delai=60):
    limite = time.time() + delai
    while f.fil is not None:
        QApplication.processEvents()
        assert time.time() < limite, "conversion trop longue"
        time.sleep(0.005)
    QApplication.processEvents()


def wav(chemin, frequence=440.0, duree=1.0):
    t = np.arange(int(duree * SR)) / SR
    sf.write(str(chemin), 0.5 * np.sin(2 * np.pi * frequence * t), SR, subtype="PCM_16")
    return chemin


def choisir(f, mode):
    f.boutons_modes[mode].setChecked(True)


def test_etat_initial(fenetre):
    assert fenetre.mode() == "melodie"
    assert not fenetre.bouton_convertir.isEnabled()
    assert not fenetre.bouton_ecouter.isEnabled()
    assert fenetre.pages.currentIndex() == 0


def test_page_suit_le_mode(fenetre):
    for rang, mode in enumerate(gui.NOMS_MODES):
        choisir(fenetre, mode)
        assert fenetre.pages.currentIndex() == rang
        assert fenetre.description.text() == gui.DESCRIPTIONS[mode]


def test_ajout_sans_doublon_et_filtrage(fenetre, tmp_path):
    a = wav(tmp_path / "a.wav")
    texte = tmp_path / "x.txt"
    texte.write_text("non")
    fenetre.ajouter([a, a, texte])
    assert fenetre.fichiers == [a]
    assert "ignoré" in fenetre.statut.text()
    assert fenetre.bouton_convertir.isEnabled()


def test_ajout_dossier(fenetre, tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    wav(d / "1.wav")
    wav(d / "2.wav")
    fenetre.ajouter([d])
    assert [p.name for p in fenetre.fichiers] == ["1.wav", "2.wav"]


def test_glisser_deposer(fenetre, tmp_path):
    a = wav(tmp_path / "a.wav")
    donnees = QMimeData()
    donnees.setUrls([QUrl.fromLocalFile(str(a))])
    evenement = QDropEvent(QPoint(5, 5), Qt.DropAction.CopyAction, donnees,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    fenetre.liste.dropEvent(evenement)
    assert fenetre.fichiers == [a]


def test_retirer(fenetre, tmp_path):
    fenetre.ajouter([wav(tmp_path / "a.wav"), wav(tmp_path / "b.wav")])
    fenetre.liste.item(0).setSelected(True)
    fenetre._retirer()
    assert [p.name for p in fenetre.fichiers] == ["b.wav"]
    assert fenetre.liste.count() == 1


@pytest.mark.parametrize("mode", ["melodie", "clip"])
def test_conversion_complete(fenetre, tmp_path, messages, mode):
    fenetre.ajouter([wav(tmp_path / "la.wav")])
    choisir(fenetre, mode)
    fenetre.convertir()
    assert not fenetre.bouton_convertir.isEnabled()  # pas de double lancement
    attendre(fenetre)
    sortie = tmp_path / "résultats" / f"la_{mode}.wav"
    audio, sr = sf.read(str(sortie), dtype="int16")
    assert sr == SR and audio.ndim == 1
    assert messages == []
    assert fenetre.statut.text() == "Terminé."
    assert fenetre.resultats.count() == 1 and fenetre.bouton_ecouter.isEnabled()
    assert fenetre.bouton_convertir.isEnabled()


def test_fil_avec_grande_pile(fenetre, tmp_path):
    """La compilation numba déborde de la pile par défaut d'un fil secondaire (plantage réel)."""
    fenetre.ajouter([wav(tmp_path / "la.wav")])
    choisir(fenetre, "clip")
    fenetre.convertir()
    assert fenetre.fil.stackSize() >= 32 * 1024 * 1024
    attendre(fenetre)


def test_reglages_transmis_au_moteur(fenetre, tmp_path):
    fenetre.ajouter([wav(tmp_path / "la.wav", duree=3)])
    choisir(fenetre, "clip")
    fenetre.volume.setValue(0.5)
    fenetre.duty.setValue(0.2)
    fenetre.case_preview.setChecked(True)
    fenetre.champ_preview.setValue(1)
    fenetre.convertir()
    attendre(fenetre)
    audio, _ = sf.read(str(tmp_path / "résultats" / "la_clip.wav"), dtype="int16")
    assert audio.size == SR  # essai rapide
    assert np.max(np.abs(audio)) == round(0.5 * 32767)  # volume
    assert np.mean(audio[SR // 4:3 * SR // 4] > 0) == pytest.approx(0.2, abs=0.02)  # rapport cyclique


def test_gate_du_bon_mode(fenetre):
    fenetre.gate_clip.setValue(-25)
    fenetre.gate_melodie.setValue(-55)
    choisir(fenetre, "clip")
    assert fenetre.reglages()["gate"] == -25
    choisir(fenetre, "melodie")
    assert fenetre.reglages()["gate"] == -55


def test_midi_et_audio_melanges(fenetre, tmp_path, messages):
    """Un .mid convient en mode Mélodie ; un fichier audio en mode MIDI donne un message clair."""
    pm = pretty_midi.PrettyMIDI()
    instrument = pretty_midi.Instrument(0)
    instrument.notes.append(pretty_midi.Note(100, 69, 0.0, 1.0))
    pm.instruments.append(instrument)
    mid = tmp_path / "m.mid"
    pm.write(str(mid))
    fenetre.ajouter([mid])
    fenetre.convertir()
    attendre(fenetre)
    assert (tmp_path / "résultats" / "m_midi.wav").exists()

    fenetre.ajouter([wav(tmp_path / "a.wav")])
    choisir(fenetre, "midi")
    fenetre.convertir()
    attendre(fenetre)
    assert any("n'accepte que les fichiers .mid" in m and "a.wav" in m for m in messages)
    assert "README" not in " ".join(messages)


def test_erreur_sur_un_fichier_laisse_passer_les_autres(fenetre, tmp_path, messages):
    casse = tmp_path / "a_casse.wav"
    casse.write_bytes(b"pas du son")
    fenetre.ajouter([casse, wav(tmp_path / "b.wav")])
    choisir(fenetre, "clip")
    fenetre.convertir()
    attendre(fenetre)
    assert (tmp_path / "résultats" / "b_clip.wav").exists()
    assert "1 problème" in fenetre.statut.text()
    assert "a_casse.wav" in messages[0]


def test_aucune_melodie_signalee(fenetre, tmp_path, messages):
    sf.write(str(tmp_path / "vide.wav"), np.zeros(SR), SR)
    fenetre.ajouter([tmp_path / "vide.wav"])
    fenetre.convertir()
    attendre(fenetre)
    assert "Aucune mélodie détectée" in messages[0]
    assert "--" not in messages[0]
    assert fenetre.resultats.count() == 0


def test_conflit_de_noms(fenetre, tmp_path, messages):
    (tmp_path / "x").mkdir()
    fenetre.ajouter([wav(tmp_path / "m.wav", 440), wav(tmp_path / "x" / "m.wav", 880)])
    choisir(fenetre, "clip")
    fenetre.convertir()
    attendre(fenetre)
    assert "m_clip.wav" in messages[0]
    assert fenetre.resultats.count() == 1


def test_reglage_invalide(fenetre, tmp_path, messages):
    fenetre.ajouter([wav(tmp_path / "a.wav")])
    choisir(fenetre, "clip")
    fenetre.low.setValue(5000)
    fenetre.high.setValue(1000)
    fenetre.convertir()
    assert fenetre.fil is None
    assert "fréquence basse" in messages[0] and "--" not in messages[0]


def test_sortie_impossible(fenetre, tmp_path, messages):
    bloc = tmp_path / "fichier"
    bloc.write_text("x")
    fenetre.ajouter([wav(tmp_path / "a.wav")])
    fenetre.champ_sortie.setText(str(bloc / "dedans"))
    fenetre.convertir()
    assert fenetre.fil is None and "Impossible de créer" in messages[0]


def test_annulation(fenetre, tmp_path):
    fenetre.ajouter([wav(tmp_path / f"{i}.wav") for i in range(5)])
    choisir(fenetre, "clip")
    fenetre.convertir()
    fenetre.convertisseur.annule = True
    attendre(fenetre)
    assert fenetre.statut.text() == "Conversion annulée."
    assert fenetre.resultats.count() < 5


def test_fermeture_pendant_conversion(app, tmp_path, messages):
    """Fermer la fenêtre en pleine conversion ne doit pas faire planter l'application."""
    f = gui.Fenetre()
    f.champ_sortie.setText(str(tmp_path / "s"))
    f.ajouter([wav(tmp_path / f"{i}.wav") for i in range(3)])
    choisir(f, "clip")
    f.convertir()
    f.close()
    assert not f.isVisible()
    assert f.fil is None or f.fil.isFinished()


def test_ecoute(fenetre, tmp_path, monkeypatch):
    """Le bouton lance puis arrête afplay. La lecture est simulée : aucun son ne doit sortir."""
    lectures = []
    monkeypatch.setattr(fenetre.lecteur, "start", lambda prog, args: lectures.append((prog, args)))
    monkeypatch.setattr(fenetre.lecteur, "state", lambda: QProcess.ProcessState.NotRunning)
    fenetre.ajouter([wav(tmp_path / "a.wav")])
    choisir(fenetre, "clip")
    fenetre.convertir()
    attendre(fenetre)
    fenetre._ecouter()
    assert lectures == [(ts.chemin_afplay(), [str(tmp_path / "résultats" / "a_clip.wav")])]
    assert fenetre.bouton_ecouter.text() == "Arrêter"
    fenetre._lecture_terminee()
    assert fenetre.bouton_ecouter.text() == "Écouter"


def test_reglages_memorises(app, tmp_path, messages):
    f = gui.Fenetre()
    choisir(f, "clip")
    f.volume.setValue(0.33)
    f.champ_octave.setValue(-2)
    f.low.setValue(450)
    f.close()
    g = gui.Fenetre()
    assert g.mode() == "clip" and g.volume.value() == 0.33 and g.low.value() == 450
    assert g.champ_octave.value() == -2
    g._reglages_defaut()
    assert g.volume.value() == 0.8 and g.low.value() == 300
    g.close()


def test_son_continu_et_riff(fenetre):
    """Le son continu est actif d'emblée ; le riff remplace le filtre des graves dans la fenêtre."""
    assert fenetre.reglages()["continu"] is True
    assert fenetre.reglages()["riff"] is False
    assert not fenetre.riff_bas.isEnabled() and fenetre.hp_analyse.isEnabled()

    fenetre.riff.setChecked(True)
    fenetre.riff_bas.setValue(110)
    fenetre.riff_haut.setValue(300)
    reglages = fenetre.reglages()
    assert reglages["riff"] is True and (reglages["riff_bas"], reglages["riff_haut"]) == (110, 300)
    assert fenetre.riff_bas.isEnabled() and not fenetre.hp_analyse.isEnabled()

    fenetre._reglages_defaut()
    assert fenetre.reglages()["riff"] is False and fenetre.reglages()["riff_bas"] == 98
    assert fenetre.reglages()["continu"] is True


def test_mode_instru(fenetre):
    assert fenetre.reglages()["octave"] == 0
    fenetre.boutons_modes["instru"].click()
    reglages = fenetre.reglages()
    assert reglages["mode"] == "instru" and reglages["octave"] == 1
    assert (reglages["instru_bas"], reglages["instru_haut"]) == (65, 262)
    assert fenetre.pages.currentIndex() == list(gui.NOMS_MODES).index("instru")


def test_instru_sans_transcripteur_explique_pourquoi(tmp_path):
    with pytest.raises(ts.ErreurTesla, match="Instru"):
        gui.mode_du_fichier(tmp_path / "morceau.mp3", "instru")
