#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tesla Square — interface graphique de tesla_square.py.

Toute la conversion reste dans tesla_square.py : ce fichier ne fait que recueillir
les réglages, lancer le travail dans un fil séparé et afficher les résultats.
"""

from __future__ import annotations

import importlib.util
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    # Dans l'application compilée, numba ne peut pas écrire son cache à côté du
    # code. Il faut le lui indiquer avant que librosa ne soit importé.
    os.environ.setdefault("NUMBA_CACHE_DIR", str(Path.home() / "Library" / "Caches" / "TeslaSquare" / "numba"))

from PySide6.QtCore import QObject, QProcess, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

import tesla_square as ts

DESCRIPTIONS = {
    "melodie": "Retrouve la note principale et la rejoue en carré pur. "
               "Idéal quand une voix ou un instrument domine.",
    "clip": "Rapide. Écrête le morceau en carré : garde le rythme et la texture, mais c'est moins mélodique.",
    "midi": "Rejoue un fichier .mid en carré : le rendu le plus net. Les fichiers audio ne sont pas acceptés ici.",
    "instru": "Recommandé. Transcrit le morceau en notes et rejoue l'instrumentale à plusieurs voix carrées : "
              "le plus proche du morceau. Moins fort, et moins franc qu'un carré pur.",
}
NOMS_MODES = {"melodie": "Mélodie", "clip": "Clip", "midi": "MIDI", "instru": "Instru"}
TAILLE_PILE_FIL = 64 * 1024 * 1024


def ouvrir_memoire() -> QSettings:
    """Ouvre les réglages mémorisés entre deux lancements."""
    return QSettings("TeslaSquare", "TeslaSquare")


def dossier_sortie_defaut() -> str:
    """Dossier de sortie initial : ~/Music/Tesla Square."""
    musique = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MusicLocation)
    return str(Path(musique) / "Tesla Square")


def mode_du_fichier(chemin: Path, mode: str) -> str:
    """Un .mid part toujours en mode midi, quel que soit le mode choisi pour le reste du lot."""
    if chemin.suffix.lower() in ts.EXT_MIDI:
        return "midi"
    if mode == "instru" and ts.chemin_transcripteur() is None and importlib.util.find_spec("basic_pitch") is None:
        raise ts.ErreurTesla("Le mode Instru n'est disponible que dans l'application compilée.")
    if mode == "midi" and importlib.util.find_spec("basic_pitch") is None:
        raise ts.ErreurTesla("Le mode MIDI n'accepte que les fichiers .mid. Pour ce morceau, choisis Instru, Mélodie ou Clip.")
    return mode


class Convertisseur(QObject):
    """Convertit une liste de fichiers. Vit dans un QThread pour ne pas figer la fenêtre."""

    debut_fichier = Signal(int, int, str)
    fichier_ok = Signal(str, str)  # chemin produit, avertissement éventuel
    fichier_erreur = Signal(str, str)  # nom du fichier, message
    fini = Signal(bool)  # True si annulé

    def __init__(self, fichiers: list[Path], reglages: dict):
        super().__init__()
        self.fichiers = fichiers
        self.reglages = reglages
        self.annule = False  # lu entre deux fichiers : pYIN ne peut pas être interrompu

    def travailler(self) -> None:
        """Traite les fichiers un à un : une erreur n'empêche pas de passer au suivant."""
        produits: set[Path] = set()
        for i, chemin in enumerate(self.fichiers):
            if self.annule:
                break
            self.debut_fichier.emit(i + 1, len(self.fichiers), chemin.name)
            try:
                mode = mode_du_fichier(chemin, self.reglages["mode"])
                sortie = Path(self.reglages["sortie"]) / f"{chemin.stem}_{mode}.wav"
                if sortie in produits:
                    raise ts.ErreurTesla(f"Un autre fichier de ce lot produit déjà « {sortie.name} » : fichier ignoré.")
                args = ts.creer_parser().parse_args([str(chemin)])
                vars(args).update(self.reglages)
                ts.valider_options(args)
                avertissement = ts.traiter_fichier(chemin, mode, sortie, args)
                produits.add(sortie)
                self.fichier_ok.emit(str(sortie), avertissement or "")
            except ts.ErreurTesla as erreur:
                self.fichier_erreur.emit(chemin.name, str(erreur))
            except Exception as erreur:  # la fenêtre doit survivre à toute panne imprévue
                traceback.print_exc()
                self.fichier_erreur.emit(chemin.name, f"Erreur inattendue : {erreur}")
        self.fini.emit(self.annule)


