"""ADBProvider: the existing ADB backend behind the Provider interface.

The brief names SSH as the only v0.1 provider, but ADB is already this repository's
primary working path (the paired test phone is on USB, and `status` prefers ADB), so it
is wrapped at the same thin level rather than left as a second, un-modelled way in. See
docs/adr/0006-ssh-provider.md.
"""

from __future__ import annotations

import time

from linkplane.core import errors
from linkplane.core.capability import (
    UNSUPPORTED,
    PERMISSION_DENIED,
    SUPPORTED,
    UNAVAILABLE,
    CapabilityReport,
    complete,
)
from linkplane.dependencies import scrcpy_dependency_plan
from linkplane.providers.base import BatteryReading, PingResult, Provider
from linkplane.core.telemetry import StatusResult
from linkplane.transports import AdbTransport, BridgeError, parse_battery


TERMUX_API_DETAIL = (
    "needs the Termux/SSH provider with the Termux:API app on the phone (linkplane pair ssh); "
    "not available over ADB"
)


class ADBProvider(Provider):
    name = "adb"

    def __init__(self, transport: AdbTransport, *, timeout: int = 8):
        self.transport = transport
        self.timeout = timeout

    @property
    def address(self) -> str:
        return self.transport.serial or "(auto)"

    def _select(self) -> dict[str, str]:
        try:
            return self.transport.select_device()
        except BridgeError as error:
            raise errors.classify(error, provider=self.name) from error

    def ping(self) -> PingResult:
        started = time.monotonic()
        try:
            self.transport.select_device()
            output = self.transport.run(
                ["adb", "-s", self.transport.serial, "shell", "echo", "ok"], timeout=self.timeout
            )
        except BridgeError as error:
            return PingResult(False, self.name, self.address, detail=str(error))
        latency = round((time.monotonic() - started) * 1000, 1)
        if output.strip() != "ok":
            return PingResult(
                False, self.name, self.address, latency, "unexpected response from the phone"
            )
        return PingResult(True, self.name, self.address, latency)

    def status(self) -> StatusResult:
        try:
            return StatusResult.from_dict(self.transport.status())
        except BridgeError as error:
            raise errors.classify(error, provider=self.name) from error
        except (KeyError, TypeError, ValueError) as error:
            raise errors.LinkplaneError(
                errors.PROVIDER_BAD_OUTPUT, f"ADB status output was malformed: {error}"
            ) from error

    def battery(self) -> BatteryReading:
        self._select()
        try:
            raw = self.transport.run(
                ["adb", "-s", self.transport.serial, "shell", "dumpsys", "battery"],
                timeout=self.timeout,
            )
            battery = parse_battery(raw)
        except BridgeError as error:
            raise errors.classify(error, provider=self.name) from error
        except (KeyError, TypeError, ValueError) as error:
            raise errors.LinkplaneError(
                errors.PROVIDER_BAD_OUTPUT, f"battery output was malformed: {error}"
            ) from error
        return BatteryReading.from_status(battery, self.name)

    def capabilities(self) -> tuple[CapabilityReport, ...]:
        try:
            self.transport.select_device()
        except BridgeError as error:
            classified = errors.classify(error, provider=self.name)
            status = (
                PERMISSION_DENIED
                if classified.code == errors.AUTH_UNAUTHORIZED_DEVICE
                else UNAVAILABLE
            )
            return complete(
                {
                    name: CapabilityReport(name, status, str(error))
                    for name in (
                        "device.ping", "device.status", "battery.read", "storage.read",
                        "files.send", "backup.photos", "notify.post", "device.find",
                    )
                },
                self.name,
            )
        reports = {
            name: CapabilityReport(name, SUPPORTED, "ADB shell")
            for name in ("device.ping", "device.status", "battery.read", "storage.read")
        }
        reports["files.send"] = CapabilityReport("files.send", SUPPORTED, "adb push")
        reports["backup.photos"] = CapabilityReport("backup.photos", SUPPORTED, "adb pull + sha256")
        reports["notify.post"] = CapabilityReport("notify.post", SUPPORTED, "Android shell API")
        reports["device.find"] = CapabilityReport("device.find", SUPPORTED, "Android shell (ring, torch, vibrate)")
        # Truthful, not merely "not implemented": these run through the SSH provider and
        # need the Termux:API app on the phone (dependencies.DEPENDENCIES: termux-api).
        # A working USB/ADB setup does not enable them, and setup must never imply it.
        for name in ("clipboard.read", "clipboard.write", "clipboard.sync", "camera.capture"):
            reports[name] = CapabilityReport(
                name, UNSUPPORTED, TERMUX_API_DETAIL
            )
        scrcpy = scrcpy_dependency_plan()
        reports["screen.control"] = (
            CapabilityReport("screen.control", SUPPORTED, "scrcpy", ("D",))
            if scrcpy.available
            else CapabilityReport(
                "screen.control", UNAVAILABLE, "scrcpy is not installed on this computer", ("D",)
            )
        )
        return complete(reports, self.name)
