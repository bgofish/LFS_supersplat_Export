import json
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supersplat.camera_path import (
    build_keyframe_samples,
    build_supersplat_animation,
    LOOP_MODES,
)


class CameraPathExportTests(unittest.TestCase):
    def test_orbit_test_case_resolves_target_to_origin(self) -> None:
        # camera at (0,5,0) looking straight down; rotation encodes a 90-degree
        # rotation about a diagonal axis when read as [w,x,y,z]. Expected
        # look-at target is world origin.
        camera_path = {
            "clip_duration": 30.0,
            "keyframes": [
                {
                    "focal_length_mm": 35.0,
                    "position": [0, 5, 0],
                    "rotation": [0.0, 0.0, 0.7071068286895752, 0.7071068286895752],
                    "time": 0,
                },
                {
                    "focal_length_mm": 35.0,
                    "position": [0, 0, -5],
                    "rotation": [0.0, 0.0, 1.0, 0.0],
                    "time": 20,
                },
            ],
        }
        times, positions, targets, fov = build_keyframe_samples(camera_path, look_distance=5.0)
        self.assertAlmostEqual(targets[0], 0.0, places=3)
        self.assertAlmostEqual(targets[1], 0.0, places=3)
        self.assertAlmostEqual(targets[2], 0.0, places=3)
        self.assertAlmostEqual(targets[3], 0.0, places=3)
        self.assertAlmostEqual(targets[4], 0.0, places=3)
        self.assertAlmostEqual(targets[5], 0.0, places=3)
        self.assertEqual(len(fov), 2)

    def test_supersplat_json_uses_frame_numbers_not_seconds(self) -> None:
        # duration=6s at fps=30 with a sample at t=5s should land at frame 150,
        # matching the confirmed real-world sample (settings-db4b8a00.json).
        data = build_supersplat_animation(
            name="camera_path",
            times_seconds=[0.0, 5.0],
            positions_flat=[0, 0, 0, 1, 1, 1],
            targets_flat=[0, 0, 1, 1, 1, 2],
            fov_values=[75, 75],
            fps=30.0,
            loop_mode="repeat",
            smoothness=0.5,
        )
        self.assertEqual(data["animTracks"][0]["keyframes"]["times"], [0, 150])
        self.assertEqual(data["startMode"], "animTrack")
        self.assertIn("fov", data["animTracks"][0]["keyframes"]["values"])
        self.assertEqual(data["cameras"][0]["initial"]["fov"], 75)

    def test_loop_mode_once_serializes_as_none(self) -> None:
        self.assertEqual(LOOP_MODES, ("repeat", "none", "pingpong"))

    def test_schema_matches_real_supersplat_export_shape(self) -> None:
        sample_path = (
            Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "settings-sample.json"
        )
        if not sample_path.is_file():
            self.skipTest("reference sample fixture not present")
        sample = json.loads(sample_path.read_text())
        data = build_supersplat_animation(
            name="x", times_seconds=[0.0], positions_flat=[0, 0, 0],
            targets_flat=[0, 0, 1], fov_values=[75], fps=30.0,
        )
        self.assertEqual(set(data.keys()), set(sample.keys()))
        self.assertEqual(set(data["animTracks"][0].keys()), set(sample["animTracks"][0].keys()))
        self.assertEqual(
            set(data["animTracks"][0]["keyframes"]["values"].keys()),
            set(sample["animTracks"][0]["keyframes"]["values"].keys()),
        )
        self.assertEqual(set(data["cameras"][0].keys()), set(sample["cameras"][0].keys()))


if __name__ == "__main__":
    unittest.main()
