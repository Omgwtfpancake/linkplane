from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

from linkplane import __version__
from linkplane.audio import AUDIO_CODECS, AUDIO_RECORD_FORMATS, AUDIO_SOURCES, audio
from linkplane.backup import DEFAULT_DESTINATION as DEFAULT_BACKUP_DESTINATION
from linkplane.backup import DEFAULT_SOURCE as DEFAULT_BACKUP_SOURCE
from linkplane.backup import backup
from linkplane.camera import CAMERA_PRESETS, capture_photo, preview_camera
from linkplane.clipboard import clipboard
from linkplane.commands import automations as automations_command
from linkplane.commands import battery as battery_command
from linkplane.commands import capabilities as capabilities_command
from linkplane.commands import daemon as daemon_command
from linkplane.commands import events as events_command
from linkplane.commands import ping as ping_command
from linkplane.commands import watch as watch_command
from linkplane.core import errors as core_errors
from linkplane.devices import discover_devices
from linkplane.doctor import DoctorRequest, run_diagnostics
from linkplane.find import find_phone
from linkplane.models import Check, Device, DiscoveryIssue, DiscoveryResult
from linkplane.notification import notify
from linkplane.operations import JSON_SCHEMA_VERSION, OperationCancelled
from linkplane import registry
from linkplane.commands import clients as clients_command
from linkplane.commands import setup as setup_command
from linkplane.commands import uninstall as uninstall_command
from linkplane.providers import Provider, select_provider
from linkplane.profiles import (
    available_adb_serial,
    list_profiles,
    pair_ssh,
    pair_usb,
    pair_wireless,
    profiles_from_config,
    refresh_profiles,
    remove_profile,
    selected_profile,
    set_default_profile,
)
from linkplane.screen import SCREEN_PRESETS, screen
from linkplane.status import StatusRequest, read_status
from linkplane.transfer import DEFAULT_DESTINATION, send
from linkplane.transports import (
    AdbTransport,
    BridgeError,
    SshTransport,
    load_config,
    ssh_from_config,
)
from linkplane.webcam import webcam_start, webcam_stop


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unavailable"
    days, remaining = divmod(seconds, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes = remaining // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def print_status(status: dict[str, Any]) -> None:
    device = status["device"]
    battery = status["battery"]
    memory = status["memory"]
    storage = status["storage"]

    print("Linkplane Status")
    print(f"Connection  {status['transport'].upper()}")
    manufacturer = device.get("manufacturer") or "Unknown"
    model = device.get("model") or "device"
    android = device.get("android") or "unavailable"
    print(f"Device      {manufacturer} {model} (Android {android})")
    if device.get("serial"):
        print(f"Serial      {device['serial']}")
    if battery is None:
        print("Battery     unavailable")
    else:
        power = ", ".join(battery["powered_by"])
        power_text = f" via {power}" if power else ""
        temperature = battery.get("temperature_c")
        temperature_text = f", {temperature:g} C" if temperature is not None else ""
        print(
            f"Battery     {battery['level']}%, {battery['status']}"
            f"{power_text}{temperature_text}"
        )
    if memory is None:
        print("Memory      unavailable")
    else:
        print(
            f"Memory      {format_bytes(memory['available_bytes'])} available of "
            f"{format_bytes(memory['total_bytes'])} ({memory['used_percent']}% used)"
        )
    if storage is None:
        print("Storage     unavailable")
    else:
        print(
            f"Storage     {format_bytes(storage['available_bytes'])} available of "
            f"{format_bytes(storage['total_bytes'])} ({storage['used_percent']}% used)"
        )
    print(f"Uptime      {format_duration(status['uptime_seconds'])}")
    for issue in status.get("issues", ()):
        print(f"Warning     {issue['component']}: {issue['error']}")


def print_backup_status(arguments: argparse.Namespace, *, jobs_dir: str | None = None, rules_path: str | None = None) -> None:
    """Automatic photo backup lines under `status`, derived from the rules file and job
    records. Best effort: status never fails because of them, and prints nothing when the
    preset was never set up for this phone."""
    from linkplane import presets

    try:
        name = getattr(arguments, "device_profile", None) or load_config(getattr(arguments, "config", None)).get("default_device")
        rules = presets.preset_rules(rules_path)
    except (BridgeError, OSError, ValueError):
        return
    rule = next((rule for rule in rules if rule.get("device") == name), None)
    if rule is None and len(rules) == 1 and name is None:
        rule = rules[0]
    if rule is None:
        return
    if not rule.get("enabled", True):
        print("Backup      automatic photo backup off")
        return
    print(f"Backup      automatic photo backup on, to {presets.rule_destination(rule)}")
    try:
        record = presets.last_run(rule["name"], jobs_dir=jobs_dir)
    except OSError:
        return
    print(f"Last backup {presets.describe_run(record)}")


def add_ssh_options(parser: argparse.ArgumentParser, *, include_device_id: bool = False) -> None:
    parser.add_argument("--config", help="configuration file path")
    parser.add_argument("--ssh-host", help="Termux SSH host")
    parser.add_argument("--ssh-user", help="Termux SSH user")
    parser.add_argument("--ssh-port", type=int, help="Termux SSH port")
    parser.add_argument("--ssh-key", help="Termux SSH private key")
    if include_device_id:
        parser.add_argument("--device-id", help="logical device ID for this SSH endpoint")


# Exit status for a run that stopped because it was cancelled (Ctrl+C or a cancellation
# token), mirroring the shell convention of 128 + SIGINT.
EXIT_CANCELLED = 130
# Exit status when the reader of our stdout went away (`linkplane events | head`):
# 128 + SIGPIPE, and no traceback, as a script-friendly tool behaves.
EXIT_BROKEN_PIPE = 141

# Core v0.1 commands that talk to the phone purely through a `Provider`.
CORE_COMMANDS = {
    "battery": battery_command,
    "ping": ping_command,
    "capabilities": capabilities_command,
}
# Commands whose `--transport auto` may fall back from ADB to a profile's SSH endpoint.
TRANSPORT_FALLBACK_COMMANDS = {"status", "notify", *CORE_COMMANDS}


def add_core_command(subparsers: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--transport",
        choices=("auto", "adb", "ssh"),
        default="auto",
        help="connection method (default: auto, preferring ADB)",
    )
    parser.add_argument("--serial", help="ADB device serial")
    add_ssh_options(parser)
    add_device_profile_option(parser)
    return parser


def add_device_profile_option(
    parser: argparse.ArgumentParser, *, include_config: bool = False
) -> None:
    parser.add_argument("--device", dest="device_profile", help="named device profile")
    if include_config:
        parser.add_argument("--config", help="configuration file path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linkplane", description="Connect Android and Linux")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log diagnostic detail to stderr and show full tracebacks",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show connected phone status")
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    status.add_argument(
        "--transport",
        choices=("auto", "adb", "ssh"),
        default="auto",
        help="connection method (default: auto, preferring ADB)",
    )
    status.add_argument("--serial", help="ADB device serial")
    add_ssh_options(status)
    add_device_profile_option(status)
    screen_parser = subparsers.add_parser("screen", help="mirror and control the phone")
    screen_parser.add_argument(
        "--quality",
        choices=tuple(SCREEN_PRESETS),
        default="balanced",
        help="screen preset (default: balanced)",
    )
    screen_parser.add_argument("--serial", help="ADB device serial")
    add_device_profile_option(screen_parser, include_config=True)
    screen_parser.add_argument("--no-audio", action="store_true", help="disable audio forwarding")
    screen_parser.add_argument("--record", metavar="FILE", help="record the session")
    screen_parser.add_argument(
        "--install",
        action="store_true",
        help="install scrcpy without asking if it is missing",
    )
    screen_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the scrcpy command without launching it",
    )
    screen_parser.add_argument(
        "scrcpy_args",
        nargs=argparse.REMAINDER,
        metavar="SCRCPY_ARG",
        help="arguments after -- are passed directly to scrcpy",
    )
    audio_parser = subparsers.add_parser(
        "audio", help="forward or record phone audio without mirroring video"
    )
    audio_parser.add_argument(
        "--source",
        choices=AUDIO_SOURCES,
        default="playback",
        help="audio source (default: playback)",
    )
    audio_parser.add_argument("--serial", help="ADB device serial")
    add_device_profile_option(audio_parser, include_config=True)
    audio_parser.add_argument("--record", metavar="FILE", help="record the session")
    audio_parser.add_argument(
        "--record-format",
        choices=AUDIO_RECORD_FORMATS,
        help="force the recording format (default: inferred from --record's extension)",
    )
    audio_parser.add_argument(
        "--codec",
        choices=AUDIO_CODECS,
        help="audio codec (default: opus)",
    )
    audio_parser.add_argument(
        "--install",
        action="store_true",
        help="install scrcpy without asking if it is missing",
    )
    audio_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the scrcpy command without launching it",
    )
    audio_parser.add_argument(
        "scrcpy_args",
        nargs=argparse.REMAINDER,
        metavar="SCRCPY_ARG",
        help="arguments after -- are passed directly to scrcpy",
    )
    send_parser = subparsers.add_parser("send", help="send files or directories to the phone")
    send_parser.add_argument("paths", nargs="+", help="files or directories to send")
    send_parser.add_argument(
        "--transport",
        choices=("auto", "adb", "localsend"),
        default="auto",
        help="transfer method (default: auto, preferring ADB)",
    )
    send_parser.add_argument(
        "--destination",
        default=DEFAULT_DESTINATION,
        help=f"Android destination for ADB (default: {DEFAULT_DESTINATION})",
    )
    send_parser.add_argument("--serial", help="ADB device serial")
    add_device_profile_option(send_parser, include_config=True)
    send_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show transfer commands without sending files",
    )
    backup_parser = subparsers.add_parser(
        "backup", help="incrementally back up phone photos"
    )
    backup_parser.add_argument(
        "destination",
        nargs="?",
        default=DEFAULT_BACKUP_DESTINATION,
        help=f"local backup directory (default: {DEFAULT_BACKUP_DESTINATION})",
    )
    backup_parser.add_argument(
        "--source",
        default=DEFAULT_BACKUP_SOURCE,
        help=f"Android source directory (default: {DEFAULT_BACKUP_SOURCE})",
    )
    backup_parser.add_argument("--serial", help="ADB device serial")
    add_device_profile_option(backup_parser, include_config=True)
    backup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show files requiring backup without downloading them",
    )
    camera_parser = subparsers.add_parser(
        "camera", help="capture photos or preview the phone camera"
    )
    camera_actions = camera_parser.add_subparsers(dest="camera_action", required=True)
    capture_parser = camera_actions.add_parser(
        "capture", help="capture a JPEG through Termux"
    )
    capture_parser.add_argument("output", nargs="?", help="local JPEG output path")
    capture_parser.add_argument(
        "--camera-id",
        type=int,
        default=0,
        help="Termux camera ID (default: 0)",
    )
    capture_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    capture_parser.add_argument(
        "--no-foreground",
        action="store_true",
        help="do not bring Termux forward through ADB",
    )
    capture_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the capture command without using the camera",
    )
    add_ssh_options(capture_parser)
    add_device_profile_option(capture_parser)
    preview_parser = camera_actions.add_parser(
        "preview", help="mirror the phone camera with scrcpy"
    )
    preview_selection = preview_parser.add_mutually_exclusive_group()
    preview_selection.add_argument(
        "--facing",
        choices=("back", "front", "external"),
        help="camera direction (default: back)",
    )
    preview_selection.add_argument(
        "--camera-id",
        dest="preview_camera_id",
        help="explicit scrcpy camera ID",
    )
    preview_parser.add_argument(
        "--quality",
        choices=tuple(CAMERA_PRESETS),
        default="balanced",
        help="camera preset (default: balanced)",
    )
    preview_parser.add_argument("--serial", help="ADB device serial")
    add_device_profile_option(preview_parser, include_config=True)
    preview_parser.add_argument("--no-audio", action="store_true")
    preview_parser.add_argument("--torch", action="store_true")
    preview_parser.add_argument("--record", metavar="FILE", help="record the preview")
    preview_parser.add_argument(
        "--install",
        action="store_true",
        help="install scrcpy without asking if it is missing",
    )
    preview_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the scrcpy command without opening the camera",
    )
    preview_parser.add_argument(
        "scrcpy_args",
        nargs=argparse.REMAINDER,
        metavar="SCRCPY_ARG",
        help="arguments after -- are passed directly to scrcpy",
    )
    webcam_parser = subparsers.add_parser(
        "webcam", help="stream the phone camera to a Linux V4L2 webcam device"
    )
    webcam_actions = webcam_parser.add_subparsers(dest="webcam_action", required=True)
    webcam_start_parser = webcam_actions.add_parser(
        "start", help="start a v4l2loopback webcam sink fed by the phone camera"
    )
    webcam_start_parser.add_argument("--serial", help="ADB device serial")
    webcam_start_parser.add_argument(
        "--device",
        type=int,
        metavar="N",
        help="V4L2 sink device number (/dev/videoN); auto-detected via v4l2-ctl if omitted",
    )
    webcam_start_parser.add_argument(
        "--install",
        action="store_true",
        help="install missing scrcpy or v4l2loopback packages without asking",
    )
    webcam_start_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the scrcpy command without starting the webcam sink",
    )
    webcam_start_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    webcam_stop_parser = webcam_actions.add_parser(
        "stop", help="stop the running webcam sink"
    )
    webcam_stop_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be stopped without stopping it",
    )
    webcam_stop_parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    notify_parser = subparsers.add_parser("notify", help="show a notification on the phone")
    notify_parser.add_argument("message", help="notification text")
    notify_parser.add_argument("--title", default="Linkplane", help="notification title")
    notify_parser.add_argument(
        "--id",
        dest="notification_id",
        type=int,
        default=8765,
        help="stable notification ID (default: 8765)",
    )
    notify_parser.add_argument(
        "--transport",
        choices=("auto", "adb", "ssh"),
        default="auto",
        help="connection method (default: auto, preferring ADB)",
    )
    notify_parser.add_argument("--serial", help="ADB device serial")
    add_ssh_options(notify_parser)
    add_device_profile_option(notify_parser)
    notify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the notification command without sending it",
    )
    find_parser = subparsers.add_parser(
        "find", help="locate the phone with ring volume, vibration, and camera torch"
    )
    find_parser.add_argument("--serial", help="ADB device serial")
    find_parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="seconds to ring/vibrate/flash (default: 5)",
    )
    find_parser.add_argument(
        "--message", default="Locate this phone", help="notification text"
    )
    find_parser.add_argument(
        "--no-ring", dest="ring", action="store_false", help="do not raise ring volume"
    )
    find_parser.add_argument(
        "--no-torch", dest="torch", action="store_false", help="do not flash the camera torch"
    )
    find_parser.add_argument(
        "--no-vibrate", dest="vibrate", action="store_false", help="do not vibrate the phone"
    )
    find_parser.add_argument(
        "--install", action="store_true", help="install scrcpy if needed for the torch"
    )
    find_parser.add_argument("--dry-run", action="store_true")
    find_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    add_device_profile_option(find_parser)
    add_core_command(subparsers, "battery", "show phone battery level and charging state")
    add_core_command(subparsers, "ping", "check that the phone answers")
    add_core_command(subparsers, "capabilities", "list what the phone can do right now")
    events_parser = subparsers.add_parser(
        "events", help="stream device events (connection, battery, charging, Wi-Fi)"
    )
    events_parser.add_argument("--json", action="store_true", help="one JSON object per line")
    events_parser.add_argument(
        "--interval", type=float, default=30.0, help="battery/Wi-Fi poll interval in seconds (default: 30)"
    )
    events_parser.add_argument(
        "--low-battery", type=int, default=20, help="battery.low threshold percent (default: 20)"
    )
    events_parser.add_argument("--no-wifi", action="store_true", help="do not poll Wi-Fi state")
    events_parser.add_argument(
        "--no-history", action="store_true", help="do not append events to the local history"
    )
    events_parser.add_argument("--history", help="history file (default: $XDG_STATE_HOME/linkplane/events.jsonl)")
    events_parser.add_argument("--config", help="configuration file path")
    events_parser.add_argument(
        "--follow", action="store_true",
        help="subscribe to the running daemon instead of observing in this process",
    )
    events_parser.add_argument("--socket", help="daemon control socket (default: $XDG_RUNTIME_DIR/linkplane/daemon.sock)")
    events_parser.add_argument("--type", dest="types", action="append", metavar="TYPE", help="only these event types (repeatable)")
    events_parser.add_argument("--device", dest="devices", action="append", metavar="NAME", help="only these devices (repeatable)")

    watch_parser = subparsers.add_parser("watch", help="react to one kind of device event (notify, run a script)")
    watch_parser.add_argument("event", help="event type, e.g. battery.low, device.connected, wifi.connected")
    watch_parser.add_argument("--if", dest="conditions", action="append", metavar="COND",
                              help="condition over the event data: level<20, ssid=Home, status!=full, powered_by in usb,ac (repeatable)")
    watch_parser.add_argument("--device", dest="devices", action="append", metavar="NAME", help="only this device (profile name or serial)")
    watch_parser.add_argument("--notify", action="append", metavar="MESSAGE", help="desktop notification; {level}, {ssid}, {device} placeholders")
    watch_parser.add_argument("--urgency", choices=("low", "normal", "critical"), default="normal", help="desktop notification urgency")
    watch_parser.add_argument("--notify-phone", action="append", metavar="MESSAGE", help="notification on the phone")
    watch_parser.add_argument("--run", action="append", metavar="COMMAND", help="run a command (receives LINKPLANE_EVENT/DEVICE/DATA in its environment)")
    watch_parser.add_argument("--backup", action="append", metavar="DIR", help="run a verified photo backup into DIR (job-tracked)")
    watch_parser.add_argument("--cooldown", type=float, default=0.0, help="seconds to ignore repeat triggers (default: 0)")
    watch_parser.add_argument("--on-initial", action="store_true", help="also fire on the opening observation, not only on changes")
    watch_parser.add_argument("--interval", type=float, default=30.0, help="poll interval when observing in-process (default: 30)")
    watch_parser.add_argument("--low-battery", type=int, default=20, help="battery.low threshold when observing in-process")
    watch_parser.add_argument("--no-wifi", action="store_true", help="do not poll Wi-Fi when observing in-process")
    watch_parser.add_argument("--socket", help="daemon control socket")
    watch_parser.add_argument("--config", help="configuration file path")

    daemon_parser = subparsers.add_parser("daemon", help="run or control the background linkplaned process")
    daemon_actions = daemon_parser.add_subparsers(dest="daemon_action", required=True)
    daemon_run = daemon_actions.add_parser("run", help="run linkplaned in the foreground")
    daemon_run.add_argument("--interval", type=float, default=30.0, help="battery/Wi-Fi poll interval in seconds (default: 30)")
    daemon_run.add_argument("--low-battery", type=int, default=20, help="battery.low threshold percent (default: 20)")
    daemon_run.add_argument("--no-wifi", action="store_true", help="do not poll Wi-Fi state")
    daemon_run.add_argument("--no-history", action="store_true", help="do not append events to the local history")
    daemon_run.add_argument("--history", help="history file")
    daemon_run.add_argument("--socket", help="control socket path")
    daemon_run.add_argument("--state", help="registry snapshot file (default: $XDG_STATE_HOME/linkplane/state.json)")
    daemon_run.add_argument("--config", help="configuration file path")
    daemon_run.add_argument("--automations", help="rules file (default: ~/.config/linkplane/automations.json)")
    daemon_run.add_argument("--audit", help="audit log (default: $XDG_STATE_HOME/linkplane/audit.jsonl)")
    daemon_run.add_argument("--clients", help="API clients file (default: ~/.config/linkplane/clients.json)")
    daemon_run.add_argument("--api-bind", default="127.0.0.1", help="local API address; loopback only (default: 127.0.0.1)")
    daemon_run.add_argument("--api-port", type=int, default=8741, help="local API port (default: 8741; 0 = any free port)")
    daemon_run.add_argument("--api-origin", dest="api_origins", action="append", metavar="ORIGIN", help="browser origin allowed to call the API (repeatable; none by default)")
    daemon_run.add_argument("--no-api", action="store_true", help="do not start the local HTTP API")

    clients_parser = subparsers.add_parser("clients", help="manage local API clients and their tokens")
    clients_actions = clients_parser.add_subparsers(dest="clients_action", required=True)
    clients_create = clients_actions.add_parser("create", help="create a client and print its token once")
    clients_create.add_argument("client_id", help="short id: lowercase letters, digits, dashes")
    clients_create.add_argument("--name", help="human-readable name")
    clients_create.add_argument("--type", dest="client_type", default="tool", choices=clients_command.CLIENT_TYPES)
    clients_create.add_argument("--scope", dest="scopes", action="append", metavar="SCOPE",
                                help="scope to grant (repeatable); 'read' expands to the read set; default: read")
    clients_create.add_argument("--token-file", action="store_true",
                                help="also write the token to clients/<id>.token (mode 0600)")
    clients_create.add_argument("--file", help="clients file path")
    clients_create.add_argument("--json", action="store_true")
    clients_list = clients_actions.add_parser("list", help="list clients (never shows tokens)")
    clients_list.add_argument("--file", help="clients file path")
    clients_list.add_argument("--json", action="store_true")
    clients_revoke = clients_actions.add_parser("revoke", help="revoke a client by id")
    clients_revoke.add_argument("client_id")
    clients_revoke.add_argument("--file", help="clients file path")
    clients_revoke.add_argument("--json", action="store_true")
    automations_parser = subparsers.add_parser("automations", help="list rules and read the audit log")
    automations_actions = automations_parser.add_subparsers(dest="automations_action", required=True)
    automations_list = automations_actions.add_parser("list", help="show the rules in automations.json")
    automations_list.add_argument("--file", help="rules file")
    automations_list.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    automations_jobs = automations_actions.add_parser("jobs", help="show recent job-tracked actions (backup, send, clipboard-sync)")
    automations_jobs.add_argument("--dir", help="job records directory")
    automations_jobs.add_argument("-n", "--lines", type=int, default=20, help="how many recent jobs (default: 20)")
    automations_jobs.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    automations_presets = automations_actions.add_parser("presets", help="show built-in presets (automatic photo backup) and whether each is on")
    automations_presets.add_argument("--file", help="rules file")
    automations_presets.add_argument("--config", help="configuration file path")
    automations_presets.add_argument("--jobs-dir", help="job records directory")
    automations_presets.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    for verb, help_text in (("enable", "turn a preset on for a device (writes an ordinary rule)"),
                            ("disable", "turn a preset off for a device (keeps the rule and every backed-up file)")):
        preset_parser = automations_actions.add_parser(verb, help=help_text)
        preset_parser.add_argument("preset", help="preset name, e.g. photo-backup")
        preset_parser.add_argument("--device", help="device profile (default: the default device)")
        if verb == "enable":
            preset_parser.add_argument("--destination", help="backup folder (default: the rule's current folder, else ~/Pictures/Linkplane)")
        preset_parser.add_argument("--file", help="rules file")
        preset_parser.add_argument("--config", help="configuration file path")
        preset_parser.add_argument("--socket", help="daemon control socket to ask for a reload")
        preset_parser.add_argument("--dry-run", action="store_true", help="show the change without writing it")
        preset_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    automations_log = automations_actions.add_parser("log", help="show the daemon's audit log")
    automations_log.add_argument("--file", help="audit log file")
    automations_log.add_argument("-n", "--lines", type=int, default=50, help="how many recent lines (default: 50)")
    automations_log.add_argument("--follow", action="store_true", help="keep printing new lines")
    automations_log.add_argument("--json", action="store_true", help="one JSON object per line")
    for name, help_text in (("status", "show the running daemon"), ("stop", "ask the running daemon to stop"),
                            ("reload", "re-read automations.json without restarting")):
        action_parser = daemon_actions.add_parser(name, help=help_text)
        action_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
        action_parser.add_argument("--socket", help="control socket path")
    daemon_install = daemon_actions.add_parser("install", help="install and start linkplaned as a systemd user service")
    daemon_install.add_argument("--interval", type=float, help="poll interval to bake into the unit")
    daemon_install.add_argument("--no-wifi", action="store_true", help="bake --no-wifi into the unit")
    daemon_install.add_argument("--api-port", type=int, help="local API port to bake into the unit")
    daemon_install.add_argument("--no-api", action="store_true", help="bake --no-api into the unit")
    daemon_uninstall = daemon_actions.add_parser("uninstall", help="stop and remove the systemd user service")
    for action_parser in (daemon_install, daemon_uninstall):
        action_parser.add_argument("--unit-dir", help="systemd user unit directory (default: ~/.config/systemd/user)")
        action_parser.add_argument("--dry-run", action="store_true", help="show the unit and commands without changing anything")
        action_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    devices_parser = subparsers.add_parser("devices", help="discover connected phones")
    devices_parser.add_argument(
        "--observed", action="store_true",
        help="answer from the daemon's last snapshot instead of probing (see `daemon run`)",
    )
    devices_parser.add_argument("--state", help="snapshot file (default: $XDG_STATE_HOME/linkplane/state.json)")
    devices_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    add_ssh_options(devices_parser, include_device_id=True)
    add_device_profile_option(devices_parser)
    doctor_parser = subparsers.add_parser("doctor", help="check Linkplane readiness")
    doctor_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    doctor_parser.add_argument(
        "--transport",
        choices=("auto", "adb", "ssh"),
        default="auto",
        help="provider to diagnose (default: auto, preferring ADB)",
    )
    add_ssh_options(doctor_parser, include_device_id=True)
    add_device_profile_option(doctor_parser)
    setup_parser = subparsers.add_parser("setup", help="guided first-run setup: connect a phone, register it, start the daemon")
    setup_parser.add_argument("--name", help="profile name for the phone (default: ask; 'phone' or the next free name)")
    setup_parser.add_argument("--serial", help="ADB serial to set up when more than one phone is connected")
    setup_parser.add_argument("--config", help="configuration file (default: ~/.config/linkplane/config.json)")
    setup_parser.add_argument("--no-daemon", action="store_true", help="do not install or start the background service")
    setup_parser.add_argument("--api-client", metavar="ID", help="also create a read-only local API client with this id and print its token once")
    setup_parser.add_argument("--dry-run", action="store_true", help="run every check but write nothing")
    setup_parser.add_argument("--non-interactive", action="store_true", help="never prompt: use defaults, never install packages, fail instead of asking")
    setup_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    setup_parser.add_argument("--unit-dir", help=argparse.SUPPRESS)
    setup_parser.add_argument("--socket", help=argparse.SUPPRESS)
    setup_parser.add_argument("--api-port", type=int, help="local API port to bake into the service unit")
    setup_parser.add_argument("--no-api", action="store_true", help="bake --no-api into the service unit")
    uninstall_parser = subparsers.add_parser("uninstall", help="stop and remove the Linkplane service integration (keeps your configuration; --purge removes it)")
    uninstall_parser.add_argument("--purge", action="store_true", help="also delete Linkplane's own configuration and state directories (never backups or your files)")
    uninstall_parser.add_argument("--force", action="store_true", help="with --purge: delete the listed Linkplane-owned directories without asking")
    uninstall_parser.add_argument("--dry-run", action="store_true", help="show what would be stopped and removed without changing anything")
    uninstall_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    uninstall_parser.add_argument("--unit-dir", help=argparse.SUPPRESS)
    uninstall_parser.add_argument("--socket", help=argparse.SUPPRESS)
    clipboard_parser = subparsers.add_parser(
        "clipboard", help="move text between phone and desktop clipboards"
    )
    clipboard_parser.add_argument(
        "action", choices=("get", "set", "pull", "push", "sync")
    )
    clipboard_parser.add_argument("text", nargs="?", help="text for the set action")
    clipboard_parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="sync polling interval in seconds (default: 1)",
    )
    clipboard_parser.add_argument(
        "--prefer",
        choices=("desktop", "phone"),
        default="desktop",
        help="side to keep when both clipboards change (default: desktop)",
    )
    clipboard_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and describe the operation without accessing clipboard contents",
    )
    clipboard_parser.add_argument(
        "--no-foreground",
        action="store_true",
        help="do not bring Termux forward through ADB",
    )
    add_ssh_options(clipboard_parser)
    add_device_profile_option(clipboard_parser)
    profiles_parser = subparsers.add_parser(
        "profiles", help="manage named device profiles"
    )
    profile_actions = profiles_parser.add_subparsers(
        dest="profile_action", required=True
    )
    profile_list = profile_actions.add_parser("list", help="list device profiles")
    profile_list.add_argument("--json", action="store_true")
    profile_list.add_argument("--config", help="configuration file path")
    profile_remove = profile_actions.add_parser("remove", help="remove a device profile")
    profile_remove.add_argument("name")
    profile_remove.add_argument("--config", help="configuration file path")
    profile_default = profile_actions.add_parser(
        "default", help="select the default device profile"
    )
    profile_default.add_argument("name")
    profile_default.add_argument("--config", help="configuration file path")
    profile_refresh = profile_actions.add_parser(
        "refresh", help="refresh drifted wireless endpoints from a reachable ADB link"
    )
    profile_refresh.add_argument(
        "name", nargs="?", help="profile to refresh (default: all profiles)"
    )
    profile_refresh.add_argument(
        "--scan",
        action="store_true",
        help=(
            "when no known alias is reachable, sweep the last-known wireless subnet "
            "for a profile with a recorded USB serial (identity is verified against "
            "that serial before anything is changed; can take up to ~15s)"
        ),
    )
    profile_refresh.add_argument("--dry-run", action="store_true")
    profile_refresh.add_argument("--json", action="store_true")
    profile_refresh.add_argument("--config", help="configuration file path")
    pair_parser = subparsers.add_parser("pair", help="pair and save a device profile")
    pair_actions = pair_parser.add_subparsers(dest="pair_action", required=True)
    pair_usb_parser = pair_actions.add_parser(
        "usb", help="save an authorized USB ADB device"
    )
    pair_usb_parser.add_argument("name", help="new or existing profile name")
    pair_usb_parser.add_argument("--serial", help="ADB device serial")
    pair_usb_parser.add_argument("--default", action="store_true")
    pair_usb_parser.add_argument("--dry-run", action="store_true")
    pair_usb_parser.add_argument("--config", help="configuration file path")
    pair_wireless_parser = pair_actions.add_parser(
        "wireless", help="pair Android wireless debugging"
    )
    pair_wireless_parser.add_argument("name", help="new or existing profile name")
    pair_wireless_parser.add_argument("pair_address", metavar="PAIR_HOST:PORT")
    pair_wireless_parser.add_argument(
        "--connect", dest="connect_address", required=True, metavar="HOST:PORT"
    )
    pair_wireless_parser.add_argument("--default", action="store_true")
    pair_wireless_parser.add_argument("--dry-run", action="store_true")
    pair_wireless_parser.add_argument("--config", help="configuration file path")
    pair_ssh_parser = pair_actions.add_parser(
        "ssh", help="verify and save a Termux SSH endpoint"
    )
    pair_ssh_parser.add_argument("name", help="new or existing profile name")
    pair_ssh_parser.add_argument("--ssh-host", required=True)
    pair_ssh_parser.add_argument("--ssh-user", required=True)
    pair_ssh_parser.add_argument("--ssh-port", type=int, default=8022)
    pair_ssh_parser.add_argument("--ssh-key")
    pair_identity = pair_ssh_parser.add_mutually_exclusive_group()
    pair_identity.add_argument("--serial", help="associated ADB serial")
    pair_identity.add_argument("--device-id", help="explicit logical device ID")
    pair_ssh_parser.add_argument("--default", action="store_true")
    pair_ssh_parser.add_argument("--dry-run", action="store_true")
    pair_ssh_parser.add_argument("--config", help="configuration file path")
    return parser


