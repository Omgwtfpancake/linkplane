"""The device registry: profiles plus observed state, resolved to Devices and targets.

One place answers "which devices exist", "what does this id refer to", and "how do I
reach it" for every interface (CLI, the local API, later a GUI), so none of them copies
the profile → serial / SSH logic (docs/local-api-design.md §6, §24 slice 0).

Identity (docs/local-api-design.md §6.1): the canonical, rename-proof id of a device is
`DeviceProfile.device_id` (the hardware serial recorded at pairing). The profile name is
an alias, and so is `serial:<x>` for a device that is observed but not paired. Pinned
records (`Event.device`, `DeviceState.device`, `JobRecord.device`) keep carrying the
registry *name*; `DeviceRecord.name` is the join key into them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from linkplane.core.state import DeviceState
from linkplane.profiles import DeviceProfile, profiles_from_config
from linkplane.transports import BridgeError, load_config

SERIAL_PREFIX = "serial:"


def profile_ssh_config(config: Mapping[str, Any], profile: DeviceProfile) -> dict[str, Any] | None:
    """The profile's SSH endpoint, or the legacy top-level `ssh` block when it names the
    same device (pre-profile configurations). Moved verbatim from the CLI."""
    if profile.ssh is not None:
        return profile.ssh
    legacy_ssh = config.get("ssh", {})
    if isinstance(legacy_ssh, dict) and legacy_ssh.get("device_id") == profile.device_id:
        resolved = dict(legacy_ssh)
        try:
            resolved["port"] = int(resolved.get("port", 8022))
        except (TypeError, ValueError) as error:
            raise BridgeError(f"invalid SSH port: {resolved.get('port')}") from error
        return resolved
    return None


def identities(config: Mapping[str, Any]) -> dict[str, str]:
    """serial -> registry name, so observed events name devices the way profiles do."""
    profiles = profiles_from_config(dict(config))
    found = {serial: profile.name for profile in profiles.values() for serial in profile.adb_serials}
    legacy = config.get("ssh")
    if isinstance(legacy, dict) and isinstance(legacy.get("device_id"), str):
        found.setdefault(legacy["device_id"], legacy["device_id"])
    return found


def ssh_address(ssh: Mapping[str, Any]) -> str:
    return f"{ssh.get('user', '')}@{ssh.get('host', '')}:{ssh.get('port', 8022)}"


@dataclass(frozen=True)
class ProviderEndpoint:
    provider: str
    address: str
    observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "address": self.address, "observed": self.observed}


@dataclass(frozen=True)
class DeviceRecord:
    """The Device projection: identity + providers + observed state. Never key material."""

    device_id: str
    name: str
    providers: tuple[ProviderEndpoint, ...] = ()
    state: DeviceState | None = None
    default: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def observed(self) -> bool:
        return self.state is not None

    def to_dict(self) -> dict[str, Any]:
        state = self.state
        return {
            "device_id": self.device_id,
            "name": self.name,
            "observed": self.observed,
            "default": self.default,
            "connection": state.connection if state else None,
            "provider": state.provider if state else None,
            "address": state.address if state else None,
            "last_seen": state.last_seen if state else None,
            "providers": [endpoint.to_dict() for endpoint in self.providers],
            "state": state.to_dict() if state else None,
        }


@dataclass(frozen=True)
class Target:
    """How to reach a device for one action: an ADB serial and/or an SSH endpoint."""

    device_id: str
    name: str
    serial: str | None = None
    ssh: dict[str, Any] | None = None

    @property
    def providers(self) -> tuple[str, ...]:
        found: list[str] = []
        if self.serial:
            found.append("adb")
        if self.ssh and self.ssh.get("host") and self.ssh.get("user"):
            found.append("ssh")
        return tuple(found)


class Registry:
    """Profiles from `config.json` merged with the daemon's observed states."""

    def __init__(self, config: Mapping[str, Any], states: Mapping[str, DeviceState] | None = None):
        self.config = dict(config)
        self.states = dict(states or {})
        self.profiles = profiles_from_config(self.config)

    @classmethod
    def load(cls, config_path: str | None = None, states: Mapping[str, DeviceState] | None = None) -> Registry:
        return cls(load_config(config_path), states)

    @staticmethod
    def states_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, DeviceState]:
        """`state.json` (`linkplane.state/1`) -> DeviceState per name."""
        devices = snapshot.get("devices") or {}
        return {name: DeviceState(**record) for name, record in devices.items() if isinstance(record, dict)}

    # -- listing --------------------------------------------------------------------

    def identities(self) -> dict[str, str]:
        return identities(self.config)

    def devices(self) -> list[DeviceRecord]:
        default_name = self.config.get("default_device")
        records: list[DeviceRecord] = []
        claimed: set[str] = set()
        for name in sorted(self.profiles):
            profile = self.profiles[name]
            claimed.add(name)
            record = self._from_profile(profile, default=name == default_name)
            if record.state is not None:
                claimed.add(record.state.device)  # a state observed under an old name still belongs here
            records.append(record)
        legacy = self.config.get("ssh")
        if isinstance(legacy, dict) and isinstance(legacy.get("device_id"), str):
            legacy_id = legacy["device_id"]
            if legacy_id not in {record.device_id for record in records}:
                claimed.add(legacy_id)
                records.append(DeviceRecord(
                    legacy_id, legacy_id,
                    (ProviderEndpoint("ssh", ssh_address(legacy)),),
                    self.states.get(legacy_id), aliases=(legacy_id,),
                ))
        for name in sorted(self.states):
            if name in claimed or name == "*":
                continue
            state = self.states[name]
            serial = name[len(SERIAL_PREFIX):] if name.startswith(SERIAL_PREFIX) else (state.address or name)
            records.append(DeviceRecord(
                serial, name,
                (ProviderEndpoint("adb", state.address or serial, observed=True),),
                state, aliases=(name, f"{SERIAL_PREFIX}{serial}"),
            ))
        return records

    def _state_for(self, profile: DeviceProfile) -> DeviceState | None:
        """By registry name first; else by serial, so a profile renamed since the daemon
        started still finds its observation (the observer keys by the name it was given)."""
        state = self.states.get(profile.name)
        if state is not None:
            return state
        for candidate in self.states.values():
            if candidate.address and candidate.address in profile.adb_serials:
                return candidate
        return None

    def _from_profile(self, profile: DeviceProfile, *, default: bool) -> DeviceRecord:
        state = self._state_for(profile)
        endpoints: list[ProviderEndpoint] = []
        serials = list(profile.adb_serials)
        if profile.preferred_adb_serial in serials:
            serials.remove(profile.preferred_adb_serial)
            serials.insert(0, profile.preferred_adb_serial)
        for serial in serials:
            observed = state is not None and state.address == serial
            endpoints.append(ProviderEndpoint("adb", serial, observed=observed))
        ssh = profile_ssh_config(self.config, profile)
        if ssh and ssh.get("host"):
            endpoints.append(ProviderEndpoint("ssh", ssh_address(ssh)))
        aliases = (profile.name, profile.device_id) + tuple(f"{SERIAL_PREFIX}{serial}" for serial in profile.adb_serials)
        return DeviceRecord(profile.device_id, profile.name, tuple(endpoints), state, default, aliases)

    # -- resolution -----------------------------------------------------------------

    def resolve(self, reference: str | None) -> DeviceRecord | None:
        """Canonical `device_id` first, then a profile name, then `serial:<x>`; None = the
        default device. Returns None when nothing matches (never raises)."""
        records = self.devices()
        if reference is None:
            return next((record for record in records if record.default), None)
        for record in records:
            if record.device_id == reference:
                return record
        for record in records:
            if record.name == reference:
                return record
        for record in records:
            if reference in record.aliases:
                return record
        return None

    def target(self, device: DeviceRecord) -> Target:
        """The serial the device is currently reachable on (the observed one when online,
        else the profile's preferred serial) and its SSH endpoint, if any."""
        profile = self.profiles.get(device.name)
        serial: str | None = None
        ssh: dict[str, Any] | None = None
        if profile is not None:
            state = device.state
            if state is not None and state.online and state.address in profile.adb_serials:
                serial = state.address
            else:
                serial = profile.adb_serial
            ssh = profile_ssh_config(self.config, profile)
        else:
            adb = next((endpoint for endpoint in device.providers if endpoint.provider == "adb"), None)
            serial = adb.address if adb else None
            legacy = self.config.get("ssh")
            if isinstance(legacy, dict) and legacy.get("device_id") == device.device_id:
                ssh = dict(legacy)
        return Target(device.device_id, device.name, serial, ssh)


StateLoader = Callable[[], Mapping[str, DeviceState]]
