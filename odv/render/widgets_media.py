# SPDX-License-Identifier: GPL-3.0-or-later
"""Media layers: free text, images (with alpha) and blur / pixelate regions."""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPainterPathStroker,
                           QPen, QTransform)

from .base import Prop, Widget
from .theme import draw_panel, draw_text

PT_TO_REF = 4.0 / 3.0  # 1 pt at 1080p output = 1.333 reference px


class TextLabel(Widget):
    TYPE, NAME = "text", "Text"
    CATEGORY = "Media"
    USES_DATA = False
    DEFAULT_SIZE = (520, 90)
    PROPS = [
        Prop("text", "textarea", "Your text", "Text", group="Text"),
        Prop("font", "font", "Barlow Condensed", "Font", group="Text"),
        Prop("size_pt", "float", 36.0, "Size (pt at 1080p)", minv=2, maxv=600, group="Text", decimals=1),
        Prop("bold", "bool", True, "Bold", group="Text"),
        Prop("italic", "bool", False, "Italic", group="Text"),
        Prop("color", "color", "#ffffff", "Colour", group="Text"),
        Prop("align", "choice", "left", "Horizontal align", ["left", "center", "right"], group="Text"),
        Prop("valign", "choice", "middle", "Vertical align", ["top", "middle", "bottom"], group="Text"),
        Prop("letter_spacing", "float", 0.0, "Letter spacing (%)", minv=-50, maxv=200, group="Text", decimals=0),
        Prop("line_spacing", "float", 1.0, "Line spacing", minv=0.5, maxv=4, group="Text", decimals=2),
        Prop("outline", "float", 0.0, "Outline width (px)", minv=0, maxv=40, group="Effects", decimals=1),
        Prop("outline_color", "color", "#000000", "Outline colour", group="Effects"),
        Prop("shadow", "bool", True, "Drop shadow", group="Effects"),
        Prop("shadow_color", "color", "#b4000000", "Shadow colour", group="Effects"),
        Prop("bg_color", "color", "", "Background colour", group="Effects"),
        Prop("padding", "float", 16.0, "Padding (px)", minv=0, maxv=200, group="Effects", decimals=0),
        Prop("rotation", "float", 0.0, "Rotation (deg)", minv=-360, maxv=360, group="Effects", decimals=1),
    ]
    DEFAULT_PROPS = {"panel": False}

    def font(self) -> QFont:
        pr = self.props
        f = QFont(pr.get("font") or "Barlow Condensed")
        f.setPixelSize(max(1, int(round(float(pr["size_pt"]) * PT_TO_REF))))
        f.setBold(bool(pr["bold"]))
        f.setItalic(bool(pr["italic"]))
        if pr.get("letter_spacing"):
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100.0 + float(pr["letter_spacing"]))
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        return f

    def draw(self, p, r, ctx):
        pr = self.props
        p.save()
        if pr.get("rotation"):
            c = r.center()
            p.translate(c)
            p.rotate(float(pr["rotation"]))
            p.translate(-c)
        if pr.get("panel"):
            if pr.get("bg_color"):
                p.save()
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(pr["bg_color"]))
                rad = ctx.theme.radius if ctx.theme else 8
                p.drawRoundedRect(r, rad, rad)
                p.restore()
            else:
                draw_panel(p, r, ctx.theme)
        font = self.font()
        fm = QFontMetricsF(font)
        lines = str(pr.get("text", "")).split("\n")
        pad = float(pr["padding"])
        inner = r.adjusted(pad, pad * 0.5, -pad, -pad * 0.5)
        lh = fm.lineSpacing() * float(pr["line_spacing"])
        total = lh * (len(lines) - 1) + fm.ascent() + fm.descent()
        if pr["valign"] == "top":
            y = inner.top() + fm.ascent()
        elif pr["valign"] == "bottom":
            y = inner.bottom() - total + fm.ascent()
        else:
            y = inner.center().y() - total / 2 + fm.ascent()
        path = QPainterPath()
        for line in lines:
            w = fm.horizontalAdvance(line)
            if pr["align"] == "center":
                x = inner.center().x() - w / 2
            elif pr["align"] == "right":
                x = inner.right() - w
            else:
                x = inner.left()
            path.addText(QPointF(x, y), font, line)
            y += lh
        p.setPen(Qt.PenStyle.NoPen)
        if pr.get("shadow"):
            off = max(1.5, font.pixelSize() * 0.05)
            p.setBrush(QColor(pr.get("shadow_color") or "#b4000000"))
            p.drawPath(path.translated(off, off))
        ow = float(pr.get("outline", 0) or 0)
        if ow > 0:
            st = QPainterPathStroker()
            st.setWidth(ow * 2)
            st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            st.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setBrush(QColor(pr.get("outline_color") or "#000000"))
            p.drawPath(st.createStroke(path))
        p.setBrush(QColor(pr.get("color") or "#ffffff"))
        p.drawPath(path)
        p.restore()


