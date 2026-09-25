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
from .video import VideoInfo, VideoTransform, display_size, probe

REF_WIDTH = 1920.0
FORMAT_VERSION = 1
OVERLAY_FORMAT = "onboard-datavis-overlay"


def _file_props(w: Widget):
    return [p.name for p in w.all_props() if p.kind == "file"]


def _rel(path: str, base: Optional[str]) -> str:
    if path and base and os.path.isabs(path):
        try:
            return os.path.relpath(path, base)
        except ValueError:  # other drive on Windows
            return path
    return path


def _abs(path: str, base: Optional[str]) -> str:
    if path and base and not os.path.isabs(path):
        return os.path.normpath(os.path.join(base, path))
    return path


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
        self.video_tf = VideoTransform()
        self.dirty = False

    # ------------------------------------------------------------------ geometry
    @property
    def ref_height(self) -> float:
        if self.video and self.video.width:
            w, h = self.display_size()
            return REF_WIDTH * h / w
        return 1080.0

    def set_video_transform(self, **kw):
        """Change rotation / mirroring / levelling; keeps bottom-anchored widgets at the bottom."""
        old_h = self.ref_height
        for k, v in kw.items():
            setattr(self.video_tf, k, v)
        new_h = self.ref_height
        if abs(new_h - old_h) > 1:
            for w in self.widgets:
                if w.y + w.h / 2 > old_h / 2:
                    w.y += new_h - old_h
                w.y = max(0.0, min(w.y, new_h - min(w.h, new_h)))
        self.dirty = True

    def display_size(self):
        """Video size after display-matrix rotation and the user's orientation fix."""
        if not self.video:
            return 1920, 1080
        return display_size(self.video, self.video_tf)

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
            widgets=[self._widget_json(w, base) for w in self.widgets], theme=self.theme, accent=self.accent,
            speed_unit=self.speed_unit, trim=[self.trim_in, self.trim_out], export=self.export,
            video_transform=self.video_tf.to_json(),
        )

    @staticmethod
    def _widget_json(w: Widget, base: Optional[str]) -> dict:
        d = w.to_json()
        for k in _file_props(w):
            d["props"][k] = _rel(d["props"].get(k, ""), base)
        return d

    # ------------------------------------------------------------------ overlay configuration
    def overlay_json(self, base: Optional[str] = None) -> dict:
        """Everything that defines the look, independent of video and data files."""
        roles = {}
        for role, key in self.session.roles.items():
            src, ch = self.session.resolve(key)
            name = ch.name if ch is not None else key.split(":", 1)[-1]
            roles[role] = dict(key=key, name=name, kind=src.kind if src is not None else "")
        sources = {s.id: dict(kind=s.kind, name=s.name) for s in self.session.sources}
        return dict(format=OVERLAY_FORMAT, version=1, ref_width=REF_WIDTH, ref_height=self.ref_height,
                    theme=self.theme, accent=self.accent, speed_unit=self.speed_unit,
                    widgets=[self._widget_json(w, base) for w in self.widgets], roles=roles, sources=sources)

    def save_overlay(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.overlay_json(os.path.dirname(os.path.abspath(path))), f, indent=2)

    def remap_key(self, key: str, saved_sources: dict) -> str:
        """Point a saved "sid:name" reference at the matching channel in this project, if any."""
        if not key or key.startswith("@") or ":" not in key:
            return key
        sid, name = key.split(":", 1)
        sess = self.session
        kind = (saved_sources.get(sid) or {}).get("kind", "")
        src = sess.source(sid)
        if src is not None and src.has(name) and (not kind or src.kind == kind):
            return key
        # same kind of source first, then anything with that channel name
        same_kind = [s for s in sess.sources if kind and s.kind == kind]
        for s in same_kind:
            if s.has(name):
                return f"{s.id}:{name}"
        found = sess.find_by_name(name, sid)
        if found:
            return f"{found[0].id}:{found[1]}"
        return key  # keep; it re-matches by name when matching data is added

    def load_overlay(self, path: str, replace: bool = True) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("format") not in (OVERLAY_FORMAT, "onboard-datavis"):
            raise ValueError("Not an Onboard DataVis overlay file")
        base = os.path.dirname(os.path.abspath(path))
        saved_sources = d.get("sources") or {}
        old_h = float(d.get("ref_height") or self.ref_height)
        new_h = self.ref_height
        widgets = []
        for wd in d.get("widgets", []):
            w = widget_from_json(wd)
            if w is None:
                continue
            for p in w.all_props():
                if p.kind == "channel" and w.props.get(p.name):
                    w.props[p.name] = self.remap_key(w.props[p.name], saved_sources)
                elif p.kind == "file":
                    w.props[p.name] = _abs(w.props.get(p.name, ""), base)
            if abs(old_h - new_h) > 1 and w.y + w.h / 2 > old_h / 2:
                w.y += new_h - old_h  # keep bottom-anchored widgets at the bottom
            if not replace:
                import uuid

                w.id = uuid.uuid4().hex[:8]
            widgets.append(w)
        # roles: take the overlay's mapping where it matches data here; keep unmatched ones pending
        for role, info in (d.get("roles") or {}).items():
            key = info.get("key", "") if isinstance(info, dict) else str(info)
            nk = self.remap_key(key, saved_sources)
            if self.session.resolve(nk)[1] is not None or not self.session.resolve_ref("@" + role)[1]:
                self.session.roles[role] = nk
        self.session.invalidate()
        self.widgets = widgets if replace else self.widgets + widgets
        if replace:
            self.theme = d.get("theme", self.theme)
            self.accent = d.get("accent", self.accent)
            self.speed_unit = d.get("speed_unit", self.speed_unit)
        self.rebuild_laps()
        self.dirty = True
        missing = sorted({r for w in widgets for r in w.missing_channels(self.session)})
        return dict(widgets=len(widgets), missing=missing)

    def missing_channels(self) -> List[str]:
        return sorted({r for w in self.widgets for r in w.missing_channels(self.session)})

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
        for w in pr.widgets:
            for k in _file_props(w):
                w.props[k] = _abs(w.props.get(k, ""), base)
        pr.theme = d.get("theme", "Broadcast")
        pr.accent = d.get("accent", "")
        pr.speed_unit = d.get("speed_unit", "km/h")
        pr.trim_in, pr.trim_out = (d.get("trim") or [0, 0])[:2]
        pr.export.update(d.get("export") or {})
        pr.video_tf = VideoTransform.from_json(d.get("video_transform"))
        pr.rebuild_laps()
        pr.dirty = False
        return pr
