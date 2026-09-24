# SPDX-License-Identifier: GPL-3.0-or-later
"""Lap detection, lap timing and live delta.

Laps come from a lap-number channel when the logger provides one (RaceBox, VBO), otherwise
from crossings of a start/finish gate on the GPS trace.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .model import Session, split_key

R_EARTH = 6371000.0


@dataclass
class Lap:
    number: int
    t_start: float  # source time of the position source
    t_end: float
    complete: bool

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


def fmt_laptime(sec: Optional[float], decimals: int = 3) -> str:
    if sec is None or not np.isfinite(sec):
        return "--:--." + "-" * decimals
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    m = int(sec // 60)
    s = sec - 60 * m
    w = 3 + decimals if decimals else 2
    return f"{sign}{m}:{s:0{w}.{decimals}f}"


class LapModel:
    def __init__(self, session: Session):
        self.session = session
        self.laps: List[Lap] = []
        self.source_id: Optional[str] = None
        self.t = np.zeros(0)
        self.x = np.zeros(0)  # local metres (east)
        self.y = np.zeros(0)  # local metres (north)
        self.dist = np.zeros(0)
        self.delta_t = np.zeros(0)
        self.delta_v = np.zeros(0)
        self.sf_point: Optional[Tuple[float, float]] = None  # (x, y) in local metres
        self.sf_gate_ll: Optional[list] = None
        self.ref_lap: Optional[Lap] = None
        self.lat0 = self.lon0 = 0.0
        self.rebuild()

    # ------------------------------------------------------------------
    def rebuild(self, sf_gate_ll: Optional[list] = None):
        s = self.session
        self.laps = []
        self.ref_lap = None
        lat_key, lon_key = s.roles.get("lat"), s.roles.get("lon")
        src, lat_ch = s.resolve(lat_key) if lat_key else (None, None)
        _, lon_ch = s.resolve(lon_key) if lon_key else (None, None)
        if lat_ch is None or lon_ch is None or len(lat_ch) < 10:
            self.source_id = None
            return
        self.source_id = src.id
        t = lat_ch.t
        lat = lat_ch.v
        lon = lon_ch.at(t)
        ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) > 1e-6)
        t, lat, lon = t[ok], lat[ok], lon[ok]
        self.lat0, self.lon0 = float(np.median(lat)), float(np.median(lon))
        self.t = t
        self.x = np.radians(lon - self.lon0) * np.cos(np.radians(self.lat0)) * R_EARTH
        self.y = np.radians(lat - self.lat0) * R_EARTH
        seg = np.hypot(np.diff(self.x), np.diff(self.y))
        seg[seg > 50] = 0  # GPS jumps
        self.dist = np.concatenate([[0.0], np.cumsum(seg)])

        gate = sf_gate_ll or src.options.get("sf_gate") or src.meta.get("sf_gate")
        lap_key = s.roles.get("lap")
        lap_src, lap_ch = s.resolve(lap_key) if lap_key else (None, None)
        if gate:
            self._laps_from_gate(gate)
        elif lap_ch is not None and lap_src is src:
            self._laps_from_channel(lap_ch)
        self._compute_delta()

    def _laps_from_channel(self, ch):
        v = ch.v
        change = np.nonzero(np.diff(v) != 0)[0] + 1
        if len(change) == 0:
            return
        bounds = [float(ch.t[i]) for i in change]
        nums = [int(v[i]) for i in change]
        # start/finish point = position at first crossing
        i0 = int(np.searchsorted(self.t, bounds[0]))
        i0 = min(max(i0, 0), len(self.x) - 1)
        self.sf_point = (float(self.x[i0]), float(self.y[i0]))
        # out-lap (before first crossing)
        self.laps.append(Lap(int(v[0]), float(ch.t[0]), bounds[0], False))
        for k in range(len(bounds)):
            end = bounds[k + 1] if k + 1 < len(bounds) else float(ch.t[-1])
            self.laps.append(Lap(nums[k], bounds[k], end, k + 1 < len(bounds)))

    def _laps_from_gate(self, gate):
        la1, lo1, la2, lo2 = gate
        gx = np.radians(np.array([lo1, lo2]) - self.lon0) * np.cos(np.radians(self.lat0)) * R_EARTH
        gy = np.radians(np.array([la1, la2]) - self.lat0) * R_EARTH
        self.sf_point = (float(gx.mean()), float(gy.mean()))
        crossings = self._crossings(gx, gy)
        self._laps_from_crossings(crossings)

    def set_start_finish_at(self, t_src: float, half_width: float = 12.0):
        """Create a gate perpendicular to the driving direction at source time ``t_src``."""
        if len(self.t) < 3:
            return
        i = int(np.clip(np.searchsorted(self.t, t_src), 1, len(self.t) - 2))
        dx, dy = self.x[i + 1] - self.x[i - 1], self.y[i + 1] - self.y[i - 1]
        n = np.hypot(dx, dy) or 1.0
        px, py = -dy / n, dx / n
        gx = np.array([self.x[i] - px * half_width, self.x[i] + px * half_width])
        gy = np.array([self.y[i] - py * half_width, self.y[i] + py * half_width])
        lat = self.lat0 + np.degrees(gy / R_EARTH)
        lon = self.lon0 + np.degrees(gx / (R_EARTH * np.cos(np.radians(self.lat0))))
        gate = [float(lat[0]), float(lon[0]), float(lat[1]), float(lon[1])]
        src = self.session.source(self.source_id)
        if src is not None:
            src.options["sf_gate"] = gate
        self.rebuild(gate)

    def _crossings(self, gx, gy) -> List[float]:
        ax, ay, bx, by = gx[0], gy[0], gx[1], gy[1]
        x0, y0, x1, y1 = self.x[:-1], self.y[:-1], self.x[1:], self.y[1:]
        d1 = (bx - ax) * (y0 - ay) - (by - ay) * (x0 - ax)
        d2 = (bx - ax) * (y1 - ay) - (by - ay) * (x1 - ax)
        e1 = (x1 - x0) * (ay - y0) - (y1 - y0) * (ax - x0)
        e2 = (x1 - x0) * (by - y0) - (y1 - y0) * (bx - x0)
        hit = (np.sign(d1) != np.sign(d2)) & (np.sign(e1) != np.sign(e2))
        idx = np.nonzero(hit)[0]
        if len(idx) == 0:
            return []
        # keep crossings in the dominant direction only
        dirs = np.sign(d1[idx])
        main = 1 if (dirs > 0).sum() >= (dirs < 0).sum() else -1
        idx = idx[dirs == main]
        out = []
        for i in idx:
            f = d1[i] / (d1[i] - d2[i])
            tc = self.t[i] + f * (self.t[i + 1] - self.t[i])
            if not out or tc - out[-1] > 10.0:  # debounce
                out.append(float(tc))
        return out

    def _laps_from_crossings(self, cr: List[float]):
        if not cr:
            return
        self.laps.append(Lap(0, float(self.t[0]), cr[0], False))
        for k, c in enumerate(cr):
            end = cr[k + 1] if k + 1 < len(cr) else float(self.t[-1])
            self.laps.append(Lap(k + 1, c, end, k + 1 < len(cr)))

    # ------------------------------------------------------------------
    def complete_laps(self) -> List[Lap]:
        return [l for l in self.laps if l.complete]

    def best_lap(self, before_t: Optional[float] = None) -> Optional[Lap]:
        laps = [l for l in self.complete_laps() if before_t is None or l.t_end <= before_t]
        return min(laps, key=lambda l: l.duration) if laps else None

    def _dist_at(self, ts):
        return np.interp(ts, self.t, self.dist)

    def _compute_delta(self):
        self.ref_lap = self.best_lap()
        self.delta_t = np.zeros(0)
        self.delta_v = np.zeros(0)
        if self.ref_lap is None or len(self.t) < 10:
            return
        ref = self.ref_lap
        m = (self.t >= ref.t_start) & (self.t <= ref.t_end)
        ref_d = self.dist[m] - self._dist_at(ref.t_start)
        ref_tt = self.t[m] - ref.t_start
        ref_d = np.maximum.accumulate(ref_d)
        ts, dv = [], []
        for lap in self.laps:
            if lap.number <= 0 and not lap.complete:
                continue
            mm = (self.t >= lap.t_start) & (self.t <= lap.t_end)
            if mm.sum() < 3:
                continue
            d = self.dist[mm] - self._dist_at(lap.t_start)
            el = self.t[mm] - lap.t_start
            ref_at = np.interp(d, ref_d, ref_tt, right=np.nan)
            ts.append(self.t[mm])
            dv.append(el - ref_at)
        if ts:
            self.delta_t = np.concatenate(ts)
            self.delta_v = np.concatenate(dv)

    # ------------------------------------------------------------------ queries (video time)
    def _src_time(self, t_video: float) -> Optional[float]:
        src = self.session.source(self.source_id) if self.source_id else None
        return None if src is None else t_video + src.offset

    def to_video_time(self, t_src: float) -> Optional[float]:
        src = self.session.source(self.source_id) if self.source_id else None
        return None if src is None else t_src - src.offset

    def lap_index_at(self, t_src: float) -> int:
        for i, l in enumerate(self.laps):
            if l.t_start <= t_src < l.t_end:
                return i
        return -1

    def state(self, t_video: float, best_so_far: bool = True) -> dict:
        """Everything a lap timer widget needs at a video time."""
        ts = self._src_time(t_video)
        out = dict(valid=False, lap=None, lap_number=None, elapsed=None, last=None, best=None,
                   delta=None, total_laps=len(self.complete_laps()), since_finish=None,
                   last_delta=None)
        if ts is None or not self.laps:
            return out
        i = self.lap_index_at(ts)
        if i < 0:
            if ts >= self.laps[-1].t_end:
                i = len(self.laps) - 1
            else:
                return out
        lap = self.laps[i]
        out.update(valid=True, lap=lap, lap_number=lap.number, elapsed=ts - lap.t_start)
        prev = [l for l in self.laps[:i] if l.complete]
        if prev:
            out["last"] = prev[-1].duration
            out["since_finish"] = ts - prev[-1].t_end
            ref = min(prev, key=lambda l: l.duration)
            earlier = prev[:-1]
            if earlier:
                out["last_delta"] = prev[-1].duration - min(l.duration for l in earlier)
        best = self.best_lap(ts if best_so_far else None)
        if best is not None:
            out["best"] = best.duration
        if len(self.delta_t) and lap.number > 0:
            j = np.searchsorted(self.delta_t, ts)
            if 0 < j < len(self.delta_t) and abs(self.delta_t[j] - ts) < 1.0:
                out["delta"] = float(self.delta_v[j])
        return out

    def track_xy(self, lap: Optional[Lap] = None):
        if lap is None:
            lap = self.ref_lap
        if lap is not None:
            m = (self.t >= lap.t_start) & (self.t <= lap.t_end)
            return self.x[m], self.y[m]
        return self.x, self.y

    def position_xy(self, t_video: float):
        ts = self._src_time(t_video)
        if ts is None or len(self.t) == 0 or ts < self.t[0] or ts > self.t[-1]:
            return None
        return float(np.interp(ts, self.t, self.x)), float(np.interp(ts, self.t, self.y))
