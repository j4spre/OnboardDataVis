# SPDX-License-Identifier: GPL-3.0-or-later
"""Background jobs (motion analysis, export) on QThreads.

Callbacks are always delivered on the GUI thread through a relay object that lives there."""
from __future__ import annotations

import traceback
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot


class Job(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def cancelled(self):
        return self._cancel

    @Slot()
    def run(self):
        try:
            res = self.fn(self, *self.args, **self.kw)
            self.finished.emit(res)
        except Exception as exc:
            traceback.print_exc()
            self.failed.emit(f"{exc}")


class Relay(QObject):
    """Lives in the GUI thread; job signals reach it as queued calls."""

    def __init__(self, parent, on_progress, on_done, on_fail):
        super().__init__(parent)
        self._p, self._d, self._f = on_progress, on_done, on_fail

    @Slot(float, str)
    def progress(self, f, s):
        if self._p:
            self._p(f, s)

    @Slot(object)
    def done(self, res):
        if self._d:
            self._d(res)

    @Slot(str)
    def fail(self, err):
        if self._f:
            self._f(err)

    @Slot()
    def cleanup(self):
        if getattr(self, "_cleanup", None):
            self._cleanup()


def start_job(parent, job: Job, on_progress: Optional[Callable] = None, on_done: Optional[Callable] = None,
              on_fail: Optional[Callable] = None) -> QThread:
    relay = Relay(parent, on_progress, on_done, on_fail)
    th = QThread(parent)
    job.moveToThread(th)
    job.progress.connect(relay.progress)
    job.finished.connect(relay.done)
    job.failed.connect(relay.fail)
    th.started.connect(job.run)
    job.finished.connect(th.quit)
    job.failed.connect(th.quit)
    if not hasattr(parent, "_jobs"):
        parent._jobs = []
    jobs = parent._jobs
    entry = (th, job, relay)
    jobs.append(entry)

    def cleanup():
        if entry in jobs:
            jobs.remove(entry)
        relay.deleteLater()
        th.deleteLater()

    relay._cleanup = cleanup
    th.finished.connect(relay.cleanup)
    th.start()
    return th
