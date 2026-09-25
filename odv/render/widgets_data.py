# SPDX-License-Identifier: GPL-3.0-or-later
"""Generic data widgets that work with any channel: dial, numeric, bar and live plot."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QLinearGradient, QPainterPath, QPen, QRadialGradient

from .base import RANGE, SPEED_UNITS, VALUE, Prop, ValueWidget, fmt_num, nice_range, nice_ticks
from .theme import draw_text, glow_pen, lerp_color, text_width


def _tick_fmt(step: float) -> int:
    if step <= 0 or not np.isfinite(step):
        return 0
    return max(0, min(4, -int(math.floor(math.log10(step))) + (0 if step >= 1 else 0)))


def _frac(v, lo, hi):
    if not np.isfinite(v) or hi == lo:
        return float("nan")
    return (v - lo) / (hi - lo)


# ====================================================================== dial

class DialGauge(ValueWidget):
    TYPE, NAME = "dial", "Dial gauge"
    DEFAULT_SIZE = (300, 300)
    PROPS = VALUE + RANGE + [
        Prop("style", "choice", "arc", "Style", ["arc", "needle"], group="Dial"),
        Prop("sweep", "float", 250.0, "Arc sweep (deg)", minv=60, maxv=340, group="Dial", decimals=0),
        Prop("divisions", "int", 5, "Major divisions", minv=1, maxv=20, group="Dial"),
        Prop("ticks", "bool", True, "Tick labels", group="Dial"),
        Prop("show_label", "bool", True, "Show label", group="Dial"),
    ]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        v = self.value(ctx)
        lo, hi = self.value_range(ctx)
        if pr.get("range_mode", "auto") == "auto":
            lo, hi, nd_auto = nice_ticks(lo, hi, int(pr["divisions"]))
        else:
            nd_auto = int(pr["divisions"])
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
        sweep = float(pr["sweep"])
        start = 90 + sweep / 2
        thick = side * 0.065
        arc_r = sq.adjusted(side * 0.1, side * 0.1, -side * 0.1, -side * 0.1)
        rad = arc_r.width() / 2
        state = self.limit_state(v)
        acc = self.limit_color(th) if state else self.accent(th)
        f = _frac(v, lo, hi)
        fz = min(1.0, max(0.0, _frac(0.0, lo, hi))) if lo < 0 < hi else 0.0

        def ang_of(fr):
            return start - sweep * fr

        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(th.track, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_r, int(start * 16), int(-sweep * 16))
        # limit zones
        if pr.get("use_limits"):
            lc = self.limit_color(th)
            zc = QColor(lc)
            zc.setAlpha(170)
            p.setPen(QPen(zc, thick * 0.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            outer = arc_r.adjusted(-thick * 0.9, -thick * 0.9, thick * 0.9, thick * 0.9)
            fl = _frac(float(pr["limit_low"]), lo, hi)
            fh = _frac(float(pr["limit_high"]), lo, hi)
            if np.isfinite(fl) and fl > 0:
                p.drawArc(outer, int(start * 16), int(-sweep * min(fl, 1) * 16))
            if np.isfinite(fh) and fh < 1:
                a0 = ang_of(max(fh, 0))
                p.drawArc(outer, int(a0 * 16), int(-sweep * (1 - max(fh, 0)) * 16))
        if pr["style"] == "arc" and np.isfinite(f):
            fc = max(0.0, min(1.0, f))
            a0, a1 = (fz, fc) if fc >= fz else (fc, fz)
            if a1 - a0 > 0.002:
                if th.glow:
                    p.setPen(glow_pen(acc, thick * 1.9, 45))
                    p.drawArc(arc_r, int(ang_of(a0) * 16), int(-sweep * (a1 - a0) * 16))
                grad = QConicalGradient(c, start - sweep)
                grad.setColorAt(0.0, acc)
                grad.setColorAt(min(0.999, sweep / 360.0), th.accent2 if not state else acc)
                p.setPen(QPen(QBrush(grad), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawArc(arc_r, int(ang_of(a0) * 16), int(-sweep * (a1 - a0) * 16))
        # ticks
        nd = max(1, nd_auto)
        step = (hi - lo) / nd
        dec = _tick_fmt(abs(step))
        for i in range(nd * 5 + 1):
            fr = i / (nd * 5)
            a = math.radians(ang_of(fr))
            major = i % 5 == 0
            rr = rad - thick * 0.75
            ln = side * (0.045 if major else 0.02)
            pa = QPointF(c.x() + math.cos(a) * rr, c.y() - math.sin(a) * rr)
            pb = QPointF(c.x() + math.cos(a) * (rr - ln), c.y() - math.sin(a) * (rr - ln))
            p.setPen(QPen(th.text if major else th.text_dim, side * (0.009 if major else 0.005)))
            p.drawLine(pa, pb)
            if major and pr["ticks"]:
                tr = rr - ln - side * 0.065
                tp = QPointF(c.x() + math.cos(a) * tr, c.y() - math.sin(a) * tr)
                draw_text(p, QRectF(tp.x() - 50, tp.y() - 20, 100, 40), f"{lo + step * (i // 5):.{dec}f}", th,
                          side * (0.05 if abs(hi) >= 1000 or abs(lo) >= 1000 else 0.058), th.text_dim, glow=False)
        # needle
        if pr["style"] == "needle" and np.isfinite(f):
            fc = max(-0.02, min(1.02, f))
            a = math.radians(ang_of(fc))
            tip = QPointF(c.x() + math.cos(a) * (rad + thick * 0.3), c.y() - math.sin(a) * (rad + thick * 0.3))
            back = QPointF(c.x() - math.cos(a) * side * 0.06, c.y() + math.sin(a) * side * 0.06)
            if th.glow:
                p.setPen(glow_pen(acc, side * 0.05, 50))
                p.drawLine(back, tip)
            p.setPen(QPen(acc, side * 0.022, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(back, tip)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(th.text)
            p.drawEllipse(c, side * 0.04, side * 0.04)
        elif np.isfinite(f):
            fc = max(0.0, min(1.0, f))
            a = math.radians(ang_of(fc))
            p.setPen(QPen(th.text, side * 0.012, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(c.x() + math.cos(a) * (rad - thick * 0.9), c.y() - math.sin(a) * (rad - thick * 0.9)),
                       QPointF(c.x() + math.cos(a) * (rad + thick * 0.9), c.y() - math.sin(a) * (rad + thick * 0.9)))
        p.restore()
        vcol = self.limit_color(th) if state else th.text
        needle = pr["style"] == "needle"
        vtxt = fmt_num(v, pr["decimals"])
        vsize = side * (0.15 if needle else 0.21)
        maxw = side * (0.46 if needle else 0.3)
        tw = text_width(th, vsize, vtxt)
        if tw > maxw:
            vsize *= maxw / tw
        if needle:
            vr = QRectF(c.x() - side / 2, c.y() + side * 0.08, side, side * 0.18)
        else:
            vr = QRectF(c.x() - side / 2, c.y() - side * 0.16, side, side * 0.3)
        draw_text(p, vr, vtxt, th, vsize, vcol)
        unit = self.unit_text(ctx)
        if unit:
            draw_text(p, QRectF(c.x() - side / 2, vr.bottom() - side * 0.01, side, side * 0.08),
                      unit.upper() if len(unit) < 6 else unit, th, side * 0.062, th.text_dim, glow=False)
        if pr.get("show_label", True):
            draw_text(p, QRectF(c.x() - side / 2, c.y() + side * 0.345, side, side * 0.08),
                      self.label_text(ctx).upper(), th, side * 0.058, th.text_dim, glow=False, tabular=False)


# ====================================================================== numeric

class ValueBox(ValueWidget):
    TYPE, NAME = "value", "Numeric value"
    DEFAULT_SIZE = (240, 110)
    PROPS = VALUE + RANGE[3:]  # limits only, no range needed
    DEFAULT_PROPS = {"channel": "@speed", "label": "SPEED", "unit": "km/h"}

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        v = self.value(ctx)
        state = self.limit_state(v)
        pad = r.height() * 0.1
        if state:
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.limit_color(th))
            p.drawRoundedRect(QRectF(r.left() + pad * 0.5, r.top() + pad, r.height() * 0.05, r.height() - 2 * pad), 2, 2)
            p.restore()
        draw_text(p, QRectF(r.left() + pad * 1.4, r.top() + pad, r.width() - pad * 2.8, r.height() * 0.25),
                  self.label_text(ctx).upper(), th, r.height() * 0.2, th.text_dim,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, glow=False, tabular=False)
        txt = fmt_num(v, pr["decimals"])
        vr = QRectF(r.left() + pad * 1.4, r.top() + r.height() * 0.35, r.width() - pad * 2.8, r.height() * 0.55)
        unit = self.unit_text(ctx)
        uw = 0
        if unit:
            uw = text_width(th, r.height() * 0.2, unit) + pad
            draw_text(p, QRectF(vr.right() - uw + pad, vr.top(), uw, vr.height() * 0.95), unit, th,
                      r.height() * 0.2, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                      glow=False)
        draw_text(p, QRectF(vr.left(), vr.top(), vr.width() - uw, vr.height()), txt, th, r.height() * 0.5,
                  self.limit_color(th) if state else th.text, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


# ====================================================================== bar

class BarGauge(ValueWidget):
    TYPE, NAME = "bar", "Bar"
    DEFAULT_SIZE = (320, 80)
    PROPS = VALUE + RANGE + [
        Prop("vertical", "bool", False, "Vertical", group="Bar"),
        Prop("show_scale", "bool", False, "Show min / max", group="Bar"),
        Prop("show_value", "bool", True, "Show value", group="Bar"),
    ]
    DEFAULT_PROPS = {"channel": "@soc", "label": "SOC", "unit": "%"}

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        v = self.value(ctx)
        lo, hi = self.value_range(ctx)
        f = _frac(v, lo, hi)
        fz = min(1.0, max(0.0, _frac(0.0, lo, hi))) if lo < 0 < hi else 0.0
        fc = 0.0 if not np.isfinite(f) else max(0.0, min(1.0, f))
        a0, a1 = (fz, fc) if fc >= fz else (fc, fz)
        state = self.limit_state(v)
        acc = self.limit_color(th) if state else self.accent(th)
        pad = min(r.width(), r.height()) * 0.16
        unit = pr.get("unit", "") or ""
        vtxt = fmt_num(v, pr["decimals"]) + (unit if len(unit) <= 2 else " " + unit)
        vcol = self.limit_color(th) if state else th.text
        if not pr["vertical"]:
            lab_h = r.height() * 0.32
            draw_text(p, QRectF(r.left() + pad, r.top() + pad * 0.7, r.width() / 2, lab_h), self.label_text(ctx).upper(),
                      th, lab_h * 0.8, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                      glow=False, tabular=False)
            if pr["show_value"]:
                draw_text(p, QRectF(r.left(), r.top() + pad * 0.7, r.width() - pad, lab_h), vtxt, th, lab_h * 0.95,
                          vcol, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bh = r.height() * 0.22
            bar = QRectF(r.left() + pad, r.bottom() - pad - bh - (lab_h * 0.5 if pr["show_scale"] else 0),
                         r.width() - 2 * pad, bh)
            fill = QRectF(bar.left() + bar.width() * a0, bar.top(), bar.width() * (a1 - a0), bar.height())
            pos = lambda fr: QPointF(bar.left() + bar.width() * fr, bar.center().y())  # noqa: E731
        else:
            lab_h = r.width() * 0.26
            draw_text(p, QRectF(r.left(), r.bottom() - pad - lab_h, r.width(), lab_h), self.label_text(ctx).upper(), th,
                      lab_h * 0.6, th.text_dim, glow=False, tabular=False)
            if pr["show_value"]:
                draw_text(p, QRectF(r.left(), r.top() + pad * 0.5, r.width(), lab_h), fmt_num(v, pr["decimals"]), th,
                          lab_h * 0.8, vcol)
            bw = r.width() * 0.36
            bar = QRectF(r.center().x() - bw / 2, r.top() + pad + lab_h, bw, r.height() - 2 * pad - 2 * lab_h)
            fill = QRectF(bar.left(), bar.bottom() - bar.height() * a1, bar.width(), bar.height() * (a1 - a0))
            pos = lambda fr: QPointF(bar.center().x(), bar.bottom() - bar.height() * fr)  # noqa: E731
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(th.track)
        rad = min(bar.width(), bar.height()) / 2
        p.drawRoundedRect(bar, rad, rad)
        if a1 - a0 > 0.001:
            g = QLinearGradient(bar.topLeft(), bar.topRight() if not pr["vertical"] else bar.bottomLeft())
            g.setColorAt(0, lerp_color(acc, QColor(255, 255, 255), 0.25))
            g.setColorAt(1, acc)
            p.setBrush(g)
            p.drawRoundedRect(fill, rad, rad)
        # zero line and limit markers
        marks = []
        if lo < 0 < hi:
            marks.append((fz, th.text))
        if pr.get("use_limits"):
            for key in ("limit_low", "limit_high"):
                fr = _frac(float(pr[key]), lo, hi)
                if np.isfinite(fr) and 0 <= fr <= 1:
                    marks.append((fr, self.limit_color(th)))
        for fr, col in marks:
            q = pos(fr)
            p.setPen(QPen(col, max(2.0, rad * 0.35)))
            if not pr["vertical"]:
                p.drawLine(QPointF(q.x(), bar.top() - rad * 0.6), QPointF(q.x(), bar.bottom() + rad * 0.6))
            else:
                p.drawLine(QPointF(bar.left() - rad * 0.6, q.y()), QPointF(bar.right() + rad * 0.6, q.y()))
        p.restore()
        if pr["show_scale"]:
            dec = _tick_fmt(abs(hi - lo) / 5)
            if not pr["vertical"]:
                sr = QRectF(bar.left(), bar.bottom() + 2, bar.width(), lab_h * 0.5)
                draw_text(p, sr, f"{lo:.{dec}f}", th, lab_h * 0.45, th.text_dim,
                          Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, glow=False)
                draw_text(p, sr, f"{hi:.{dec}f}", th, lab_h * 0.45, th.text_dim,
                          Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, glow=False)
            else:
                draw_text(p, QRectF(bar.right() + 4, bar.top() - 10, r.right() - bar.right(), 20), f"{hi:.{dec}f}", th,
                          lab_h * 0.4, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, glow=False)
                draw_text(p, QRectF(bar.right() + 4, bar.bottom() - 10, r.right() - bar.right(), 20), f"{lo:.{dec}f}",
                          th, lab_h * 0.4, th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                          glow=False)


# ====================================================================== live plot

def _channel_props(i: int):
    g = f"Channel {i}"
    return [
        Prop(f"ch{i}", "channel", "", "Channel", group=g),
        Prop(f"label{i}", "text", "", "Name", group=g),
        Prop(f"color{i}", "color", "", "Colour", group=g),
        Prop(f"scale{i}", "float", 1.0, "Scale", group=g, decimals=6),
        Prop(f"add{i}", "float", 0.0, "Offset", group=g, decimals=4),
    ]


class Graph(ValueWidget):
    TYPE, NAME = "graph", "Live plot"
    DEFAULT_SIZE = (560, 200)
    N_CH = 4
    PROPS = [
        Prop("label", "text", "", "Title", group="Plot"),
        Prop("window", "float", 10.0, "Time window (s)", minv=0.5, maxv=600, group="Plot", decimals=1),
        Prop("now_position", "choice", "right", "'Now' position", ["right", "center"], group="Plot"),
        Prop("y_mode", "choice", "auto per channel", "Y axis",
             ["auto per channel", "shared auto", "fixed"], group="Plot"),
        Prop("ymin", "float", 0.0, "Y min (fixed)", group="Plot", decimals=4),
        Prop("ymax", "float", 100.0, "Y max (fixed)", group="Plot", decimals=4),
        Prop("grid", "bool", True, "Grid", group="Plot"),
        Prop("axis", "bool", False, "Y axis labels", group="Plot"),
        Prop("legend", "bool", True, "Legend with live values", group="Plot"),
        Prop("decimals", "int", 1, "Legend decimals", minv=0, maxv=6, group="Plot"),
        Prop("line_width", "float", 3.0, "Line width", minv=0.5, maxv=12, group="Plot", decimals=1),
        Prop("fill", "bool", False, "Fill under first channel", group="Plot"),
    ] + [q for i in range(1, N_CH + 1) for q in _channel_props(i)]
    DEFAULT_PROPS = {"ch1": "@speed", "ch2": "@throttle", "ch3": "@brake"}

    def label_text(self, ctx):
        return self.props.get("label") or ""

    def _name(self, i, ctx):
        n = self.props.get(f"label{i}") or ""
        if n:
            return n
        ref = self.props.get(f"ch{i}", "")
        return ref[1:] if ref.startswith("@") else ref.split(":", 1)[-1]

    def draw(self, p, r, ctx):
        th, pr = ctx.theme, self.props
        self.panel(p, r, th)
        pad = min(r.width(), r.height()) * 0.08
        area = r.adjusted(pad, pad, -pad, -pad)
        title_h = r.height() * 0.13 if pr["label"] else 0
        if title_h:
            draw_text(p, QRectF(area.left(), area.top(), area.width(), title_h), pr["label"].upper(), th, title_h * 0.8,
                      th.text_dim, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, glow=False, tabular=False)
            area.setTop(area.top() + title_h + pad * 0.3)
        chans = [i for i in range(1, self.N_CH + 1) if pr.get(f"ch{i}")]
        defaults = [th.accent2, th.throttle, th.brake, QColor("#64d2ff")]
        W = float(pr["window"])
        # Sample on a grid fixed in absolute time (not relative to "now"): every point keeps its
        # value from frame to frame and just scrolls left, so already-drawn history never wiggles.
        step = W / 400.0
        if pr["now_position"] == "center":
            t_lo, t_hi = ctx.t - W / 2, ctx.t + W / 2
        else:
            t_lo, t_hi = ctx.t - W, ctx.t
        k0, k1 = int(np.floor(t_lo / step)), int(np.ceil(t_hi / step))
        ts = np.arange(k0, k1 + 1) * step
        if pr["now_position"] != "center":
            ts = np.append(ts[ts < ctx.t], ctx.t)  # the live edge ends exactly at the current value
        n = len(ts)
        xs = (ts - t_lo) / W
        data = {}
        for i in chans:
            ref = pr[f"ch{i}"]
            s, a = float(pr.get(f"scale{i}", 1.0)), float(pr.get(f"add{i}", 0.0))
            v = ctx.series(ref, ts) * s + a
            lo, hi = ctx.auto_range(ref)
            lo, hi = sorted((lo * s + a, hi * s + a))
            now = ctx.val(ref) * s + a
            col = QColor(pr.get(f"color{i}")) if pr.get(f"color{i}") else defaults[(i - 1) % 4]
            data[i] = (v, lo, hi, now, col)
        mode = pr["y_mode"]
        if mode == "fixed":
            shared = (float(pr["ymin"]), float(pr["ymax"]))
        elif mode == "shared auto" and data:
            shared = nice_range(min(d[1] for d in data.values()), max(d[2] for d in data.values()))
        else:
            shared = None
        # legend
        if pr["legend"] and chans:
            lh = max(12.0, r.height() * 0.1)
            x, y = area.left(), area.top()
            for i in chans:
                v, lo, hi, now, col = data[i]
                name = self._name(i, ctx)
                if len(name) > 22:
                    name = name[:20] + "…"
                txt = f"{name}  {fmt_num(now, pr['decimals'])}"
                w = text_width(th, lh * 0.75, txt)
                if x > area.left() and x + lh * 0.7 + w > area.right():
                    x, y = area.left(), y + lh * 1.1
                p.save()
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(x, y + lh * 0.3, lh * 0.5, lh * 0.5), 2, 2)
                p.restore()
                draw_text(p, QRectF(x + lh * 0.7, y, w + 4, lh), txt, th, lh * 0.75, th.text,
                          Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, glow=False)
                x += lh * 0.7 + w + lh
            area.setTop(y + lh * 1.3)
        axis_w = 0.0
        if pr["axis"] and shared is not None:
            dec = _tick_fmt(abs(shared[1] - shared[0]) / 4)
            axis_w = max(text_width(th, r.height() * 0.07, f"{shared[0]:.{dec}f}"),
                         text_width(th, r.height() * 0.07, f"{shared[1]:.{dec}f}")) + 6
            for k in range(5):
                y = area.bottom() - area.height() * k / 4
                draw_text(p, QRectF(area.left(), y - 10, axis_w - 6, 20), f"{shared[0] + (shared[1] - shared[0]) * k / 4:.{dec}f}",
                          th, r.height() * 0.07, th.text_dim, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                          glow=False)
            area.setLeft(area.left() + axis_w)
        p.save()
        p.setClipRect(area.adjusted(-2, -4, 2, 4))
        if pr["grid"]:
            p.setPen(QPen(th.track, 1))
            for k in range(0, 5):
                y = area.top() + area.height() * k / 4
                p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        if pr["now_position"] == "center":
            p.setPen(QPen(th.text_dim, 1.5, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(area.center().x(), area.top()), QPointF(area.center().x(), area.bottom()))
        lw = float(pr["line_width"])
        for idx, i in enumerate(chans):
            v, lo, hi, now, col = data[i]
            ylo, yhi = shared if shared is not None else nice_range(lo, hi)
            if yhi == ylo:
                yhi = ylo + 1
            f = (v - ylo) / (yhi - ylo)
            path = QPainterPath()
            started = False
            first_pt = last_pt = None
            for k in range(n):
                if not np.isfinite(f[k]):
                    started = False
                    continue
                pt = QPointF(area.left() + area.width() * float(xs[k]),
                             area.bottom() - area.height() * float(np.clip(f[k], -0.05, 1.05)))
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
                    first_pt = first_pt or pt
                last_pt = pt
            if idx == 0 and pr["fill"] and first_pt is not None:
                fp = QPainterPath(path)
                fp.lineTo(QPointF(last_pt.x(), area.bottom()))
                fp.lineTo(QPointF(first_pt.x(), area.bottom()))
                fp.closeSubpath()
                g = QLinearGradient(area.topLeft(), area.bottomLeft())
                c1, c2 = QColor(col), QColor(col)
                c1.setAlpha(110)
                c2.setAlpha(10)
                g.setColorAt(0, c1)
                g.setColorAt(1, c2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(g)
                p.drawPath(fp)
                p.setBrush(Qt.BrushStyle.NoBrush)
            if th.glow:
                p.setPen(glow_pen(col, lw * 2.3, 50))
                p.drawPath(path)
            p.setPen(QPen(col, lw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        p.restore()