def apply_device_profile(arguments: argparse.Namespace) -> None:
    if not hasattr(arguments, "device_profile"):
        return
    config = load_config(getattr(arguments, "config", None))
    requested_name = arguments.device_profile
    transport = getattr(arguments, "transport", None)
    ssh_relevant = (
        arguments.command in {"clipboard", "devices", "doctor"}
        or (arguments.command in TRANSPORT_FALLBACK_COMMANDS and transport != "adb")
        or (arguments.command == "camera" and arguments.camera_action == "capture")
    )
    endpoint_overridden = any(
        getattr(arguments, name, None) is not None for name in ("serial", "device_id")
    ) or (
        ssh_relevant
        and (
            any(
                getattr(arguments, name, None) is not None
                for name in ("ssh_host", "ssh_user", "ssh_port", "ssh_key")
            )
            or any(
                os.environ.get(name)
                for name in (
                    "LINKPLANE_SSH_HOST",
                    "LINKPLANE_SSH_USER",
                    "LINKPLANE_SSH_PORT",
                    "LINKPLANE_SSH_KEY",
                )
            )
        )
    )
    if arguments.command in {"devices", "doctor"} and requested_name is None:
        if not endpoint_overridden:
            arguments.discover_all_profiles = True
        return
    if requested_name is None and endpoint_overridden:
        return
    profile = selected_profile(config, requested_name)
    if profile is None:
        return

    profile_ssh = registry.profile_ssh_config(config, profile) or {}
    if requested_name is not None:
        serial = getattr(arguments, "serial", None)
        if serial is not None and serial not in profile.adb_serials:
            raise BridgeError(
                f"--serial {serial} conflicts with device profile {profile.name}"
            )
        for argument_name, config_name in (
            ("ssh_host", "host"),
            ("ssh_user", "user"),
            ("ssh_port", "port"),
            ("ssh_key", "identity_file"),
        ):
            explicit = getattr(arguments, argument_name, None)
            if explicit is not None and explicit != profile_ssh.get(config_name):
                raise BridgeError(
                    f"--{argument_name.replace('_', '-')} conflicts with device profile "
                    f"{profile.name}"
                )

    if arguments.command == "send" and transport == "localsend":
        if requested_name is not None:
            raise BridgeError("--device cannot be used with the LocalSend transport")
        return
    uses_adb = transport not in {"ssh", "localsend"} and not (
        arguments.command == "camera" and arguments.camera_action == "capture"
    )
    needs_foreground_adb = arguments.command == "clipboard" or (
        arguments.command == "camera" and arguments.camera_action == "capture"
    )
    profile_adb_serial = (
        available_adb_serial(profile)
        if uses_adb or needs_foreground_adb
        else profile.adb_serial
    )
    arguments.profile_adb_serial = profile_adb_serial
    arguments.profile_adb_identities = {
        serial: profile.device_id for serial in profile.adb_serials
    }
    arguments.profile_adb_serials = frozenset(profile.adb_serials)
    if uses_adb and hasattr(arguments, "serial") and arguments.serial is None:
        if profile_adb_serial is None:
            if (
                arguments.command in TRANSPORT_FALLBACK_COMMANDS
                and transport == "auto"
                and profile_ssh
            ):
                arguments.transport = "ssh"
            else:
                raise BridgeError(f"device profile {profile.name} has no ADB endpoint")
        else:
            arguments.serial = profile_adb_serial
            arguments.serial_from_profile = True
    if profile.ssh:
        for argument_name, config_name in (
            ("ssh_host", "host"),
            ("ssh_user", "user"),
            ("ssh_port", "port"),
            ("ssh_key", "identity_file"),
        ):
            if hasattr(arguments, argument_name) and getattr(arguments, argument_name) is None:
                setattr(arguments, argument_name, profile.ssh.get(config_name))
    arguments.profile_device_id = profile.device_id
    arguments.profile_ssh_config = profile_ssh


