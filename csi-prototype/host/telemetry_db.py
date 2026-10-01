"""SQLite storage for test-run metadata; each operation owns its connection."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, created_at TEXT, setup_room TEXT, schema_version INT);
CREATE TABLE IF NOT EXISTS configs (config_id TEXT PRIMARY KEY, config_json TEXT, config_sha256 TEXT, dsp_version TEXT);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, session_id TEXT, scenario TEXT, status TEXT, requested_duration_s REAL, started_at TEXT, ended_at TEXT, recording_id TEXT, config_id TEXT, csi_frame_count INT, wire_frame_count INT, gap_count INT, fsm_highest TEXT, first_alert_ns INT, quality_flags TEXT);
CREATE TABLE IF NOT EXISTS labels (label_id TEXT PRIMARY KEY, run_id TEXT, revision INT, actual_activity TEXT, verdict TEXT, created_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, run_id TEXT, action_id TEXT, event_type TEXT, origin TEXT, client_instance_id TEXT, client_seq INT, client_wall_ms INT, client_mono_ns TEXT, server_wall_ns INT, server_mono_ns INT, server_seq INT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS recordings (recording_id TEXT PRIMARY KEY, run_id TEXT, rel_path TEXT, schema_version INT, status TEXT, frame_count INT, byte_count INT, sha256 TEXT);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS labels_run ON labels(run_id, revision);
CREATE UNIQUE INDEX IF NOT EXISTS labels_rev ON labels(run_id, revision);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, server_seq);
"""


class TelemetryDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL;")
            db.execute("PRAGMA synchronous=NORMAL;")
            with db:
                yield db
        finally:
            db.close()

    def execute(self, statement: str, params=()):
        with self.connect() as db:
            return db.execute(statement, params).rowcount

    def one(self, statement: str, params=()):
        with self.connect() as db:
            row = db.execute(statement, params).fetchone()
            return dict(row) if row else None

    def all(self, statement: str, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(statement, params)]

    def create_run(self, run: dict, recording: dict, session: dict, config: dict):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO sessions VALUES (:session_id,:created_at,:setup_room,:schema_version)", session)
            db.execute("INSERT OR IGNORE INTO configs VALUES (:config_id,:config_json,:config_sha256,:dsp_version)", config)
            db.execute("INSERT INTO runs VALUES (:run_id,:session_id,:scenario,:status,:requested_duration_s,:started_at,:ended_at,:recording_id,:config_id,:csi_frame_count,:wire_frame_count,:gap_count,:fsm_highest,:first_alert_ns,:quality_flags)", run)
            db.execute("INSERT INTO recordings VALUES (:recording_id,:run_id,:rel_path,:schema_version,:status,:frame_count,:byte_count,:sha256)", recording)

    def transition(self, run_id: str, old: str, new: str, **fields):
        columns = ["status=?", *(f"{key}=?" for key in fields)]
        return self.execute(f"UPDATE runs SET {', '.join(columns)} WHERE run_id=? AND status=?",
                            (new, *fields.values(), run_id, old)) == 1

    def detail(self, run_id: str):
        run = self.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if run is None:
            return None
        return {"run": run,
                "labels": self.all("SELECT * FROM labels WHERE run_id=? ORDER BY revision", (run_id,)),
                "events": self.all("SELECT * FROM events WHERE run_id=? ORDER BY server_seq", (run_id,)),
                "config": self.one("SELECT * FROM configs WHERE config_id=?", (run["config_id"],)),
                "recording": self.one("SELECT * FROM recordings WHERE recording_id=?", (run["recording_id"],))}

    def insert_events(self, rows):
        with self.connect() as db:
            db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
