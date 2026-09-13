import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from linkplane.clipboard import (
    ClipboardRequest,
    DesktopClipboard,
    clipboard,
    desktop_clipboard,
    get_desktop_clipboard,
    get_phone_clipboard,
    set_desktop_clipboard,
    set_phone_clipboard,
    sync_clipboards,
    use_clipboard,
)
from linkplane.transports import BridgeError, SshTransport


class ClipboardTests(unittest.TestCase):
    def test_desktop_backend_requires_complete_wayland_toolset(self):
        paths = {
            "wl-copy": "/usr/bin/wl-copy",
            "xclip": "/usr/bin/xclip",
            "apt-get": "/usr/bin/apt-get",
        }

        backend = desktop_clipboard(paths.get)

        self.assertEqual(backend.name, "X11")

    def test_desktop_backend_prefers_active_x11_session(self):
        paths = {
            "wl-copy": "/usr/bin/wl-copy",
            "wl-paste": "/usr/bin/wl-paste",
            "xclip": "/usr/bin/xclip",
        }

        backend = desktop_clipboard(paths.get, {"XDG_SESSION_TYPE": "x11"})

        self.assertEqual(backend.name, "X11")

    def test_phone_clipboard_uses_stdout_and_stdin(self):
        runner = Mock(side_effect=["phone text", "", "new text"])
        transport = SshTransport("phone.local", "termux", runner=runner)

        self.assertEqual(get_phone_clipboard(transport), "phone text")
        set_phone_clipboard(transport, "new text")

        self.assertEqual(runner.call_args_list[1].kwargs["input_text"], "new text")
        self.assertNotIn("new text", runner.call_args_list[1].args[0])
        self.assertEqual(len(runner.call_args_list), 3)

    def test_desktop_clipboard_uses_stdout_and_stdin(self):
        backend = DesktopClipboard("Wayland", ("read",), ("write",))
        runner = Mock(side_effect=["desktop text", "", "text/plain"])

        self.assertEqual(get_desktop_clipboard(backend, runner), "desktop text")
        set_desktop_clipboard(backend, "new text", runner)

        self.assertEqual(runner.call_args_list[1].kwargs["input_text"], "new text")
        self.assertEqual(runner.call_args_list[2].args[0], ["wl-paste", "--list-types"])

    def test_empty_desktop_clipboard_is_valid(self):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        runner = Mock(side_effect=BridgeError("wl-paste: Nothing is copied"))

        self.assertEqual(get_desktop_clipboard(backend, runner), "")

    @patch("builtins.print")
    def test_set_requires_text(self, _print):
        arguments = SimpleNamespace(action="set", text=None, dry_run=False)
        transport = Mock()

        with self.assertRaisesRegex(BridgeError, "requires text"):
            clipboard(arguments, transport)

    @patch("linkplane.clipboard.set_desktop_clipboard")
    @patch("linkplane.clipboard.get_phone_clipboard", return_value="phone text")
    @patch("linkplane.clipboard.desktop_clipboard")
    @patch("builtins.print")
    def test_pull_copies_phone_to_desktop(
        self, _print, desktop_clipboard, get_phone, set_desktop
    ):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        desktop_clipboard.return_value = backend
        arguments = SimpleNamespace(
            action="pull",
            text=None,
            dry_run=False,
            no_foreground=True,
        )

        transport = SshTransport("phone.local", "termux", runner=Mock())

        self.assertEqual(clipboard(arguments, transport), 0)
        set_desktop.assert_called_once_with(backend, "phone text")

    @patch("linkplane.clipboard.set_phone_clipboard")
    @patch("linkplane.clipboard.set_desktop_clipboard")
    @patch("linkplane.clipboard.get_desktop_clipboard")
    @patch("linkplane.clipboard.get_phone_clipboard")
    @patch("linkplane.clipboard.time.sleep")
    def test_sync_propagates_changes_without_echoing_them(
        self,
        sleep,
        get_phone,
        get_desktop,
        set_desktop,
        set_phone,
    ):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        transport = Mock()
        sleep.side_effect = [None, None, KeyboardInterrupt]
        get_phone.side_effect = ["phone initial", "phone changed", "phone changed"]
        get_desktop.side_effect = ["desktop initial", "desktop initial", "desktop changed"]

        events = []

        self.assertEqual(
            sync_clipboards(
                transport,
                backend,
                interval=0.1,
                prefer="desktop",
                progress=events.append,
            ),
            2,
        )

        set_desktop.assert_called_once_with(backend, "phone changed")
        set_phone.assert_called_once_with(transport, "desktop changed")
        self.assertEqual(
            [event.phase for event in events], ["watching", "updated", "updated"]
        )
        self.assertTrue(
            all("phone changed" not in str(event.to_dict()) for event in events)
        )

    @patch("linkplane.clipboard.set_phone_clipboard")
    @patch("linkplane.clipboard.set_desktop_clipboard")
    @patch(
        "linkplane.clipboard.get_desktop_clipboard",
        side_effect=["desktop 1", "desktop 2"],
    )
    @patch("linkplane.clipboard.get_phone_clipboard", side_effect=["phone 1", "phone 2"])
    @patch("linkplane.clipboard.time.sleep", side_effect=[None, KeyboardInterrupt])
    def test_sync_resolves_simultaneous_changes_with_preference(
        self,
        _sleep,
        _get_phone,
        _get_desktop,
        set_desktop,
        set_phone,
    ):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        transport = Mock()

        sync_clipboards(transport, backend, interval=1, prefer="phone")

        set_desktop.assert_called_once_with(backend, "phone 2")
        set_phone.assert_not_called()

    @patch("linkplane.clipboard.get_phone_clipboard", side_effect=KeyboardInterrupt)
    def test_sync_stops_cleanly_during_initial_read(self, _get_phone):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        events = []

        self.assertEqual(
            sync_clipboards(
                Mock(),
                backend,
                interval=1,
                prefer="desktop",
                progress=events.append,
            ),
            0,
        )
        self.assertEqual(events, [])

    @patch("builtins.print")
    def test_sync_rejects_nonpositive_interval(self, _print):
        arguments = SimpleNamespace(
            action="sync",
            text=None,
            interval=0,
            prefer="desktop",
            dry_run=False,
        )

        with self.assertRaisesRegex(BridgeError, "greater than zero"):
            clipboard(arguments, Mock())

    @patch("linkplane.clipboard.get_phone_clipboard", return_value="private text")
    @patch("builtins.print")
    def test_service_returns_typed_get_without_printing_or_progress_content(
        self, output, _get_phone
    ):
        transport = SshTransport("phone.local", "termux", runner=Mock())
        events = []

        result = use_clipboard(
            ClipboardRequest("get", foreground=False),
            transport,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.text, "private text")
        self.assertEqual(result.value.bytes_transferred, 12)
        self.assertEqual(result.value.commands[0][-1], "termux-clipboard-get")
        self.assertEqual([event.phase for event in events], ["started", "completed"])
        self.assertTrue(
            all("private text" not in str(event.to_dict()) for event in events)
        )
        output.assert_not_called()

    def test_service_dry_run_keeps_text_out_of_commands(self):
        runner = Mock()
        transport = SshTransport("phone.local", "termux", runner=runner)
        foreground = Mock()
        backend_factory = Mock()

        result = use_clipboard(
            ClipboardRequest("set", text="private text", dry_run=True),
            transport,
            "serial-1",
            foreground_runner=foreground,
            backend_factory=backend_factory,
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.value.dry_run)
        self.assertFalse(
            any(
                "private text" in part
                for command in result.value.commands
                for part in command
            )
        )
        runner.assert_not_called()
        foreground.assert_not_called()
        backend_factory.assert_not_called()

    @patch("linkplane.clipboard.set_desktop_clipboard")
    @patch(
        "linkplane.clipboard.get_desktop_clipboard",
        side_effect=["desktop initial", "desktop initial"],
    )
    @patch(
        "linkplane.clipboard.get_phone_clipboard",
        side_effect=["phone initial", "phone changed"],
    )
    @patch("linkplane.clipboard.time.sleep", side_effect=[None, KeyboardInterrupt])
    def test_sync_service_returns_update_count_and_structured_progress(
        self, _sleep, _get_phone, _get_desktop, set_desktop
    ):
        backend = DesktopClipboard("Test", ("read",), ("write",))
        transport = SshTransport("phone.local", "termux", runner=Mock())
        events = []

        result = use_clipboard(
            ClipboardRequest("sync", interval=0.25, foreground=False),
            transport,
            backend_factory=lambda: backend,
            progress=events.append,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value.desktop_backend, "Test")
        self.assertEqual(result.value.updates, 1)
        self.assertEqual(
            [event.phase for event in events],
            ["started", "watching", "updated", "completed"],
        )
        self.assertEqual(events[2].details["direction"], "phone_to_desktop")
        set_desktop.assert_called_once_with(backend, "phone changed")

    def test_service_returns_typed_dependency_error(self):
        transport = SshTransport("phone.local", "termux", runner=Mock())

        result = use_clipboard(
            ClipboardRequest("pull", foreground=False),
            transport,
            backend_factory=lambda: None,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "dependency_missing")


if __name__ == "__main__":
    unittest.main()
