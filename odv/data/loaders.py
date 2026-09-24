# SPDX-License-Identifier: GPL-3.0-or-later
"""File importers. Each returns a :class:`DataSource` with channels on its own time axis.

Supported: RaceBox CSV (all presets), generic CSV/TSV, Racelogic VBO (incl. RaceBox VBO),
GPX, ASAM MDF (MF4/MDF/DAT via asammdf).
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import math
import os
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from .model import Channel, DataSource

UTC = _dt.timezone.utc

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

SPEED_FACTORS = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444}


def _unit_from_name(name: str) -> Tuple[str, str]:
    """'Speed (km/h)' -> ('Speed', 'km/h')."""
    m = re.match(r"^\s*(.*?)\s*[\(\[]\s*([^\)\]]*)\s*[\)\]]\s*$", name)
    if m:
        return m.group(1), m.group(2)
    return name.strip(), ""


def _norm_unit(u: str) -> str:
    u = u.strip().lower().replace(" ", "")
    return {"kmh": "km/h", "kph": "km/h", "km/h": "km/h", "m/s": "m/s", "mps": "m/s",
            "mph": "mph", "kn": "kn", "knots": "kn", "kts": "kn"}.get(u, u)


def haversine_speed(t, lat, lon) -> np.ndarray:
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    dx = np.diff(lon_r) * np.cos((lat_r[1:] + lat_r[:-1]) / 2) * 6371000.0
    dy = np.diff(lat_r) * 6371000.0
    dt = np.diff(t)
    dt[dt <= 0] = np.nan
    s = np.hypot(dx, dy) / dt
    return np.concatenate([[s[0] if len(s) else 0.0], s])


def guess_speed_factor(t, speed, lat, lon) -> Optional[float]:
    """Compare a speed column with GPS-derived speed; return factor to m/s."""
    try:
        gs = haversine_speed(t, lat, lon)
        best, best_err = None, 1e9
        mask = np.isfinite(gs) & (gs > 3)
        if mask.sum() < 20:
            return None
        for f in SPEED_FACTORS.values():
            err = np.nanmedian(np.abs(speed[mask] * f - gs[mask]) / gs[mask])
            if err < best_err:
                best, best_err = f, err
        return best if best_err < 0.25 else None
    except Exception:
        return None


def clean_spikes(v: np.ndarray, k: float = 0.5) -> np.ndarray:
    """Replace absurd outliers (CAN glitches) by interpolated neighbours."""
    if len(v) < 20 or not np.issubdtype(v.dtype, np.floating):
        return v
    lo, hi = np.nanpercentile(v, [0.1, 99.9])
    span = max(hi - lo, 1e-9)
    bad = (v < lo - k * span) | (v > hi + k * span)
    if bad.any() and (~bad).sum() > 2:
        idx = np.arange(len(v))
        v = v.copy()
        v[bad] = np.interp(idx[bad], idx[~bad], v[~bad])
    return v


ROLE_PATTERNS: List[Tuple[str, str]] = [
    ("lat", r"^(lat|latitude|gps[_ ]?lat.*|posn?[_ ]?lat.*)$"),
    ("lon", r"^(lon|lng|long|longitude|gps[_ ]?lon.*|posn?[_ ]?lon.*)$"),
    ("alt", r"^(alt|altitude|height|elevation|ele)$"),
    ("speed", r"^(speed|gps[_ ]?speed|velocity|ground[_ ]?speed|vehicle[_ ]?speed|v)$"),
    ("lap", r"^(lap|lap[_ ]?(number|num|nr|no|#)?|lapnumber)$"),
    ("g_long", r"^(gforcex|g[_ ]?long.*|long.*[_ ]?g|longacc.*|accel[_ ]?x|acc[_ ]?x|ax|inline[_ ]?acc.*)$"),
    ("g_lat", r"^(gforcey|g[_ ]?lat.*|lat.*[_ ]?g|latacc.*|accel[_ ]?y|acc[_ ]?y|ay|lateral[_ ]?acc.*)$"),
    ("yaw_rate", r"^(gyroz|yaw[_ ]?rate|gyro[_ ]?z|rot[_ ]?z|wz)$"),
    ("heading", r"^(heading|course|bearing|hdg)$"),
    ("throttle", r"^(throttle.*|tps|accel.*pedal.*|pedal.*|apps.*)$"),
    ("brake", r"^(brake.*|brk.*)$"),
    ("rpm", r"^(rpm|engine[_ ]?(rpm|speed)|motor[_ ]?rpm|revs)$"),
    ("gear", r"^(gear|gear[_ ]?pos.*)$"),
    ("steering", r"^(steer.*|swa|steering[_ ]?angle)$"),
    ("soc", r"^(soc|state[_ ]?of[_ ]?charge|battery[_ ]?soc)$"),
    ("power", r"^(power|pwr|battery[_ ]?power)$"),
]


def guess_roles(names: List[str]) -> Dict[str, str]:
    roles: Dict[str, str] = {}
    for role, pat in ROLE_PATTERNS:
        rx = re.compile(pat, re.I)
        for n in names:
            base, _ = _unit_from_name(n)
            if rx.match(base.strip()):
                roles.setdefault(role, n)
                break
    return roles


# --------------------------------------------------------------------------------------
# CSV (RaceBox + generic)
# --------------------------------------------------------------------------------------

def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def sniff_csv(path: str, max_lines: int = 300) -> dict:
    """Locate delimiter, header row and an optional units row."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = [next(f, "") for _ in range(max_lines)]
    lines = [l.rstrip("\r\n") for l in lines if l is not None]
    sample = "\n".join(lines[:50])
    delim = ","
    counts = {d: sample.count(d) for d in [",", ";", "\t", "|"]}
    delim = max(counts, key=counts.get)
    rows = list(csv.reader(lines, delimiter=delim))
    header_idx = 0
    for i, r in enumerate(rows[:-1]):
        cells = [c.strip() for c in r if c.strip() != ""]
        if len(cells) < 2:
            continue
        non_num = sum(not _is_num(c) for c in cells)
        nxt = [c.strip() for c in rows[i + 1] if c.strip() != ""]
        # header: mostly text; data: find the first numeric row within the next 3 rows
        if non_num >= 0.7 * len(cells):
            for j in range(i + 1, min(i + 4, len(rows))):
                cand = [c.strip() for c in rows[j] if c.strip() != ""]
                if cand and sum(_is_num(c) or ":" in c or "-" in c for c in cand) >= 0.6 * len(cand) \
                        and len(rows[j]) >= 0.8 * len(r):
                    header_idx = i
                    units_row = j - 1 if j - 1 > i else None
                    meta_lines = lines[:i]
                    return dict(delim=delim, header=i, units=units_row, data=j,
                                columns=[c.strip() for c in r], meta_lines=meta_lines,
                                unit_cells=[c.strip() for c in rows[units_row]] if units_row else None)
    return dict(delim=delim, header=header_idx, units=None, data=header_idx + 1,
                columns=[c.strip() for c in rows[header_idx]] if rows else [], meta_lines=[],
                unit_cells=None)


