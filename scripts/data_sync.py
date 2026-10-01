#!/usr/bin/env python3
"""
scripts/data_sync.py

Coordinates test-run data synchronization between tnas and dragonfly:
- Never copies raw SQLite database files directly between running instances.
- Uses logical exchange bundles (ZIP containing manifest.json + recording.jsonl).
- Supports listing local completed runs, bulk exporting, bulk importing.
- Supports peer-to-peer sync over SSH using staging directory (/tmp/run_sync_staging).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Locate paths relative to repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
CSI_DIR = REPO_ROOT / "csi-prototype"
HOST_DIR = CSI_DIR / "host"
sys.path.insert(0, str(HOST_DIR))

try:
    from host.run_exchange import export_run, import_run
    from host.telemetry_db import TelemetryDB
except ImportError:
    from run_exchange import export_run, import_run
    from telemetry_db import TelemetryDB


LOCAL_DATA_DIR = CSI_DIR / "data"
LOCAL_DB_PATH = LOCAL_DATA_DIR / "telemetry.db"
DEFAULT_STAGING = Path("/tmp/run_sync_staging")

PEER_DEFAULTS = {
    "dragonfly": {"user": "hp", "host": "dragonfly", "repo": "/home/hp/workspaces/esp32"},
    "tnas": {"user": "root", "host": "tnas", "repo": "/root/workspaces/esp32"},
}


def get_db(db_path: Optional[Path] = None) -> TelemetryDB:
    path = db_path or LOCAL_DB_PATH
    return TelemetryDB(path)


def list_completed_runs(db: TelemetryDB) -> List[Dict[str, Any]]:
    query = """
    SELECT r.run_id, r.scenario, r.status, r.started_at, r.ended_at,
           r.csi_frame_count, rec.recording_id, rec.byte_count, rec.sha256
    FROM runs r
    LEFT JOIN recordings rec ON r.recording_id = rec.recording_id
    WHERE r.status = 'COMPLETED'
    ORDER BY r.started_at DESC;
    """
    return db.all(query)


def cmd_list_local(args: argparse.Namespace) -> int:
    db = get_db(Path(args.db) if args.db else None)
    runs = list_completed_runs(db)

    if args.json:
        print(json.dumps(runs, indent=2))
        return 0

    if not runs:
        print("No completed runs found.")
        return 0

    print(f"Found {len(runs)} completed run(s):")
    print("-" * 80)
    print(f"{'Run ID':<34} {'Scenario':<16} {'Frames':<8} {'Bytes':<10} {'Started At'}")
    print("-" * 80)
    for r in runs:
        print(
            f"{r['run_id']:<34} {str(r.get('scenario', '')):<16} "
            f"{str(r.get('csi_frame_count', '')):<8} {str(r.get('byte_count', '')):<10} "
            f"{str(r.get('started_at', ''))[:19]}"
        )
    print("-" * 80)
    return 0


def cmd_export_all(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    db = get_db(Path(args.db) if args.db else None)
    data_dir = Path(args.data_dir) if args.data_dir else LOCAL_DATA_DIR

    runs = list_completed_runs(db)
    if not runs:
        print("No completed runs to export.")
        return 0

    print(f"Exporting {len(runs)} run(s) to {out_dir}...")
    success = 0
    for r in runs:
        run_id = r["run_id"]
        dest = out_dir / f"{run_id}.zip"
        try:
            res = export_run(db, data_dir, run_id, dest)
            print(f"  [OK] {run_id} -> {dest.name} ({res['recording_byte_count']} bytes)")
            success += 1
        except Exception as e:
            print(f"  [ERR] {run_id}: {e}", file=sys.stderr)

    print(f"Export completed: {success}/{len(runs)} succeeded.")
    return 0 if success == len(runs) else 1


def cmd_import_all(args: argparse.Namespace) -> int:
    in_dir = Path(args.in_dir)
    if not in_dir.is_dir():
        print(f"Error: Directory not found: {in_dir}", file=sys.stderr)
        return 1

    db = get_db(Path(args.db) if args.db else None)
    data_dir = Path(args.data_dir) if args.data_dir else LOCAL_DATA_DIR

    bundles = sorted(in_dir.glob("*.zip"))
    if not bundles:
        print(f"No .zip bundles found in {in_dir}")
        return 0

    print(f"Importing {len(bundles)} bundle(s) from {in_dir}...")
    imported = 0
    unchanged = 0
    conflicts = 0
    errors = 0

    for b in bundles:
        try:
            res = import_run(db, data_dir, b)
            st = res.get("status")
            if st == "imported":
                imported += 1
                print(f"  [IMPORTED] {b.name} -> {res.get('run_id')}")
            elif st == "unchanged":
                unchanged += 1
                print(f"  [UNCHANGED] {b.name} -> {res.get('run_id')}")
            elif st == "conflict":
                conflicts += 1
                print(f"  [CONFLICT] {b.name} -> {res.get('run_id')}: {res.get('reason')}", file=sys.stderr)
            else:
                print(f"  [?] {b.name}: {res}")
        except Exception as e:
            errors += 1
            print(f"  [ERR] {b.name}: {e}", file=sys.stderr)

    print(f"Import results: {imported} imported, {unchanged} unchanged, {conflicts} conflicts, {errors} errors.")
    return 0 if (conflicts == 0 and errors == 0) else 1


def _resolve_peer_target(peer_name: str) -> tuple[str, str]:
    """Returns (ssh_target, remote_repo_path)."""
    if peer_name in PEER_DEFAULTS:
        p = PEER_DEFAULTS[peer_name]
        return f"{p['user']}@{p['host']}", p["repo"]
    elif "@" in peer_name:
        # e.g. hp@dragonfly
        host = peer_name.split("@")[-1]
        repo = "/home/hp/workspaces/esp32" if "dragonfly" in host or "hp" in peer_name else "/root/workspaces/esp32"
        return peer_name, repo
    else:
        # hostname only
        user = "hp" if "dragonfly" in peer_name else "root"
        repo = "/home/hp/workspaces/esp32" if "dragonfly" in peer_name else "/root/workspaces/esp32"
        return f"{user}@{peer_name}", repo


def cmd_sync_peer(args: argparse.Namespace) -> int:
    peer_arg = args.peer
    ssh_target, peer_repo = _resolve_peer_target(peer_arg)
    staging_dir = Path(args.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    remote_staging = "/tmp/run_sync_staging"

    db = get_db(Path(args.db) if args.db else None)
    data_dir = Path(args.data_dir) if args.data_dir else LOCAL_DATA_DIR

    print(f"Connecting to peer: {ssh_target} (peer repo: {peer_repo})...")

    # 1. Probe peer and ensure remote staging dir exists
    probe_cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ssh_target,
        f"mkdir -p {remote_staging} && test -d {peer_repo} && echo 'OK'"
    ]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True)
    if probe.returncode != 0 or "OK" not in probe.stdout:
        print(f"Error: Unable to connect to peer {ssh_target}: {probe.stderr.strip()}", file=sys.stderr)
        return 1

    # 2. Get peer's list of completed runs
    list_peer_cmd = [
        "ssh", ssh_target,
        f"python3 {peer_repo}/scripts/data_sync.py list-local --json"
    ]
    res = subprocess.run(list_peer_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error listing runs on peer: {res.stderr.strip()}", file=sys.stderr)
        return 1

    try:
        peer_runs = json.loads(res.stdout)
    except Exception as e:
        print(f"Failed to parse peer run list: {e}\nRaw output: {res.stdout}", file=sys.stderr)
        return 1

    # 3. Compare with local runs
    local_runs = list_completed_runs(db)
    local_run_ids = {r["run_id"] for r in local_runs}
    peer_run_ids = {r["run_id"] for r in peer_runs}

    missing_locally = [r for r in peer_runs if r["run_id"] not in local_run_ids]
    missing_on_peer = [r for r in local_runs if r["run_id"] not in peer_run_ids]

    print(f"Peer status: {len(peer_runs)} completed run(s).")
    print(f"Local status: {len(local_runs)} completed run(s).")
    print(f"Missing locally (pull candidate): {len(missing_locally)}")
    print(f"Missing on peer (push candidate): {len(missing_on_peer)}")

    imported_count = 0
    pushed_count = 0

    # 4. Pull missing runs from peer -> local
    if missing_locally:
        print(f"\n--- Pulling {len(missing_locally)} run(s) from {ssh_target} ---")
        for r in missing_locally:
            rid = r["run_id"]
            bundle_filename = f"{rid}.zip"
            remote_bundle = f"{remote_staging}/{bundle_filename}"
            local_bundle = staging_dir / bundle_filename

            print(f"Fetching run '{rid}'...")
            # Peer exports to remote staging
            exp_cmd = [
                "ssh", ssh_target,
                f"cd {peer_repo}/csi-prototype && python3 -m host.run_exchange export {rid} {remote_bundle}"
            ]
            exp_proc = subprocess.run(exp_cmd, capture_output=True, text=True)
            if exp_proc.returncode != 0:
                print(f"  [ERR] Remote export failed for {rid}: {exp_proc.stderr.strip()}", file=sys.stderr)
                continue

            # SCP from remote staging to local staging
            scp_cmd = ["scp", f"{ssh_target}:{remote_bundle}", str(local_bundle)]
            scp_proc = subprocess.run(scp_cmd, capture_output=True, text=True)

            # Cleanup remote bundle
            subprocess.run(["ssh", ssh_target, f"rm -f {remote_bundle}"])

            if scp_proc.returncode != 0 or not local_bundle.is_file():
                print(f"  [ERR] SCP transfer failed for {rid}", file=sys.stderr)
                if local_bundle.exists():
                    local_bundle.unlink()
                continue

            # Local import
            try:
                imp_res = import_run(db, data_dir, local_bundle)
                status = imp_res.get("status")
                print(f"  [{status.upper()}] {rid}")
                if status == "imported":
                    imported_count += 1
            except Exception as e:
                print(f"  [ERR] Local import failed for {rid}: {e}", file=sys.stderr)
            finally:
                if local_bundle.exists():
                    local_bundle.unlink()

    # 5. Push missing runs local -> peer (if bidirectional sync or requested)
    if args.direction in ("push", "both") and missing_on_peer:
        print(f"\n--- Pushing {len(missing_on_peer)} run(s) to {ssh_target} ---")
        for r in missing_on_peer:
            rid = r["run_id"]
            bundle_filename = f"{rid}.zip"
            local_bundle = staging_dir / bundle_filename
            remote_bundle = f"{remote_staging}/{bundle_filename}"

            print(f"Pushing run '{rid}'...")
            try:
                export_run(db, data_dir, rid, local_bundle)
            except Exception as e:
                print(f"  [ERR] Local export failed for {rid}: {e}", file=sys.stderr)
                continue

            # SCP to remote staging
            scp_cmd = ["scp", str(local_bundle), f"{ssh_target}:{remote_bundle}"]
            scp_proc = subprocess.run(scp_cmd, capture_output=True, text=True)
            if local_bundle.exists():
                local_bundle.unlink()

            if scp_proc.returncode != 0:
                print(f"  [ERR] SCP push failed for {rid}", file=sys.stderr)
                continue

            # Remote import
            imp_cmd = [
                "ssh", ssh_target,
                f"cd {peer_repo}/csi-prototype && python3 -m host.run_exchange import {remote_bundle} && rm -f {remote_bundle}"
            ]
            imp_proc = subprocess.run(imp_cmd, capture_output=True, text=True)
            if imp_proc.returncode == 0:
                print(f"  [IMPORTED ON PEER] {rid}")
                pushed_count += 1
            else:
                print(f"  [ERR] Remote import failed for {rid}: {imp_proc.stderr.strip()}", file=sys.stderr)

    print(f"\nSync complete. Pulled {imported_count} run(s), pushed {pushed_count} run(s).")
    return 0


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="Path to SQLite telemetry.db")
    common.add_argument("--data-dir", default=None, help="Path to data directory")

    parser = argparse.ArgumentParser(description="Logical Data Exchange Coordinator", parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list-local
    p_list = subparsers.add_parser("list-local", help="List completed runs in local database", parents=[common])
    p_list.add_argument("--json", action="store_true", help="Output in JSON format")

    # export-all
    p_exp = subparsers.add_parser("export-all", help="Export all completed runs into bundle zip files", parents=[common])
    p_exp.add_argument("--out-dir", required=True, help="Destination directory for zip bundles")

    # import-all
    p_imp = subparsers.add_parser("import-all", help="Import all bundle zip files from a directory", parents=[common])
    p_imp.add_argument("--in-dir", required=True, help="Source directory containing zip bundles")

    # sync-peer
    p_sync = subparsers.add_parser("sync-peer", help="Synchronize completed runs with remote peer via SSH", parents=[common])
    p_sync.add_argument("--peer", required=True, help="Peer name (dragonfly|tnas) or [user@]host")
    p_sync.add_argument(
        "--direction",
        choices=["pull", "push", "both"],
        default="both",
        help="Sync direction: pull (peer->local), push (local->peer), both (default: both)",
    )
    p_sync.add_argument(
        "--staging-dir",
        default=str(DEFAULT_STAGING),
        help="Local temporary staging directory",
    )

    args = parser.parse_args()

    if args.command == "list-local":
        sys.exit(cmd_list_local(args))
    elif args.command == "export-all":
        sys.exit(cmd_export_all(args))
    elif args.command == "import-all":
        sys.exit(cmd_import_all(args))
    elif args.command == "sync-peer":
        sys.exit(cmd_sync_peer(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