def configured_ssh_context(
    arguments: argparse.Namespace,
) -> tuple[SshTransport, str | None]:
    config = load_config(arguments.config)
    ssh_config_source = config
    if hasattr(arguments, "profile_ssh_config"):
        ssh_config_source = {"ssh": arguments.profile_ssh_config}
    ssh = ssh_from_config(
        ssh_config_source,
        host=arguments.ssh_host,
        user=arguments.ssh_user,
        port=arguments.ssh_port,
        identity_file=arguments.ssh_key,
        use_environment=not hasattr(arguments, "profile_ssh_config"),
    )
    ssh_config = ssh_config_source.get("ssh", {})
    if not isinstance(ssh_config, dict):
        raise BridgeError("the ssh configuration must be a JSON object")
    endpoint_overridden = any(
        (
            arguments.ssh_host,
            arguments.ssh_user,
            arguments.ssh_port,
            arguments.ssh_key,
            os.environ.get("LINKPLANE_SSH_HOST"),
            os.environ.get("LINKPLANE_SSH_USER"),
            os.environ.get("LINKPLANE_SSH_PORT"),
            os.environ.get("LINKPLANE_SSH_KEY"),
        )
    )
    device_id = getattr(arguments, "device_id", None)
    if device_id is None:
        device_id = getattr(arguments, "profile_device_id", None)
    if device_id is None and not endpoint_overridden:
        device_id = ssh_config.get("device_id")
    if device_id is not None and not isinstance(device_id, str):
        raise BridgeError("ssh.device_id must be a string")
    return ssh, device_id


