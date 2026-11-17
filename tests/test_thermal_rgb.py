"""Configuration, runtime and installer tests using mocked hardware and services."""
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thermal_rgb import read_config, rgb_for_temp, sample_sensors, temperature_targets, Output, run, connection_closed, setup_rgb

ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG = ROOT / "examples/x570-rtx3080.toml"


class LogicTests(unittest.TestCase):
    def test_discovery_lists_devices_without_valid_outputs_or_hardware_writes(self):
        from thermal_rgb import main
        zone = SimpleNamespace(name="Header", leds=[1, 2])
        device = SimpleNamespace(name="Board", zones=[zone])
        client = Mock(devices=[device])
        module = SimpleNamespace(OpenRGBClient=Mock(return_value=client))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[openrgb]\nport = 7777\n')
            with patch.dict(sys.modules, {"openrgb": module}), \
                 patch("sys.argv", ["thermal_rgb.py", "--config", str(config), "--list-devices"]), \
                 patch("builtins.print") as output:
                main()
            output.assert_any_call("Board")
            output.assert_any_call("  Header (2 LEDs)")
            module.OpenRGBClient.assert_called_once_with("127.0.0.1", 7777, "thermal-rgb-inspect")
            client.disconnect.assert_called_once()

    def test_check_devices_does_not_initialize_lighting(self):
        from thermal_rgb import main
        device = Mock(name="device")
        device.name = "YOUR OPENRGB DEVICE NAME"
        client = Mock(devices=[device])
        module = SimpleNamespace(OpenRGBClient=Mock(return_value=client))
        with patch.dict(sys.modules, {"openrgb": module}), \
             patch("sys.argv", ["thermal_rgb.py", "--config", str(ROOT / "config.example.toml"), "--check-devices"]), \
             patch("builtins.print"):
            main()
        device.set_mode.assert_not_called()
        device.set_color.assert_not_called()
        device.update.assert_not_called()
        client.disconnect.assert_called_once()

    def test_missing_target_lists_available_names(self):
        from thermal_rgb import match
        with self.assertRaisesRegex(RuntimeError, "Available: 'Actual Board'"):
            match([SimpleNamespace(name="Actual Board")], "Old Board")

    def test_example_config(self):
        c = read_config(LEGACY_CONFIG)
        self.assertEqual(c["devices"]["argb_led_count"], 8)
        self.assertEqual(c["animation"]["cpu_max_brightness"], 0.4)

    def test_blue_below_room_temp(self):
        c = rgb_for_temp(20, 25, 90, 1)
        self.assertEqual(c, (0, 64, 255))

    def test_red_at_threshold(self):
        c = rgb_for_temp(90, 25, 90, 0.4)
        self.assertEqual(c, (102, 0, 0))

    def test_sensor_failure_does_not_block_other_sensor(self):
        for failed in ("cpu", "gpu"):
            with self.subTest(failed=failed):
                readings = {"cpu": (40, 1), "gpu": (50, 1)}
                with patch("thermal_rgb.cpu_temperature", return_value=(60, "test"),
                           side_effect=RuntimeError("offline") if failed == "cpu" else None), \
                     patch("thermal_rgb.gpu_temperature", return_value=70,
                           side_effect=RuntimeError("offline") if failed == "gpu" else None), \
                     patch("thermal_rgb.time.monotonic", return_value=20):
                    sample_sensors(readings, read_config(LEGACY_CONFIG)["sensors"])
                self.assertEqual(readings[failed][1], 1)
                healthy = "gpu" if failed == "cpu" else "cpu"
                self.assertEqual(readings[healthy][1], 20)

    def test_missing_stale_and_recovered_sensors(self):
        readings = {"cpu": (None, 0), "gpu": (50, 19)}
        self.assertEqual(temperature_targets(readings, 20, 10, read_config(LEGACY_CONFIG)["outputs"]), [(90, True), (50, False)])
        readings["cpu"] = (40, 1)
        self.assertEqual(temperature_targets(readings, 20, 10, read_config(LEGACY_CONFIG)["outputs"]), [(90, True), (50, False)])
        readings["cpu"] = (45, 20)
        self.assertEqual(temperature_targets(readings, 20, 10, read_config(LEGACY_CONFIG)["outputs"]), [(45, False), (50, False)])

    def test_bad_config(self):
        config = LEGACY_CONFIG.read_text()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "broken.toml"
            path.write_text(config.replace("cpu_max_brightness = 0.40", "cpu_max_brightness = 2.0"))
            with self.assertRaises(ValueError):
                read_config(path)

    def test_hysteresis_and_duplicate_colors(self):
        device = Mock()
        output = Output(device, 25, 90, 0.4, 2)
        factory = lambda *rgb: rgb
        for temp, expected in ((40, True), (41, False), (41.9, False),
                               (42, True), (42, False), (44, True)):
            self.assertEqual(output.apply(temp, False, factory), expected)
        self.assertEqual(device.set_color.call_count, 3)
        output.apply(100, False, factory)
        self.assertFalse(output.apply(110, False, factory))
        # Fallback bypasses hysteresis, even near the red threshold.
        output = Output(device, 25, 90, 0.4, 2)
        output.apply(89, False, factory)
        self.assertTrue(output.apply(90, True, factory))
        self.assertTrue(output.apply(89, False, factory))

    def test_defaults_legacy_and_validation(self):
        original = LEGACY_CONFIG.read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(original.replace("sensor_interval = 60.0", "fan_interval = 0.04")
                            .replace("temperature_hysteresis = 2.0", "smooth_seconds = 3.0")
                            .replace("breathing = false", "breathing = true"))
            config = read_config(path)
            self.assertEqual(config["animation"]["sensor_interval"], 60)
            self.assertEqual(config["animation"]["temperature_hysteresis"], 2)
            for key in ("sensor_interval", "report_interval", "temperature_hysteresis"):
                path.write_text(original.replace(key + " = " + ("2.0" if key == "temperature_hysteresis" else "60.0"), key + " = -1"))
                with self.assertRaises(ValueError):
                    read_config(path)

    def test_cadence_stability_and_shutdown(self):
        config = read_config(LEGACY_CONFIG)
        config["animation"]["diagnostics"] = True
        clock = [0.0]
        class Stop:
            def is_set(self):
                return clock[0] >= 240
            def wait(self, seconds):
                self_waits.append(seconds)
                clock[0] += seconds
        self_waits = []
        client, fans, gpu = Mock(), Mock(), Mock()
        with patch("thermal_rgb.time.monotonic", side_effect=lambda: clock[0]), \
             patch("thermal_rgb.cpu_temperature", return_value=(40, "test")) as cpu, \
             patch("thermal_rgb.gpu_temperature", return_value=50) as sensor_gpu, \
             patch("thermal_rgb.connection_closed", return_value=False), \
             patch("thermal_rgb.setup_rgb", return_value=(client, [fans, gpu])):
            run(config, Stop(), lambda *rgb: rgb)
        self.assertEqual(cpu.call_count, 4)
        self.assertEqual(sensor_gpu.call_count, 4)
        self.assertEqual(self_waits, [60] * 4)
        fans.set_color.assert_called_once()
        gpu.set_color.assert_called_once()
        client.disconnect.assert_called_once()

    def test_disconnect_retries_on_next_sample(self):
        config = read_config(LEGACY_CONFIG)
        clock = [0.0]
        stop = threading.Event()
        first, second, fans, gpu = Mock(), Mock(), Mock(), Mock()
        fans.set_color.side_effect = [OSError("disconnected"), None]
        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= 120:
                stop.set()
        with patch("thermal_rgb.time.monotonic", side_effect=lambda: clock[0]), \
             patch("thermal_rgb.sample_sensors"), \
             patch("thermal_rgb.connection_closed", return_value=True), \
             patch("thermal_rgb.setup_rgb", side_effect=[(first, [fans, gpu]), (second, [fans, gpu])]) as setup, \
             patch.object(stop, "wait", side_effect=wait):
            run(config, stop, lambda *rgb: rgb)
        self.assertEqual(setup.call_count, 2)
        self.assertEqual(fans.set_color.call_count, 2)
        gpu.set_color.assert_called_once()
        first.disconnect.assert_called_once()
        second.disconnect.assert_called_once()

    def test_passive_disconnect_check(self):
        local, peer = socket.socketpair()
        client = Mock()
        client.comms.sock = local
        try:
            self.assertFalse(connection_closed(client))
            peer.close()
            self.assertTrue(connection_closed(client))
        finally:
            local.close()
            peer.close()
        client.comms.sock = None
        self.assertTrue(connection_closed(client))

    def test_stable_temperatures_reconnect_and_reports_keep_cadence(self):
        config = read_config(LEGACY_CONFIG)
        clock = [0.0]
        stop = threading.Event()
        first, second, fans, gpu = Mock(), Mock(), Mock(), Mock()
        def sample(*args):
            # First setup/sample takes longer; subsequent samples finish earlier.
            clock[0] += 1 if clock[0] < 1 else 0.1
        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= 180:
                stop.set()
        with patch("thermal_rgb.time.monotonic", side_effect=lambda: clock[0]), \
             patch("thermal_rgb.sample_sensors", side_effect=sample), \
             patch("thermal_rgb.connection_closed", side_effect=[True, False]), \
             patch("thermal_rgb.setup_rgb", side_effect=[(first, [fans, gpu]), (second, [fans, gpu])]) as setup, \
             patch.object(stop, "wait", side_effect=wait), patch("builtins.print") as log:
            run(config, stop, lambda *rgb: rgb)
        self.assertEqual(setup.call_count, 2)
        self.assertEqual(fans.set_color.call_count, 2)
        self.assertEqual(gpu.set_color.call_count, 2)
        self.assertEqual(log.call_count, 3)
        first.disconnect.assert_called_once()
        second.disconnect.assert_called_once()

    def test_shutdown_during_sample_does_not_write(self):
        config = read_config(LEGACY_CONFIG)
        stop = threading.Event()
        with patch("thermal_rgb.sample_sensors", side_effect=lambda *_: stop.set()), \
             patch("thermal_rgb.setup_rgb") as setup:
            run(config, stop, lambda *rgb: rgb)
        setup.assert_not_called()

    def test_numeric_types(self):
        original = LEGACY_CONFIG.read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            for old, new in (("sensor_interval = 60.0", "sensor_interval = true"),
                             ("port = 6742", "port = 6742.5"),
                             ("argb_led_count = 8", "argb_led_count = 8.5"),
                             ("nvidia_gpu_index = 0", "nvidia_gpu_index = -1"),
                             ("temperature_hysteresis = 2.0", "temperature_hysteresis = nan")):
                with self.subTest(new=new):
                    path.write_text(original.replace(old, new))
                    with self.assertRaises(ValueError):
                        read_config(path)

    def test_installer_preserves_config_and_prepares_before_stop(self):
        # Run a copy with only its home path redirected; every service/hardware
        # command is a stub and the Python wrapper never installs dependencies.
        for failure in (None, "pip", "config", "new", "generic", "stop", "absent"):
            fail_pip = failure == "pip"
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                repo, commands = base / "repo", base / "bin"
                repo.mkdir()
                commands.mkdir()
                (repo / "systemd").mkdir()
                for name in ("thermal_rgb.py", "config.example.toml", "requirements.txt",
                             "systemd/thermal-rgb.service", "systemd/openrgb-local.service.in"):
                    (repo / name).write_bytes((ROOT / name).read_bytes())
                installer = (ROOT / "install.sh").read_text().replace("${HOME}", "${TEST_INSTALL_HOME}")
                (repo / "install.sh").write_text(installer)
                config_dir = base / "config/thermal-rgb"
                config_dir.mkdir(parents=True)
                original = LEGACY_CONFIG.read_text() + "\n# keep my settings\n"
                if failure == "config":
                    original = original.replace("sensor_interval = 60.0", "sensor_interval = -1")
                if failure == "generic":
                    original = (ROOT / "config.example.toml").read_text()
                if failure != "new":
                    (config_dir / "config.toml").write_text(original)
                if failure == "generic":
                    original = original.replace("port = 6742", "port = 6743")
                    (config_dir / "config.toml").write_text(original)
                app_script = base / "home/.local/share/thermal-rgb/thermal_rgb.py"
                app_script.parent.mkdir(parents=True)
                app_script.write_text("# previous installed version\n")
                log = base / "calls"
                def stub(name, body):
                    path = commands / name
                    path.write_text("#!/bin/bash\n" + body + "\n")
                    path.chmod(0o755)
                stub("id", 'if [[ "$1" == "-u" ]]; then echo 1000; else echo tester; fi')
                stub("pgrep", "exit 1")
                stub("openrgb", "exit 0")
                stub("nvidia-smi", "exit 1")
                stub("systemctl", 'echo "systemctl $*" >> "$TEST_CALLS"\n'
                     'if [[ "$*" == *LoadState* ]]; then\n'
                     'if [[ "$TEST_INSTALL_CASE" == absent ]]; then echo not-found; exit 1; else echo loaded; fi\nfi\n'
                     'if [[ "$*" == *"--user stop"* && "$TEST_INSTALL_CASE" == stop ]]; then exit 1; fi\n'
                     'if [[ "$*" == *is-active* || "$*" == *is-enabled* ]]; then exit 1; fi')
                stub("python3", 'if [[ "$1 $2" == "-m venv" ]]; then\n'
                     'mkdir -p "$3/bin"\ncp "$TEST_PY_WRAPPER" "$3/bin/python"\n'
                     'else exec "$TEST_REAL_PY" "$@"; fi')
                stub("python-wrapper", 'if [[ "$1 $2" == "-m pip" ]]; then\n'
                     'echo pip >> "$TEST_CALLS"\nexit "$TEST_FAIL_PIP"\nfi\n'
                     'echo check >> "$TEST_CALLS"\nexec "$TEST_REAL_PY" "$@"')
                env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                           TEST_INSTALL_HOME=str(base / "home"), XDG_CONFIG_HOME=str(base / "config"),
                           TEST_CALLS=str(log), TEST_REAL_PY=sys.executable,
                           TEST_PY_WRAPPER=str(commands / "python-wrapper"), TEST_FAIL_PIP=str(int(fail_pip)), TEST_INSTALL_CASE=str(failure))
                result = subprocess.run(["bash", str(repo / "install.sh")], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, int(failure in ("pip", "config", "stop")), result.stdout + result.stderr)
                if failure == "new":
                    self.assertEqual((config_dir / "config.toml").read_text(),
                                     (ROOT / "config.example.toml").read_text())
                    self.assertEqual(log.read_text(), "pip\n")
                    continue
                self.assertEqual((config_dir / "config.toml").read_text(), original)
                calls = log.read_text()
                if failure in ("pip", "config"):
                    self.assertNotIn("systemctl", calls)
                elif failure == "stop":
                    self.assertEqual(app_script.read_text(), "# previous installed version\n")
                    self.assertNotIn("enable --now", calls)
                    self.assertFalse((base / "config/systemd/user/thermal-rgb.service").exists())
                else:
                    if failure == "absent":
                        self.assertNotIn("systemctl --user stop", calls)
                    else:
                        self.assertLess(calls.index("check"), calls.index("systemctl --user stop"))
                    for name in ("thermal-rgb.service", "openrgb-local.service"):
                        unit = (base / "config/systemd/user" / name).read_text()
                        for setting in ("Nice=19", "IOSchedulingClass=idle", "CPUWeight=1", "IOWeight=1"):
                            self.assertIn(setting, unit)
                        self.assertNotIn("@OPENRGB_BINARY@", unit)
                        self.assertNotIn("@OPENRGB_PORT@", unit)
                        if name == "openrgb-local.service":
                            self.assertIn("--server-port " + ("6743" if failure == "generic" else "6742"), unit)
                    # Re-running an upgrade retains config and produces the same installed files.
                    before = {name: (base / "config/systemd/user" / name).read_bytes()
                              for name in ("thermal-rgb.service", "openrgb-local.service")}
                    again = subprocess.run(["bash", str(repo / "install.sh")], env=env,
                                           capture_output=True, text=True)
                    self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                    self.assertEqual((config_dir / "config.toml").read_text(), original)
                    self.assertEqual(app_script.read_bytes(), (ROOT / "thermal_rgb.py").read_bytes())
                    for name, content in before.items():
                        self.assertEqual((base / "config/systemd/user" / name).read_bytes(), content)

    def test_generic_template_and_legacy_normalization(self):
        generic = read_config(ROOT / "config.example.toml")
        self.assertEqual(len(generic["outputs"]), 1)
        self.assertEqual(generic["sensors"], {"cpu": {"type": "auto_cpu"}})
        legacy = read_config(LEGACY_CONFIG)
        self.assertEqual(legacy["outputs"][0]["device"], "ASUS PRIME X570-P")
        self.assertEqual(legacy["outputs"][0]["led_count"], 8)
        self.assertEqual(legacy["outputs"][1]["brightness"], 1)

    def test_shared_psutil_snapshot_and_nvidia_query(self):
        sensors = {"cpu": {"type": "auto_cpu"},
                   "gpu": {"type": "psutil", "driver": "amdgpu", "label": "edge", "index": 0},
                   "other": {"type": "psutil", "driver": "custom", "index": 1},
                   "nv1": {"type": "nvidia", "index": 0},
                   "nv2": {"type": "nvidia", "index": 0}}
        readings = {name: (None, 0) for name in sensors}
        snapshot = {"k10temp": [SimpleNamespace(label="Tdie", current=40)],
                    "amdgpu": [SimpleNamespace(label="edge", current=50)],
                    "custom": [SimpleNamespace(current=20), SimpleNamespace(current=30)]}
        with patch("thermal_rgb.psutil.sensors_temperatures", return_value=snapshot) as ps, \
             patch("thermal_rgb.gpu_temperature", return_value=60) as nv:
            sample_sensors(readings, sensors)
        ps.assert_called_once()
        nv.assert_called_once_with(0)
        self.assertEqual([reading[0] for reading in readings.values()], [40, 50, 30, 60, 60])

    def test_cpu_only_ignores_unused_nvidia(self):
        config = read_config(ROOT / "config.example.toml")
        config["sensors"]["unused"] = {"type": "nvidia", "index": 0}
        stop = threading.Event()
        device = Mock()
        with patch("thermal_rgb.psutil.sensors_temperatures", return_value={}) as ps, \
             patch("thermal_rgb.gpu_temperature") as nv, \
             patch("thermal_rgb.setup_rgb", return_value=(Mock(), [device])), \
             patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            run(config, stop, lambda *rgb: rgb)
        ps.assert_called_once()
        nv.assert_not_called()
        device.set_color.assert_called_once()

    def test_gpu_only_does_not_read_psutil(self):
        sensors = {"gpu": {"type": "nvidia", "index": 1}}
        readings = {"gpu": (None, 0)}
        with patch("thermal_rgb.psutil.sensors_temperatures") as ps, \
             patch("thermal_rgb.gpu_temperature", return_value=55) as nv:
            sample_sensors(readings, sensors)
        ps.assert_not_called()
        nv.assert_called_once_with(1)
        self.assertEqual(readings["gpu"][0], 55)

    def test_generic_config_validation(self):
        template = (ROOT / "config.example.toml").read_text()
        for old, new in (('sensor = "cpu"', 'sensor = "missing"'),
                         ('type = "auto_cpu"', 'type = "unknown"'),
                         ("red_temp = 90.0", "red_temp = 20.0"),
                         ("brightness = 0.40", "brightness = 2.0"),
                         ('device = "YOUR OPENRGB DEVICE NAME"', 'device = ""'),
                         ('# zone = "YOUR ZONE NAME"', 'zone = ""'),
                         ("# led_count = 8", "led_count = 8")):
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.toml"
                path.write_text(template.replace(old, new))
                with self.assertRaises(ValueError):
                    read_config(path)

    def test_generic_device_and_zone_selection(self):
        config = read_config(ROOT / "config.example.toml")
        device = Mock(name="device")
        device.name = config["outputs"][0]["device"]
        device.active_mode = 0
        device.modes = [SimpleNamespace(name="Direct")]
        zone = Mock()
        zone.name, zone.leds = "LED strip", [object()]
        device.zones = [zone]
        client = Mock()
        client.devices = [device]
        module = SimpleNamespace(OpenRGBClient=Mock(return_value=client))
        with patch.dict(sys.modules, {"openrgb": module}):
            _, targets = setup_rgb(config)
            self.assertEqual(targets, [device])
            config["outputs"][0]["zone"] = "strip"
            _, targets = setup_rgb(config)
            self.assertEqual(targets, [zone])
            zone.leds = []
            with self.assertRaisesRegex(RuntimeError, "led_count explicitly"):
                setup_rgb(config)
            zone.resize.assert_not_called()
            config["outputs"][0]["led_count"] = 12
            zone.resize.side_effect = lambda _: setattr(zone, "leds", [object()] * 12)
            _, targets = setup_rgb(config)
            zone.resize.assert_called_once_with(12)
            device.set_mode.assert_not_called()

    def test_ambiguous_or_overlapping_outputs_fail_before_writes(self):
        config = read_config(ROOT / "config.example.toml")
        config["outputs"][0]["device"] = "Board"
        first, second = Mock(), Mock()
        first.name, second.name = "Board one", "Board two"
        client = Mock()
        client.devices = [first, second]
        module = SimpleNamespace(OpenRGBClient=Mock(return_value=client))
        with patch.dict(sys.modules, {"openrgb": module}):
            with self.assertRaisesRegex(RuntimeError, "matched 2"):
                setup_rgb(config)
            client.devices = [first]
            config["outputs"].append(dict(config["outputs"][0], name="duplicate"))
            with self.assertRaisesRegex(RuntimeError, "Overlapping"):
                setup_rgb(config)
        first.set_mode.assert_not_called()
        first.set_color.assert_not_called()

    def test_shared_source_stale_fallback_and_recovery(self):
        config = read_config(ROOT / "config.example.toml")
        config["animation"]["diagnostics"] = True
        config["outputs"].append(dict(config["outputs"][0], name="second-device"))
        clock = [0.0]
        stop, report = threading.Event(), threading.Event()
        devices = [Mock(), Mock()]
        good = {"k10temp": [SimpleNamespace(label="Tdie", current=40)]}
        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= 300:
                report.set()
            if clock[0] >= 360:
                stop.set()
        with patch("thermal_rgb.time.monotonic", side_effect=lambda: clock[0]), \
             patch("thermal_rgb.psutil.sensors_temperatures",
                   side_effect=[good] + [OSError("offline")] * 4 + [good]) as ps, \
             patch("thermal_rgb.gpu_temperature") as nv, \
             patch("thermal_rgb.connection_closed", return_value=False), \
             patch("thermal_rgb.setup_rgb", return_value=(Mock(), devices)), \
             patch.object(stop, "wait", side_effect=wait), patch("builtins.print") as log:
            run(config, stop, lambda *rgb: rgb, report)
        self.assertEqual(ps.call_count, 6)
        nv.assert_not_called()
        for device in devices:
            self.assertEqual(device.set_color.call_count, 3)
            colors = [call.args[0] for call in device.set_color.call_args_list]
            self.assertEqual(colors[0], colors[2])
            self.assertNotEqual(colors[0], colors[1])
        summary = [call.args[0] for call in log.call_args_list if "Diagnostics" in call.args[0]][0]
        self.assertIn("'readings': {'cpu': 6}", summary)
        self.assertIn("'sensor_failures': 4", summary)
        self.assertIn("'cpu-lighting': 3", summary)

    def test_failed_output_keeps_healthy_outputs_cached(self):
        config = read_config(ROOT / "config.example.toml")
        config["outputs"].append(dict(config["outputs"][0], name="second"))
        config["outputs"].append(dict(config["outputs"][0], name="third"))
        clock = [0.0]
        stop = threading.Event()
        client, first, second, third = Mock(), Mock(), Mock(), Mock()
        second.set_color.side_effect = [OSError("output failed"), OSError("output failed"), None]
        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= 180:
                stop.set()
        with patch("thermal_rgb.time.monotonic", side_effect=lambda: clock[0]), \
             patch("thermal_rgb.sample_sensors"), \
             patch("thermal_rgb.connection_closed", return_value=False), \
             patch("thermal_rgb.setup_rgb", return_value=(client, [first, second, third])) as setup, \
             patch.object(stop, "wait", side_effect=wait):
            run(config, stop, lambda *rgb: rgb)
        setup.assert_called_once()
        first.set_color.assert_called_once()
        third.set_color.assert_called_once()
        self.assertEqual(second.set_color.call_count, 3)
        client.disconnect.assert_called_once()

    def test_service_priorities_and_installer(self):
        for name in ("thermal-rgb.service", "openrgb-local.service.in"):
            text = (ROOT / "systemd" / name).read_text()
            for setting in ("Nice=19", "IOSchedulingClass=idle", "CPUWeight=1", "IOWeight=1"):
                self.assertIn(setting, text)
            self.assertNotIn("CPUQuota", text)
            self.assertNotIn("MemoryMax", text)
        unit = (ROOT / "systemd/thermal-rgb.service").read_text()
        self.assertIn("Wants=openrgb-local.service", unit)
        self.assertNotIn("Requires=", unit)
        installer = (ROOT / "install.sh").read_text()
        self.assertIn('if [[ ! -e "$CONFIG_DIR/config.toml" ]]', installer)
        self.assertIn('"$ROOT/systemd/openrgb-local.service.in"', installer)
        self.assertIn('"$ROOT/systemd/thermal-rgb.service"', installer)


if __name__ == "__main__":
    unittest.main()
