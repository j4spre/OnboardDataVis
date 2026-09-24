# SPDX-License-Identifier: GPL-3.0-or-later
"""Core tests with synthetic data (no sample files needed).  Run:  python -m pytest -q"""
import datetime as dt
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from odv.data.laps import LapModel  # noqa: E402
from odv.data.loaders import load_any  # noqa: E402
from odv.data.model import Session  # noqa: E402
from odv import sync  # noqa: E402

LAT0, LON0 = 49.33, 8.566
R = 150.0  # m, circular test track -> lap length 942 m


def circle_track(n_laps=4, v=20.0, rate=10.0, t_pre=5.0):
    """Car waits t_pre s, then drives laps at constant speed v on a circle."""
    lap_len = 2 * math.pi * R
    T = t_pre + n_laps * lap_len / v + 3
    t = np.arange(0, T, 1 / rate)
    s = np.clip((t - t_pre) * v, 0, None)
    ang = s / R
    x, y = R * np.sin(ang), -R * np.cos(ang) + R
    lat = LAT0 + np.degrees(y / 6371000)
    lon = LON0 + np.degrees(x / (6371000 * math.cos(math.radians(LAT0))))
    speed = np.where(t >= t_pre, v, 0.0)
    lap = np.floor(s / lap_len).astype(int) + (t >= t_pre)
    yaw = np.where(t >= t_pre, -math.degrees(v / R), 0.0)  # clockwise
    yaw = yaw + 8 * np.sin(t * 0.7) * (t >= t_pre)  # some texture for correlation
    return t, lat, lon, speed, lap, yaw, lap_len / v


def write_racebox_csv(path, **kw):
    t, lat, lon, speed, lap, yaw, lap_t = circle_track(**kw)
    with open(path, "w") as f:
        f.write("Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ\n")
        for i in range(len(t)):
            f.write(f"{i + 1},{t[i]:.1f},{lat[i]:.7f},{lon[i]:.7f},100,{speed[i]:.2f},0,0.1,1,{lap[i]},0,0,{yaw[i]:.2f}\n")
    return t, lap_t


def test_racebox_csv_and_laps(tmp_path):
    p = tmp_path / "racebox.csv"
    t, lap_t = write_racebox_csv(str(p))
    src = load_any(str(p), "rb")
    assert src.kind == "racebox"
    roles = src.default_roles()
    for r in ("lat", "lon", "speed", "lap", "g_lat", "g_long", "yaw_rate"):
        assert r in roles
    assert src.meta["speed_factor"] == pytest.approx(1.0)  # m/s detected from GPS
    s = Session()
    s.add_source(src)
    lm = LapModel(s)
    done = lm.complete_laps()
    assert len(done) >= 3
    for l in done:
        assert l.duration == pytest.approx(lap_t, abs=0.15)


def test_generic_csv_kmh_absolute_time(tmp_path):
    t, lat, lon, speed, lap, yaw, _ = circle_track(n_laps=2)
    t0 = dt.datetime(2026, 8, 16, 9, 30, tzinfo=dt.timezone.utc)
    p = tmp_path / "logger.csv"
    with open(p, "w") as f:
        f.write("# exported by some logger\n# driver: test\n")
        f.write("timestamp;lat;lon;speed (km/h);rpm\n")
        for i in range(len(t)):
            ts = (t0 + dt.timedelta(seconds=float(t[i]))).isoformat()
            f.write(f"{ts};{lat[i]:.7f};{lon[i]:.7f};{speed[i] * 3.6:.2f};{3000 + speed[i] * 100:.0f}\n")
    src = load_any(str(p), "g")
    assert src.utc_start == t0
    assert src.t_end == pytest.approx(t[-1], abs=0.01)
    assert src.meta["speed_factor"] == pytest.approx(1 / 3.6)
    sp = src.get(src.default_roles()["speed"])
    assert sp.unit == "m/s"
    assert np.nanmax(sp.v) == pytest.approx(20.0, rel=0.01)
    assert "rpm" in src.default_roles()


