from __future__ import annotations

import json
import signal
import sys
import threading
from typing import Any

from linkplane import daemon as daemond
from linkplane import service
from linkplane.commands import print_envelope
from linkplane.operations import CancellationToken, cancel_on_interrupt


def run(arguments: Any, *, identities: dict[str, str] | None = None) -> int:
    action = arguments.daemon_action
    if action == "run":
        instance = daemond.Daemon(
            socket_path=arguments.socket,
            state_path=arguments.state,
            history_path=arguments.history,
            identities=identities,
            interval=arguments.interval,
            low_battery=arguments.low_battery,
            poll_wifi=not arguments.no_wifi,
            write_history=not arguments.no_history,
            automations_path=arguments.automations,
            audit_path=arguments.audit,
            config_path=arguments.config,
            clients_path=arguments.clients,
            api_enabled=not arguments.no_api,
            api_bind=arguments.api_bind,
            api_port=arguments.api_port,
            api_origins=tuple(arguments.api_origins or ()),
        )
        # systemd (and `kill`) stop with SIGTERM: route it to the same cooperative stop
        # as Ctrl+C so the socket is removed and the snapshot marked stopped.
        previous_term = None
        if threading.current_thread() is threading.main_thread():
            previous_term = signal.signal(
                signal.SIGTERM, lambda _signum, _frame: instance.stop("SIGTERM")
            )
        try:
            with cancel_on_interrupt(instance.cancel):
                print(f"linkplaned listening on {instance.socket_path}", file=sys.stderr, flush=True)
                instance.run()
        finally:
            if previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)
        print(f"linkplaned stopped: {instance.stop_reason or 'observer exited'}", file=sys.stderr)
        return 0
    if action == "status":
        info = daemond.request("status", arguments.socket)
        info.pop("ok", None)
        if arguments.json:
            print_envelope(True, info)
            return 0
        print("Linkplane Daemon")
        print(f"Running     pid {info['pid']} since {info['started']}")
        if info.get("version"):
            print(f"Version     {info['version']}")
        print(f"Socket      {info['socket']}")
        print(f"State file  {info['state_file']}")
        print(f"Events      {info['events_seen']} seen, {info['subscribers']} subscriber(s), polling every {info['interval']:g}s")
        api = info.get("api")
        if api:
            print(f"API         {api['url']} ({api.get('streams', 0)} stream(s))")
        else:
            print("API         not running")
        rules = info.get("automations") or {}
        if rules:
            blocked = rules.get("blocked") or {}
            print(f"Automations {rules.get('loaded', 0)} active, {len(blocked)} blocked, {rules.get('fired', 0)} fired")
        devices = info.get("devices") or {}
        if not devices:
            print("Devices     none observed")
        for name, state in devices.items():
            battery = state.get("battery") or {}
            level = f"{battery['level']}%" if battery.get("level") is not None else "-"
            wifi = state.get("wifi_ssid") or "-"
            print(f"Device      {name}: {state['connection']} via {state.get('provider') or '-'}; battery {level}; wifi {wifi}")
        return 0
    if action == "reload":
        info = daemond.request("reload", arguments.socket)
        info.pop("ok", None)
        if arguments.json:
            print_envelope(True, info)
        else:
            print(f"Reloaded    {info['loaded']} automation(s) from {info['path']}")
            for name, blocked in (info.get("blocked") or {}).items():
                print(f"Blocked     {name}: needs \"allow\" for {', '.join(blocked)}")
        return 0
    if action == "stop":
        daemond.request("stop", arguments.socket)
        if arguments.json:
            print_envelope(True, {"stopping": True})
        else:
            print("linkplaned is stopping")
        return 0
    if action in {"install", "uninstall"}:
        if action == "install":
            extra = []
            if arguments.interval is not None:
                extra += ["--interval", str(arguments.interval)]
            if arguments.no_wifi:
                extra.append("--no-wifi")
            if arguments.api_port is not None:
                extra += ["--api-port", str(arguments.api_port)]
            if arguments.no_api:
                extra.append("--no-api")
            result = service.install(
                unit_dir=arguments.unit_dir, extra_arguments=tuple(extra), dry_run=arguments.dry_run
            )
        else:
            result = service.uninstall(unit_dir=arguments.unit_dir, dry_run=arguments.dry_run)
        if arguments.json:
            print_envelope(True, result.to_dict())
            return 0
        verb = "Would install" if arguments.dry_run else "Installed"
        if action == "uninstall":
            verb = "Would remove" if arguments.dry_run else "Removed"
        print("Linkplane Daemon")
        print(f"{verb:<14}{result.unit_path}")
        for command in result.commands:
            print(f"{'Command':<14}{' '.join(command)}")
        if arguments.dry_run and result.unit_text:
            print()
            print(result.unit_text.rstrip())
        elif action == "install":
            print("Check         linkplane daemon status")
        return 0
    raise AssertionError(f"unknown daemon action {action}")
