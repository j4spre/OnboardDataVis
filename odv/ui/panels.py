# SPDX-License-Identifier: GPL-3.0-or-later
"""Side panels: data & sync (incl. video orientation), channel roles, laps."""
from __future__ import annotations

import os
from functools import partial
from typing import Optional

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QCompleter, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QToolButton,
                               QVBoxLayout, QWidget)

from ..data.laps import fmt_laptime
from ..data.model import ROLES
from ..render.theme import THEMES
from ..render.widgets import SPEED_UNITS, WIDGET_TYPES, Widget
from .timeline import fmt_tc


def muted(text):
    l = QLabel(text)
    l.setObjectName("muted")
    l.setWordWrap(True)
    return l


def hline():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color:#2a2e35")
    return f


# ====================================================================== data + sync

class SourceCard(QGroupBox):
    def __init__(self, panel, src, primary: bool):
        super().__init__(src.name)
        self.panel, self.src = panel, src
        pr = panel.project
        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        top = QHBoxLayout()
        kind = {"racebox": "RaceBox", "csv": "CSV", "mf4": "MF4", "vbo": "VBO", "gpx": "GPX"}.get(src.kind, src.kind)
        info = f"{kind} · {len(src.channel_names())} channels"
        if src.meta.get("rate_hz"):
            info += f" · {src.meta['rate_hz']:.0f} Hz"
        dur = src.t_end - src.t_start
        info += f" · {fmt_tc(dur)}"
        top.addWidget(muted(info), 1)
        if primary:
            b = QLabel("PRIMARY")
            b.setObjectName("badge")
            b.setToolTip("Lap timing, map and G-forces come from this source. Other sources follow its offset.")
            top.addWidget(b)
        else:
            mp = QToolButton()
            mp.setText("Make primary")
            mp.clicked.connect(lambda: panel.make_primary(src.id))
            top.addWidget(mp)
        rm = QToolButton()
        rm.setText("✕")
        rm.setToolTip("Remove this data source")
        rm.clicked.connect(lambda: panel.remove_source(src.id))
        top.addWidget(rm)
        lay.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Data time at video 0:00"))
        self.spin = QDoubleSpinBox()
        self.spin.setRange(-1e7, 1e7)
        self.spin.setDecimals(3)
        self.spin.setSingleStep(0.1)
        self.spin.setSuffix(" s")
        self.spin.setKeyboardTracking(False)
        self.spin.setValue(src.offset)
        self.spin.valueChanged.connect(self._spin_changed)
        self.spin.setToolTip("Offset between video and data: the data timestamp shown at the first video frame.")
        row.addWidget(self.spin, 1)
        lay.addLayout(row)

        fd = pr.video.frame_dur if pr.video else 1 / 30
        nud = QHBoxLayout()
        nud.setSpacing(3)
        for lab, d, tip in (("−1 s", -1.0, ""), ("−0.1", -0.1, ""), ("−1 f", -fd, "one video frame"),
                            ("+1 f", fd, "one video frame"), ("+0.1", 0.1, ""), ("+1 s", 1.0, "")):
            b = QPushButton(lab)
            b.setMinimumWidth(10)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setToolTip(f"Shift data by {d:+.3f} s. Positive = overlay shows events earlier. {tip}")
            b.clicked.connect(partial(self._nudge, d))
            nud.addWidget(b)
        lay.addLayout(nud)

        act = QHBoxLayout()
        if primary:
            self.btn_motion = QPushButton("Auto-sync from video motion")
            self.btn_motion.setObjectName("primary")
            self.btn_motion.setToolTip("Matches camera rotation in the video with the logger's yaw rate (gyro or GPS "
                                       "heading). Works without any timestamps.")
            self.btn_motion.clicked.connect(lambda: panel.motion_sync(src.id))
            act.addWidget(self.btn_motion)
            self.btn_ts = QPushButton("Use timestamps")
            ok = pr.video is not None and pr.video.creation_time is not None and src.utc_start is not None
            self.btn_ts.setEnabled(ok)
            self.btn_ts.setToolTip("Align using the video's creation time and the log's UTC start time."
                                   if ok else "Needs a video creation time and absolute log timestamps.")
            self.btn_ts.clicked.connect(lambda: panel.timestamp_sync(src.id))
            act.addWidget(self.btn_ts)
        else:
            b = QPushButton("Align to primary (speed)")
            b.setObjectName("primary")
            b.setToolTip("Cross-correlates the speed channels of both logs to find the time shift.")
            b.clicked.connect(lambda: panel.align_source(src.id))
            act.addWidget(b)
        lay.addLayout(act)
        self.prog = QProgressBar()
        self.prog.setVisible(False)
        self.prog.setRange(0, 100)
        lay.addWidget(self.prog)
        self.result = muted(src.meta.get("sync_note", ""))
        lay.addWidget(self.result)

    def _spin_changed(self, v):
        if abs(v - self.src.offset) > 1e-9:
            self.panel.set_offset(self.src.id, v)

    def _nudge(self, d):
        self.panel.set_offset(self.src.id, self.src.offset + d)

    def refresh(self):
        self.spin.blockSignals(True)
        self.spin.setValue(self.src.offset)
        self.spin.blockSignals(False)
        self.result.setText(self.src.meta.get("sync_note", ""))


