"""Console behaviour that the browser Manager relies on."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_personal


class ConsoleTest(unittest.TestCase):
    def test_storage_retries_only_when_firmware_reports_busy_before_dispatch(self):
        console = import_personal.Console(mock.Mock())
        with mock.patch.object(console, "_exchange", side_effect=[
                "ERR Native flash busy", "OK", TimeoutError("lost acknowledgment")
        ]) as exchange, mock.patch.object(import_personal.time, "sleep"):
            self.assertEqual(console.command("native_install erase 2"), "OK")
            self.assertEqual(exchange.call_count, 2)
            self.assertEqual(exchange.call_args_list[0], exchange.call_args_list[1])
            with self.assertRaisesRegex(TimeoutError, "lost acknowledgment"):
                console.command("native_install erase 3")
            self.assertEqual(exchange.call_count, 3)


if __name__ == "__main__":
    unittest.main()
