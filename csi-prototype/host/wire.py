"""Bounded CSI UART framing, independent of serial hardware."""

import struct
import zlib

MAGIC = b"CSI1"
MAX_PAYLOAD = 512
PREFIX = struct.Struct("<6s6sIQHBBbB")
STATS = struct.Struct("<6sIIIIII")
COMMAND = struct.Struct("<IBHB")
ACK = struct.Struct("<IB")


def encode(kind: int, payload: bytes) -> bytes:
    if kind not in (1, 2, 3, 4) or len(payload) > MAX_PAYLOAD:
        raise ValueError("invalid frame")
    header = MAGIC + struct.pack("<BBH", 1, kind, len(payload)) + payload
    return header + struct.pack("<I", zlib.crc32(header))


class Parser:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes, bytes]]:
        self.buffer.extend(data)
        found = []
        while len(self.buffer) >= 8:
            pos = self.buffer.find(MAGIC)
            if pos < 0:
                del self.buffer[:-3]
                break
            if pos:
                del self.buffer[:pos]
            if len(self.buffer) < 8:
                break
            version, kind, size = struct.unpack_from("<BBH", self.buffer, 4)
            if version != 1 or kind not in (1, 2, 3, 4) or size > MAX_PAYLOAD:
                self.errors += 1
                del self.buffer[0]
                continue
            total = 12 + size
            if len(self.buffer) < total:
                break
            raw = bytes(self.buffer[:total])
            if zlib.crc32(raw[:-4]) != struct.unpack_from("<I", raw, total - 4)[0]:
                self.errors += 1
                del self.buffer[0]
                continue
            found.append((kind, raw[8:-4], raw))
            del self.buffer[:total]
        return found


def mac(raw: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in raw)


def parse_csi(payload: bytes, source: str, receiver: str) -> dict:
    if len(payload) < PREFIX.size:
        raise ValueError("truncated CSI")
    src, rx, seq, capture_us, length, invalid, ltf, rssi, channel = PREFIX.unpack_from(payload)
    if (len(payload) != PREFIX.size + length or length > 384 or length % 2
            or invalid not in (0, 1) or mac(src) != source or mac(rx) != receiver):
        raise ValueError("invalid CSI length, layout or identity")
    return {"source_mac": mac(src), "rx_mac": mac(rx), "rx_seq": seq,
            "capture_us": capture_us, "csi_len": length, "first_word_invalid": bool(invalid),
            "ltf": ltf, "rssi": rssi, "channel": channel,
            "pairs": [(int.from_bytes(payload[PREFIX.size+i:PREFIX.size+i+1], "little", signed=True),
                       int.from_bytes(payload[PREFIX.size+i+1:PREFIX.size+i+2], "little", signed=True))
                      for i in range(0, length, 2)]}
