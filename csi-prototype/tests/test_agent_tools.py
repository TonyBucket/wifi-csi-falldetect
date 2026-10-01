"""Tests for agent_tools module: import-v1, runs, replay, evaluate, sweep."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

import pytest

# Ensure host is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from agent_tools import (
    cmd_evaluate,
    cmd_import_v1,
    cmd_replay,
    cmd_runs,
    cmd_sweep,
    main,
    parse_ground_truth,
    replay_recording_file,
    resolve_thresholds,
)
from dsp import Thresholds
from fsm import State
from telemetry_db import TelemetryDB
from wire import PREFIX, encode

SRC_MAC = bytes.fromhex("98c377b42244")
RX_MAC = bytes.fromhex("3030f9673434")


def make_csi_raw(seq: int, capture_us: int, iq_val: int = 10) -> str:
    """Create a base64-encoded valid CSI frame with 64 subcarrier pairs."""
    iq_bytes = bytes([iq_val, iq_val] * 64)
    payload = PREFIX.pack(SRC_MAC, RX_MAC, seq, capture_us, len(iq_bytes), 0, 1, -40, 11) + iq_bytes
    return base64.b64encode(encode(1, payload)).decode()


def create_sample_recording(
    path: Path,
    scenario: str = "fall",
    actual: str = "fall",
    fsm_highest: str = "NORMAL",
    num_frames: int = 20,
) -> Path:
    """Create a synthetic JSONL recording file with valid CSI frames and ground truth."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "type": "meta",
            "version": 1,
            "source_mac": "98:c3:77:b4:22:44",
            "rx_mac": "30:30:f9:67:34:34",
            "notes": f"Safe fall test scenario: {scenario}",
            "wall_ns": 1_000_000_000,
            "mono_ns": 100_000_000,
        })
    ]
    for i in range(num_frames):
        lines.append(json.dumps({
            "type": "frame",
            "raw": make_csi_raw(i + 1, (i + 1) * 10_000, iq_val=10 + (i % 3)),
            "wall_ns": 1_000_000_000 + (i + 1) * 10_000_000,
            "mono_ns": 100_000_000 + (i + 1) * 10_000_000,
        }))
    if actual:
        lines.append(json.dumps({
            "type": "marker",
            "kind": "note",
            "note": f"GROUND_TRUTH: actual={actual}, fsm_highest={fsm_highest}, scenario={scenario}",
            "wall_ns": 1_000_000_000 + num_frames * 10_000_000,
            "mono_ns": 100_000_000 + num_frames * 10_000_000,
        }))
    lines.append(json.dumps({
        "type": "end",
        "wall_ns": 1_000_000_000 + (num_frames + 1) * 10_000_000,
        "mono_ns": 100_000_000 + (num_frames + 1) * 10_000_000,
        "counts": {"frames": num_frames, "markers": 1 if actual else 0},
    }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_ground_truth_helper():
    res = parse_ground_truth("GROUND_TRUTH: actual=fall, fsm_highest=FALL_ALERT, scenario=fall")
    assert res == {"actual": "fall", "fsm_highest": "FALL_ALERT", "scenario": "fall"}

    res_spaces = parse_ground_truth("  GROUND_TRUTH:  actual = normal , fsm_highest = NORMAL  ")
    assert res_spaces["actual"] == "normal"
    assert res_spaces["fsm_highest"] == "NORMAL"

    assert parse_ground_truth("Just a normal note") is None


def test_import_v1_idempotent(tmp_path):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"

    create_sample_recording(rec_dir / "test_fall_1.jsonl", scenario="fall", actual="fall", fsm_highest="NORMAL")
    create_sample_recording(rec_dir / "test_sit_1.jsonl", scenario="sit", actual="normal", fsm_highest="NORMAL")
    create_sample_recording(rec_dir / "test_baseline_1.jsonl", scenario="baseline", actual="", fsm_highest="UNKNOWN")

    # First import
    res1 = cmd_import_v1(rec_dir, db_path)
    assert res1["total"] == 3

    db = TelemetryDB(db_path)
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 3
    assert db.one("SELECT count(*) AS n FROM recordings")["n"] == 3
    assert db.one("SELECT count(*) AS n FROM sessions")["n"] == 3
    assert db.one("SELECT count(*) AS n FROM labels")["n"] == 2

    fall_label = db.one("SELECT * FROM labels WHERE run_id = 'test_fall_1'")
    assert fall_label["actual_activity"] == "fall"

    cfg = db.one("SELECT * FROM configs")
    assert len(cfg["config_id"]) == 64
    assert cfg["config_id"] == cfg["config_sha256"]

    run_row = db.one("SELECT * FROM runs WHERE run_id = 'test_fall_1'")
    assert len(run_row["config_id"]) == 64
    assert run_row["config_id"] == cfg["config_id"]

    # Second import: verify idempotence (counts must not change)
    res2 = cmd_import_v1(rec_dir, db_path)
    assert res2["total"] == 3
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 3
    assert db.one("SELECT count(*) AS n FROM labels")["n"] == 2


def test_runs_command_output(tmp_path, capsys):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"

    create_sample_recording(rec_dir / "test_fall_1.jsonl", scenario="fall", actual="fall", fsm_highest="NORMAL")
    cmd_import_v1(rec_dir, db_path)

    # JSON output
    capsys.readouterr()  # Clear import output
    runs_json = cmd_runs(db_path, json_output=True)
    captured = capsys.readouterr()
    assert len(runs_json) == 1
    assert runs_json[0]["id"] == "test_fall_1"
    assert runs_json[0]["scenario"] == "fall"
    assert runs_json[0]["ground_truth"] == "fall"
    assert json.loads(captured.out)[0]["id"] == "test_fall_1"

    # Table output
    cmd_runs(db_path, json_output=False)
    captured_tbl = capsys.readouterr()
    assert "ID" in captured_tbl.out
    assert "test_fall_1" in captured_tbl.out
    assert "fall" in captured_tbl.out


def test_replay_offline(tmp_path):
    rec_file = create_sample_recording(tmp_path / "rec.jsonl", num_frames=25)
    t = Thresholds(still=0.03, move=0.05, spike=0.20)

    res = replay_recording_file(rec_file, t)
    assert "fsm_highest" in res
    assert "alert_count" in res
    assert "peak_motion" in res
    assert "frames_processed" in res
    assert res["frames_processed"] == 25
    assert res["peak_motion"] >= 0.0


def test_cmd_replay_with_run_id(tmp_path, capsys):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"
    create_sample_recording(rec_dir / "test_sit_run.jsonl", scenario="sit", actual="normal", num_frames=15)
    cmd_import_v1(rec_dir, db_path)
    capsys.readouterr()  # Clear import output

    res = cmd_replay(
        db_path=db_path,
        data_dir=tmp_path,
        run_id="test_sit_run",
        recording_path=None,
        still=0.04,
        move=0.06,
        spike=0.15,
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["frames_processed"] == 15
    assert res["frames_processed"] == 15


def test_evaluate_confusion_matrix_calculation(tmp_path, monkeypatch):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"

    # Create 4 test recordings
    f1 = create_sample_recording(rec_dir / "run_tp.jsonl", actual="fall")
    f2 = create_sample_recording(rec_dir / "run_fn.jsonl", actual="fall")
    f3 = create_sample_recording(rec_dir / "run_fp.jsonl", actual="normal")
    f4 = create_sample_recording(rec_dir / "run_tn.jsonl", actual="normal")
    cmd_import_v1(rec_dir, db_path)

    # Monkeypatch replay_recording_file to return deterministic states
    state_map = {
        "run_tp": State.FALL_ALERT.value,  # Actual fall + Alert -> TP
        "run_fn": State.NORMAL.value,      # Actual fall + Normal -> FN
        "run_fp": State.FALL_ALERT.value,  # Actual normal + Alert -> FP
        "run_tn": State.NORMAL.value,      # Actual normal + Normal -> TN
    }

    import agent_tools
    def mock_replay(rec_file, thresholds):
        run_id = rec_file.stem
        return {
            "fsm_highest": state_map[run_id],
            "alert_count": 1 if state_map[run_id] == State.FALL_ALERT.value else 0,
            "peak_motion": 0.15,
            "frames_processed": 20,
        }

    monkeypatch.setattr(agent_tools, "replay_recording_file", mock_replay)

    metrics = cmd_evaluate(db_path, tmp_path, json_output=True)

    cm = metrics["confusion_matrix"]
    assert cm["TP"] == 1
    assert cm["FN"] == 1
    assert cm["FP"] == 1
    assert cm["TN"] == 1

    # Recall = TP / (TP + FN) = 1 / 2 = 0.5
    assert metrics["recall"] == 0.5
    # Precision = TP / (TP + FP) = 1 / 2 = 0.5
    assert metrics["precision"] == 0.5
    # F1 = 2 * (0.5 * 0.5) / (0.5 + 0.5) = 0.5
    assert metrics["f1"] == 0.5


def test_sweep_grid_search(tmp_path):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"

    create_sample_recording(rec_dir / "run_fall.jsonl", scenario="fall", actual="fall", num_frames=10)
    create_sample_recording(rec_dir / "run_sit.jsonl", scenario="sit", actual="normal", num_frames=10)
    cmd_import_v1(rec_dir, db_path)

    res = cmd_sweep(
        db_path=db_path,
        data_dir=tmp_path,
        still_range=(0.02, 0.04, 0.02),
        move_range=(0.05, 0.07, 0.02),
        spike_range=(0.08, 0.10, 0.02),
    )

    assert "recommended" in res
    assert "metrics" in res
    assert res["total_evaluated"] > 0
    rec = res["recommended"]
    assert rec["still"] < rec["move"] < rec["spike"]


def test_cli_main_entry_point(tmp_path, capsys):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"
    create_sample_recording(rec_dir / "test_cli_1.jsonl", scenario="fall", actual="fall", num_frames=10)

    # 1. import-v1
    rc = main(["import-v1", "--recordings-dir", str(rec_dir), "--db", str(db_path)])
    assert rc == 0

    # 2. runs
    rc = main(["runs", "--db", str(db_path), "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "test_cli_1" in captured.out

    # 3. replay
    rc = main(["replay", "--recording", str(rec_dir / "test_cli_1.jsonl"), "--still", "0.03", "--move", "0.05", "--spike", "0.10"])
    assert rc == 0

    # 4. evaluate
    rc = main(["evaluate", "--db", str(db_path), "--recordings-dir", str(rec_dir), "--json"])
    assert rc == 0

    # 5. sweep
    rc = main([
        "sweep",
        "--db", str(db_path),
        "--recordings-dir", str(rec_dir),
        "--still-min", "0.02", "--still-max", "0.03", "--still-step", "0.01",
        "--move-min", "0.04", "--move-max", "0.05", "--move-step", "0.01",
        "--spike-min", "0.08", "--spike-max", "0.09", "--spike-step", "0.01",
    ])
    assert rc == 0


def test_evaluate_and_sweep_multiple_label_revisions(tmp_path, monkeypatch):
    rec_dir = tmp_path / "recordings"
    db_path = tmp_path / "telemetry.db"

    create_sample_recording(rec_dir / "run_multi.jsonl", actual="normal", num_frames=10)
    cmd_import_v1(rec_dir, db_path)

    db = TelemetryDB(db_path)
    # Insert revision 2 correcting 'normal' to 'fall'
    db.execute(
        "INSERT INTO labels VALUES (?,?,?,?,?,?,?)",
        ("lbl_rev2", "run_multi", 2, "fall", "FALL_ALERT", "2026-09-29T00:00:00Z", "corrected label"),
    )

    # Verify cmd_runs picks up revision 2
    runs = cmd_runs(db_path, json_output=True)
    assert len(runs) == 1
    assert runs[0]["ground_truth"] == "fall"

    # Mock replay to return FALL_ALERT so fall -> TP
    import agent_tools
    monkeypatch.setattr(
        agent_tools,
        "replay_recording_file",
        lambda *args, **kwargs: {
            "fsm_highest": State.FALL_ALERT.value,
            "alert_count": 1,
            "peak_motion": 0.2,
            "frames_processed": 10,
        },
    )

    metrics = cmd_evaluate(db_path, tmp_path, json_output=True)
    # Must evaluate only 1 run, not 2
    assert metrics["runs_evaluated"] == 1
    assert metrics["confusion_matrix"]["TP"] == 1
    assert metrics["confusion_matrix"]["FP"] == 0
    assert metrics["details"][0]["actual"] == "fall"

    # Sweep should also evaluate exactly 1 run
    res = cmd_sweep(
        db_path=db_path,
        data_dir=tmp_path,
        still_range=(0.02, 0.04, 0.02),
        move_range=(0.05, 0.07, 0.02),
        spike_range=(0.08, 0.10, 0.02),
    )
    assert res["recommended"] is not None

