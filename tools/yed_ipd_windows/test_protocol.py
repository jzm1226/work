import unittest

from protocol import AtStreamParser, crc16_modbus


class ProtocolTests(unittest.TestCase):
    def test_modbus_crc_matches_firmware_vector(self):
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)

    def test_fragmented_binary_frame_and_text(self):
        crc = crc16_modbus(b"+IPD,1,7,42")
        wire = f"\r\n+IPD,1,7,42,{crc:04X}:".encode() + b"ab\r\n>cd" + b"\r\nOK\r\n"
        parser = AtStreamParser()
        events = []
        for offset in range(0, len(wire), 3):
            events.extend(parser.feed(wire[offset:offset + 3]))
        frames = [event.frame for event in events if event.kind == "frame"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].link_id, 1)
        self.assertEqual(frames[0].frame_id, 42)
        self.assertEqual(frames[0].payload, b"ab\r\n>cd")
        self.assertTrue(frames[0].crc_valid)
        lines = [event.line for event in events if event.kind == "line"]
        self.assertIn(b"OK", lines)

    def test_remote_info_header(self):
        crc = crc16_modbus(b"+IPD,4,4,99")
        wire = (
            f'+IPD,4,4,99,{crc:04X},"192.168.4.2",12345:'.encode()
            + b"data"
        )
        events = AtStreamParser().feed(wire)
        frame = next(event.frame for event in events if event.kind == "frame")
        self.assertEqual(frame.payload, b"data")
        self.assertTrue(frame.crc_valid)


if __name__ == "__main__":
    unittest.main()