TIME_NAMES = ["time", "timestamp", "utc", "utc time", "time (s)", "time(s)", "elapsed", "elapsed time",
              "datetime", "date time", "gps time", "t", "session time", "time_s", "time [s]", "time_utc"]


def _find_time_col(columns: List[str]) -> Optional[str]:
    low = {c.lower().strip(): c for c in columns}
    for n in TIME_NAMES:
        if n in low:
            return low[n]
    for c in columns:
        if re.search(r"time|utc|stamp", c, re.I) and not re.search(r"lap ?time|laptime|sector", c, re.I):
            return c
    return None


def parse_time_column(values, date_values=None) -> Tuple[np.ndarray, Optional[_dt.datetime], str]:
    """Return (seconds relative to t0, absolute UTC of t0 or None, description)."""
    import pandas as pd

    s = pd.Series(values)
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().mean() > 0.95:
        arr = num.to_numpy(dtype=np.float64)
        med = np.nanmedian(arr)
        if med > 1e11:  # epoch milliseconds
            t0 = _dt.datetime.fromtimestamp(arr[0] / 1000.0, tz=UTC)
            return (arr - arr[0]) / 1000.0, t0, "epoch ms"
        if med > 1e9:  # epoch seconds
            t0 = _dt.datetime.fromtimestamp(arr[0], tz=UTC)
            return arr - arr[0], t0, "epoch s"
        return arr, None, "seconds"
    txt = s.astype(str)
    if date_values is not None:
        txt = pd.Series(date_values).astype(str) + " " + txt
    # clock time only (HH:MM:SS.sss)?
    if txt.str.match(r"^\s*\d{1,2}:\d{2}:\d{2}(\.\d+)?\s*$").mean() > 0.95:
        td = pd.to_timedelta(txt.str.strip(), errors="coerce").dt.total_seconds().to_numpy()
        td = np.where(np.diff(np.concatenate([[td[0]], td])) < -43200, td + 86400, td)  # midnight wrap
        return td, None, "clock (no date)"
    dt = pd.to_datetime(txt, errors="coerce", utc=True, format="mixed")
    if dt.notna().mean() > 0.95:
        rel = (dt - dt.iloc[0]).dt.total_seconds().to_numpy(dtype=np.float64)
        t0 = dt.iloc[0].to_pydatetime()
        return rel, t0, "absolute"
    raise ValueError("Could not parse the time column")