def test_vbo(tmp_path):
    t, lat, lon, speed, lap, yaw, _ = circle_track(n_laps=1)
    p = tmp_path / "log.vbo"
    lines = ["File created on 16/08/2026 @ 11:12", "", "[header]", "satellites", "time", "latitude", "longitude",
             "velocity kmh", "heading", "", "[column names]", "sats time lat long velocity heading", "", "[data]"]
    for i in range(len(t)):
        tod = 11 * 3600 + 12 * 60 + t[i]
        hh, mm = int(tod // 3600), int(tod % 3600 // 60)
        ss = tod - hh * 3600 - mm * 60
        lines.append(f"010 {hh:02d}{mm:02d}{ss:05.2f} +{lat[i] * 60:.5f} -{lon[i] * 60:.5f} {speed[i] * 3.6:.2f} 0")
    p.write_text("\n".join(lines))
    src = load_any(str(p), "v")
    la = src.get("Latitude")
    lo = src.get("Longitude")
    assert la.v[0] == pytest.approx(lat[0], abs=1e-6)
    assert lo.v[0] == pytest.approx(lon[0], abs=1e-6)  # +ve West convention handled
    assert src.utc_start.hour == 11
    assert np.nanmax(src.get("Speed").v) == pytest.approx(20.0, rel=0.01)


def test_motion_style_correlation():
    rng = np.random.default_rng(1)
    dt_ = 0.1
    t = np.arange(0, 600, dt_)
    data = np.convolve(rng.normal(size=len(t)), np.ones(15) / 15, "same")
    true_off = 123.45
    vt = np.arange(0, 90, 1 / 15)
    vid = -2.5 * np.interp(vt + true_off, t, data) + rng.normal(scale=0.05, size=len(vt))  # flipped sign + scale
    res = sync.correlate_search(vt, vid, t, data, 0, 500, step=0.1, allow_flip=True)
    off, score, margin, sign = res
    assert off == pytest.approx(true_off, abs=0.03)
    assert sign == -1


def test_align_sources():
    from odv.data.model import Channel, DataSource

    rng = np.random.default_rng(3)
    ta = np.arange(0, 900, 0.1)
    va = 15 + 8 * np.convolve(rng.normal(size=len(ta)), np.ones(40) / 40 * 6, "same")
    a = DataSource("a", "gps")
    a.add(Channel("Speed", ta, va, "m/s"))
    a.meta["roles"] = {"speed": "Speed"}
    b = DataSource("b", "car")
    tb = np.arange(0, 600, 0.01)
    vb = np.interp(tb + 217.3, ta, va) * 1.03
    vb[5000] = 400.0  # CAN glitch
    b.add(Channel("wheel_speed", tb, vb, "m/s"))
    b.meta["roles"] = {"speed": "wheel_speed"}
    s = Session()
    s.add_source(a)
    s.add_source(b, auto_roles=False)
    a.offset = 5.0
    res = sync.align_sources(s, a, b)
    # t_a = t_b + 217.3 ;  t_a = tv + 5  ->  t_b = tv + 5 - 217.3
    assert res.offset == pytest.approx(5.0 - 217.3, abs=0.03)


def test_timestamp_timezone_correction():
    from odv.data.model import DataSource

    src = DataSource("x", "x")
    src.utc_start = dt.datetime(2026, 8, 16, 9, 0, tzinfo=dt.timezone.utc)
    src.meta.update(t_start=0, t_end=3600)
    cam = dt.datetime(2026, 8, 16, 11, 10, tzinfo=dt.timezone.utc)  # local CEST written as UTC
    r = sync.timestamp_offset(cam, src)
    assert r.offset == pytest.approx(600.0)
    assert "+2 h" in r.detail


def test_render_widgets(tmp_path):
    from PySide6.QtGui import QGuiApplication, QImage

    app = QGuiApplication.instance() or QGuiApplication([])
    from odv.project import Project
    from odv.render.renderer import OverlayRenderer
    from odv.render.widgets import WIDGET_TYPES

    p = tmp_path / "racebox.csv"
    write_racebox_csv(str(p))
    pr = Project()
    pr.add_data(str(p))
    pr.default_layout()
    for i, cls in enumerate(WIDGET_TYPES.values()):
        pr.widgets.append(cls(20 + (i % 6) * 300, 20 + (i // 6) * 300))
    img = QImage(1280, 720, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    OverlayRenderer(pr).render_image(img, 30.0)
    # something was drawn
    arr = np.frombuffer(bytes(img.constBits()), dtype=np.uint8).reshape(720, 1280, 4)
    assert (arr[..., 3] > 0).mean() > 0.2
    # project round trip
    pr.save(str(tmp_path / "p.odv"))
    pr2 = Project.load(str(tmp_path / "p.odv"))
    assert len(pr2.widgets) == len(pr.widgets)
    assert pr2.session.roles == pr.session.roles


# Optional: point ODV_SAMPLE_CSV at a real RaceBox export to run an extra smoke test.
SAMPLE_CSV = os.environ.get("ODV_SAMPLE_CSV", "")


@pytest.mark.skipif(not os.path.isfile(SAMPLE_CSV), reason="set ODV_SAMPLE_CSV to run")
def test_sample_racebox_csv():
    src = load_any(SAMPLE_CSV, "rb")
    s = Session()
    s.add_source(src)
    lm = LapModel(s)
    assert src.default_roles().get("speed")
    assert lm.best_lap() is not None
