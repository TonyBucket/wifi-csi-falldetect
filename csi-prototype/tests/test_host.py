import asyncio
import base64
import json
import struct
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from app import Hub, make_app
from wire import ACK, COMMAND, PREFIX, Parser, encode, parse_csi

SOURCE = bytes.fromhex("98c377b42244")
RX = bytes.fromhex("3030f9673434")

@pytest.fixture(autouse=True)
def isolate_audit_files(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA", tmp_path)


def sample(seq=1, invalid=0, values=b"\x01\x02\x03\x04\x05\x06"):
    return encode(1, PREFIX.pack(SOURCE, RX, seq, seq * 10000, len(values), invalid, 1, -40, 11) + values)


def test_parser_resynchronizes_after_noise_bad_crc_and_invalid_length():
    good = sample()
    bad = bytearray(good)
    bad[-1] ^= 1
    oversized = b"CSI1" + struct.pack("<BBH", 1, 1, 513)
    parser = Parser()
    assert parser.feed(b"garbage" + oversized + bad + good[:7]) == []
    assert [raw for _, _, raw in parser.feed(good[7:])] == [good]
    assert parser.errors == 2
    assert len(parser.buffer) <= 527


def test_golden_vectors_round_trip_fixed_wire_contract():
    given = {
        1: (encode(1, PREFIX.pack(SOURCE, RX, 1, 10000, 6, 1, 0, -40, 11) + bytes([1, 2, 3, 4, 5, 6])), "435349310101240098c377b422443030f967343401000000102700000000000006000100d80b010203040506a27d61fd"),
        2: (encode(2, struct.pack("<6sIIIIII", RX, 1, 1, 0, 0, 0, 0)), "4353493101021e003030f9673434010000000100000000000000000000000000000000000000ce8d7b51"),
        3: (encode(3, COMMAND.pack(1, 1, 500, 1)), "43534931010308000100000001f40101b76cbef8"),
        4: (encode(4, ACK.pack(1, 2)), "43534931010405000100000002f580857c"),
    }
    for kind, (raw, golden) in given.items():
        assert raw.hex() == golden
        assert Parser().feed(raw)[0] == (kind, raw[8:-4], raw)


def test_parser_rejects_wrong_version_and_unknown_kind():
    p = Parser()
    invalid = b"CSI1" + struct.pack("<BBH", 2, 1, 0)
    unknown = b"CSI1" + struct.pack("<BBH", 1, 99, 0)
    assert [r for _, _, r in p.feed(invalid + unknown + sample())] == [sample()]
    assert p.errors == 2


def test_csi_rejects_source_and_invalid_layout():
    payload = Parser().feed(sample())[0][1]
    with pytest.raises(ValueError):
        parse_csi(payload, "00:00:00:00:00:00", "30:30:f9:67:34:34")
    with pytest.raises(ValueError):
        parse_csi(payload[:-1], "98:c3:77:b4:22:44", "30:30:f9:67:34:34")
    with pytest.raises(ValueError):
        parse_csi(payload[:26] + b"\x02" + payload[27:], "98:c3:77:b4:22:44", "30:30:f9:67:34:34")


def test_first_word_invalid_preserves_raw_and_excludes_first_two_pairs():
    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    hub.selected_pair = 0
    hub.ingest(sample(invalid=1))
    assert hub.trace[-1]["valid"] is False
    hub.selected_pair = 2
    hub.ingest(sample(seq=2, invalid=1))
    assert hub.trace[-1]["real"] == 6 and hub.trace[-1]["imag"] == 5


def test_gaps_stale_and_disconnect_are_distinct():
    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    hub.ingest(sample(1)); hub.ingest(sample(4))
    assert hub.sequence_gaps == 2 and hub.trace[-1]["gap"]
    hub.last_sample_ns -= 3_000_000_000
    assert hub.snapshot()["csi_health"] == "UNKNOWN" and hub.snapshot()["variation"]["value"] is None
    assert hub.snapshot()["connection"] == "disconnected"


def test_receiver_clock_reset_does_not_invent_huge_gap():
    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    hub.ingest(sample(99))
    hub.ingest(sample(1))
    assert hub.sequence_gaps == 0
    assert any(event["type"] == "rx_clock_reset_or_reorder" for event in hub.events)

def test_ingest_exposes_new_features_in_snapshot_and_telemetry():
    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    for seq in range(1, 18):
        hub.ingest(sample(seq), wall_ns=seq * 100_000_000, mono_ns=seq * 100_000_000)
    state = hub.snapshot()
    for key in ("burst_ratio", "spectral_dist", "room_presence", "motion_slope"):
        assert key in state and key in hub.telemetry[-1]
    assert state["room_presence"] == "UNKNOWN"

@pytest.mark.asyncio
async def test_empty_room_capture_requires_connected_stream_and_collects_frames():
    hub = Hub()
    async with TestClient(TestServer(make_app(hub))) as client:
        response = await client.post("/api/empty_room/capture", json={})
        assert response.status == 400
        hub.mode = "REPLAY"
        hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
        response = await client.post("/api/empty_room/capture", json={})
        assert response.status == 200
        for seq in range(1, 225):
            hub.ingest(sample(seq, values=b"\x01\x02" * 64))
        assert hub.dsp.get_empty_baseline() is not None
        assert hub.room_presence == "EMPTY_COMPATIBLE"


@pytest.mark.asyncio
async def test_http_blocks_commands_without_live_verified_port_and_cross_origin():
    async with TestClient(TestServer(make_app())) as client:
        response = await client.post("/api/command", json={"action": "identify", "ttl_ms": 500, "source": "manual_test"})
        assert response.status == 400
        response = await client.post("/api/record/start", json={"name": "../escape", "notes": ""})
        assert response.status == 400
        response = await client.post("/api/replay/start", json={"name": "missing"}, headers={"Origin": "http://evil.example"})
        assert response.status == 403


def test_record_preserves_wire_bytes_and_timestamps(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "RECORDINGS", tmp_path)
    hub = Hub()
    hub.source, hub.receiver, hub.verified = "98:c3:77:b4:22:44", "30:30:f9:67:34:34", True
    hub.recorder = (tmp_path / "trial.jsonl").open("x", encoding="utf-8")
    hub.record_name = "trial"
    frame = sample()
    hub.ingest(frame, wall_ns=111, mono_ns=222)
    hub.stop_record()
    entry = json.loads((tmp_path / "trial.jsonl").read_text().splitlines()[0])
    assert base64.b64decode(entry["raw"]) == frame
    assert entry["wall_ns"] == 111 and entry["mono_ns"] == 222


@pytest.mark.parametrize("failure", ["write", "flush", "close"])
def test_record_failure_detaches_and_retains_incomplete_state(failure):
    # Given a disk failing at a specific I/O boundary, including close's flush.
    class FailingFile:
        writes = 0
        closed = False

        def write(self, text):
            self.writes += 1
            if failure == "write":
                raise OSError("disk full")

        def flush(self):
            if failure == "flush":
                raise OSError("disk full")

        def close(self):
            self.closed = True
            raise OSError("close flush failed")

    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    sink = FailingFile()
    hub.recorder, hub.record_name = sink, "broken"
    # When capture encounters the failure, then continues after stopping.
    hub.ingest(sample())
    hub.stop_record()
    hub.ingest(sample(2))
    # Then failure is visible, detached, never recursively written; capture survives.
    assert hub.recorder is None and hub.record_name is None and sink.closed
    assert hub.snapshot()["recording_status"]["state"] == "incomplete"
    assert hub.snapshot()["recording_status"]["name"] == "broken"
    assert hub.host_record_drops == 1
    assert sink.writes == (2 if failure == "close" else 1)
    assert len(hub.trace) == 2


def test_terminal_counts_only_successfully_recorded_frames_and_markers(tmp_path):
    # Given a recording with accepted and rejected input.
    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    path = tmp_path / "counts.jsonl"
    hub.recorder = path.open("x")
    hub.ingest(sample() + encode(1, b"invalid"))
    hub.write_record({"type": "marker", "kind": "note", "note": "test"})
    hub.ingest(sample(2))
    # When stopped.
    hub.stop_record()
    # Then the terminal counts match persisted records.
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["counts"] == {"frames": 2, "markers": 1}


@pytest.mark.parametrize("failure", ["write", "flush"])
@pytest.mark.asyncio
async def test_stop_reports_terminal_disk_failure_and_always_closes(failure):
    # Given a recorder failing during the terminal record.
    class Disk:
        closed = False

        def write(self, text):
            if failure == "write":
                raise OSError("disk full")

        def flush(self):
            if failure == "flush":
                raise OSError("disk full")

        def close(self):
            self.closed = True

    hub = Hub()
    disk = Disk()
    hub.recorder, hub.record_name = disk, "terminal"
    async with TestClient(TestServer(make_app(hub))) as client:
        # When stopped through the real HTTP handler.
        response = await client.post("/api/record/stop", json={})
        result = await response.json()
        # Then the API reports failure, closes, preserves incomplete status.
        assert response.status == 500 and result["ok"] is False
        assert result["state"]["recording_status"]["state"] == "incomplete"
        assert hub.host_record_drops == 1 and disk.closed
        assert hub.recorder is None and hub.record_name is None


@pytest.mark.asyncio
async def test_reader_continues_after_recorder_disk_failure():
    # Given an in-memory serial source and a failing disk, never hardware.
    hub = Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    class Source:
        is_open = True
        reads = 0

        def read(self, size):
            self.reads += 1
            if self.reads == 2:
                hub.port = None
            return sample(self.reads)

    class Disk:
        def write(self, text):
            raise OSError("disk full")

        def close(self):
            pass

    source = Source()
    hub.port, hub.recorder, hub.record_name = source, Disk(), "reader"
    # When the reader drains two frames across a disk error.
    await asyncio.wait_for(hub.read_loop(), timeout=2)
    # Then both arrive without misclassifying the disk failure as serial failure.
    assert source.reads == 2 and len(hub.trace) == 2
    assert hub.recording_status["state"] == "incomplete"
    assert not any(event["type"] == "serial_error" for event in hub.events)


@pytest.mark.parametrize("host,origin,status", [
    ("evil.example", "http://evil.example", 403),
    ("localhost.evil.example", "http://localhost.evil.example", 403),
    ("localhost", "http://evil.example", 403),
    ("127.0.0.1", "null", 403),
    ("localhost", "http://localhost", 200),
    ("127.0.0.1:8765", "http://127.0.0.1:8765", 200),
    ("[::1]:8765", "http://[::1]:8765", 200),
    ("localhost", None, 200),
])
@pytest.mark.asyncio
async def test_local_host_and_origin_guard_covers_post_and_websocket(host, origin, status):
    # Given untrusted browser headers on a loopback connection.
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    async with TestClient(TestServer(make_app())) as client:
        # When requesting mutation and a websocket handshake.
        response = await client.post("/api/record/stop", json={}, headers=headers)
        response_ws = await client.get("/ws", headers={**headers, "Upgrade": "websocket",
            "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        # Then both surfaces enforce the same origin policy.
        assert response.status == status
        assert response_ws.status == (101 if status == 200 else status)
        response_ws.close()


@pytest.mark.asyncio
async def test_allow_remote_accepts_external_host_and_matching_origin():
    headers = {"Host": "192.168.1.102:8765", "Origin": "http://192.168.1.102:8765"}
    async with TestClient(TestServer(make_app(allow_remote=True))) as client:
        response = await client.post("/api/record/stop", json={}, headers=headers)
        assert response.status == 200


@pytest.mark.parametrize("seq,capture_us", [(4, 40000), (1, 1000), (3, 3_000_000), (1, 30000)])
def test_variation_recovers_using_only_contiguous_post_gap_samples(seq, capture_us):
    # Given two pre-gap values far from the recovery segment.
    hub = Hub()
    hub.mode, hub.replayer = "REPLAY", True
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    hub.ingest(sample(1, values=b"\0\0\0\0\0\x64"))
    hub.ingest(sample(2, values=b"\0\0\0\0\0\x78"))
    def recovery(sequence, timestamp, real):
        return encode(1, PREFIX.pack(SOURCE, RX, sequence, timestamp, 6, 0, 1, -40, 11)
                      + bytes([0, 0, 0, 0, 0, real]))
    # When a discontinuity begins a new segment, then a second sample arrives.
    hub.ingest(recovery(seq, capture_us, 2))
    first = hub.snapshot()
    hub.ingest(recovery(seq + 1, capture_us + 10000, 6))
    # Then graph breaks and variation never includes pre-gap values.
    assert first["trace"][-1]["gap"] is True
    assert first["variation"]["value"] is None
    assert first["variation"]["valid_samples"] == 1
    assert hub.snapshot()["variation"]["value"] == 2
    assert hub.snapshot()["variation"]["valid_samples"] == 2


def test_command_rejects_ttl_bounds_and_replay_without_hardware_write():
    class Sink:
        is_open = True
        writes = []

        def write(self, data):
            self.writes.append(data)

    hub = Hub()
    hub.port = Sink()
    hub.verified = True
    with pytest.raises(ValueError):
        hub.send_command("identify", 2001)
    hub.mode = "REPLAY"
    with pytest.raises(ValueError):
        hub.send_command("identify", 500)
    assert not hub.port.writes
    hub.mode = "LIVE"
    hub.send_command("identify", 500)
    kind, payload, _ = Parser().feed(hub.port.writes[0])[0]
    assert kind == 3 and COMMAND.unpack(payload) == (1, 1, 500, 1)


@pytest.mark.asyncio
async def test_replay_reuses_parser_preserves_markers_without_serial(tmp_path):
    from app import replay
    path = tmp_path / "trial.jsonl"
    path.write_text("\n".join((json.dumps({"type": "frame", "raw": base64.b64encode(sample()).decode(), "wall_ns": 1, "mono_ns": 2}), json.dumps({"type": "marker", "kind": "motion_start", "note": "walk", "mono_ns": 3}), json.dumps({"type": "frame", "raw": base64.b64encode(sample(4)).decode(), "wall_ns": 4, "mono_ns": 5}))))
    hub = Hub()
    hub.mode, hub.source, hub.receiver = "REPLAY", "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    hub.replayer = asyncio.create_task(replay(hub, path))
    await hub.replayer
    assert len(hub.trace) == 2
    assert len(hub.events) == 1 and hub.events[0]["kind"] == "motion_start"


@pytest.mark.asyncio
async def test_alert_ack_and_calibration_endpoints():
    hub = Hub()
    from fsm import State
    hub.fsm.state = State.FALL_ALERT
    async with TestClient(TestServer(make_app(hub, allow_remote=True))) as client:
        # Test ack
        res = await client.post("/api/alert/ack", json={})
        assert res.status == 200
        data = await res.json()
        assert data["ok"] is True
        assert data["state"]["fsm"]["state"] == "NORMAL"

        # Test calibrate start
        res = await client.post("/api/calibrate/start", json={"step": 1})
        assert res.status == 200
        assert hub.calibrating_step == 1

        # Test calibrate stop with custom thresholds
        res = await client.post("/api/calibrate/stop", json={"thresholds": {"still": 0.05, "move": 0.2, "spike": 0.6}})
        assert res.status == 200
        assert hub.thresholds.still == 0.05
        assert hub.thresholds.move == 0.2
        assert hub.thresholds.spike == 0.6
        assert hub.calibrating_step is None


@pytest.mark.asyncio
async def test_ports_includes_serial_and_prioritizes_acm():
    async with TestClient(TestServer(make_app(allow_remote=True))) as client:
        res = await client.get("/api/ports")
        assert res.status == 200
        data = await res.json()
        assert "ports" in data
        assert isinstance(data["ports"], list)

@pytest.mark.asyncio
async def test_calibration_replays_capture_times_and_rejects_score_scale():
    hub = Hub()
    shape = [1.0] * 28 + [3.0] * 28
    hub.calib_still_samples = [(i * 10_000, shape) for i in range(30)]
    hub.calib_walk_samples = [0.1, 0.2, 0.3, 0.4, 0.5]
    async with TestClient(TestServer(make_app(hub, allow_remote=True))) as client:
        step_two = await client.post('/api/calibrate/start', json={'step': 2})
        assert step_two.status == 200
        assert hub.dsp.last_us == 290_000
        bad = await client.post('/api/calibrate/stop', json={'thresholds': {'still': 20, 'move': 50, 'spike': 80}})
        assert bad.status == 400
        assert hub.calibrating_step is None or hub.thresholds.still == 0.0459
        done = await client.post('/api/calibrate/stop', json={})
        assert done.status == 200
        assert hub.dsp.baseline == (0.5,) * 28 + (1.5,) * 28
        assert hub.dsp.last_us == 290_000
        assert hub.thresholds == hub.fsm.thresholds
        hub.last_motion = hub.thresholds.move
        fsm = hub.snapshot()['fsm']
        assert fsm['motion_score'] == 50
        assert fsm['thresholds_mapped'] == {'still': 20, 'move': 50, 'spike': 80}

@pytest.mark.asyncio
async def test_calibration_samples_capture_actual_timestamp_from_ingest(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, 'DATA', tmp_path)
    hub = Hub()
    hub.calibrating_step = 1
    hub.source, hub.receiver = '98:c3:77:b4:22:44', '30:30:f9:67:34:34'
    hub.ingest(sample(7, values=bytes([1, 2]) * 64))
    assert hub.calib_still_samples[0][0] == 70_000
    assert len(hub.calib_still_samples[0][1]) == 56

@pytest.mark.asyncio
async def test_audit_and_telemetry_persist_and_export(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, 'DATA', tmp_path)
    hub = Hub()
    hub.source, hub.receiver = '98:c3:77:b4:22:44', '30:30:f9:67:34:34'
    async with TestClient(TestServer(make_app(hub, allow_remote=True))) as client:
        assert (await client.post('/api/calibrate/start', json={'step': 1})).status == 200
        assert (await client.post('/api/calibrate/stop', json={})).status == 400
        events = await (await client.get('/api/audit')).json()
        assert [event['outcome'] for event in events] == ['success', 'failed']
        assert events[0]['event'] == 'calibrate_start'
        assert all({'timestamp', 'mono_ns', 'event', 'action_id', 'params', 'outcome', 'detail'} <= e.keys() for e in events)
        exported = await (await client.get('/api/audit/export')).text()
        assert len(exported.splitlines()) == 2
        for i in range(25):
            hub.ingest(sample(i + 1, values=bytes([1, 2]) * 64))
        telemetry = hub.telemetry[-1]
        assert {'timestamp_ms', 'rx_rate_hz', 'raw_motion', 'motion_score', 'fsm_state', 'mean_amp', 'rssi_approx', 'drops', 'sequence_gaps'} <= telemetry.keys()
        assert (await (await client.get('/api/telemetry/export')).text()).strip()
