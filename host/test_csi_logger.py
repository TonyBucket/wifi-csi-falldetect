"""Run with python3 host/test_csi_logger.py."""

import csv
import unittest

from csi_logger import CaptureStats, parse_line


class LoggerTests(unittest.TestCase):
    def test_parse_frame_when_valid(self):
        # Given: firmware CSI line with quoted comma-separated samples.
        line = 'CSI_DATA,105,1a:00:00:00:00:00,-42,-92,11,4,1,"[10,-5,2,4]"'
        # When: line is parsed.
        result = parse_line(line)
        # Then: CSI stays one CSV cell and metadata stays typed.
        self.assertEqual(result, (105, "1a:00:00:00:00:00", -42, -92, 11, 4, 1, "[10,-5,2,4]"))

    def test_ignore_malformed_lines_when_serial_contains_logs(self):
        # Given: unrelated boot logs and incomplete or inconsistent samples.
        lines = ["boot", "CSI_DATA,1,broken", 'CSI_DATA,1,aa,-42,-92,11,4,1,"[1,2]"']
        # When: lines are parsed.
        results = [parse_line(line) for line in lines]
        # Then: no invalid line becomes a frame.
        self.assertEqual(results, [None, None, None])

    def test_count_gaps_and_cumulative_firmware_drops(self):
        # Given: sequence gap and repeated firmware cumulative report.
        stats = CaptureStats()
        # When: packets and firmware reports arrive.
        stats.record_frame(105, -42)
        stats.record_frame(108, -40)
        stats.record_dropped(3)
        stats.record_dropped(3)
        stats.record_dropped(5)
        # Then: sequence losses plus new reported losses, not repeated totals.
        self.assertEqual((stats.frames, stats.drops, stats.drop_rate, stats.rssi), (2, 7, 7 / 9 * 100, -40))


if __name__ == "__main__":
    unittest.main()