class DataPanel(QWidget):
    offsetsChanged = Signal()
    sourcesChanged = Signal()
    motionRequested = Signal(str)
    alignRequested = Signal(str)
    timestampRequested = Signal(str)
    addDataRequested = Signal()
    openVideoRequested = Signal()
    videoTransformChanged = Signal()

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.cards = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        vid = QGroupBox("Video")
        vl = QVBoxLayout(vid)
        self.video_label = muted("No video")
        vl.addWidget(self.video_label)
        bv = QPushButton("Open video…")
        bv.clicked.connect(self.openVideoRequested)
        vl.addWidget(bv)
        # orientation
        orow = QHBoxLayout()
        orow.setSpacing(3)
        self.rot_l = QPushButton("⟲ 90°")
        self.rot_l.setToolTip("Rotate the video 90° counter-clockwise")
        self.rot_l.clicked.connect(lambda: self._rotate(-90))
        self.rot_r = QPushButton("⟳ 90°")
        self.rot_r.setToolTip("Rotate the video 90° clockwise")
        self.rot_r.clicked.connect(lambda: self._rotate(90))
        self.flip_h = QPushButton("⇋ Flip H")
        self.flip_h.setCheckable(True)
        self.flip_h.setToolTip("Mirror left/right (e.g. footage from a mirrored mount)")
        self.flip_h.toggled.connect(self._flip)
        self.flip_v = QPushButton("⇵ Flip V")
        self.flip_v.setCheckable(True)
        self.flip_v.setToolTip("Mirror top/bottom (camera mounted upside down: use Rotate 180° instead)")
        self.flip_v.toggled.connect(self._flip)
        for b in (self.rot_l, self.rot_r, self.flip_h, self.flip_v):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            orow.addWidget(b)
        vl.addLayout(orow)
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("Level"))
        self.angle = QDoubleSpinBox()
        self.angle.setRange(-45, 45)
        self.angle.setDecimals(1)
        self.angle.setSingleStep(0.5)
        self.angle.setSuffix(" °")
        self.angle.setKeyboardTracking(False)
        self.angle.setToolTip("Fine rotation to straighten a tilted horizon (clockwise positive)")
        self.angle.valueChanged.connect(self._angle)
        lrow.addWidget(self.angle, 1)
        self.fill = QCheckBox("Zoom to fill")
        self.fill.setToolTip("Enlarge slightly so a levelled video has no black corners")
        self.fill.toggled.connect(self._angle)
        lrow.addWidget(self.fill)
        rst = QToolButton()
        rst.setText("Reset")
        rst.clicked.connect(self._reset_tf)
        lrow.addWidget(rst)
        vl.addLayout(lrow)
        self.tf_label = muted("")
        vl.addWidget(self.tf_label)
        outer.addWidget(vid)

        hb = QHBoxLayout()
        ad = QPushButton("＋ Add data (RaceBox / CSV / MF4 / VBO / GPX)…")
        ad.setObjectName("primary")
        ad.clicked.connect(self.addDataRequested)
        hb.addWidget(ad)
        outer.addLayout(hb)

        self.link = QCheckBox("Keep other sources locked to the primary")
        self.link.setChecked(project.link_offsets)
        self.link.toggled.connect(self._link)
        outer.addWidget(self.link)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.cards_lay = QVBoxLayout(self.inner)
        self.cards_lay.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self.inner)
        outer.addWidget(self.scroll, 1)

        sp = QGroupBox("Sync point (manual)")
        sl = QVBoxLayout(sp)
        sl.addWidget(muted("Pause on the frame where the car crosses the start/finish line, pick the lap and "
                           "click Set. Or type the data time that belongs to the current frame."))
        r1 = QHBoxLayout()
        self.lap_combo = QComboBox()
        r1.addWidget(self.lap_combo, 1)
        b1 = QPushButton("This frame = lap start")
        b1.clicked.connect(self._sync_to_lap)
        r1.addWidget(b1)
        sl.addLayout(r1)
        r2 = QHBoxLayout()
        self.data_t = QDoubleSpinBox()
        self.data_t.setRange(-1e7, 1e7)
        self.data_t.setDecimals(2)
        self.data_t.setSuffix(" s (data)")
        r2.addWidget(self.data_t, 1)
        b2 = QPushButton("This frame = data time")
        b2.clicked.connect(self._sync_to_time)
        r2.addWidget(b2)
        sl.addLayout(r2)
        outer.addWidget(sp)
        self.current_t = 0.0
        self.rebuild()

    # ---------------------------------------------------------------
    def set_project(self, project):
        self.project = project
        self.link.setChecked(project.link_offsets)
        self.rebuild()

    def rebuild(self):
        while self.cards_lay.count():
            it = self.cards_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.cards = {}
        pr = self.project
        if pr.video:
            v = pr.video
            ct = v.creation_time.strftime("%Y-%m-%d %H:%M:%S UTC") if v.creation_time else "no timestamp"
            self.video_label.setText(f"{os.path.basename(v.path)}\n{v.width}×{v.height} · {v.fps:.2f} fps · "
                                     f"{fmt_tc(v.duration)} · {ct}")
        else:
            self.video_label.setText("No video")
        self._show_tf()
        if not pr.session.sources:
            self.cards_lay.addWidget(muted("No data yet. Add a RaceBox export, a CSV, an MF4 (CAN/ECU log), "
                                           "a VBO or a GPX file."))
        for s in pr.session.sources:
            card = SourceCard(self, s, s.id == pr.primary_source)
            self.cards[s.id] = card
            self.cards_lay.addWidget(card)
        self.cards_lay.addStretch(1)
        self.refresh_laps()

    def refresh(self):
        for c in self.cards.values():
            c.refresh()

    # ---------------------------------------------------------------- orientation
    def _show_tf(self):
        tf = self.project.video_tf
        for wdg, val in ((self.flip_h, tf.flip_h), (self.flip_v, tf.flip_v), (self.angle, tf.angle),
                         (self.fill, tf.fill)):
            wdg.blockSignals(True)
            if isinstance(wdg, QDoubleSpinBox):
                wdg.setValue(val)
            else:
                wdg.setChecked(bool(val))
            wdg.blockSignals(False)
        enabled = self.project.video is not None
        for wdg in (self.rot_l, self.rot_r, self.flip_h, self.flip_v, self.angle, self.fill):
            wdg.setEnabled(enabled)
        if enabled:
            w, h = self.project.display_size()
            parts = []
            if self.project.video.rotation:
                parts.append(f"file says rotate {self.project.video.rotation}°")
            if tf.rot90:
                parts.append(f"rotated {tf.rot90}°")
            if tf.flip_h or tf.flip_v:
                parts.append("mirrored " + "+".join(x for x, f in (("H", tf.flip_h), ("V", tf.flip_v)) if f))
            if tf.angle:
                parts.append(f"levelled {tf.angle:+.1f}°")
            self.tf_label.setText(f"Output {w}×{h}" + (" · " + ", ".join(parts) if parts else ""))
        else:
            self.tf_label.setText("")

    def _apply_tf(self, **kw):
        self.project.set_video_transform(**kw)
        self._show_tf()
        self.videoTransformChanged.emit()

    def _rotate(self, d):
        self._apply_tf(rot90=(self.project.video_tf.rot90 + d) % 360)

    def _flip(self):
        self._apply_tf(flip_h=self.flip_h.isChecked(), flip_v=self.flip_v.isChecked())

    def _angle(self):
        self._apply_tf(angle=self.angle.value(), fill=self.fill.isChecked())

    def _reset_tf(self):
        self._apply_tf(rot90=0, flip_h=False, flip_v=False, angle=0.0, fill=True)

    def refresh_laps(self):
        self.lap_combo.clear()
        laps = self.project.laps
        if laps is not None:
            for i, l in enumerate(laps.laps):
                if l.number > 0:
                    self.lap_combo.addItem(f"Lap {l.number}  ({fmt_laptime(l.duration, 2)})", i)

    def set_current_time(self, t):
        self.current_t = t
        prim = self.project.primary()
        if prim is not None and not self.data_t.hasFocus():
            self.data_t.blockSignals(True)
            self.data_t.setValue(t + prim.offset)
            self.data_t.blockSignals(False)

    # ---------------------------------------------------------------
    def _link(self, v):
        self.project.link_offsets = v

    def set_offset(self, sid, value):
        self.project.set_offset(sid, value)
        self.refresh()
        self.offsetsChanged.emit()

    def make_primary(self, sid):
        self.project.primary_source = sid
        self.project.rebuild_laps()
        self.rebuild()
        self.sourcesChanged.emit()

    def remove_source(self, sid):
        self.project.remove_source(sid)
        self.rebuild()
        self.sourcesChanged.emit()

    def motion_sync(self, sid):
        self.motionRequested.emit(sid)

    def align_source(self, sid):
        self.alignRequested.emit(sid)

    def timestamp_sync(self, sid):
        self.timestampRequested.emit(sid)

    def _sync_to_lap(self):
        laps = self.project.laps
        prim = self.project.primary()
        i = self.lap_combo.currentData()
        if laps is None or prim is None or i is None:
            return
        lap = laps.laps[i]
        src = self.project.session.source(laps.source_id)
        new = lap.t_start - self.current_t
        prim_new = prim.offset + (new - src.offset) if src is not prim else new
        prim.meta["sync_note"] = f"Manual sync point: frame {fmt_tc(self.current_t)} = start of lap {lap.number}"
        self.set_offset(prim.id, prim_new)

    def _sync_to_time(self):
        prim = self.project.primary()
        if prim is None:
            return
        prim.meta["sync_note"] = f"Manual sync point: frame {fmt_tc(self.current_t)} = data {self.data_t.value():.2f} s"
        self.set_offset(prim.id, self.data_t.value() - self.current_t)

    def card(self, sid) -> Optional[SourceCard]:
        return self.cards.get(sid)


