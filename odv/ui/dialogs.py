# SPDX-License-Identifier: GPL-3.0-or-later
"""Export and CSV import dialogs."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..render.export import ENCODERS, TRANSPARENT, ExportSettings, Exporter, available_encoders, pick_encoder
from .timeline import fmt_tc
from .workers import Job, start_job


class ExportDialog(QDialog):
    def __init__(self, project, current_t: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export video")
        self.setMinimumWidth(560)
        self.project = project
        self.current_t = current_t
        self.job = None
        pr = project
        v = pr.video
        lay = QVBoxLayout(self)
        form = QFormLayout()
        row = QHBoxLayout()
        base = os.path.splitext(v.path)[0]
        self.out = QLineEdit(base + "_overlay.mp4")
        row.addWidget(self.out, 1)
        br = QPushButton("Browse…")
        br.clicked.connect(self._browse)
        row.addWidget(br)
        form.addRow("Output", row)

        self.fmt = QComboBox()
        self.fmt.addItem("MP4 video with overlay", "mp4")
        for k, t in TRANSPARENT.items():
            self.fmt.addItem("Overlay only – " + t["label"], k)
        self.fmt.currentIndexChanged.connect(self._fmt_changed)
        form.addRow("Format", self.fmt)

        self.enc = QComboBox()
        self.enc.addItem("Detecting encoders…", "auto")
        form.addRow("Encoder", self.enc)

        self.res = QComboBox()
        ar = v.width / v.height
        opts = [("Source", v.width, v.height)]
        for h in (720, 1080, 1440, 2160):
            w = int(round(h * ar / 2) * 2)
            if (w, h) != (v.width, v.height):
                opts.append((f"{h}p", w, h))
        for lab, w, h in opts:
            self.res.addItem(f"{lab}  ({w}×{h})", (w, h))
        if v.height < 1080:
            self.res.setCurrentIndex(next((i for i, o in enumerate(opts) if o[0] == "1080p"), 0))
        form.addRow("Resolution", self.res)
        form.addRow("", self._note("Overlay graphics are drawn at the output resolution, so upscaling a 720p "
                                   "video to 1080p gives crisper gauges."))

        self.quality = QComboBox()
        self.quality.addItems(["standard", "high", "max"])
        self.quality.setCurrentText(pr.export.get("quality", "high"))
        form.addRow("Quality", self.quality)

        self.range = QComboBox()
        self.range.addItem(f"Whole video (0:00 – {fmt_tc(v.duration)})", (0.0, v.duration))
        if pr.trim_in > 0 or pr.trim_out > 0:
            self.range.addItem(f"Trim range ({fmt_tc(pr.trim_in)} – {fmt_tc(pr.out_point)})", (pr.trim_in, pr.out_point))
            self.range.setCurrentIndex(1)
        laps = pr.laps
        if laps is not None:
            for l in laps.laps:
                a, b = laps.to_video_time(l.t_start), laps.to_video_time(l.t_end)
                if a is None or b <= 0 or a >= v.duration or l.number <= 0:
                    continue
                a, b = max(0.0, a - 2), min(v.duration, b + 2)
                self.range.addItem(f"Lap {l.number} ({fmt_tc(a)} – {fmt_tc(b)})", (a, b))
        self.range.addItem("Custom…", None)
        self.range.currentIndexChanged.connect(self._range_changed)
        form.addRow("Range", self.range)
        crow = QHBoxLayout()
        self.cin = QDoubleSpinBox()
        self.cout = QDoubleSpinBox()
        for s in (self.cin, self.cout):
            s.setRange(0, v.duration)
            s.setDecimals(2)
            s.setSuffix(" s")
            crow.addWidget(s)
        self.cin.setValue(pr.trim_in)
        self.cout.setValue(pr.out_point)
        self.custom_row = QLabel()
        form.addRow("From / to", crow)
        self.audio = QCheckBox("Include audio")
        self.audio.setChecked(pr.export.get("audio", True) and v.has_audio)
        self.audio.setEnabled(v.has_audio)
        form.addRow("", self.audio)
        lay.addLayout(form)

        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        self.prog.setVisible(False)
        lay.addWidget(self.prog)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        lay.addWidget(self.status)
        bb = QHBoxLayout()
        bb.addStretch(1)
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(self._open_folder)
        bb.addWidget(self.open_btn)
        self.cancel_btn = QPushButton("Close")
        self.cancel_btn.clicked.connect(self._cancel)
        bb.addWidget(self.cancel_btn)
        self.go = QPushButton("Export")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self._start)
        bb.addWidget(self.go)
        lay.addLayout(bb)
        self._range_changed()
        # encoder detection in background
        self._detect = Job(lambda job: available_encoders())
        start_job(self, self._detect, on_done=self._encoders_ready)

    def _note(self, text):
        l = QLabel(text)
        l.setObjectName("muted")
        l.setWordWrap(True)
        return l

    def _encoders_ready(self, encs):
        self.enc.clear()
        best = pick_encoder("auto")
        self.enc.addItem(f"Automatic (best available: {ENCODERS.get(best, {}).get('label', best)})", "auto")
        for e in encs:
            self.enc.addItem(ENCODERS[e]["label"], e)
        pref = self.project.export.get("encoder", "auto")
        i = self.enc.findData(pref)
        if i >= 0:
            self.enc.setCurrentIndex(i)

    def _fmt_changed(self):
        k = self.fmt.currentData()
        base = os.path.splitext(self.out.text())[0]
        if base.endswith("_overlay_frames"):
            base = base[: -len("_frames")]
        if k == "mp4":
            self.out.setText(base + ".mp4")
        elif k == "png":
            self.out.setText(base + "_frames")
        else:
            self.out.setText(base + TRANSPARENT[k]["ext"])
        self.enc.setEnabled(k == "mp4")
        self.audio.setEnabled(k == "mp4" and self.project.video.has_audio)

    def _range_changed(self):
        d = self.range.currentData()
        custom = d is None
        self.cin.setEnabled(custom)
        self.cout.setEnabled(custom)
        if not custom:
            self.cin.setValue(d[0])
            self.cout.setValue(d[1])

    def _browse(self):
        k = self.fmt.currentData()
        if k == "png":
            path = QFileDialog.getExistingDirectory(self, "Folder for PNG frames")
        else:
            flt = "MP4 video (*.mp4)" if k == "mp4" else "QuickTime (*.mov)"
            path, _ = QFileDialog.getSaveFileName(self, "Export to", self.out.text(), flt)
        if path:
            self.out.setText(path)

    def _start(self):
        pr = self.project
        out = self.out.text().strip()
        if not out:
            return
        if os.path.exists(out) and os.path.isfile(out):
            if QMessageBox.question(self, "Overwrite?", f"{os.path.basename(out)} exists. Overwrite?") != QMessageBox.StandardButton.Yes:
                return
        w, h = self.res.currentData()
        k = self.fmt.currentData()
        st = ExportSettings(out, w, h, self.cin.value(), self.cout.value(), self.enc.currentData() or "auto",
                            self.quality.currentText(), self.audio.isChecked(), "" if k == "mp4" else k)
        pr.export.update(encoder=st.encoder, quality=st.quality, audio=st.audio)
        self.exporter = Exporter(pr, st)
        self.prog.setVisible(True)
        self.prog.setValue(0)
        self.go.setEnabled(False)
        self.cancel_btn.setText("Cancel")
        self.status.setText("Rendering…")
        self._t0 = time.time()

        def work(job):
            def prog(i, n, eta):
                job.progress.emit(i / max(n, 1), f"{i}/{n} frames · {i / max(time.time() - self._t0, 1e-3):.1f} fps · "
                                                 f"about {eta:.0f} s left")
            ok = self.exporter.run(prog)
            return ok

        self.job = Job(work)
        start_job(self, self.job, on_progress=lambda f, s: (self.prog.setValue(int(f * 1000)), self.status.setText(s)),
                  on_done=self._done, on_fail=lambda e: self._done(False, e))

    def _done(self, ok, err=None):
        self.go.setEnabled(True)
        self.cancel_btn.setText("Close")
        if ok:
            self.prog.setValue(1000)
            n = getattr(self.exporter, "frames_written", 0)
            el = getattr(self.exporter, "elapsed", 0)
            self.status.setText(f"Done – {n} frames in {el:.0f} s. Saved to {self.out.text()}")
            self.open_btn.setVisible(True)
        elif self.exporter.cancelled:
            self.status.setText("Cancelled.")
        else:
            self.status.setText("Export failed.")
            QMessageBox.warning(self, "Export failed", (err or self.exporter.error or "Unknown error")[-1500:])
        self.job = None

    def _cancel(self):
        if self.job is not None:
            self.exporter.cancel()
        else:
            self.reject()

    def _open_folder(self):
        p = self.out.text()
        QDesktopServices.openUrl(QUrl.fromLocalFile(p if os.path.isdir(p) else os.path.dirname(p)))

    def closeEvent(self, ev):
        if self.job is not None:
            self.exporter.cancel()
        super().closeEvent(ev)


class CsvImportDialog(QDialog):
    """Lets the user confirm/override how a generic CSV is read."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        from ..data.loaders import sniff_csv, _find_time_col

        self.setWindowTitle("Import CSV – " + os.path.basename(path))
        self.setMinimumWidth(640)
        sn = sniff_csv(path)
        cols = sn["columns"]
        lay = QVBoxLayout(self)
        info = QLabel(f"Detected {len(cols)} columns, header on line {sn['header'] + 1}, delimiter "
                      f"'{sn['delim'] if sn['delim'] != chr(9) else 'TAB'}'.")
        info.setObjectName("muted")
        lay.addWidget(info)
        form = QFormLayout()
        self.time_col = QComboBox()
        self.time_col.addItem("(none – constant sample rate)", "")
        for c in cols:
            self.time_col.addItem(c, c)
        tc = _find_time_col(cols)
        if tc:
            self.time_col.setCurrentIndex(self.time_col.findData(tc))
        form.addRow("Time column", self.time_col)
        self.time_unit = QComboBox()
        self.time_unit.addItem("Auto (seconds, clock time or date-time)", "")
        self.time_unit.addItem("Milliseconds", "ms")
        self.time_unit.addItem("Microseconds", "us")
        form.addRow("Time unit", self.time_unit)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.1, 10000)
        self.rate.setValue(10)
        self.rate.setSuffix(" Hz")
        form.addRow("Sample rate (if no time column)", self.rate)
        self.speed_unit = QComboBox()
        self.speed_unit.addItem("Auto (checked against GPS)", "")
        for u in ("km/h", "mph", "m/s", "kn"):
            self.speed_unit.addItem(u, u)
        form.addRow("Speed unit", self.speed_unit)
        lay.addLayout(form)
        tbl = QTableWidget()
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = [next(f, "") for _ in range(sn["data"] + 12)]
        import csv as _csv

        rows = list(_csv.reader([l for l in lines[sn["data"]:] if l.strip()], delimiter=sn["delim"]))[:12]
        tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row[: len(cols)]):
                tbl.setItem(r, c, QTableWidgetItem(v))
        tbl.setMaximumHeight(260)
        lay.addWidget(tbl)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def mapping(self) -> dict:
        m = {}
        if self.time_col.currentData():
            m["time_col"] = self.time_col.currentData()
        else:
            m["time_col"] = "__none__"
            m["rate"] = self.rate.value()
        if self.time_unit.currentData():
            m["time_unit"] = self.time_unit.currentData()
        if self.speed_unit.currentData():
            m["speed_unit"] = self.speed_unit.currentData()
        return m
