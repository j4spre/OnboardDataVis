# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the overlay onto the video with FFmpeg (decode pipe -> QPainter -> encode pipe)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from PySide6.QtGui import QImage

from .renderer import OverlayRenderer

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_ffmpeg_cache: Optional[str] = None
_encoder_cache: Optional[List[str]] = None


def ffmpeg_exe() -> str:
    global _ffmpeg_cache
    if _ffmpeg_cache:
        return _ffmpeg_cache
    cand = os.environ.get("ODV_FFMPEG")
    if cand and os.path.exists(cand):
        _ffmpeg_cache = cand
        return cand
    local = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ffmpeg",
                         "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.exists(local):
        _ffmpeg_cache = local
        return local
    p = shutil.which("ffmpeg")
    if p:
        _ffmpeg_cache = p
        return p
    try:
        import imageio_ffmpeg

        _ffmpeg_cache = imageio_ffmpeg.get_ffmpeg_exe()
        return _ffmpeg_cache
    except Exception as exc:
        raise RuntimeError("FFmpeg not found. Install it or run: pip install imageio-ffmpeg") from exc


def _run(args, **kw):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW, **kw)


ENCODERS: Dict[str, dict] = {
    "h264_nvenc": dict(label="H.264 – NVIDIA (NVENC)", args=["-preset", "p5", "-rc", "vbr", "-cq", "{q}", "-b:v", "0"], q=(23, 19, 16)),
    "hevc_nvenc": dict(label="H.265 – NVIDIA (NVENC)", args=["-preset", "p5", "-rc", "vbr", "-cq", "{q}", "-b:v", "0", "-tag:v", "hvc1"], q=(25, 21, 18)),
    "h264_qsv": dict(label="H.264 – Intel Quick Sync", args=["-global_quality", "{q}", "-preset", "slow"], q=(24, 20, 17)),
    "hevc_qsv": dict(label="H.265 – Intel Quick Sync", args=["-global_quality", "{q}", "-preset", "slow", "-tag:v", "hvc1"], q=(26, 22, 19)),
    "h264_amf": dict(label="H.264 – AMD (AMF)", args=["-quality", "quality", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"], q=(24, 20, 17)),
    "hevc_amf": dict(label="H.265 – AMD (AMF)", args=["-quality", "quality", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}", "-tag:v", "hvc1"], q=(26, 22, 19)),
    "libx264": dict(label="H.264 – CPU (x264)", args=["-preset", "medium", "-crf", "{q}"], q=(22, 18, 15)),
    "libx265": dict(label="H.265 – CPU (x265)", args=["-preset", "medium", "-crf", "{q}", "-tag:v", "hvc1"], q=(26, 22, 19)),
    "mpeg4": dict(label="MPEG-4 Part 2 – CPU (fallback)", args=["-q:v", "{q}"], q=(6, 3, 2)),
}
TRANSPARENT = {
    "prores4444": dict(label="ProRes 4444 with alpha (.mov)", ext=".mov",
                       args=["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]),
    "png": dict(label="PNG sequence with alpha (folder)", ext="", args=["-c:v", "png"]),
}
QUALITY = {"standard": 0, "high": 1, "max": 2}


def available_encoders() -> List[str]:
    """Encoders that actually work on this machine (tested with a tiny encode)."""
    global _encoder_cache
    if _encoder_cache is not None:
        return _encoder_cache
    exe = ffmpeg_exe()
    try:
        listed = _run([exe, "-hide_banner", "-encoders"], timeout=20).stdout.decode("utf-8", "replace")
    except Exception:
        listed = ""
    ok = []
    for name in ENCODERS:
        if f" {name} " not in listed:
            continue
        test = [exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.2:r=30",
                "-c:v", name, "-pix_fmt", "yuv420p", "-f", "null", "-"]
        try:
            if _run(test, timeout=30).returncode == 0:
                ok.append(name)
        except Exception:
            pass
    _encoder_cache = ok
    return ok


def pick_encoder(pref: str = "auto") -> str:
    av = available_encoders()
    if pref != "auto" and pref in av:
        return pref
    for n in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264", "mpeg4"):
        if n in av:
            return n
    return "libx264"


@dataclass
class ExportSettings:
    out_path: str
    width: int
    height: int
    t_in: float
    t_out: float
    encoder: str = "auto"
    quality: str = "high"
    audio: bool = True
    transparent: str = ""  # '' | 'prores4444' | 'png'
    fps: float = 0.0  # 0 = source


class Exporter:
    def __init__(self, project, settings: ExportSettings):
        self.project = project
        self.s = settings
        self.cancelled = False
        self.error: Optional[str] = None
        self.log_path: Optional[str] = None

    def cancel(self):
        self.cancelled = True

    def run(self, progress: Callable[[int, int, float], None] = None) -> bool:
        pr, s = self.project, self.s
        vi = pr.video
        exe = ffmpeg_exe()
        fps = s.fps or vi.fps
        fps_str = str(vi.fps_frac) if not s.fps else f"{fps:g}"
        dur = max(0.0, s.t_out - s.t_in)
        n_total = int(round(dur * fps))
        W, H = int(s.width) // 2 * 2, int(s.height) // 2 * 2
        renderer = OverlayRenderer(pr)
        ctx = renderer.context(s.t_in)
        log = tempfile.NamedTemporaryFile("w+b", suffix="_odv_ffmpeg.log", delete=False)
        self.log_path = log.name

        dec = None
        if not s.transparent:
            dec_cmd = [exe, "-hide_banner", "-v", "error", "-ss", f"{s.t_in:.6f}", "-i", vi.path, "-t", f"{dur:.6f}",
                       "-map", "0:v:0", "-vf", f"scale={W}:{H}:flags=bicubic", "-r", fps_str, "-pix_fmt", "bgra",
                       "-f", "rawvideo", "-"]
            dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=log, creationflags=_NO_WINDOW,
                                   bufsize=W * H * 4 * 2)

        enc_cmd = [exe, "-hide_banner", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}",
                   "-framerate", fps_str, "-i", "-"]
        if s.transparent:
            t = TRANSPARENT[s.transparent]
            out = s.out_path
            if s.transparent == "png":
                os.makedirs(out, exist_ok=True)
                out = os.path.join(out, "overlay_%06d.png")
            enc_cmd += t["args"] + [out]
        else:
            use_audio = s.audio and vi.has_audio
            if use_audio:
                enc_cmd += ["-ss", f"{s.t_in:.6f}", "-t", f"{dur:.6f}", "-i", vi.path]
            enc = pick_encoder(s.encoder)
            spec = ENCODERS.get(enc, ENCODERS["libx264"])
            q = spec["q"][QUALITY.get(s.quality, 1)]
            enc_cmd += ["-map", "0:v:0"] + (["-map", "1:a:0?", "-c:a", "aac", "-b:a", "192k"] if use_audio else [])
            enc_cmd += ["-c:v", enc] + [a.replace("{q}", str(q)) for a in spec["args"]]
            enc_cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", s.out_path]
        encp = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=log, creationflags=_NO_WINDOW,
                                bufsize=W * H * 4)
        frame_bytes = W * H * 4
        t0 = time.time()
        i = 0
        ok = True
        try:
            while not self.cancelled:
                if dec is not None:
                    buf = dec.stdout.read(frame_bytes)
                    if len(buf) < frame_bytes:
                        break
                    raw = bytearray(buf)
                    img = QImage(raw, W, H, W * 4, QImage.Format.Format_ARGB32)
                else:
                    if i >= n_total:
                        break
                    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
                    img.fill(0)
                t = s.t_in + i / fps
                renderer.render_image(img, t, ctx)
                if img.format() != QImage.Format.Format_ARGB32:
                    img = img.convertToFormat(QImage.Format.Format_ARGB32)
                encp.stdin.write(bytes(img.constBits())[:frame_bytes])
                i += 1
                if progress and (i % 5 == 0):
                    el = time.time() - t0
                    progress(i, n_total, el / i * max(0, n_total - i))
        except (BrokenPipeError, OSError) as exc:
            ok = False
            self.error = f"Encoder stopped: {exc}"
        finally:
            try:
                encp.stdin.close()
            except Exception:
                pass
            if dec is not None:
                try:
                    dec.stdout.close()
                    dec.kill()
                except Exception:
                    pass
            rc = encp.wait()
            log.close()
        if self.cancelled:
            return False
        if rc != 0:
            ok = False
            try:
                with open(self.log_path, "r", errors="replace") as f:
                    self.error = (self.error or "") + "\n" + f.read()[-2000:]
            except Exception:
                pass
        if progress:
            progress(i, n_total, 0.0)
        self.frames_written = i
        self.elapsed = time.time() - t0
        return ok
