"""`linkplane events` through the CLI with a fake observer."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linkplane.cli import main
from linkplane.core.events import Event


class FakeObserver:
    def __init__(self, sink, **kwargs):
        self.sink = sink
        self.kwargs = kwargs

    def run(self):
        self.sink(Event("observer.started", "*", data={"sources": ["fake"], "interval": 1.0}))
        self.sink(Event("device.connected", "phone", provider="adb", data={"address": "S1", "initial": True}))
        self.sink(Event("battery.low", "phone", provider="adb", data={"level": 12, "threshold": 20}))
        self.sink(Event("observer.stopped", "*"))


def run(argv, history):
    out, err = StringIO(), StringIO()
    with patch("linkplane.commands.events.Observer", FakeObserver), patch(
        "linkplane.cli.profile_identities", return_value={"S1": "phone"}
    ), redirect_stdout(out), redirect_stderr(err):
        code = main([*argv, "--history", history])
    return code, out.getvalue(), err.getvalue()


class EventsCommandTests(unittest.TestCase):
    def test_human_stream_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            history = str(Path(directory) / "events.jsonl")
            code, out, err = run(["events", "--interval", "5", "--low-battery", "15"], history)
            lines = Path(history).read_text().splitlines()
        self.assertEqual(code, 0)
        self.assertIn("device.connected     phone              S1  (initial)", out)
        self.assertIn("battery.low          phone              12% (threshold 20)", out)
        self.assertEqual(err.strip(), "Stopped.")
        self.assertEqual([json.loads(l)["seq"] for l in lines], [1, 2, 3, 4])

    def test_json_stream_is_one_object_per_line_with_seq(self):
        with tempfile.TemporaryDirectory() as directory:
            history = str(Path(directory) / "events.jsonl")
            code, out, _ = run(["events", "--json"], history)
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual([r["type"] for r in records][1:3], ["device.connected", "battery.low"])
        self.assertEqual([r["seq"] for r in records], [1, 2, 3, 4])
        self.assertTrue(all(r["schema"] == "linkplane.event/1" for r in records))

    def test_no_history_leaves_seq_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            history = str(Path(directory) / "events.jsonl")
            code, out, _ = run(["events", "--json", "--no-history"], history)
            self.assertFalse(Path(history).exists())
        self.assertTrue(all(json.loads(l)["seq"] is None for l in out.splitlines()))

    def test_observer_receives_flags(self):
        captured = {}

        class Capturing(FakeObserver):
            def __init__(self, sink, **kwargs):
                captured.update(kwargs)
                super().__init__(sink, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "linkplane.commands.events.Observer", Capturing
        ), patch("linkplane.cli.profile_identities", return_value={"S1": "phone"}), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main(["events", "--interval", "7", "--low-battery", "33", "--no-wifi", "--history", str(Path(directory) / "h.jsonl")])
        self.assertEqual((captured["interval"], captured["low_battery"], captured["poll_wifi_enabled"]), (7.0, 33, False))
        self.assertEqual(captured["identities"], {"S1": "phone"})


if __name__ == "__main__":
    unittest.main()