class ImageLayer(Widget):
    TYPE, NAME = "image", "Image / logo"
    CATEGORY = "Media"
    USES_DATA = False
    DEFAULT_SIZE = (300, 200)
    PROPS = [
        Prop("path", "file", "", "Image file", group="Image"),
        Prop("keep_aspect", "bool", True, "Keep aspect ratio", group="Image"),
        Prop("rotation", "float", 0.0, "Rotation (deg)", minv=-360, maxv=360, group="Image", decimals=1),
        Prop("flip_h", "bool", False, "Mirror horizontally", group="Image"),
        Prop("flip_v", "bool", False, "Mirror vertically", group="Image"),
    ]
    DEFAULT_PROPS = {"panel": False}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._img = None
        self._path = None

    def image(self):
        path = self.props.get("path", "")
        if path != self._path:
            self._path = path
            img = QImage(path) if path and os.path.exists(path) else None
            if img is not None and not img.isNull():
                img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            self._img = img
        return self._img

    def draw(self, p, r, ctx):
        pr = self.props
        img = self.image()
        if pr.get("panel"):
            draw_panel(p, r, ctx.theme)
        if img is None or img.isNull():
            if ctx.edit_mode or not pr.get("path"):
                p.save()
                p.setPen(QPen(QColor(255, 255, 255, 120), 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(0, 0, 0, 60))
                p.drawRect(r)
                p.restore()
                draw_text(p, r, "IMAGE" if not pr.get("path") else "IMAGE NOT FOUND", ctx.theme,
                          min(r.height() * 0.2, 40), ctx.theme.text_dim, tabular=False)
            return
        iw, ih = img.width(), img.height()
        if pr.get("keep_aspect", True):
            s = min(r.width() / iw, r.height() / ih)
            tw, th_ = iw * s, ih * s
        else:
            tw, th_ = r.width(), r.height()
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        c = r.center()
        p.translate(c)
        if pr.get("rotation"):
            p.rotate(float(pr["rotation"]))
        p.scale(-1 if pr.get("flip_h") else 1, -1 if pr.get("flip_v") else 1)
        p.drawImage(QRectF(-tw / 2, -th_ / 2, tw, th_), img)
        p.restore()


class BlurRegion(Widget):
    TYPE, NAME = "blur", "Blur / pixelate region"
    CATEGORY = "Media"
    USES_DATA = False
    DEFAULT_SIZE = (360, 200)
    PROPS = [
        Prop("mode", "choice", "blur", "Effect", ["blur", "pixelate"], group="Blur"),
        Prop("strength", "float", 25.0, "Blur radius (px)", minv=0.5, maxv=300, group="Blur", decimals=1),
        Prop("pixel_size", "float", 24.0, "Pixel size (px)", minv=2, maxv=300, group="Blur", decimals=0),
        Prop("shape", "choice", "rectangle", "Shape", ["rectangle", "rounded", "ellipse"], group="Blur"),
        Prop("corner", "float", 30.0, "Corner radius (px)", minv=0, maxv=500, group="Blur", decimals=0),
        Prop("edge", "float", 20.0, "Soft edge (px, 0 = hard)", minv=0, maxv=400, group="Blur", decimals=0),
    ]
    DEFAULT_PROPS = {"panel": False}
    DRAW_FIRST = True  # always rendered below the gauges, straight onto the video

    def _mask(self, shape_w, shape_h, ox, oy, cw, ch, edge, corner):
        import cv2

        m = np.zeros((ch, cw), np.float32)
        inset = edge / 2.0
        x0, y0 = ox + inset, oy + inset
        x1, y1 = ox + shape_w - inset, oy + shape_h - inset
        if x1 <= x0 or y1 <= y0:
            return m
        shape = self.props["shape"]
        sh = 4  # sub-pixel precision for cv2 drawing
        P = lambda v: int(round(v * (1 << sh)))  # noqa: E731
        if shape == "ellipse":
            cv2.ellipse(m, (P((x0 + x1) / 2), P((y0 + y1) / 2)), (P((x1 - x0) / 2), P((y1 - y0) / 2)), 0, 0, 360,
                        1.0, -1, cv2.LINE_AA, sh)
        elif shape == "rounded" and corner > 0:
            rr = min(corner, (x1 - x0) / 2, (y1 - y0) / 2)
            cv2.rectangle(m, (P(x0 + rr), P(y0)), (P(x1 - rr), P(y1)), 1.0, -1, cv2.LINE_AA, sh)
            cv2.rectangle(m, (P(x0), P(y0 + rr)), (P(x1), P(y1 - rr)), 1.0, -1, cv2.LINE_AA, sh)
            for cx, cy in ((x0 + rr, y0 + rr), (x1 - rr, y0 + rr), (x0 + rr, y1 - rr), (x1 - rr, y1 - rr)):
                cv2.circle(m, (P(cx), P(cy)), P(rr), 1.0, -1, cv2.LINE_AA, sh)
        else:
            cv2.rectangle(m, (P(x0), P(y0)), (P(x1), P(y1)), 1.0, -1, cv2.LINE_AA, sh)
        if edge > 0.5:
            m = cv2.GaussianBlur(m, (0, 0), edge / 3.0)
        return m

    def draw(self, p, r, ctx):
        arr = ctx.frame_array() if ctx.frame is not None else None
        if arr is None:
            if ctx.edit_mode:
                p.save()
                p.setPen(QPen(QColor(100, 210, 255, 200), 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(100, 210, 255, 40))
                if self.props["shape"] == "ellipse":
                    p.drawEllipse(r)
                else:
                    p.drawRect(r)
                p.restore()
            return
        import cv2

        pr = self.props
        fs = ctx.frame_scale
        H, W = arr.shape[:2]
        edge = max(0.0, float(pr["edge"])) * fs
        rad = max(0.5, float(pr["strength"])) * fs
        margin = int(edge / 2 + (3 * rad if pr["mode"] == "blur" else 0)) + 2
        rx0, ry0 = r.left() * fs, r.top() * fs
        rw, rh = r.width() * fs, r.height() * fs
        cx0, cy0 = max(0, int(rx0 - margin)), max(0, int(ry0 - margin))
        cx1, cy1 = min(W, int(rx0 + rw + margin) + 1), min(H, int(ry0 + rh + margin) + 1)
        if cx1 - cx0 < 2 or cy1 - cy0 < 2:
            return
        crop = np.ascontiguousarray(arr[cy0:cy1, cx0:cx1, :3])
        ch, cw = crop.shape[:2]
        if pr["mode"] == "pixelate":
            ps = max(2.0, float(pr["pixel_size"]) * fs)
            sw, sh_ = max(1, int(round(cw / ps))), max(1, int(round(ch / ps)))
            small = cv2.resize(crop, (sw, sh_), interpolation=cv2.INTER_AREA)
            out = cv2.resize(small, (cw, ch), interpolation=cv2.INTER_NEAREST)
        else:
            sigma = rad / 2.0
            d = max(1, int(sigma / 3))
            if d > 1:
                small = cv2.resize(crop, (max(1, cw // d), max(1, ch // d)), interpolation=cv2.INTER_AREA)
                small = cv2.GaussianBlur(small, (0, 0), sigma / d, borderType=cv2.BORDER_REFLECT)
                out = cv2.resize(small, (cw, ch), interpolation=cv2.INTER_LINEAR)
            else:
                out = cv2.GaussianBlur(crop, (0, 0), sigma, borderType=cv2.BORDER_REFLECT)
        mask = self._mask(rw, rh, rx0 - cx0, ry0 - cy0, cw, ch, edge, float(pr["corner"]) * fs)
        bgra = np.empty((ch, cw, 4), np.uint8)
        bgra[..., :3] = out
        bgra[..., 3] = np.clip(mask * 255.0 + 0.5, 0, 255).astype(np.uint8)
        img = QImage(bgra.data, cw, ch, cw * 4, QImage.Format.Format_ARGB32)
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(QRectF(cx0 / fs, cy0 / fs, cw / fs, ch / fs), img)
        p.restore()
        del bgra
