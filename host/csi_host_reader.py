"""Read firmware CSI frames from serial and record unmodified decoded measurements."""

from __future__ import annotations

import argparse
import csv
import json
import signal
import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


MAGIC = b"\xaa\x55"
HEADER_LEN = 32
MAX_CSI_LEN = 384  # firmware/common/csi_protocol.h: CSI_MAX_LEN
HEADER = struct.Struct("<2sBBIII6sbbBBIH")
FIELDS = ("rx_seq", "tx_seq", "timestamp_us", "tx_mac", "rssi", "noise_floor",
          "channel", "first_word_invalid", "rx_drops", "csi_len", "csi_iq")


@dataclass(frozen=True, slots=True)
class Frame:
    rx_seq: int
    tx_seq: int
    timestamp_us: int
    tx_mac: str
    rssi: int
    noise_floor: int
    channel: int
    first_word_invalid: int
    rx_drops: int
    csi_len: int
    csi_iq: tuple[int, ...]


def crc16(data: bytes | bytearray) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, initial value 0xffff."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


class FrameParser:
    """Incremental sliding-window parser; corrupted counts rejected candidates."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.corrupted = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer.extend(data)
        frames: list[Frame] = []
        while True:
            start = self.buffer.find(MAGIC)
            if start < 0:
                # Retain possible first magic byte across serial reads.
                self.buffer[:] = self.buffer[-1:] if self.buffer.endswith(MAGIC[:1]) else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < HEADER_LEN:
                break
            _magic, version, header_len, rx_seq, tx_seq, timestamp_us, mac, rssi, \
                noise_floor, channel, first_word_invalid, rx_drops, length = HEADER.unpack_from(self.buffer)
            if (version != 1 or header_len != HEADER_LEN or length == 0
                    or length > MAX_CSI_LEN or length % 2 or first_word_invalid > 1):
                self.corrupted += 1
                del self.buffer[0]
                continue
            total = HEADER_LEN + length + 2
            if len(self.buffer) < total:
                break
            expected = struct.unpack_from("<H", self.buffer, total - 2)[0]
            if crc16(self.buffer[:total - 2]) != expected:
                self.corrupted += 1
                del self.buffer[0]
                continue
            iq = struct.unpack_from(f"<{length}b", self.buffer, HEADER_LEN)
            frames.append(Frame(rx_seq, tx_seq, timestamp_us,
                                ":".join(f"{byte:02x}" for byte in mac), rssi,
                                noise_floor, channel, first_word_invalid, rx_drops, length, iq))
            del self.buffer[:total]
        return frames


class RecordWriter:
    """Own output file; CSV repeats metadata, JSONL writes one metadata header."""

    def __init__(self, path: Path, port: str, baud: int, started_at: str) -> None:
        if path.suffix.lower() not in (".csv", ".jsonl"):
            raise ValueError("output must have .csv or .jsonl extension")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8", newline="")
        self.metadata = {"started_at": started_at, "port": port, "baud": baud}
        self.csv_writer = None
        if path.suffix.lower() == ".csv":
            self.csv_writer = csv.DictWriter(self.file, fieldnames=("type", *self.metadata, *FIELDS))
            self.csv_writer.writeheader()
            self.csv_writer.writerow({"type": "session", **self.metadata})
        else:
            self.file.write(json.dumps({"type": "session", **self.metadata}) + "\n")

    def write(self, frame: Frame) -> None:
        record = asdict(frame)
        record["csi_iq"] = " ".join(map(str, frame.csi_iq))
        if self.csv_writer is not None:
            self.csv_writer.writerow({"type": "frame", **self.metadata, **record})
        else:
            self.file.write(json.dumps({"type": "frame", **record}) + "\n")

    def __enter__(self) -> RecordWriter:
        return self

    def __exit__(self, *_: type | None) -> None:
        self.file.close()  # close flushes buffered rows, including on SIGTERM.


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture real ESP32 CSI serial frames")
    parser.add_argument("--port", help="serial port (auto-detect if exactly one /dev/ttyACM*)")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--output", type=Path, help=".csv or .jsonl (default: data/session_<UTC>.csv)")
    parser.add_argument("--duration", type=float, help="capture duration in seconds")
    parser.add_argument("--min-frames", type=int, default=0, help="exit nonzero if fewer frames captured")
    args = parser.parse_args()
    if args.baud <= 0 or (args.duration is not None and args.duration < 0):
        parser.error("--baud must be positive and --duration must be nonnegative")
    ports = sorted(list(Path("/dev").glob("ttyACM*")) + list(Path("/dev").glob("ttyUSB*"))) if args.port is None else []
    if args.port is None and len(ports) != 1:
        parser.error("specify --port (auto-detect requires exactly one /dev/ttyACM*)")
    port = args.port or str(ports[0])
    started_at = datetime.now(timezone.utc).isoformat()
    output = args.output or Path("data") / f"session_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.csv"

    import serial  # pyserial is only required when connecting, not for offline parsing/tests.

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    decoded = FrameParser()
    count = 0
    window_count = 0
    last_frame: float | None = None
    latest: Frame | None = None
    start = time.monotonic()
    last_report = start
    disconnected = False
    try:
        with serial.Serial(port, args.baud, timeout=0.2) as connection, \
                RecordWriter(output, port, args.baud, started_at) as writer:
            print(f"Recording {port} at {args.baud} baud to {output}", flush=True)
            while not stopping:
                now = time.monotonic()
                if args.duration is not None and now - start >= args.duration:
                    break
                try:
                    chunk = connection.read(4096)
                except (serial.SerialException, OSError):
                    disconnected = True
                    break
                now = time.monotonic()
                for frame in decoded.feed(chunk):
                    writer.write(frame)
                    count += 1
                    window_count += 1
                    last_frame = now
                    latest = frame
                if now - last_report >= 1:
                    age = now - last_frame if last_frame is not None else now - start
                    status = "[STREAMING OK]" if age < 2 else "[IDLE / NO CSI - TX OFF?]"
                    rssi = latest.rssi if latest is not None else "--"
                    mac = latest.tx_mac if latest is not None else "--"
                    drops = latest.rx_drops if latest is not None else 0
                    print(f"{status} valid={count} rate={window_count / (now - last_report):.1f} Hz "
                          f"corrupted={decoded.corrupted} driver_drops={drops} "
                          f"elapsed={now - start:.1f}s last_frame={age:.1f}s "
                          f"rssi={rssi} tx_mac={mac}", flush=True)
                    window_count = 0
                    last_report = now
    except (serial.SerialException, OSError) as error:
        disconnected = True
        print(f"Serial/output error: {error}")
    finally:
        now = time.monotonic()
        age = now - last_frame if last_frame is not None else now - start
        status = "[PORT DISCONNECTED]" if disconnected else "[SESSION ENDED]"
        print(f"{status} valid={count} corrupted={decoded.corrupted} "
              f"driver_drops={latest.rx_drops if latest is not None else 0} "
              f"elapsed={now - start:.1f}s last_frame={age:.1f}s output={output}", flush=True)
    if disconnected:
        return 1
    if args.min_frames > 0 and count < args.min_frames:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
