import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_host import RX, SOURCE, sample

@pytest.mark.asyncio
async def test_run_saves_peak_fsm_state_not_last_state(tmp_path, monkeypatch):
    import app
    from fsm import State
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "RECORDINGS", tmp_path / "recordings")
    hub = app.Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    class Motion:
        def __init__(self):
            self.values = iter([0.8, 0.8, 0.01, 0.01, 0.01])
        def push(self, amps, capture_us):
            return next(self.values)
    hub.dsp = Motion()
    from wire import PREFIX, encode
    def csi(seq):
        values = bytes([1, 2]) * 64
        return encode(1, PREFIX.pack(SOURCE, RX, seq, seq * 10000, len(values), 0, 1, -40, 11) + values)
    async with TestClient(TestServer(app.make_app(hub))) as client:
        run_id = (await (await client.post("/api/runs", json={"scenario": "walk", "requested_duration_s": 3})).json())["run_id"]
        assert (await client.post(f"/api/runs/{run_id}/start", json={})).status == 200
        hub.ingest(csi(1)); hub.ingest(csi(10)); hub.ingest(csi(11))
        # A capture-clock reset returns the instantaneous FSM to NORMAL.
        hub.ingest(csi(1)); hub.ingest(csi(2))
        assert hub.fsm.state is State.NORMAL
        assert (await client.post(f"/api/runs/{run_id}/stop", json={})).status == 200
        detail = await (await client.get(f"/api/runs/{run_id}")).json()
        assert detail["run"]["fsm_highest"] == "CANDIDATE"

@pytest.mark.parametrize("started", [False, True])
@pytest.mark.asyncio
async def test_serial_failure_interrupts_run_and_closes_v2(tmp_path, monkeypatch, started):
    import app
    import serial
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "RECORDINGS", tmp_path / "recordings")
    hub = app.Hub()
    async with TestClient(TestServer(app.make_app(hub))) as client:
        run_id = (await (await client.post("/api/runs", json={"scenario": "walk", "requested_duration_s": 3})).json())["run_id"]
        recorder = hub.run_recorder
        if started:
            assert (await client.post(f"/api/runs/{run_id}/start", json={})).status == 200
        class BrokenPort:
            def read(self, size):
                raise serial.SerialException("unplugged")
            def close(self):
                pass
        hub.port = BrokenPort()
        await hub.read_loop()
        detail = await (await client.get(f"/api/runs/{run_id}")).json()
        assert detail["run"]["status"] == "INTERRUPTED"
        assert detail["run"]["ended_at"] is not None
        assert detail["recording"]["status"] == "INCOMPLETE"
        assert recorder.closed and hub.run_recorder is None and hub.run_id is None

def test_db_uses_wal_and_unique_label_revisions(tmp_path):
    from telemetry_db import TelemetryDB
    db = TelemetryDB(tmp_path / "telemetry.db")
    assert db.one("PRAGMA journal_mode")["journal_mode"] == "wal"
    assert db.one("PRAGMA synchronous")["synchronous"] == 1
    label = ("first", "run", 1, "walk", "correct", "now", "")
    db.execute("INSERT INTO labels VALUES (?,?,?,?,?,?,?)", label)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO labels VALUES (?,?,?,?,?,?,?)", ("second", *label[1:]))

@pytest.mark.asyncio
async def test_events_batch_validates_run_ids_once(tmp_path, monkeypatch):
    import app
    from telemetry_db import TelemetryDB
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "RECORDINGS", tmp_path / "recordings")
    queries = []
    original = TelemetryDB.all
    def tracking(self, statement, params=()):
        if "SELECT run_id FROM runs" in statement:
            queries.append((statement, params))
        return original(self, statement, params)
    monkeypatch.setattr(TelemetryDB, "all", tracking)
    async with TestClient(TestServer(app.make_app())) as client:
        run_id = (await (await client.post("/api/runs", json={"scenario": "walk", "requested_duration_s": 3})).json())["run_id"]
        batch = [{"run_id": run_id, "event_type": "go"} for _ in range(3)]
        assert (await client.post("/api/telemetry/events", json={"events": batch})).status == 200
        assert len(queries) == 1 and queries[0][1] == (run_id,)
        assert (await client.post("/api/telemetry/events", json={"events": batch + [{"run_id": "missing", "event_type": "go"}]})).status == 400
        assert len(queries) == 2


