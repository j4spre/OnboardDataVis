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
    rotation: int = 0  # clockwise degrees needed to display upright (from the display matrix)
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
            vs.thread_type = "AUTO"
            fr = next(c.decode(vs))
            rot = int(round(-float(getattr(fr, "rotation", 0) or 0))) % 360
            rot = int(round(rot / 90.0)) * 90 % 360
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


# ---------------------------------------------------------------------- orientation

@dataclass
class VideoTransform:
    """User orientation fix: 90° steps, mirroring and a fine horizon angle."""

    rot90: int = 0  # extra clockwise rotation: 0 / 90 / 180 / 270
    flip_h: bool = False
    flip_v: bool = False
    angle: float = 0.0  # fine rotation, degrees clockwise
    fill: bool = True  # zoom in so no black corners appear with a fine angle

    def to_json(self) -> dict:
        return dict(rot90=self.rot90, flip_h=self.flip_h, flip_v=self.flip_v, angle=self.angle, fill=self.fill)

    @classmethod
    def from_json(cls, d) -> "VideoTransform":
        d = d or {}
        return cls(int(d.get("rot90", 0)) % 360, bool(d.get("flip_h", False)), bool(d.get("flip_v", False)),
                   float(d.get("angle", 0.0)), bool(d.get("fill", True)))

    def is_identity(self) -> bool:
        return self.rot90 % 360 == 0 and not self.flip_h and not self.flip_v and abs(self.angle) < 1e-6


def total_rotation(info: Optional[VideoInfo], tf: Optional[VideoTransform]) -> int:
    return ((info.rotation if info else 0) + (tf.rot90 if tf else 0)) % 360


def display_size(info: VideoInfo, tf: Optional[VideoTransform]):
    rot = total_rotation(info, tf)
    return (info.height, info.width) if rot in (90, 270) else (info.width, info.height)


def fill_scale(w: float, h: float, angle_deg: float) -> float:
    import math

    a = math.radians(abs(angle_deg) % 180)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    return max(c + (h / w) * s, (w / h) * s + c)


def fine_rotate(img, angle: float, fill: bool = True):
    """Rotate a QImage by ``angle`` degrees (clockwise) about its centre, keeping its size."""
    if abs(angle) < 1e-6:
        return img
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter

    w, h = img.width(), img.height()
    out = QImage(w, h, QImage.Format.Format_ARGB32)
    out.fill(0xFF000000)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.translate(QPointF(w / 2, h / 2))
    p.rotate(angle)
    if fill:
        k = fill_scale(w, h, angle)
        p.scale(k, k)
    p.drawImage(QPointF(-w / 2, -h / 2), img)
    p.end()
    return out


def transform_qimage(img, info: Optional[VideoInfo], tf: Optional[VideoTransform], fine: bool = True):
    """Apply display-matrix rotation + user transform to a decoded frame (QImage)."""
    from PySide6.QtGui import QTransform

    rot = total_rotation(info, tf)
    if rot:
        img = img.transformed(QTransform().rotate(rot))
    if tf is not None and (tf.flip_h or tf.flip_v):
        img = img.flipped(_flip_orient(tf.flip_h, tf.flip_v)) if hasattr(img, "flipped") else \
            img.mirrored(tf.flip_h, tf.flip_v)
    if fine and tf is not None and abs(tf.angle) > 1e-6:
        img = fine_rotate(img, tf.angle, tf.fill)
    return img


def _flip_orient(h: bool, v: bool):
    from PySide6.QtCore import Qt

    o = Qt.Orientation(0)
    if h:
        o |= Qt.Orientation.Horizontal
    if v:
        o |= Qt.Orientation.Vertical
    return o


def transform_array(a: np.ndarray, info: Optional[VideoInfo], tf: Optional[VideoTransform]) -> np.ndarray:
    """Same orientation change for a numpy image (used by motion analysis; fine angle ignored)."""
    rot = total_rotation(info, tf)
    if rot:
        a = np.rot90(a, k=-(rot // 90))
    if tf is not None and tf.flip_h:
        a = a[:, ::-1]
    if tf is not None and tf.flip_v:
        a = a[::-1]
    return np.ascontiguousarray(a)


def ffmpeg_orient_filters(info: Optional[VideoInfo], tf: Optional[VideoTransform]) -> list:
    """FFmpeg filters equivalent to :func:`transform_qimage` minus the fine angle (use with -noautorotate)."""
    rot = total_rotation(info, tf)
    f = {90: ["transpose=clock"], 180: ["hflip", "vflip"], 270: ["transpose=cclock"]}.get(rot, [])
    if tf is not None and tf.flip_h:
        f.append("hflip")
    if tf is not None and tf.flip_v:
        f.append("vflip")
    return f
