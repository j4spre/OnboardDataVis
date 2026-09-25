# SPDX-License-Identifier: GPL-3.0-or-later
"""Overlay panel: theme, widget list, overlay files and the property inspector."""
from __future__ import annotations

import copy
import os
from functools import partial
from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QCompleter, QDoubleSpinBox, QFileDialog,
                               QFontComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from ..data.model import ROLES
from ..render.theme import THEMES
from ..render.widgets import SPEED_UNITS, WIDGET_TYPES, Widget, widget_from_json

OVERLAY_FILTER = "Onboard DataVis overlay (*.odvoverlay);;All files (*)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.svg *.tif *.tiff);;All files (*)"

# prop -> (other prop, predicate) : editor is enabled only when predicate(other value) is true
DEPENDS = {
    "min": ("range_mode", lambda v: v == "fixed"),
    "max": ("range_mode", lambda v: v == "fixed"),
    "limit_low": ("use_limits", bool),
    "limit_high": ("use_limits", bool),
    "limit_color": ("use_limits", bool),
    "ymin": ("y_mode", lambda v: v == "fixed"),
    "ymax": ("y_mode", lambda v: v == "fixed"),
    "strength": ("mode", lambda v: v == "blur"),
    "pixel_size": ("mode", lambda v: v == "pixelate"),
    "corner": ("shape", lambda v: v == "rounded"),
    "outlier_window_s": ("outlier_k", lambda v: float(v or 0) > 0),
    "outline_color": ("outline", lambda v: float(v or 0) > 0),
    "shadow_color": ("shadow", bool),
    "bg_color": ("panel", bool),
}
COLLAPSED_BY_DEFAULT = {"Appearance", "Signal filter", "Effects", "Channel 3", "Channel 4"}


def muted(text):
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, value="", placeholder="theme default"):
        super().__init__()
        self.value = value
        self.placeholder = placeholder
        self.setFixedHeight(26)
        self.clicked.connect(self._pick)
        self._show()

    def _show(self):
        if self.value:
            c = QColor(self.value)
            self.setText(c.name(QColor.NameFormat.HexArgb) if c.alpha() < 255 else c.name())
            fg = "black" if c.lightness() > 140 and c.alpha() > 100 else "white"
            self.setStyleSheet(f"background:{c.name()}; color:{fg};")
        else:
            self.setText(self.placeholder)
            self.setStyleSheet("")

    def _pick(self):
        m = QMenu(self)
        m.addAction("Choose…", self._choose)
        m.addAction("Use default", lambda: self._set(""))
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


class ChannelCombo(QComboBox):
    """Editable channel picker: roles (@speed…) first, then every channel. Red when not found."""

    def __init__(self, session, value: str):
        super().__init__()
        self.session = session
        self.setEditable(True)
        keys = [""] + ["@" + r for r in ROLES] + session.all_keys()
        if value and value not in keys:
            keys.insert(1, value)
        self.addItems(keys)
        comp = QCompleter(keys, self)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setCompleter(comp)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(14)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setCurrentText(value)
        self.currentTextChanged.connect(self._mark)
        self._mark(value)

    def _mark(self, text):
        ok = not text or self.session.is_resolved(text)
        self.setStyleSheet("" if ok else "QComboBox { border: 1px solid #ff453a; }")
        self.setToolTip("" if ok else "Not found in the loaded data – add the data or pick another channel. "
                                      "It reconnects automatically when a file with this channel is added.")


class Section(QWidget):
    """Collapsible group of rows."""

    expanded_state: Dict[str, bool] = {}

    def __init__(self, title: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(2)
        self.btn = QToolButton()
        self.btn.setText(title.replace("&", "&&"))
        self.btn.setCheckable(True)
        self.btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn.setStyleSheet("QToolButton, QToolButton:checked, QToolButton:hover { font-weight: 600; "
                               "color: #aab1ba; border: none; background: transparent; }")
        exp = self.expanded_state.get(title, title not in COLLAPSED_BY_DEFAULT)
        self.btn.setChecked(exp)
        self.btn.setArrowType(Qt.ArrowType.DownArrow if exp else Qt.ArrowType.RightArrow)
        self.btn.toggled.connect(self._toggle)
        self.title = title
        lay.addWidget(self.btn)
        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setContentsMargins(8, 0, 0, 4)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.body.setVisible(exp)
        lay.addWidget(self.body)

    def _toggle(self, on):
        self.expanded_state[self.title] = on
        self.btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)


