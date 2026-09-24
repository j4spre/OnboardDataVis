# SPDX-License-Identifier: GPL-3.0-or-later
"""Video probing and frame-accurate preview decoding (PyAV)."""
from __future__ import annotations

import datetime as _dt
import os
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

import numpy as np


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    fps_frac: Fraction
    duration: float
    frames: int
    has_audio: bool
    creation_time: Optional[_dt.datetime]
    rotation: int = 0
    codec: str = ""

    @property
    def frame_dur(self) -> float:
        return 1.0 / self.fps if self.fps else 1 / 30


def _parse_time(s: str) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        if d.year < 2005:  # unset camera clock
            return None
        return d
    except ValueError:
        return None


def probe(path: str) -> VideoInfo:
    import av

    c = av.open(path)
    try:
        vs = c.streams.video[0]
        rate = vs.average_rate or vs.guessed_rate or Fraction(30, 1)
        dur = float(c.duration / 1e6) if c.duration else float(vs.duration * vs.time_base)
        if vs.duration and vs.time_base:
            dur = min(dur, float(vs.duration * vs.time_base)) or dur
        frames = vs.frames or int(round(dur * float(rate)))
        ct = _parse_time(c.metadata.get("creation_time", "")) or _parse_time(vs.metadata.get("creation_time", ""))
        rot = 0
        try:
            for sd in getattr(vs, "side_data", []) or []:
                pass
            rot = int(vs.metadata.get("rotate", 0) or 0)
        except Exception:
            rot = 0
        return VideoInfo(path=path, width=vs.codec_context.width, height=vs.codec_context.height, fps=float(rate),
                         fps_frac=Fraction(rate), duration=dur, frames=frames, has_audio=len(c.streams.audio) > 0,
                         creation_time=ct, rotation=rot, codec=vs.codec_context.name)
    finally:
        c.close()


class VideoReader:
    """Sequential decoder with cheap forward stepping and keyframe seeking."""

    def __init__(self, path: str, max_width: int = 1280):
        import av

        self.path = path
        self.c = av.open(path)
        self.vs = self.c.streams.video[0]
        self.vs.thread_type = "AUTO"
        w, h = self.vs.codec_context.width, self.vs.codec_context.height
        s = min(1.0, max_width / w)
        self.out_w = int(w * s) // 2 * 2
        self.out_h = int(h * s) // 2 * 2
        rate = self.vs.average_rate or Fraction(30, 1)
        self.fps = float(rate)
        self._it = None
        self._last = None  # (time, ndarray)
        self._next = None
        self.lock = threading.Lock()

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass

    def _seek(self, t: float):
        ts = int(max(0.0, t) / self.vs.time_base)
        self.c.seek(ts, stream=self.vs, backward=True, any_frame=False)
        self._it = self.c.decode(self.vs)
        self._last = None
        self._next = None

    def _pull(self):
        try:
            fr = next(self._it)
        except (StopIteration, Exception):
            return None
        if fr.time is None:
            return self._pull()
        return fr

    def frame_at(self, t: float) -> Optional[tuple]:
        """Return (frame_time, BGRA ndarray) of the frame displayed at time t."""
        with self.lock:
            half = 0.5 / self.fps
            if self._last is not None and abs(self._last[0] - t) < half:
                return self._last
            need_seek = (self._it is None or self._last is None or t < self._last[0] - half
                         or t > self._last[0] + 2.0)
            if need_seek:
                self._seek(t - 0.001)
            best = self._last
            while True:
                fr = self._next or self._pull()
                self._next = None
                if fr is None:
                    break
                if fr.time > t + half:
                    self._next = fr
                    if best is None:
                        best = (fr.time, fr.to_ndarray(format="bgra", width=self.out_w, height=self.out_h))
                    break
                # only convert frames close to the target (skipping is cheap)
                if fr.time >= t - half or best is None:
                    best = (fr.time, fr.to_ndarray(format="bgra", width=self.out_w, height=self.out_h))
                else:
                    best = (fr.time, None)
            if best is not None and best[1] is None:
                return self._last
            self._last = best
            return best