def configured_ssh(arguments: argparse.Namespace) -> SshTransport:
    return configured_ssh_context(arguments)[0]


def discovered_devices(arguments: argparse.Namespace) -> DiscoveryResult:
    if (
        getattr(arguments, "discover_all_profiles", False)
    ):
        try:
            config = load_config(arguments.config)
            profiles = profiles_from_config(config)
            additional_ssh: list[tuple[SshTransport, str]] = []
            adb_identities: dict[str, str] = {}
            for profile in profiles.values():
                for serial in profile.adb_serials:
                    adb_identities[serial] = profile.device_id
                if profile.ssh:
                    additional_ssh.append(
                        (
                            ssh_from_config(
                                {"ssh": profile.ssh}, use_environment=False
                            ),
                            profile.device_id,
                        )
                    )
            legacy_ssh = ssh_from_config(config)
            legacy_config = config.get("ssh", {})
            legacy_id = (
                legacy_config.get("device_id")
                if isinstance(legacy_config, dict)
                else None
            )
            discovery = discover_devices(
                legacy_ssh,
                legacy_id,
                additional_ssh=tuple(additional_ssh),
                adb_identities=adb_identities,
            )
            return discovery
        except BridgeError as error:
            discovery = discover_devices(SshTransport(None, None))
            return DiscoveryResult(
                discovery.devices,
                (*discovery.issues, DiscoveryIssue("profiles", str(error))),
            )
    ssh_issue = None
    try:
        ssh, device_id = configured_ssh_context(arguments)
    except BridgeError as error:
        ssh = SshTransport(None, None)
        device_id = None
        ssh_issue = DiscoveryIssue("ssh", str(error))
    discovery = discover_devices(
        ssh,
        device_id,
        adb_identities=getattr(arguments, "profile_adb_identities", None),
        allowed_adb_serials=getattr(arguments, "profile_adb_serials", None),
    )
    if ssh_issue:
        return DiscoveryResult(discovery.devices, (*discovery.issues, ssh_issue))
    return discovery