def load_csv(path: str, sid: str, mapping: Optional[dict] = None) -> DataSource:
    """Load RaceBox or any other CSV. ``mapping`` may override: time_col, time_unit,
    speed_unit, roles{role: column}."""
    import pandas as pd

    mapping = mapping or {}
    sn = sniff_csv(path)
    df = pd.read_csv(path, sep=sn["delim"], skiprows=sn["header"], header=0,
                     skip_blank_lines=True, low_memory=False, encoding="utf-8-sig",
                     engine="python" if sn["delim"] == "|" else "c")
    df.columns = [str(c).strip() for c in df.columns]
    if sn["units"] is not None:
        df = df.iloc[sn["data"] - sn["header"] - 1:].reset_index(drop=True)
    df = df.dropna(how="all")

    cols = list(df.columns)
    is_racebox = ("GForceX" in cols and "Lap" in cols) or "racebox" in os.path.basename(path).lower() \
        or any("racebox" in l.lower() for l in sn["meta_lines"])
    src = DataSource(sid, os.path.basename(path), path, "racebox" if is_racebox else "csv")
    src.meta["preset"] = "RaceBox" if is_racebox else "Generic CSV"

    time_col = mapping.get("time_col") or _find_time_col(cols)
    date_col = None
    for c in cols:
        if c.lower() in ("date", "utc date", "gps date"):
            date_col = c
    if time_col is None or time_col not in df.columns:
        rate = float(mapping.get("rate", 10.0))
        t = np.arange(len(df)) / rate
        t0, desc = None, f"sample index @ {rate:g} Hz"
    else:
        t, t0, desc = parse_time_column(df[time_col].values,
                                        df[date_col].values if date_col and date_col != time_col else None)
        tu = mapping.get("time_unit")
        if tu == "ms":
            t = t / 1000.0
        elif tu == "us":
            t = t / 1e6
    src.utc_start = t0
    src.meta["time_desc"] = desc
    src.meta["time_col"] = time_col

    unit_cells = sn.get("unit_cells")
    numeric: Dict[str, np.ndarray] = {}
    units: Dict[str, str] = {}
    for i, c in enumerate(cols):
        if c in (time_col, date_col) or c.lower() == "record":
            continue
        v = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=np.float64)
        if np.isfinite(v).mean() < 0.5:
            continue
        base, unit = _unit_from_name(c)
        if not unit and unit_cells and i < len(unit_cells):
            unit = unit_cells[i]
        numeric[c] = v
        units[c] = unit

    roles = guess_roles(list(numeric.keys()))
    roles.update({k: v for k, v in mapping.get("roles", {}).items() if v in numeric})

    # speed -> m/s
    if "speed" in roles:
        sc = roles["speed"]
        f = None
        su = mapping.get("speed_unit") or _norm_unit(units.get(sc, ""))
        if mapping.get("speed_unit") in SPEED_FACTORS:
            f = SPEED_FACTORS[mapping["speed_unit"]]
        elif "lat" in roles and "lon" in roles:
            f = guess_speed_factor(t, numeric[sc], numeric[roles["lat"]], numeric[roles["lon"]])
        if f is None:
            f = SPEED_FACTORS.get(su, 1 / 3.6 if np.nanmax(numeric[sc]) > 80 else 1.0)
        numeric[sc] = numeric[sc] * f
        units[sc] = "m/s"
        src.meta["speed_factor"] = f

    if is_racebox:
        for c in ("GForceX", "GForceY", "GForceZ"):
            if c in units and not units[c]:
                units[c] = "g"
        for c in ("GyroX", "GyroY", "GyroZ"):
            if c in units and not units[c]:
                units[c] = "deg/s"
        for c, u in (("Latitude", "deg"), ("Longitude", "deg"), ("Altitude", "m")):
            if c in units and not units[c]:
                units[c] = u

    for c, v in numeric.items():
        src.add(Channel(c, t, v, units.get(c, ""), step=(roles.get("lap") == c or roles.get("gear") == c)))

    src.meta["roles"] = roles
    src.meta["t_start"], src.meta["t_end"] = float(np.nanmin(t)), float(np.nanmax(t))
    src.meta["columns"] = cols
    src.meta["rate_hz"] = float(1.0 / np.nanmedian(np.diff(t))) if len(t) > 2 else 0.0
    src.options.update(mapping)
    return src


