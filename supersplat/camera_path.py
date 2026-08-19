"""Camera path -> SuperSplat animation JSON export.

Ported from the CamPath-HTML plugin (core.py / supersplat_export.py), which
independently solved and confirmed the coordinate conventions this depends
on:
  - camera_path.json's rotation array is [w, x, y, z], not [x, y, z, w]
  - camera_path.json's coordinate frame needs a 180-degree world yaw
    (-x, y, -z) relative to LFS's own scene/viewer convention
  - local forward vector is (0, 0, -1)

Those corrections are baked in as fixed constants here rather than exposed
as UI knobs -- this plugin only needs to read camera_path.json (or the live
sequencer) and produce a matching SuperSplat animation JSON, so the
axis/quaternion-convention controls that CamPath-HTML exposes for
troubleshooting new export sources aren't relevant to this simpler flow.

SuperSplat animation JSON schema (confirmed against a real export):
    {
      "version": 2, "tonemapping": "linear", "highPrecisionRendering": false,
      "background": {"color": [r,g,b]},
      "postEffectSettings": {...},  -- left at SuperSplat's own defaults
      "animTracks": [{
          "name": ..., "duration": <seconds>, "frameRate": <fps>,
          "loopMode": ..., "interpolation": ..., "smoothness": ...,
          "keyframes": {
              "times": [<frame numbers, NOT seconds>, ...],
              "values": {"position": [...], "target": [...], "fov": [...]}
          }
      }],
      "cameras": [{"initial": {"position": [...], "target": [...], "fov": ...}}],
      "annotations": [],
      "startMode": "animTrack"
    }
`times` are frame numbers (time_seconds * fps), confirmed by cross-checking
a sample export's duration/frameRate/largest-time-value against its
accompanying UI screenshot's frame counter.
"""

from __future__ import annotations

import math
import tempfile
import os
import json
from pathlib import Path
from typing import Any, Optional

# Confirmed-working defaults -- see module docstring.
_AXIS_ORDER = ((0, 1, 2), (-1, 1, -1))  # (-x, y, -z), as (index_order, signs)
_FORWARD_LOCAL = (0.0, 0.0, -1.0)
_SENSOR_WIDTH_MM = 36.0
_LOOK_DISTANCE = 10.0

LOOP_MODES = ("repeat", "none", "pingpong")  # JSON values; "none" displays as "once" in the UI
INTERPOLATION = "spline"


def _permute(v):
    idx, signs = _AXIS_ORDER
    return [signs[i] * v[idx[i]] for i in range(3)]


def _reorder_quat_wxyz(raw):
    w, x, y, z = raw
    return (x, y, z, w)


def _quat_to_matrix(x, y, z, w):
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ]


def _rotate_vec(quat_xyzw, v):
    m = _quat_to_matrix(*quat_xyzw)
    return [
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    ]


def _compute_target(position, rotation_xyzw, distance):
    fwd = _rotate_vec(rotation_xyzw, _FORWARD_LOCAL)
    norm = math.sqrt(sum(c * c for c in fwd)) or 1.0
    fwd = [c / norm for c in fwd]
    return [position[i] + fwd[i] * distance for i in range(3)]


def _focal_length_to_fov_deg(focal_mm: float, sensor_width_mm: float = _SENSOR_WIDTH_MM) -> float:
    return math.degrees(2 * math.atan(sensor_width_mm / (2 * focal_mm)))


def build_keyframe_samples(camera_path: dict, look_distance: float = _LOOK_DISTANCE):
    """Sample every keyframe in a camera_path.json-shaped dict into
    (times_seconds, positions_flat, targets_flat, fov_list)."""
    keyframes = camera_path["keyframes"]
    times = [kf["time"] for kf in keyframes]

    positions_flat, targets_flat, fov_list = [], [], []
    for kf in keyframes:
        pos = kf["position"]
        rot = _reorder_quat_wxyz(kf["rotation"])
        tgt = _compute_target(pos, rot, look_distance)
        positions_flat.extend(_permute(pos))
        targets_flat.extend(_permute(tgt))
        fov_list.append(_focal_length_to_fov_deg(kf["focal_length_mm"]))

    return times, positions_flat, targets_flat, fov_list


def build_supersplat_animation(name: str, times_seconds, positions_flat, targets_flat,
                                fov_values, fps: float, loop_mode: str = "repeat",
                                smoothness: float = 0.5,
                                background_color=(0.0, 0.0, 0.0)) -> dict:
    times_frames = [int(round(t * fps)) for t in times_seconds]
    duration_seconds = (times_seconds[-1] - times_seconds[0]) if times_seconds else 0.0

    initial_position = list(positions_flat[0:3]) if positions_flat else [0.0, 0.0, 0.0]
    initial_target = list(targets_flat[0:3]) if targets_flat else [0.0, 0.0, 0.0]
    initial_fov = fov_values[0] if fov_values else 60.0

    return {
        "version": 2,
        "tonemapping": "linear",
        "highPrecisionRendering": False,
        "background": {"color": list(background_color)},
        "postEffectSettings": {
            "sharpness": {"enabled": False, "amount": 0},
            "bloom": {"enabled": False, "intensity": 0.1, "blurLevel": 2},
            "grading": {"enabled": False, "brightness": 1, "contrast": 1, "saturation": 1, "tint": [1, 1, 1]},
            "vignette": {"enabled": False, "intensity": 0.5, "inner": 0.3, "outer": 0.75, "curvature": 1},
            "fringing": {"enabled": False, "intensity": 0.5},
        },
        "animTracks": [
            {
                "name": name or "camera_path",
                "duration": duration_seconds,
                "frameRate": fps,
                "loopMode": loop_mode,
                "interpolation": INTERPOLATION,
                "smoothness": smoothness,
                "keyframes": {
                    "times": times_frames,
                    "values": {
                        "position": positions_flat,
                        "target": targets_flat,
                        "fov": fov_values,
                    },
                },
            }
        ],
        "cameras": [
            {"initial": {"position": initial_position, "target": initial_target, "fov": initial_fov}}
        ],
        "annotations": [],
        "startMode": "animTrack",
    }


class CameraPathExporter:
    """Main-thread adapter for reading a camera path (file or live sequencer)
    and writing a SuperSplat animation JSON. Mirrors LfsExportAdapter's role
    for the scene/PLY export side of this plugin."""

    def __init__(self, lf: Any) -> None:
        self.lf = lf

    def read_from_sequencer(self) -> dict:
        """Pull the LFS sequencer's current keyframes via the same
        save-to-tempfile round trip used by the keyframe editor
        (lf.ui doesn't expose an in-memory getter)."""
        tmp = tempfile.mktemp(suffix=".json")
        try:
            self.lf.ui.save_camera_path(tmp)
            with open(tmp, "r") as f:
                return json.load(f)
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

    def read_from_file(self, path: str) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def build_json(self, camera_path: dict, name: str, loop_mode: str,
                    smoothness: float, fps: float) -> dict:
        times, positions, targets, fov_list = build_keyframe_samples(camera_path)
        return build_supersplat_animation(
            name=name, times_seconds=times, positions_flat=positions,
            targets_flat=targets, fov_values=fov_list, fps=fps,
            loop_mode=loop_mode, smoothness=smoothness,
        )

    def save_json(self, path: str, data: dict) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
