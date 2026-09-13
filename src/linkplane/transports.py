from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


class BridgeError(RuntimeError):
    """An actionable transport or device error."""


def run_command(
    command: list[str],
    *,
    timeout: int = 10,
    input_text: str | None = None,
    discard_output: bool = False,
) -> str:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL if discard_output else subprocess.PIPE,
            stderr=subprocess.DEVNULL if discard_output else subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            input=input_text,
        )
    except FileNotFoundError as error:
        raise BridgeError(f"{command[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise BridgeError(f"{command[0]} timed out after {timeout} seconds") from error

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "command failed"
        raise BridgeError(f"{command[0]}: {detail}")
    return result.stdout or ""


# `adb devices -l` states Linkplane distinguishes. `no permissions` is the one multi-word
# state adb prints (`<serial> no permissions (missing udev rules? …); see [https://…]`);
# it means the Linux host may not open the USB device, which is a host-side problem and
# not the phone declining authorization.
ADB_STATE_DEVICE = "device"
ADB_STATE_UNAUTHORIZED = "unauthorized"
ADB_STATE_OFFLINE = "offline"
ADB_STATE_NO_PERMISSIONS = "no permissions"

_ADB_DETAIL = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<value>\S+)$")


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    """Parse `adb devices -l` (and `track-devices -l`) lines into `{serial, state, …}`.

    The state is the second column, except that adb spells one state as two words
    followed by free text (`no permissions (…); see [https://…]`). Only `key:value`
    tokens whose key is an identifier become details, so the URL inside that free text
    is never mistaken for one.
    """
    devices: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip() or line.startswith("*") or line.startswith("List of devices"):
            # adb's own chatter ("* daemon not running; starting now …") and the header.
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        state = fields[1]
        rest = fields[2:]
        if state == "no" and rest and rest[0].startswith("permissions"):
            state = ADB_STATE_NO_PERMISSIONS
            rest = rest[1:]
        details = {"serial": fields[0], "state": state}
        for field in rest:
            match = _ADB_DETAIL.match(field)
            if match:
                details[match["key"]] = match["value"]
        devices.append(details)
    return devices


def describe_blocked_adb_state(serial: str, state: str) -> str:
    """One message per blocked state, worded so `core.errors.classify` maps it exactly."""
    if state == ADB_STATE_NO_PERMISSIONS:
        return f"this computer has no permissions to access ADB device {serial} over USB"
    if state == ADB_STATE_UNAUTHORIZED:
        return f"ADB device {serial} is unauthorized"
    if state == ADB_STATE_OFFLINE:
        return f"ADB device {serial} is offline"
    return f"ADB device {serial} is {state}"


_WIFI_ROUTE_SOURCE = re.compile(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b")


def parse_wifi_route(output: str) -> str | None:
    """Extract the device's current source address from `ip route get` output.

    Works without mDNS/OpenScreen support, which some `adb` builds omit.
    """
    match = _WIFI_ROUTE_SOURCE.search(output)
    return match.group(1) if match else None


def parse_key_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        values[key.strip()] = value.strip()
    return values


def parse_battery(output: str) -> dict[str, Any]:
    values = parse_key_values(output)
    status_names = {
        "1": "unknown",
        "2": "charging",
        "3": "discharging",
        "4": "not charging",
        "5": "full",
    }
    health_names = {
        "1": "unknown",
        "2": "good",
        "3": "overheat",
        "4": "dead",
        "5": "over voltage",
        "6": "failure",
        "7": "cold",
    }
    powered = [
        source
        for source in ("AC", "USB", "Wireless", "Dock")
        if values.get(f"{source} powered", "false").lower() == "true"
    ]
    battery: dict[str, Any] = {
        "level": int(values["level"]),
        "status": status_names.get(values.get("status", ""), values.get("status", "unknown")),
        "health": health_names.get(values.get("health", ""), values.get("health", "unknown")),
        "powered_by": [source.lower() for source in powered],
    }
    if "temperature" in values:
        battery["temperature_c"] = int(values["temperature"]) / 10
    if "voltage" in values:
        battery["voltage_mv"] = int(values["voltage"])
    return battery


def parse_meminfo(output: str) -> dict[str, int | float]:
    values: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].rstrip(":") in {"MemTotal", "MemAvailable"}:
            values[fields[0].rstrip(":")] = int(fields[1]) * 1024
    if "MemTotal" not in values or "MemAvailable" not in values:
        raise BridgeError("unable to read device memory")
    used = values["MemTotal"] - values["MemAvailable"]
    return {
        "total_bytes": values["MemTotal"],
        "available_bytes": values["MemAvailable"],
        "used_percent": round(used / values["MemTotal"] * 100, 1),
    }


def parse_df(output: str, path: str) -> dict[str, int | str]:
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        raise BridgeError(f"unable to read storage for {path}")
    fields = lines[-1].split()
    if len(fields) < 5:
        raise BridgeError(f"unexpected storage response for {path}")
    return {
        "path": path,
        "total_bytes": int(fields[1]) * 1024,
        "used_bytes": int(fields[2]) * 1024,
        "available_bytes": int(fields[3]) * 1024,
        "used_percent": int(fields[4].rstrip("%")),
    }