class Inspector(QWidget):
    changed = Signal()

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        self.widget: Optional[Widget] = None
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(4, 4, 4, 4)
        self.geo = {}
        self.editors = {}
        self.set_widget(None)

    def _clear(self):
        while self.lay.count():
            it = self.lay.takeAt(0)
            wdg = it.widget()
            if wdg is not None:
                wdg.hide()
                wdg.setParent(None)
                wdg.deleteLater()

    def set_widget(self, w: Optional[Widget]):
        self.widget = w
        self._clear()
        self.geo, self.editors = {}, {}
        if w is None:
            self.lay.addWidget(muted("Select a widget on the preview (✎ Edit on) or in the list above to change "
                                     "it. Use ＋ Add or the Signals tab to create gauges from any channel."))
            self.lay.addStretch(1)
            return
        t = QLabel(w.NAME)
        t.setStyleSheet("font-weight:600; font-size:11pt")
        self.lay.addWidget(t)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color:#ff8a80")
        self.lay.addWidget(self.warn)
        self._update_warning()
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
        self.lay.addWidget(gw)
        sections: Dict[str, Section] = {}
        for prop in w.all_props():
            grp = prop.group or "General"
            if grp not in sections:
                sections[grp] = Section(grp)
                self.lay.addWidget(sections[grp])
            ed = self._editor(prop, w.props.get(prop.name, prop.default))
            self.editors[prop.name] = ed
            sections[grp].form.addRow(prop.label or prop.name, ed)
        self.lay.addStretch(1)
        self._update_enabled()

    def _editor(self, prop, val):
        name = prop.name
        if prop.kind == "channel":
            ed = ChannelCombo(self.project.session, val or "")
            ed.currentTextChanged.connect(partial(self._set, name))
        elif prop.kind == "bool":
            ed = QCheckBox()
            ed.setChecked(bool(val))
            ed.toggled.connect(partial(self._set, name))
        elif prop.kind == "float":
            ed = QDoubleSpinBox()
            ed.setRange(max(prop.minv, -1e12), min(prop.maxv, 1e12))
            dec = prop.decimals if prop.decimals >= 0 else (3 if abs(prop.default) < 10 else 1)
            ed.setDecimals(dec)
            span = min(prop.maxv, 1e6) - max(prop.minv, -1e6)
            ed.setSingleStep(0.05 if span <= 1.5 else (0.1 if span < 20 else 1))
            ed.setValue(float(val))
            ed.setKeyboardTracking(False)
            ed.valueChanged.connect(partial(self._set, name))
        elif prop.kind == "int":
            ed = QSpinBox()
            ed.setRange(int(max(prop.minv, -1e6)), int(min(prop.maxv, 1e6)))
            ed.setValue(int(val))
            ed.valueChanged.connect(partial(self._set, name))
        elif prop.kind == "choice":
            ed = QComboBox()
            ed.addItems(prop.options or [])
            ed.setCurrentText(str(val))
            ed.currentTextChanged.connect(partial(self._set, name))
        elif prop.kind == "color":
            ed = ColorButton(val, "default")
            ed.colorChanged.connect(partial(self._set, name))
        elif prop.kind == "font":
            ed = QFontComboBox()
            ed.setCurrentFont(QFont(val or "Barlow Condensed"))
            ed.currentFontChanged.connect(lambda f, n=name: self._set(n, f.family()))
        elif prop.kind == "textarea":
            ed = QPlainTextEdit(str(val))
            ed.setFixedHeight(70)
            ed.textChanged.connect(lambda e=ed, n=name: self._set(n, e.toPlainText()))
        elif prop.kind == "file":
            ed = QPushButton(os.path.basename(val) if val else "Choose file…")
            ed.setToolTip(val or "")
            ed.clicked.connect(partial(self._file, name, ed))
        else:
            ed = QLineEdit(str(val))
            ed.textChanged.connect(partial(self._set, name))
        return ed

    def _update_enabled(self):
        if self.widget is None:
            return
        for name, (dep, pred) in DEPENDS.items():
            if name in self.editors and dep in self.widget.props:
                try:
                    self.editors[name].setEnabled(bool(pred(self.widget.props.get(dep))))
                except (TypeError, ValueError):
                    pass

    def _update_warning(self):
        if self.widget is None:
            return
        miss = self.widget.missing_channels(self.project.session)
        self.warn.setText(("⚠ Not found in the loaded data: " + ", ".join(miss) +
                           ". The widget stays and reconnects when matching data is added.") if miss else "")
        self.warn.setVisible(bool(miss))

    def refresh_geometry(self):
        if self.widget is None:
            return
        for k, sp in self.geo.items():
            sp.blockSignals(True)
            sp.setValue(int(getattr(self.widget, k)))
            sp.blockSignals(False)

    def refresh_channels(self):
        """Data was added/removed: rebuild so channel lists and warnings are current."""
        self.set_widget(self.widget)

    def _geo(self, k, v):
        if self.widget is not None:
            setattr(self.widget, k, float(v))
            self.project.dirty = True
            self.changed.emit()

    def _set(self, name, v):
        if self.widget is not None:
            self.widget.props[name] = v
            self.project.dirty = True
            self._update_enabled()
            pdef = self.widget.prop(name)
            if pdef is not None and pdef.kind == "channel":
                self._update_warning()
            self.changed.emit()

    def _file(self, name, btn):
        start = self.widget.props.get(name) or ""
        path, _ = QFileDialog.getOpenFileName(self, "Image", os.path.dirname(start), IMAGE_FILTER)
        if path:
            btn.setText(os.path.basename(path))
            btn.setToolTip(path)
            self._set(name, path)