class ListeDepot(QListWidget):
    """Liste de fichiers qui accepte le glisser-déposer depuis le Finder."""

    deposes = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, evenement):
        if evenement.mimeData().hasUrls():
            evenement.acceptProposedAction()

    def dragMoveEvent(self, evenement):
        if evenement.mimeData().hasUrls():
            evenement.acceptProposedAction()

    def dropEvent(self, evenement):
        self.deposes.emit([Path(u.toLocalFile()) for u in evenement.mimeData().urls() if u.isLocalFile()])
        evenement.acceptProposedAction()

    def paintEvent(self, evenement):
        super().paintEvent(evenement)
        if self.count() == 0:  # invitation affichée tant que la liste est vide
            from PySide6.QtGui import QPainter
            peintre = QPainter(self.viewport())
            peintre.setPen(self.palette().placeholderText().color())
            peintre.drawText(
                self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                "Glisse tes morceaux ou un dossier ici\nmp3, wav, flac, m4a, ogg, mid",
            )


class Curseur(QWidget):
    """Curseur à deux décimales accompagné de sa valeur."""

    def __init__(self, minimum: float, maximum: float, valeur: float):
        super().__init__()
        self.curseur = QSlider(Qt.Orientation.Horizontal)
        self.curseur.setRange(round(minimum * 100), round(maximum * 100))
        self.etiquette = QLabel()
        self.etiquette.setMinimumWidth(34)
        self.etiquette.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ligne = QHBoxLayout(self)
        ligne.setContentsMargins(0, 0, 0, 0)
        ligne.addWidget(self.curseur)
        ligne.addWidget(self.etiquette)
        self.curseur.valueChanged.connect(lambda v: self.etiquette.setText(f"{v / 100:.2f}"))
        self.setValue(valeur)
        self.etiquette.setText(f"{self.value():.2f}")

    def value(self) -> float:
        return self.curseur.value() / 100

    def setValue(self, valeur: float) -> None:
        self.curseur.setValue(round(valeur * 100))


def spin_reel(minimum: float, maximum: float, valeur: float, suffixe: str, pas: float, decimales: int = 0):
    """Crée un champ numérique avec son unité."""
    champ = QDoubleSpinBox()
    champ.setRange(minimum, maximum)
    champ.setDecimals(decimales)
    champ.setSingleStep(pas)
    champ.setValue(valeur)
    champ.setSuffix(suffixe)
    return champ


def formulaire() -> tuple[QWidget, QFormLayout]:
    """Crée un formulaire aligné à gauche, sans marges."""
    page = QWidget()
    disposition = QFormLayout(page)
    disposition.setContentsMargins(0, 0, 0, 0)
    disposition.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return page, disposition


