import tempfile
import threading
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from protocol import IpdFrame, StreamEvent, crc16_modbus

try:
    import tkinter  # noqa: F401
except ModuleNotFoundError:
    tkinter_stub = types.ModuleType("tkinter")
    tkinter_stub.Tk = object
    tkinter_stub.filedialog = types.SimpleNamespace()
    tkinter_stub.messagebox = types.SimpleNamespace()
    tkinter_stub.ttk = types.SimpleNamespace()
    sys.modules["tkinter"] = tkinter_stub

from yed_ipd_tool import (
    TestConfig,
    TestFailure,
    TestRunner,
    application_directory,
)


def make_runner(fault_interval=0):
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
        fault_interval=fault_interval,
        output_root=tempfile.gettempdir(),
    )
    return TestRunner(config, threading.Event(), lambda _kind, _payload: None)


class RunnerTests(unittest.TestCase):
    @staticmethod
    def _frame(frame_id, link_id=0, payload=b"data"):
        return IpdFrame(
            link_id=link_id,
            frame_id=frame_id,
            payload=payload,
            received_crc=0,
            expected_crc=0,
        )

    def test_fault_modes_alternate_by_global_unique_frame_count(self):
        runner = make_runner(fault_interval=2)
        for frame_id in range(1, 5):
            runner._accept_frame(self._frame(frame_id, link_id=frame_id % 2))
        self.assertEqual(
            runner.planned_fault_modes,
            {2: "explicit", 4: "timeout"},
        )

    def test_explicit_fault_accepts_matching_retransmission(self):
        runner = make_runner(fault_interval=1)
        original = self._frame(100, link_id=1, payload=b"payload")
        runner._accept_frame(original)
        runner.pending_frames.popleft()
        with patch.object(runner, "_command", return_value=True) as command:
            runner._process_frame(original)
        command.assert_called_once_with(
            "AT+IPDRETRY=100", timeout=2.0, display=False
        )
        self.assertEqual(runner.fault_pending[100].mode, "explicit")

        runner._accept_frame(original)
        retransmission = runner.pending_frames.popleft()
        with patch.object(runner, "_echo_frame") as echo:
            runner._process_frame(retransmission)
        echo.assert_called_once_with(original)
        self.assertNotIn(100, runner.fault_pending)
        self.assertEqual(runner.stats.fault_explicit_completed, 1)
        self.assertEqual(
            runner.stats.links[1].intentional_retransmissions, 1
        )

    def test_fault_completes_only_after_ack_and_echo_succeed(self):
        runner = make_runner(fault_interval=1)
        frame = self._frame(100, link_id=1, payload=b"payload")
        runner._accept_frame(frame)
        runner.pending_frames.popleft()
        with patch.object(runner, "_command", return_value=True):
            runner._process_frame(frame)
        runner._accept_frame(frame)
        retransmission = runner.pending_frames.popleft()

        with patch.object(
            runner, "_echo_frame", side_effect=TestFailure("echo failed")
        ):
            with self.assertRaisesRegex(TestFailure, "echo failed"):
                runner._process_frame(retransmission)

        self.assertIn(100, runner.fault_pending)
        self.assertEqual(runner.stats.fault_explicit_completed, 0)

    def test_fault_retransmission_must_match_link_and_payload(self):
        runner = make_runner(fault_interval=1)
        original = self._frame(100, link_id=0, payload=b"original")
        runner._accept_frame(original)
        runner.pending_frames.popleft()
        with patch.object(runner, "_command", return_value=True):
            runner._process_frame(original)

        with self.assertRaisesRegex(
            TestFailure, "Retransmission changed link or payload"
        ):
            runner._accept_frame(
                self._frame(100, link_id=1, payload=b"changed")
            )

    def test_timeout_fault_sends_no_command(self):
        runner = make_runner(fault_interval=1)
        first = self._frame(100)
        second = self._frame(101)
        runner._accept_frame(first)
        runner.pending_frames.popleft()
        with patch.object(runner, "_command", return_value=True):
            runner._process_frame(first)
        runner._accept_frame(first)
        runner.pending_frames.popleft()
        with patch.object(runner, "_echo_frame"):
            runner._process_frame(first)

        runner._accept_frame(second)
        runner.pending_frames.popleft()
        with patch.object(runner, "_command") as command:
            runner._process_frame(second)
        command.assert_not_called()
        self.assertEqual(runner.fault_pending[101].mode, "timeout")
        self.assertEqual(runner.stats.fault_timeout_withheld, 1)

    def test_ack_timeout_is_retried(self):
        class FakeAt:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(data)

            def flush(self):
                pass

        runner = make_runner()
        runner.at = FakeAt()
        frame = self._frame(89)
        with patch.object(
            runner,
            "_wait_for",
            side_effect=[TestFailure("timeout"), b"OK"],
        ), patch("yed_ipd_tool.time.sleep"):
            runner._ack_frame(frame)
        self.assertEqual(
            runner.at.writes,
            [b"AT+IPDACK=89\r\n", b"AT+IPDACK=89\r\n"],
        )
        self.assertEqual(runner.stats.links[0].ack_ok, 1)
        self.assertEqual(runner.stats.links[0].ack_command_retries, 1)

    def test_frozen_app_uses_executable_directory(self):
        executable = str(Path(tempfile.gettempdir()) / "tool" / "YED_IPD_Test.exe")
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", executable
        ):
            self.assertEqual(
                application_directory(), Path(executable).resolve().parent
            )

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

    def test_completion_fails_for_scheduled_fault_not_processed(self):
        runner = make_runner(fault_interval=1)
        runner.stats.links[0].unique_frames = 1
        runner.stats.links[1].unique_frames = 1
        runner.planned_fault_modes[100] = "explicit"
        runner._complete_result()
        self.assertEqual(runner.stats.result, "FAIL")
        self.assertIn("scheduled faults not processed=100", runner.stats.failure_reason)

    def test_reversed_at_cli_ports_are_corrected(self):
        class FakePort:
            def reset_input_buffer(self):
                pass

        runner = make_runner()
        configured_at = FakePort()
        configured_cli = FakePort()
        runner.at = configured_at
        runner.cli = configured_cli
        runner.resolved_at_port = "COM3"
        runner.resolved_cli_port = "COM4"
        with patch.object(runner, "_probe_at", side_effect=[False, True]):
            runner._detect_port_roles()
        self.assertIs(runner.at, configured_cli)
        self.assertIs(runner.cli, configured_at)
        self.assertEqual(runner.resolved_at_port, "COM4")
        self.assertEqual(runner.resolved_cli_port, "COM3")

    def test_cli_debug_does_not_require_response(self):
        class FakeCli:
            def __init__(self):
                self.incoming = bytearray()

            @property
            def in_waiting(self):
                return len(self.incoming)

            def write(self, data):
                self.asserted_wire = data

            def flush(self):
                pass

            def read(self, size):
                data = bytes(self.incoming[:size])
                del self.incoming[:size]
                return data

        runner = make_runner()
        runner.cli = FakeCli()
        runner.resolved_cli_port = "COM4"
        runner._enable_cli_debug()
        self.assertEqual(runner.cli.asserted_wire, b"yed_ipd_debug 1\r\n")

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
                fault_interval=0,
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
