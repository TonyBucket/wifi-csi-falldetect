"""
Tests for logical run exchange bundle export and import (host/run_exchange.py).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

# Ensure host is on sys.path
HOST_DIR = Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST_DIR))

from run_exchange import export_run, import_run, compute_file_sha256
from telemetry_db import TelemetryDB


def _make_dummy_recording(path: Path, num_lines: int = 10) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(num_lines):
        line_obj = {
            "type": "csi",
            "seq": i,
            "timestamp": f"2026-10-01T12:00:{i:02d}Z",
            "rssi": -45 + (i % 5),
            "data": [0.1 * i, 0.2 * i, 0.3 * i],
        }
        lines.append(json.dumps(line_obj) + "\n")
    raw = "".join(lines).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)


@pytest.fixture
def test_env(tmp_path):
    src_data = tmp_path / "src_data"
    src_data.mkdir()
    dst_data = tmp_path / "dst_data"
    dst_data.mkdir()

    src_db = TelemetryDB(src_data / "telemetry.db")
    dst_db = TelemetryDB(dst_data / "telemetry.db")

    return {
        "src_data": src_data,
        "dst_data": dst_data,
        "src_db": src_db,
        "dst_db": dst_db,
        "bundles": tmp_path / "bundles",
    }


def _seed_completed_run(db: TelemetryDB, data_dir: Path, run_id: str = "run_101") -> dict:
    rec_file = data_dir / "recordings" / f"{run_id}.jsonl"
    sha, size = _make_dummy_recording(rec_file, num_lines=15)

    session = {
        "session_id": f"sess_{run_id}",
        "created_at": "2026-10-01T10:00:00Z",
        "setup_room": "Lab 1",
        "schema_version": 1,
    }
    config = {
        "config_id": f"cfg_{run_id}",
        "config_json": json.dumps({"sample_rate": 100}),
        "config_sha256": "abcdef123456",
        "dsp_version": "v1.0",
    }
    run = {
        "run_id": run_id,
        "session_id": session["session_id"],
        "scenario": "fall_forward",
        "status": "COMPLETED",
        "requested_duration_s": 10.0,
        "started_at": "2026-10-01T10:05:00Z",
        "ended_at": "2026-10-01T10:05:10Z",
        "recording_id": f"rec_{run_id}",
        "config_id": config["config_id"],
        "csi_frame_count": 15,
        "wire_frame_count": 15,
        "gap_count": 0,
        "fsm_highest": "SPIKE",
        "first_alert_ns": 12345678,
        "quality_flags": "GOOD",
    }
    recording = {
        "recording_id": run["recording_id"],
        "run_id": run_id,
        "rel_path": f"recordings/{run_id}.jsonl",
        "schema_version": 1,
        "status": "COMPLETE",
        "frame_count": 15,
        "byte_count": size,
        "sha256": sha,
    }

    db.create_run(run, recording, session, config)

    # Insert label and event
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO labels VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"lbl_{run_id}", run_id, 1, "fall", "FALL_DETECTED", "2026-10-01T10:06:00Z", "Verified by video"),
        )
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"evt_{run_id}", run_id, "act_1", "ALERT", "LOCAL", "client_1", 1, 100, "100ns", 1000, 1000, 1, "{}"),
        )

    return {"run": run, "recording": recording, "session": session, "config": config}


def test_export_and_import_roundtrip(test_env):
    src_db = test_env["src_db"]
    src_data = test_env["src_data"]
    dst_db = test_env["dst_db"]
    dst_data = test_env["dst_data"]
    bundle_path = test_env["bundles"] / "run_101.zip"

    seed_data = _seed_completed_run(src_db, src_data, run_id="run_101")

    # 1. Export
    exp_res = export_run(src_db, src_data, "run_101", bundle_path)
    assert exp_res["status"] == "exported"
    assert bundle_path.is_file()

    # Verify bundle contents
    with zipfile.ZipFile(bundle_path, "r") as zf:
        assert set(zf.namelist()) == {"manifest.json", "recording.jsonl"}
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["run_id"] == "run_101"
        assert manifest["recording_sha256"] == exp_res["recording_sha256"]

    # 2. Import into empty dst
    imp_res = import_run(dst_db, dst_data, bundle_path)
    assert imp_res["status"] == "imported"
    assert imp_res["run_id"] == "run_101"

    # Verify all 6 tables populated in dst_db
    detail = dst_db.detail("run_101")
    assert detail is not None
    assert detail["run"]["run_id"] == "run_101"
    assert detail["run"]["scenario"] == "fall_forward"
    assert detail["run"]["status"] == "COMPLETED"
    assert detail["config"]["config_id"] == seed_data["config"]["config_id"]
    assert detail["recording"]["recording_id"] == seed_data["recording"]["recording_id"]
    assert len(detail["labels"]) == 1
    assert detail["labels"][0]["verdict"] == "FALL_DETECTED"
    assert len(detail["events"]) == 1

    session_row = dst_db.one("SELECT * FROM sessions WHERE session_id=?", (seed_data["session"]["session_id"],))
    assert session_row is not None
    assert session_row["setup_room"] == "Lab 1"

    # Verify recording file exists and matches original hash
    dst_rec_file = dst_data / detail["recording"]["rel_path"]
    assert dst_rec_file.is_file()
    dst_sha, dst_bytes = compute_file_sha256(dst_rec_file)
    assert dst_sha == exp_res["recording_sha256"]
    assert dst_bytes == exp_res["recording_byte_count"]


def test_import_idempotency(test_env):
    src_db = test_env["src_db"]
    src_data = test_env["src_data"]
    dst_db = test_env["dst_db"]
    dst_data = test_env["dst_data"]
    bundle_path = test_env["bundles"] / "run_idempotent.zip"

    _seed_completed_run(src_db, src_data, run_id="run_idempotent")
    export_run(src_db, src_data, "run_idempotent", bundle_path)

    # First import
    res1 = import_run(dst_db, dst_data, bundle_path)
    assert res1["status"] == "imported"

    # Second import of identical bundle
    res2 = import_run(dst_db, dst_data, bundle_path)
    assert res2["status"] == "unchanged"
    assert res2["run_id"] == "run_idempotent"

    # Verify DB has exactly 1 run record
    runs = dst_db.all("SELECT * FROM runs WHERE run_id='run_idempotent'")
    assert len(runs) == 1


def test_import_conflict_detection(test_env):
    src_db = test_env["src_db"]
    src_data = test_env["src_data"]
    dst_db = test_env["dst_db"]
    dst_data = test_env["dst_data"]

    # Seed run in dst_db
    _seed_completed_run(dst_db, dst_data, run_id="run_conflict")

    # Seed run in src_db with same run_id but DIFFERENT recording content
    _seed_completed_run(src_db, src_data, run_id="run_conflict")
    rec_file = src_data / "recordings" / "run_conflict.jsonl"
    rec_file.write_text("completely different csi content\n")
    new_sha, new_size = compute_file_sha256(rec_file)
    with src_db.connect() as conn:
        conn.execute("UPDATE recordings SET sha256=?, byte_count=? WHERE run_id='run_conflict'", (new_sha, new_size))

    bundle_path = test_env["bundles"] / "conflict.zip"
    export_run(src_db, src_data, "run_conflict", bundle_path)

    # Import into dst_db -> must report CONFLICT
    res = import_run(dst_db, dst_data, bundle_path)
    assert res["status"] == "conflict"
    assert res["run_id"] == "run_conflict"
    assert "fingerprint mismatch" in res["reason"]

    # Verify dst_db was NOT modified
    dst_rec = dst_db.one("SELECT * FROM recordings WHERE run_id='run_conflict'")
    assert dst_rec["sha256"] != new_sha


def test_export_rejects_non_completed_run(test_env):
    src_db = test_env["src_db"]
    src_data = test_env["src_data"]
    bundle_path = test_env["bundles"] / "incomplete.zip"

    _seed_completed_run(src_db, src_data, run_id="run_incomplete")
    with src_db.connect() as conn:
        conn.execute("UPDATE runs SET status='RUNNING' WHERE run_id='run_incomplete'")

    with pytest.raises(ValueError, match="Only COMPLETED runs can be exported"):
        export_run(src_db, src_data, "run_incomplete", bundle_path)


def test_import_validation_rejects_corrupted_checksum(test_env):
    src_db = test_env["src_db"]
    src_data = test_env["src_data"]
    dst_db = test_env["dst_db"]
    dst_data = test_env["dst_data"]
    bundle_path = test_env["bundles"] / "corrupt_sha.zip"

    _seed_completed_run(src_db, src_data, run_id="run_corrupt")
    export_run(src_db, src_data, "run_corrupt", bundle_path)

    # Tamper with the zip: overwrite recording.jsonl with garbage
    tampered_bundle = test_env["bundles"] / "tampered.zip"
    with zipfile.ZipFile(bundle_path, "r") as z_in, zipfile.ZipFile(tampered_bundle, "w") as z_out:
        for item in z_in.infolist():
            if item.filename == "recording.jsonl":
                z_out.writestr(item, b"corrupted payload")
            else:
                z_out.writestr(item, z_in.read(item.filename))

    with pytest.raises(ValueError, match="Checksum mismatch"):
        import_run(dst_db, dst_data, tampered_bundle)

    # Verify nothing was inserted into dst_db
    assert dst_db.one("SELECT * FROM runs WHERE run_id='run_corrupt'") is None


def test_import_validation_rejects_unexpected_files(test_env):
    dst_db = test_env["dst_db"]
    dst_data = test_env["dst_data"]
    invalid_bundle = test_env["bundles"] / "invalid_structure.zip"
    invalid_bundle.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(invalid_bundle, "w") as zf:
        zf.writestr("manifest.json", "{}")
        zf.writestr("recording.jsonl", "data")
        zf.writestr("extra_payload.sh", "evil script")

    with pytest.raises(ValueError, match="Invalid bundle structure"):
        import_run(dst_db, dst_data, invalid_bundle)