def print_devices(discovery: DiscoveryResult) -> None:
    print("Linkplane Devices")
    devices = discovery.devices
    if not devices:
        print("No devices found")
    for device in devices:
        version = f" (Android {device.android})" if device.android else ""
        print(f"\n{device.name}{version}")
        print(f"ID           {device.id}")
        for endpoint in device.endpoints:
            detail = endpoint.address
            if endpoint.error:
                detail = f"{detail}: {endpoint.error}"
            print(f"{endpoint.transport.upper():<12} {endpoint.state}: {detail}")
        capabilities = ", ".join(
            capability.name
            if capability.status == "ready"
            else f"{capability.name} ({capability.status.replace('_', ' ')})"
            for capability in device.capabilities
        ) or "none"
        print(f"Capabilities {capabilities}")
    for issue in discovery.issues:
        print(f"\n{issue.backend.upper()} discovery error: {issue.error}")


def print_observed_devices(arguments: argparse.Namespace) -> int:
    """`devices --observed`: the daemon's snapshot, no probing at all."""
    from linkplane.daemon import resolve_state_path

    path = resolve_state_path(arguments.state)
    if not path.exists():
        raise core_errors.LinkplaneError(
            core_errors.DAEMON_NOT_RUNNING,
            f"no observed state at {path}",
            ("run `linkplane daemon run` (or `daemon install`) to start observing",),
        )
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BridgeError(f"unable to read observed state at {path}: {error}") from error
    if arguments.json:
        print(json.dumps({"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": snapshot}, indent=2))
        return 0
    print("Linkplane Devices (observed)")
    daemon = snapshot.get("daemon")
    print(f"Daemon       {'running, pid ' + str(daemon['pid']) if daemon else 'not running'}")
    print(f"Updated      {snapshot.get('updated')}")
    devices = snapshot.get("devices") or {}
    if not devices:
        print("No devices observed")
    for name, state in devices.items():
        battery = state.get("battery") or {}
        level = f"{battery['level']}%" if battery.get("level") is not None else "-"
        print(f"\n{name}")
        print(f"Connection   {state.get('connection')} via {state.get('provider') or '-'} ({state.get('address') or '-'})")
        print(f"Battery      {level}")
        print(f"Wi-Fi        {state.get('wifi_ssid') or '-'}")
        print(f"Last seen    {state.get('last_seen') or '-'}")
    return 0


