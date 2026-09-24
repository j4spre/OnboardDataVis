# SPDX-License-Identifier: GPL-3.0-or-later
"""Overlay widgets (gauges). All drawing happens in *reference* coordinates
(1920 px wide canvas); the renderer scales the painter to the output size."""
from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPolygonF, QRadialGradient)

from ..data.laps import fmt_laptime
from .theme import Theme, draw_panel, draw_text, glow_pen, lerp_color, text_width

SPEED_UNITS = {"km/h": 3.6, "mph": 2.2369363, "m/s": 1.0, "kn": 1.9438445}


def convert(value, from_unit: str, to_unit: str):
    if not to_unit or to_unit == from_unit:
        return value
    if from_unit == "m/s" and to_unit in SPEED_UNITS:
        return value * SPEED_UNITS[to_unit]
    if from_unit == "km/h" and to_unit in SPEED_UNITS:
        return value / 3.6 * SPEED_UNITS[to_unit]
    return value


def nice_ceil(x: float) -> float:
    if not np.isfinite(x) or x <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= x:
            return m * e
    return 10 * e


def fmt_num(v, decimals=0) -> str:
    if v is None or not np.isfinite(v):
        return "--"
    return f"{v:.{decimals}f}"


# ------------------------------------------------------------------ context

class RenderContext:
    def __init__(self, session, laps, theme: Theme, t: float, speed_unit: str = "km/h"):
        self.session = session
        self.laps = laps
        self.theme = theme
        self.t = t
        self.speed_unit = speed_unit
        self._range_cache: Dict[str, Tuple[float, float]] = {}

    def unit(self, ref: str) -> str:
        return self.session.unit_of(ref) if self.session else ""

    def val(self, ref: str, unit: str = "") -> float:
        if not ref or self.session is None:
            return float("nan")
        v = self.session.value(ref, self.t)
        return convert(v, self.unit(ref), unit)

    def series(self, ref: str, times: np.ndarray, unit: str = "") -> np.ndarray:
        if not ref or self.session is None:
            return np.full(len(times), np.nan)
        v = self.session.value(ref, times)
        return convert(v, self.unit(ref), unit)

    def auto_range(self, ref: str, unit: str = "") -> Tuple[float, float]:
        key = ref + "|" + unit
        if key not in self._range_cache:
            src, ch = self.session.resolve_ref(ref) if self.session else (None, None)
            if ch is None:
                rng = (0.0, 1.0)
            else:
                st = ch.stats()
                rng = (convert(st["p01"], ch.unit, unit), convert(st["p99"], ch.unit, unit))
            self._range_cache[key] = rng
        return self._range_cache[key]


# ------------------------------------------------------------------ props

@dataclass
class Prop:
    name: str
    kind: str  # channel | float | int | bool | color | choice | text | file
    default: Any
    label: str = ""
    options: Optional[List[str]] = None
    minv: float = -1e9
    maxv: float = 1e9


COMMON = [
    Prop("visible", "bool", True, "Visible"),
    Prop("opacity", "float", 1.0, "Opacity", minv=0.0, maxv=1.0),
    Prop("panel", "bool", True, "Background panel"),
    Prop("accent", "color", "", "Accent colour (blank = theme)"),
]


class Widget:
    TYPE = "base"
    NAME = "Widget"
    PROPS: List[Prop] = []
    DEFAULT_SIZE = (300, 200)

    def __init__(self, x=0.0, y=0.0, w=None, h=None, props: Optional[dict] = None, wid: Optional[str] = None):
        self.id = wid or uuid.uuid4().hex[:8]
        self.x, self.y = float(x), float(y)
        self.w = float(w if w is not None else self.DEFAULT_SIZE[0])
        self.h = float(h if h is not None else self.DEFAULT_SIZE[1])
        self.props: Dict[str, Any] = {p.name: p.default for p in self.all_props()}
        if props:
            self.props.update(props)

    @classmethod
    def all_props(cls) -> List[Prop]:
        return COMMON + cls.PROPS

    @property
    def rect(self) -> QRectF:
        return QRectF(self.x, self.y, self.w, self.h)

    def accent(self, theme: Theme) -> QColor:
        c = self.props.get("accent")
        return QColor(c) if c else theme.accent

    def panel(self, p, r, theme, radius=None):
        if self.props.get("panel", True):
            draw_panel(p, r, theme, radius)

    def paint(self, p: QPainter, ctx: RenderContext):
        if not self.props.get("visible", True):
            return
        p.save()
        p.setOpacity(float(self.props.get("opacity", 1.0)))
        try:
            self.draw(p, self.rect, ctx)
        finally:
            p.restore()

    def draw(self, p: QPainter, r: QRectF, ctx: RenderContext):  # pragma: no cover - abstract
        raise NotImplementedError

    def to_json(self) -> dict:
        return dict(type=self.TYPE, id=self.id, x=self.x, y=self.y, w=self.w, h=self.h, props=self.props)


# ------------------------------------------------------------------ speed dial

