"""Observed device state and the pure diff that turns two observations into events.

`DeviceState` is the last thing known about one device. `diff()` compares the previous
and current state and emits the canonical `Event`s (docs/core-v0.2-design.md, "Event
catalogue"); it is the only place event semantics live, so sources stay dumb and the whole
catalogue is unit-testable without a phone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from linkplane.core.events import Event, now_iso

CONNECTED = "connected"
DISCONNECTED = "disconnected"
UNAUTHORIZED = "unauthorized"
OFFLINE = "offline"

DEFAULT_LOW_BATTERY = 20


@dataclass(frozen=True)
class DeviceState:
    device: str
    provider: str | None = None
    connection: str = DISCONNECTED
    address: str | None = None
    battery: dict[str, Any] | None = None
    wifi_ssid: str | None = None
    last_seen: str | None = None
    updated: str = field(default_factory=now_iso)

    @property
    def online(self) -> bool:
        return self.connection == CONNECTED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def touch(self, **changes: Any) -> DeviceState:
        return replace(self, updated=now_iso(), **changes)


def _event(state: DeviceState, type_: str, initial: bool, **data: Any) -> Event:
    payload = dict(data)
    if initial:
        payload["initial"] = True
    return Event(type_, state.device, provider=state.provider, data=payload)


def diff(
    previous: DeviceState | None,
    current: DeviceState,
    *,
    low_battery: int = DEFAULT_LOW_BATTERY,
    fresh: bool = False,
) -> list[Event]:
    """Events that take `previous` to `current`.

    `previous=None` is the opening snapshot; `fresh=True` marks the first telemetry read
    after a (re)connection. Both flag their events `initial` so a subscriber can tell an
    observation from a change.
    """
    initial = previous is None or fresh
    events: list[Event] = []
    before = previous or DeviceState(current.device)

    if before.connection != current.connection:
        if current.connection == CONNECTED:
            if before.connection == UNAUTHORIZED:
                events.append(_event(current, "device.authorized", initial, address=current.address))
            events.append(
                _event(
                    current, "device.connected", initial,
                    address=current.address, transport=current.provider,
                )
            )
        elif current.connection == UNAUTHORIZED:
            events.append(_event(current, "device.unauthorized", initial, address=current.address))
        elif before.connection == CONNECTED:
            # CONNECTED -> offline/disconnected. Any other transition (e.g. unauthorized
            # -> gone, or the opening snapshot of an absent device) is not an event.
            events.append(
                _event(
                    current, "device.disconnected", initial,
                    address=before.address, transport=before.provider,
                )
            )

    if not current.online:
        # A device that is gone has no battery or Wi-Fi to report on; its telemetry is
        # cleared silently, and a reconnect re-emits it as fresh observations.
        return events

    old_battery = before.battery or {}
    new_battery = current.battery or {}
    if new_battery and new_battery.get("level") != old_battery.get("level"):
        events.append(
            _event(
                current, "battery.changed", initial,
                level=new_battery.get("level"), previous=old_battery.get("level"),
                status=new_battery.get("status"), powered_by=list(new_battery.get("powered_by", ())),
            )
        )
        level = new_battery.get("level")
        old_level = old_battery.get("level")
        if isinstance(level, int):
            was_low = isinstance(old_level, int) and old_level < low_battery
            is_low = level < low_battery
            if is_low and (initial or not was_low):
                events.append(_event(current, "battery.low", initial, level=level, threshold=low_battery))
            elif was_low and not is_low:
                events.append(_event(current, "battery.ok", initial, level=level, threshold=low_battery))
    if new_battery:
        old_power = bool(old_battery.get("powered_by"))
        new_power = bool(new_battery.get("powered_by"))
        if new_power and (initial or not old_power):
            events.append(
                _event(current, "charging.started", initial, powered_by=list(new_battery["powered_by"]))
            )
        elif old_power and not new_power and not initial:
            events.append(_event(current, "charging.stopped", initial, powered_by=[]))

    if before.wifi_ssid != current.wifi_ssid:
        if current.wifi_ssid:
            events.append(
                _event(current, "wifi.connected", initial, ssid=current.wifi_ssid, previous=before.wifi_ssid)
            )
        elif before.wifi_ssid and not initial:
            events.append(_event(current, "wifi.disconnected", initial, previous=before.wifi_ssid))
    return events
