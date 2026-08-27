import tempfile
import threading
import sys
import types
import unittest
from unittest.mock import patch

from protocol import StreamEvent, crc16_modbus

try:
    import tkinter  # noqa: F401
except ModuleNotFoundError:
    tkinter_stub = types.ModuleType("tkinter")
    tkinter_stub.Tk = object
    tkinter_stub.filedialog = types.SimpleNamespace()
    tkinter_stub.messagebox = types.SimpleNamespace()
    tkinter_stub.ttk = types.SimpleNamespace()
    sys.modules["tkinter"] = tkinter_stub

from yed_ipd_tool import TestConfig, TestRunner


def make_runner():
    config = TestConfig(
        at_port="COM1",
        cli_port="",
        baudrate=115200,
        ssid="YED_IPD_TEST",
        password="12345678",
        channel=6,
        duration_minutes=30,
        ack_timeout_ms=5000,
        retry_count=3,
        output_root=tempfile.gettempdir(),
    )
    return TestRunner(config, threading.Event(), lambda _kind, _payload: None)


class RunnerTests(unittest.TestCase):
    def test_wait_keeps_events_after_matching_response(self):
        runner = make_runner()
        runner.serial_events.extend(
            [StreamEvent("line", line=b"OK"), StreamEvent("prompt", line=b">")]
        )
        self.assertEqual(runner._wait_for("ok", 0.1), b"OK")
        self.assertEqual(len(runner.serial_events), 1)
        self.assertEqual(runner._wait_for("prompt", 0.1), b">")

    def test_completion_requires_two_links(self):
        runner = make_runner()
        runner.stats.links[0].unique_frames = 10
        runner._complete_result()
        self.assertEqual(runner.stats.result, "FAIL")
        self.assertIn("at least 2", runner.stats.failure_reason)

    def test_clean_two_link_completion_passes(self):
        runner = make_runner()
        runner.stats.links[0].unique_frames = 10
        runner.stats.links[1].unique_frames = 10
        runner._complete_result()
        self.assertEqual(runner.stats.result, "PASS")

    def test_full_two_link_ack_and_echo_flow(self):
        class FakeSerial:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.incoming = bytearray()
                self.writes = []
                self.expected_payload = None
                self.is_open = True
                self.__class__.instances.append(self)

            @property
            def in_waiting(self):
                return len(self.incoming)

            def reset_input_buffer(self):
                self.incoming.clear()

            def reset_output_buffer(self):
                pass

            def flush(self):
                pass

            def close(self):
                self.is_open = False

            def read(self, size):
                data = bytes(self.incoming[:size])
                del self.incoming[:size]
                return data

            def write(self, data):
                self.writes.append(bytes(data))
                if self.expected_payload is not None:
                    if len(data) != self.expected_payload:
                        raise AssertionError("wrong echo payload length")
                    self.expected_payload = None
                    self.incoming.extend(b"\r\nRecv 5 bytes\r\n\r\nSEND OK\r\n")
                    return len(data)

                command = data.decode("ascii").strip()
                if command.startswith("AT+CIPSEND="):
                    self.expected_payload = int(command.rsplit(",", 1)[1])
                    self.incoming.extend(b"\r\nOK\r\n\r\n>\r\n")
                elif command.startswith("AT+CIPSERVER=1"):
                    self.incoming.extend(b"\r\nOK\r\n")
                    for link_id, frame_id, payload in (
                        (0, 101, b"link0"),
                        (1, 102, b"link1"),
                    ):
                        crc_input = f"+IPD,{link_id},5,{frame_id}".encode()
                        crc = crc16_modbus(crc_input)
                        self.incoming.extend(
                            f"\r\n+IPD,{link_id},5,{frame_id},{crc:04X}:".encode()
                            + payload
                        )
                else:
                    self.incoming.extend(b"\r\nOK\r\n")
                return len(data)

        with tempfile.TemporaryDirectory() as output_root:
            config = TestConfig(
                at_port="COM1",
                cli_port="",
                baudrate=115200,
                ssid="YED_IPD_TEST",
                password="12345678",
                channel=6,
                duration_minutes=0.001,
                ack_timeout_ms=5000,
                retry_count=3,
                output_root=output_root,
            )
            notifications = []
            runner = TestRunner(
                config,
                threading.Event(),
                lambda kind, payload: notifications.append((kind, payload)),
            )
            with patch("yed_ipd_tool.serial.Serial", FakeSerial):
                runner.run()

        self.assertEqual(runner.stats.result, "PASS")
        self.assertEqual(runner.stats.links[0].ack_ok, 1)
        self.assertEqual(runner.stats.links[1].ack_ok, 1)
        self.assertEqual(runner.stats.links[0].echoed_frames, 1)
        self.assertEqual(runner.stats.links[1].echoed_frames, 1)
        writes = FakeSerial.instances[0].writes
        self.assertIn(b"AT+IPDACK=101\r\n", writes)
        self.assertIn(b"AT+IPDACK=102\r\n", writes)
        self.assertIn(b"AT+CIPSEND=0,5\r\n", writes)
        self.assertIn(b"AT+CIPSEND=1,5\r\n", writes)


if __name__ == "__main__":
    unittest.main()
