import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane.core.events import Event
from linkplane.history import HistoryWriter, last_seq, read_history, resolve_history_path


class HistoryTests(unittest.TestCase):
    def test_seq_continues_across_writers_and_skips_corrupt_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "nested" / "events.jsonl")
            with HistoryWriter(path) as writer:
                first = writer.append(Event("device.connected", "phone"))
                self.assertEqual(first.seq, 1)
                writer.append(Event("battery.changed", "phone", data={"level": 3}))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"type": "battery.')  # crash mid-write
            self.assertEqual(last_seq(path), 2)
            with HistoryWriter(path) as writer:
                self.assertEqual(writer.append(Event("device.disconnected", "phone")).seq, 3)
            events = list(read_history(path))
            self.assertEqual([e.seq for e in events], [1, 2, 3])
            self.assertEqual(json.loads(Path(path).read_text().splitlines()[0])["schema"], "linkplane.event/1")

    def test_default_path_follows_xdg_state_home(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-test", "LINKPLANE_HISTORY": ""}):
            self.assertEqual(resolve_history_path(), Path("/tmp/xdg-test/linkplane/events.jsonl"))
        with patch.dict(os.environ, {"LINKPLANE_HISTORY": "/tmp/custom.jsonl"}):
            self.assertEqual(resolve_history_path(), Path("/tmp/custom.jsonl"))

    def test_missing_history_reads_empty(self):
        self.assertEqual(list(read_history("/nonexistent/linkplane/events.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
