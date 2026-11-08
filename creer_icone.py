#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dessine l'icône de l'application : une onde carrée sur fond sombre. Usage : creer_icone.py sortie.iconset"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen


def dessiner(taille: int) -> QImage:
    image = QImage(taille, taille, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(taille / 1024, taille / 1024)

    # Marges et arrondi de la grille d'icônes macOS.
    fond = QLinearGradient(0, 100, 0, 924)
    fond.setColorAt(0, QColor("#2b2350"))
    fond.setColorAt(1, QColor("#0d0b1e"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(fond)
    p.drawRoundedRect(QRectF(100, 100, 824, 824), 185, 185)

    # Deux périodes et demie de carré.
    gauche, droite, haut, bas = 210.0, 814.0, 345.0, 679.0
    pas = (droite - gauche) / 5
    onde = QPainterPath(QPointF(gauche, bas))
    x, y = gauche, haut
    onde.lineTo(x, y)
    for _ in range(5):
        x += pas
        onde.lineTo(x, y)
        y = bas if y == haut else haut
        onde.lineTo(x, y)

    p.setBrush(Qt.BrushStyle.NoBrush)
    for largeur, couleur in ((64, QColor(130, 170, 255, 45)), (44, QColor(130, 170, 255, 80)), (24, QColor("#dfe9ff"))):
        p.setPen(QPen(couleur, largeur, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.MiterJoin))
        p.drawPath(onde)
    p.end()
    return image


def main() -> None:
    QGuiApplication([])
    dossier = Path(sys.argv[1])
    dossier.mkdir(parents=True, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        dessiner(base).save(str(dossier / f"icon_{base}x{base}.png"))
        dessiner(base * 2).save(str(dossier / f"icon_{base}x{base}@2x.png"))


if __name__ == "__main__":
    main()
