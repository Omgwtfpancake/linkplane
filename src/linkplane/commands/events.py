from __future__ import annotations

import json
import sys
from typing import Any

from linkplane import daemon as daemond
from linkplane.core.events import Event
from linkplane.history import HistoryWriter
from linkplane.observe import Observer
from linkplane.operations import CancellationToken, cancel_on_interrupt


def summarize(event: Event) -> str:
    data = event.data
    if event.type == "battery.changed":
        return f"{data.get('level')}%"
    if event.type in {"battery.low", "battery.ok"}:
        return f"{data.get('level')}% (threshold {data.get('threshold')})"
    if event.type in {"charging.started", "charging.stopped"}:
        return ", ".join(data.get("powered_by", ())) or ""
    if event.type == "wifi.connected":
        return str(data.get("ssid", ""))
    if event.type == "wifi.disconnected":
        return f"was {data.get('previous')}"
    if event.type in {"device.connected", "device.disconnected", "device.authorized", "device.unauthorized"}:
        return str(data.get("address", ""))
    if event.type == "observer.started":
        return f"sources: {', '.join(data.get('sources', ()))}; every {data.get('interval'):g}s"
    return ""


def render(event: Event) -> str:
    clock = event.ts[11:19] if len(event.ts) >= 19 else event.ts
    initial = "  (initial)" if event.initial else ""
    return f"{clock}  {event.type:<20} {event.device:<18} {summarize(event)}{initial}"


def _emit(arguments: Any, event: Event) -> None:
    line = json.dumps(event.to_dict(), sort_keys=True) if arguments.json else render(event)
    print(line, flush=True)


def _wanted(arguments: Any, event: Event) -> bool:
    types = set(getattr(arguments, "types", None) or ())
    devices = set(getattr(arguments, "devices", None) or ())
    if types and event.type not in types:
        return False
    if devices and event.device not in devices and event.device != "*":
        return False
    return True


def follow(arguments: Any) -> int:
    """Subscribe to a running daemon; the daemon owns history, so nothing is written here."""
    with cancel_on_interrupt(CancellationToken()) as cancel:
        for event in daemond.subscribe(
            getattr(arguments, "socket", None),
            types=tuple(getattr(arguments, "types", None) or ()),
            devices=tuple(getattr(arguments, "devices", None) or ()),
            cancel=cancel,
        ):
            _emit(arguments, event)
    if not arguments.json:
        print("Stopped.", file=sys.stderr)
    return 0


def run(arguments: Any, *, identities: dict[str, str] | None = None, observer_factory=None) -> int:
    if getattr(arguments, "follow", False):
        if daemond.is_running(getattr(arguments, "socket", None)):
            return follow(arguments)
        print("no daemon running; observing in this process instead", file=sys.stderr, flush=True)
    # Resolved at call time (not as a default argument) so tests can patch `Observer`.
    make_observer = observer_factory or Observer
    history = None if arguments.no_history else HistoryWriter(getattr(arguments, "history", None))

    def sink(event: Event) -> None:
        stamped = history.append(event) if history is not None else event
        if _wanted(arguments, stamped):
            _emit(arguments, stamped)

    try:
        with cancel_on_interrupt(CancellationToken()) as cancel:
            observer = make_observer(
                sink,
                identities=identities,
                interval=arguments.interval,
                low_battery=arguments.low_battery,
                cancel=cancel,
                poll_wifi_enabled=not arguments.no_wifi,
            )
            observer.run()
    finally:
        if history is not None:
            history.close()
    if not arguments.json:
        print("Stopped.", file=sys.stderr)
    return 0
