# SPDX-License-Identifier: GPL-3.0-or-later
"""Side panels: data & sync, channel roles, overlay widgets + inspector, laps."""
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

class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, value=""):
        super().__init__()
        self.value = value
        self.setFixedHeight(26)
        self.clicked.connect(self._pick)
        self._show()

    def _show(self):
        if self.value:
            self.setText(self.value)
            c = QColor(self.value)
            fg = "black" if c.lightness() > 140 else "white"
            self.setStyleSheet(f"background:{self.value}; color:{fg};")
        else:
            self.setText("theme default")
            self.setStyleSheet("")

    def _pick(self):
        m = QMenu(self)
        m.addAction("Choose…", self._choose)
        m.addAction("Use theme default", lambda: self._set(""))
        m.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _choose(self):
        c = QColorDialog.getColor(QColor(self.value or "#ff3b30"), self, "Colour",
                                  QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if c.isValid():
            self._set(c.name(QColor.NameFormat.HexArgb) if c.alpha() < 255 else c.name())

    def _set(self, v):
        self.value = v
        self._show()
        self.colorChanged.emit(v)


class Inspector(QWidget):
    changed = Signal()

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.widget: Optional[Widget] = None
        self.form = QFormLayout(self)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.geo = {}

    def set_widget(self, w: Optional[Widget]):
        self.widget = w
        while self.form.rowCount():
            self.form.removeRow(0)
        self.geo = {}
        if w is None:
            self.form.addRow(muted("Select a widget on the preview (Edit layout on) or in the list above."))
            return
        t = QLabel(w.NAME)
        t.setStyleSheet("font-weight:600; font-size:11pt")
        self.form.addRow(t)
        g = QGridLayout()
        for i, k in enumerate(("x", "y", "w", "h")):
            sp = QSpinBox()
            sp.setRange(-4000, 8000)
            sp.setValue(int(getattr(w, k)))
            sp.setPrefix(f"{k.upper()} ")
            sp.valueChanged.connect(partial(self._geo, k))
            g.addWidget(sp, i // 2, i % 2)
            self.geo[k] = sp
        gw = QWidget()
        gw.setLayout(g)
        self.form.addRow("Position", gw)
        keys = [""] + ["@" + r for r in ROLES] + self.project.session.all_keys()
        for prop in w.all_props():
            val = w.props.get(prop.name, prop.default)
            if prop.kind == "channel":
                ed = QComboBox()
                ed.setEditable(True)
                ed.addItems(keys)
                comp = QCompleter(keys, ed)
                comp.setFilterMode(Qt.MatchFlag.MatchContains)
                comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                ed.setCompleter(comp)
                ed.setCurrentText(val)
                ed.setMinimumContentsLength(14)
                ed.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                ed.currentTextChanged.connect(partial(self._set, prop.name))
            elif prop.kind == "bool":
                ed = QCheckBox()
                ed.setChecked(bool(val))
                ed.toggled.connect(partial(self._set, prop.name))
            elif prop.kind == "float":
                ed = QDoubleSpinBox()
                ed.setRange(prop.minv, prop.maxv)
                ed.setDecimals(3 if abs(prop.default) < 10 else 1)
                ed.setSingleStep(0.1 if abs(prop.maxv - prop.minv) < 20 else 1)
                ed.setValue(float(val))
                ed.valueChanged.connect(partial(self._set, prop.name))
            elif prop.kind == "int":
                ed = QSpinBox()
                ed.setRange(int(max(prop.minv, -1e6)), int(min(prop.maxv, 1e6)))
                ed.setValue(int(val))
                ed.valueChanged.connect(partial(self._set, prop.name))
            elif prop.kind == "choice":
                ed = QComboBox()
                ed.addItems(prop.options or [])
                ed.setCurrentText(str(val))
                ed.currentTextChanged.connect(partial(self._set, prop.name))
            elif prop.kind == "color":
                ed = ColorButton(val)
                ed.colorChanged.connect(partial(self._set, prop.name))
            elif prop.kind == "file":
                ed = QPushButton(os.path.basename(val) if val else "Choose file…")
                ed.clicked.connect(partial(self._file, prop.name, ed))
            else:
                ed = QLineEdit(str(val))
                ed.textChanged.connect(partial(self._set, prop.name))
            self.form.addRow(prop.label or prop.name, ed)

    def refresh_geometry(self):
        if self.widget is None:
            return
        for k, sp in self.geo.items():
            sp.blockSignals(True)
            sp.setValue(int(getattr(self.widget, k)))
            sp.blockSignals(False)

    def _geo(self, k, v):
        if self.widget is not None:
            setattr(self.widget, k, float(v))
            self.project.dirty = True
            self.changed.emit()

    def _set(self, name, v):
        if self.widget is not None:
            self.widget.props[name] = v
            self.project.dirty = True
            self.changed.emit()

    def _file(self, name, btn):
        path, _ = QFileDialog.getOpenFileName(self, "Image", "", "Images (*.png *.jpg *.jpeg *.svg *.webp)")
        if path:
            btn.setText(os.path.basename(path))
            self._set(name, path)


class WidgetsPanel(QWidget):
    changed = Signal()
    selectRequested = Signal(object)

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        style = QGroupBox("Style")
        sf = QFormLayout(style)
        self.theme = QComboBox()
        self.theme.addItems(list(THEMES.keys()))
        self.theme.setCurrentText(project.theme)
        self.theme.currentTextChanged.connect(self._theme)
        sf.addRow("Theme", self.theme)
        self.accent = ColorButton(project.accent)
        self.accent.colorChanged.connect(self._accent)
        sf.addRow("Accent", self.accent)
        self.unit = QComboBox()
        self.unit.addItems(["km/h", "mph", "m/s"])
        self.unit.setCurrentText(project.speed_unit)
        self.unit.currentTextChanged.connect(self._unit)
        sf.addRow("Speed unit", self.unit)
        lay.addWidget(style)

        bar = QHBoxLayout()
        add = QPushButton("＋ Add widget")
        add.setObjectName("primary")
        m = QMenu(add)
        for t, cls in WIDGET_TYPES.items():
            m.addAction(cls.NAME, partial(self._add, t))
        add.setMenu(m)
        bar.addWidget(add)
        for txt, tip, fn in (("▲", "Bring forward", partial(self._move, 1)), ("▼", "Send backward", partial(self._move, -1)),
                             ("⧉", "Duplicate", self._dup), ("✕", "Delete", self._del)):
            b = QToolButton()
            b.setText(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        reset = QToolButton()
        reset.setText("Reset layout")
        reset.clicked.connect(self._reset)
        bar.addWidget(reset)
        lay.addLayout(bar)
        self.list = QListWidget()
        self.list.setMaximumHeight(170)
        self.list.currentRowChanged.connect(self._row)
        self.list.itemChanged.connect(self._item_changed)
        lay.addWidget(self.list)
        self.inspector = Inspector(project)
        self.inspector.changed.connect(self.changed)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.inspector)
        lay.addWidget(sc, 1)
        self.rebuild()

    def set_project(self, project):
        self.project = project
        self.inspector.project = project
        self.theme.setCurrentText(project.theme)
        self.unit.setCurrentText(project.speed_unit)
        self.accent._set(project.accent) if self.accent.value != project.accent else None
        self.rebuild()

    def rebuild(self, select: Optional[Widget] = None):
        self.list.blockSignals(True)
        self.list.clear()
        for w in reversed(self.project.widgets):
            it = QListWidgetItem(w.NAME + (f" – {w.props.get('label') or w.props.get('channel', '')}"
                                           if w.TYPE in ("value", "bar") else ""))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if w.props.get("visible", True) else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, w.id)
            self.list.addItem(it)
        self.list.blockSignals(False)
        if select is not None:
            self.show_selection(select)
        else:
            self.inspector.set_widget(None)

    def show_selection(self, w: Optional[Widget]):
        self.list.blockSignals(True)
        if w is None:
            self.list.clearSelection()
            self.list.setCurrentRow(-1)
        else:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == w.id:
                    self.list.setCurrentRow(i)
        self.list.blockSignals(False)
        if w is not self.inspector.widget:
            self.inspector.set_widget(w)

    def _widget_by_id(self, wid):
        return next((w for w in self.project.widgets if w.id == wid), None)

    def _row(self, row):
        it = self.list.item(row)
        w = self._widget_by_id(it.data(Qt.ItemDataRole.UserRole)) if it else None
        self.inspector.set_widget(w)
        self.selectRequested.emit(w)

    def _item_changed(self, it):
        w = self._widget_by_id(it.data(Qt.ItemDataRole.UserRole))
        if w is not None:
            w.props["visible"] = it.checkState() == Qt.CheckState.Checked
            self.changed.emit()

    def _add(self, t):
        cls = WIDGET_TYPES[t]
        W, H = cls.DEFAULT_SIZE
        w = cls(1920 / 2 - W / 2, self.project.ref_height / 2 - H / 2)
        if t == "speed_dial":
            w.props["unit"] = self.project.speed_unit
        self.project.widgets.append(w)
        self.project.dirty = True
        self.rebuild(w)
        self.selectRequested.emit(w)
        self.changed.emit()

    def _current(self):
        return self.inspector.widget

    def _move(self, d):
        w = self._current()
        if w is None:
            return
        ws = self.project.widgets
        i = ws.index(w)
        j = max(0, min(len(ws) - 1, i + d))
        ws.insert(j, ws.pop(i))
        self.rebuild(w)
        self.changed.emit()

    def _dup(self):
        w = self._current()
        if w is None:
            return
        from ..render.widgets import widget_from_json
        import copy

        d = copy.deepcopy(w.to_json())
        d.pop("id")
        d["x"] += 30
        d["y"] += 30
        nw = widget_from_json(d)
        self.project.widgets.append(nw)
        self.rebuild(nw)
        self.selectRequested.emit(nw)
        self.changed.emit()

    def _del(self):
        w = self._current()
        if w is None:
            return
        self.project.widgets.remove(w)
        self.project.dirty = True
        self.rebuild()
        self.selectRequested.emit(None)
        self.changed.emit()

    def _reset(self):
        self.project.default_layout()
        for w in self.project.widgets:
            if w.TYPE == "speed_dial":
                w.props["unit"] = self.project.speed_unit
        self.rebuild()
        self.selectRequested.emit(None)
        self.changed.emit()

    def _theme(self, name):
        self.project.theme = name
        self.project.dirty = True
        self.changed.emit()

    def _accent(self, v):
        self.project.accent = v
        self.project.dirty = True
        self.changed.emit()

    def _unit(self, u):
        self.project.speed_unit = u
        for w in self.project.widgets:
            if w.TYPE == "speed_dial" and w.props.get("unit") in SPEED_UNITS:
                w.props["unit"] = u
            if w.TYPE == "value" and w.props.get("unit") in SPEED_UNITS:
                w.props["unit"] = u
        self.project.dirty = True
        self.inspector.set_widget(self.inspector.widget)
        self.changed.emit()


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