class SpeedDial(Widget):
    TYPE, NAME = "speed_dial", "Speed dial"
    DEFAULT_SIZE = (330, 330)
    PROPS = [
        Prop("channel", "channel", "@speed", "Channel"),
        Prop("unit", "choice", "km/h", "Unit", ["km/h", "mph", "m/s", "(channel)"]),
        Prop("max", "float", 0.0, "Max (0 = auto)", minv=0),
        Prop("label", "text", "", "Label (blank = unit)"),
        Prop("sweep", "float", 250.0, "Arc sweep (deg)", minv=90, maxv=330),
        Prop("ticks", "bool", True, "Tick labels"),
    ]

    def draw(self, p, r, ctx):
        th = ctx.theme
        pr = self.props
        unit = "" if pr["unit"] == "(channel)" else pr["unit"]
        v = ctx.val(pr["channel"], unit)
        vmax = pr["max"] or nice_ceil(ctx.auto_range(pr["channel"], unit)[1] * 1.05)
        side = min(r.width(), r.height())
        c = r.center()
        sq = QRectF(c.x() - side / 2, c.y() - side / 2, side, side)
        if pr.get("panel", True) and th.show_panels:
            p.save()
            g = QRadialGradient(c, side / 2)
            base = QColor(th.panel)
            g.setColorAt(0.0, QColor(base.red(), base.green(), base.blue(), min(255, base.alpha() + 30)))
            g.setColorAt(0.85, base)
            g.setColorAt(1.0, QColor(base.red(), base.green(), base.blue(), int(base.alpha() * 0.6)))
            p.setBrush(g)
            p.setPen(QPen(th.panel_border, 1.5))
            p.drawEllipse(sq.adjusted(2, 2, -2, -2))
            p.restore()
        sweep = pr["sweep"]
        start = 90 + sweep / 2  # degrees, Qt: 0 = 3 o'clock, CCW positive
        thick = side * 0.075
        arc_r = sq.adjusted(side * 0.1, side * 0.1, -side * 0.1, -side * 0.1)
        frac = 0.0 if not np.isfinite(v) else max(0.0, min(1.0, v / vmax))
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(th.track, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_r, int(start * 16), int(-sweep * 16))
        acc = self.accent(th)
        if frac > 0.002:
            grad = QConicalGradient(c, start)
            grad.setColorAt(0.0, th.accent2)
            grad.setColorAt(min(0.999, sweep / 360.0), acc)
            if th.glow:
                p.setPen(glow_pen(acc, thick * 1.9, 45))
                p.drawArc(arc_r, int(start * 16), int(-sweep * frac * 16))
            pen = QPen(QBrush(grad), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
            # conical gradients go CCW; flip by mirroring the angle list
            grad2 = QConicalGradient(c, start - sweep)
            grad2.setColorAt(0.0, acc)
            grad2.setColorAt(min(0.999, sweep / 360.0), th.accent2)
            pen = QPen(QBrush(grad2), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(arc_r, int(start * 16), int(-sweep * frac * 16))
            # tip marker
            ang = math.radians(start - sweep * frac)
            rad = arc_r.width() / 2
            tip_in = QPointF(c.x() + math.cos(ang) * (rad - thick * 0.9), c.y() - math.sin(ang) * (rad - thick * 0.9))
            tip_out = QPointF(c.x() + math.cos(ang) * (rad + thick * 0.9), c.y() - math.sin(ang) * (rad + thick * 0.9))
            p.setPen(QPen(th.text, side * 0.012, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(tip_in, tip_out)
        # ticks
        n_major = 5 if vmax % 5 == 0 or vmax < 10 else 4
        for i in range(n_major * 5 + 1):
            f = i / (n_major * 5)
            ang = math.radians(start - sweep * f)
            major = i % 5 == 0
            rad = arc_r.width() / 2 - thick * 0.75
            ln = side * (0.045 if major else 0.02)
            a = QPointF(c.x() + math.cos(ang) * rad, c.y() - math.sin(ang) * rad)
            b = QPointF(c.x() + math.cos(ang) * (rad - ln), c.y() - math.sin(ang) * (rad - ln))
            col = th.text if major else th.text_dim
            p.setPen(QPen(col, side * (0.009 if major else 0.005)))
            p.drawLine(a, b)
            if major and pr["ticks"]:
                tr = rad - ln - side * 0.06
                tp = QPointF(c.x() + math.cos(ang) * tr, c.y() - math.sin(ang) * tr)
                val = vmax * f
                txt = f"{val:.0f}" if vmax >= 10 else f"{val:.1f}"
                draw_text(p, QRectF(tp.x() - 40, tp.y() - 20, 80, 40), txt, th, side * 0.062, th.text_dim,
                          glow=False)
        p.restore()
        # value
        big = f"{v:.0f}" if np.isfinite(v) else "--"
        draw_text(p, QRectF(c.x() - side / 2, c.y() - side * 0.2, side, side * 0.36), big, th, side * 0.30, th.text)
        label = pr["label"] or (unit or ctx.unit(pr["channel"])).upper()
        draw_text(p, QRectF(c.x() - side / 2, c.y() + side * 0.17, side, side * 0.1), label, th, side * 0.075,
                  th.text_dim, glow=False)


# ------------------------------------------------------------------ numeric value

class ValueBox(Widget):
    TYPE, NAME = "value", "Value"
    DEFAULT_SIZE = (220, 110)
    PROPS = [
        Prop("channel", "channel", "@speed", "Channel"),
        Prop("label", "text", "SPEED", "Label"),
        Prop("unit", "text", "km/h", "Unit (speed units convert)"),
        Prop("decimals", "int", 0, "Decimals", minv=0, maxv=4),
        Prop("scale", "float", 1.0, "Multiply by"),
        Prop("add", "float", 0.0, "Add"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        v = ctx.val(pr["channel"], pr["unit"] if pr["unit"] in SPEED_UNITS else "")
        v = v * pr["scale"] + pr["add"]
        pad = r.height() * 0.1
        draw_text(p, QRectF(r.left() + pad * 1.4, r.top() + pad, r.width(), r.height() * 0.25), pr["label"].upper(),
                  th, r.height() * 0.2, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, glow=False)
        txt = fmt_num(v, int(pr["decimals"]))
        vr = QRectF(r.left() + pad * 1.4, r.top() + r.height() * 0.35, r.width() - pad * 2.8, r.height() * 0.55)
        uw = 0
        if pr["unit"]:
            uw = text_width(th, r.height() * 0.2, pr["unit"]) + pad
            draw_text(p, QRectF(vr.right() - uw + pad, vr.top(), uw, vr.height() * 0.95), pr["unit"], th,
                      r.height() * 0.2, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                      glow=False)
        draw_text(p, QRectF(vr.left(), vr.top(), vr.width() - uw, vr.height()), txt, th, r.height() * 0.5, th.text,
                  Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


# ------------------------------------------------------------------ bar

class BarGauge(Widget):
    TYPE, NAME = "bar", "Bar"
    DEFAULT_SIZE = (320, 70)
    PROPS = [
        Prop("channel", "channel", "@soc", "Channel"),
        Prop("label", "text", "SOC", "Label"),
        Prop("unit", "text", "%", "Unit suffix"),
        Prop("min", "float", 0.0, "Min"),
        Prop("max", "float", 0.0, "Max (0 = auto)"),
        Prop("decimals", "int", 0, "Decimals", minv=0, maxv=3),
        Prop("scale", "float", 1.0, "Multiply by"),
        Prop("vertical", "bool", False, "Vertical"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        v = ctx.val(pr["channel"]) * pr["scale"]
        lo = pr["min"]
        hi = pr["max"] or nice_ceil(ctx.auto_range(pr["channel"])[1] * pr["scale"])
        f = 0.0 if not np.isfinite(v) or hi == lo else max(0.0, min(1.0, (v - lo) / (hi - lo)))
        acc = self.accent(th)
        pad = min(r.width(), r.height()) * 0.16
        if not pr["vertical"]:
            lab_h = r.height() * 0.32
            draw_text(p, QRectF(r.left() + pad, r.top() + pad * 0.7, r.width() / 2, lab_h), pr["label"].upper(), th,
                      lab_h * 0.8, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, glow=False)
            draw_text(p, QRectF(r.left(), r.top() + pad * 0.7, r.width() - pad, lab_h),
                      fmt_num(v, int(pr["decimals"])) + pr["unit"], th, lab_h * 0.95, th.text,
                      Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bar = QRectF(r.left() + pad, r.bottom() - pad - r.height() * 0.22, r.width() - 2 * pad, r.height() * 0.22)
            fill = QRectF(bar.left(), bar.top(), bar.width() * f, bar.height())
        else:
            lab_h = r.width() * 0.3
            draw_text(p, QRectF(r.left(), r.bottom() - pad - lab_h, r.width(), lab_h), pr["label"].upper(), th,
                      lab_h * 0.7, th.text_dim, glow=False)
            draw_text(p, QRectF(r.left(), r.top() + pad * 0.5, r.width(), lab_h), fmt_num(v, int(pr["decimals"])),
                      th, lab_h * 0.8, th.text)
            bar = QRectF(r.center().x() - r.width() * 0.18, r.top() + pad + lab_h, r.width() * 0.36,
                         r.height() - 2 * pad - 2 * lab_h)
            fill = QRectF(bar.left(), bar.bottom() - bar.height() * f, bar.width(), bar.height() * f)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(th.track)
        rad = min(bar.width(), bar.height()) / 2
        p.drawRoundedRect(bar, rad, rad)
        if f > 0:
            g = QLinearGradient(bar.topLeft(), bar.topRight() if not pr["vertical"] else bar.bottomLeft())
            g.setColorAt(0, lerp_color(acc, QColor(255, 255, 255), 0.25))
            g.setColorAt(1, acc)
            p.setBrush(g)
            p.drawRoundedRect(fill, rad, rad)
        p.restore()


# ------------------------------------------------------------------ pedals

class Pedals(Widget):
    TYPE, NAME = "pedals", "Throttle / brake"
    DEFAULT_SIZE = (120, 280)
    PROPS = [
        Prop("throttle", "channel", "@throttle", "Throttle channel"),
        Prop("brake", "channel", "@brake", "Brake channel"),
        Prop("throttle_max", "float", 0.0, "Throttle max (0 = auto)"),
        Prop("brake_max", "float", 0.0, "Brake max (0 = auto)"),
        Prop("show_values", "bool", True, "Show %"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        pad = r.width() * 0.14
        lab_h = r.width() * 0.2
        items = []
        for key, mkey, col, lab in (("throttle", "throttle_max", th.throttle, "THR"),
                                     ("brake", "brake_max", th.brake, "BRK")):
            ref = pr[key]
            if not ref:
                continue
            v = ctx.val(ref)
            hi = pr[mkey] or max(ctx.auto_range(ref)[1], 1e-6)
            f = 0.0 if not np.isfinite(v) else max(0.0, min(1.0, v / hi))
            items.append((f, col, lab))
        if not items:
            return
        n = len(items)
        colw = (r.width() - pad * (n + 1)) / n
        for i, (f, col, lab) in enumerate(items):
            x = r.left() + pad + i * (colw + pad)
            top = r.top() + pad + (lab_h if pr["show_values"] else 0)
            bar = QRectF(x, top, colw, r.bottom() - pad - lab_h - top)
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(th.track)
            rad = min(colw * 0.3, 8)
            p.drawRoundedRect(bar, rad, rad)
            if f > 0.005:
                fill = QRectF(bar.left(), bar.bottom() - bar.height() * f, bar.width(), bar.height() * f)
                g = QLinearGradient(fill.bottomLeft(), bar.topLeft())
                g.setColorAt(0, QColor(col.red(), col.green(), col.blue(), 150))
                g.setColorAt(1, col)
                if th.glow:
                    p.setBrush(QColor(col.red(), col.green(), col.blue(), 60))
                    p.drawRoundedRect(fill.adjusted(-4, -4, 4, 4), rad + 4, rad + 4)
                p.setBrush(g)
                p.drawRoundedRect(fill, rad, rad)
            p.restore()
            draw_text(p, QRectF(x - pad / 2, bar.bottom() + lab_h * 0.15, colw + pad, lab_h), lab, th, lab_h * 0.75,
                      th.text_dim, glow=False)
            if pr["show_values"]:
                draw_text(p, QRectF(x - pad / 2, r.top() + pad * 0.6, colw + pad, lab_h), f"{f * 100:.0f}", th,
                          lab_h * 0.8, th.text)


# ------------------------------------------------------------------ g circle

class GCircle(Widget):
    TYPE, NAME = "g_circle", "G-force circle"
    DEFAULT_SIZE = (250, 250)
    PROPS = [
        Prop("lat", "channel", "@g_lat", "Lateral G"),
        Prop("long", "channel", "@g_long", "Longitudinal G"),
        Prop("max_g", "float", 2.0, "Scale (g)", minv=0.5, maxv=6),
        Prop("trail", "float", 1.5, "Trail (s)", minv=0, maxv=10),
        Prop("invert_lat", "bool", False, "Mirror left/right"),
        Prop("invert_long", "bool", False, "Mirror up/down"),
        Prop("show_value", "bool", True, "Show total g"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        side = min(r.width(), r.height())
        c = r.center()
        sq = QRectF(c.x() - side / 2, c.y() - side / 2, side, side)
        if pr.get("panel", True) and th.show_panels:
            p.save()
            p.setBrush(th.panel)
            p.setPen(QPen(th.panel_border, 1.5))
            p.drawEllipse(sq.adjusted(1, 1, -1, -1))
            p.restore()
        R = side * 0.42
        mg = pr["max_g"]
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        for g in np.arange(0.5, mg + 1e-6, 0.5):
            rr = R * g / mg
            major = abs(g - round(g)) < 1e-6
            p.setPen(QPen(th.text_dim if major else th.track, side * (0.006 if major else 0.004),
                          Qt.PenStyle.SolidLine if major else Qt.PenStyle.DashLine))
            p.drawEllipse(c, rr, rr)
        p.setPen(QPen(th.track, side * 0.004))
        p.drawLine(QPointF(c.x() - R, c.y()), QPointF(c.x() + R, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - R), QPointF(c.x(), c.y() + R))
        sx = -1 if pr["invert_lat"] else 1
        sy = 1 if pr["invert_long"] else -1

        def pt(gl, go):
            return QPointF(c.x() - sx * gl / mg * R, c.y() + sy * go / mg * R)

        acc = self.accent(th)
        if pr["trail"] > 0:
            ts = ctx.t - np.linspace(pr["trail"], 0, 24)
            gl = np.clip(ctx.series(pr["lat"], ts), -mg * 1.1, mg * 1.1)
            go = np.clip(ctx.series(pr["long"], ts), -mg * 1.1, mg * 1.1)
            for i in range(1, len(ts)):
                if np.isfinite(gl[i - 1]) and np.isfinite(gl[i]):
                    a = i / len(ts)
                    col = QColor(acc)
                    col.setAlpha(int(200 * a))
                    p.setPen(QPen(col, side * 0.018 * (0.4 + 0.6 * a), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(pt(gl[i - 1], go[i - 1]), pt(gl[i], go[i]))
        gl, go = ctx.val(pr["lat"]), ctx.val(pr["long"])
        if np.isfinite(gl) and np.isfinite(go):
            gl_c, go_c = np.clip(gl, -mg * 1.1, mg * 1.1), np.clip(go, -mg * 1.1, mg * 1.1)
            q = pt(gl_c, go_c)
            p.setPen(Qt.PenStyle.NoPen)
            if th.glow:
                p.setBrush(QColor(acc.red(), acc.green(), acc.blue(), 70))
                p.drawEllipse(q, side * 0.06, side * 0.06)
            p.setBrush(acc)
            p.drawEllipse(q, side * 0.035, side * 0.035)
            p.setBrush(th.text)
            p.drawEllipse(q, side * 0.013, side * 0.013)
        p.restore()
        # labels
        draw_text(p, QRectF(c.x() + side * 0.02, c.y() - R - side * 0.005, R, side * 0.08), f"{mg:g}g", th, side * 0.065,
                  th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, glow=False)
        if pr["show_value"] and np.isfinite(gl) and np.isfinite(go):
            draw_text(p, QRectF(c.x() - side / 2, c.y() + R * 0.55, side, side * 0.12), f"{math.hypot(gl, go):.2f} g",
                      th, side * 0.08, th.text)


# ------------------------------------------------------------------ track map

class TrackMap(Widget):
    TYPE, NAME = "track_map", "Track map"
    DEFAULT_SIZE = (360, 360)
    PROPS = [
        Prop("rotation", "float", 0.0, "Rotation (deg)", minv=-360, maxv=360),
        Prop("line", "float", 7.0, "Line width", minv=1, maxv=30),
        Prop("show_sf", "bool", True, "Start/finish marker"),
        Prop("speed_colour", "bool", True, "Colour trail by speed"),
        Prop("trail", "float", 6.0, "Trail (s)", minv=0, maxv=60),
    ]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._cache_key = None
        self._geom = None

    def _geometry(self, ctx, r):
        laps = ctx.laps
        key = (id(laps), getattr(laps, "ref_lap", None), len(getattr(laps, "t", [])), r.width(), r.height(),
               self.props["rotation"])
        if key == self._cache_key and self._geom is not None:
            return self._geom
        x, y = laps.track_xy()
        if len(x) < 5:
            self._geom = None
            return None
        ang = math.radians(self.props["rotation"])
        ca, sa = math.cos(ang), math.sin(ang)
        rx, ry = x * ca - y * sa, x * sa + y * ca
        cx, cy = (rx.min() + rx.max()) / 2, (ry.min() + ry.max()) / 2
        span = max(rx.max() - rx.min(), ry.max() - ry.min(), 1.0)
        pad = min(r.width(), r.height()) * 0.12
        scale = (min(r.width(), r.height()) - 2 * pad) / span
        # keep aspect but use full box
        sx_span = (rx.max() - rx.min()) * scale
        sy_span = (ry.max() - ry.min()) * scale
        scale2 = min((r.width() - 2 * pad) / max(sx_span / scale, 1e-6), (r.height() - 2 * pad) / max(sy_span / scale, 1e-6))
        step = max(1, len(rx) // 1200)
        poly = QPolygonF([QPointF((a - cx) * scale2, -(b - cy) * scale2) for a, b in zip(rx[::step], ry[::step])])
        self._geom = dict(poly=poly, cx=cx, cy=cy, scale=scale2, ca=ca, sa=sa)
        self._cache_key = key
        return self._geom

    def _map(self, g, x, y, c):
        rx, ry = x * g["ca"] - y * g["sa"], x * g["sa"] + y * g["ca"]
        return QPointF(c.x() + (rx - g["cx"]) * g["scale"], c.y() - (ry - g["cy"]) * g["scale"])

    def _draw_static(self, p, r, g, c, lw, th, laps):
        p.save()
        p.translate(c)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 110), lw + 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(g["poly"])
        p.setPen(QPen(QColor(255, 255, 255, 70) if not th.glow else th.track, lw, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(g["poly"])
        p.setPen(QPen(QColor(255, 255, 255, 170) if not th.glow else th.accent, max(1.0, lw * 0.22),
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(g["poly"])
        p.restore()
        if self.props["show_sf"] and laps.sf_point is not None and len(laps.x) > 3:
            sx, sy = laps.sf_point
            q = self._map(g, sx, sy, c)
            i = int(np.argmin((laps.x - sx) ** 2 + (laps.y - sy) ** 2))
            j = min(i + 3, len(laps.x) - 1)
            d = self._map(g, laps.x[j], laps.y[j], c) - q
            n = math.hypot(d.x(), d.y()) or 1
            nx, ny = -d.y() / n, d.x() / n
            L = lw * 1.8
            p.save()
            p.setPen(QPen(QColor(255, 255, 255), lw * 0.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawLine(QPointF(q.x() - nx * L, q.y() - ny * L), QPointF(q.x() + nx * L, q.y() + ny * L))
            p.setPen(QPen(QColor(0, 0, 0), lw * 0.9, Qt.PenStyle.DotLine, Qt.PenCapStyle.FlatCap))
            p.drawLine(QPointF(q.x() - nx * L, q.y() - ny * L), QPointF(q.x() + nx * L, q.y() + ny * L))
            p.restore()

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        laps = ctx.laps
        if laps is None or len(getattr(laps, "t", [])) < 5:
            draw_text(p, r, "NO GPS", th, min(r.width(), r.height()) * 0.1, th.text_dim)
            return
        g = self._geometry(ctx, r)
        if g is None:
            return
        c = r.center()
        lw = pr["line"]
        # static layer (outline + start/finish) cached as an image at device resolution
        scale = p.transform().m11() or 1.0
        skey = (self._cache_key, scale, th.name, bool(th.glow), pr["show_sf"], lw)
        if getattr(self, "_static_key", None) != skey:
            W, H = max(1, int(r.width() * scale)), max(1, int(r.height() * scale))
            img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0)
            q = QPainter(img)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.scale(scale, scale)
            q.translate(-r.left(), -r.top())
            self._draw_static(q, r, g, c, lw, th, laps)
            q.end()
            self._static_img, self._static_key = img, skey
        p.drawImage(r, self._static_img)
        acc = self.accent(th)
        src = ctx.session.source(laps.source_id) if laps.source_id else None
        if src is None:
            return
        ts = ctx.t + src.offset
        # trail coloured by speed
        if pr["trail"] > 0:
            tt = np.linspace(ts - pr["trail"], ts, 40)
            xx = np.interp(tt, laps.t, laps.x)
            yy = np.interp(tt, laps.t, laps.y)
            valid = (tt >= laps.t[0]) & (tt <= laps.t[-1])
            spd = None
            if pr["speed_colour"] and ctx.session.roles.get("speed"):
                spd = ctx.series("@speed", tt - src.offset)
                lo, hi = ctx.auto_range("@speed")
            p.save()
            for i in range(1, len(tt)):
                if not (valid[i] and valid[i - 1]):
                    continue
                a = i / len(tt)
                if spd is not None and np.isfinite(spd[i]):
                    f = (spd[i] - lo) / max(hi - lo, 1e-6)
                    col = lerp_color(th.bad, th.good, f) if not th.glow else lerp_color(th.accent2, acc, f)
                else:
                    col = QColor(acc)
                col.setAlpha(int(255 * a))
                p.setPen(QPen(col, lw * 0.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(self._map(g, xx[i - 1], yy[i - 1], c), self._map(g, xx[i], yy[i], c))
            p.restore()
        if laps.t[0] <= ts <= laps.t[-1]:
            q = self._map(g, float(np.interp(ts, laps.t, laps.x)), float(np.interp(ts, laps.t, laps.y)), c)
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(acc.red(), acc.green(), acc.blue(), 80))
            p.drawEllipse(q, lw * 2.2, lw * 2.2)
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(q, lw * 1.35, lw * 1.35)
            p.setBrush(acc)
            p.drawEllipse(q, lw * 0.95, lw * 0.95)
            p.restore()


# ------------------------------------------------------------------ lap timer

class LapTimer(Widget):
    TYPE, NAME = "lap_timer", "Lap timer"
    DEFAULT_SIZE = (430, 190)
    PROPS = [
        Prop("title", "text", "LAP", "Title"),
        Prop("show_total", "bool", True, "Show lap count (n / total)"),
        Prop("decimals", "int", 2, "Decimals", minv=0, maxv=3),
        Prop("best_so_far", "bool", True, "Best = best so far (no spoilers)"),
        Prop("flash", "float", 6.0, "Show finished lap for (s)", minv=0, maxv=30),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        laps = ctx.laps
        st = laps.state(ctx.t, pr["best_so_far"]) if laps is not None else {"valid": False}
        H = r.height()
        pad = H * 0.09
        dec = int(pr["decimals"])
        acc = self.accent(th)
        # header strip
        head = QRectF(r.left(), r.top(), r.width(), H * 0.26)
        if st.get("valid") and st["lap_number"] is not None and st["lap_number"] > 0:
            lap_txt = f"{pr['title']} {st['lap_number']}"
            if pr["show_total"] and st["total_laps"]:
                lap_txt += f" / {st['total_laps']}"
        elif st.get("valid"):
            lap_txt = "OUT LAP"
        else:
            lap_txt = pr["title"]
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(acc)
        p.drawRect(QRectF(r.left() + pad, head.top() + pad * 0.9, H * 0.03, head.height() - pad * 0.8))
        p.restore()
        draw_text(p, QRectF(r.left() + pad * 1.9, head.top() + pad * 0.5, r.width(), head.height()), lap_txt, th,
                  H * 0.15, th.text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # main time or flashing finished lap
        main = QRectF(r.left() + pad * 1.9, r.top() + H * 0.27, r.width() - pad * 3.8, H * 0.4)
        flash = st.get("since_finish") is not None and st["since_finish"] < pr["flash"] and st.get("last") is not None
        if flash:
            is_best = st.get("best") is not None and abs(st["last"] - st["best"]) < 1e-6
            col = th.best if is_best else th.text
            draw_text(p, main, fmt_laptime(st["last"], dec), th, H * 0.34, col,
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            ld = st.get("last_delta")
            if ld is not None:
                dcol = th.good if ld < 0 else th.bad
                draw_text(p, main, f"{ld:+.{dec}f}", th, H * 0.17, dcol,
                          Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            draw_text(p, main, fmt_laptime(st.get("elapsed"), dec) if st.get("valid") else fmt_laptime(None, dec), th,
                      H * 0.34, th.text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # last / best row
        row = QRectF(r.left() + pad * 1.9, r.bottom() - H * 0.28, r.width() - pad * 3.8, H * 0.22)
        half = row.width() / 2
        for i, (lab, val, col) in enumerate((("LAST", st.get("last"), th.text), ("BEST", st.get("best"), th.best))):
            rr = QRectF(row.left() + i * half, row.top(), half, row.height())
            lw = text_width(th, H * 0.11, lab) + H * 0.05
            draw_text(p, rr, lab, th, H * 0.11, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                      glow=False)
            draw_text(p, QRectF(rr.left() + lw, rr.top(), half - lw, rr.height()), fmt_laptime(val, dec), th, H * 0.14,
                      col if val is not None else th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


# ------------------------------------------------------------------ delta bar

class DeltaBar(Widget):
    TYPE, NAME = "delta", "Delta to best"
    DEFAULT_SIZE = (430, 70)
    PROPS = [
        Prop("range", "float", 2.0, "Bar range (± s)", minv=0.1, maxv=30),
        Prop("label", "text", "Δ BEST", "Label"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        st = ctx.laps.state(ctx.t, True) if ctx.laps is not None else {}
        d = st.get("delta")
        H = r.height()
        pad = H * 0.16
        lab_w = r.width() * 0.26
        draw_text(p, QRectF(r.left() + pad, r.top(), lab_w, H), pr["label"], th, H * 0.3, th.text_dim,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, glow=False)
        num_w = r.width() * 0.26
        col = th.text_dim if d is None else (th.good if d < 0 else th.bad)
        draw_text(p, QRectF(r.right() - pad - num_w, r.top(), num_w, H), "--.--" if d is None else f"{d:+.2f}", th,
                  H * 0.46, col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar = QRectF(r.left() + pad + lab_w * 0.75, r.center().y() - H * 0.12, r.width() - 2 * pad - lab_w * 0.75 - num_w - pad,
                     H * 0.24)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(th.track)
        p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
        cx = bar.center().x()
        if d is not None:
            f = max(-1.0, min(1.0, d / pr["range"]))
            w = bar.width() / 2 * abs(f)
            fr = QRectF(cx, bar.top(), w, bar.height()) if f > 0 else QRectF(cx - w, bar.top(), w, bar.height())
            p.setBrush(col)
            p.drawRoundedRect(fr, bar.height() / 2, bar.height() / 2)
        p.setBrush(th.text)
        p.drawRect(QRectF(cx - 1.5, bar.top() - H * 0.1, 3, bar.height() + H * 0.2))
        p.restore()


# ------------------------------------------------------------------ rolling graph

class Graph(Widget):
    TYPE, NAME = "graph", "Rolling graph"
    DEFAULT_SIZE = (560, 160)
    PROPS = [
        Prop("ch1", "channel", "@speed", "Channel 1"),
        Prop("ch2", "channel", "@throttle", "Channel 2"),
        Prop("ch3", "channel", "@brake", "Channel 3"),
        Prop("window", "float", 10.0, "Window (s)", minv=1, maxv=120),
        Prop("color1", "color", "", "Colour 1 (blank = accent)"),
        Prop("color2", "color", "", "Colour 2 (blank = throttle)"),
        Prop("color3", "color", "", "Colour 3 (blank = brake)"),
        Prop("label", "text", "", "Label"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        pad = min(r.width(), r.height()) * 0.1
        area = r.adjusted(pad, pad, -pad, -pad)
        if pr["label"]:
            draw_text(p, QRectF(area.left(), area.top(), area.width(), r.height() * 0.15), pr["label"].upper(), th,
                      r.height() * 0.11, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, glow=False)
        p.save()
        p.setPen(QPen(th.track, 1))
        for k in range(1, 4):
            y = area.top() + area.height() * k / 4
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        n = 160
        ts = ctx.t - np.linspace(pr["window"], 0, n)
        defaults = [self.accent(th), th.throttle, th.brake]
        for i in range(3):
            ref = pr[f"ch{i + 1}"]
            if not ref:
                continue
            v = ctx.series(ref, ts)
            lo, hi = ctx.auto_range(ref)
            if hi <= lo:
                hi = lo + 1
            f = np.clip((v - lo) / (hi - lo), 0, 1)
            col = QColor(pr[f"color{i + 1}"]) if pr[f"color{i + 1}"] else defaults[i]
            path = QPainterPath()
            started = False
            for k in range(n):
                if not np.isfinite(f[k]):
                    started = False
                    continue
                pt = QPointF(area.left() + area.width() * k / (n - 1), area.bottom() - area.height() * f[k])
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
            if th.glow:
                p.setPen(glow_pen(col, 7, 50))
                p.drawPath(path)
            p.setPen(QPen(col, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        p.restore()


# ------------------------------------------------------------------ steering

class Steering(Widget):
    TYPE, NAME = "steering", "Steering wheel"
    DEFAULT_SIZE = (200, 200)
    PROPS = [
        Prop("channel", "channel", "@steering", "Steering channel"),
        Prop("scale", "float", 1.0, "Degrees per unit", minv=-100, maxv=100),
        Prop("show_value", "bool", True, "Show angle"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        side = min(r.width(), r.height())
        c = r.center()
        if pr.get("panel", True) and th.show_panels:
            p.save()
            p.setBrush(th.panel)
            p.setPen(QPen(th.panel_border, 1.5))
            p.drawEllipse(c, side / 2 - 1, side / 2 - 1)
            p.restore()
        v = ctx.val(pr["channel"])
        ang = (v if np.isfinite(v) else 0.0) * pr["scale"]
        acc = self.accent(th)
        p.save()
        p.translate(c.x(), c.y() - side * 0.04)
        p.rotate(ang)
        R = side * 0.31
        p.setBrush(Qt.BrushStyle.NoBrush)
        rim_w = side * 0.055
        if th.glow:
            p.setPen(glow_pen(th.text, rim_w * 2.2, 40))
            p.drawEllipse(QPointF(0, 0), R, R)
        p.setPen(QPen(th.text, rim_w))
        p.drawEllipse(QPointF(0, 0), R, R)
        p.setPen(QPen(th.text, side * 0.04, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        hub = R * 0.24
        for a_deg in (180, 0, 90):  # left, right, bottom spokes (y down)
            a_r = math.radians(a_deg)
            p.drawLine(QPointF(math.cos(a_r) * hub, math.sin(a_r) * hub),
                       QPointF(math.cos(a_r) * (R - rim_w * 0.4), math.sin(a_r) * (R - rim_w * 0.4)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(th.text)
        p.drawEllipse(QPointF(0, 0), hub, hub)
        p.setBrush(acc)
        p.drawRoundedRect(QRectF(-rim_w * 0.55, -R - rim_w * 0.6, rim_w * 1.1, rim_w * 1.2), 2, 2)
        p.restore()
        if pr["show_value"]:
            draw_text(p, QRectF(c.x() - side / 2, c.y() + side * 0.33, side, side * 0.12),
                      (f"{round(ang) + 0:+d}°" if round(ang) != 0 else "0°") if np.isfinite(v) else "--", th, side * 0.1,
                      th.text)


# ------------------------------------------------------------------ static

class TextLabel(Widget):
    TYPE, NAME = "text", "Text"
    DEFAULT_SIZE = (400, 70)
    PROPS = [
        Prop("text", "text", "DRIVER NAME", "Text"),
        Prop("size", "float", 0.6, "Size (fraction of height)", minv=0.1, maxv=1),
        Prop("align", "choice", "left", "Align", ["left", "center", "right"]),
        Prop("color", "color", "", "Colour (blank = theme)"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        al = {"left": Qt.AlignmentFlag.AlignLeft, "center": Qt.AlignmentFlag.AlignHCenter,
              "right": Qt.AlignmentFlag.AlignRight}[pr["align"]] | Qt.AlignmentFlag.AlignVCenter
        pad = r.height() * 0.25
        draw_text(p, r.adjusted(pad, 0, -pad, 0), pr["text"], th, r.height() * pr["size"],
                  QColor(pr["color"]) if pr["color"] else th.text, al, tabular=False)


class ImageLayer(Widget):
    TYPE, NAME = "image", "Image / logo"
    DEFAULT_SIZE = (200, 120)
    PROPS = [Prop("path", "file", "", "Image file")]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._img = None
        self._path = None

    def draw(self, p, r, ctx):
        path = self.props["path"]
        if path != self._path:
            self._path = path
            self._img = QImage(path) if path and os.path.exists(path) else None
        if self._img is None or self._img.isNull():
            if ctx.theme and not path:
                draw_panel(p, r, ctx.theme, force=True)
                draw_text(p, r, "IMAGE", ctx.theme, r.height() * 0.25, ctx.theme.text_dim)
            return
        iw, ih = self._img.width(), self._img.height()
        s = min(r.width() / iw, r.height() / ih)
        tr = QRectF(r.center().x() - iw * s / 2, r.center().y() - ih * s / 2, iw * s, ih * s)
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(tr, self._img)
        p.restore()


WIDGET_TYPES = {cls.TYPE: cls for cls in
                (SpeedDial, ValueBox, BarGauge, Pedals, GCircle, TrackMap, LapTimer, DeltaBar, Graph, Steering,
                 TextLabel, ImageLayer)}


def widget_from_json(d: dict) -> Optional[Widget]:
    cls = WIDGET_TYPES.get(d.get("type"))
    if cls is None:
        return None
    return cls(d.get("x", 0), d.get("y", 0), d.get("w"), d.get("h"), d.get("props"), d.get("id"))