# --------------------------------------------------------------------------------------
# VBO (Racelogic / RaceBox)
# --------------------------------------------------------------------------------------

def load_vbo(path: str, sid: str) -> DataSource:
    with open(path, "r", encoding="latin-1", errors="replace") as f:
        text = f.read()
    sections: Dict[str, List[str]] = {}
    cur = "_pre"
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].lower()
            sections.setdefault(cur, [])
        elif s:
            sections.setdefault(cur, []).append(s)
    names = " ".join(sections.get("column names", [])).split()
    rows = [r.split() for r in sections.get("data", [])]
    rows = [r for r in rows if len(r) == len(names)]
    if not names or not rows:
        raise ValueError("VBO file has no [column names] / [data]")

    def num(x):
        try:
            return float(x)
        except ValueError:
            return np.nan

    arr = np.array([[num(x) for x in r] for r in rows], dtype=np.float64)
    col = {n.lower(): i for i, n in enumerate(names)}
    src = DataSource(sid, os.path.basename(path), path, "vbo")
    # time HHMMSS.SS UTC
    ti = col.get("time")
    hhmmss = arr[:, ti]
    h = np.floor(hhmmss / 10000)
    m = np.floor((hhmmss - h * 10000) / 100)
    sec = hhmmss - h * 10000 - m * 100
    tod = h * 3600 + m * 60 + sec
    tod = tod + 86400 * np.cumsum(np.concatenate([[0], np.diff(tod) < -43200]))
    t = tod - tod[0]
    # date from header "File created on 16/08/2026 @ ..."
    date = None
    mm = re.search(r"(\d{2})/(\d{2})/(\d{4})", sections.get("_pre", [""])[0] if sections.get("_pre") else "")
    if mm:
        try:
            date = _dt.date(int(mm.group(3)), int(mm.group(2)), int(mm.group(1)))
        except ValueError:
            date = None
    if date:
        src.utc_start = _dt.datetime.combine(date, _dt.time(0), tzinfo=UTC) + _dt.timedelta(seconds=float(tod[0]))

    roles = {}
    for n, i in col.items():
        v = arr[:, i]
        name, unit = names[i], ""
        if n == "time":
            continue
        if n == "lat":
            v, name, unit = v / 60.0, "Latitude", "deg"
            roles["lat"] = name
        elif n == "long":
            v, name, unit = -v / 60.0, "Longitude", "deg"
            roles["lon"] = name
        elif n == "velocity":
            v, name, unit = v / 3.6, "Speed", "m/s"
            roles["speed"] = name
        elif n == "heading":
            unit = "deg"
            roles["heading"] = name
        elif n == "height":
            unit = "m"
            roles["alt"] = name
        elif n in ("lat_acc", "latacc", "lateral_acceleration"):
            unit = "g"
            roles["g_lat"] = name
        elif n in ("long_acc", "longacc", "longitudinal_acceleration"):
            unit = "g"
            roles["g_long"] = name
        elif n in ("yaw_rate", "yawrate", "gyro_z"):
            unit = "deg/s"
            roles["yaw_rate"] = name
        elif n in ("lap", "lap_number"):
            roles["lap"] = name
        src.add(Channel(name, t, v, unit, step=(roles.get("lap") == name)))
    for role, c in guess_roles(src.channel_names()).items():
        roles.setdefault(role, c)
    src.meta.update(roles=roles, t_start=float(t[0]), t_end=float(t[-1]), time_desc="UTC time of day",
                    rate_hz=float(1 / np.median(np.diff(t))) if len(t) > 2 else 0)
    # [laptiming] gate: "Start  +lon1 +lat1 +lon2 +lat2 ¬ Start / Finish"
    for line in sections.get("laptiming", []):
        p = line.split()
        if len(p) >= 5 and p[0].lower() == "start":
            try:
                lo1, la1, lo2, la2 = (float(x) for x in p[1:5])
                src.meta["sf_gate"] = [la1 / 60.0, -lo1 / 60.0, la2 / 60.0, -lo2 / 60.0]
            except ValueError:
                pass
    return src


