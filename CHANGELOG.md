# Changelog

## 1.1.0 – 2026-09-25

- **Widgets from any signal**: new dial gauge (arc or needle). Numeric, bar and live plot were
  reworked. Every widget has scale and offset, an automatic or fixed range, upper/lower warning
  limits and colours. Bars can be centred on zero. The live plot takes 4 channels, each with its
  own colour, scale and name, plus a fixed or shared Y axis, grid, legend with live values, and a
  "now" marker on the right or in the centre.
- **Signals tab**: searchable list of every channel in every loaded file. One click adds a
  channel as a widget, or to the selected plot.
- **Filters on every data widget**: zero-phase Butterworth low-pass (cutoff in Hz) and a Hampel
  outlier filter (threshold in σ, adjustable window).
- **Overlay files** (`.odvoverlay`): save, load or append a complete overlay. Widgets whose
  channels are missing are kept and marked ⚠. Channels reconnect by name when matching CSV or MF4
  data is added, across different source ids and loosely matching names.
- **Text layer**: any installed font, pt size (at 1080p), bold/italic, colour, outline, shadow,
  background, letter and line spacing, alignment, rotation, multi-line text.
- **Image layer**: transparent PNGs and other formats, keep aspect, rotation, mirroring,
  opacity. Available as *Insert ▸ Image…*. **Animated GIFs** and animated WebP are timed by the
  video clock, so they play the same in preview and export. Speed, loop and start time are
  adjustable.
- **Custom steering wheel image**: the steering widget can rotate any image (e.g. a photo of the
  car's wheel), with size and angle offset.
- **Blur / pixelate regions**: rectangle, rounded or ellipse shape; adjustable strength; hard or
  soft edge. Drawn beneath the gauges, onto the video.
- **Video orientation**: 90° rotation, horizontal/vertical mirroring, fine levelling with
  zoom-to-fill. Honours phone rotation metadata. The preview, export and motion sync match exactly.
- Menus *Insert* and *Overlay*. The inspector groups settings into collapsible sections and
  greys out settings that don't apply.
- Live plot history is sampled on a time-anchored grid, so it scrolls without wiggling during playback.
- Export resolutions follow the rotated output (portrait video gives portrait presets).

## 1.0.0 – 2026-09-24

First release.

- Import: RaceBox CSV (all presets), generic CSV/TSV with import dialog, ASAM MF4/MDF (asammdf, lazy channel loading, CAN glitch filter), Racelogic/RaceBox VBO, GPX.
- Sync: timestamps with automatic time-zone correction, auto-sync from video motion (optical flow vs gyro / GPS heading), speed cross-correlation between logs, frame nudging and sync points.
- Laps from logger lap channel or a start/finish gate; live delta to best lap.
- 12 widgets, 3 themes, drag-and-drop layout editor with snapping.
- Export via FFmpeg with NVENC / Quick Sync / AMF detection; transparent ProRes 4444 or PNG sequence.
- Command line: `new`, `render`, `still`.