class AdbTransport:
    name = "adb"

    def __init__(self, serial: str | None = None, runner: Callable[..., str] | None = None):
        self.serial = serial
        # Resolved at call time (not a signature default) so tests and the setup flow can
        # patch `linkplane.transports.run_command` or inject a runner.
        self.run = runner or run_command

    def select_device(self) -> dict[str, str]:
        if shutil.which("adb") is None:
            raise BridgeError("ADB is not installed")
        devices = parse_adb_devices(self.run(["adb", "devices", "-l"]))
        if self.serial:
            matches = [device for device in devices if device["serial"] == self.serial]
            if not matches:
                raise BridgeError(f"ADB device {self.serial} was not found")
            device = matches[0]
        else:
            ready = [device for device in devices if device["state"] == ADB_STATE_DEVICE]
            if not ready:
                blocked = [device for device in devices if device["state"] != ADB_STATE_DEVICE]
                if blocked:
                    # One diagnosis, by severity: a host permission problem first (the
                    # user cannot fix it on the phone), then a pending authorization,
                    # then an offline device. The remaining states are listed after it.
                    priority = {ADB_STATE_NO_PERMISSIONS: 0, ADB_STATE_UNAUTHORIZED: 1, ADB_STATE_OFFLINE: 2}
                    blocked.sort(key=lambda item: priority.get(item["state"], 3))
                    first = blocked[0]
                    message = describe_blocked_adb_state(first["serial"], first["state"])
                    if len(blocked) > 1:
                        others = ", ".join(f"{item['serial']} ({item['state']})" for item in blocked[1:])
                        message += f"; also found {others}"
                    raise BridgeError(message)
                raise BridgeError("no ADB device connected")
            if len(ready) > 1:
                serials = ", ".join(device["serial"] for device in ready)
                raise BridgeError(f"multiple ADB devices connected ({serials}); use --serial")
            device = ready[0]
        if device["state"] != ADB_STATE_DEVICE:
            raise BridgeError(describe_blocked_adb_state(device["serial"], device["state"]))
        self.serial = device["serial"]
        return device

    def _shell(self, *arguments: str) -> str:
        if not self.serial:
            raise BridgeError("ADB device has not been selected")
        return self.run(["adb", "-s", self.serial, "shell", *arguments])

    def status(self) -> dict[str, Any]:
        selected = self.select_device()
        issues: list[dict[str, str]] = []

        def probe(component: str, operation: Callable[[], Any]) -> Any:
            try:
                return operation()
            except KeyError as error:
                issues.append(
                    {"component": component, "error": f"missing field {error.args[0]}"}
                )
                return None
            except (BridgeError, TypeError, ValueError, ZeroDivisionError) as error:
                issues.append({"component": component, "error": str(error)})
                return None

        def uptime() -> int:
            output = self._shell("cat", "/proc/uptime").split()
            if not output:
                raise BridgeError("unable to read device uptime")
            return int(float(output[0]))

        def text(*arguments: str) -> str:
            value = self._shell(*arguments).strip()
            if not value:
                raise BridgeError("device returned an empty response")
            return value

        manufacturer = probe(
            "device.manufacturer",
            lambda: text("getprop", "ro.product.manufacturer"),
        )
        model = probe(
            "device.model", lambda: text("getprop", "ro.product.model")
        )
        android = probe(
            "device.android",
            lambda: text("getprop", "ro.build.version.release"),
        )
        kernel = probe("device.kernel", lambda: text("uname", "-r"))
        battery = probe(
            "battery", lambda: parse_battery(self._shell("dumpsys", "battery"))
        )
        memory = probe(
            "memory", lambda: parse_meminfo(self._shell("cat", "/proc/meminfo"))
        )
        storage = probe(
            "storage", lambda: parse_df(self._shell("df", "-Pk", "/sdcard"), "/sdcard")
        )
        uptime_seconds = probe("uptime", uptime)
        if all(
            value is None
            for value in (
                manufacturer,
                model,
                android,
                kernel,
                battery,
                memory,
                storage,
                uptime_seconds,
            )
        ):
            raise BridgeError("unable to read any ADB telemetry")

        return {
            "transport": self.name,
            "device": {
                "serial": self.serial,
                "manufacturer": manufacturer,
                "model": model,
                "android": android,
                "kernel": kernel,
                "product": selected.get("product"),
            },
            "battery": battery,
            "memory": memory,
            "storage": storage,
            "uptime_seconds": uptime_seconds,
            "issues": issues,
        }