# --------------------------------------------------------------------------------------
# GPX
# --------------------------------------------------------------------------------------

def load_gpx(path: str, sid: str) -> DataSource:
    import xml.etree.ElementTree as ET
    import pandas as pd

    root = ET.parse(path).getroot()
    pts = [e for e in root.iter() if e.tag.endswith("trkpt")]
    lat = np.array([float(p.get("lat")) for p in pts])
    lon = np.array([float(p.get("lon")) for p in pts])
    ele, times = [], []
    for p in pts:
        e = next((c for c in p if c.tag.endswith("ele")), None)
        tm = next((c for c in p if c.tag.endswith("time")), None)
        ele.append(float(e.text) if e is not None else np.nan)
        times.append(tm.text if tm is not None else None)
    dt = pd.to_datetime(pd.Series(times), utc=True, errors="coerce", format="mixed")
    t = (dt - dt.iloc[0]).dt.total_seconds().to_numpy(dtype=np.float64)
    src = DataSource(sid, os.path.basename(path), path, "gpx")
    src.utc_start = dt.iloc[0].to_pydatetime()
    src.add(Channel("Latitude", t, lat, "deg"))
    src.add(Channel("Longitude", t, lon, "deg"))
    src.add(Channel("Altitude", t, np.array(ele), "m"))
    sp = haversine_speed(t, lat, lon)
    from scipy.ndimage import uniform_filter1d
    src.add(Channel("Speed", t, uniform_filter1d(np.nan_to_num(sp), 5), "m/s"))
    src.meta.update(roles=dict(lat="Latitude", lon="Longitude", alt="Altitude", speed="Speed"),
                    t_start=float(t[0]), t_end=float(t[-1]), time_desc="absolute")
    return src


# --------------------------------------------------------------------------------------
# MF4 / MDF
# --------------------------------------------------------------------------------------

MF4_ROLE_PATTERNS: List[Tuple[str, str]] = [
    ("speed", r"(vehicle|veh|car|gps|ground)[_ ]?speed|^speed$|^v_?car"),
    ("speed", r"wheel_?speed.*front.*(left|fl)|wheelspeed_fl|wss_fl"),
    ("speed", r"wheel_?speed"),
    ("throttle", r"apps_?linear|throttle|pedal_?pos|accel.*pedal|\bapps\b|tps"),
    ("brake", r"brake_?press.*front|brake_?press|brk_?press|brake"),
    ("steering", r"steer.*angle|steering(?!_(fault|valid|enabled))|\bswa\b"),
    ("rpm", r"engine_?(rpm|speed)|\brpm\b|motor_?rotation_?speed|motor_?speed|n_mot"),
    ("soc", r"(^|_)soc$|state_?of_?charge"),
    ("power", r"(ivt_)?result_w$|power$|_power(_|$)"),
    ("gear", r"(^|_)gear(_|$)"),
    ("g_lat", r"lat(eral)?_?acc|acc(el)?_?y$|(^|_)ay$"),
    ("g_long", r"long(itudinal)?_?acc|acc(el)?_?x$|(^|_)ax$"),
    ("yaw_rate", r"yaw_?rate|gyro_?z"),
    ("lat", r"(^|_)lat(itude)?$"),
    ("lon", r"(^|_)lon(g|gitude)?$"),
]


