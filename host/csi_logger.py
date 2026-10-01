#!/usr/bin/env python3
"""Record text CSI frames from csi_rx into CSV; requires pyserial."""

import argparse
import csv
import io
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import serial


HEADER = ("timestamp", "seq", "mac", "rssi", "noise_floor", "channel", "len", "first_word_invalid", "data")


def parse_line(line: str) -> tuple[int, str, int, int, int, int, int, str] | None:
    """Parse one complete CSI_DATA line; ignore logs and damaged frames."""
    if not line.startswith("CSI_DATA,"):
        return None
    try:
        fields = next(csv.reader(io.StringIO(line), strict=True))
        if len(fields) != 9 or fields[0] != "CSI_DATA":
            return None
        seq, mac, rssi, noise, channel, length, invalid = (
            int(fields[1]), fields[2], int(fields[3]), int(fields[4]),
            int(fields[5]), int(fields[6]), int(fields[7]),
        )
        samples = json.loads(fields[8])
        if (seq < 0 or length < 0 or invalid not in (0, 1)
                or len(mac.split(":")) != 6 or not isinstance(samples, list)
                or len(samples) != length or any(type(value) is not int for value in samples)):
            return None
    except (csv.Error, ValueError, TypeError, StopIteration):
        return None
    return seq, mac, rssi, noise, channel, length, invalid, fields[8]


@dataclass
class CaptureStats:
    frames: int = 0
    seq_drops: int = 0
    firmware_drops: int = 0
    initial_firmware_drops: int | None = None
    prev_seq: int | None = None
    rssi: int | None = None

    def record_frame(self, seq: int, rssi: int) -> None:
        if self.prev_seq is not None and seq > self.prev_seq + 1:
            self.seq_drops += seq - self.prev_seq - 1
        self.prev_seq = seq
        self.rssi = rssi
        self.frames += 1

    def record_dropped(self, count: int) -> None:
        self.firmware_drops = count

    @property
    def drops(self) -> int:
        return self.seq_drops + self.firmware_drops

    @property
    def drop_rate(self) -> float:
        total = self.frames + self.drops
        return self.drops / total * 100 if total else 0.0


def capture(port: str, baud: int, duration: float, output: Path) -> None:
    stats = CaptureStats()
    started = time.monotonic()
    next_report = started + 1
    last_report = started
    last_frames = 0
    try:
        with serial.Serial(port, baudrate=baud, timeout=0.2) as connection:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(HEADER)
                file.flush()
                try:
                    while True:
                        now = time.monotonic()
                        elapsed = now - started
                        if duration > 0 and elapsed >= duration:
                            break
                        if now >= next_report:
                            rate = (stats.frames - last_frames) / (now - last_report)
                            label = f"{duration:g}s" if duration > 0 else "∞"
                            rssi = f"{stats.rssi} dBm" if stats.rssi is not None else "N/A"
                            print(f"[Elapsed: {elapsed:.1f}s / {label}] Frames: {stats.frames} | "
                                  f"Rate: {rate:.1f} Hz | Drops: {stats.drops} "
                                  f"({stats.drop_rate:.1f}%) | RSSI: {rssi}", flush=True)
                            file.flush()
                            last_frames, last_report = stats.frames, now
                            next_report = now + 1
                        line = connection.readline().decode("utf-8", errors="replace").strip()
                        frame = parse_line(line)
                        if frame is not None:
                            writer.writerow((datetime.now(timezone.utc).isoformat(), *frame))
                            stats.record_frame(frame[0], frame[2])
                        elif line.startswith("CSI_DROPPED,"):
                            try:
                                count = int(line.split(",", 1)[1])
                                if count >= 0:
                                    stats.record_dropped(count)
                            except ValueError:
                                continue
                except KeyboardInterrupt:
                    pass
                finally:
                    file.flush()
    finally:
        elapsed = time.monotonic() - started
        size = output.stat().st_size if output.exists() else 0
        print("\n=== CAPTURE SUMMARY ===")
        print(f"Duration: {elapsed:.1f} seconds")
        print(f"Total frames received: {stats.frames}")
        print(f"Average sample rate: {stats.frames / elapsed if elapsed else 0:.1f} Hz")
        print(f"Total dropped frames: {stats.drops} ({stats.drop_rate:.1f}%)")
        print(f"Firmware buffer drops: {stats.firmware_drops}")
        print(f"Output file: {output}")
        print(f"File size: {size} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture CSI_DATA from ESP32 serial into CSV")
    parser.add_argument("--port", default="/dev/ttyACM1")
    parser.add_argument("--baud", type=int, default=921600)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--output", type=Path, default=Path("/root/workspaces/esp32/data") /
                        f"csi_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    args = parser.parse_args()
    if args.baud <= 0:
        parser.error("--baud must be positive")
    capture(args.port, args.baud, args.duration, args.output)


if __name__ == "__main__":
    main()
