"""Protocol and recording checks; run with python3 host/test_csi_host.py."""

import csv
import json
import random
import struct
import tempfile
import unittest
from pathlib import Path

from csi_host_reader import Frame, FrameParser, RecordWriter, crc16


def wire(iq=(-128, -1, 0, 127), *, rx_seq=7):
    header = b"\xaa\x55\x01\x20" + struct.pack(
        "<III6sbbBBIH", rx_seq, 13, 123456, bytes.fromhex("3030f9673434"),
        -62, -95, 6, 1, 3, len(iq),
    )
    body = header + struct.pack(f"<{len(iq)}b", *iq)
    return body + struct.pack("<H", crc16(body))


class HostTests(unittest.TestCase):
    def test_valid_frame_encode_decode(self):
        # Given: known firmware-layout frame.
        raw = wire()
        # When: parser consumes wire bytes.
        frames = FrameParser().feed(raw)
        # Then: all fields retain wire values.
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], Frame(7, 13, 123456, "30:30:f9:67:34:34", -62,
                                          -95, 6, 1, 3, 4, (-128, -1, 0, 127)))

    def test_noise_partial_frames_and_resync(self):
        # Given: seeded noise, truncated candidate, and two split valid frames.
        rng = random.Random(13)
        noise = bytes(rng.randrange(256) for _ in range(23))
        stream = noise + b"\xaa\x55\x02\x20" + wire(rx_seq=9) + wire(rx_seq=10)
        parser = FrameParser()
        # When: transport divides stream at arbitrary byte boundaries.
        frames = []
        for offset in range(0, len(stream), 3):
            frames.extend(parser.feed(stream[offset:offset + 3]))
        # Then: both valid frames recovered without inserted samples.
        self.assertEqual([frame.rx_seq for frame in frames], [9, 10])
        self.assertGreaterEqual(parser.corrupted, 1)

    def test_crc_rejects_corrupted_frame_and_recovers(self):
        # Given: payload corruption followed by intact frame.
        invalid = bytearray(wire())
        invalid[33] ^= 1
        parser = FrameParser()
        # When: parser reads both frames.
        frames = parser.feed(bytes(invalid) + wire(rx_seq=8))
        # Then: bad frame rejected; good frame retained.
        self.assertEqual([frame.rx_seq for frame in frames], [8])
        self.assertEqual(parser.corrupted, 1)

    def test_negative_iq_components(self):
        # Given: signed extremes in imaginary-real order.
        raw = wire(iq=(-128, 127, -1, 0))
        # When: stream is decoded.
        frame = FrameParser().feed(raw)[0]
        # Then: negative bytes remain negative and order stays intact.
        self.assertEqual(frame.csi_iq, (-128, 127, -1, 0))

    def test_recording_roundtrip_csv_and_jsonl(self):
        # Given: one true decoded frame and fixed session metadata.
        frame = FrameParser().feed(wire())[0]
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ("csv", "jsonl"):
                path = Path(directory) / f"session.{suffix}"
                # When: session records frame in selected format.
                with RecordWriter(path, "test-port", 921600, "2026-01-02T03:04:05Z") as writer:
                    writer.write(frame)
                # Then: metadata and exact frame fields survive round-trip.
                with path.open(newline="", encoding="utf-8") as source:
                    if suffix == "csv":
                        rows = list(csv.DictReader(source))
                        self.assertEqual(len(rows), 2)
                        self.assertEqual(rows[0]["type"], "session")
                        self.assertEqual(rows[0]["rx_seq"], "")
                        record = rows[1]
                        self.assertEqual(record["port"], "test-port")
                    else:
                        lines = [json.loads(line) for line in source]
                        self.assertEqual(lines[0]["port"], "test-port")
                        record = lines[1]
                    self.assertEqual(str(record["rx_seq"]), "7")
                    self.assertEqual(str(record["rx_drops"]), "3")
                    self.assertEqual(record["csi_iq"], "-128 -1 0 127")


if __name__ == "__main__":
    unittest.main()
