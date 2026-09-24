# SPDX-License-Identifier: GPL-3.0-or-later
"""Project = video + data sources + sync offsets + overlay layout. Saved as JSON (.odv)."""
from __future__ import annotations

import json
import os
from typing import List, Optional

from .data.laps import LapModel
from .data.loaders import load_any
from .data.model import Session
from .render.widgets import (DeltaBar, GCircle, Graph, LapTimer, Pedals, SpeedDial, Steering, TrackMap,
                             ValueBox, BarGauge, Widget, widget_from_json)
from .video import VideoInfo, probe

REF_WIDTH = 1920.0
FORMAT_VERSION = 1


class Project:
    def __init__(self):
        self.path: Optional[str] = None
        self.video: Optional[VideoInfo] = None
        self.session = Session()
        self.laps: Optional[LapModel] = None
        self.widgets: List[Widget] = []
        self.theme = "Broadcast"
        self.accent = ""
        self.speed_unit = "km/h"
        self.trim_in = 0.0
        self.trim_out = 0.0  # 0 = end of video
        self.link_offsets = True
        self.primary_source: Optional[str] = None
        self.export = dict(resolution="source", encoder="auto", quality="high", audio=True)
        self.dirty = False

    # ------------------------------------------------------------------ geometry
    @property
    def ref_height(self) -> float:
        if self.video and self.video.width:
            return REF_WIDTH * self.video.height / self.video.width
        return 1080.0

    # ------------------------------------------------------------------ media
    def set_video(self, path: str):
        self.video = probe(path)
        self.trim_in, self.trim_out = 0.0, 0.0
        self.dirty = True

    def add_data(self, path: str, kind: Optional[str] = None, options: Optional[dict] = None, sid: str = None):
        base = os.path.splitext(os.path.basename(path))[0]
        k = kind or ("mf4" if path.lower().endswith((".mf4", ".mdf", ".dat")) else "data")
        sid = sid or self.session.new_id(k if k in ("mf4", "vbo", "gpx") else "log")
        src = load_any(path, sid, kind if kind != "racebox" else "csv", options)
        self.session.add_source(src)
        if self.primary_source is None:
            self.primary_source = src.id
        self.rebuild_laps()
        self.dirty = True
        return src

    def remove_source(self, sid: str):
        self.session.remove_source(sid)
        if self.primary_source == sid:
            self.primary_source = self.session.sources[0].id if self.session.sources else None
        self.rebuild_laps()
        self.dirty = True

    def rebuild_laps(self):
        self.laps = LapModel(self.session)
        self.session.laps = self.laps

    def primary(self):
        return self.session.source(self.primary_source) if self.primary_source else None

    def set_offset(self, sid: str, offset: float, propagate: Optional[bool] = None):
        """Change a source's offset. When linked, moving the primary moves all others too."""
        src = self.session.source(sid)
        if src is None:
            return
        delta = offset - src.offset
        propagate = self.link_offsets if propagate is None else propagate
        if propagate and sid == self.primary_source:
            for s in self.session.sources:
                if s.id != sid:
                    s.offset += delta
        src.offset = offset
        self.dirty = True

    @property
    def duration(self) -> float:
        return self.video.duration if self.video else 0.0

    @property
    def out_point(self) -> float:
        return self.trim_out if self.trim_out > 0 else self.duration

    # ------------------------------------------------------------------ layout
    def default_layout(self):
        H = self.ref_height
        W = REF_WIDTH
        m = 40
        roles = self.session.roles
        ws: List[Widget] = []
        ws.append(LapTimer(m, m, 430, 190))
        ws.append(DeltaBar(m, m + 200, 430, 64))
        ws.append(GCircle(W - m - 240, m, 240, 240))
        ws.append(SpeedDial(m, H - m - 320, 320, 320))
        if roles.get("throttle") or roles.get("brake"):
            ws.append(Pedals(m + 330, H - m - 250, 116, 250))
        ws.append(TrackMap(W - m - 360, H - m - 360, 360, 360))
        if roles.get("steering"):
            ws.append(Steering(W - m - 240, m + 260, 240, 240, {"panel": True}))
        extra_y = H - m - 110
        x = m + 460
        if roles.get("soc"):
            ws.append(BarGauge(x, extra_y + 30, 250, 80, {"channel": "@soc", "label": "SOC", "unit": "%"}))
            x += 262
        if roles.get("power"):
            ws.append(BarGauge(x, extra_y + 30, 250, 80, {"channel": "@power", "label": "Power", "unit": " kW",
                                                          "scale": 0.001, "decimals": 0}))
        self.widgets = ws
        self.dirty = True

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> dict:
        base = os.path.dirname(self.path) if self.path else None

        def rel(p):
            if p and base:
                try:
                    return os.path.relpath(p, base)
                except ValueError:
                    return p
            return p

        return dict(
            format="onboard-datavis", version=FORMAT_VERSION,
            video=rel(self.video.path) if self.video else None,
            sources=[dict(s.to_json(), path=rel(s.path)) for s in self.session.sources],
            roles=self.session.roles, primary=self.primary_source, link_offsets=self.link_offsets,
            widgets=[w.to_json() for w in self.widgets], theme=self.theme, accent=self.accent,
            speed_unit=self.speed_unit, trim=[self.trim_in, self.trim_out], export=self.export,
        )

    def save(self, path: Optional[str] = None):
        if path:
            self.path = path
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=2)
        self.dirty = False

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        pr = cls()
        pr.path = path
        base = os.path.dirname(os.path.abspath(path))

        def absp(p):
            if not p:
                return p
            return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

        if d.get("video"):
            pr.video = probe(absp(d["video"]))
        for s in d.get("sources", []):
            src = load_any(absp(s["path"]), s["id"], s.get("kind") if s.get("kind") != "racebox" else "csv",
                           s.get("options"))
            src.name = s.get("name", src.name)
            src.offset = float(s.get("offset", 0.0))
            src.options.update(s.get("options") or {})
            pr.session.add_source(src, auto_roles=False)
        pr.session.roles = dict(d.get("roles", {}))
        pr.primary_source = d.get("primary") or (pr.session.sources[0].id if pr.session.sources else None)
        pr.link_offsets = d.get("link_offsets", True)
        pr.widgets = [w for w in (widget_from_json(x) for x in d.get("widgets", [])) if w is not None]
        pr.theme = d.get("theme", "Broadcast")
        pr.accent = d.get("accent", "")
        pr.speed_unit = d.get("speed_unit", "km/h")
        pr.trim_in, pr.trim_out = (d.get("trim") or [0, 0])[:2]
        pr.export.update(d.get("export") or {})
        pr.rebuild_laps()
        pr.dirty = False
        return pr
