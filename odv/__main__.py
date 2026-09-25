# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point.

    python -m odv                      # open the editor
    python -m odv project.odv          # open a project in the editor
    python -m odv render project.odv out.mp4 [--start S --end S --width W --height H --encoder E]
    python -m odv new video.mp4 log.csv [car.mf4 ...] -o project.odv   # auto-sync + default layout
    python -m odv still project.odv t out.png
"""
from __future__ import annotations

import argparse
import os
import sys


def _headless_app():
    if "QT_QPA_PLATFORM" not in os.environ and not sys.platform.startswith("win"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication(sys.argv[:1])


def cmd_new(a):
    _headless_app()
    from .project import Project
    from . import sync

    pr = Project()
    pr.set_video(a.video)
    for d in a.data:
        pr.add_data(d)
    prim = pr.primary()
    ts = sync.timestamp_offset(pr.video.creation_time, prim)
    print("timestamp:", ts)
    print("analysing video motion…")
    mo = sync.motion_signal(a.video)
    res = sync.motion_sync(pr.session, prim, mo)
    print("motion:", res)
    if res:
        pr.set_offset(prim.id, res.offset, propagate=False)
    for s in pr.session.sources:
        if s is not prim:
            r2 = sync.align_sources(pr.session, prim, s)
            print(f"align {s.name}:", r2)
            if r2:
                s.offset = r2.offset
    pr.default_layout()
    pr.save(os.path.abspath(a.output))
    print("saved", a.output)


def cmd_render(a):
    _headless_app()
    from .project import Project
    from .render.export import ExportSettings, Exporter

    pr = Project.load(a.project)
    dw, dh = pr.display_size()
    W = a.width or dw
    H = a.height or int(round(W * dh / dw / 2) * 2)
    st = ExportSettings(a.out, W, H, a.start if a.start is not None else pr.trim_in,
                        a.end if a.end is not None else pr.out_point, a.encoder, a.quality,
                        transparent=a.transparent or "")
    ex = Exporter(pr, st)

    def prog(i, n, eta):
        print(f"\r{i}/{n} frames  ETA {eta:5.0f}s", end="", flush=True)

    ok = ex.run(prog)
    print()
    if not ok:
        print("FAILED:", ex.error)
        sys.exit(1)
    print(f"done: {ex.frames_written} frames in {ex.elapsed:.1f}s ({ex.frames_written / max(ex.elapsed, 1e-6):.1f} fps)")


def cmd_still(a):
    _headless_app()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from .project import Project
    from .render.renderer import OverlayRenderer
    from .video import VideoReader

    pr = Project.load(a.project)
    from .video import transform_qimage

    rd = VideoReader(pr.video.path, max_width=max(pr.video.width, pr.video.height))
    ft, arr = rd.frame_at(a.time)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
    img = transform_qimage(img, pr.video, pr.video_tf).convertToFormat(QImage.Format.Format_ARGB32)
    if a.width:
        img = img.scaledToWidth(a.width, Qt.TransformationMode.SmoothTransformation)
    OverlayRenderer(pr).render_image(img, ft)
    img.save(a.out)
    print("saved", a.out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("render", "new", "still"):
        ap = argparse.ArgumentParser(prog="odv")
        sub = ap.add_subparsers(dest="cmd")
        r = sub.add_parser("render")
        r.add_argument("project")
        r.add_argument("out")
        r.add_argument("--start", type=float)
        r.add_argument("--end", type=float)
        r.add_argument("--width", type=int)
        r.add_argument("--height", type=int)
        r.add_argument("--encoder", default="auto")
        r.add_argument("--quality", default="high", choices=["standard", "high", "max"])
        r.add_argument("--transparent", choices=["prores4444", "png"])
        n = sub.add_parser("new")
        n.add_argument("video")
        n.add_argument("data", nargs="+")
        n.add_argument("-o", "--output", default="project.odv")
        s = sub.add_parser("still")
        s.add_argument("project")
        s.add_argument("time", type=float)
        s.add_argument("out")
        s.add_argument("--width", type=int)
        a = ap.parse_args(argv)
        {"render": cmd_render, "new": cmd_new, "still": cmd_still}[a.cmd](a)
        return
    from .ui.app import run_gui

    run_gui(argv[0] if argv else None)


if __name__ == "__main__":
    main()
