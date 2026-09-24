# SPDX-License-Identifier: GPL-3.0-or-later
"""Core data model: channels, data sources and the session that ties them to a video.

Time conventions
----------------
* Every channel stores its own sample times ``t`` in *source time* (seconds).
* Each :class:`DataSource` has an ``offset`` so that ``t_source = t_video + offset``.
* The rest of the program works in *video time*; conversion happens here.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

# Canonical roles that widgets and features look for. Values are human labels.
ROLES: Dict[str, str] = {
    "speed": "Speed",
    "lat": "Latitude",
    "lon": "Longitude",
    "alt": "Altitude",
    "lap": "Lap number",
    "g_long": "G longitudinal",
    "g_lat": "G lateral",
    "yaw_rate": "Yaw rate (gyro Z)",
    "heading": "Heading",
    "throttle": "Throttle",
    "brake": "Brake",
    "rpm": "RPM",
    "gear": "Gear",
    "steering": "Steering angle",
    "soc": "State of charge",
    "power": "Power",
}


@dataclass
class Channel:
    name: str
    t: np.ndarray
    v: np.ndarray
    unit: str = ""
    step: bool = False  # hold previous value instead of interpolating (lap, gear, flags)
    _stats: Optional[dict] = field(default=None, repr=False)

    def __post_init__(self):
        self.t = np.asarray(self.t, dtype=np.float64)
        self.v = np.asarray(self.v, dtype=np.float64)
        if len(self.t) > 1 and np.any(np.diff(self.t) < 0):
            order = np.argsort(self.t, kind="stable")
            self.t, self.v = self.t[order], self.v[order]
        # drop NaNs so interpolation stays clean
        ok = np.isfinite(self.v) & np.isfinite(self.t)
        if not ok.all():
            self.t, self.v = self.t[ok], self.v[ok]

    def __len__(self):
        return len(self.t)

    @property
    def t_start(self) -> float:
        return float(self.t[0]) if len(self.t) else 0.0

    @property
    def t_end(self) -> float:
        return float(self.t[-1]) if len(self.t) else 0.0

    def at(self, ts):
        """Value(s) at source time(s) ``ts``. NaN outside the recorded range."""
        scalar = np.isscalar(ts)
        ts = np.atleast_1d(np.asarray(ts, dtype=np.float64))
        if len(self.t) == 0:
            out = np.full(ts.shape, np.nan)
        elif self.step:
            idx = np.searchsorted(self.t, ts, side="right") - 1
            out = self.v[np.clip(idx, 0, len(self.v) - 1)].astype(np.float64)
            out[idx < 0] = np.nan
        else:
            out = np.interp(ts, self.t, self.v)
        if len(self.t):
            out[(ts < self.t[0] - 0.5) | (ts > self.t[-1] + 0.5)] = np.nan
        return float(out[0]) if scalar else out

    def stats(self) -> dict:
        if self._stats is None:
            v = self.v
            if len(v) == 0:
                self._stats = dict(min=0.0, max=1.0, p01=0.0, p99=1.0)
            else:
                self._stats = dict(
                    min=float(np.min(v)), max=float(np.max(v)),
                    p01=float(np.percentile(v, 0.5)), p99=float(np.percentile(v, 99.5)),
                )
        return self._stats


class DataSource:
    """One imported log file."""

    kind = "generic"

    def __init__(self, sid: str, name: str, path: str = "", kind: str = "generic"):
        self.id = sid
        self.name = name
        self.path = path
        self.kind = kind
        self.offset = 0.0  # t_source = t_video + offset
        self.utc_start: Optional[_dt.datetime] = None  # absolute time of source t=0 (UTC)
        self.meta: dict = {}
        self.options: dict = {}
        self._channels: Dict[str, Channel] = {}
        self._lazy: Dict[str, Callable[[], Channel]] = {}
        self._units: Dict[str, str] = {}

    # -- channels -------------------------------------------------------
    def add(self, ch: Channel):
        self._channels[ch.name] = ch
        self._units[ch.name] = ch.unit

    def add_lazy(self, name: str, unit: str, loader: Callable[[], Channel]):
        self._lazy[name] = loader
        self._units[name] = unit

    def channel_names(self) -> List[str]:
        names = list(self._channels.keys()) + [n for n in self._lazy if n not in self._channels]
        return names

    def unit_of(self, name: str) -> str:
        return self._units.get(name, "")

    def has(self, name: str) -> bool:
        return name in self._channels or name in self._lazy

    def get(self, name: str) -> Optional[Channel]:
        ch = self._channels.get(name)
        if ch is None and name in self._lazy:
            try:
                ch = self._lazy[name]()
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[odv] failed to load channel {name}: {exc}")
                ch = None
            if ch is not None:
                self._channels[name] = ch
        return ch

    @property
    def t_start(self) -> float:
        return float(self.meta.get("t_start", 0.0))

    @property
    def t_end(self) -> float:
        return float(self.meta.get("t_end", 0.0))

    def default_roles(self) -> Dict[str, str]:
        """Role -> channel name guesses for this source."""
        return dict(self.meta.get("roles", {}))

    def to_json(self) -> dict:
        return dict(id=self.id, name=self.name, path=self.path, kind=self.kind,
                    offset=self.offset, options=self.options)


def channel_key(source_id: str, name: str) -> str:
    return f"{source_id}:{name}"


def split_key(key: str):
    if not key or ":" not in key:
        return None, key
    sid, name = key.split(":", 1)
    return sid, name


class Session:
    """All data sources plus the role mapping, expressed on the video timeline."""

    def __init__(self):
        self.sources: List[DataSource] = []
        self.roles: Dict[str, str] = {}  # role -> channel key
        self.listeners: List[Callable[[], None]] = []
        self.laps = None  # set by odv.data.laps.LapModel

    # -- sources --------------------------------------------------------
    def source(self, sid: str) -> Optional[DataSource]:
        for s in self.sources:
            if s.id == sid:
                return s
        return None

    def new_id(self, base: str) -> str:
        base = "".join(c for c in base.lower() if c.isalnum()) or "src"
        sid, i = base, 2
        while self.source(sid):
            sid, i = f"{base}{i}", i + 1
        return sid

    def add_source(self, src: DataSource, auto_roles: bool = True):
        self.sources.append(src)
        if auto_roles:
            for role, name in src.default_roles().items():
                if role not in self.roles or self.resolve(self.roles[role])[1] is None:
                    self.roles[role] = channel_key(src.id, name)

    def remove_source(self, sid: str):
        self.sources = [s for s in self.sources if s.id != sid]
        self.roles = {r: k for r, k in self.roles.items() if split_key(k)[0] != sid}

    def all_keys(self) -> List[str]:
        out = []
        for s in self.sources:
            out += [channel_key(s.id, n) for n in s.channel_names()]
        return out

    def resolve(self, key: str):
        sid, name = split_key(key)
        src = self.source(sid) if sid else None
        if src is None:
            return None, None
        return src, src.get(name)

    def role_key(self, role: str) -> Optional[str]:
        return self.roles.get(role)

    def resolve_ref(self, ref: str):
        """``ref`` is a role name (``@speed``) or a channel key."""
        if not ref:
            return None, None
        if ref.startswith("@"):
            key = self.roles.get(ref[1:])
            if not key:
                return None, None
            return self.resolve(key)
        return self.resolve(ref)

    def unit_of(self, ref: str) -> str:
        src, ch = self.resolve_ref(ref)
        return ch.unit if ch is not None else ""

    def value(self, ref: str, t_video):
        src, ch = self.resolve_ref(ref)
        if ch is None:
            return np.nan if np.isscalar(t_video) else np.full(np.shape(t_video), np.nan)
        return ch.at(np.asarray(t_video) + src.offset if not np.isscalar(t_video) else t_video + src.offset)

    def channel_for(self, ref: str):
        return self.resolve_ref(ref)

    def notify(self):
        for cb in list(self.listeners):
            cb()
