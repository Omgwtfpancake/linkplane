"""Observed state sources and the Observer loop (Core v0.2, slice 1).

Sources produce `DeviceState`s and nothing else: the ADB device tracker (push, via
`adb track-devices -l`), and battery / Wi-Fi polls over the ADB provider. `Observer`
merges them, keeps the last state per device, and hands `core.state.diff` events to a
sink. Everything is injectable so the loop is tested with canned frames and fake pollers.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Any, Callable, Iterator

from linkplane.core.events import Event, now_iso
from linkplane.core.state import (
    CONNECTED,
    DEFAULT_LOW_BATTERY,
    DISCONNECTED,
    OFFLINE,
    UNAUTHORIZED,
    DeviceState,
    diff,
)
from linkplane.operations import CancellationToken
from linkplane.providers.adb import ADBProvider
from linkplane.transports import AdbTransport, BridgeError, parse_adb_devices, run_command

log = logging.getLogger("linkplane.observe")

EventSink = Callable[[Event], None]
BatteryPoller = Callable[[str], dict[str, Any] | None]
WifiPoller = Callable[[str], str | None]

_WIFI_SSID = re.compile(r'Wifi is connected to "(?P<ssid>[^"]*)"')


def parse_wifi_status(output: str) -> str | None:
    """SSID from `adb shell cmd wifi status`, or None when not connected."""
    match = _WIFI_SSID.search(output)
    if match is None:
        return None
    return match.group("ssid") or None


def parse_track_frames(chunk: bytes, buffer: bytearray) -> list[str]:
    """Split the `adb track-devices` byte stream into complete frames (payload text).

    Each frame is a 4-hex-digit length followed by that many bytes of `adb devices -l`
    style text. Partial frames stay in `buffer` for the next call.
    """
    buffer.extend(chunk)
    frames: list[str] = []
    while len(buffer) >= 4:
        try:
            length = int(bytes(buffer[:4]).decode("ascii"), 16)
        except ValueError:
            log.warning("discarding unparseable track-devices data")
            buffer.clear()
            break
        if len(buffer) < 4 + length:
            break
        payload = bytes(buffer[4 : 4 + length]).decode("utf-8", errors="replace")
        del buffer[: 4 + length]
        frames.append(payload)
    return frames


def connection_from_adb_state(state: str) -> str:
    if state == "device":
        return CONNECTED
    if state == "unauthorized":
        return UNAUTHORIZED
    if state == "offline":
        return OFFLINE
    return DISCONNECTED


class AdbDeviceTracker:
    """Yields `{serial: adb_state}` snapshots from a long-lived `adb track-devices -l`."""

    def __init__(self, cancel: CancellationToken | None = None):
        self.cancel = cancel
        self._process: subprocess.Popen[bytes] | None = None

    def snapshots(self) -> Iterator[dict[str, dict[str, str]]]:
        self._process = subprocess.Popen(
            ["adb", "track-devices", "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        assert self._process.stdout is not None
        buffer = bytearray()
        try:
            while True:
                chunk = self._process.stdout.read1(4096)  # type: ignore[attr-defined]
                if not chunk:
                    return
                for payload in parse_track_frames(chunk, buffer):
                    yield {
                        raw["serial"]: raw
                        for raw in parse_adb_devices("List of devices attached\n" + payload)
                    }
        finally:
            self.stop()

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        if self._process.stdout is not None:
            self._process.stdout.close()


def poll_battery(serial: str) -> dict[str, Any] | None:
    try:
        return ADBProvider(AdbTransport(serial)).battery().to_dict()
    except BridgeError as error:
        log.warning("battery poll failed for %s: %s", serial, error)
        return None


def poll_wifi(serial: str) -> str | None:
    try:
        return parse_wifi_status(
            run_command(["adb", "-s", serial, "shell", "cmd", "wifi", "status"], timeout=8)
        )
    except BridgeError as error:
        log.warning("wifi poll failed for %s: %s", serial, error)
        return None


class Observer:
    """Merge the tracker and pollers into per-device state; emit diff events to `sink`."""

    def __init__(
        self,
        sink: EventSink,
        *,
        identities: dict[str, str] | None = None,
        interval: float = 30.0,
        low_battery: int = DEFAULT_LOW_BATTERY,
        cancel: CancellationToken | None = None,
        tracker: Iterator[dict[str, dict[str, str]]] | None = None,
        battery_poller: BatteryPoller = poll_battery,
        wifi_poller: WifiPoller = poll_wifi,
        poll_wifi_enabled: bool = True,
    ):
        self.sink = sink
        self.identities = dict(identities or {})
        self.interval = interval
        self.low_battery = low_battery
        self.cancel = cancel or CancellationToken()
        self._tracker_source = tracker
        self.battery_poller = battery_poller
        self.wifi_poller = wifi_poller
        self.poll_wifi_enabled = poll_wifi_enabled
        self.states: dict[str, DeviceState] = {}
        self._lock = threading.Lock()
        # The tracker thread polls right after a snapshot and the main loop polls on its
        # timer; without serializing them both can observe "no telemetry yet" for a
        # freshly connected device and emit the opening observations twice.
        self._poll_lock = threading.Lock()
        self._addresses: dict[str, str] = {}  # device -> serial
        # The tracker's first snapshot is the start-up baseline: devices in it are reported
        # as `initial` observations. A device that first appears in a later snapshot was
        # plugged in while observing, which is a change (initial=false), even though this
        # observer has never seen it before.
        self._baseline_seen = False

    def device_name(self, serial: str) -> str:
        return self.identities.get(serial, f"serial:{serial}")

    # -- state transitions ------------------------------------------------------------

    def _apply(self, current: DeviceState, *, fresh: bool = False, baseline: bool = False) -> None:
        with self._lock:
            previous = self.states.get(current.device)
            if previous is None and not baseline:
                # Unknown before, but not part of the start-up baseline: compare against
                # "absent" so the arrival is a change, not an opening observation.
                previous = DeviceState(current.device)
            events = diff(previous, current, low_battery=self.low_battery, fresh=fresh)
            self.states[current.device] = current
        for event in events:
            self.sink(event)

    def on_tracker_snapshot(self, snapshot: dict[str, dict[str, str]]) -> None:
        baseline = not self._baseline_seen
        self._baseline_seen = True
        seen: set[str] = set()
        for serial, raw in snapshot.items():
            device = self.device_name(serial)
            seen.add(device)
            self._addresses[device] = serial
            previous = self.states.get(device)
            connection = connection_from_adb_state(raw.get("state", ""))
            state = (previous or DeviceState(device)).touch(
                provider="adb",
                connection=connection,
                address=serial,
                last_seen=now_iso() if connection == CONNECTED else (previous.last_seen if previous else None),
            )
            if connection != CONNECTED:
                state = state.touch(battery=None, wifi_ssid=None)
            self._apply(state, baseline=baseline)
        for device, previous in list(self.states.items()):
            if device not in seen and previous.connection != DISCONNECTED:
                self._apply(previous.touch(connection=DISCONNECTED, battery=None, wifi_ssid=None))

    def poll_once(self) -> None:
        with self._poll_lock:
            for device, state in list(self.states.items()):
                if not state.online or state.address is None:
                    continue
                battery = self.battery_poller(state.address)
                wifi = self.wifi_poller(state.address) if self.poll_wifi_enabled else state.wifi_ssid
                self._apply(
                    state.touch(
                        battery=battery if battery is not None else state.battery,
                        wifi_ssid=wifi,
                        last_seen=now_iso(),
                    ),
                    fresh=state.battery is None,  # first telemetry since (re)connection
                )

    # -- the loop ---------------------------------------------------------------------

    def run(self) -> None:
        tracker = self._tracker_source
        owned: AdbDeviceTracker | None = None
        if tracker is None:
            owned = AdbDeviceTracker(self.cancel)
            tracker = owned.snapshots()
        sources = ["adb.track-devices", "adb.battery"] + (["adb.wifi"] if self.poll_wifi_enabled else [])
        self.sink(Event("observer.started", "*", data={"sources": sources, "interval": self.interval}))
        stop = threading.Event()

        def pump() -> None:
            try:
                for snapshot in tracker:
                    if self.cancel.cancelled:
                        break
                    self.on_tracker_snapshot(snapshot)
                    self.poll_once()
            except Exception as error:  # noqa: BLE001 - a dead tracker must not kill the loop
                log.warning("device tracker stopped: %s", error)
            finally:
                stop.set()

        thread = threading.Thread(target=pump, name="linkplane-tracker", daemon=True)
        thread.start()
        try:
            while not stop.is_set():
                if self.cancel.wait(self.interval):
                    break
                self.poll_once()
        finally:
            self.cancel.cancel("observer stopped")
            if owned is not None:
                owned.stop()
            thread.join(timeout=3)
            self.sink(Event("observer.stopped", "*", data={"sources": sources}))