class WidgetsPanel(QWidget):
    changed = Signal()
    selectRequested = Signal(object)
    overlayLoaded = Signal()

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
        add = QPushButton("＋ Add")
        add.setObjectName("primary")
        add.setMenu(self.add_menu(add))
        bar.addWidget(add)
        for txt, tip, fn in (("▲", "Bring forward", partial(self._move, 1)), ("▼", "Send backward", partial(self._move, -1)),
                             ("⧉", "Duplicate", self._dup), ("✕", "Delete", self._del)):
            b = QToolButton()
            b.setText(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        ov = QToolButton()
        ov.setText("Overlay file ▾")
        ov.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(ov)
        m.addAction("Save overlay as…", self.save_overlay)
        m.addAction("Load overlay (replace)…", partial(self.load_overlay, True))
        m.addAction("Add widgets from overlay file…", partial(self.load_overlay, False))
        m.addSeparator()
        m.addAction("Reset to default layout", self._reset)
        ov.setMenu(m)
        bar.addWidget(ov)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.list = QListWidget()
        self.list.setMaximumHeight(180)
        self.list.currentRowChanged.connect(self._row)
        self.list.itemChanged.connect(self._item_changed)
        lay.addWidget(self.list)
        self.inspector = Inspector(project)
        self.inspector.changed.connect(self._inspector_changed)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setWidget(self.inspector)
        lay.addWidget(sc, 1)
        self.rebuild()

    # ------------------------------------------------------------------ menus
    def add_menu(self, parent) -> QMenu:
        m = QMenu(parent)
        cats = {}
        for t, cls in WIDGET_TYPES.items():
            cats.setdefault(cls.CATEGORY, []).append((t, cls))
        for cat in ("Data", "Motorsport", "Media"):
            if cat not in cats:
                continue
            m.addSection(cat)
            for t, cls in cats[cat]:
                if t == "image":
                    m.addAction(cls.NAME + "…", self.insert_image)
                else:
                    m.addAction(cls.NAME, partial(self.add_widget, t))
        return m

    # ------------------------------------------------------------------ list
    def set_project(self, project):
        self.project = project
        self.inspector.project = project
        self.theme.blockSignals(True)
        self.theme.setCurrentText(project.theme)
        self.theme.blockSignals(False)
        self.unit.blockSignals(True)
        self.unit.setCurrentText(project.speed_unit)
        self.unit.blockSignals(False)
        self.accent.value = project.accent
        self.accent._show()
        self.rebuild()

    def _title(self, w: Widget) -> str:
        extra = ""
        if w.TYPE in ("value", "bar", "dial"):
            extra = w.props.get("label") or (w.props.get("channel") or "").split(":", 1)[-1]
        elif w.TYPE == "text":
            extra = (w.props.get("text") or "").split("\n")[0][:24]
        elif w.TYPE == "image":
            extra = os.path.basename(w.props.get("path") or "")
        elif w.TYPE == "graph":
            extra = w.props.get("label") or ""
        miss = "⚠ " if w.missing_channels(self.project.session) else ""
        return f"{miss}{w.NAME}" + (f" – {extra}" if extra else "")

    def rebuild(self, select: Optional[Widget] = None):
        self.list.blockSignals(True)
        self.list.clear()
        for w in reversed(self.project.widgets):
            it = QListWidgetItem(self._title(w))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if w.props.get("visible", True) else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, w.id)
            if w.missing_channels(self.project.session):
                it.setForeground(QColor("#ff8a80"))
                it.setToolTip("Some channels are not in the loaded data")
            self.list.addItem(it)
        self.list.blockSignals(False)
        if select is not None:
            self.show_selection(select)
        else:
            self.inspector.set_widget(None)

    def refresh_titles(self):
        for i in range(self.list.count()):
            it = self.list.item(i)
            w = self._widget_by_id(it.data(Qt.ItemDataRole.UserRole))
            if w is not None:
                self.list.blockSignals(True)
                it.setText(self._title(w))
                miss = bool(w.missing_channels(self.project.session))
                it.setForeground(QColor("#ff8a80") if miss else self.list.palette().text().color())
                self.list.blockSignals(False)

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

    def _inspector_changed(self):
        self.refresh_titles()
        self.changed.emit()

    # ------------------------------------------------------------------ add / edit
    def insert(self, w: Widget):
        self.project.widgets.append(w)
        self.project.dirty = True
        self.rebuild(w)
        self.selectRequested.emit(w)
        self.changed.emit()

    def add_widget(self, t: str, props: Optional[dict] = None) -> Widget:
        cls = WIDGET_TYPES[t]
        W, H = cls.DEFAULT_SIZE
        w = cls(1920 / 2 - W / 2, self.project.ref_height / 2 - H / 2, props=props)
        if t == "speed_dial":
            w.props["unit"] = self.project.speed_unit
        self.insert(w)
        return w

    def insert_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Insert image", "", IMAGE_FILTER)
        if not path:
            return
        from PySide6.QtGui import QImage

        img = QImage(path)
        w, h = 360.0, 240.0
        if not img.isNull():
            s = min(480.0 / img.width(), 320.0 / img.height(), 1920.0 / img.width())
            w, h = img.width() * s, img.height() * s
        wdg = WIDGET_TYPES["image"](1920 / 2 - w / 2, self.project.ref_height / 2 - h / 2, w, h, {"path": path})
        self.insert(wdg)

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
        d = copy.deepcopy(w.to_json())
        d.pop("id")
        d["x"] += 30
        d["y"] += 30
        self.insert(widget_from_json(d))

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

    # ------------------------------------------------------------------ overlay files
    def _dir(self):
        pr = self.project
        if pr.path:
            return os.path.dirname(pr.path)
        if pr.video:
            return os.path.dirname(pr.video.path)
        return os.path.expanduser("~")

    def save_overlay(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save overlay", os.path.join(self._dir(), "overlay.odvoverlay"),
                                              OVERLAY_FILTER)
        if not path:
            return
        if not path.lower().endswith(".odvoverlay"):
            path += ".odvoverlay"
        self.project.save_overlay(path)
        self.window().statusBar().showMessage(f"Overlay saved to {path}", 6000)

    def load_overlay(self, replace: bool = True):
        from PySide6.QtWidgets import QMessageBox

        path, _ = QFileDialog.getOpenFileName(self, "Load overlay", self._dir(), OVERLAY_FILTER)
        if not path:
            return
        try:
            rep = self.project.load_overlay(path, replace=replace)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot load overlay", str(exc))
            return
        self.set_project(self.project)
        self.selectRequested.emit(None)
        self.overlayLoaded.emit()
        self.changed.emit()
        msg = f"Loaded {rep['widgets']} widgets from {os.path.basename(path)}."
        if rep["missing"]:
            QMessageBox.information(
                self, "Overlay loaded",
                msg + "\n\nThese channels are not in the loaded data yet:\n  " + "\n  ".join(rep["missing"][:15]) +
                ("\n  …" if len(rep["missing"]) > 15 else "") +
                "\n\nThe widgets are kept (marked ⚠). They connect automatically when you add a file with "
                "matching channel names, or pick other channels in the inspector.")
        else:
            self.window().statusBar().showMessage(msg + " All channels matched.", 8000)

    # ------------------------------------------------------------------ style
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
            if w.TYPE in ("speed_dial", "value", "dial", "bar") and w.props.get("unit") in SPEED_UNITS:
                w.props["unit"] = u
        self.project.dirty = True
        self.inspector.set_widget(self.inspector.widget)
        self.changed.emit()