def load_mf4(path: str, sid: str, despike: bool = True) -> DataSource:
    from asammdf import MDF

    mdf = MDF(path)
    src = DataSource(sid, os.path.basename(path), path, "mf4")
    try:
        st = mdf.start_time
        if st.tzinfo is None:
            st = st.replace(tzinfo=UTC)
        src.utc_start = st.astimezone(UTC)
    except Exception:
        src.utc_start = None

    index: Dict[str, Tuple[int, int, str]] = {}
    counts: Dict[str, int] = {}
    t_lo, t_hi = math.inf, -math.inf
    for gi, grp in enumerate(mdf.groups):
        try:
            nrec = int(grp.channel_group.cycles_nr)
        except Exception:
            nrec = 0
        if nrec == 0:
            continue
        master_idx = mdf.masters_db.get(gi) if hasattr(mdf, "masters_db") else None
        try:
            tm = mdf.get_master(gi)
            if len(tm):
                t_lo, t_hi = min(t_lo, float(tm[0])), max(t_hi, float(tm[-1]))
        except Exception:
            pass
        acq = ""
        try:
            acq = grp.channel_group.acq_name or ""
        except Exception:
            pass
        for ci, ch in enumerate(grp.channels):
            if ci == master_idx or ch.name in ("time", "t", "Time", "timestamps"):
                continue
            name = ch.name
            if name in index:
                # keep the group with most samples under the plain name
                if nrec > counts[name]:
                    old = index[name]
                    index[f"{name} #{old[0]}"] = old
                    counts[f"{name} #{old[0]}"] = counts[name]
                    index[name] = (gi, ci, getattr(ch, "unit", "") or "")
                    counts[name] = nrec
                else:
                    index[f"{name} #{gi}"] = (gi, ci, getattr(ch, "unit", "") or "")
                    counts[f"{name} #{gi}"] = nrec
                continue
            index[name] = (gi, ci, getattr(ch, "unit", "") or "")
            counts[name] = nrec

    def make_loader(nm, gi, ci, unit):
        def _load():
            sig = mdf.get(group=gi, index=ci, raw=False)
            v = sig.samples
            if v.dtype.kind in "SUO" or v.dtype.names:
                sig = mdf.get(group=gi, index=ci, raw=True)
                v = sig.samples
            v = np.asarray(v, dtype=np.float64)
            if despike and src.options.get("despike", True):
                v = clean_spikes(v)
            return Channel(nm, sig.timestamps, v, sig.unit or unit)
        return _load

    for nm in sorted(index):
        gi, ci, unit = index[nm]
        src.add_lazy(nm, unit, make_loader(nm, gi, ci, unit))

    roles: Dict[str, str] = {}
    names = [n for n in sorted(index) if "#" not in n]
    for role, pat in MF4_ROLE_PATTERNS:
        if role in roles:
            continue
        rx = re.compile(pat, re.I)
        cands = [n for n in names if rx.search(n) and not re.search(r"valid|fault|flag|enable|active|request|raw|limit|max|min", n, re.I)]
        if cands:
            cands.sort(key=lambda n: -counts.get(n, 0))
            roles[role] = cands[0]
    src.meta.update(roles=roles, t_start=0.0 if t_lo == math.inf else t_lo,
                    t_end=0.0 if t_hi == -math.inf else t_hi, time_desc="MDF master time",
                    n_channels=len(index), counts=counts)
    src.options["despike"] = despike
    src._mdf = mdf  # keep file open for lazy loading
    return src


# --------------------------------------------------------------------------------------

def detect_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".mf4", ".mdf", ".dat"):
        return "mf4"
    if ext == ".vbo":
        return "vbo"
    if ext == ".gpx":
        return "gpx"
    return "csv"


def load_any(path: str, sid: str, kind: Optional[str] = None, options: Optional[dict] = None) -> DataSource:
    kind = kind or detect_kind(path)
    options = options or {}
    if kind == "mf4":
        return load_mf4(path, sid, despike=options.get("despike", True))
    if kind == "vbo":
        return load_vbo(path, sid)
    if kind == "gpx":
        return load_gpx(path, sid)
    return load_csv(path, sid, mapping=options)
