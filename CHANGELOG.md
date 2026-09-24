# Changelog

## 1.0.0 – 2026-09-24

First release.

- Import: RaceBox CSV (all presets), generic CSV/TSV with import dialog, ASAM MF4/MDF (asammdf, lazy channel loading, CAN glitch filter), Racelogic/RaceBox VBO, GPX.
- Sync: timestamps with automatic time-zone correction, auto-sync from video motion (optical flow vs gyro / GPS heading), speed cross-correlation between logs, frame nudging and sync points.
- Laps from logger lap channel or a start/finish gate; live delta to best lap.
- 12 widgets, 3 themes, drag-and-drop layout editor with snapping.
- Export via FFmpeg with NVENC / Quick Sync / AMF detection; transparent ProRes 4444 or PNG sequence.
- Command line: `new`, `render`, `still`.
