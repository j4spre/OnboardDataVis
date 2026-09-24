# SPDX-License-Identifier: GPL-3.0-or-later
"""Visual themes and small drawing helpers shared by all widgets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Dict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetricsF, QLinearGradient,
                           QPainter, QPainterPath, QPen)

ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
_fonts_loaded = False


def load_fonts():
    global _fonts_loaded
    if _fonts_loaded:
        return
    fdir = os.path.join(ASSETS, "fonts")
    if os.path.isdir(fdir):
        for f in sorted(os.listdir(fdir)):
            if f.lower().endswith((".ttf", ".otf")):
                QFontDatabase.addApplicationFont(os.path.join(fdir, f))
    _fonts_loaded = True


@dataclass
class Theme:
    name: str
    font: str = "Barlow Condensed"
    panel: QColor = field(default_factory=lambda: QColor(12, 14, 18, 185))
    panel_border: QColor = field(default_factory=lambda: QColor(255, 255, 255, 30))
    radius: float = 14.0
    text: QColor = field(default_factory=lambda: QColor(255, 255, 255))
    text_dim: QColor = field(default_factory=lambda: QColor(160, 168, 178))
    accent: QColor = field(default_factory=lambda: QColor("#ff3b30"))
    accent2: QColor = field(default_factory=lambda: QColor("#ffd60a"))
    good: QColor = field(default_factory=lambda: QColor("#30d158"))
    bad: QColor = field(default_factory=lambda: QColor("#ff453a"))
    best: QColor = field(default_factory=lambda: QColor("#bf5af2"))
    throttle: QColor = field(default_factory=lambda: QColor("#30d158"))
    brake: QColor = field(default_factory=lambda: QColor("#ff453a"))
    track: QColor = field(default_factory=lambda: QColor(255, 255, 255, 40))
    glow: bool = False
    shadow: bool = False
    show_panels: bool = True
    weight: int = QFont.Weight.DemiBold

    def with_accent(self, color: str) -> "Theme":
        return replace(self, accent=QColor(color))


THEMES: Dict[str, Theme] = {
    "Broadcast": Theme("Broadcast"),
    "Motorsport HUD": Theme(
        "Motorsport HUD", font="Chakra Petch", panel=QColor(0, 12, 20, 150),
        panel_border=QColor(0, 229, 255, 90), radius=4, text=QColor("#eaffff"), text_dim=QColor(120, 200, 215),
        accent=QColor("#00e5ff"), accent2=QColor("#ffb000"), good=QColor("#39ff88"), bad=QColor("#ff3864"),
        best=QColor("#c77dff"), throttle=QColor("#39ff88"), brake=QColor("#ff3864"),
        track=QColor(0, 229, 255, 50), glow=True),
    "Minimal": Theme(
        "Minimal", panel=QColor(0, 0, 0, 0), panel_border=QColor(0, 0, 0, 0), radius=0,
        text=QColor(255, 255, 255), text_dim=QColor(235, 235, 235, 200), accent=QColor("#ffffff"),
        accent2=QColor("#ffcc00"), track=QColor(255, 255, 255, 60), shadow=True, show_panels=False,
        weight=QFont.Weight.Bold),
}


# ------------------------------------------------------------------ helpers

def make_font(theme: Theme, px: float, weight=None, family=None) -> QFont:
    f = QFont(family or theme.font)
    f.setPixelSize(max(1, int(round(px))))
    f.setWeight(weight if weight is not None else theme.weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def draw_panel(p: QPainter, r: QRectF, theme: Theme, radius=None, force=False):
    if not (theme.show_panels or force):
        return
    rad = theme.radius if radius is None else radius
    p.save()
    p.setPen(QPen(theme.panel_border, 1.5) if theme.panel_border.alpha() else Qt.PenStyle.NoPen)
    g = QLinearGradient(r.topLeft(), r.bottomLeft())
    c1 = QColor(theme.panel)
    c2 = QColor(theme.panel)
    c1.setAlpha(min(255, int(theme.panel.alpha() * 0.85)))
    c2.setAlpha(min(255, int(theme.panel.alpha() * 1.1)))
    g.setColorAt(0, c1)
    g.setColorAt(1, c2)
    p.setBrush(QBrush(g))
    p.drawRoundedRect(r, rad, rad)
    p.restore()


def draw_text(p: QPainter, r: QRectF, text: str, theme: Theme, px: float, color: QColor = None,
              align=Qt.AlignmentFlag.AlignCenter, weight=None, family=None, glow=None, tabular=True):
    """Text with optional glow / shadow. ``align`` uses Qt alignment flags inside ``r``."""
    color = color or theme.text
    font = make_font(theme, px, weight, family)
    if tabular:
        try:
            font.setFeature(QFont.Tag("tnum"), 1)
        except Exception:
            pass
    fm = QFontMetricsF(font)
    w = fm.horizontalAdvance(text)
    asc, desc = fm.ascent(), fm.descent()
    cap = fm.capHeight() if hasattr(fm, "capHeight") else asc * 0.7
    if align & Qt.AlignmentFlag.AlignLeft:
        x = r.left()
    elif align & Qt.AlignmentFlag.AlignRight:
        x = r.right() - w
    else:
        x = r.center().x() - w / 2
    if align & Qt.AlignmentFlag.AlignTop:
        y = r.top() + cap
    elif align & Qt.AlignmentFlag.AlignBottom:
        y = r.bottom()
    else:
        y = r.center().y() + cap / 2
    glow = theme.glow if glow is None else glow
    p.save()
    if glow or theme.shadow:
        path = QPainterPath()
        path.addText(QPointF(x, y), font, text)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if glow:
            for width, alpha in ((px * 0.22, 30), (px * 0.12, 60)):
                gc = QColor(color)
                gc.setAlpha(alpha)
                p.setPen(QPen(gc, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.drawPath(path)
        if theme.shadow:
            p.translate(px * 0.04, px * 0.05)
            sc = QColor(0, 0, 0, 170)
            p.setPen(QPen(sc, px * 0.08, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(sc)
            p.drawPath(path)
            p.translate(-px * 0.04, -px * 0.05)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(path)
    else:
        p.setFont(font)
        p.setPen(color)
        p.drawText(QPointF(x, y), text)
    p.restore()
    return w


def text_width(theme: Theme, px: float, text: str, weight=None, family=None) -> float:
    return QFontMetricsF(make_font(theme, px, weight, family)).horizontalAdvance(text)


def glow_pen(color: QColor, width: float, alpha: int) -> QPen:
    c = QColor(color)
    c.setAlpha(alpha)
    return QPen(c, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def lerp_color(a: QColor, b: QColor, f: float) -> QColor:
    f = max(0.0, min(1.0, f))
    return QColor(int(a.red() + (b.red() - a.red()) * f), int(a.green() + (b.green() - a.green()) * f),
                  int(a.blue() + (b.blue() - a.blue()) * f), int(a.alpha() + (b.alpha() - a.alpha()) * f))
