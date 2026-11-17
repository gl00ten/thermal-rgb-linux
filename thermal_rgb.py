#!/usr/bin/env python3
"""Temperature-controlled RGB lighting through the local OpenRGB SDK."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import select
import subprocess
import threading
import time
import tomllib

import psutil

CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "thermal-rgb" / "config.toml"
PALETTE = [
    (0.00, (0, 64, 255)),
    (0.23, (0, 191, 255)),
    (0.46, (0, 232, 117)),
    (0.69, (255, 224, 0)),
    (0.85, (255, 136, 0)),
    (1.00, (255, 0, 0)),
]


def read_config(path: Path) -> dict:
    with path.open("rb") as f:
        config = tomllib.load(f)
    a = config.setdefault("animation", {})
    for key, value in {"sensor_interval": 60.0, "report_interval": 60.0,
                       "breathing": False, "temperature_hysteresis": 2.0,
                       "diagnostics": False}.items():
        a.setdefault(key, value)

    # Normalize the original machine-specific format once, at the boundary.
    if "outputs" not in config and "devices" in config:
        d, t = config["devices"], config["temperatures"]
        config["sensors"] = {"cpu": {"type": "auto_cpu"},
                             "gpu": {"type": "nvidia", "index": d["nvidia_gpu_index"]}}
        config["outputs"] = [
            {"name": "cpu", "sensor": "cpu", "device": d["motherboard_name"],
             "zone": d["argb_zone"], "led_count": d["argb_led_count"],
             "blue_temp": t["room_temp"], "red_temp": t["cpu_red"],
             "brightness": a["cpu_max_brightness"]},
            {"name": "gpu", "sensor": "gpu", "device": d["gpu_name"],
             "blue_temp": t["room_temp"], "red_temp": t["gpu_red"],
             "brightness": a["gpu_max_brightness"]},
        ]

    def within(name, value, minimum, maximum):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not minimum <= value <= maximum):
            raise ValueError(f"{name} must be between {minimum} and {maximum}; got {value}")

    def integer(name, value, minimum, maximum):
        if type(value) is not int:
            raise ValueError(f"{name} must be an integer")
        within(name, value, minimum, maximum)

    def text(name, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")

    for key in ("sensor_interval", "report_interval"):
        within(key, a[key], 0.01, 86400)
    within("temperature_hysteresis", a["temperature_hysteresis"], 0, 100)
    for key in ("breathing", "diagnostics"):
        if not isinstance(a[key], bool):
            raise ValueError(f"{key} must be a boolean")
    sensors, outputs = config.get("sensors", {}), config.get("outputs", [])
    if not isinstance(sensors, dict) or not sensors:
        raise ValueError("Define at least one [sensors.NAME] table")
    for name, sensor in sensors.items():
        if not isinstance(sensor, dict):
            raise ValueError("Each sensor must be a table")
        text("sensor name", name)
        if sensor.get("type") not in ("auto_cpu", "psutil", "nvidia"):
            raise ValueError(f"Unknown sensor type for {name}")
        if sensor["type"] == "psutil":
            text("driver", sensor.get("driver"))
            if not isinstance(sensor.get("label", ""), str):
                raise ValueError("Sensor label must be a string")
            integer("sensor index", sensor.setdefault("index", 0), 0, 65535)
        if sensor["type"] == "nvidia":
            integer("NVIDIA index", sensor.setdefault("index", 0), 0, 65535)
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("Define at least one [[outputs]] table")
    names = set()
    for output in outputs:
        if not isinstance(output, dict):
            raise ValueError("Each output must be a table")
        for key in ("name", "sensor", "device"):
            text(key, output.get(key))
        if output["name"] in names:
            raise ValueError("Output names must be unique")
        names.add(output["name"])
        if output["sensor"] not in sensors:
            raise ValueError(f"Unknown sensor: {output['sensor']}")
        if "zone" in output:
            text("zone", output["zone"])
        if "led_count" in output:
            if "zone" not in output:
                raise ValueError("led_count requires a zone")
            integer("led_count", output["led_count"], 1, 65535)
        within("brightness", output.setdefault("brightness", 1.0), 0.0, 1.0)
        within("blue_temp", output.setdefault("blue_temp", 25.0), -273, 1000)
        within("red_temp", output.setdefault("red_temp", 90.0), -273, 1000)
        if output["blue_temp"] >= output["red_temp"]:
            raise ValueError("red_temp must exceed blue_temp")
    net = config.setdefault("openrgb", {})
    net.setdefault("host", "127.0.0.1")
    integer("port", net.setdefault("port", 6742), 1024, 65535)
    if net["host"] not in ("127.0.0.1", "localhost"):
        raise ValueError("The managed server is localhost-only")
    return config


def cpu_temperature(readings=None):
    if readings is None:
        readings = psutil.sensors_temperatures()
    for driver, labels in (
        ("k10temp", ("Tdie", "Tctl", "")),
        ("zenpower", ("Tdie", "Tctl", "")),
        ("coretemp", ("Package id 0", "Physical id 0", "")),
        ("cpu_thermal", ("",)),
    ):
        for label in labels:
            for item in readings.get(driver, []):
                if item.label == label and item.current is not None:
                    return float(item.current), f"{driver}/{label or 'default'}"
    raise RuntimeError(f"No compatible CPU sensor found: {list(readings)}")


def gpu_temperature(gpu_index: int):
    result = subprocess.run(
        ["nvidia-smi", f"--id={gpu_index}", "--query-gpu=temperature.gpu",
         "--format=csv,noheader,nounits"],
        text=True, capture_output=True, check=True, timeout=5,
    )
    return float(result.stdout.strip().splitlines()[0])


def rgb_for_temp(temp, room, red, intensity):
    f = max(0.0, min(1.0, (temp - room) / (red - room)))
    for (a, low), (b, high) in zip(PALETTE, PALETTE[1:]):
        if f <= b:
            q = (f - a) / (b - a)
            return tuple(round((x + (y - x) * q) * intensity) for x, y in zip(low, high))
    return tuple(round(channel * intensity) for channel in PALETTE[-1][1])


def sample_sensors(readings, sensors, counters=None):
    # One psutil call per cycle, even with multiple configured sensor sources.
    snapshot = None
    snapshot_error = None
    if any(sensor["type"] != "nvidia" for sensor in sensors.values()):
        try:
            snapshot = psutil.sensors_temperatures()
        except Exception as exc:
            snapshot_error = exc
    nvidia_values = {}
    for name, sensor in sensors.items():
        if counters is not None:
            counters["readings"][name] += 1
        try:
            kind = sensor["type"]
            if kind == "nvidia":
                index = sensor["index"]
                if index not in nvidia_values:
                    try:
                        nvidia_values[index] = gpu_temperature(index)
                    except Exception as exc:
                        nvidia_values[index] = exc
                value = nvidia_values[index]
                if isinstance(value, Exception):
                    raise value
            else:
                if snapshot_error is not None:
                    raise snapshot_error
                if kind == "auto_cpu":
                    value = cpu_temperature(snapshot)[0]
                else:
                    entries = snapshot.get(sensor["driver"], [])
                    if "label" in sensor:
                        entries = [entry for entry in entries if entry.label == sensor["label"]]
                    value = entries[sensor["index"]].current
            if value is None or not math.isfinite(value):
                raise ValueError("missing/non-finite temperature")
            readings[name] = (float(value), time.monotonic())
        except Exception as exc:
            if counters is not None:
                counters["sensor_failures"] += 1
            print(f"Sensor {name} warning: {exc}", flush=True)


def temperature_targets(readings, now, max_age, outputs):
    targets = []
    for output in outputs:
        value, sampled = readings[output["sensor"]]
        fallback = value is None or now - sampled > max_age
        targets.append((output["red_temp"] if fallback else value, fallback))
    return targets


def connection_closed(client):
    """Inspect local TCP state without sending SDK requests or touching hardware."""
    sock = client.comms.sock
    if sock is None:
        return True
    poll = select.poll()
    poll.register(sock, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
    return bool(poll.poll(0))


def match(items, name):
    exact = [item for item in items if item.name.casefold() == name.casefold()]
    matches = exact or [item for item in items if name.casefold() in item.name.casefold()]
    if len(matches) != 1:
        available = ", ".join(repr(item.name) for item in items) or "none"
        raise RuntimeError(f"{name!r} matched {len(matches)} targets. Available: {available}")
    return matches[0]


def resolve_outputs(available, config):
    devices, selected = [], []
    for output in config["outputs"]:
        device = match(available, output["device"])
        target = match(device.zones, output["zone"]) if "zone" in output else device
        # Whole-device and zone outputs on the same device would conflict.
        if any(previous is device and (zone is None or output.get("zone") is None
                                      or zone is target)
               for previous, zone in selected):
            raise RuntimeError("Overlapping RGB outputs on the same device")
        selected.append((device, target if "zone" in output else None))
        devices.append((device, target, output))

    return devices


def setup_rgb(config):
    from openrgb import OpenRGBClient

    net = config["openrgb"]
    client = OpenRGBClient(net["host"], net["port"], "thermal-rgb-linux")
    try:
        devices = resolve_outputs(client.devices, config)

        # Resolve every selector before changing hardware modes or zone sizes.
        initialized = set()
        targets = []
        for device, target, output in devices:
            if "zone" in output:
                target = match(device.zones, output["zone"])
            if id(device) not in initialized:
                if device.modes[device.active_mode].name.lower() != "direct":
                    device.set_mode("direct")
                initialized.add(id(device))
            if "zone" in output and not target.leds:
                if "led_count" not in output:
                    raise RuntimeError(f"Empty zone for {output['name']}; configure led_count explicitly")
                target.resize(output["led_count"])
                device.update()
                target = match(device.zones, output["zone"])
                if not target.leds:
                    raise RuntimeError(f"Zone for {output['name']} still has no LEDs")
            targets.append(target)
            print(f"Output {output['name']}: {device.name}" +
                  (f" / {target.name}" if "zone" in output else ""), flush=True)
        return client, targets
    except Exception:
        try:
            client.disconnect()
        except Exception:
            pass
        raise


class Output:
    """Hysteresis is measured from the last accepted temperature, not last sample."""

    def __init__(self, device, room, red, brightness, hysteresis):
        self.device = device
        self.room, self.red = room, red
        self.brightness, self.hysteresis = brightness, hysteresis
        self.temperature = self.color = self.fallback = None

    def apply(self, temperature, fallback, color_factory):
        if (self.temperature is not None and fallback == self.fallback
                and abs(temperature - self.temperature) < self.hysteresis):
            return False
        color = rgb_for_temp(temperature, self.room, self.red, self.brightness)
        if color != self.color:
            self.device.set_color(color_factory(*color), fast=True)
        changed = color != self.color
        self.temperature, self.color, self.fallback = temperature, color, fallback
        return changed


def run(config, stop, color_factory, report_requested=None):
    a, specifications = config["animation"], config["outputs"]
    # Ignore unused sensor definitions: no unnecessary subprocesses or reads.
    sensors = {name: config["sensors"][name] for name in config["sensors"]
               if any(output["sensor"] == name for output in specifications)}
    readings = {name: (None, 0) for name in sensors}
    counters = ({"readings": dict.fromkeys(sensors, 0),
                 "writes": {output["name"]: 0 for output in specifications},
                 "skipped_updates": 0, "sensor_failures": 0}
                if a["diagnostics"] else None)
    client = None
    outputs = []
    max_age = max(10.0, 3 * a["sensor_interval"])
    next_sample = time.monotonic()
    next_report = next_sample
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_sample:
                sample_sensors(readings, sensors, counters)
                if stop.is_set():
                    break
                now = time.monotonic()
                targets = temperature_targets(readings, now, max_age,
                                              specifications)
                try:
                    if client is not None and connection_closed(client):
                        client.disconnect()
                        client = None
                    if client is None:
                        client, devices = setup_rgb(config)
                        outputs = [Output(device, spec["blue_temp"], spec["red_temp"],
                                          spec["brightness"], a["temperature_hysteresis"])
                                   for spec, device in zip(specifications, devices)]
                    for spec, output, target in zip(specifications, outputs, targets):
                        if stop.is_set():
                            break
                        temperature, fallback = target
                        try:
                            changed = output.apply(temperature, fallback, color_factory)
                        except Exception as exc:
                            if connection_closed(client):
                                raise
                            # Keep successful outputs cached when only one output fails.
                            print(f"Output {spec['name']} warning (retry next sample): {exc}", flush=True)
                            continue
                        if counters is not None:
                            if changed:
                                counters["writes"][spec["name"]] += 1
                            else:
                                counters["skipped_updates"] += 1
                except Exception as exc:
                    print(f"OpenRGB warning (retry next sample): {exc}", flush=True)
                    if client is not None:
                        try:
                            client.disconnect()
                        except Exception:
                            pass
                    client = None
                # Fixed cadence; skip missed deadlines instead of catch-up polling.
                finished = time.monotonic()
                next_sample += (int((finished - next_sample) // a["sensor_interval"]) + 1) * a["sensor_interval"]
                if finished >= next_report:
                    print(" | ".join(f"{spec['name']}: {target[0]:.1f}°C"
                                     + (" (fallback)" if target[1] else "")
                                     for spec, target in zip(specifications, targets)), flush=True)
                    next_report += (int((finished - next_report) // a["report_interval"]) + 1) * a["report_interval"]
            if report_requested is not None and report_requested.is_set():
                report_requested.clear()
                print(f"Diagnostics (since startup; successful colour commands): {counters if counters is not None else 'disabled; set animation.diagnostics=true'}", flush=True)
            stop.wait(max(0, next_sample - time.monotonic()))
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        # Leave the last static colour; shutdown never writes to hardware.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--check-config", action="store_true", help="Validate config without hardware")
    parser.add_argument("--list-sensors", action="store_true", help="List psutil drivers/labels without OpenRGB")
    parser.add_argument("--list-devices", action="store_true", help="List devices/zones from the running SDK server")
    parser.add_argument("--check-devices", action="store_true", help="Check output names without changing lighting")
    args = parser.parse_args()
    if args.list_sensors:
        for driver, entries in psutil.sensors_temperatures().items():
            label_counts = {}
            for index, entry in enumerate(entries):
                label_index = label_counts.get(entry.label, 0)
                label_counts[entry.label] = label_index + 1
                print(f"driver={driver!r} driver_index={index} label={entry.label!r} "
                      f"label_index={label_index}: {entry.current}°C")
        return
    if args.list_devices or args.check_devices:
        from openrgb import OpenRGBClient

        client = None
        try:
            # Discovery works before the output placeholders have been configured.
            raw = tomllib.loads(args.config.read_text()) if args.config.exists() else {}
            net = raw.get("openrgb", {})
            client = OpenRGBClient(net.get("host", "127.0.0.1"), net.get("port", 6742), "thermal-rgb-inspect")
            if args.list_devices:
                if not client.devices:
                    print("No devices detected. Check OpenRGB device permissions.")
                for device in client.devices:
                    print(device.name)
                    for zone in device.zones:
                        print(f"  {zone.name} ({len(zone.leds)} LEDs)")
            else:
                for device, target, output in resolve_outputs(client.devices, read_config(args.config)):
                    print(f"OK: {output['name']} -> {device.name}" +
                          (f" / {target.name}" if "zone" in output else ""))
        except Exception as exc:
            parser.exit(1, f"Device check failed: {exc}\nCheck ./manage.sh status and ./manage.sh devices.\n")
        finally:
            if client is not None:
                client.disconnect()
        return
    config = read_config(args.config)
    if args.check_config:
        print(f"Configuration OK: {args.config}")
        return
    from openrgb.utils import RGBColor

    stop, report_requested = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGUSR1, lambda *_: report_requested.set())
    if config["animation"]["breathing"]:
        print("Legacy breathing/smoothing settings are ignored; lighting is static.", flush=True)
    run(config, stop, RGBColor, report_requested)


if __name__ == "__main__":
    main()