def test_db_connections_are_safe_across_threads(tmp_path):
    from telemetry_db import TelemetryDB
    db = TelemetryDB(tmp_path / "nested" / "telemetry.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (str(n), "now", "", 2)), range(40)))
    assert db.one("SELECT count(*) AS n FROM sessions")["n"] == 40


@pytest.mark.asyncio
async def test_run_lifecycle_persists_raw_frames_and_labels(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "RECORDINGS", tmp_path / "recordings")
    hub = app.Hub()
    hub.source, hub.receiver = "98:c3:77:b4:22:44", "30:30:f9:67:34:34"
    async with TestClient(TestServer(app.make_app(hub))) as client:
        response = await client.post("/api/runs", json={"scenario": "walk", "requested_duration_s": 12})
        assert response.status == 201
        run_id = (await response.json())["run_id"]
        hub.ingest(sample(1))
        hub.ingest(sample(3))
        hub.ingest(__import__("wire").encode(2, __import__("wire").STATS.pack(RX, 1, 1, 0, 0, 0, 0)))
        response = await client.post(f"/api/runs/{run_id}/start", json={})
        assert response.status == 200
        response = await client.post("/api/telemetry/events", json={"events": [{"run_id": run_id, "event_type": "countdown_go", "origin": "ui", "client_instance_id": "tab", "client_seq": 1, "action_id": "a1", "client_wall_ms": 1000, "client_mono_ns": "123", "payload": {"count": 0}}]})
        assert response.status == 200
        assert (await response.json())["accepted"] == 1
        response = await client.post(f"/api/runs/{run_id}/stop", json={})
        assert response.status == 200
        path = tmp_path / "recordings" / f"{run_id}.jsonl"
        frames = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(frames) == 3
        assert frames[0]["version"] == 2 and frames[0]["frame_index"] == 0
        assert frames[0]["capture_us"] == 10000 and frames[0]["rx_seq"] == 1
        assert base64.b64decode(frames[0]["raw"]) == sample(1)
        assert [frame["frame_kind"] for frame in frames] == ["csi", "csi", "stats"]
        assert (await (await client.get(f"/api/runs/{run_id}")).json())["run"]["status"] == "AWAITING_LABEL"
        response = await client.post(f"/api/runs/{run_id}/label", json={"actual_activity": "walk", "verdict": "correct", "note": "observed"})
        assert response.status == 200
        detail = await (await client.get(f"/api/runs/{run_id}")).json()
        assert detail["run"]["status"] == "COMPLETED"
        assert detail["run"]["csi_frame_count"] == 2 and detail["run"]["wire_frame_count"] == 3
        assert detail["run"]["gap_count"] == 1
        assert detail["labels"][0]["revision"] == 1
        assert detail["events"][0]["client_mono_ns"] == "123"
        assert detail["recording"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert (await (await client.get("/api/runs?limit=1&offset=0")).json())["runs"][0]["run_id"] == run_id
        assert (await client.post(f"/api/runs/{run_id}/stop", json={})).status == 409
    with sqlite3.connect(tmp_path / "telemetry.db") as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM configs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_run_invalid_transitions_cancel_and_event_validation(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "RECORDINGS", tmp_path / "recordings")
    async with TestClient(TestServer(app.make_app())) as client:
        assert (await client.post("/api/runs", json={"scenario": "", "requested_duration_s": 0})).status == 400
        run_id = (await (await client.post("/api/runs", json={"scenario": "sit", "requested_duration_s": 3})).json())["run_id"]
        assert (await client.post("/api/runs", json={"scenario": "walk", "requested_duration_s": 3})).status == 409
        assert (await client.post(f"/api/runs/{run_id}/label", json={"actual_activity": "sit", "verdict": "ok"})).status == 409
        assert (await client.post("/api/telemetry/events", json={"events": [{"run_id": "nonexistent", "event_type": "go"}]})).status == 400
        assert (await client.post(f"/api/runs/{run_id}/cancel", json={"reason": "operator"})).status == 200
        assert (await client.post(f"/api/runs/{run_id}/start", json={})).status == 409
        assert (await (await client.get(f"/api/runs/{run_id}")).json())["run"]["status"] == "CANCELLED"
        assert (await client.get("/api/runs/not-found")).status == 404


@pytest.mark.asyncio
async def test_events_batch_is_atomic_and_pagination_validates(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA", tmp_path)
    monkeypatch.setattr(app, "RECORDINGS", tmp_path / "recordings")
    async with TestClient(TestServer(app.make_app())) as client:
        run_id = (await (await client.post("/api/runs", json={"scenario": "walk", "requested_duration_s": 3})).json())["run_id"]
        event = {"run_id": run_id, "event_type": "ready", "event_id": "same"}
        assert (await client.post("/api/telemetry/events", json={"events": [event, event]})).status == 409
        assert (await (await client.get(f"/api/runs/{run_id}")).json())["events"] == []
        assert (await client.post("/api/telemetry/events", json={"events": [event]})).status == 200
        assert (await client.get("/api/runs?limit=101")).status == 400
        assert (await client.post(f"/api/runs/{run_id}/cancel", json={})).status == 400
        assert (await client.post(f"/api/runs/{run_id}/cancel", json={"reason": "operator"})).status == 200
