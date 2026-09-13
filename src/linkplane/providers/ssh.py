"""SSHProvider: the existing Termux + SSH backend behind the Provider interface.

Wraps `SshTransport` and the phone-side scripts (`legacy/termux/phone-status-json.sh`,
Termux:API commands) unchanged; this module only translates between them and the public
capability vocabulary and structured errors.
"""

from __future__ import annotations

import time

from linkplane.core import errors
from linkplane.core.capability import (
    PROVIDER_ERROR,
    SUPPORTED,
    UNAVAILABLE,
    CapabilityReport,
    complete,
)
from linkplane.devices import probe_ssh_endpoint
from linkplane.providers.base import BatteryReading, PingResult, Provider
from linkplane.core.telemetry import StatusResult
from linkplane.transports import BridgeError, SshTransport


class SSHProvider(Provider):
    name = "ssh"

    def __init__(self, transport: SshTransport, *, timeout: int = 8):
        self.transport = transport
        self.timeout = timeout

    @property
    def address(self) -> str:
        return f"{self.transport.user}@{self.transport.host}:{self.transport.port}"

    def ping(self) -> PingResult:
        started = time.monotonic()
        try:
            output = self.transport.run(self.transport.command("printf ok"), timeout=self.timeout)
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
                errors.PROVIDER_BAD_OUTPUT, f"Termux status output was malformed: {error}"
            ) from error

    def battery(self) -> BatteryReading:
        status = self.status()
        if status.battery is None:
            issue = next((item for item in status.issues if item.component == "battery"), None)
            raise errors.LinkplaneError(
                errors.PROVIDER_PARTIAL,
                "battery data could not be retrieved"
                + (f": {issue.error}" if issue is not None else ""),
                ("check that Termux:API is installed and `termux-battery-status` works",),
            )
        return BatteryReading.from_status(status.battery, self.name)

    def capabilities(self) -> tuple[CapabilityReport, ...]:
        try:
            _details, commands = probe_ssh_endpoint(self.transport, timeout=self.timeout)
        except BridgeError as error:
            reason = str(error)
            return complete(
                {
                    "device.ping": CapabilityReport("device.ping", UNAVAILABLE, reason),
                    "device.status": CapabilityReport("device.status", PROVIDER_ERROR, reason),
                    "battery.read": CapabilityReport("battery.read", PROVIDER_ERROR, reason),
                    "storage.read": CapabilityReport("storage.read", PROVIDER_ERROR, reason),
                },
                self.name,
            )
        reports: dict[str, CapabilityReport] = {
            "device.ping": CapabilityReport("device.ping", SUPPORTED, "SSH round trip"),
        }
        if "phone-status-json.sh" in commands:
            for name in ("device.status", "battery.read", "storage.read"):
                reports[name] = CapabilityReport(name, SUPPORTED, "Termux status script")
        else:
            for name in ("device.status", "battery.read", "storage.read"):
                reports[name] = CapabilityReport(
                    name, UNAVAILABLE, "phone-status-json.sh is not installed on the phone"
                )
        reports["notify.post"] = self._termux_api("notify.post", "termux-notification", commands)
        reports["clipboard.read"] = self._termux_api(
            "clipboard.read", "termux-clipboard-get", commands
        )
        reports["clipboard.write"] = self._termux_api(
            "clipboard.write", "termux-clipboard-set", commands
        )
        reports["camera.capture"] = self._termux_api(
            "camera.capture", "termux-camera-photo", commands
        )
        if {"termux-clipboard-get", "termux-clipboard-set"} <= commands:
            reports["clipboard.sync"] = CapabilityReport(
                "clipboard.sync", SUPPORTED, "Termux:API (termux-clipboard-get/set)"
            )
        else:
            reports["clipboard.sync"] = CapabilityReport(
                "clipboard.sync", UNAVAILABLE, "termux-clipboard-get and termux-clipboard-set are not both installed on the phone"
            )
        return complete(reports, self.name)

    @staticmethod
    def _termux_api(name: str, command: str, commands: set[str]) -> CapabilityReport:
        if command in commands:
            return CapabilityReport(name, SUPPORTED, f"Termux:API ({command})")
        return CapabilityReport(name, UNAVAILABLE, f"{command} is not installed on the phone")
