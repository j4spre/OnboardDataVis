# SPDX-License-Identifier: GPL-3.0-or-later
"""Signal conditioning applied to channels before they reach a widget.

* :func:`remove_outliers` – Hampel filter: samples further than ``k`` robust standard deviations
  (1.4826 × MAD) from the rolling median are replaced by that median.
* :func:`lowpass` – zero-phase 2nd-order Butterworth low-pass (no time lag) at ``cutoff_hz``.
"""
from __future__ import annotations

import numpy as np


def _uniform(t: np.ndarray, v: np.ndarray, max_points: int = 2_000_000):
    """Resample to a uniform grid (median sample interval). Returns (tu, vu, dt)."""
    if len(t) < 3:
        return t, v, 1.0
    dt = float(np.median(np.diff(t)))
    if not np.isfinite(dt) or dt <= 0:
        dt = (t[-1] - t[0]) / max(len(t) - 1, 1) or 1.0
    n = int((t[-1] - t[0]) / dt) + 1
    if n > max_points:
        dt = (t[-1] - t[0]) / (max_points - 1)
        n = max_points
    tu = t[0] + np.arange(n) * dt
    return tu, np.interp(tu, t, v), dt


def remove_outliers(t: np.ndarray, v: np.ndarray, k: float = 3.0, window_s: float = 0.5) -> np.ndarray:
    """Hampel filter on (possibly non-uniform) samples. Keeps the original time base."""
    from scipy.ndimage import median_filter

    if k <= 0 or len(v) < 5:
        return v
    dt = float(np.median(np.diff(t))) if len(t) > 2 else 1.0
    w = int(round(window_s / dt)) if dt > 0 else 5
    w = max(3, min(w | 1, 2001))  # odd, bounded
    med = median_filter(v, size=w, mode="nearest")
    dev = np.abs(v - med)
    mad = median_filter(dev, size=w, mode="nearest")
    # fall back to a global scale where the local MAD is zero (flat signal with a spike)
    glob = np.median(dev) or (np.std(v) * 0.05) or 1e-9
    sigma = 1.4826 * np.maximum(mad, glob)
    bad = dev > k * sigma
    if not bad.any():
        return v
    out = v.copy()
    out[bad] = med[bad]
    return out


def lowpass(t: np.ndarray, v: np.ndarray, cutoff_hz: float, order: int = 2):
    """Zero-phase Butterworth low-pass. Returns (t_uniform, v_filtered)."""
    from scipy.signal import butter, sosfiltfilt

    if cutoff_hz <= 0 or len(v) < 12:
        return t, v
    tu, vu, dt = _uniform(t, v)
    fs = 1.0 / dt
    wn = cutoff_hz / (fs / 2.0)
    if wn >= 0.99:  # cutoff above Nyquist: nothing to do
        return t, v
    sos = butter(order, max(wn, 1e-6), btype="low", output="sos")
    try:
        vf = sosfiltfilt(sos, vu)
    except ValueError:  # too short for the filter padding
        return t, v
    return tu, vf


def process(t: np.ndarray, v: np.ndarray, lowpass_hz: float = 0.0, outlier_k: float = 0.0,
            outlier_window_s: float = 0.5):
    """Outliers first (so spikes don't smear), then low-pass."""
    if outlier_k and outlier_k > 0:
        v = remove_outliers(t, v, outlier_k, outlier_window_s)
    if lowpass_hz and lowpass_hz > 0:
        t, v = lowpass(t, v, lowpass_hz)
    return t, v
