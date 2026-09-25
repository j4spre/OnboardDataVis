# SPDX-License-Identifier: GPL-3.0-or-later
"""Synchronisation between video and data, and between data sources.

Convention: ``t_source = t_video + offset``.

* :func:`timestamp_offset`  – from absolute clocks (video creation_time vs log UTC start)
* :func:`motion_signal` + :func:`motion_sync` – camera yaw from optical flow vs gyro/heading
* :func:`align_sources` – cross-correlate two data sources on a speed-like channel
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import numpy as np

from .data.model import DataSource, Session


@dataclass
class SyncResult:
    offset: float
    score: float  # correlation at the peak (0..1)
    confidence: str  # 'high' | 'medium' | 'low'
    detail: str = ""


def _confidence(score: float, margin: float) -> str:
    if score > 0.9 and margin > 0.005:
        return "high"
    if score > 0.6 and margin > 0.12:
        return "high"
    if score > 0.4 and margin > 0.05:
        return "medium"
    return "low"


# ------------------------------------------------------------------ timestamp

def timestamp_offset(video_utc: Optional[_dt.datetime], src: DataSource) -> Optional[SyncResult]:
    if video_utc is None or src.utc_start is None:
        return None
    v = video_utc if video_utc.tzinfo else video_utc.replace(tzinfo=_dt.timezone.utc)
    s = src.utc_start if src.utc_start.tzinfo else src.utc_start.replace(tzinfo=_dt.timezone.utc)
    off = (v - s).total_seconds()
    detail = "video creation time vs log start"
    if not (src.t_start - 5 <= off <= src.t_end):
        # cameras often store local time without a zone: try whole-hour corrections
        for h in sorted(range(-14, 15), key=abs):
            if h and src.t_start - 5 <= off - h * 3600 <= src.t_end:
                off -= h * 3600
                detail += f" (corrected camera clock by {h:+d} h – local time zone)"
                break
    return SyncResult(off, 0.0, "low", detail)


# ------------------------------------------------------------------ correlation core

def _norm(x):
    x = np.asarray(x, dtype=np.float64)
    x = x - np.nanmean(x)
    sd = np.nanstd(x)
    return x / sd if sd > 0 else x


def correlate_search(sig_t: np.ndarray, sig_v: np.ndarray, ref_t: np.ndarray, ref_v: np.ndarray,
                     lo: float, hi: float, step: float = 0.1, min_overlap: float = 0.5,
                     allow_flip: bool = False, progress: Optional[Callable[[float], None]] = None):
    """Find offset o maximising corr(sig(t), ref(t + o)).

    ``sig`` is the shorter signal (e.g. video), ``ref`` the longer one (data).
    Returns (best_offset, best_score, margin, sign)."""
    # coarse-to-fine: keep the brute-force work bounded
    n_est = (sig_t[-1] - sig_t[0]) / step
    if n_est * (hi - lo) / step > 4e7 and step < 2.0:
        coarse = step * 5
        res = correlate_search(sig_t, sig_v, ref_t, ref_v, lo, hi, coarse, min_overlap, allow_flip, progress)
        if res is None:
            return None
        c_off, c_score, c_margin, _ = res
        res2 = correlate_search(sig_t, sig_v, ref_t, ref_v, c_off - 2 * coarse, c_off + 2 * coarse, step,
                                min_overlap, allow_flip, None)
        if res2 is None:
            return res
        return res2[0], res2[1], c_margin, res2[3]
    grid = np.arange(sig_t[0], sig_t[-1], step)
    s = np.interp(grid, sig_t, sig_v)
    s = _norm(s)
    n = len(grid)
    rg = np.arange(ref_t[0] - (grid[-1] - grid[0]), ref_t[-1] + step, step)
    r = np.interp(rg, ref_t, ref_v, left=np.nan, right=np.nan)
    offsets = np.arange(lo, hi, step)
    scores = np.full(len(offsets), np.nan)
    chunk = 400
    for c0 in range(0, len(offsets), chunk):
        offs = offsets[c0:c0 + chunk]
        idx0 = np.round((grid[0] + offs - rg[0]) / step).astype(int)
        cols = idx0[:, None] + np.arange(n)[None, :]
        valid_idx = (cols >= 0) & (cols < len(rg))
        cols = np.clip(cols, 0, len(rg) - 1)
        R = r[cols]
        R[~valid_idx] = np.nan
        ok = np.isfinite(R)
        cnt = ok.sum(1)
        S = np.broadcast_to(s, R.shape)
        Rz = np.where(ok, R, 0.0)
        Sz = np.where(ok, S, 0.0)
        mr = Rz.sum(1) / np.maximum(cnt, 1)
        ms = Sz.sum(1) / np.maximum(cnt, 1)
        Rc = np.where(ok, R - mr[:, None], 0.0)
        Sc = np.where(ok, S - ms[:, None], 0.0)
        num = (Rc * Sc).sum(1)
        den = np.sqrt((Rc ** 2).sum(1) * (Sc ** 2).sum(1))
        sc = np.where((den > 0) & (cnt >= min_overlap * n), num / np.maximum(den, 1e-12), np.nan)
        scores[c0:c0 + chunk] = sc
        if progress:
            progress(min(1.0, (c0 + chunk) / len(offsets)))
    if allow_flip:
        use = np.abs(scores)
    else:
        use = scores
    if not np.isfinite(use).any():
        return None
    k = int(np.nanargmax(use))
    best = float(offsets[k])
    score = float(use[k])
    sign = 1 if scores[k] >= 0 else -1
    # margin vs best peak at least 3 s away
    far = np.abs(offsets - best) > 3.0
    second = float(np.nanmax(use[far])) if np.isfinite(use[far]).any() else 0.0
    if step > 0.2:
        return best, score, score - second, sign
    # refine at 10 ms
    fine = np.arange(best - step, best + step, 0.01)
    fs = []
    for o in fine:
        rr = np.interp(grid + o, ref_t, ref_v, left=np.nan, right=np.nan)
        ok = np.isfinite(rr)
        fs.append(np.corrcoef(s[ok], rr[ok])[0, 1] * sign if ok.sum() > 10 else -1)
    best = float(fine[int(np.argmax(fs))])
    score = max(score, float(np.max(fs)))
    return best, score, score - second, sign


# ------------------------------------------------------------------ motion (video)

def motion_signal(video_path: str, sample_fps: float = 15.0, band=(0.2, 0.55),
                  progress: Optional[Callable[[float], None]] = None,
                  cancelled: Callable[[], bool] = lambda: False, orient: Optional[Callable] = None):
    """Horizontal image motion (camera yaw proxy) per sampled frame.

    Returns (t, yaw_px_per_s, magnitude)."""
    import av
    import cv2

    cont = av.open(video_path)
    vs = cont.streams.video[0]
    vs.thread_type = "AUTO"
    dur = float(cont.duration / 1e6) if cont.duration else float(vs.duration * vs.time_base)
    W = 320
    H = int(round(W * vs.codec_context.height / vs.codec_context.width / 2) * 2)
    prev, prev_t = None, None
    ts, yaw, mag = [], [], []
    next_t = 0.0
    for frame in cont.decode(vs):
        if frame.time is None:
            continue
        if frame.time + 1e-6 < next_t:
            continue
        next_t = frame.time + 1.0 / sample_fps
        g = frame.to_ndarray(format="gray", width=W, height=H)
        if orient is not None:
            g = orient(g)
        g = np.ascontiguousarray(g[int(band[0] * g.shape[0]):int(band[1] * g.shape[0])])
        if prev is not None:
            fl = cv2.calcOpticalFlowFarneback(prev, g, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            dt = frame.time - prev_t
            ts.append((frame.time + prev_t) / 2)
            yaw.append(float(np.median(fl[..., 0])) / dt)
            mag.append(float(np.mean(np.abs(fl))) / dt)
        prev, prev_t = g, frame.time
        if progress and dur and len(ts) % 15 == 0:
            progress(min(1.0, frame.time / dur))
        if cancelled():
            break
    cont.close()
    from scipy.ndimage import uniform_filter1d
    yaw = uniform_filter1d(np.array(yaw), 3)
    return np.array(ts), yaw, np.array(mag)


def data_yaw_rate(session: Session, src: DataSource) -> Optional[Tuple[np.ndarray, np.ndarray, str]]:
    """Yaw rate in deg/s from gyro Z, or derived from GPS heading."""
    s_yaw, ch = session.resolve_ref("@yaw_rate")
    if ch is not None and s_yaw is src and len(ch) > 20:
        return ch.t, ch.v, "gyro"
    s_lat, lat = session.resolve_ref("@lat")
    s_lon, lon = session.resolve_ref("@lon")
    if lat is not None and lon is not None and s_lat is src and s_lon is src:
        if lat is not None and lon is not None:
            t = lat.t
            la, lo = np.radians(lat.v), np.radians(lon.at(t))
            hd = np.unwrap(np.arctan2(np.gradient(lo) * np.cos(la), np.gradient(la)))
            dt = np.gradient(t)
            dt[dt <= 0] = np.nan
            yr = -np.degrees(np.gradient(hd) / dt)  # CCW positive like a gyro
            from scipy.ndimage import uniform_filter1d
            yr = uniform_filter1d(np.nan_to_num(yr), 3)
            return t, yr, "gps heading"
    return None


def motion_sync(session: Session, src: DataSource, motion, progress=None) -> Optional[SyncResult]:
    mt, myaw, _ = motion
    yr = data_yaw_rate(session, src)
    if yr is None or len(mt) < 20:
        return None
    t, v, kind = yr
    vid_len = mt[-1] - mt[0]
    lo = t[0] - 0.5 * vid_len
    hi = t[-1] - 0.5 * vid_len
    res = correlate_search(mt, myaw, t, v, lo, hi, step=0.1, allow_flip=True, progress=progress)
    if res is None:
        return None
    off, score, margin, sign = res
    return SyncResult(off, score, _confidence(score, margin),
                      f"camera yaw vs {kind}, r={score:.2f}, margin {margin:.2f}")


# ------------------------------------------------------------------ source to source

def speed_channel(session: Session, src: DataSource):
    s_sp, ch = session.resolve_ref("@speed")
    if ch is not None and s_sp is src:
        return ch
    name = src.default_roles().get("speed")
    if name:
        return src.get(name)
    return None


def align_sources(session: Session, ref: DataSource, other: DataSource,
                  ref_channel=None, other_channel=None, progress=None) -> Optional[SyncResult]:
    """Return the offset for ``other`` so that it lines up with ``ref`` (keeping ref's offset)."""
    a = ref_channel or speed_channel(session, ref)
    b = other_channel or speed_channel(session, other)
    if a is None or b is None:
        return None
    # resample the other source onto a 10 Hz grid, with spike suppression
    from scipy.signal import medfilt
    bt = np.arange(b.t[0], b.t[-1], 0.05)
    bv = medfilt(np.interp(bt, b.t, b.v), 5)
    at = np.arange(a.t[0], a.t[-1], 0.05)
    av_ = np.interp(at, a.t, a.v)
    # lag L such that a(t + L) == b(t)
    short_is_b = (bt[-1] - bt[0]) <= (at[-1] - at[0])
    if short_is_b:
        lo, hi = at[0] - bt[-1] + 0.3 * (bt[-1] - bt[0]), at[-1] - bt[0] - 0.3 * (bt[-1] - bt[0])
        res = correlate_search(bt, bv, at, av_, lo, hi, step=0.1, min_overlap=0.3, progress=progress)
        if res is None:
            return None
        lag, score, margin, _ = res
    else:
        lo, hi = bt[0] - at[-1] + 0.3 * (at[-1] - at[0]), bt[-1] - at[0] - 0.3 * (at[-1] - at[0])
        res = correlate_search(at, av_, bt, bv, lo, hi, step=0.1, min_overlap=0.3, progress=progress)
        if res is None:
            return None
        lag, score, margin, _ = res
        lag = -lag
    # t_ref = t_other + lag ;  t_ref = tv + off_ref  ->  t_other = tv + off_ref - lag
    return SyncResult(ref.offset - lag, score, _confidence(score, margin),
                      f"speed correlation with {ref.name}: lag {lag:+.2f} s, r={score:.2f}")