# ====================================================================== roles

class RolesPanel(QWidget):
    rolesChanged = Signal()

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(muted("Which channel feeds each gauge. Widgets use roles like @speed, so changing a role "
                            "updates every widget that uses it. Type to search."))
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        box = QWidget()
        box.setLayout(self.form)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(box)
        lay.addWidget(sc, 1)
        self.combos = {}
        self.rebuild()

    def set_project(self, project):
        self.project = project
        self.rebuild()

    def rebuild(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.combos = {}
        keys = [""] + self.project.session.all_keys()
        for role, label in ROLES.items():
            cb = QComboBox()
            cb.setEditable(True)
            cb.addItems(keys)
            comp = QCompleter(keys, cb)
            comp.setFilterMode(Qt.MatchFlag.MatchContains)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            cb.setCompleter(comp)
            cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            cur = self.project.session.roles.get(role, "")
            cb.setCurrentText(cur)
            cb.setMinimumContentsLength(18)
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.currentTextChanged.connect(partial(self._changed, role))
            self.combos[role] = cb
            self.form.addRow(label, cb)

    def _changed(self, role, text):
        keys = set(self.project.session.all_keys())
        if text and text not in keys:
            return
        if text:
            self.project.session.roles[role] = text
        else:
            self.project.session.roles.pop(role, None)
        if role in ("lat", "lon", "lap"):
            self.project.rebuild_laps()
        self.project.dirty = True
        self.rolesChanged.emit()


# ====================================================================== widgets + inspector

from .overlay_panel import ColorButton, Inspector, WidgetsPanel  # noqa: E402,F401


# ====================================================================== laps

class LapsPanel(QWidget):
    seekRequested = Signal(float)
    lapsChanged = Signal()

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.current_t = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.info = muted("")
        lay.addWidget(self.info)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Lap", "Time", "Δ best", "In video"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.cellDoubleClicked.connect(self._jump)
        lay.addWidget(self.table, 1)
        lay.addWidget(muted("Double-click a lap to jump to its start."))
        hb = QHBoxLayout()
        b1 = QPushButton("Start/finish at current position")
        b1.setToolTip("Creates a timing gate across the track where the car is at the current frame.")
        b1.clicked.connect(self._set_sf)
        hb.addWidget(b1)
        b2 = QPushButton("Use logger laps")
        b2.clicked.connect(self._reset_sf)
        hb.addWidget(b2)
        lay.addLayout(hb)
        self.rebuild()

    def set_project(self, project):
        self.project = project
        self.rebuild()

    def rebuild(self):
        laps = self.project.laps
        self.table.setRowCount(0)
        if laps is None or not laps.laps:
            self.info.setText("No lap data. Needs GPS (latitude/longitude) and either a lap channel or a "
                              "start/finish gate.")
            return
        best = laps.best_lap()
        src = self.project.session.source(laps.source_id)
        mode = "start/finish gate" if (src and (src.options.get("sf_gate") or src.meta.get("sf_gate"))) else "logger lap channel"
        self.info.setText(f"{len(laps.complete_laps())} complete laps from {src.name if src else '?'} ({mode}). "
                          f"Best: {fmt_laptime(best.duration) if best else '–'}")
        dur = self.project.duration
        for i, l in enumerate(laps.laps):
            r = self.table.rowCount()
            self.table.insertRow(r)
            tv = laps.to_video_time(l.t_start)
            tv_end = laps.to_video_time(l.t_end)
            in_vid = tv is not None and dur and tv_end > 0 and tv < dur
            cells = [
                "OUT" if l.number <= 0 else str(l.number),
                fmt_laptime(l.duration) + ("" if l.complete else "  (partial)"),
                "" if not (l.complete and best) else ("BEST" if l is best else f"+{l.duration - best.duration:.3f}"),
                fmt_tc(tv) if in_vid else "–",
            ]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setData(Qt.ItemDataRole.UserRole, i)
                if l is best:
                    it.setForeground(QColor("#bf5af2"))
                if not in_vid:
                    it.setForeground(QColor("#6b727c"))
                self.table.setItem(r, c, it)

    def _jump(self, row, col):
        laps = self.project.laps
        i = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        tv = laps.to_video_time(laps.laps[i].t_start)
        if tv is not None:
            self.seekRequested.emit(max(0.0, tv))

    def _set_sf(self):
        laps = self.project.laps
        src = self.project.session.source(laps.source_id) if laps and laps.source_id else None
        if src is None:
            return
        laps.set_start_finish_at(self.current_t + src.offset)
        self.project.dirty = True
        self.rebuild()
        self.lapsChanged.emit()

    def _reset_sf(self):
        laps = self.project.laps
        src = self.project.session.source(laps.source_id) if laps and laps.source_id else None
        if src is None:
            return
        src.options.pop("sf_gate", None)
        self.project.rebuild_laps()
        self.project.dirty = True
        self.rebuild()
        self.lapsChanged.emit()