class Fenetre(QMainWindow):
    """Fenêtre principale."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tesla Square")
        self.resize(860, 720)
        self.fichiers: list[Path] = []
        self.fil: QThread | None = None
        self.convertisseur: Convertisseur | None = None
        self.erreurs: list[tuple[str, str]] = []
        self.lecteur = QProcess(self)
        self.lecteur.finished.connect(self._lecture_terminee)
        self.fichier_lu: str | None = None

        centre = QWidget()
        self.setCentralWidget(centre)
        grille = QGridLayout(centre)
        grille.setContentsMargins(16, 16, 16, 16)
        grille.setHorizontalSpacing(16)
        grille.setVerticalSpacing(12)
        grille.addWidget(self._bloc_morceaux(), 0, 0)
        grille.addWidget(self._bloc_reglages(), 0, 1, 2, 1)
        grille.addWidget(self._bloc_resultats(), 1, 0)
        grille.addWidget(self._bloc_lancement(), 2, 0, 1, 2)
        grille.setColumnStretch(0, 5)
        grille.setColumnStretch(1, 4)
        grille.setRowStretch(0, 3)
        grille.setRowStretch(1, 2)

        self._charger_reglages()
        self._mettre_a_jour_etat()

    # --- Construction de la fenêtre ---

    def _bloc_morceaux(self) -> QWidget:
        bloc = QGroupBox("Morceaux")
        colonne = QVBoxLayout(bloc)
        self.liste = ListeDepot()
        self.liste.deposes.connect(self.ajouter)
        self.liste.itemSelectionChanged.connect(self._mettre_a_jour_etat)
        colonne.addWidget(self.liste)

        ligne = QHBoxLayout()
        ajouter = QPushButton("Ajouter des fichiers…")
        ajouter.clicked.connect(self._choisir_fichiers)
        dossier = QPushButton("Ajouter un dossier…")
        dossier.clicked.connect(self._choisir_dossier)
        self.bouton_retirer = QPushButton("Retirer")
        self.bouton_retirer.clicked.connect(self._retirer)
        for bouton in (ajouter, dossier):
            ligne.addWidget(bouton)
        ligne.addStretch()
        ligne.addWidget(self.bouton_retirer)
        colonne.addLayout(ligne)
        return bloc

    def _bloc_reglages(self) -> QWidget:
        bloc = QGroupBox("Réglages")
        colonne = QVBoxLayout(bloc)
        colonne.setSpacing(10)

        self.groupe_modes = QButtonGroup(self)
        self.boutons_modes: dict[str, QRadioButton] = {}
        ligne = QHBoxLayout()
        for cle, nom in NOMS_MODES.items():
            bouton = QRadioButton(nom)
            self.groupe_modes.addButton(bouton)
            self.boutons_modes[cle] = bouton
            bouton.toggled.connect(self._mode_change)
            ligne.addWidget(bouton)
        ligne.addStretch()
        colonne.addLayout(ligne)
        # Le rendu de ce mode a été validé une octave plus haut. Sur un clic seulement :
        # au lancement, c'est l'octave mémorisée qui compte.
        self.boutons_modes["instru"].clicked.connect(lambda: self.champ_octave.setValue(1))

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setMinimumHeight(48)
        self.description.setAlignment(Qt.AlignmentFlag.AlignTop)
        colonne.addWidget(self.description)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._page_melodie())
        self.pages.addWidget(self._page_clip())
        self.pages.addWidget(self._page_midi())
        self.pages.addWidget(self._page_instru())
        colonne.addWidget(self.pages)

        trait = QFrame()
        trait.setFrameShape(QFrame.Shape.HLine)
        trait.setFrameShadow(QFrame.Shadow.Sunken)
        colonne.addWidget(trait)

        page, formu = formulaire()
        self.duty = Curseur(0.05, 0.95, 0.5)
        self.duty.setToolTip("0.50 : carré classique. 0.10–0.30 : son plus fin sur certaines bobines.")
        self.volume = Curseur(0.05, ts.VOLUME_MAX, 0.8)
        self.volume.setToolTip(f"Niveau du fichier produit, plafonné à {ts.VOLUME_MAX}.")
        formu.addRow("Rapport cyclique", self.duty)
        formu.addRow("Volume", self.volume)
        self.champ_octave = QSpinBox()
        self.champ_octave.setRange(-3, 3)
        self.champ_octave.setToolTip("Transpose le résultat. Modes Mélodie et MIDI.")
        self.ligne_octave = QLabel("Octave")
        formu.addRow(self.ligne_octave, self.champ_octave)

        extrait = QWidget()
        ligne = QHBoxLayout(extrait)
        ligne.setContentsMargins(0, 0, 0, 0)
        self.case_preview = QCheckBox("Seulement les")
        self.champ_preview = spin_reel(1, 3600, 20, " s", 5)
        self.case_preview.toggled.connect(self.champ_preview.setEnabled)
        self.champ_preview.setEnabled(False)
        ligne.addWidget(self.case_preview)
        ligne.addWidget(self.champ_preview)
        ligne.addStretch()
        formu.addRow("Essai rapide", extrait)
        colonne.addWidget(page)

        colonne.addStretch()
        defaut = QPushButton("Réglages par défaut")
        defaut.clicked.connect(self._reglages_defaut)
        colonne.addWidget(defaut, alignment=Qt.AlignmentFlag.AlignRight)
        return bloc

    def _page_melodie(self) -> QWidget:
        page, formu = formulaire()
        self.quantize = QCheckBox("Caler sur les demi-tons")
        self.quantize.setChecked(True)
        self.quantize.setToolTip("Donne des notes franches, sans vibrato. Conseillé sur une bobine.")
        self.continu = QCheckBox("Son continu, sans trous")
        self.continu.setChecked(True)
        self.continu.setToolTip(
            "Tient chaque note jusqu'à la suivante, comme au synthé.\n"
            "Seul un vrai silence du morceau coupe encore le son."
        )
        self.riff = QCheckBox("Suivre le riff")
        self.riff.setToolTip(
            "Pour un riff répété dans un morceau chargé : il y a toujours une note.\n"
            "Règle la plage pour qu'elle s'arrête sous la voix. Remplace le filtre des graves."
        )
        self.riff_bas = spin_reel(30, 3000, 98, " Hz", 10)
        self.riff_haut = spin_reel(30, 3000, 262, " Hz", 10)
        self.riff.toggled.connect(self._riff_change)
        self.min_note = spin_reel(0, 1000, 60, " ms", 10)
        self.min_note.setToolTip("À monter (100–150 ms) s'il y a trop de notes parasites.")
        self.hp_analyse = spin_reel(0, 2000, 200, " Hz", 50)
        self.hp_analyse.setToolTip(
            "Retire les graves avant de chercher la mélodie.\n"
            "400–500 Hz si l'outil suit les accords au lieu d'une mélodie aiguë.\n"
            "0–100 Hz pour une mélodie grave, comme une voix d'homme."
        )
        self.gate_melodie = spin_reel(-90, 0, -40, " dB", 5)
        self.gate_melodie.setToolTip("En dessous de ce niveau : silence total. À baisser si des notes manquent.")
        formu.addRow("", self.quantize)
        formu.addRow("", self.continu)
        formu.addRow("", self.riff)
        formu.addRow("Bas de la plage du riff", self.riff_bas)
        formu.addRow("Haut de la plage du riff", self.riff_haut)
        formu.addRow("Note la plus courte", self.min_note)
        formu.addRow("Filtre des graves", self.hp_analyse)
        formu.addRow("Seuil de bruit", self.gate_melodie)
        self._riff_change(False)
        return page

    def _riff_change(self, actif: bool) -> None:
        self.riff_bas.setEnabled(actif)
        self.riff_haut.setEnabled(actif)
        self.hp_analyse.setEnabled(not actif)

    def _page_clip(self) -> QWidget:
        page, formu = formulaire()
        self.low = spin_reel(20, 20000, 300, " Hz", 50)
        self.high = spin_reel(20, 22000, 3000, " Hz", 100)
        self.gate_clip = spin_reel(-90, 0, -40, " dB", 5)
        self.gate_clip.setToolTip("En dessous de ce niveau : silence total, au lieu d'un buzz.")
        formu.addRow("Fréquence basse", self.low)
        formu.addRow("Fréquence haute", self.high)
        formu.addRow("Seuil de bruit", self.gate_clip)
        return page

    def _page_instru(self) -> QWidget:
        page, formu = formulaire()
        self.instru_bas = spin_reel(20, 8000, 65, " Hz", 5)
        self.instru_haut = spin_reel(20, 8000, 262, " Hz", 10)
        self.instru_haut.setToolTip("Au-dessus, les notes sont retirées : c'est là que chante surtout la voix.")
        formu.addRow("Note la plus grave", self.instru_bas)
        formu.addRow("Note la plus aiguë", self.instru_haut)
        return page

    def _page_midi(self) -> QWidget:
        page, formu = formulaire()
        self.poly = QCheckBox("Plusieurs voix en même temps")
        self.poly.setToolTip("Par défaut, seule la note la plus aiguë est jouée, ce qui convient le mieux aux bobines.")
        formu.addRow("", self.poly)
        return page

    def _bloc_resultats(self) -> QWidget:
        bloc = QGroupBox("Résultats")
        colonne = QVBoxLayout(bloc)
        self.resultats = QListWidget()
        self.resultats.itemSelectionChanged.connect(self._mettre_a_jour_etat)
        self.resultats.itemDoubleClicked.connect(lambda _: self._ecouter())
        colonne.addWidget(self.resultats)
        ligne = QHBoxLayout()
        self.bouton_ecouter = QPushButton("Écouter")
        self.bouton_ecouter.setToolTip("Pense à baisser le volume de la bobine avant la première écoute.")
        self.bouton_ecouter.clicked.connect(self._ecouter)
        self.bouton_finder = QPushButton("Afficher dans le Finder")
        self.bouton_finder.clicked.connect(self._afficher_finder)
        ligne.addWidget(self.bouton_ecouter)
        ligne.addWidget(self.bouton_finder)
        ligne.addStretch()
        colonne.addLayout(ligne)
        return bloc

    def _bloc_lancement(self) -> QWidget:
        bloc = QWidget()
        colonne = QVBoxLayout(bloc)
        colonne.setContentsMargins(0, 0, 0, 0)

        ligne = QHBoxLayout()
        ligne.addWidget(QLabel("Enregistrer dans"))
        self.champ_sortie = QLineEdit()
        ligne.addWidget(self.champ_sortie)
        choisir = QPushButton("Choisir…")
        choisir.clicked.connect(self._choisir_sortie)
        ligne.addWidget(choisir)
        colonne.addLayout(ligne)

        ligne = QHBoxLayout()
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        self.barre = QProgressBar()
        self.barre.setRange(0, 0)
        self.barre.setTextVisible(False)
        self.barre.setMaximumWidth(160)
        self.barre.hide()
        self.bouton_annuler = QPushButton("Annuler")
        self.bouton_annuler.clicked.connect(self._annuler)
        self.bouton_annuler.hide()
        self.bouton_convertir = QPushButton("Convertir")
        self.bouton_convertir.setDefault(True)
        police = QFont(self.bouton_convertir.font())
        police.setBold(True)
        self.bouton_convertir.setFont(police)
        self.bouton_convertir.setMinimumWidth(130)
        self.bouton_convertir.clicked.connect(self.convertir)
        ligne.addWidget(self.statut, 1)
        ligne.addWidget(self.barre)
        ligne.addWidget(self.bouton_annuler)
        ligne.addWidget(self.bouton_convertir)
        colonne.addLayout(ligne)
        return bloc

    # --- Liste des morceaux ---

    def ajouter(self, chemins: list[Path]) -> None:
        """Ajoute des fichiers ou le contenu de dossiers, sans doublon."""
        ignores = 0
        for chemin in chemins:
            if chemin.is_dir():
                try:
                    candidats = ts.lister_entrees(chemin, None)
                except ts.ErreurTesla:
                    candidats, ignores = [], ignores + 1
            elif chemin.suffix.lower() in ts.EXT_AUDIO | ts.EXT_MIDI:
                candidats = [chemin]
            else:
                candidats, ignores = [], ignores + 1
            for fichier in candidats:
                if fichier not in self.fichiers:
                    self.fichiers.append(fichier)
                    element = QListWidgetItem(fichier.name)
                    element.setToolTip(str(fichier))
                    self.liste.addItem(element)
        self._mettre_a_jour_etat()
        self.statut.setText(f"{ignores} élément(s) ignoré(s) : format non pris en charge." if ignores else "")

    def _choisir_fichiers(self) -> None:
        formats = " ".join(f"*{e}" for e in sorted(ts.EXT_AUDIO | ts.EXT_MIDI))
        noms, _ = QFileDialog.getOpenFileNames(self, "Choisir des morceaux", "", f"Musique ({formats})")
        self.ajouter([Path(n) for n in noms])

    def _choisir_dossier(self) -> None:
        nom = QFileDialog.getExistingDirectory(self, "Choisir un dossier de morceaux")
        if nom:
            self.ajouter([Path(nom)])

    def _retirer(self) -> None:
        for rang in sorted((self.liste.row(e) for e in self.liste.selectedItems()), reverse=True):
            self.liste.takeItem(rang)
            del self.fichiers[rang]
        self._mettre_a_jour_etat()

    def _choisir_sortie(self) -> None:
        nom = QFileDialog.getExistingDirectory(self, "Dossier d'enregistrement", self.champ_sortie.text())
        if nom:
            self.champ_sortie.setText(nom)

    # --- Réglages ---

    def mode(self) -> str:
        return next(c for c, b in self.boutons_modes.items() if b.isChecked())

    def _mode_change(self) -> None:
        mode = self.mode() if self.groupe_modes.checkedButton() else "melodie"
        self.description.setText(DESCRIPTIONS[mode])
        self.pages.setCurrentIndex(list(NOMS_MODES).index(mode))
        octave_utile = mode != "clip"
        self.champ_octave.setVisible(octave_utile)
        self.ligne_octave.setVisible(octave_utile)

    def reglages(self) -> dict:
        """Traduit l'état de la fenêtre en options de tesla_square.py."""
        mode = self.mode()
        return {
            "mode": mode,
            "sortie": str(Path(self.champ_sortie.text().strip()).expanduser()),
            "duty": self.duty.value(),
            "volume": self.volume.value(),
            "preview": self.champ_preview.value() if self.case_preview.isChecked() else None,
            "gate": self.gate_clip.value() if mode == "clip" else self.gate_melodie.value(),
            "low": self.low.value(),
            "high": self.high.value(),
            "min_note": self.min_note.value(),
            "quantize": self.quantize.isChecked(),
            "continu": self.continu.isChecked(),
            "riff": self.riff.isChecked(),
            "instru_bas": self.instru_bas.value(),
            "instru_haut": self.instru_haut.value(),
            "riff_bas": self.riff_bas.value(),
            "riff_haut": self.riff_haut.value(),
            "hp_analyse": self.hp_analyse.value(),
            "poly": self.poly.isChecked(),
            "octave": self.champ_octave.value(),
        }

    def _champs_memorises(self) -> dict:
        return {
            "duty": self.duty, "volume": self.volume, "octave": self.champ_octave, "min_note": self.min_note,
            "hp_analyse": self.hp_analyse, "gate_melodie": self.gate_melodie, "gate_clip": self.gate_clip,
            "low": self.low, "high": self.high, "riff_bas": self.riff_bas, "riff_haut": self.riff_haut,
            "instru_bas": self.instru_bas, "instru_haut": self.instru_haut,
        }

    def _reglages_defaut(self) -> None:
        self.duty.setValue(0.5)
        self.volume.setValue(0.8)
        self.champ_octave.setValue(1 if self.mode() == "instru" else 0)
        self.instru_bas.setValue(65)
        self.instru_haut.setValue(262)
        self.min_note.setValue(60)
        self.hp_analyse.setValue(200)
        self.gate_melodie.setValue(-40)
        self.gate_clip.setValue(-40)
        self.low.setValue(300)
        self.high.setValue(3000)
        self.quantize.setChecked(True)
        self.continu.setChecked(True)
        self.riff.setChecked(False)
        self.riff_bas.setValue(98)
        self.riff_haut.setValue(262)
        self.poly.setChecked(False)
        self.case_preview.setChecked(False)

    def _charger_reglages(self) -> None:
        memoire = ouvrir_memoire()
        mode = str(memoire.value("mode", "melodie"))
        self.boutons_modes.get(mode, self.boutons_modes["melodie"]).setChecked(True)
        self._mode_change()
        for cle, champ in self._champs_memorises().items():
            valeur = memoire.value(cle)
            if valeur is not None:
                champ.setValue(type(champ.value())(float(valeur)))
        self.quantize.setChecked(memoire.value("quantize", True, type=bool))
        self.continu.setChecked(memoire.value("continu", True, type=bool))
        self.riff.setChecked(memoire.value("riff", False, type=bool))
        self.poly.setChecked(memoire.value("poly", False, type=bool))
        self.champ_sortie.setText(str(memoire.value("sortie", dossier_sortie_defaut())))

    def _sauver_reglages(self) -> None:
        memoire = ouvrir_memoire()
        memoire.setValue("mode", self.mode())
        for cle, champ in self._champs_memorises().items():
            memoire.setValue(cle, champ.value())
        memoire.setValue("quantize", self.quantize.isChecked())
        memoire.setValue("continu", self.continu.isChecked())
        memoire.setValue("riff", self.riff.isChecked())
        memoire.setValue("poly", self.poly.isChecked())
        memoire.setValue("sortie", self.champ_sortie.text())

    # --- Conversion ---

    def convertir(self) -> None:
        """Lance la conversion dans un fil séparé pour que la fenêtre reste réactive."""
        reglages = self.reglages()
        if not self.champ_sortie.text().strip():
            QMessageBox.warning(self, "Tesla Square", "Choisis un dossier d'enregistrement.")
            return
        try:
            args = ts.creer_parser().parse_args(["x"])
            vars(args).update(reglages)
            ts.valider_options(args)
            Path(reglages["sortie"]).mkdir(parents=True, exist_ok=True)
        except ts.ErreurTesla as erreur:
            # Les messages du moteur citent les options : on les rend lisibles ici.
            QMessageBox.warning(self, "Réglage incorrect", _message_sans_option(str(erreur)))
            return
        except OSError as erreur:
            QMessageBox.warning(self, "Tesla Square", f"Impossible de créer le dossier d'enregistrement :\n{erreur}")
            return

        self.erreurs = []
        self.fil = QThread(self)
        # numba compile son code via LLVM au premier passage : la petite pile par
        # défaut d'un fil secondaire déborde et fait planter l'application.
        self.fil.setStackSize(TAILLE_PILE_FIL)
        self.convertisseur = Convertisseur(list(self.fichiers), reglages)
        self.convertisseur.moveToThread(self.fil)
        self.fil.started.connect(self.convertisseur.travailler)
        self.convertisseur.debut_fichier.connect(self._debut_fichier)
        self.convertisseur.fichier_ok.connect(self._fichier_ok)
        self.convertisseur.fichier_erreur.connect(lambda nom, msg: self.erreurs.append((nom, msg)))
        self.convertisseur.fini.connect(self._conversion_finie)
        self.fil.start()
        self._mettre_a_jour_etat()

    def _debut_fichier(self, rang: int, total: int, nom: str) -> None:
        prefixe = f"{rang}/{total} — " if total > 1 else ""
        self.statut.setText(f"{prefixe}{nom}… Le premier morceau peut prendre plus de temps.")

    def _fichier_ok(self, chemin: str, avertissement: str) -> None:
        if not any(self.resultats.item(i).data(Qt.ItemDataRole.UserRole) == chemin for i in range(self.resultats.count())):
            element = QListWidgetItem(Path(chemin).name)
            element.setData(Qt.ItemDataRole.UserRole, chemin)
            element.setToolTip(chemin)
            self.resultats.addItem(element)
            self.resultats.setCurrentItem(element)
        if avertissement:
            self.erreurs.append((Path(chemin).name, avertissement))

    def _conversion_finie(self, annule: bool) -> None:
        self.fil.quit()
        self.fil.wait()
        self.fil = None
        self.convertisseur = None
        self._mettre_a_jour_etat()
        nb_erreurs = len(self.erreurs)
        if annule:
            self.statut.setText("Conversion annulée.")
        elif nb_erreurs:
            self.statut.setText(f"Terminé, avec {nb_erreurs} problème(s).")
        else:
            self.statut.setText("Terminé.")
        if self.erreurs:
            detail = "\n\n".join(f"{nom}\n{_message_sans_option(msg)}" for nom, msg in self.erreurs)
            QMessageBox.warning(self, "Tesla Square", detail)

    def _annuler(self) -> None:
        if self.convertisseur:
            self.convertisseur.annule = True
            self.bouton_annuler.setEnabled(False)
            self.statut.setText("Annulation à la fin du morceau en cours…")

    # --- Écoute ---

    def _ecouter(self) -> None:
        """Lance ou arrête la lecture du résultat sélectionné."""
        if self.lecteur.state() != QProcess.ProcessState.NotRunning:
            self.lecteur.kill()
            return
        element = self.resultats.currentItem()
        if element:
            self.fichier_lu = element.data(Qt.ItemDataRole.UserRole)
            self.lecteur.start(ts.chemin_afplay(), [self.fichier_lu])
            self.bouton_ecouter.setText("Arrêter")

    def _lecture_terminee(self) -> None:
        self.fichier_lu = None
        self.bouton_ecouter.setText("Écouter")

    def _afficher_finder(self) -> None:
        element = self.resultats.currentItem()
        if element:
            chemin = element.data(Qt.ItemDataRole.UserRole)
            if not QProcess.startDetached("/usr/bin/open", ["-R", chemin]):
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(chemin).parent)))

    # --- État général ---

    def _mettre_a_jour_etat(self) -> None:
        en_cours = self.fil is not None
        self.bouton_convertir.setEnabled(bool(self.fichiers) and not en_cours)
        self.bouton_retirer.setEnabled(bool(self.liste.selectedItems()) and not en_cours)
        self.barre.setVisible(en_cours)
        self.bouton_annuler.setVisible(en_cours)
        self.bouton_annuler.setEnabled(True)
        selection = self.resultats.currentItem() is not None
        self.bouton_ecouter.setEnabled(selection)
        self.bouton_finder.setEnabled(selection)

    def closeEvent(self, evenement) -> None:
        """Arrête proprement la lecture et le travail en cours avant de quitter."""
        self._sauver_reglages()
        if self.lecteur.state() != QProcess.ProcessState.NotRunning:
            self.lecteur.kill()
            self.lecteur.waitForFinished(2000)
        if self.fil is not None:
            # Détruire un QThread actif ferait planter l'app : on attend la fin du morceau.
            self.convertisseur.annule = True
            self.hide()
            self.fil.quit()
            self.fil.wait()
        evenement.accept()


