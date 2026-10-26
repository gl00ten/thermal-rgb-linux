"""Small config/colour tests, with no OpenRGB hardware connection."""
import tempfile
import unittest
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thermal_rgb import read_config, rgb_for_temp, breath, sample_sensors, temperature_targets

ROOT = Path(__file__).resolve().parents[1]


class LogicTests(unittest.TestCase):
    def test_example_config(self):
        c = read_config(ROOT / "config.example.toml")
        self.assertEqual(c["devices"]["argb_led_count"], 8)
        self.assertEqual(c["animation"]["cpu_max_brightness"], 0.4)

    def test_blue_below_room_temp(self):
        c = rgb_for_temp(20, 25, 90, 1)
        self.assertEqual(c, (0, 64, 255))

    def test_red_at_threshold(self):
        c = rgb_for_temp(90, 25, 90, 0.4)
        self.assertEqual(c, (102, 0, 0))

    def test_breath_min_and_max(self):
        a = {"breathing": True, "breath_min": 0.5, "breath_period": 6.0}
        self.assertAlmostEqual(breath(0, a), 0.5)
        self.assertAlmostEqual(breath(3, a), 1.0)
        self.assertAlmostEqual(breath(6, a), 0.5)

    def test_sensor_failure_does_not_block_other_sensor(self):
        for failed in ("cpu", "gpu"):
            with self.subTest(failed=failed):
                readings = {"cpu": (40, 1), "gpu": (50, 1)}
                with patch("thermal_rgb.cpu_temperature", return_value=(60, "test"),
                           side_effect=RuntimeError("offline") if failed == "cpu" else None), \
                     patch("thermal_rgb.gpu_temperature", return_value=70,
                           side_effect=RuntimeError("offline") if failed == "gpu" else None), \
                     patch("thermal_rgb.time.monotonic", return_value=20):
                    sample_sensors(readings, threading.Lock(), 0)
                self.assertEqual(readings[failed][1], 1)
                healthy = "gpu" if failed == "cpu" else "cpu"
                self.assertEqual(readings[healthy][1], 20)

    def test_missing_stale_and_recovered_sensors(self):
        readings = {"cpu": (None, 0), "gpu": (50, 19)}
        self.assertEqual(temperature_targets(readings, 20, 10, (90, 85)), (90, 50))
        readings["cpu"] = (40, 1)
        self.assertEqual(temperature_targets(readings, 20, 10, (90, 85)), (90, 50))
        readings["cpu"] = (45, 20)
        self.assertEqual(temperature_targets(readings, 20, 10, (90, 85)), (45, 50))

    def test_bad_config(self):
        config = (ROOT / "config.example.toml").read_text()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "broken.toml"
            path.write_text(config.replace("cpu_max_brightness = 0.40", "cpu_max_brightness = 2.0"))
            with self.assertRaises(ValueError):
                read_config(path)


if __name__ == "__main__":
    unittest.main()
