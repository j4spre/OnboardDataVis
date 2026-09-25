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

    def ordered(self, widgets=None):
        ws = list(widgets if widgets is not None else self.project.widgets)
        return [w for w in ws if getattr(w, "DRAW_FIRST", False)] + [w for w in ws if not getattr(w, "DRAW_FIRST", False)]

    def needs_frame(self, widgets=None) -> bool:
        return any(getattr(w, "DRAW_FIRST", False) and w.props.get("visible", True)
                   for w in (widgets if widgets is not None else self.project.widgets))

    def paint(self, p: QPainter, width: float, t: float, ctx: RenderContext = None, widgets=None,
              frame: QImage = None, edit_mode: bool = False):
        """Paint onto an existing painter whose origin is the video's top-left and whose video
        width in device units is ``width``. ``frame`` is the clean video frame (needed by blur)."""
        ctx = ctx or self.context(t)
        ctx.t = t
        ctx.edit_mode = edit_mode
        if frame is not None or ctx.frame is not None:
            ctx.set_frame(frame, REF_WIDTH)
        s = width / REF_WIDTH
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.scale(s, s)
        for w in self.ordered(widgets):
            try:
                w.paint(p, ctx)
            except Exception as exc:  # one broken widget must not kill the render
                print(f"[odv] widget {w.NAME} failed: {exc}")
        p.restore()
        return ctx

    def render_image(self, img: QImage, t: float, ctx: RenderContext = None) -> RenderContext:
        """Draw the overlay onto ``img`` (a video frame, or a transparent image)."""
        frame = img.copy() if self.needs_frame() else None
        p = QPainter(img)
        try:
            return self.paint(p, img.width(), t, ctx, frame=frame)
        finally:
            p.end()
