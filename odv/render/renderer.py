# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw the whole overlay for one moment in time."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from .theme import THEMES, load_fonts
from .widgets import RenderContext

REF_WIDTH = 1920.0


def theme_for(project):
    th = THEMES.get(project.theme, THEMES["Broadcast"])
    if project.accent:
        th = replace(th, accent=QColor(project.accent))
    return th


class OverlayRenderer:
    def __init__(self, project):
        load_fonts()
        self.project = project

    def context(self, t: float) -> RenderContext:
        pr = self.project
        return RenderContext(pr.session, pr.laps, theme_for(pr), t, pr.speed_unit)

    def paint(self, p: QPainter, width: float, t: float, ctx: RenderContext = None, widgets=None):
        """Paint onto an existing painter whose origin is the video's top-left and whose video
        width in device units is ``width``."""
        ctx = ctx or self.context(t)
        ctx.t = t
        s = width / REF_WIDTH
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.scale(s, s)
        for w in (widgets if widgets is not None else self.project.widgets):
            w.paint(p, ctx)
        p.restore()
        return ctx

    def render_image(self, img: QImage, t: float, ctx: RenderContext = None) -> RenderContext:
        p = QPainter(img)
        try:
            return self.paint(p, img.width(), t, ctx)
        finally:
            p.end()
