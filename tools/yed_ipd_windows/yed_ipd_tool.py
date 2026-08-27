"""Windows GUI for YED SoftAP TCP server reliable-IPD echo testing."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Deque, Dict, List, Optional, Set

import serial
from serial.tools import list_ports

from protocol import AtStreamParser, IpdFrame, StreamEvent


APP_NAME = "YED IPD TCP Server Test"
VERSION = "1.0.0"
SOFTAP_IP = "192.168.4.1"
SOFTAP_MASK = "255.255.255.0"
SERVER_PORT = 4000
MAX_LINKS = 5
MIN_TEST_LINKS = 2


@dataclass(frozen=True)
class TestConfig:
    at_port: str
    cli_port: str
    baudrate: int
    ssid: str
    password: str
    channel: int
    duration_minutes: float
    ack_timeout_ms: int
    retry_count: int
    output_root: str


@dataclass
class LinkStats:
    connected: bool = False
    frames: int = 0
    unique_frames: int = 0
    duplicate_frames: int = 0
    bytes_received: int = 0
    ack_ok: int = 0
    echoed_frames: int = 0
    echoed_bytes: int = 0
    send_fail: int = 0
    crc_errors: int = 0
    max_gap_seconds: float = 0.0
    last_frame_at: float = 0.0


@dataclass
class TestStats:
    links: List[LinkStats] = field(
        default_factory=lambda: [LinkStats() for _ in range(MAX_LINKS)]
    )
    retry_logs: int = 0
    drop_logs: int = 0
    at_errors: int = 0
    parse_errors: int = 0
    started_at: str = ""
    first_data_at: str = ""
    elapsed_seconds: float = 0.0
    result: str = "RUNNING"
    failure_reason: str = ""


class TestFailure(RuntimeError):
    pass


class TestRunner:
    CONNECT_RE = re.compile(rb"(?:^|,)([0-4]),CONNECT$")
    CLOSED_RE = re.compile(rb"(?:^|,)([0-4]),CLOSED$")

    def __init__(
        self,
        config: TestConfig,
        stop_event: threading.Event,
        notify: Callable[[str, object], None],
    ) -> None:
        self.config = config
        self.stop_event = stop_event
        self.notify = notify
        self.stats = TestStats(started_at=datetime.now().isoformat(timespec="seconds"))
        self.parser = AtStreamParser()
        self.at: Optional[serial.Serial] = None
        self.cli: Optional[serial.Serial] = None
        self.output_dir: Optional[Path] = None
        self.at_log = None
        self.cli_log = None
        self.event_log = None
        self.serial_events: Deque[StreamEvent] = deque()
        self.pending_frames: Deque[IpdFrame] = deque()
        self.queued_frame_ids: Set[int] = set()
        self.processed_ids: Set[int] = set()
        self.processed_order: Deque[int] = deque()
        self.first_data_monotonic: Optional[float] = None
        self.last_snapshot = 0.0

    def _emit_log(self, message: str, display: bool = True) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {message}"
        if self.event_log is not None:
            self.event_log.write(line + "\n")
            self.event_log.flush()
        if display:
            self.notify("log", line)

    def _write_at_marker(self, direction: str, data: bytes) -> None:
        if self.at_log is None:
            return
        stamp = datetime.now().isoformat(timespec="milliseconds")
        self.at_log.write(f"\n[{stamp}][{direction}] ".encode("ascii"))
        self.at_log.write(data)
        self.at_log.flush()

    def _write_at(self, data: bytes, label: str, display: bool = True) -> None:
        if self.at is None:
            raise TestFailure("AT serial port is not open")
        self._write_at_marker("PC->AT", data)
        self.at.write(data)
        self.at.flush()
        self._emit_log(label, display=display)

    def _write_command(self, command: str, display: bool = True) -> None:
        self._write_at(
            (command + "\r\n").encode("ascii"),
            f"> {command}",
            display=display,
        )

    def _read_serial(self) -> None:
        events: List[StreamEvent] = []
        if self.at is not None:
            waiting = self.at.in_waiting
            chunk = self.at.read(max(waiting, 1))
            if chunk:
                if self.at_log is not None:
                    self.at_log.write(chunk)
                    self.at_log.flush()
                events.extend(self.parser.feed(chunk))

        if self.cli is not None:
            waiting = self.cli.in_waiting
            chunk = self.cli.read(max(waiting, 1))
            if chunk:
                stamp = datetime.now().isoformat(timespec="milliseconds")
                if self.cli_log is not None:
                    self.cli_log.write(f"[{stamp}] ".encode("ascii") + chunk)
                    self.cli_log.flush()
                self._scan_cli(chunk)
        self.serial_events.extend(events)

    def _scan_cli(self, chunk: bytes) -> None:
        if not hasattr(self, "_cli_scan_buffer"):
            self._cli_scan_buffer = bytearray()
        self._cli_scan_buffer.extend(chunk)
        while b"\n" in self._cli_scan_buffer:
            line, _, rest = self._cli_scan_buffer.partition(b"\n")
            self._cli_scan_buffer = bytearray(rest)
            if b"[YED-IPD] timeout retry" in line:
                self.stats.retry_logs += 1
                self._emit_log("CLI retry: " + line.decode("utf-8", "replace").strip())
            elif b"[YED-IPD] timeout drop" in line:
                self.stats.drop_logs += 1
                self._emit_log("CLI drop: " + line.decode("utf-8", "replace").strip())

    def _handle_line(self, line: bytes) -> None:
        stripped = line.strip()
        if not stripped:
            return
        match = self.CONNECT_RE.search(stripped)
        if match:
            link_id = int(match.group(1))
            self.stats.links[link_id].connected = True
            self._emit_log(f"link {link_id} connected")
            return
        match = self.CLOSED_RE.search(stripped)
        if match:
            link_id = int(match.group(1))
            self.stats.links[link_id].connected = False
            self._emit_log(f"link {link_id} closed")

    def _accept_frame(self, frame: IpdFrame) -> None:
        if frame.link_id < 0 or frame.link_id >= MAX_LINKS:
            self.stats.parse_errors += 1
            raise TestFailure(f"Invalid link ID in +IPD: {frame.link_id}")

        link = self.stats.links[frame.link_id]
        now = time.monotonic()
        link.frames += 1
        link.bytes_received += len(frame.payload)
        if link.last_frame_at:
            link.max_gap_seconds = max(link.max_gap_seconds, now - link.last_frame_at)
        link.last_frame_at = now

        if not frame.crc_valid:
            link.crc_errors += 1
            raise TestFailure(
                f"CRC mismatch, link={frame.link_id}, frame={frame.frame_id}, "
                f"received={frame.received_crc:04X}, expected={frame.expected_crc:04X}"
            )

        if frame.frame_id in self.queued_frame_ids or frame.frame_id in self.processed_ids:
            link.duplicate_frames += 1
            self._emit_log(
                f"duplicate frame link={frame.link_id} frame={frame.frame_id} "
                f"len={len(frame.payload)}"
            )
            return

        if self.first_data_monotonic is None:
            self.first_data_monotonic = now
            self.stats.first_data_at = datetime.now().isoformat(timespec="seconds")
            self.notify("state", "RUNNING")
            self._emit_log("First TCP data received; duration timer started")

        link.unique_frames += 1
        self.queued_frame_ids.add(frame.frame_id)
        self.pending_frames.append(frame)
        self._emit_log(
            f"RX link={frame.link_id} frame={frame.frame_id} len={len(frame.payload)}",
            display=False,
        )

    def _handle_event(self, event: StreamEvent) -> None:
        if event.kind == "frame" and event.frame is not None:
            self._accept_frame(event.frame)
        elif event.kind in ("line", "text"):
            self._handle_line(event.line)

    def _wait_for(
        self,
        expected: str,
        timeout: float,
        fail_on_error: bool = True,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.serial_events:
                self._read_serial()
            while self.serial_events:
                event = self.serial_events.popleft()
                if event.kind == "frame" and event.frame is not None:
                    self._accept_frame(event.frame)
                    continue
                if event.kind == "prompt" and expected == "prompt":
                    return b">"
                if event.kind in ("line", "text"):
                    self._handle_line(event.line)
                    line = event.line.strip()
                    if expected == "ok" and line == b"OK":
                        return line
                    if expected == "send_ok" and line == b"SEND OK":
                        return line
                    if line in (b"ERROR", b"SEND FAIL"):
                        if fail_on_error:
                            self.stats.at_errors += 1
                            raise TestFailure(
                                f"AT returned {line.decode('ascii', 'replace')} while waiting for {expected}"
                            )
                        return line
            time.sleep(0.002)
        raise TestFailure(f"Timeout waiting for AT {expected} ({timeout:.1f}s)")

    def _command(
        self,
        command: str,
        timeout: float = 3.0,
        allow_error: bool = False,
        display: bool = True,
    ) -> bool:
        self._write_command(command, display=display)
        response = self._wait_for("ok", timeout, fail_on_error=not allow_error)
        return response == b"OK"

    def _echo_frame(self, frame: IpdFrame) -> None:
        link = self.stats.links[frame.link_id]
        self._command(f"AT+IPDACK={frame.frame_id}", timeout=2.0, display=False)
        link.ack_ok += 1

        self._write_command(
            f"AT+CIPSEND={frame.link_id},{len(frame.payload)}", display=False
        )
        self._wait_for("prompt", 2.0)
        self._write_at(
            frame.payload,
            f"> [payload link={frame.link_id} len={len(frame.payload)}]",
            display=False,
        )
        try:
            self._wait_for("send_ok", 5.0)
        except TestFailure:
            link.send_fail += 1
            raise
        link.echoed_frames += 1
        link.echoed_bytes += len(frame.payload)
        self._emit_log(
            f"echo OK link={frame.link_id} frame={frame.frame_id} len={len(frame.payload)}",
            display=False,
        )

        self.queued_frame_ids.discard(frame.frame_id)
        self.processed_ids.add(frame.frame_id)
        self.processed_order.append(frame.frame_id)
        while len(self.processed_order) > 8192:
            old_id = self.processed_order.popleft()
            self.processed_ids.discard(old_id)

    def _setup_logs(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path(self.config.output_root) / f"YED_IPD_{timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.at_log = (self.output_dir / "at_uart.bin").open("wb", buffering=0)
        self.cli_log = (self.output_dir / "cli_uart.log").open("wb", buffering=0)
        self.event_log = (self.output_dir / "events.log").open(
            "w", encoding="utf-8", buffering=1
        )
        (self.output_dir / "config.json").write_text(
            json.dumps(asdict(self.config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.notify("output_dir", str(self.output_dir))

    def _open_ports(self) -> None:
        self.at = serial.Serial(
            self.config.at_port,
            self.config.baudrate,
            timeout=0.02,
            write_timeout=2.0,
        )
        self.at.reset_input_buffer()
        self.at.reset_output_buffer()
        if self.config.cli_port:
            self.cli = serial.Serial(
                self.config.cli_port,
                self.config.baudrate,
                timeout=0.01,
                write_timeout=2.0,
            )
            self.cli.reset_input_buffer()
            self.cli.reset_output_buffer()
            self.cli.write(b"yed_ipd_debug 1\r\n")
            self.cli.flush()
            self._emit_log("CLI debug enabled")

    def _setup_board(self) -> None:
        self.notify("state", "CONFIGURING")
        self._command("AT", timeout=2.0)
        self._command("AT+CIPSERVER=0,1", timeout=2.0, allow_error=True)
        self._command("AT+CWMODE=2", timeout=5.0)
        self._command(
            f'AT+CWSAP="{self.config.ssid}","{self.config.password}",'
            f"{self.config.channel},3,{MAX_LINKS},0",
            timeout=8.0,
        )
        self._command(
            f'AT+CIPAP="{SOFTAP_IP}","{SOFTAP_IP}","{SOFTAP_MASK}"',
            timeout=5.0,
        )
        self._command("AT+CIPMUX=1", timeout=3.0)
        self._command(f"AT+CIPSERVERMAXCONN={MAX_LINKS}", timeout=3.0)
        self._command(
            f"AT+IPDRETRYCFG={self.config.ack_timeout_ms},{self.config.retry_count}",
            timeout=3.0,
        )
        self._command(f'AT+CIPSERVER=1,{SERVER_PORT},"TCP"', timeout=5.0)
        self.notify("state", "WAITING_FOR_DATA")
        self._emit_log(
            f"Ready: SoftAP {self.config.ssid}, TCP {SOFTAP_IP}:{SERVER_PORT}; "
            "waiting for clients"
        )

    def _snapshot(self) -> Dict[str, object]:
        now = time.monotonic()
        if self.first_data_monotonic is not None:
            self.stats.elapsed_seconds = now - self.first_data_monotonic
        return asdict(self.stats)

    def _complete_result(self) -> None:
        anomalies: List[str] = []
        active_data_links = sum(
            1 for link in self.stats.links if link.unique_frames > 0
        )
        if active_data_links < MIN_TEST_LINKS:
            anomalies.append(
                f"data received on only {active_data_links} link(s); "
                f"at least {MIN_TEST_LINKS} required"
            )
        if self.stats.retry_logs:
            anomalies.append(f"CLI retries={self.stats.retry_logs}")
        if self.stats.drop_logs:
            anomalies.append(f"CLI drops={self.stats.drop_logs}")
        for link_id, link in enumerate(self.stats.links):
            if link.duplicate_frames:
                anomalies.append(f"link{link_id} duplicate frames={link.duplicate_frames}")
            if link.crc_errors:
                anomalies.append(f"link{link_id} CRC errors={link.crc_errors}")
            if link.send_fail:
                anomalies.append(f"link{link_id} SEND FAIL={link.send_fail}")
        if anomalies:
            self.stats.result = "FAIL"
            self.stats.failure_reason = "; ".join(anomalies)
        else:
            self.stats.result = "PASS"

    def _write_summary(self) -> None:
        if self.output_dir is None:
            return
        snapshot = self._snapshot()
        lines = [
            f"YED IPD TCP Server Test {VERSION}",
            f"RESULT: {self.stats.result}",
            f"FAILURE_REASON: {self.stats.failure_reason}",
            f"STARTED_AT: {self.stats.started_at}",
            f"FIRST_DATA_AT: {self.stats.first_data_at}",
            f"ELAPSED_SECONDS: {self.stats.elapsed_seconds:.3f}",
            f"SOFTAP: {self.config.ssid} ({SOFTAP_IP})",
            f"TCP_SERVER_PORT: {SERVER_PORT}",
            f"MIN_DATA_LINKS: {MIN_TEST_LINKS}",
            f"CLI_RETRY_LOGS: {self.stats.retry_logs}",
            f"CLI_DROP_LOGS: {self.stats.drop_logs}",
            f"AT_ERRORS: {self.stats.at_errors}",
            f"PARSE_ERRORS: {self.stats.parse_errors}",
        ]
        for link_id, link in enumerate(self.stats.links):
            lines.append(
                f"LINK{link_id}: frames={link.frames} unique={link.unique_frames} "
                f"duplicates={link.duplicate_frames} bytes_rx={link.bytes_received} "
                f"ack_ok={link.ack_ok} echoed={link.echoed_frames} "
                f"bytes_echoed={link.echoed_bytes} send_fail={link.send_fail} "
                f"crc_errors={link.crc_errors} max_gap={link.max_gap_seconds:.3f}s"
            )
        (self.output_dir / "summary.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        (self.output_dir / "summary.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _cleanup(self) -> None:
        if self.at is not None and self.at.is_open:
            try:
                self._command("AT+CIPSERVER=0,1", timeout=2.0, allow_error=True)
            except Exception as exc:
                self._emit_log(f"Cleanup server stop failed: {exc}")
            try:
                self._command("AT+IPDRETRYCFG=5000,3", timeout=2.0, allow_error=True)
            except Exception as exc:
                self._emit_log(f"Cleanup retry config failed: {exc}")
        if self.cli is not None and self.cli.is_open:
            try:
                self.cli.write(b"yed_ipd_debug 0\r\n")
                self.cli.flush()
                self._emit_log("CLI debug disabled")
            except Exception as exc:
                self._emit_log(f"Cleanup CLI debug failed: {exc}")

        self._write_summary()
        for port in (self.at, self.cli):
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        for handle in (self.at_log, self.cli_log, self.event_log):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def run(self) -> None:
        try:
            self._setup_logs()
            self._open_ports()
            self._setup_board()

            duration_seconds = self.config.duration_minutes * 60.0
            while not self.stop_event.is_set():
                self._read_serial()
                while self.serial_events:
                    event = self.serial_events.popleft()
                    self._handle_event(event)

                if self.pending_frames:
                    frame = self.pending_frames.popleft()
                    self._echo_frame(frame)

                # A response read can also contain a following +IPD frame.
                while self.serial_events:
                    self._handle_event(self.serial_events.popleft())

                now = time.monotonic()
                if now - self.last_snapshot >= 1.0:
                    self.last_snapshot = now
                    self.notify("stats", self._snapshot())

                if (
                    self.first_data_monotonic is not None
                    and now - self.first_data_monotonic >= duration_seconds
                ):
                    self._complete_result()
                    break
                time.sleep(0.002)

            if self.stats.result == "RUNNING":
                self.stats.result = "STOPPED"
        except Exception as exc:
            self.stats.result = "FAIL"
            self.stats.failure_reason = str(exc)
            self._emit_log(f"FAIL: {exc}")
        finally:
            self.notify("stats", self._snapshot())
            self._cleanup()
            self.notify(
                "finished",
                {
                    "result": self.stats.result,
                    "reason": self.stats.failure_reason,
                    "output_dir": str(self.output_dir or ""),
                },
            )


class TestApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("1040x720")
        self.minsize(900, 620)
        self.events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.stop_event: Optional[threading.Event] = None
        self.worker: Optional[threading.Thread] = None
        self.output_dir = ""
        self._build_ui()
        self._refresh_ports()
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        ports = ttk.LabelFrame(root, text="Serial ports", padding=10)
        ports.grid(row=0, column=0, sticky="ew")
        ports.columnconfigure(1, weight=1)
        ports.columnconfigure(3, weight=1)
        ttk.Label(ports, text="AT port").grid(row=0, column=0, sticky="w")
        self.at_port = ttk.Combobox(ports, state="readonly")
        self.at_port.grid(row=0, column=1, sticky="ew", padx=(8, 18))
        ttk.Label(ports, text="CLI port (optional)").grid(row=0, column=2, sticky="w")
        self.cli_port = ttk.Combobox(ports, state="readonly")
        self.cli_port.grid(row=0, column=3, sticky="ew", padx=(8, 8))
        ttk.Button(ports, text="Refresh", command=self._refresh_ports).grid(
            row=0, column=4
        )

        settings = ttk.LabelFrame(root, text="Test settings", padding=10)
        settings.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for column in (1, 3, 5):
            settings.columnconfigure(column, weight=1)

        self.ssid = tk.StringVar(value="YED_IPD_TEST")
        self.password = tk.StringVar(value="12345678")
        self.channel = tk.StringVar(value="6")
        self.duration = tk.StringVar(value="30")
        self.output_root = tk.StringVar(value=str(Path.home() / "Documents"))

        ttk.Label(settings, text="SoftAP SSID").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.ssid).grid(
            row=0, column=1, sticky="ew", padx=(8, 18)
        )
        ttk.Label(settings, text="Password").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings, textvariable=self.password, show="*").grid(
            row=0, column=3, sticky="ew", padx=(8, 18)
        )
        ttk.Label(settings, text="Channel").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.channel,
            values=[str(value) for value in range(1, 14)],
            state="readonly",
            width=6,
        ).grid(row=0, column=5, sticky="w", padx=(8, 0))

        ttk.Label(settings, text="Duration (minutes)").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Entry(settings, textvariable=self.duration).grid(
            row=1, column=1, sticky="ew", padx=(8, 18), pady=(10, 0)
        )
        ttk.Label(settings, text="Server").grid(
            row=1, column=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(settings, text=f"{SOFTAP_IP}:{SERVER_PORT} (TCP)").grid(
            row=1, column=3, sticky="w", padx=(8, 18), pady=(10, 0)
        )
        ttk.Label(settings, text="Output folder").grid(
            row=1, column=4, sticky="w", pady=(10, 0)
        )
        output_frame = ttk.Frame(settings)
        output_frame.grid(row=1, column=5, sticky="ew", padx=(8, 0), pady=(10, 0))
        output_frame.columnconfigure(0, weight=1)
        ttk.Entry(output_frame, textvariable=self.output_root).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(output_frame, text="Browse", command=self._choose_output).grid(
            row=0, column=1, padx=(6, 0)
        )

        controls = ttk.Frame(root, padding=(0, 10))
        controls.grid(row=2, column=0, sticky="ew")
        self.start_button = ttk.Button(controls, text="Start", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            controls, text="Stop", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(
            controls, text="Open logs", command=self._open_logs, state="disabled"
        )
        self.open_button.pack(side="left", padx=(8, 0))
        self.state_label = ttk.Label(controls, text="IDLE")
        self.state_label.pack(side="right")

        notebook = ttk.Notebook(root)
        notebook.grid(row=3, column=0, sticky="nsew")

        stats_tab = ttk.Frame(notebook, padding=8)
        stats_tab.rowconfigure(1, weight=1)
        stats_tab.columnconfigure(0, weight=1)
        notebook.add(stats_tab, text="Statistics")
        self.summary_label = ttk.Label(
            stats_tab, text="Elapsed: 0s    Retry: 0    Drop: 0    AT errors: 0"
        )
        self.summary_label.grid(row=0, column=0, sticky="w", pady=(0, 8))
        columns = (
            "connected",
            "frames",
            "unique",
            "duplicates",
            "bytes_rx",
            "ack",
            "echoed",
            "bytes_echoed",
            "max_gap",
        )
        self.stats_tree = ttk.Treeview(
            stats_tab, columns=columns, show="tree headings", height=8
        )
        self.stats_tree.heading("#0", text="Link")
        headings = {
            "connected": "Connected",
            "frames": "Frames",
            "unique": "Unique",
            "duplicates": "Retries",
            "bytes_rx": "RX bytes",
            "ack": "ACK OK",
            "echoed": "Echo OK",
            "bytes_echoed": "Echo bytes",
            "max_gap": "Max gap",
        }
        self.stats_tree.column("#0", width=65, anchor="center", stretch=False)
        for name, heading in headings.items():
            self.stats_tree.heading(name, text=heading)
            self.stats_tree.column(name, width=95, anchor="center")
        for link_id in range(MAX_LINKS):
            self.stats_tree.insert("", "end", iid=str(link_id), text=str(link_id))
        self.stats_tree.grid(row=1, column=0, sticky="nsew")

        log_tab = ttk.Frame(notebook, padding=8)
        log_tab.rowconfigure(0, weight=1)
        log_tab.columnconfigure(0, weight=1)
        notebook.add(log_tab, text="Events")
        self.log_text = tk.Text(log_tab, wrap="none", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_tab, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _refresh_ports(self) -> None:
        current_at = self.at_port.get()
        current_cli = self.cli_port.get()
        devices = [port.device for port in list_ports.comports()]
        self.at_port["values"] = devices
        self.cli_port["values"] = [""] + devices
        if current_at in devices:
            self.at_port.set(current_at)
        elif devices:
            self.at_port.set(devices[0])
        if current_cli in devices:
            self.cli_port.set(current_cli)
        else:
            self.cli_port.set("")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_root.get())
        if selected:
            self.output_root.set(selected)

    def _validate_config(self) -> TestConfig:
        at_port = self.at_port.get().strip()
        cli_port = self.cli_port.get().strip()
        if not at_port:
            raise ValueError("Select the AT serial port")
        if cli_port and cli_port == at_port:
            raise ValueError("AT and CLI ports must be different")
        ssid = self.ssid.get().strip()
        password = self.password.get()
        if not 1 <= len(ssid.encode("utf-8")) <= 32:
            raise ValueError("SoftAP SSID must be 1 to 32 bytes")
        if not 8 <= len(password.encode("utf-8")) <= 63:
            raise ValueError("SoftAP password must be 8 to 63 bytes")
        if '"' in ssid or '"' in password:
            raise ValueError("SSID and password cannot contain double quotes")
        duration = float(self.duration.get())
        if duration <= 0 or duration > 24 * 60:
            raise ValueError("Duration must be between 0 and 1440 minutes")
        output_root = Path(self.output_root.get()).expanduser()
        output_root.mkdir(parents=True, exist_ok=True)
        return TestConfig(
            at_port=at_port,
            cli_port=cli_port,
            baudrate=115200,
            ssid=ssid,
            password=password,
            channel=int(self.channel.get()),
            duration_minutes=duration,
            ack_timeout_ms=5000,
            retry_count=3,
            output_root=str(output_root),
        )

    def _start(self) -> None:
        try:
            config = self._validate_config()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for link_id in range(MAX_LINKS):
            self.stats_tree.item(str(link_id), values=("No", 0, 0, 0, 0, 0, 0, 0, "0.000s"))
        self.summary_label.configure(
            text="Elapsed: 0s    Retry: 0    Drop: 0    AT errors: 0"
        )
        self.output_dir = ""
        self.stop_event = threading.Event()
        runner = TestRunner(config, self.stop_event, self._notify)
        self.worker = threading.Thread(target=runner.run, daemon=True)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.state_label.configure(text="STARTING")
        self.worker.start()

    def _stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
            self.stop_button.configure(state="disabled")
            self.state_label.configure(text="STOPPING")

    def _notify(self, kind: str, payload: object) -> None:
        self.events.put((kind, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", str(payload) + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "state":
                    self.state_label.configure(text=str(payload))
                elif kind == "output_dir":
                    self.output_dir = str(payload)
                elif kind == "stats":
                    self._update_stats(payload)
                elif kind == "finished":
                    self._finished(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _update_stats(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        links = payload.get("links", [])
        for link_id, link in enumerate(links):
            self.stats_tree.item(
                str(link_id),
                values=(
                    "Yes" if link["connected"] else "No",
                    link["frames"],
                    link["unique_frames"],
                    link["duplicate_frames"],
                    link["bytes_received"],
                    link["ack_ok"],
                    link["echoed_frames"],
                    link["echoed_bytes"],
                    f"{link['max_gap_seconds']:.3f}s",
                ),
            )
        self.summary_label.configure(
            text=(
                f"Elapsed: {payload.get('elapsed_seconds', 0):.1f}s    "
                f"Retry: {payload.get('retry_logs', 0)}    "
                f"Drop: {payload.get('drop_logs', 0)}    "
                f"AT errors: {payload.get('at_errors', 0)}"
            )
        )

    def _finished(self, payload: object) -> None:
        details = payload if isinstance(payload, dict) else {}
        result = details.get("result", "UNKNOWN")
        reason = details.get("reason", "")
        self.state_label.configure(text=str(result))
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.open_button.configure(state="normal" if self.output_dir else "disabled")
        message = f"Result: {result}\nLogs: {details.get('output_dir', '')}"
        if reason:
            message += f"\nReason: {reason}"
        if result == "FAIL":
            messagebox.showerror(APP_NAME, message, parent=self)
        else:
            messagebox.showinfo(APP_NAME, message, parent=self)

    def _open_logs(self) -> None:
        if not self.output_dir:
            return
        try:
            os.startfile(self.output_dir)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                APP_NAME, "The test is running. Stop it and exit?", parent=self
            ):
                return
            if self.stop_event is not None:
                self.stop_event.set()
            self.after(200, self._wait_close)
            return
        self.destroy()

    def _wait_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.after(200, self._wait_close)
        else:
            self.destroy()


if __name__ == "__main__":
    TestApp().mainloop()
