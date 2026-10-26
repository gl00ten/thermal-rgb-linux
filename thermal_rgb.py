#!/usr/bin/env python3
"""CPU/GPU-temperature RGB lighting through the local OpenRGB SDK."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
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
    t = config["temperatures"]
    a = config["animation"]
    d = config["devices"]
    net = config["openrgb"]
    def within(name, value, minimum, maximum):
        if not (minimum <= value <= maximum):
            raise ValueError(f"{name} must be between {minimum} and {maximum}; got {value}")
    within("room_temp", t["room_temp"], -10, 45)
    within("cpu_red", t["cpu_red"], 50, 110)
    within("gpu_red", t["gpu_red"], 50, 110)
    if not t["room_temp"] < min(t["cpu_red"], t["gpu_red"]):
        raise ValueError("Red thresholds must exceed room_temp")
    for key in ("cpu_max_brightness", "gpu_max_brightness", "breath_min"):
        within(key, a[key], 0.01, 1.0)
    for key in ("breath_period", "sensor_interval", "smooth_seconds", "fan_interval", "gpu_interval", "report_interval"):
        within(key, a[key], 0.01, 300)
    within("argb_led_count", d["argb_led_count"], 1, 120)
    within("port", net["port"], 1024, 65535)
    if net["host"] not in ("127.0.0.1", "localhost"):
        raise ValueError("The default service is localhost-only; set host=127.0.0.1")
    return config


def cpu_temperature():
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


def breath(elapsed, a):
    if not a["breathing"]:
        return 1.0
    wave = (1 - math.cos(2 * math.pi * elapsed / a["breath_period"])) / 2
    return a["breath_min"] + (1 - a["breath_min"]) * wave


def sample_sensors(readings, lock, gpu_index):
    for name, read in (("cpu", lambda: cpu_temperature()[0]),
                       ("gpu", lambda: gpu_temperature(gpu_index))):
        try:
            value = read()
            if not math.isfinite(value):
                raise ValueError("non-finite temperature")
            with lock:
                readings[name] = (value, time.monotonic())
        except Exception as exc:
            print(f"{name.upper()} sensor warning: {exc}", flush=True)


def sensor_worker(readings, lock, stop, animation, gpu_index):
    while not stop.is_set():
        sample_sensors(readings, lock, gpu_index)
        stop.wait(animation["sensor_interval"])


def temperature_targets(readings, now, max_age, thresholds):
    # Missing/stale sensors show red instead of a misleading old temperature.
    return tuple(value if value is not None and now - sampled <= max_age else red
                 for (value, sampled), red in zip(
                     (readings["cpu"], readings["gpu"]), thresholds))


def setup_rgb(config):
    from openrgb import OpenRGBClient

    net, dev = config["openrgb"], config["devices"]
    client = OpenRGBClient(net["host"], net["port"], "thermal-rgb-linux")
    mobo = next((x for x in client.devices if dev["motherboard_name"].lower() in x.name.lower()), None)
    gpu = next((x for x in client.devices if dev["gpu_name"].lower() in x.name.lower()), None)
    if mobo is None or gpu is None:
        names = ", ".join(x.name for x in client.devices)
        raise RuntimeError(f"Required RGB device not found (detected: {names or 'none'}). Check OpenRGB udev access and config.toml")
    for device in (mobo, gpu):
        current_mode = device.modes[device.active_mode].name.lower()
        if current_mode != "direct":
            device.set_mode("direct")
            time.sleep(0.5)
    fans = next((z for z in mobo.zones if dev["argb_zone"].lower() in z.name.lower()), None)
    if fans is None:
        raise RuntimeError(f"ARGB zone '{dev['argb_zone']}' not found on {mobo.name}")
    if len(fans.leds) == 0:
        count = dev["argb_led_count"]
        print(f"ARGB zone has zero LEDs; configuring {count} (previously tested on this machine)", flush=True)
        fans.resize(count)
        time.sleep(0.5)
        # Re-fetch device/zone state after resize; SDK objects may become stale.
        mobo.update()
        fans = next(z for z in mobo.zones if dev["argb_zone"].lower() in z.name.lower())
    if not fans.leds:
        raise RuntimeError("ARGB zone still has zero LEDs. Set zone size manually in OpenRGB GUI")
    time.sleep(0.5)
    print(f"ARGB: {mobo.name} / {fans.name} ({len(fans.leds)} configured LEDs)")
    print(f"GPU: {gpu.name}")
    return client, fans, gpu


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--check-config", action="store_true", help="Validate config and exit without contacting hardware")
    args = parser.parse_args()
    config = read_config(args.config)
    if args.check_config:
        print(f"Configuration OK: {args.config}")
        return
    from openrgb.utils import RGBColor

    t, a, dev = config["temperatures"], config["animation"], config["devices"]
    stop = threading.Event()
    def request_stop(signum, _frame):
        print(f"Signal {signum}: stopping lighting", flush=True)
        stop.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    readings = {"cpu": (None, 0), "gpu": (None, 0)}
    lock = threading.Lock()
    client, fans, gpu = setup_rgb(config)
    worker = threading.Thread(
        target=sensor_worker,
        args=(readings, lock, stop, a, dev["nvidia_gpu_index"]),
        daemon=True,
    )
    worker.start()
    print("Running temperature RGB; missing/stale sensors show red. Ctrl+C to stop.", flush=True)
    cpu_smooth = t["cpu_red"]
    gpu_smooth = t["gpu_red"]
    max_age = max(10.0, 3 * a["sensor_interval"])
    started = last_step = time.monotonic()
    next_report = started
    outputs = [
        {"device": fans, "interval": a["fan_interval"], "brightness": a["cpu_max_brightness"],
         "red": t["cpu_red"], "next": started, "last": None},
        {"device": gpu, "interval": a["gpu_interval"], "brightness": a["gpu_max_brightness"],
         "red": t["gpu_red"], "next": started, "last": None},
    ]
    try:
        while not stop.is_set():
            now = time.monotonic()
            delta = max(0.0, now - last_step)
            last_step = now
            with lock:
                target_cpu, target_gpu = temperature_targets(
                    readings, now, max_age, (t["cpu_red"], t["gpu_red"]))
            alpha = 1 - math.exp(-delta / a["smooth_seconds"])
            cpu_smooth += alpha * (target_cpu - cpu_smooth)
            gpu_smooth += alpha * (target_gpu - gpu_smooth)
            factor = breath(now - started, a)
            for output, temperature in zip(outputs, (cpu_smooth, gpu_smooth)):
                if now < output["next"]:
                    continue
                color = rgb_for_temp(temperature, t["room_temp"], output["red"],
                                     factor * output["brightness"])
                if color != output["last"]:
                    output["device"].set_color(RGBColor(*color), fast=True)
                    output["last"] = color
                output["next"] = now + output["interval"]
            if now >= next_report:
                print(f"CPU {target_cpu:5.1f}°C  | GPU {target_gpu:5.1f}°C  | pulse {factor:0.2f}", flush=True)
                next_report = now + a["report_interval"]
            deadline = min(next_report, *(output["next"] for output in outputs))
            stop.wait(max(0.001, deadline - time.monotonic()))
    finally:
        stop.set()
        worker.join(timeout=1)
        # Leave a temperature-appropriate static colour on intentional shutdown.
        try:
            fans.set_color(RGBColor(*rgb_for_temp(cpu_smooth, t["room_temp"], t["cpu_red"], a["cpu_max_brightness"])), fast=True)
            gpu.set_color(RGBColor(*rgb_for_temp(gpu_smooth, t["room_temp"], t["gpu_red"], a["gpu_max_brightness"])), fast=True)
        except Exception as exc:
            print(f"Could not restore static colour: {exc}", flush=True)
        client.disconnect()


if __name__ == "__main__":
    main()