def print_checks(checks: Sequence[Check]) -> None:
    print("Linkplane Doctor")
    for check in checks:
        marker = {"ok": "OK", "warning": "WARN", "error": "ERROR"}[check.status]
        code = f"  ({check.code})" if check.code else ""
        print(f"[{marker:<5}] {check.name}: {check.summary}{code}")
        if check.fix:
            print(f"        Fix: {check.fix}")
    problems = [check for check in checks if check.status == "error"]
    if problems:
        print()
        print(f"{len(problems)} problem{'s' if len(problems) != 1 else ''} found.")
        for check in problems:
            if check.code:
                print(f"{check.name}: {check.summary}  Error: {check.code}")


def profile_identities(config_path: str | None) -> dict[str, str]:
    """serial -> profile name, so observed events name devices the way profiles do."""
    try:
        return registry.identities(load_config(config_path))
    except BridgeError:
        return {}


def resolve_provider(arguments: argparse.Namespace) -> Provider:
    """The one place the CLI turns flags, profiles, and config into a `Provider`."""
    return select_provider(
        getattr(arguments, "transport", "auto"),
        serial=getattr(arguments, "serial", None),
        serial_from_profile=getattr(arguments, "serial_from_profile", False),
        ssh_factory=lambda: configured_ssh(arguments),
    )


