# SPDX-License-Identifier: GPL-3.0-or-later
"""Signals browser: search every channel of every loaded file and turn it into a widget."""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..data.model import channel_key
from ..render.base import nice_range


class SignalsPanel(QWidget):
    """Emits ``createRequested(widget_type, props)``."""

    createRequested = Signal(str, dict)
    addToPlotRequested = Signal(str)

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search channels… (e.g. temp, apps, wheelspeed)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Channel", "Unit"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._current)
        self.tree.itemDoubleClicked.connect(lambda it, c: self._create("value"))
        lay.addWidget(self.tree, 1)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        lay.addWidget(QLabel("Add selected channel as:"))
        row = QHBoxLayout()
        self.buttons = []
        for label, t in (("Numeric", "value"), ("Bar", "bar"), ("Dial", "dial"), ("Live plot", "graph")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, t=t: self._create(t))
            row.addWidget(b)
            self.buttons.append(b)
        lay.addLayout(row)
        self.add_plot = QPushButton("＋ Add to selected live plot")
        self.add_plot.setToolTip("Adds the channel to the next free slot of the live plot selected in the Overlay panel")
        self.add_plot.clicked.connect(self._add_to_plot)
        lay.addWidget(self.add_plot)
        tip = QLabel("Double-click a channel for a numeric readout. Every gauge has scaling, range, limits and "
                     "low-pass / outlier filters in the inspector.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.rebuild()

    def set_project(self, project):
        self.project = project
        self.rebuild()

    def rebuild(self):
        self.tree.clear()
        for s in self.project.session.sources:
            top = QTreeWidgetItem([f"{s.name}", ""])
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            top.setFirstColumnSpanned(True)
            names = sorted(s.channel_names(), key=str.lower)
            for n in names:
                it = QTreeWidgetItem([n, s.unit_of(n)])
                it.setData(0, Qt.ItemDataRole.UserRole, channel_key(s.id, n))
                top.addChild(it)
            self.tree.addTopLevelItem(top)
            top.setExpanded(len(self.project.session.sources) <= 2)
        if not self.project.session.sources:
            self.info.setText("No data loaded.")
        self._filter(self.search.text())

    def _filter(self, text):
        text = (text or "").lower().strip()
        terms = text.split()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            shown = 0
            for j in range(top.childCount()):
                ch = top.child(j)
                hay = (ch.text(0) + " " + ch.text(1)).lower()
                ok = all(t in hay for t in terms)
                ch.setHidden(not ok)
                shown += ok
            top.setText(0, f"{top.text(0).split('  (')[0]}  ({shown})")
            if terms:
                top.setExpanded(True)

    def selected_key(self) -> Optional[str]:
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None

    def _current(self, it, prev=None):
        key = it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None
        for b in self.buttons + [self.add_plot]:
            b.setEnabled(bool(key))
        if not key:
            self.info.setText("")
            return
        src, ch = self.project.session.resolve(key)
        if ch is None or len(ch) == 0:
            self.info.setText("(empty channel)")
            return
        st = ch.stats()
        rate = (len(ch) - 1) / max(ch.t[-1] - ch.t[0], 1e-9)
        self.info.setText(f"{ch.name} · {len(ch):,} samples · ~{rate:.0f} Hz · range {st['min']:.4g} … {st['max']:.4g}"
                          f" (typ. {st['p01']:.4g} … {st['p99']:.4g}) {ch.unit}")

    def _create(self, t):
        key = self.selected_key()
        if not key:
            return
        src, ch = self.project.session.resolve(key)
        name = key.split(":", 1)[-1]
        unit = ch.unit if ch is not None else ""
        props = {}
        if t == "graph":
            props = {"ch1": key, "ch2": "", "ch3": "", "ch4": "", "label": name, "legend": True}
        else:
            decimals = 0
            if ch is not None and len(ch):
                st = ch.stats()
                span = abs(st["p99"] - st["p01"])
                decimals = 0 if span >= 50 else (1 if span >= 5 else 2)
                lo, hi = nice_range(st["p01"], st["p99"])
                props.update(min=float(lo), max=float(hi), limit_low=float(lo), limit_high=float(hi))
            props.update(channel=key, label=name, unit=unit, decimals=decimals, range_mode="auto")
        self.createRequested.emit(t, props)

    def _add_to_plot(self):
        key = self.selected_key()
        if key:
            self.addToPlotRequested.emit(key)
