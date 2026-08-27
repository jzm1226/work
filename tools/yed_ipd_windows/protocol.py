"""Protocol helpers for the YED reliable +IPD UART stream."""

from dataclasses import dataclass
import re
from typing import List, Optional


IPD_HEADER_RE = re.compile(
    rb"\+IPD,(\d+),(\d+),(\d+),([0-9A-Fa-f]{4})"
    rb'(?:,"[^"]+",\d+)?:'
)


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


@dataclass(frozen=True)
class IpdFrame:
    link_id: int
    frame_id: int
    payload: bytes
    received_crc: int
    expected_crc: int

    @property
    def crc_valid(self) -> bool:
        return self.received_crc == self.expected_crc


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    line: bytes = b""
    frame: Optional[IpdFrame] = None


class AtStreamParser:
    """Split mixed AT response text, prompts, and binary +IPD frames."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> List[StreamEvent]:
        if data:
            self.buffer.extend(data)
        events: List[StreamEvent] = []

        while self.buffer:
            if self.buffer.startswith(b"+IPD,"):
                match = IPD_HEADER_RE.match(self.buffer)
                if match is None:
                    if b":" not in self.buffer and len(self.buffer) < 192:
                        break
                    events.append(StreamEvent("text", line=bytes(self.buffer[:1])))
                    del self.buffer[:1]
                    continue

                payload_length = int(match.group(2))
                total_length = match.end() + payload_length
                if len(self.buffer) < total_length:
                    break

                link_id = int(match.group(1))
                frame_id = int(match.group(3))
                received_crc = int(match.group(4), 16)
                crc_input = (
                    f"+IPD,{link_id},{payload_length},{frame_id}".encode("ascii")
                )
                payload = bytes(self.buffer[match.end():total_length])
                del self.buffer[:total_length]
                events.append(
                    StreamEvent(
                        "frame",
                        frame=IpdFrame(
                            link_id=link_id,
                            frame_id=frame_id,
                            payload=payload,
                            received_crc=received_crc,
                            expected_crc=crc16_modbus(crc_input),
                        ),
                    )
                )
                continue

            ipd_index = self.buffer.find(b"+IPD,")
            scan_limit = ipd_index if ipd_index >= 0 else len(self.buffer)
            newline_index = self.buffer.find(b"\n", 0, scan_limit)
            prompt_index = self.buffer.find(b">", 0, scan_limit)

            if newline_index >= 0 and (
                prompt_index < 0 or newline_index < prompt_index
            ):
                line = bytes(self.buffer[:newline_index + 1])
                del self.buffer[:newline_index + 1]
                events.append(StreamEvent("line", line=line.rstrip(b"\r\n")))
                continue

            if prompt_index >= 0:
                prefix = bytes(self.buffer[:prompt_index])
                del self.buffer[:prompt_index + 1]
                if prefix.strip(b"\r\n "):
                    events.append(StreamEvent("text", line=prefix))
                events.append(StreamEvent("prompt", line=b">"))
                continue

            if ipd_index > 0:
                prefix = bytes(self.buffer[:ipd_index])
                del self.buffer[:ipd_index]
                if prefix:
                    events.append(StreamEvent("text", line=prefix))
                continue

            if len(self.buffer) > 4096:
                # Retain enough bytes to recognize a marker split across reads.
                prefix = bytes(self.buffer[:-16])
                del self.buffer[:-16]
                events.append(StreamEvent("text", line=prefix))
                continue
            break

        return events
