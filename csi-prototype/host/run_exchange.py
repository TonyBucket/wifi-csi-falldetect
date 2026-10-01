"""
Logical Run Exchange Bundle (host/run_exchange.py)

Exports and imports test runs and recordings as self-contained ZIP bundles.
Guarantees:
- Never copies raw SQLite database files directly between running instances.
- Exactly 2 entries in ZIP: manifest.json and recording.jsonl.
- Prevents zip-slip, zip bomb, and data corruption via SHA-256 validation.
- Idempotent: repeated imports return status='unchanged'.
- Conflict detection: matching run_id with mismatched content returns status='conflict'.
- Single SQLite transaction for DB imports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Support importing within package or as standalone module
try:
    from host.telemetry_db import TelemetryDB
except ImportError:
    from telemetry_db import TelemetryDB


MANIFEST_NAME = "manifest.json"
RECORDING_NAME = "recording.jsonl"
FORMAT_VERSION = "1.0"

# Safety limits
MAX_MANIFEST_SIZE = 8 * 1024 * 1024  # 8 MiB
MAX_RECORDING_SIZE = 2 * 1024 * 1024 * 1024  # 2 GiB
CHUNK_SIZE = 64 * 1024


def compute_file_sha256(path: Path) -> Tuple[str, int]:
    sha = hashlib.sha256()
    total_bytes = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            sha.update(chunk)
            total_bytes += len(chunk)
    return sha.hexdigest(), total_bytes


def export_run(
    db: TelemetryDB,
    data_dir: Path,
    run_id: str,
    dest_path: Path,
) -> Dict[str, Any]:
    """
    Export a COMPLETED run with its recording and metadata into a ZIP bundle.
    """
    data_dir = Path(data_dir)
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Fetch run metadata
    run = db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
    if not run:
        raise ValueError(f"Run '{run_id}' not found in database")

    if run.get("status") != "COMPLETED":
        raise ValueError(
            f"Run '{run_id}' status is '{run.get('status')}'. "
            f"Only COMPLETED runs can be exported."
        )

    recording_id = run.get("recording_id")
    if not recording_id:
        raise ValueError(f"Run '{run_id}' has no associated recording_id")

    recording = db.one("SELECT * FROM recordings WHERE recording_id=?", (recording_id,))
    if not recording:
        raise ValueError(f"Recording '{recording_id}' not found in database")

    # 2. Resolve recording file path
    rec_rel = recording.get("rel_path", "")
    rec_file = data_dir / rec_rel
    if not rec_file.is_file():
        # Fallback check inside data_dir/recordings/<filename>
        rec_file = data_dir / "recordings" / Path(rec_rel).name
        if not rec_file.is_file():
            raise FileNotFoundError(f"Recording file for run '{run_id}' not found: {rec_file}")

    # 3. Checksum verification
    file_sha256, file_bytes = compute_file_sha256(rec_file)
    db_sha256 = recording.get("sha256")
    if db_sha256 and db_sha256 != file_sha256:
        raise ValueError(
            f"Recording checksum mismatch with DB: DB={db_sha256}, disk={file_sha256}"
        )

    # 4. Fetch related tables
    session = None
    if run.get("session_id"):
        session = db.one("SELECT * FROM sessions WHERE session_id=?", (run["session_id"],))

    config = None
    if run.get("config_id"):
        config = db.one("SELECT * FROM configs WHERE config_id=?", (run["config_id"],))

    labels = db.all("SELECT * FROM labels WHERE run_id=? ORDER BY revision", (run_id,))
    events = db.all("SELECT * FROM events WHERE run_id=? ORDER BY server_seq", (run_id,))

    # 5. Build manifest
    manifest = {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "origin_node": socket.gethostname(),
        "run_id": run_id,
        "recording_id": recording_id,
        "recording_sha256": file_sha256,
        "recording_byte_count": file_bytes,
        "tables": {
            "session": session,
            "config": config,
            "run": run,
            "labels": labels,
            "events": events,
            "recording": recording,
        },
    }

    # 6. Write bundle atomically (.part -> final destination)
    part_path = dest_path.with_name(f"{dest_path.name}.part")
    try:
        with zipfile.ZipFile(part_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
            zf.write(rec_file, arcname=RECORDING_NAME)
        part_path.replace(dest_path)
    except Exception:
        if part_path.exists():
            part_path.unlink()
        raise

    return {
        "status": "exported",
        "run_id": run_id,
        "bundle_path": str(dest_path),
        "recording_sha256": file_sha256,
        "recording_byte_count": file_bytes,
    }


def import_run(
    db: TelemetryDB,
    data_dir: Path,
    bundle_path: Path,
) -> Dict[str, Any]:
    """
    Import a run bundle into local DB and recordings directory.
    Validates structure, checksums, and handles idempotency / conflicts.
    """
    data_dir = Path(data_dir)
    bundle_path = Path(bundle_path)

    if not bundle_path.is_file():
        raise FileNotFoundError(f"Bundle file not found: {bundle_path}")

    # 1. Inspect ZIP entries and sizes safely
    try:
        with zipfile.ZipFile(bundle_path, mode="r") as zf:
            entries = zf.namelist()
            expected = {MANIFEST_NAME, RECORDING_NAME}
            if set(entries) != expected or len(entries) != 2:
                raise ValueError(
                    f"Invalid bundle structure: expected exactly {sorted(expected)}, got {entries}"
                )

            manifest_info = zf.getinfo(MANIFEST_NAME)
            recording_info = zf.getinfo(RECORDING_NAME)

            if manifest_info.file_size > MAX_MANIFEST_SIZE:
                raise ValueError(f"Manifest size ({manifest_info.file_size}) exceeds limit ({MAX_MANIFEST_SIZE})")
            if recording_info.file_size > MAX_RECORDING_SIZE:
                raise ValueError(f"Recording size ({recording_info.file_size}) exceeds limit ({MAX_RECORDING_SIZE})")

            # Read manifest
            manifest_bytes = zf.read(MANIFEST_NAME)
            manifest = json.loads(manifest_bytes.decode("utf-8"))

            run_id = manifest.get("run_id")
            if not run_id:
                raise ValueError("Manifest missing 'run_id'")

            expected_sha256 = manifest.get("recording_sha256")
            expected_bytes = manifest.get("recording_byte_count")

            # 2. Extract recording.jsonl to temporary file and verify SHA256
            rec_dir = data_dir / "recordings"
            rec_dir.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_file_str = tempfile.mkstemp(prefix="imp_", suffix=".tmp", dir=str(rec_dir))
            os.close(temp_fd)
            temp_path = Path(temp_file_str)

            try:
                sha = hashlib.sha256()
                bytes_read = 0
                with zf.open(RECORDING_NAME, mode="r") as src, open(temp_path, "wb") as dst:
                    while chunk := src.read(CHUNK_SIZE):
                        sha.update(chunk)
                        dst.write(chunk)
                        bytes_read += len(chunk)

                computed_sha256 = sha.hexdigest()
                if computed_sha256 != expected_sha256:
                    raise ValueError(
                        f"Checksum mismatch: manifest={expected_sha256}, actual={computed_sha256}"
                    )
                if expected_bytes is not None and bytes_read != expected_bytes:
                    raise ValueError(
                        f"Byte count mismatch: manifest={expected_bytes}, actual={bytes_read}"
                    )
            except Exception:
                if temp_path.exists():
                    temp_path.unlink()
                raise
    except zipfile.BadZipFile as e:
        raise ValueError(f"Corrupt or invalid zip archive: {e}")

    # 3. Check for existing run in DB
    existing_run = db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
    if existing_run:
        # Clean up temp extracted file
        if temp_path.exists():
            temp_path.unlink()

        # Check existing recording checksum
        rec_id = existing_run.get("recording_id")
        existing_rec = db.one("SELECT * FROM recordings WHERE recording_id=?", (rec_id,)) if rec_id else None
        existing_sha = existing_rec.get("sha256") if existing_rec else None

        # Compare portable properties
        is_identical = True
        if existing_sha != expected_sha256:
            is_identical = False
        if existing_run.get("scenario") != manifest["tables"]["run"].get("scenario"):
            is_identical = False
        if existing_run.get("csi_frame_count") != manifest["tables"]["run"].get("csi_frame_count"):
            is_identical = False

        if is_identical:
            return {"status": "unchanged", "run_id": run_id}
        else:
            return {
                "status": "conflict",
                "run_id": run_id,
                "reason": "fingerprint mismatch",
                "details": f"Local SHA={existing_sha}, Incoming SHA={expected_sha256}",
            }

    # 4. New run: finalize recording file and insert records in a single transaction
    recording_meta = manifest["tables"]["recording"]
    original_rel = recording_meta.get("rel_path", f"recordings/{run_id}.jsonl")
    safe_basename = Path(original_rel).name
    # Fallback if basename is blank or unsafe
    if not safe_basename or safe_basename in (".", ".."):
        safe_basename = f"{recording_meta.get('recording_id', run_id)}.jsonl"

    final_rec_path = rec_dir / safe_basename
    # If final_rec_path exists with identical hash, reuse; if different hash, pick distinct name
    if final_rec_path.exists():
        exist_hash, _ = compute_file_sha256(final_rec_path)
        if exist_hash != computed_sha256:
            safe_basename = f"{manifest.get('recording_id', run_id)}_{safe_basename}"
            final_rec_path = rec_dir / safe_basename

    try:
        temp_path.replace(final_rec_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    # 5. Insert rows in a single transaction
    try:
        with db.connect() as conn:
            tables = manifest["tables"]

            # Sessions table
            if tables.get("session"):
                conn.execute(
                    "INSERT OR IGNORE INTO sessions VALUES (:session_id,:created_at,:setup_room,:schema_version)",
                    tables["session"],
                )

            # Configs table
            if tables.get("config"):
                conn.execute(
                    "INSERT OR IGNORE INTO configs VALUES (:config_id,:config_json,:config_sha256,:dsp_version)",
                    tables["config"],
                )

            # Runs table
            conn.execute(
                "INSERT INTO runs VALUES (:run_id,:session_id,:scenario,:status,:requested_duration_s,:started_at,:ended_at,:recording_id,:config_id,:csi_frame_count,:wire_frame_count,:gap_count,:fsm_highest,:first_alert_ns,:quality_flags)",
                tables["run"],
            )

            # Recordings table
            rec_row = dict(tables["recording"])
            rec_row["rel_path"] = f"recordings/{safe_basename}"
            rec_row["sha256"] = computed_sha256
            rec_row["byte_count"] = bytes_read
            conn.execute(
                "INSERT INTO recordings VALUES (:recording_id,:run_id,:rel_path,:schema_version,:status,:frame_count,:byte_count,:sha256)",
                rec_row,
            )

            # Labels table
            for lbl in tables.get("labels", []):
                conn.execute(
                    "INSERT OR IGNORE INTO labels VALUES (:label_id,:run_id,:revision,:actual_activity,:verdict,:created_at,:note)",
                    lbl,
                )

            # Events table
            for ev in tables.get("events", []):
                conn.execute(
                    "INSERT OR IGNORE INTO events VALUES (:event_id,:run_id,:action_id,:event_type,:origin,:client_instance_id,:client_seq,:client_wall_ms,:client_mono_ns,:server_wall_ns,:server_mono_ns,:server_seq,:payload_json)",
                    ev,
                )
    except Exception:
        # If DB insert failed, remove recording file to avoid leaving orphan if newly written
        if final_rec_path.exists():
            final_rec_path.unlink()
        raise

    return {"status": "imported", "run_id": run_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Logical Run Exchange Bundle CLI")
    parser.add_argument("--data-dir", default=None, help="Path to data directory")
    parser.add_argument("--db", default=None, help="Path to SQLite telemetry.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # export
    p_exp = subparsers.add_parser("export", help="Export a run into a ZIP bundle")
    p_exp.add_argument("run_id", help="ID of completed run to export")
    p_exp.add_argument("dest_path", help="Path to output bundle zip file")

    # import
    p_imp = subparsers.add_parser("import", help="Import a run bundle")
    p_imp.add_argument("bundle_path", help="Path to incoming bundle zip file")

    args = parser.parse_args()

    default_data = Path(__file__).resolve().parent.parent / "data"
    data_dir = Path(args.data_dir) if args.data_dir else default_data
    db_path = Path(args.db) if args.db else (data_dir / "telemetry.db")

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    db = TelemetryDB(db_path)

    if args.command == "export":
        try:
            res = export_run(db, data_dir, args.run_id, Path(args.dest_path))
            print(json.dumps(res, indent=2))
            sys.exit(0)
        except Exception as e:
            print(f"Export failed: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "import":
        try:
            res = import_run(db, data_dir, Path(args.bundle_path))
            print(json.dumps(res, indent=2))
            if res.get("status") == "conflict":
                sys.exit(2)
            sys.exit(0)
        except Exception as e:
            print(f"Import failed: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