class SshTransport:
    name = "ssh"

    def __init__(
        self,
        host: str | None,
        user: str | None,
        port: int = 8022,
        identity_file: str | None = None,
        runner: Callable[..., str] = run_command,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.run = runner

    def command(self, remote_command: str) -> list[str]:
        if not self.host or not self.user:
            raise BridgeError("SSH requires a host and user in the Linkplane configuration")
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
        ]
        if self.identity_file:
            command.extend(["-i", os.path.expanduser(self.identity_file)])
        command.extend(["-p", str(self.port), f"{self.user}@{self.host}", remote_command])
        return command

    def execute(self, arguments: list[str], *, timeout: int = 15) -> str:
        return self.run(self.command(shlex.join(arguments)), timeout=timeout)

    def execute_input(
        self, arguments: list[str], input_text: str, *, timeout: int = 15
    ) -> str:
        return self.run(
            self.command(shlex.join(arguments)),
            timeout=timeout,
            input_text=input_text,
        )

    def status(self) -> dict[str, Any]:
        try:
            legacy = json.loads(self.run(self.command("$HOME/phone-status-json.sh"), timeout=15))
        except json.JSONDecodeError as error:
            raise BridgeError("Termux returned invalid status JSON") from error
        if not isinstance(legacy, dict):
            raise BridgeError("Termux status JSON must contain an object")

        issues: list[dict[str, str]] = []

        def probe(component: str, operation: Callable[[], Any]) -> Any:
            try:
                return operation()
            except KeyError as error:
                issues.append(
                    {"component": component, "error": f"missing field {error.args[0]}"}
                )
                return None
            except (TypeError, ValueError, ZeroDivisionError) as error:
                issues.append({"component": component, "error": str(error)})
                return None

        def normalize_battery() -> dict[str, Any]:
            battery = legacy["battery"]
            if not isinstance(battery, dict):
                raise TypeError("battery telemetry must be an object")
            plugged = str(battery.get("plugged", "")).removeprefix("PLUGGED_").lower()
            normalized: dict[str, Any] = {
                "level": int(battery.get("percentage", battery.get("level", 0))),
                "status": str(battery.get("status", "unknown")).lower().replace("_", " "),
                "health": str(battery.get("health", "unknown")).lower().replace("_", " "),
                "powered_by": [] if plugged in {"", "unplugged"} else [plugged],
            }
            if battery.get("temperature") is not None:
                normalized["temperature_c"] = float(battery["temperature"])
            if battery.get("voltage") is not None:
                normalized["voltage_mv"] = int(battery["voltage"])
            return normalized

        def normalize_memory() -> dict[str, int | float]:
            memory = legacy["memory"]
            if not isinstance(memory, dict):
                raise TypeError("memory telemetry must be an object")
            total = int(memory["total_kb"])
            available = int(memory["available_kb"])
            return {
                "total_bytes": total * 1024,
                "available_bytes": available * 1024,
                "used_percent": round((total - available) / total * 100, 1),
            }

        def normalize_storage() -> dict[str, int | str]:
            storage = legacy["storage"]
            if not isinstance(storage, dict):
                raise TypeError("storage telemetry must be an object")
            return {
                "path": "$HOME",
                "total_bytes": int(storage["total_kb"]) * 1024,
                "used_bytes": int(storage["used_kb"]) * 1024,
                "available_bytes": int(storage["free_kb"]) * 1024,
                "used_percent": int(storage["used_percent"]),
            }

        raw_device = legacy.get("device")
        if isinstance(raw_device, dict):
            device = dict(raw_device)
        else:
            device = {}
            issues.append(
                {"component": "device", "error": "device telemetry must be an object"}
            )
        device["serial"] = None
        battery = probe("battery", normalize_battery)
        memory = probe("memory", normalize_memory)
        storage = probe("storage", normalize_storage)
        if not raw_device and battery is None and memory is None and storage is None:
            raise BridgeError("Termux status JSON did not contain usable telemetry")
        return {
            "transport": self.name,
            "device": device,
            "battery": battery,
            "memory": memory,
            "storage": storage,
            "uptime_seconds": None,
            "issues": issues,
        }


def resolve_config_path(path: str | None = None) -> Path:
    from linkplane.paths import config_file

    return config_file("config.json", path, env_name="CONFIG")


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = resolve_config_path(path)
    if not config_path.exists():
        return {}
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"unable to read configuration at {config_path}: {error}") from error
    if not isinstance(config, dict):
        raise BridgeError(f"configuration at {config_path} must contain a JSON object")
    return config


def ssh_from_config(
    config: dict[str, Any],
    *,
    host: str | None = None,
    user: str | None = None,
    port: int | None = None,
    identity_file: str | None = None,
    use_environment: bool = True,
) -> SshTransport:
    ssh_config = config.get("ssh", {})
    if not isinstance(ssh_config, dict):
        raise BridgeError("the ssh configuration must be a JSON object")
    environment = os.environ.get if use_environment else lambda _name: None
    raw_port = port or environment("LINKPLANE_SSH_PORT") or ssh_config.get("port", 8022)
    try:
        resolved_port = int(raw_port)
    except (TypeError, ValueError) as error:
        raise BridgeError(f"invalid SSH port: {raw_port}") from error
    return SshTransport(
        host=host or environment("LINKPLANE_SSH_HOST") or ssh_config.get("host"),
        user=user or environment("LINKPLANE_SSH_USER") or ssh_config.get("user"),
        port=resolved_port,
        identity_file=identity_file
        or environment("LINKPLANE_SSH_KEY")
        or ssh_config.get("identity_file"),
    )
