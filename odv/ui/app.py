# SPDX-License-Identifier: GPL-3.0-or-later
"""Main window."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time

import numpy as np
from PySide6.QtCore import QElapsedTimer, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QSlider, QTabWidget, QToolButton, QVBoxLayout, QWidget, QComboBox)

from .. import __version__, sync
from ..data.loaders import detect_kind, sniff_csv
from ..project import Project
from ..render.theme import load_fonts
from .canvas import PreviewCanvas
from .dialogs import CsvImportDialog, ExportDialog
from .panels import DataPanel, LapsPanel, RolesPanel, WidgetsPanel
from .signals_panel import SignalsPanel
from .style import apply_dark
from .timeline import Timeline, fmt_tc
from .workers import Job, start_job

DATA_FILTER = ("Telemetry (*.csv *.txt *.tsv *.mf4 *.mdf *.dat *.vbo *.gpx);;RaceBox / CSV (*.csv *.txt *.tsv);;"
               "MF4 / MDF (*.mf4 *.mdf *.dat);;VBO (*.vbo);;GPX (*.gpx);;All files (*)")
VIDEO_FILTER = "Video (*.mp4 *.mov *.m4v *.avi *.mkv *.MP4 *.MOV *.insv *.360);;All files (*)"


def motion_cache_path(video_path: str, tf=None) -> str:
    st = os.stat(video_path)
    o = f"{tf.rot90}|{tf.flip_h}|{tf.flip_v}" if tf is not None else "0|False|False"
    h = hashlib.sha1(f"{os.path.abspath(video_path)}|{st.st_size}|{int(st.st_mtime)}|{o}".encode()).hexdigest()[:16]
    d = os.path.join(tempfile.gettempdir(), "onboard_datavis_cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"motion_{h}.npz")


class MainWindow(QMainWindow):
    def __init__(self, project_path=None):
        super().__init__()
        self.settings = QSettings("OnboardDataVis", "OnboardDataVis")
        self.project = Project()
        self.motion = None
        self.playing = False
        self.play_rate = 1.0
        self.clock = QElapsedTimer()
        self.play_t0 = 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)

        self.setWindowTitle("Onboard DataVis")
        self.resize(1600, 960)
        self._build()
        self._menus()
        self._refresh_all()
        if project_path:
            QTimer.singleShot(50, lambda: self.open_project(project_path))

    # ------------------------------------------------------------------ UI
    def _build(self):
        pr = self.project
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)
        self.canvas = PreviewCanvas(pr)
        self.canvas.selectionChanged.connect(self._canvas_selection)
        self.canvas.layoutChanged.connect(self._layout_changed)
        v.addWidget(self.canvas, 1)

        tr = QHBoxLayout()
        tr.setSpacing(4)

        def tb(text, tip, fn, checkable=False):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.clicked.connect(fn)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setMinimumWidth(max(40, b.fontMetrics().horizontalAdvance(text) + 30))
            tr.addWidget(b)
            return b

        tb("⏮", "Go to start (Home)", lambda: self.seek(0))
        tb("◀|", "Previous frame (←)", lambda: self.step(-1))
        self.play_btn = tb("▶", "Play / pause (Space)", self.toggle_play)
        self.play_btn.setMinimumWidth(56)
        tb("|▶", "Next frame (→)", lambda: self.step(1))
        self.rate = QComboBox()
        self.rate.addItems(["0.25×", "0.5×", "1×", "2×"])
        self.rate.setCurrentText("1×")
        self.rate.currentTextChanged.connect(self._rate)
        tr.addWidget(self.rate)
        self.time_label = QLabel("0:00.00")
        self.time_label.setStyleSheet("font-family: Consolas, monospace; font-size: 11pt; padding: 0 10px;")
        self.time_label.setMinimumWidth(210)
        tr.addWidget(self.time_label)
        self.data_label = QLabel("")
        self.data_label.setObjectName("muted")
        tr.addWidget(self.data_label, 1)
        self.edit_btn = tb("✎ Edit", "Move / resize gauges on the preview", self._toggle_edit, True)
        self.edit_btn.setChecked(True)
        self.ov_btn = tb("Overlay", "Show / hide overlay", self._toggle_overlay, True)
        self.ov_btn.setChecked(True)
        exp = QPushButton("Export video…")
        exp.setObjectName("primary")
        exp.clicked.connect(self.export)
        tr.addWidget(exp)
        v.addLayout(tr)

        self.timeline = Timeline(pr)
        self.timeline.setMinimumHeight(170)
        self.timeline.seek.connect(self.scrub)
        self.timeline.trimChanged.connect(lambda a, b: self._status(f"Trim {fmt_tc(a)} – {fmt_tc(b)}"))
        v.addWidget(self.timeline)
        self.setCentralWidget(central)

        self.data_panel = DataPanel(pr)
        self.data_panel.offsetsChanged.connect(self._offsets_changed)
        self.data_panel.sourcesChanged.connect(self._sources_changed)
        self.data_panel.motionRequested.connect(self.run_motion_sync)
        self.data_panel.alignRequested.connect(self.run_align)
        self.data_panel.timestampRequested.connect(self.run_timestamp_sync)
        self.data_panel.addDataRequested.connect(self.add_data)
        self.data_panel.openVideoRequested.connect(self.open_video)
        self.data_panel.videoTransformChanged.connect(self._video_tf_changed)
        self.signals_panel = SignalsPanel(pr)
        self.signals_panel.createRequested.connect(self._create_from_signal)
        self.signals_panel.addToPlotRequested.connect(self._add_to_plot)
        self.roles_panel = RolesPanel(pr)
        self.roles_panel.rolesChanged.connect(self._roles_changed)
        self.laps_panel = LapsPanel(pr)
        self.laps_panel.seekRequested.connect(self.seek)
        self.laps_panel.lapsChanged.connect(self._laps_changed)
        self.widgets_panel = WidgetsPanel(pr)
        self.widgets_panel.changed.connect(self._widgets_changed)
        self.widgets_panel.selectRequested.connect(self._panel_select)
        self.widgets_panel.overlayLoaded.connect(self._overlay_loaded)

        left = QTabWidget()
        left.addTab(self.data_panel, "Data && Sync")
        left.addTab(self.laps_panel, "Laps")
        left.addTab(self.signals_panel, "Signals")
        left.addTab(self.roles_panel, "Roles")
        d1 = QDockWidget("Project", self)
        d1.setObjectName("dock_project")
        d1.setWidget(left)
        d1.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        d1.setMinimumWidth(360)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, d1)
        d2 = QDockWidget("Overlay", self)
        d2.setObjectName("dock_overlay")
        d2.setWidget(self.widgets_panel)
        d2.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        d2.setMinimumWidth(330)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d2)
        self.left_tabs = left
        self.statusBar().showMessage("Ready")

    def _menus(self):
        mb = self.menuBar()
        f = mb.addMenu("&File")

        def act(menu, text, fn, sc=None):
            a = QAction(text, self)
            if sc:
                a.setShortcut(QKeySequence(sc))
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        act(f, "New project", self.new_project, "Ctrl+N")
        act(f, "Open project…", self.open_project_dialog, "Ctrl+O")
        act(f, "Save project", self.save, "Ctrl+S")
        act(f, "Save project as…", self.save_as, "Ctrl+Shift+S")
        f.addSeparator()
        act(f, "Open video…", self.open_video, "Ctrl+Shift+O")
        act(f, "Add data…", self.add_data, "Ctrl+D")
        f.addSeparator()
        act(f, "Export video…", self.export, "Ctrl+E")
        f.addSeparator()
        act(f, "Quit", self.close, "Ctrl+Q")
        pb = mb.addMenu("&Playback")
        act(pb, "Play / pause", self.toggle_play, "Space")
        act(pb, "Next frame", lambda: self.step(1), "Right")
        act(pb, "Previous frame", lambda: self.step(-1), "Left")
        act(pb, "Forward 1 s", lambda: self.seek(self.canvas.t + 1), "Shift+Right")
        act(pb, "Back 1 s", lambda: self.seek(self.canvas.t - 1), "Shift+Left")
        act(pb, "Go to start", lambda: self.seek(0), "Home")
        act(pb, "Go to end", lambda: self.seek(self.project.duration), "End")
        pb.addSeparator()
        act(pb, "Set trim in", self.set_in, "I")
        act(pb, "Set trim out", self.set_out, "O")
        act(pb, "Clear trim", self.clear_trim)
        sm = mb.addMenu("&Sync")
        act(sm, "Auto-sync primary from video motion", lambda: self.run_motion_sync(self.project.primary_source or ""))
        act(sm, "Align all other sources to primary", self.align_all)
        sm.addSeparator()
        act(sm, "Data +1 frame", lambda: self.nudge(+1), "]")
        act(sm, "Data −1 frame", lambda: self.nudge(-1), "[")
        act(sm, "Data +0.1 s", lambda: self.nudge(0, 0.1), "Shift+]")
        act(sm, "Data −0.1 s", lambda: self.nudge(0, -0.1), "Shift+[")
        im = mb.addMenu("&Insert")
        for t, label in (("value", "Numeric value"), ("bar", "Bar"), ("dial", "Dial gauge"), ("graph", "Live plot")):
            act(im, label, lambda _=False, t=t: self.widgets_panel.add_widget(t))
        im.addSeparator()
        act(im, "Text", lambda: self.widgets_panel.add_widget("text"), "Ctrl+T")
        act(im, "Image…", self.widgets_panel.insert_image, "Ctrl+I")
        act(im, "Blur region", lambda: self.widgets_panel.add_widget("blur"), "Ctrl+B")
        act(im, "Pixelate region", lambda: self.widgets_panel.add_widget("blur", {"mode": "pixelate", "edge": 0}))
        im.addSeparator()
        sub = im.addMenu("Motorsport gauge")
        from ..render.widgets import WIDGET_TYPES as _WT

        for t, cls in _WT.items():
            if cls.CATEGORY == "Motorsport":
                act(sub, cls.NAME, lambda _=False, t=t: self.widgets_panel.add_widget(t))
        om = mb.addMenu("O&verlay")
        act(om, "Save overlay as…", self.widgets_panel.save_overlay, "Ctrl+Shift+E")
        act(om, "Load overlay (replace)…", lambda: self.widgets_panel.load_overlay(True), "Ctrl+L")
        act(om, "Add widgets from overlay file…", lambda: self.widgets_panel.load_overlay(False))
        om.addSeparator()
        act(om, "Reset to default layout", self.widgets_panel._reset)
        vm = mb.addMenu("&View")
        act(vm, "Toggle layout editing", lambda: self.edit_btn.click(), "E")
        act(vm, "Toggle overlay", lambda: self.ov_btn.click(), "H")
        act(vm, "Reset timeline zoom", self.timeline.reset_view, "Z")
        hm = mb.addMenu("&Help")
        act(hm, "Keyboard shortcuts", self.show_shortcuts)
        act(hm, "About", self.about)

    # ------------------------------------------------------------------ helpers
    def _status(self, msg, ms=6000):
        self.statusBar().showMessage(msg, ms)

    def _refresh_all(self):
        pr = self.project
        self.canvas.set_project(pr)
        self.timeline.project = pr
        self.timeline.motion = (self.motion[0], self.motion[1]) if self.motion else None
        self.timeline.reset_view()
        for pnl in (self.data_panel, self.roles_panel, self.laps_panel, self.widgets_panel, self.signals_panel):
            pnl.set_project(pr)
        self._update_title()
        self.seek(self.canvas.t)

    def _update_title(self):
        name = os.path.basename(self.project.path) if self.project.path else "Untitled"
        self.setWindowTitle(f"{name}{' •' if self.project.dirty else ''} – Onboard DataVis {__version__}")

    def _load_cached_motion(self):
        self.motion = None
        if self.project.video:
            try:
                cp = motion_cache_path(self.project.video.path, self.project.video_tf)
                if os.path.exists(cp):
                    z = np.load(cp)
                    self.motion = (z["t"], z["yaw"], z["mag"])
            except Exception:
                self.motion = None
        self.timeline.motion = (self.motion[0], self.motion[1]) if self.motion else None
        self.timeline.invalidate()

    # ------------------------------------------------------------------ playback
    def seek(self, t):
        if self.playing:
            self.play_t0 = t
            self.clock.restart()
        self.canvas.set_time(t)
        self._time_changed()

    def scrub(self, t):
        """Coalesce rapid seek requests (timeline dragging) so decoding never lags behind the mouse."""
        self._pending_seek = t
        if not getattr(self, "_scrub_timer", None):
            self._scrub_timer = QTimer(self)
            self._scrub_timer.setSingleShot(True)
            self._scrub_timer.timeout.connect(lambda: self.seek(self._pending_seek))
        if not self._scrub_timer.isActive():
            self._scrub_timer.start(0)

    def step(self, n):
        fd = self.project.video.frame_dur if self.project.video else 1 / 30
        self.seek(self.canvas.frame_t + n * fd + 1e-4)

    def toggle_play(self):
        if not self.project.video:
            return
        self.playing = not self.playing
        self.play_btn.setText("❚❚" if self.playing else "▶")
        if self.playing:
            if self.canvas.t >= self.project.duration - 0.05:
                self.canvas.set_time(0)
            self.play_t0 = self.canvas.t
            self.clock.start()
            self.timer.start(int(1000 / min(60, self.project.video.fps)))
        else:
            self.timer.stop()

    def _tick(self):
        t = self.play_t0 + self.clock.elapsed() / 1000.0 * self.play_rate
        if t >= self.project.duration:
            t = self.project.duration
            self.toggle_play()
        self.canvas.set_time(t)
        self._time_changed()

    def _rate(self, txt):
        self.play_t0 = self.canvas.t
        self.clock.restart()
        self.play_rate = float(txt.replace("×", ""))

    def _time_changed(self):
        t = self.canvas.frame_t
        fps = self.project.video.fps if self.project.video else 30
        self.time_label.setText(f"{fmt_tc(t)}  f{int(round(t * fps))}")
        prim = self.project.primary()
        if prim is not None:
            dt = t + prim.offset
            inside = prim.t_start <= dt <= prim.t_end
            self.data_label.setText(f"data {dt:.2f} s" + ("" if inside else "  (outside log)"))
        self.timeline.set_time(t)
        self.data_panel.set_current_time(t)
        self.laps_panel.current_t = t

    # ------------------------------------------------------------------ change handlers
    def _canvas_selection(self, w):
        self.widgets_panel.show_selection(w)

    def _panel_select(self, w):
        self.canvas.selected = w
        self.canvas.update()

    def _layout_changed(self):
        self.widgets_panel.inspector.refresh_geometry()
        if self.canvas.selected is None or self.widgets_panel.list.count() != len(self.project.widgets):
            self.widgets_panel.rebuild(self.canvas.selected)
        self._update_title()

    def _widgets_changed(self):
        self.canvas.update()
        self._update_title()

    def _offsets_changed(self):
        self.timeline.invalidate()
        self.laps_panel.rebuild()
        self.canvas.update()
        self._time_changed()
        self._update_title()

    def _sources_changed(self):
        self.project.session.invalidate()
        self.signals_panel.rebuild()
        self.widgets_panel.refresh_titles()
        self.widgets_panel.inspector.refresh_channels()
        self.roles_panel.rebuild()
        self.laps_panel.rebuild()
        self.data_panel.refresh_laps()
        self.timeline.invalidate()
        self.canvas.update()
        self._update_title()

    def _roles_changed(self):
        self.widgets_panel.refresh_titles()
        self.laps_panel.rebuild()
        self.data_panel.refresh_laps()
        self.timeline.invalidate()
        self.canvas.renderer = self.canvas.renderer.__class__(self.project)
        self.canvas.update()
        self._update_title()

    def _video_tf_changed(self):
        self._load_cached_motion()
        self.canvas.set_time(self.canvas.t)
        self.widgets_panel.inspector.refresh_geometry()
        self.canvas.update()
        self._update_title()

    def _overlay_loaded(self):
        self.roles_panel.rebuild()
        self.laps_panel.rebuild()
        self.data_panel.refresh_laps()
        self.timeline.invalidate()
        self.canvas.renderer = self.canvas.renderer.__class__(self.project)
        self.canvas.selected = None
        self.canvas.update()
        self._update_title()

    def _create_from_signal(self, t, props):
        self.widgets_panel.add_widget(t, props)
        self.edit_btn.setChecked(True)
        self._toggle_edit()

    def _add_to_plot(self, key):
        w = self.widgets_panel.inspector.widget
        if w is None or w.TYPE != "graph":
            self.widgets_panel.add_widget("graph", {"ch1": key, "ch2": "", "ch3": "", "ch4": "",
                                                    "label": key.split(":", 1)[-1]})
            return
        for i in range(1, 5):
            if not w.props.get(f"ch{i}"):
                w.props[f"ch{i}"] = key
                break
        else:
            w.props["ch4"] = key
        self.project.dirty = True
        self.widgets_panel.inspector.set_widget(w)
        self.widgets_panel.refresh_titles()
        self.canvas.update()

    def _laps_changed(self):
        self.data_panel.refresh_laps()
        self.timeline.invalidate()
        self.canvas.update()

    def _toggle_edit(self):
        self.canvas.edit_mode = self.edit_btn.isChecked()
        if not self.canvas.edit_mode:
            self.canvas.select(None)
        self.canvas.update()

    def _toggle_overlay(self):
        self.canvas.show_overlay = self.ov_btn.isChecked()
        self.canvas.update()

    # ------------------------------------------------------------------ project I/O
    def _last_dir(self):
        return self.settings.value("last_dir", os.path.expanduser("~"))

    def _remember_dir(self, path):
        self.settings.setValue("last_dir", os.path.dirname(path))

    def _confirm_discard(self):
        if not self.project.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save changes to the current project?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                 QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def new_project(self):
        if not self._confirm_discard():
            return
        self.project = Project()
        self.motion = None
        self._refresh_all()

    def open_project_dialog(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self._last_dir(), "Onboard DataVis project (*.odv)")
        if path:
            self.open_project(path)

    def open_project(self, path):
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self.project = Project.load(path)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Cannot open project", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self._remember_dir(path)
        self._load_cached_motion()
        self._refresh_all()
        self._status(f"Opened {path}")

    def save(self):
        if not self.project.path:
            return self.save_as()
        self.project.save()
        self._update_title()
        self._status("Saved")
        return True

    def save_as(self):
        start = self.project.path or (os.path.splitext(self.project.video.path)[0] + ".odv" if self.project.video
                                      else os.path.join(self._last_dir(), "project.odv"))
        path, _ = QFileDialog.getSaveFileName(self, "Save project", start, "Onboard DataVis project (*.odv)")
        if not path:
            return False
        if not path.lower().endswith(".odv"):
            path += ".odv"
        self.project.save(path)
        self._remember_dir(path)
        self._update_title()
        self._status(f"Saved {path}")
        return True

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open video", self._last_dir(), VIDEO_FILTER)
        if not path:
            return
        self._remember_dir(path)
        try:
            self.project.set_video(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open video", str(exc))
            return
        first_layout = not self.project.widgets
        if first_layout:
            self.project.default_layout()
        self._load_cached_motion()
        self._refresh_all()
        self.seek(0)
        if self.project.session.sources:
            self._offer_autosync()

    def add_data(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add data", self._last_dir(), DATA_FILTER)
        if not paths:
            return
        self._remember_dir(paths[0])
        added = []
        for path in paths:
            opts = None
            kind = detect_kind(path)
            if kind == "csv":
                sn = sniff_csv(path)
                is_rb = "GForceX" in sn["columns"] or "racebox" in os.path.basename(path).lower() or any(
                    "racebox" in l.lower() for l in sn["meta_lines"])
                if not is_rb:
                    dlg = CsvImportDialog(path, self)
                    if dlg.exec() != dlg.DialogCode.Accepted:
                        continue
                    opts = dlg.mapping()
            try:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                src = self.project.add_data(path, options=opts)
                added.append(src)
            except Exception as exc:
                QMessageBox.critical(self, "Cannot load data", f"{os.path.basename(path)}\n\n{exc}")
            finally:
                QApplication.restoreOverrideCursor()
        if not added:
            return
        if not self.project.widgets or len(self.project.session.sources) == len(added):
            self.project.default_layout()
        self._refresh_all()
        self._status(f"Loaded {', '.join(s.name for s in added)}")
        self._offer_autosync(added)

    def _offer_autosync(self, added=None):
        pr = self.project
        if not pr.video or not pr.session.sources:
            return
        prim = pr.primary()
        if added is None or prim in added:
            ts = sync.timestamp_offset(pr.video.creation_time, prim)
            in_range = ts is not None and prim.t_start - 5 <= ts.offset <= prim.t_end
            if in_range:
                pr.set_offset(prim.id, ts.offset, propagate=False)
                prim.meta["sync_note"] = "Synced from timestamps (" + ts.detail + "). Check and fine-tune."
                self.data_panel.refresh()
            else:
                r = QMessageBox.question(
                    self, "Synchronise",
                    ("The video has no usable timestamp for this log." if ts is None else
                     "The video timestamp does not fall inside this log (" + ts.detail + ").") +
                    "\n\nRun automatic sync from video motion now? (analyses the camera movement – takes "
                    "a little while for long videos)")
                if r == QMessageBox.StandardButton.Yes:
                    self.run_motion_sync(prim.id, then_align=True)
                    return
        self.align_all()

    # ------------------------------------------------------------------ sync jobs
    def run_timestamp_sync(self, sid):
        src = self.project.session.source(sid)
        res = sync.timestamp_offset(self.project.video.creation_time if self.project.video else None, src)
        if res is None:
            QMessageBox.information(self, "Timestamps", "The video or the log has no absolute timestamp.")
            return
        self.project.set_offset(sid, res.offset)
        src.meta["sync_note"] = "Timestamps: " + res.detail
        self.data_panel.refresh()
        self._offsets_changed()

    def run_motion_sync(self, sid, then_align=False):
        pr = self.project
        src = pr.session.source(sid)
        if src is None or pr.video is None:
            QMessageBox.information(self, "Auto-sync", "Load a video and a data file first.")
            return
        if sync.data_yaw_rate(pr.session, src) is None:
            QMessageBox.information(self, "Auto-sync", "This source has no gyro (yaw rate) or GPS heading to "
                                    "match against. Use the manual sync tools.")
            return
        card = self.data_panel.card(sid)

        def work(job):
            mo = self.motion
            if mo is None:
                job.progress.emit(0.0, "Analysing video motion…")
                from ..video import transform_array

                vinfo, vtf = pr.video, pr.video_tf
                mo = sync.motion_signal(pr.video.path, progress=lambda f: job.progress.emit(f * 0.9, "Analysing video motion…"),
                                        cancelled=job.cancelled, orient=lambda g: transform_array(g, vinfo, vtf))
                try:
                    np.savez(motion_cache_path(pr.video.path, vtf), t=mo[0], yaw=mo[1], mag=mo[2])
                except Exception:
                    pass
            job.progress.emit(0.92, "Matching with data…")
            res = sync.motion_sync(pr.session, src, mo)
            return mo, res

        job = Job(work)
        if card:
            card.prog.setVisible(True)
            card.btn_motion.setEnabled(False)

        def done(out):
            mo, res = out
            self.motion = mo
            self.timeline.motion = (mo[0], mo[1])
            c = self.data_panel.card(sid)
            if c:
                c.prog.setVisible(False)
                c.btn_motion.setEnabled(True)
            if res is None:
                QMessageBox.warning(self, "Auto-sync", "Could not find a match. Try the manual sync point.")
                return
            pr.set_offset(sid, res.offset)
            src.meta["sync_note"] = f"Motion sync: {res.detail} · confidence {res.confidence}"
            self.data_panel.refresh()
            self._offsets_changed()
            self._status(f"Auto-sync: data t = {res.offset:.2f} s at video start ({res.confidence} confidence)")
            if then_align:
                self.align_all()

        def fail(err):
            c = self.data_panel.card(sid)
            if c:
                c.prog.setVisible(False)
                c.btn_motion.setEnabled(True)
            QMessageBox.warning(self, "Auto-sync failed", err)

        start_job(self, job, on_progress=lambda f, s: (card.prog.setValue(int(f * 100)) if card else None,
                                                       self._status(s)), on_done=done, on_fail=fail)

    def run_align(self, sid):
        pr = self.project
        prim, other = pr.primary(), pr.session.source(sid)
        if prim is None or other is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            res = sync.align_sources(pr.session, prim, other)
        finally:
            QApplication.restoreOverrideCursor()
        if res is None:
            QMessageBox.warning(self, "Align", f"No speed channel found in {other.name}. Set one under Channels, "
                                "or type the offset manually.")
            return
        other.offset = res.offset
        other.meta["sync_note"] = f"{res.detail} · confidence {res.confidence}"
        pr.dirty = True
        self.data_panel.refresh()
        self._offsets_changed()

    def align_all(self):
        pr = self.project
        for s in pr.session.sources:
            if s.id != pr.primary_source:
                self.run_align(s.id)

    def nudge(self, frames=0, seconds=0.0):
        prim = self.project.primary()
        if prim is None:
            return
        fd = self.project.video.frame_dur if self.project.video else 1 / 30
        self.project.set_offset(prim.id, prim.offset + frames * fd + seconds)
        self.data_panel.refresh()
        self._offsets_changed()
        self._status(f"Offset {prim.offset:.3f} s")

    # ------------------------------------------------------------------ trim / export
    def set_in(self):
        self.project.trim_in = min(self.canvas.frame_t, self.project.out_point - 0.5)
        self.timeline.update()

    def set_out(self):
        self.project.trim_out = max(self.canvas.frame_t, self.project.trim_in + 0.5)
        self.timeline.update()

    def clear_trim(self):
        self.project.trim_in, self.project.trim_out = 0.0, 0.0
        self.timeline.update()

    def export(self):
        if not self.project.video:
            QMessageBox.information(self, "Export", "Open a video first.")
            return
        if self.playing:
            self.toggle_play()
        dlg = ExportDialog(self.project, self.canvas.t, self)
        dlg.exec()

    # ------------------------------------------------------------------ misc
    def show_shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts", (
            "Space  play / pause\n←/→  previous / next frame\nShift+←/→  ±1 s\nHome / End  start / end\n"
            "I / O  trim in / out\n[ / ]  shift data −/+ 1 frame\nShift+[ / ]  shift data −/+ 0.1 s\n"
            "E  toggle layout editing\nH  hide overlay\nZ  reset timeline zoom (Ctrl+wheel zooms)\n"
            "Ctrl+T / Ctrl+I / Ctrl+B  insert text / image / blur\n"
            "Ctrl+L / Ctrl+Shift+E  load / save overlay file\n\n"
            "Layout editing: drag to move, corner handles to resize (Shift keeps aspect), arrows nudge "
            "(Shift = 10 px), Alt disables snapping, Delete removes, right-click for more."))

    def about(self):
        QMessageBox.about(self, "Onboard DataVis", f"<b>Onboard DataVis {__version__}</b><br>Telemetry overlays for "
                          "onboard racing video.<br>RaceBox, CSV, MF4, VBO and GPX data.<br><br>"
                          "Licensed under the GNU GPL v3 or later.<br>Fonts: Barlow Condensed, Chakra Petch (SIL Open Font License).")

    def keyPressEvent(self, ev):
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        if not self._confirm_discard():
            ev.ignore()
            return
        self.timer.stop()
        ev.accept()


def _install_excepthook():
    import traceback

    log_path = os.path.join(tempfile.gettempdir(), "onboard_datavis_error.log")

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + text + "\n")
        except Exception:
            pass
        sys.__stderr__ and sys.__stderr__.write(text)
        try:
            QMessageBox.critical(None, "Unexpected error", f"{value}\n\nDetails were written to\n{log_path}")
        except Exception:
            pass

    sys.excepthook = hook


def run_gui(project_path=None):
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OnboardDataVis.App")
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Onboard DataVis")
    _install_excepthook()
    load_fonts()
    apply_dark(app)
    ico = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "icon.png")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    w = MainWindow(project_path)
    w.show()
    sys.exit(app.exec())
