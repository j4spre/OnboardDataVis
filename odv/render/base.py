# SPDX-License-Identifier: GPL-3.0-or-later
"""Widget base class, property schema and render context.

All drawing happens in *reference* coordinates (1920 px wide canvas); the renderer scales the
painter to the output size."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from .theme import Theme, draw_panel

SPEED_UNITS = {"km/h": 3.6, "mph": 2.2369363, "m/s": 1.0, "kn": 1.9438445}


def convert(value, from_unit: str, to_unit: str):
    if not to_unit or to_unit == from_unit:
        return value
    if from_unit == "m/s" and to_unit in SPEED_UNITS:
        return value * SPEED_UNITS[to_unit]
    if from_unit == "km/h" and to_unit in SPEED_UNITS:
        return value / 3.6 * SPEED_UNITS[to_unit]
    return value


def nice_ceil(x: float) -> float:
    if not np.isfinite(x) or x <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= x:
            return m * e
    return 10 * e


def nice_range(lo: float, hi: float) -> Tuple[float, float]:
    """Round a data range outwards to tidy gauge limits (keeps 0 when the data starts near 0)."""
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return 0.0, 1.0
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        hi = lo + 1.0
    if lo >= 0 and lo < 0.25 * hi:
        return 0.0, nice_ceil(hi)
    if hi <= 0 and hi > 0.25 * lo:
        return -nice_ceil(-lo), 0.0
    if lo < 0 < hi and -lo < 0.1 * hi:
        return 0.0, nice_ceil(hi)
    if lo < 0 < hi and hi < 0.1 * -lo:
        return -nice_ceil(-lo), 0.0
    if lo < 0 < hi:
        m = nice_ceil(max(-lo, hi))
        return (-m, m) if min(-lo, hi) > 0.4 * m else (-nice_ceil(-lo), nice_ceil(hi))
    step = nice_ceil((hi - lo) / 5)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


def nice_ticks(lo: float, hi: float, divisions: int = 5) -> Tuple[float, float, int]:
    """Extend (lo, hi) so that it splits into ~``divisions`` round steps. Returns (lo, hi, n)."""
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return lo, hi, max(1, divisions)
    step = nice_ceil((hi - lo) / max(1, divisions))
    lo2 = math.floor(lo / step + 1e-9) * step
    hi2 = math.ceil(hi / step - 1e-9) * step
    n = max(1, int(round((hi2 - lo2) / step)))
    return lo2, hi2, n


def fmt_num(v, decimals=0) -> str:
    if v is None or not np.isfinite(v):
        return "--"
    return f"{v:.{int(decimals)}f}"


# ------------------------------------------------------------------ context

class RenderContext:
    def __init__(self, session, laps, theme: Theme, t: float, speed_unit: str = "km/h"):
        self.session = session
        self.laps = laps
        self.theme = theme
        self.t = t
        self.speed_unit = speed_unit
        self.proc: Optional[dict] = None  # signal filter of the widget being drawn
        self.frame: Optional[QImage] = None  # clean video frame (for blur); output orientation
        self.frame_scale = 1.0  # frame pixels per reference unit
        self.edit_mode = False
        self._frame_arr = None
        self._range_cache: Dict[str, Tuple[float, float]] = {}

    def set_frame(self, img: Optional[QImage], ref_width: float = 1920.0):
        self.frame = img
        self._frame_arr = None
        self.frame_scale = (img.width() / ref_width) if img is not None and not img.isNull() else 1.0

    def frame_array(self):
        """BGRA numpy view of the clean frame (cached per frame)."""
        if self._frame_arr is None and self.frame is not None:
            img = self.frame
            if img.format() not in (QImage.Format.Format_ARGB32, QImage.Format.Format_RGB32,
                                    QImage.Format.Format_ARGB32_Premultiplied):
                img = img.convertToFormat(QImage.Format.Format_ARGB32)
            h, w, bpl = img.height(), img.width(), img.bytesPerLine()
            arr = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)[:, : w * 4]
            self._frame_arr = arr.reshape(h, w, 4)
            self._frame_img = img  # keep the buffer alive
        return self._frame_arr

    def unit(self, ref: str) -> str:
        return self.session.unit_of(ref) if self.session else ""

    def val(self, ref: str, unit: str = "") -> float:
        if not ref or self.session is None:
            return float("nan")
        v = self.session.value(ref, self.t, self.proc)
        return convert(v, self.unit(ref), unit)

    def series(self, ref: str, times: np.ndarray, unit: str = "") -> np.ndarray:
        if not ref or self.session is None:
            return np.full(len(times), np.nan)
        v = self.session.value(ref, times, self.proc)
        return convert(v, self.unit(ref), unit)

    def resolved(self, ref: str) -> bool:
        return bool(ref) and self.session is not None and self.session.is_resolved(ref)

    def auto_range(self, ref: str, unit: str = "") -> Tuple[float, float]:
        pk = tuple(sorted((self.proc or {}).items()))
        key = f"{ref}|{unit}|{pk}"
        if key not in self._range_cache:
            if self.session is None:
                src, ch = None, None
            elif self.proc:
                src, ch = self.session.processed(ref, self.proc.get("lowpass_hz", 0), self.proc.get("outlier_k", 0),
                                                 self.proc.get("outlier_window_s", 0.5))
            else:
                src, ch = self.session.resolve_ref(ref)
            if ch is None:
                rng = (0.0, 1.0)
            else:
                st = ch.stats()
                rng = (convert(st["p01"], ch.unit, unit), convert(st["p99"], ch.unit, unit))
            self._range_cache[key] = rng
        return self._range_cache[key]


# ------------------------------------------------------------------ props

@dataclass
class Prop:
    name: str
    kind: str  # channel | float | int | bool | color | choice | text | textarea | file | font
    default: Any
    label: str = ""
    options: Optional[List[str]] = None
    minv: float = -1e9
    maxv: float = 1e9
    group: str = ""
    decimals: int = -1  # -1 = automatic


COMMON = [
    Prop("visible", "bool", True, "Visible", group="Appearance"),
    Prop("opacity", "float", 1.0, "Opacity", minv=0.0, maxv=1.0, group="Appearance", decimals=2),
    Prop("panel", "bool", True, "Background panel", group="Appearance"),
    Prop("accent", "color", "", "Accent colour", group="Appearance"),
]

SIGNAL = [
    Prop("lowpass_hz", "float", 0.0, "Low-pass (Hz, 0 = off)", minv=0, maxv=500, group="Signal filter",
         decimals=2),
    Prop("outlier_k", "float", 0.0, "Outliers (σ, 0 = off)", minv=0, maxv=50, group="Signal filter",
         decimals=1),
    Prop("outlier_window_s", "float", 0.5, "Outlier window (s)", minv=0.02, maxv=30, group="Signal filter",
         decimals=2),
]

# single-channel value props shared by numeric / bar / dial
VALUE = [
    Prop("channel", "channel", "", "Channel", group="Signal"),
    Prop("label", "text", "", "Label", group="Signal"),
    Prop("unit", "text", "", "Unit", group="Signal"),
    Prop("scale", "float", 1.0, "Scale ×", group="Signal", decimals=6),
    Prop("add", "float", 0.0, "Offset +", group="Signal", decimals=4),
    Prop("decimals", "int", 0, "Decimals", minv=0, maxv=6, group="Signal"),
]

RANGE = [
    Prop("range_mode", "choice", "auto", "Range", ["auto", "fixed"], group="Range & limits"),
    Prop("min", "float", 0.0, "Min (fixed)", group="Range & limits", decimals=4),
    Prop("max", "float", 100.0, "Max (fixed)", group="Range & limits", decimals=4),
    Prop("use_limits", "bool", False, "Warn outside limits", group="Range & limits"),
    Prop("limit_low", "float", 0.0, "Lower limit", group="Range & limits", decimals=4),
    Prop("limit_high", "float", 100.0, "Upper limit", group="Range & limits", decimals=4),
    Prop("limit_color", "color", "", "Limit colour", group="Range & limits"),
]


class Widget:
    TYPE = "base"
    NAME = "Widget"
    CATEGORY = "Motorsport"
    PROPS: List[Prop] = []
    DEFAULT_SIZE = (300, 200)
    USES_DATA = True
    DEFAULT_PROPS: Dict[str, Any] = {}

    def __init__(self, x=0.0, y=0.0, w=None, h=None, props: Optional[dict] = None, wid: Optional[str] = None):
        self.id = wid or uuid.uuid4().hex[:8]
        self.x, self.y = float(x), float(y)
        self.w = float(w if w is not None else self.DEFAULT_SIZE[0])
        self.h = float(h if h is not None else self.DEFAULT_SIZE[1])
        self.props: Dict[str, Any] = {p.name: p.default for p in self.all_props()}
        self.props.update(self.DEFAULT_PROPS)
        if props:
            self.props.update(props)

    @classmethod
    def all_props(cls) -> List[Prop]:
        return cls.PROPS + COMMON + (SIGNAL if cls.USES_DATA else [])

    @classmethod
    def prop(cls, name: str) -> Optional[Prop]:
        return next((p for p in cls.all_props() if p.name == name), None)

    @property
    def rect(self) -> QRectF:
        return QRectF(self.x, self.y, self.w, self.h)

    def accent(self, theme: Theme) -> QColor:
        c = self.props.get("accent")
        return QColor(c) if c else theme.accent

    def panel(self, p, r, theme, radius=None):
        if self.props.get("panel", True):
            draw_panel(p, r, theme, radius)

    def signal_params(self) -> Optional[dict]:
        if not self.USES_DATA:
            return None
        lp = float(self.props.get("lowpass_hz", 0) or 0)
        ok = float(self.props.get("outlier_k", 0) or 0)
        if lp <= 0 and ok <= 0:
            return None
        return dict(lowpass_hz=lp, outlier_k=ok, outlier_window_s=float(self.props.get("outlier_window_s", 0.5)))

    def channel_refs(self) -> List[str]:
        return [self.props.get(p.name) for p in self.all_props() if p.kind == "channel" and self.props.get(p.name)]

    def missing_channels(self, session) -> List[str]:
        if session is None:
            return self.channel_refs()
        return [r for r in self.channel_refs() if not session.is_resolved(r)]

    def paint(self, p: QPainter, ctx: RenderContext):
        if not self.props.get("visible", True):
            return
        p.save()
        p.setOpacity(float(self.props.get("opacity", 1.0)))
        ctx.proc = self.signal_params()
        try:
            self.draw(p, self.rect, ctx)
        finally:
            ctx.proc = None
            p.restore()

    def draw(self, p: QPainter, r: QRectF, ctx: RenderContext):  # pragma: no cover - abstract
        raise NotImplementedError

    def to_json(self) -> dict:
        return dict(type=self.TYPE, id=self.id, x=self.x, y=self.y, w=self.w, h=self.h, props=dict(self.props))


class ValueWidget(Widget):
    """Single channel with scaling, range and warning limits."""

    CATEGORY = "Data"

    def label_text(self, ctx) -> str:
        lab = self.props.get("label") or ""
        if lab:
            return lab
        ref = self.props.get("channel", "")
        if ref.startswith("@"):
            return ref[1:].replace("_", " ")
        return ref.split(":", 1)[-1]

    def unit_text(self, ctx) -> str:
        return self.props.get("unit") or ctx.unit(self.props.get("channel", ""))

    def value(self, ctx) -> float:
        pr = self.props
        u = pr.get("unit", "")
        v = ctx.val(pr.get("channel", ""), u if u in SPEED_UNITS else "")
        return v * float(pr.get("scale", 1.0)) + float(pr.get("add", 0.0))

    def value_range(self, ctx) -> Tuple[float, float]:
        pr = self.props
        if pr.get("range_mode", "auto") == "fixed":
            lo, hi = float(pr.get("min", 0.0)), float(pr.get("max", 1.0))
            return (lo, hi) if hi != lo else (lo, lo + 1)
        u = pr.get("unit", "")
        lo, hi = ctx.auto_range(pr.get("channel", ""), u if u in SPEED_UNITS else "")
        s, a = float(pr.get("scale", 1.0)), float(pr.get("add", 0.0))
        lo, hi = sorted((lo * s + a, hi * s + a))
        return nice_range(lo, hi)

    def limit_state(self, v: float) -> int:
        """-1 below lower limit, +1 above upper, 0 inside (or limits off)."""
        pr = self.props
        if not pr.get("use_limits") or not np.isfinite(v):
            return 0
        if v < float(pr.get("limit_low", -1e18)):
            return -1
        if v > float(pr.get("limit_high", 1e18)):
            return 1
        return 0

    def limit_color(self, theme) -> QColor:
        c = self.props.get("limit_color")
        return QColor(c) if c else theme.bad