def render_error(error: BaseException) -> str:
    """Friendly text for the human CLI: title, hints, and the stable code when known."""
    if isinstance(error, core_errors.LinkplaneError):
        lines = [error.title, "", str(error)]
        if error.hints:
            lines.extend(["", "Check that:"])
            lines.extend(f"  • {hint}" for hint in error.hints)
        lines.extend(["", f"Error: {error.code}"])
        return "\n".join(lines)
    return f"linkplane: {error}"


def get_status(arguments: argparse.Namespace) -> dict[str, Any]:
    result = read_status(
        StatusRequest(
            transport=arguments.transport,
            serial=arguments.serial,
            serial_from_profile=getattr(arguments, "serial_from_profile", False),
        ),
        adb_factory=lambda serial: AdbTransport(serial),
        ssh_factory=lambda: configured_ssh(arguments),
    )
    if result.error is not None:
        if result.error.error_code is not None:
            raise core_errors.LinkplaneError(
                result.error.error_code, result.error.message, result.error.hints
            )
        raise BridgeError(result.error.message)
    if result.value is None:
        raise AssertionError("successful status operation did not return a value")
    return result.value.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        apply_device_profile(arguments)
        if arguments.command in CORE_COMMANDS:
            provider = resolve_provider(arguments)
            return CORE_COMMANDS[arguments.command].run(arguments, provider)
        if arguments.command == "events":
            return events_command.run(arguments, identities=profile_identities(arguments.config))
        if arguments.command == "watch":
            return watch_command.run(arguments, identities=profile_identities(arguments.config))
        if arguments.command == "automations":
            return automations_command.run(arguments)
        if arguments.command == "clients":
            return clients_command.run(arguments)
        if arguments.command == "setup":
            return setup_command.run(arguments)
        if arguments.command == "uninstall":
            return uninstall_command.run(arguments)
        if arguments.command == "daemon":
            return daemon_command.run(
                arguments, identities=profile_identities(getattr(arguments, "config", None))
            )
        if arguments.command == "profiles":
            if arguments.profile_action == "list":
                return list_profiles(arguments)
            if arguments.profile_action == "remove":
                return remove_profile(arguments)
            if arguments.profile_action == "refresh":
                return refresh_profiles(arguments)
            return set_default_profile(arguments)
        if arguments.command == "pair":
            if arguments.pair_action == "usb":
                return pair_usb(arguments)
            if arguments.pair_action == "wireless":
                return pair_wireless(arguments)
            return pair_ssh(arguments)
        if arguments.command == "status":
            status = get_status(arguments)
            if arguments.json:
                print(
                    json.dumps(
                        {"schema_version": JSON_SCHEMA_VERSION, "ok": True, "data": status},
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print_status(status)
                print_backup_status(arguments)
            return 0
        if arguments.command == "screen":
            return screen(arguments)
        if arguments.command == "audio":
            return audio(arguments)
        if arguments.command == "send":
            return send(arguments)
        if arguments.command == "backup":
            return backup(arguments)
        if arguments.command == "camera":
            if arguments.camera_action == "capture":
                ssh, device_id = configured_ssh_context(arguments)
                adb_serial = getattr(arguments, "profile_adb_serial", device_id)
                return capture_photo(arguments, ssh, adb_serial)
            return preview_camera(arguments)
        if arguments.command == "webcam":
            if arguments.webcam_action == "start":
                return webcam_start(arguments)
            return webcam_stop(arguments)
        if arguments.command == "notify":
            return notify(arguments, lambda: configured_ssh(arguments))
        if arguments.command == "find":
            return find_phone(arguments)
        if arguments.command == "clipboard":
            ssh, device_id = configured_ssh_context(arguments)
            adb_serial = getattr(arguments, "profile_adb_serial", device_id)
            return clipboard(arguments, ssh, adb_serial)
        if arguments.command == "devices" and arguments.observed:
            return print_observed_devices(arguments)
        if arguments.command in {"devices", "doctor"}:
            discovery = discovered_devices(arguments)
            if arguments.command == "devices":
                if arguments.json:
                    print(
                        json.dumps(
                            {
                                "schema_version": JSON_SCHEMA_VERSION,
                                "ok": not discovery.issues,
                                "data": {
                                    "devices": [
                                        device.to_dict() for device in discovery.devices
                                    ],
                                    "issues": [
                                        issue.to_dict() for issue in discovery.issues
                                    ],
                                },
                            },
                            indent=2,
                        )
                    )
                else:
                    print_devices(discovery)
                return 1 if discovery.issues else 0
            result = run_diagnostics(
                DoctorRequest(
                    discovery,
                    config_path=arguments.config,
                    provider_resolver=lambda: resolve_provider(arguments),
                )
            )
            if result.error is not None:
                raise BridgeError(result.error.message)
            if result.value is None:
                raise AssertionError("successful diagnostics did not return a value")
            checks = result.value.checks
            if arguments.json:
                print(
                    json.dumps(
                        {
                            "schema_version": JSON_SCHEMA_VERSION,
                            "ok": not any(check.status == "error" for check in checks),
                            "data": result.value.to_dict(),
                        },
                        indent=2,
                    )
                )
            else:
                print_checks(checks)
            return 1 if any(check.status == "error" for check in checks) else 0
    except (BridgeError, KeyError, TypeError, ValueError, OperationCancelled) as error:
        if arguments.debug:
            logging.getLogger("linkplane").debug("command failed", exc_info=error)
        cancelled = isinstance(error, OperationCancelled)
        if getattr(arguments, "json", False):
            payload: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
            if cancelled:
                payload["code"] = core_errors.CANCELLED
            elif isinstance(error, BridgeError):
                classified = core_errors.classify(error)
                payload["code"] = classified.code
                if classified.hints:
                    payload["hints"] = list(classified.hints)
            print(
                json.dumps(
                    {"schema_version": JSON_SCHEMA_VERSION, "ok": False, "error": payload},
                    indent=2,
                )
            )
            return EXIT_CANCELLED if cancelled else 1
        print(render_error(error), file=sys.stderr)
        return EXIT_CANCELLED if cancelled else 1
    except KeyboardInterrupt:
        # A second Ctrl+C (or one during a command with no cancellation boundary).
        print("linkplane: interrupted", file=sys.stderr)
        return EXIT_CANCELLED
    except BrokenPipeError:
        # The consumer closed the pipe; make sure the interpreter's final flush of
        # stdout cannot raise again, then exit the way `head` expects.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return EXIT_BROKEN_PIPE
    return 0
