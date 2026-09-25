# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview canvas: video frame + live overlay, with direct manipulation of widgets."""
from __future__ import annotations

import copy
from typing import Optional

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QMenu, QSizePolicy, QWidget

from ..render.renderer import REF_WIDTH, OverlayRenderer
from ..render.widgets import Widget, widget_from_json
from ..video import VideoReader, transform_qimage

HANDLE = 9  # px, screen space
SNAP = 10  # ref units


class PreviewCanvas(QWidget):
    selectionChanged = Signal(object)
    layoutChanged = Signal()
    timeClicked = Signal(float)

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.renderer = OverlayRenderer(project)
        self.reader: Optional[VideoReader] = None
        self.t = 0.0
        self.frame_t = 0.0
        self.frame: Optional[QImage] = None
        self.edit_mode = True
        self.show_overlay = True
        self.selected: Optional[Widget] = None
        self._drag = None
        self._hover: Optional[Widget] = None
        self._guides = []
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------ media
    def set_project(self, project):
        self.project = project
        self.renderer = OverlayRenderer(project)
        self.selected = None
        self.open_video()

    def open_video(self):
        if self.reader:
            self.reader.close()
            self.reader = None
        self.frame = None
        if self.project.video:
            try:
                self.reader = VideoReader(self.project.video.path, max_width=1920)
            except Exception as exc:  # pragma: no cover
                print("[odv] cannot open video:", exc)
        self.set_time(self.t)

    def set_time(self, t: float):
        dur = self.project.duration
        self.t = max(0.0, min(t, dur)) if dur else max(0.0, t)
        if self.reader is not None:
            res = self.reader.frame_at(self.t)
            if res is not None and res[1] is not None:
                ft, arr = res
                h, w = arr.shape[:2]
                self._arr = arr  # keep buffer alive
                img = QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32)
                if self.project.video_tf.is_identity() and not self.project.video.rotation:
                    self.frame = img
                else:
                    self.frame = transform_qimage(img, self.project.video, self.project.video_tf)
                self.frame_t = ft
            else:
                self.frame_t = self.t
        else:
            self.frame_t = self.t
        self.update()

    # ------------------------------------------------------------------ geometry
    def video_rect(self) -> QRectF:
        dw, dh = self.project.display_size()
        ar = dw / dh if self.project.video else 16 / 9
        W, H = self.width() - 16, self.height() - 16
        if W / H > ar:
            h = H
            w = h * ar
        else:
            w = W
            h = w / ar
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def to_ref(self, pos) -> QPointF:
        vr = self.video_rect()
        s = REF_WIDTH / vr.width()
        return QPointF((pos.x() - vr.left()) * s, (pos.y() - vr.top()) * s)

    def ref_to_screen(self, r: QRectF) -> QRectF:
        vr = self.video_rect()
        s = vr.width() / REF_WIDTH
        return QRectF(vr.left() + r.left() * s, vr.top() + r.top() * s, r.width() * s, r.height() * s)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0d0f12"))
        vr = self.video_rect()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self.frame is not None:
            p.drawImage(vr, self.frame)
        else:
            p.fillRect(vr, QColor("#1a1d22"))
            p.setPen(QColor("#6b727c"))
            f = QFont()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(vr, Qt.AlignmentFlag.AlignCenter,
                       "Open a video  (File ▸ Open video…)\nthen add RaceBox / CSV / MF4 data")
        if self.show_overlay:
            p.save()
            p.translate(vr.topLeft())
            p.setClipRect(QRectF(0, 0, vr.width(), vr.height()))
            self.renderer.paint(p, vr.width(), self.frame_t, frame=self.frame, edit_mode=self.edit_mode)
            p.restore()
        if self.edit_mode:
            self._paint_edit(p)
        p.end()

    def _paint_edit(self, p: QPainter):
        for g in self._guides:
            p.setPen(QPen(QColor(255, 59, 48, 180), 1, Qt.PenStyle.DashLine))
            vr = self.video_rect()
            s = vr.width() / REF_WIDTH
            if g[0] == "x":
                x = vr.left() + g[1] * s
                p.drawLine(QPointF(x, vr.top()), QPointF(x, vr.bottom()))
            else:
                y = vr.top() + g[1] * s
                p.drawLine(QPointF(vr.left(), y), QPointF(vr.right(), y))
        if self._hover is not None and self._hover is not self.selected:
            p.setPen(QPen(QColor(255, 255, 255, 120), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.ref_to_screen(self._hover.rect))
        if self.selected is not None:
            r = self.ref_to_screen(self.selected.rect)
            p.setPen(QPen(QColor("#ff3b30"), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            p.setBrush(QColor("white"))
            p.setPen(QPen(QColor("#ff3b30"), 1.5))
            for c in self._handles(r).values():
                p.drawRect(c)
            p.setPen(QColor("white"))
            p.drawText(QPointF(r.left(), r.top() - 5), self.selected.NAME)

    def _handles(self, r: QRectF):
        h = HANDLE
        return {
            "tl": QRectF(r.left() - h / 2, r.top() - h / 2, h, h),
            "tr": QRectF(r.right() - h / 2, r.top() - h / 2, h, h),
            "bl": QRectF(r.left() - h / 2, r.bottom() - h / 2, h, h),
            "br": QRectF(r.right() - h / 2, r.bottom() - h / 2, h, h),
        }

    # ------------------------------------------------------------------ interaction
    def widget_at(self, pos) -> Optional[Widget]:
        rp = self.to_ref(pos)
        for w in reversed(self.project.widgets):
            if w.props.get("visible", True) and w.rect.contains(rp):
                return w
        return None

    def select(self, w: Optional[Widget]):
        self.selected = w
        self.selectionChanged.emit(w)
        self.update()

    def mousePressEvent(self, ev):
        self.setFocus()
        if not self.edit_mode or ev.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(ev)
        pos = ev.position()
        if self.selected is not None:
            r = self.ref_to_screen(self.selected.rect)
            for name, hr in self._handles(r).items():
                if hr.adjusted(-4, -4, 4, 4).contains(pos):
                    self._drag = ("resize", name, self.to_ref(pos), QRectF(self.selected.rect))
                    return
        w = self.widget_at(pos)
        self.select(w)
        if w is not None:
            self._drag = ("move", None, self.to_ref(pos), QRectF(w.rect))

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._drag and self.selected is not None:
            kind, handle, start, orig = self._drag
            cur = self.to_ref(pos)
            dx, dy = cur.x() - start.x(), cur.y() - start.y()
            w = self.selected
            if kind == "move":
                nx, ny = orig.left() + dx, orig.top() + dy
                if not (ev.modifiers() & Qt.KeyboardModifier.AltModifier):
                    nx, ny = self._snap_move(w, nx, ny, orig.width(), orig.height())
                w.x, w.y = nx, ny
            else:
                l, t, r, b = orig.left(), orig.top(), orig.right(), orig.bottom()
                if "l" in handle:
                    l = min(r - 30, l + dx)
                if "r" in handle:
                    r = max(l + 30, r + dx)
                if "t" in handle:
                    t = min(b - 30, t + dy)
                if "b" in handle:
                    b = max(t + 30, b + dy)
                if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    ar = orig.width() / orig.height()
                    wdt = r - l
                    hgt = wdt / ar
                    if "t" in handle:
                        t = b - hgt
                    else:
                        b = t + hgt
                w.x, w.y, w.w, w.h = l, t, r - l, b - t
            self.project.dirty = True
            self.update()
            self.layoutChanged.emit()
            return
        if self.edit_mode:
            hw = self.widget_at(pos)
            if hw is not self._hover:
                self._hover = hw
                self.update()
            cursor = Qt.CursorShape.ArrowCursor
            if self.selected is not None:
                r = self.ref_to_screen(self.selected.rect)
                for name, hr in self._handles(r).items():
                    if hr.adjusted(-4, -4, 4, 4).contains(pos):
                        cursor = Qt.CursorShape.SizeFDiagCursor if name in ("tl", "br") else Qt.CursorShape.SizeBDiagCursor
                if cursor == Qt.CursorShape.ArrowCursor and hw is not None:
                    cursor = Qt.CursorShape.SizeAllCursor
            elif hw is not None:
                cursor = Qt.CursorShape.PointingHandCursor
            self.setCursor(cursor)

    def mouseReleaseEvent(self, ev):
        self._drag = None
        self._guides = []
        self.update()

    def _snap_move(self, w, x, y, ww, hh):
        self._guides = []
        H = self.project.ref_height
        xs = [0, REF_WIDTH, REF_WIDTH / 2, 40, REF_WIDTH - 40]
        ys = [0, H, H / 2, 40, H - 40]
        for o in self.project.widgets:
            if o is w:
                continue
            xs += [o.x, o.x + o.w]
            ys += [o.y, o.y + o.h]
        best_x = None
        for cand_edge, off in ((x, 0), (x + ww, ww), (x + ww / 2, ww / 2)):
            for g in xs:
                if abs(cand_edge - g) < SNAP and (best_x is None or abs(cand_edge - g) < best_x[0]):
                    best_x = (abs(cand_edge - g), g - off, g)
        best_y = None
        for cand_edge, off in ((y, 0), (y + hh, hh), (y + hh / 2, hh / 2)):
            for g in ys:
                if abs(cand_edge - g) < SNAP and (best_y is None or abs(cand_edge - g) < best_y[0]):
                    best_y = (abs(cand_edge - g), g - off, g)
        if best_x:
            x = best_x[1]
            self._guides.append(("x", best_x[2]))
        if best_y:
            y = best_y[1]
            self._guides.append(("y", best_y[2]))
        return x, y

    def event(self, ev):
        if ev.type() == QEvent.Type.ShortcutOverride and self.edit_mode and self.selected is not None and \
                ev.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Delete,
                             Qt.Key.Key_Backspace, Qt.Key.Key_Escape):
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        w = self.selected
        if self.edit_mode and w is not None:
            step = 10 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            k = ev.key()
            moved = True
            if k == Qt.Key.Key_Left:
                w.x -= step
            elif k == Qt.Key.Key_Right:
                w.x += step
            elif k == Qt.Key.Key_Up:
                w.y -= step
            elif k == Qt.Key.Key_Down:
                w.y += step
            elif k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_selected()
            elif k == Qt.Key.Key_Escape:
                self.select(None)
            else:
                moved = False
            if moved:
                self.project.dirty = True
                self.layoutChanged.emit()
                self.update()
                return
        super().keyPressEvent(ev)

    def contextMenuEvent(self, ev):
        if not self.edit_mode:
            return
        w = self.widget_at(ev.pos())
        if w is None:
            return
        self.select(w)
        m = QMenu(self)
        m.addAction("Bring to front", lambda: self._reorder(w, 1))
        m.addAction("Send to back", lambda: self._reorder(w, -1))
        m.addAction("Duplicate", lambda: self.duplicate(w))
        m.addSeparator()
        m.addAction("Delete", self.delete_selected)
        m.exec(ev.globalPos())

    def _reorder(self, w, d):
        ws = self.project.widgets
        ws.remove(w)
        if d > 0:
            ws.append(w)
        else:
            ws.insert(0, w)
        self.layoutChanged.emit()
        self.update()

    def duplicate(self, w):
        d = w.to_json()
        d = copy.deepcopy(d)
        d.pop("id", None)
        d["x"] += 30
        d["y"] += 30
        nw = widget_from_json(d)
        self.project.widgets.append(nw)
        self.select(nw)
        self.layoutChanged.emit()

    def delete_selected(self):
        if self.selected in self.project.widgets:
            self.project.widgets.remove(self.selected)
            self.project.dirty = True
        self.select(None)
        self.layoutChanged.emit()