def _message_sans_option(message: str) -> str:
    """Remplace les noms d'options de la ligne de commande par les libellés de la fenêtre."""
    if message.startswith("Aucune mélodie détectée"):
        return ("Aucune mélodie détectée. Baisse le seuil de bruit ou la durée de la note la plus courte, "
                "ou essaie le mode Clip si le morceau n'a pas de voix principale claire.")
    for option, libelle in {
        "--duty": "le rapport cyclique", "--volume": "le volume", "--preview": "l'essai rapide",
        "--low": "la fréquence basse", "--high": "la fréquence haute", "--gate": "le seuil de bruit",
        "--min-note": "la note la plus courte", "--hp-analyse": "le filtre des graves", "--octave": "l'octave",
        "--riff-bas": "le bas de la plage du riff", "--riff-haut": "le haut de la plage du riff",
        "--instru-bas": "la note la plus grave", "--instru-haut": "la note la plus aiguë",
    }.items():
        message = message.replace(option, libelle)
    return message[0].upper() + message[1:]


def auto_test(application: QApplication, entree: Path, sortie: Path) -> int:
    """Vérification de compilation : convertit un fichier par le même chemin que le bouton Convertir.

    Couvre ce que la ligne de commande ne couvre pas dans l'application compilée :
    la vraie fenêtre et le fil de travail, avec librosa et numba. Renvoie 0 si tout va bien.
    """
    fenetre = Fenetre()
    fenetre._sauver_reglages = lambda: None  # ne touche pas aux préférences de l'utilisateur
    QMessageBox.warning = lambda _f, _t, texte: print(f"Échec : {texte}", file=sys.stderr)
    fenetre.show()
    fenetre.boutons_modes["melodie"].setChecked(True)
    fenetre.champ_sortie.setText(str(sortie))
    fenetre.ajouter([entree])

    def verifier() -> None:
        # Interrogé depuis le fil principal : quand fil vaut None, la fenêtre a tout reçu.
        if fenetre.fil is None:
            application.exit(0 if not fenetre.erreurs and fenetre.resultats.count() == 1 else 1)

    fenetre.convertir()
    minuterie = QTimer()
    minuterie.timeout.connect(verifier)
    minuterie.start(100)
    QTimer.singleShot(300_000, lambda: application.exit(2))  # garde-fou : jamais de blocage
    code = application.exec()
    fenetre.close()
    return code


def main() -> int:
    """Lance la fenêtre, ou la conversion en ligne de commande avec --cli."""
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        return ts.main(sys.argv[2:])
    if len(sys.argv) == 4 and sys.argv[1] == "--auto-test":
        return auto_test(QApplication(sys.argv[:1]), Path(sys.argv[2]), Path(sys.argv[3]))

    application = QApplication(sys.argv)
    application.setApplicationName("Tesla Square")
    fenetre = Fenetre()

    def signaler_panne(type_erreur, erreur, trace):
        traceback.print_exception(type_erreur, erreur, trace)
        QMessageBox.critical(fenetre, "Tesla Square", f"Erreur inattendue :\n{erreur}")

    sys.excepthook = signaler_panne
    fenetre.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
