from __future__ import annotations

from typing import Any

from linkplane.commands import print_envelope
from linkplane.providers.base import BatteryReading, Provider


def render(reading: BatteryReading) -> None:
    print("Linkplane Battery")
    print(f"Level       {reading.level}%")
    print(f"Status      {reading.status}")
    print(f"Health      {reading.health}")
    powered = ", ".join(reading.powered_by) if reading.powered_by else "battery"
    print(f"Powered by  {powered}")
    if reading.temperature_c is not None:
        print(f"Temperature {reading.temperature_c:g} °C")
    if reading.voltage_mv is not None:
        print(f"Voltage     {reading.voltage_mv} mV")


def run(arguments: Any, provider: Provider) -> int:
    reading = provider.battery()
    if arguments.json:
        print_envelope(True, {**reading.to_dict(), "address": provider.address})
    else:
        render(reading)
        print(f"Provider    {provider.name} ({provider.address})")
    return 0
