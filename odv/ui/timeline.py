# SPDX-License-Identifier: GPL-3.0-or-later
"""Timeline: speed trace, laps, trim range, playhead and a sync-check lane."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..render.widgets import convert


def fmt_tc(t: float) -> str:
    if t is None or not np.isfinite(t):
        return "--:--"
    sign = "-" if t < 0 else ""
    t = abs(t)
    m = int(t // 60)
    return f"{sign}{m}:{t - 60 * m:05.2f}"


class Timeline(QWidget):
    seek = Signal(float)
    trimChanged = Signal(float, float)

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.t = 0.0
        self.v0, self.v1 = 0.0, 1.0
        self.motion = None  # (t, yaw) from video analysis
        self._drag = None
        self._cache = None
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.reset_view()

    def reset_view(self):
        d = self.project.duration or 60.0
        self.v0, self.v1 = 0.0, d
        self.invalidate()

    def invalidate(self):
        self._cache = None
        self.update()

    def set_time(self, t):
        self.t = t
        # follow playhead when zoomed
        span = self.v1 - self.v0
        if t > self.v1 - span * 0.05 or t < self.v0:
            d = self.project.duration or span
            self.v0 = max(0.0, min(t - span * 0.1, d - span))
            self.v1 = self.v0 + span
            self._cache = None
        self.update()

    # ------------------------------------------------------------------ mapping
    def lane(self):
        return QRectF(8, 22, self.width() - 16, self.height() - 30)

    def x_of(self, t):
        L = self.lane()
        return L.left() + (t - self.v0) / max(self.v1 - self.v0, 1e-6) * L.width()

    def t_of(self, x):
        L = self.lane()
        return self.v0 + (x - L.left()) / max(L.width(), 1) * (self.v1 - self.v0)

    # ------------------------------------------------------------------ data
    def _series(self):
        if self._cache is not None:
            return self._cache
        n = max(200, self.width())
        ts = np.linspace(self.v0, self.v1, n)
        sess = self.project.session
        out = dict(ts=ts)
        if sess.roles.get("speed"):
            out["speed"] = convert(sess.value("@speed", ts), sess.unit_of("@speed"), self.project.speed_unit)
        if sess.roles.get("throttle"):
            out["thr"] = sess.value("@throttle", ts)
        if sess.roles.get("brake"):
            out["brk"] = sess.value("@brake", ts)
        # sync lane: video yaw vs data yaw
        if self.motion is not None and sess.roles.get("yaw_rate"):
            mt, my = self.motion
            out["vyaw"] = np.interp(ts, mt, my, left=np.nan, right=np.nan)
            out["dyaw"] = sess.value("@yaw_rate", ts)
        self._cache = out
        return out

    # ------------------------------------------------------------------ paint
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#121418"))
        L = self.lane()
        dur = self.project.duration
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        # ruler
        span = self.v1 - self.v0
        step = next(s for s in (0.1, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600, 1e9) if span / s < 14)
        t = np.ceil(self.v0 / step) * step
        p.setPen(QColor("#5c636d"))
        while t <= self.v1:
            x = self.x_of(t)
            p.drawLine(QPointF(x, 14), QPointF(x, 20))
            p.drawText(QPointF(x + 3, 12), fmt_tc(t) if step >= 1 else f"{t:.1f}")
            t += step
        p.fillRect(L, QColor("#171a1f"))
        if not dur:
            p.setPen(QColor("#6b727c"))
            p.drawText(L, Qt.AlignmentFlag.AlignCenter, "No video loaded")
            p.end()
            return
        # trim shading
        pr = self.project
        tin, tout = pr.trim_in, pr.out_point
        shade = QColor(0, 0, 0, 140)
        if tin > self.v0:
            p.fillRect(QRectF(L.left(), L.top(), self.x_of(tin) - L.left(), L.height()), shade)
        if tout < self.v1:
            p.fillRect(QRectF(self.x_of(tout), L.top(), L.right() - self.x_of(tout), L.height()), shade)
        s = self._series()
        ts = s["ts"]
        has_sync = "vyaw" in s
        main = QRectF(L.left(), L.top() + 4, L.width(), L.height() * (0.62 if has_sync else 1.0) - 8)

        def trace(vals, rect, color, lo=None, hi=None, width=1.6, fill=False):
            v = np.asarray(vals, dtype=float)
            if not np.isfinite(v).any():
                return
            lo = np.nanmin(v) if lo is None else lo
            hi = np.nanmax(v) if hi is None else hi
            if hi <= lo:
                hi = lo + 1
            path = QPainterPath()
            started = False
            for x_t, val in zip(ts, v):
                if not np.isfinite(val):
                    started = False
                    continue
                pt = QPointF(self.x_of(x_t), rect.bottom() - (val - lo) / (hi - lo) * rect.height())
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
            p.setPen(QPen(color, width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        if "thr" in s:
            trace(s["thr"], QRectF(main.left(), main.top() + main.height() * 0.55, main.width(), main.height() * 0.45),
                  QColor(48, 209, 88, 120), width=1.0)
        if "brk" in s:
            trace(s["brk"], QRectF(main.left(), main.top() + main.height() * 0.55, main.width(), main.height() * 0.45),
                  QColor(255, 69, 58, 140), width=1.0)
        if "speed" in s:
            trace(s["speed"], main, QColor("#ffd60a"), width=1.8)
        # laps
        laps = pr.laps
        if laps is not None and laps.laps:
            for lap in laps.laps:
                tv = laps.to_video_time(lap.t_start)
                if tv is None or not (self.v0 <= tv <= self.v1):
                    continue
                x = self.x_of(tv)
                p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(x, L.top()), QPointF(x, L.bottom()))
                p.setPen(QColor("#e6e8eb"))
                p.drawText(QPointF(x + 3, L.top() + 11), f"L{lap.number}" if lap.number > 0 else "OUT")
        # data coverage (where no data -> hatch)
        prim = pr.primary()
        if prim is not None:
            a, b = prim.t_start - prim.offset, prim.t_end - prim.offset
            nod = QColor(255, 69, 58, 40)
            if a > self.v0:
                p.fillRect(QRectF(L.left(), L.top(), self.x_of(min(a, self.v1)) - L.left(), L.height()), nod)
            if b < self.v1:
                p.fillRect(QRectF(self.x_of(max(b, self.v0)), L.top(), L.right() - self.x_of(max(b, self.v0)), L.height()), nod)
        if has_sync:
            lane2 = QRectF(L.left(), L.top() + L.height() * 0.64, L.width(), L.height() * 0.34)
            p.fillRect(lane2, QColor("#14171b"))
            vy, dy = s["vyaw"], s["dyaw"]
            m = np.isfinite(vy) & np.isfinite(dy)
            if m.sum() > 10:
                sv = np.nanstd(vy[m]) or 1
                sd = np.nanstd(dy[m]) or 1
                sign = 1.0 if np.nansum(vy[m] * dy[m]) >= 0 else -1.0
                trace(sign * vy / sv, lane2, QColor(100, 210, 255, 200), -3, 3, 1.2)
                trace(dy / sd, lane2, QColor(255, 159, 10, 200), -3, 3, 1.2)
            p.fillRect(QRectF(lane2.left(), lane2.top(), 250, 14), QColor(20, 23, 27, 220))
            x0 = lane2.left() + 4
            p.setPen(QColor("#8b939d"))
            p.drawText(QPointF(x0, lane2.top() + 11), "SYNC CHECK")
            p.setPen(QColor(100, 210, 255))
            p.drawText(QPointF(x0 + 72, lane2.top() + 11), "▬ video motion")
            p.setPen(QColor(255, 159, 10))
            p.drawText(QPointF(x0 + 160, lane2.top() + 11), "▬ data yaw")
        # trim handles
        for tt, lab in ((tin, "IN"), (tout, "OUT")):
            if self.v0 <= tt <= self.v1:
                x = self.x_of(tt)
                p.setPen(QPen(QColor("#30d158"), 2))
                p.drawLine(QPointF(x, L.top()), QPointF(x, L.bottom()))
                p.setBrush(QColor("#30d158"))
                p.drawRect(QRectF(x - (22 if lab == "OUT" else 0), L.bottom() - 12, 22, 12))
                p.setPen(QColor("black"))
                p.drawText(QRectF(x - (22 if lab == "OUT" else 0), L.bottom() - 12, 22, 12), Qt.AlignmentFlag.AlignCenter, lab)
        # playhead
        x = self.x_of(self.t)
        p.setPen(QPen(QColor("#ff3b30"), 2))
        p.drawLine(QPointF(x, 12), QPointF(x, L.bottom()))
        p.setBrush(QColor("#ff3b30"))
        p.drawPolygon([QPointF(x - 6, 6), QPointF(x + 6, 6), QPointF(x, 14)])
        p.end()

    # ------------------------------------------------------------------ interaction
    def mousePressEvent(self, ev):
        x = ev.position().x()
        L = self.lane()
        pr = self.project
        for tt, which in ((pr.trim_in, "in"), (pr.out_point, "out")):
            if abs(self.x_of(tt) - x) < 6 and ev.position().y() > L.bottom() - 16:
                self._drag = which
                return
        self._drag = "seek"
        self.seek.emit(max(0.0, min(self.t_of(x), pr.duration)))

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        t = max(0.0, min(self.t_of(ev.position().x()), self.project.duration))
        if self._drag == "seek":
            self.seek.emit(t)
        elif self._drag == "in":
            self.project.trim_in = min(t, self.project.out_point - 0.5)
            self.trimChanged.emit(self.project.trim_in, self.project.out_point)
            self.update()
        elif self._drag == "out":
            self.project.trim_out = max(t, self.project.trim_in + 0.5)
            self.trimChanged.emit(self.project.trim_in, self.project.out_point)
            self.update()

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def wheelEvent(self, ev):
        d = self.project.duration or 60
        span = self.v1 - self.v0
        tc = self.t_of(ev.position().x())
        if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            shift = -np.sign(ev.angleDelta().y()) * span * 0.1
            self.v0 = max(0.0, min(self.v0 + shift, d - span))
            self.v1 = self.v0 + span
        else:
            f = 0.8 if ev.angleDelta().y() > 0 else 1.25
            ns = max(2.0, min(d, span * f))
            a = (tc - self.v0) / span
            self.v0 = max(0.0, tc - a * ns)
            self.v1 = min(d, self.v0 + ns)
            self.v0 = max(0.0, self.v1 - ns)
        self._cache = None
        self.update()

    def resizeEvent(self, ev):
        self._cache = None
        super().resizeEvent(ev)
