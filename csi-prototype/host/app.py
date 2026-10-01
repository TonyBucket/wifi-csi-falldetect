"""Local CSI recorder, replay, and live dashboard API."""

import argparse
import asyncio
import base64
from collections import deque
import json
from pathlib import Path
import re
import sqlite3
import statistics
import time
import fcntl
import hashlib
import math
import uuid
from datetime import datetime, timezone
import socket

from aiohttp import web
import serial
from serial.tools import list_ports

try:
    from .wire import ACK, COMMAND, Parser, STATS, encode, mac, parse_csi
    from .dsp import amplitudes, Thresholds, MotionDSP, Calibrator, normalize
    from .fsm import FallFSM, State as FsmState
    from .detector import FallDetector
    from .classifier import FallClassifier
    from .telemetry_db import TelemetryDB
except ImportError:
    from wire import ACK, COMMAND, Parser, STATS, encode, mac, parse_csi
    from dsp import amplitudes, Thresholds, MotionDSP, Calibrator, normalize
    from fsm import FallFSM, State as FsmState
    from detector import FallDetector
    from classifier import FallClassifier
    from telemetry_db import TelemetryDB

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RECORDINGS = ROOT / "data" / "recordings"
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\Z")
ACTIONS = {"identify": 1, "FALL_DETECTED": 2, "FALL_CONFIRMED": 3, "reset": 4}
FSM_RANK = {state: rank for rank, state in enumerate(
    (FsmState.NORMAL, FsmState.MOVING, FsmState.CANDIDATE, FsmState.FALL_ALERT))}


class Hub:
    def __init__(self) -> None:
        self.mode = "LIVE"
        self.port: serial.Serial | None = None
        self.lock = None
        self.port_name: str | None = None
        self.source: str | None = None
        self.receiver: str | None = None
        self.verified = False
        self.parser = Parser()
        self.trace: deque[dict] = deque(maxlen=300)
        self.events: deque[dict] = deque(maxlen=100)
        self.arrivals: deque[int] = deque(maxlen=500)
        self.last_sample_ns: int | None = None
        self.last_capture_us: int | None = None
        self.last_seq: int | None = None
        self.stats: dict = {}
        self.sequence_gaps = 0
        self.rejected = 0
        self.host_record_drops = 0
        self.recording_status = {"state": "idle", "name": None}
        self.record_counts = {"frames": 0, "markers": 0}
        self.recorder = None
        self.record_name: str | None = None
        self.run_recorder = None
        self.run_id: str | None = None
        self.run_frames = self.run_csi = 0
        self.run_gap_start = 0
        self.run_record_error: str | None = None
        self.run_fsm_highest = FsmState.NORMAL
        self.run_db: TelemetryDB | None = None
        self.command: dict = {"desired": None, "ack": None, "physical_observation": "unverified"}
        self.command_id = 0
        self.selected_pair = 2
        self.reader: asyncio.Task | None = None
        self.replayer: asyncio.Task | None = None
        # ponytail: preserve existing uncalibrated limits; calibrate from measured still/walk samples.
        self.thresholds = Thresholds(still=0.0459, move=0.0616, spike=0.2159)
        self.dsp: MotionDSP | None = None
        self.classifier = FallClassifier()
        self.detector = FallDetector(self.thresholds, self.classifier)
        self.fsm = self.detector.fsm
        self.last_motion: float | None = None
        self.burst_ratio: float | None = None
        self.motion_slope: float | None = None
        self.spectral_dist: float | None = None
        self.room_presence = "UNKNOWN"
        self.empty_baseline = None
        self.empty_capture: list[tuple[int, list[float]]] | None = None
        self.calibrating_step: int | None = None
        self.calib_still_samples: list[tuple[int, list[float]]] = []
        self.calib_walk_samples: list[float] = []
        self.audit: deque[dict] = deque(maxlen=100)
        self.telemetry: deque[dict] = deque(maxlen=500)
        self.last_telemetry_ns: int | None = None
        self.last_mean_amp: float | None = None
        self.last_rssi: int | None = None

    def replay_still(self) -> tuple[MotionDSP, list[float]]:
        if len(self.calib_still_samples) < 5:
            raise ValueError("need at least five still samples")
        dsp = MotionDSP(Calibrator.baseline(self.calib_still_samples))
        motions = [motion for capture_us, amps in self.calib_still_samples
                   if (motion := dsp.push(amps, capture_us)) is not None]
        return dsp, motions

    def append_jsonl(self, name: str, entry: dict) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / name
        if name == "telemetry.jsonl" and path.exists() and path.stat().st_size > 5_000_000:
            path.replace(DATA / "telemetry.previous.jsonl")
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def log_audit(self, event: str, params: dict, outcome: str, detail: str = "") -> None:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "timestamp_ns": time.time_ns(),
                 "mono_ns": time.monotonic_ns(), "event": event,
                 "action_id": params.get("action_id"), "params": params,
                 "outcome": outcome, "detail": detail}
        self.audit.append(entry)
        self.append_jsonl("audit.jsonl", entry)

    def score(self, motion: float | None) -> float | None:
        if motion is None:
            return None
        t = self.thresholds
        if motion <= t.still:
            return 20 * motion / t.still if t.still else 0.0
        if motion <= t.move:
            return 20 + 30 * (motion - t.still) / (t.move - t.still)
        if motion <= t.spike:
            return 50 + 30 * (motion - t.move) / (t.spike - t.move)
        return min(100, 80 + 20 * (motion - t.spike) / max(t.spike, 1e-6))

    def log_telemetry(self, wall_ns: int, mono_ns: int) -> None:
        if self.last_telemetry_ns is not None and mono_ns - self.last_telemetry_ns < 200_000_000:
            return
        self.last_telemetry_ns = mono_ns
        while self.arrivals and mono_ns - self.arrivals[0] > 1_000_000_000:
            self.arrivals.popleft()
        entry = {"timestamp_ms": wall_ns // 1_000_000, "rx_rate_hz": len(self.arrivals),
                 "raw_motion": self.last_motion, "motion_score": self.score(self.last_motion),
                 "burst_ratio": self.burst_ratio, "motion_slope": self.motion_slope,
                 "spectral_dist": self.spectral_dist, "room_presence": self.room_presence,
                 "fsm_state": self.fsm.state.value, "mean_amp": self.last_mean_amp,
                 "rssi_approx": self.last_rssi, "drops": self.stats.get("queue_full", 0) + self.host_record_drops,
                 "sequence_gaps": self.sequence_gaps}
        self.telemetry.append(entry)
        try:
            self.append_jsonl("telemetry.jsonl", entry)
        except OSError as exc:
            self.events.append({"type": "telemetry_error", "message": str(exc)})

    def snapshot(self) -> dict:
        now = time.monotonic_ns()
        age = None if self.last_sample_ns is None else max(0, (now-self.last_sample_ns)//1_000_000)
        health = "UNKNOWN" if age is None else "STALE" if age > 2000 else "OK"
        if self.mode == "LIVE" and (not self.port or not self.port.is_open):
            health = "UNKNOWN"
        if self.mode == "REPLAY" and self.replayer is None:
            health = "UNKNOWN"
        if self.mode == "REPLAY" and age is not None and age > 2000:
            health = "UNKNOWN"
        trace = list(self.trace)
        boundary = max((i for i, point in enumerate(trace) if point["gap"]), default=0)
        recent = [p["real"] for p in trace[boundary:] if p["valid"] and p["t_ms"] >= (self.last_capture_us or 0)//1000-1000]
        variation = None if health != "OK" or len(recent) < 2 else statistics.pstdev(recent)
        while self.arrivals and now-self.arrivals[0] > 1_000_000_000:
            self.arrivals.popleft()
        motion_gauge = self.score(self.last_motion) or 0.0

        fsm_state_str = self.fsm.state.value if hasattr(self.fsm.state, "value") else str(self.fsm.state)

        fsm_info = {
            "state": fsm_state_str,
            "motion_score": round(motion_gauge, 1),
            "raw_motion": round(self.last_motion, 4) if self.last_motion is not None else None,
            "thresholds": {
                "still": round(self.thresholds.still, 4),
                "move": round(self.thresholds.move, 4),
                "spike": round(self.thresholds.spike, 4),
                "candidate_spike": self.thresholds.candidate_spike,
                "burst": self.thresholds.burst,
                "empty": self.thresholds.empty,
            } if self.thresholds else None,
            "thresholds_mapped": {"still": 20, "move": 50, "spike": 80},
            "calibrating_step": self.calibrating_step,
            "classifier": {
                "score": round(self.detector.last_prediction.score, 3) if self.detector.last_prediction else None,
                "is_fall": self.detector.last_prediction.is_fall if self.detector.last_prediction else None,
                "reason": self.detector.last_prediction.reason if self.detector.last_prediction else None,
                "confirmed": self.detector.classifier_confirmed,
            },
        }
        return {"mode": self.mode, "connection": "open" if self.port and self.port.is_open else "disconnected",
                "burst_ratio": self.burst_ratio if health == "OK" else None,
                "motion_slope": self.motion_slope if health == "OK" else None,
                "spectral_dist": self.spectral_dist if health == "OK" else None,
                "room_presence": self.room_presence if health == "OK" else "UNKNOWN",
                "port": self.port_name, "source_verified": self.verified, "source_mac": self.source, "rx_mac": self.receiver,
                "rx_rate_hz": len(self.arrivals), "last_sample_age_ms": age, "csi_health": health,
                "stats": self.stats, "frame_errors": self.parser.errors + self.rejected,
                "host_record_drops": self.host_record_drops,
                "sequence_gaps": self.sequence_gaps, "selected_pair": self.selected_pair,
                "trace": list(self.trace), "variation": {"value": variation, "unit": "CSI signed-LSB stddev",
                "window_ms": 1000, "valid_samples": len(recent) if health == "OK" else 0},
                "recording": self.record_name, "recording_status": self.recording_status,
                "command": self.command, "events": list(self.events), "fsm": fsm_info}

    def ingest(self, data: bytes, wall_ns: int | None = None, mono_ns: int | None = None, *, replay: bool = False) -> None:
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        mono_ns = time.monotonic_ns() if mono_ns is None else mono_ns
        for kind, payload, raw in self.parser.feed(data):
            accepted = False
            if kind == 1:
                try:
                    csi = parse_csi(payload, self.source or "", self.receiver or "")
                except ValueError:
                    self.rejected += 1
                    continue
                seq = csi["rx_seq"]
                clock_gap = (self.last_capture_us is not None and
                             (csi["capture_us"] <= self.last_capture_us or csi["capture_us"] - self.last_capture_us > 1_000_000))
                if clock_gap:
                    self.events.append({"type": "rx_clock_reset_or_reorder", "previous_us": self.last_capture_us,
                                        "current_us": csi["capture_us"]})
                    if csi["capture_us"] <= self.last_capture_us:
                        self.last_seq = None
                if self.last_seq is not None:
                    gap = (seq-self.last_seq-1) & 0xffffffff
                    if gap < 0x80000000:
                        self.sequence_gaps += gap
                    else:
                        self.events.append({"type": "rx_sequence_reset_or_reorder", "previous": self.last_seq, "current": seq})
                        gap = 1
                else:
                    gap = 0
                gap = max(gap, int(bool(clock_gap)))
                self.last_seq = seq
                self.last_capture_us = csi["capture_us"]
                self.last_sample_ns = time.monotonic_ns()
                self.arrivals.append(mono_ns)
                self.last_rssi = csi["rssi"]
                pairs = csi["pairs"]
                index = self.selected_pair
                valid = index < len(pairs) and not (csi["first_word_invalid"] and index < 2)
                imag, real = pairs[index] if valid else (None, None)
                self.trace.append({"t_ms": csi["capture_us"]//1000, "real": real, "imag": imag,
                                   "valid": valid, "gap": bool(gap)})
                if len(csi["pairs"]) in (64, 128):
                    try:
                        amps = amplitudes(csi["pairs"], first_word_invalid=csi["first_word_invalid"])
                        self.last_mean_amp = sum(amps) / 56
                        if self.calibrating_step == 1:
                            if len(self.calib_still_samples) < 3000:
                                self.calib_still_samples.append((csi["capture_us"], amps))
                        if self.dsp is None:
                            self.dsp = MotionDSP(normalize(amps))
                            self.dsp.empty_baseline = self.empty_baseline
                        if self.empty_capture is not None:
                            if self.empty_capture and csi["capture_us"] <= self.empty_capture[-1][0]:
                                self.empty_capture.clear()
                            self.empty_capture.append((csi["capture_us"], amps))
                            if (len(self.empty_capture) >= 2 and
                                    csi["capture_us"] - self.empty_capture[0][0] >= 2_000_000):
                                self.dsp.set_empty_baseline([row for _, row in self.empty_capture])
                                self.empty_baseline = self.dsp.get_empty_baseline()
                                self.empty_capture = None
                                self.events.append({"type": "empty_room_captured"})
                        self.spectral_dist = self.dsp.spectral_distance(amps) if hasattr(self.dsp, "spectral_distance") else None
                        self.room_presence = ("UNKNOWN" if self.spectral_dist is None else
                                              "EMPTY_COMPATIBLE" if self.spectral_dist < self.thresholds.empty else
                                              "NONEMPTY_CHANGED")
                        motion = self.dsp.push(amps, csi["capture_us"])
                        if motion is not None:
                            self.last_motion = motion
                            if hasattr(self.dsp, "push_motion_energy"):
                                noise_floor = (self.thresholds.still ** 2) if self.thresholds else 0.0
                                self.dsp.push_motion_energy(csi["capture_us"], motion * motion, activity=motion, noise_floor=noise_floor)
                                self.burst_ratio = self.dsp.burst_ratio
                                self.motion_slope = self.dsp.motion_slope
                            if self.calibrating_step == 2:
                                if len(self.calib_walk_samples) < 3000:
                                    self.calib_walk_samples.append(motion)
                        state = self.detector.update(csi["capture_us"], motion, amps,
                                                     burst_ratio=self.burst_ratio if motion is not None else None,
                                                     is_empty_room=True if self.room_presence == "EMPTY_COMPATIBLE" else
                                                     False if self.room_presence == "NONEMPTY_CHANGED" else None,
                                                     motion_slope=self.motion_slope)
                        if self.run_id and self.run_db and state in FSM_RANK and FSM_RANK[state] > FSM_RANK[self.run_fsm_highest]:
                            self.run_fsm_highest = state
                    except Exception as err:
                        self.events.append({"type": "dsp_error", "message": str(err)})
                self.log_telemetry(wall_ns, mono_ns)
                accepted = True
            elif kind == 2 and len(payload) == STATS.size and mac(payload[:6]) == self.receiver:
                values = STATS.unpack(payload)
                self.stats = dict(zip(("callbacks", "enqueued", "queue_full", "oversize", "serial_write_fail", "source_filtered"), values[1:]))
                accepted = True
            elif kind == 4 and len(payload) == ACK.size:
                command_id, status = ACK.unpack(payload)
                if command_id == self.command_id:
                    self.command["ack"] = {"id": command_id, "status": status}
                accepted = True
            else:
                self.rejected += 1
            if self.recorder and not replay and accepted:
                self.write_record({"type": "frame", "raw": base64.b64encode(raw).decode(),
                                   "wall_ns": wall_ns, "mono_ns": mono_ns})
            if self.run_recorder and not replay and accepted:
                capture_us = rx_seq = None
                if kind == 1:
                    capture_us, rx_seq = csi["capture_us"], csi["rx_seq"]
                frame = {"version": 2, "run_id": self.run_id, "frame_index": self.run_frames,
                         "frame_kind": {1: "csi", 2: "stats", 4: "ack"}[kind],
                         "capture_us": capture_us, "rx_seq": rx_seq,
                         "raw": base64.b64encode(raw).decode()}
                try:
                    self.run_recorder.write(json.dumps(frame, separators=(",", ":")) + "\n")
                    self.run_recorder.flush()
                    self.run_frames += 1
                    self.run_csi += kind == 1
                except OSError as exc:
                    self.run_record_error = str(exc)
                    self.close_run_record()

    def close_run_record(self):
        recorder, self.run_recorder = self.run_recorder, None
        if recorder:
            try:
                recorder.close()
            except OSError as exc:
                self.run_record_error = str(exc)
        self.run_id = None

    def write_record(self, record: dict) -> None:
        if self.recorder:
            try:
                self.recorder.write(json.dumps(record, separators=(",", ":")) + "\n")
                self.recorder.flush()
                if record["type"] == "frame":
                    self.record_counts["frames"] += 1
                elif record["type"] == "marker":
                    self.record_counts["markers"] += 1
            except OSError as exc:
                self.host_record_drops += 1
                self.recording_status = {"state": "incomplete", "name": self.record_name, "error": str(exc)}
                recorder = self.recorder
                self.recorder = None
                self.record_name = None
                try:
                    recorder.close()
                except OSError:
                    pass

    def stop_record(self) -> None:
        if self.recorder:
            self.write_record({"type": "end", "wall_ns": time.time_ns(), "mono_ns": time.monotonic_ns(),
                               "counts": self.record_counts.copy()})
        if self.recorder:
            try:
                self.recorder.close()
            except OSError as exc:
                self.host_record_drops += 1
                self.recording_status = {"state": "incomplete", "name": self.record_name, "error": str(exc)}
            else:
                self.recording_status = {"state": "complete", "name": self.record_name}
            finally:
                self.recorder = None
                self.record_name = None

    async def disconnect(self) -> None:
        self.stop_record()
        self.close_run_record()
        if self.reader:
            self.reader.cancel()
            try:
                await self.reader
            except asyncio.CancelledError:
                pass
            self.reader = None
        if self.port:
            self.port.close()
            self.port = None
        if self.lock:
            self.lock.close()
            self.lock = None
        self.port_name = None
        self.verified = False
        self.last_sample_ns = None
        self.arrivals.clear()
        self.trace.clear()
        self.last_seq = self.last_capture_us = None
        self.stats = {}
        self.source = self.receiver = None
        self.command = {"desired": None, "ack": None, "physical_observation": "unverified"}
        self.dsp = None
        self.detector = FallDetector(self.thresholds, self.classifier)
        self.fsm = self.detector.fsm
        self.last_motion = None
        self.calibrating_step = None
        self.calib_still_samples.clear()
        self.calib_walk_samples.clear()

    async def read_loop(self) -> None:
        try:
            while self.port:
                data = await asyncio.to_thread(self.port.read, 4096)
                if data:
                    self.ingest(data)
                elif not self.port.is_open:
                    raise serial.SerialException("receiver port closed")
                elif not (self.port_name or "").startswith("socket://") and not Path(self.port_name or "").exists():
                    raise serial.SerialException("receiver device disappeared")
        except (serial.SerialException, OSError) as exc:
            self.events.append({"type": "serial_error", "message": str(exc)})
            try:
                self.log_audit("disconnect", {}, "failed", str(exc))
            except OSError:
                pass
            if self.port:
                self.port.close()
                self.port = None
            if self.lock:
                self.lock.close()
                self.lock = None
            self.port_name = None
            self.verified = False
            self.stop_record()
            run_id = self.run_id
            self.close_run_record()
            if run_id and self.run_db:
                now = datetime.now(timezone.utc).isoformat()
                with self.run_db.connect() as db:
                    db.execute("UPDATE runs SET status='INTERRUPTED',ended_at=?,csi_frame_count=?,wire_frame_count=?,gap_count=?,fsm_highest=?,quality_flags=? WHERE run_id=? AND status IN ('PREPARING','RUNNING')",
                               (now, self.run_csi, self.run_frames, max(0, self.sequence_gaps - self.run_gap_start),
                                self.run_fsm_highest.value, json.dumps([str(exc)]), run_id))
                    db.execute("UPDATE recordings SET status='INCOMPLETE',frame_count=? WHERE run_id=?", (self.run_frames, run_id))
            self.last_sample_ns = None

    def send_command(self, action: str, ttl: int) -> None:
        if (not self.port or not self.verified or self.mode != "LIVE" or action not in ACTIONS
                or type(ttl) is not int or not 1 <= ttl <= 2000):
            raise ValueError("verified live RX, manual_test action and TTL 1..2000 required")
        self.command_id = (self.command_id+1) & 0xffffffff
        self.port.write(encode(3, COMMAND.pack(self.command_id, ACTIONS[action], ttl, 1)))
        self.command = {"desired": {"id": self.command_id, "action": action, "ttl_ms": ttl, "source": "manual_test"},
                        "ack": None, "physical_observation": "unverified"}


def make_app(hub: Hub | None = None, allow_remote: bool = False) -> web.Application:
    hub = hub or Hub()
    db = TelemetryDB(DATA / "telemetry.db")
    hub.run_db = db

    @web.middleware
    async def guard(request: web.Request, handler):
        host = request.host
        if not allow_remote:
            if host not in ("localhost", "127.0.0.1", "[::1]") and not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\]):\d{1,5}", host):
                raise web.HTTPForbidden(text="local host required")
        origin = request.headers.get("Origin")
        if origin:
            if origin != f"{request.scheme}://{host}":
                raise web.HTTPForbidden(text="origin rejected")
        if request.method == "POST" or request.path == "/ws":
            if not allow_remote and request.remote not in ("127.0.0.1", "::1"):
                raise web.HTTPForbidden(text="local clients only")
            if request.method == "POST" and request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
        return await handler(request)

    app = web.Application(middlewares=[guard])

    async def runs_create(request):
        try:
            body = await request.json()
            scenario, duration = body.get("scenario"), body.get("requested_duration_s")
            if (not isinstance(scenario, str) or not 1 <= len(scenario) <= 100 or
                    type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 3600):
                raise ValueError("valid scenario and duration (0, 3600] required")
            if hub.run_recorder or hub.recorder or hub.mode != "LIVE":
                raise web.HTTPConflict(text="recording already active or replaying")
            run_id = uuid.uuid4().hex
            recording_id = uuid.uuid4().hex
            session_id = body.get("session_id", uuid.uuid4().hex)
            if not isinstance(session_id, str) or not NAME.fullmatch(session_id):
                raise ValueError("invalid session_id")
            if not isinstance(body.get("setup_room", ""), str) or len(body.get("setup_room", "")) > 200:
                raise ValueError("invalid setup_room")
            config_json = json.dumps({"thresholds": {key: getattr(hub.thresholds, key) for key in ("still", "move", "spike")},
                                      "selected_pair": hub.selected_pair}, sort_keys=True)
            config_sha = hashlib.sha256(config_json.encode()).hexdigest()
            now = datetime.now(timezone.utc).isoformat()
            run = dict(run_id=run_id, session_id=session_id, scenario=scenario, status="PREPARING",
                       requested_duration_s=duration, started_at=None, ended_at=None, recording_id=recording_id,
                       config_id=config_sha, csi_frame_count=0, wire_frame_count=0, gap_count=0,
                       fsm_highest=FsmState.NORMAL.value, first_alert_ns=None, quality_flags="[]")
            recording = dict(recording_id=recording_id, run_id=run_id,
                             rel_path=f"recordings/{run_id}.jsonl", schema_version=2,
                             status="RECORDING", frame_count=0, byte_count=0, sha256=None)
            RECORDINGS.mkdir(parents=True, exist_ok=True)
            recorder = (RECORDINGS / f"{run_id}.jsonl").open("x", encoding="utf-8")
            try:
                db.create_run(run, recording,
                              dict(session_id=session_id, created_at=now, setup_room=body.get("setup_room", ""), schema_version=2),
                              dict(config_id=config_sha, config_json=config_json, config_sha256=config_sha, dsp_version="host-v1"))
            except Exception:
                recorder.close()
                (RECORDINGS / f"{run_id}.jsonl").unlink()
                raise
            hub.run_id, hub.run_recorder = run_id, recorder
            hub.run_frames = hub.run_csi = 0
            hub.run_gap_start = hub.sequence_gaps
            hub.run_record_error = None
            hub.run_fsm_highest = FsmState.NORMAL
            return web.json_response({"run_id": run_id, "status": "PREPARING"}, status=201)
        except (ValueError, AttributeError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def runs_action(request):
        run_id, action = request.match_info["run_id"], request.match_info["action"]
        row = db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if row is None:
            raise web.HTTPNotFound()
        if action not in ("start", "stop", "cancel", "label"):
            raise web.HTTPNotFound()
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        expected = {"start": "PREPARING", "stop": "RUNNING", "cancel": None, "label": "AWAITING_LABEL"}[action]
        if (row["status"] != expected if expected else row["status"] not in ("PREPARING", "RUNNING")):
            raise web.HTTPConflict(text="invalid run transition")
        if action in ("start", "stop", "cancel") and hub.run_id != run_id:
            raise web.HTTPConflict(text="recording is not active")
        now = datetime.now(timezone.utc).isoformat()
        if action == "start":
            db.transition(run_id, "PREPARING", "RUNNING", started_at=now)
            hub.run_fsm_highest = hub.fsm.state if hub.fsm.state in FSM_RANK else FsmState.NORMAL
        elif action in ("stop", "cancel"):
            if action == "cancel" and (not isinstance(body.get("reason"), str) or not body["reason"].strip()):
                raise web.HTTPBadRequest(text="reason required")
            hub.close_run_record()
            path = RECORDINGS / f"{run_id}.jsonl"
            data = path.read_bytes()
            error = hub.run_record_error
            db.execute("UPDATE recordings SET status=?,frame_count=?,byte_count=?,sha256=? WHERE recording_id=?",
                       ("INCOMPLETE" if error else "COMPLETE" if action == "stop" else "CANCELLED",
                        hub.run_frames, len(data), hashlib.sha256(data).hexdigest(), row["recording_id"]))
            db.transition(run_id, row["status"], "AWAITING_LABEL" if action == "stop" else "CANCELLED",
                          ended_at=now, csi_frame_count=hub.run_csi, wire_frame_count=hub.run_frames,
                          gap_count=max(0, hub.sequence_gaps - hub.run_gap_start), fsm_highest=hub.run_fsm_highest.value,
                          quality_flags=json.dumps([error] if error else []))
            if action == "cancel":
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, run_id, None, "run_cancelled", "server", None, None, None, None,
                            time.time_ns(), time.monotonic_ns(), time.time_ns(), json.dumps({"reason": body["reason"]})))
            if error:
                raise web.HTTPInternalServerError(text=error)
        else:
            activity, verdict, note = body.get("actual_activity"), body.get("verdict"), body.get("note", "")
            if any(not isinstance(v, str) or not v.strip() for v in (activity, verdict)) or not isinstance(note, str) or len(note) > 2000:
                raise web.HTTPBadRequest(text="actual_activity, verdict and bounded note required")
            db.execute("INSERT INTO labels VALUES (?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, run_id, 1, activity, verdict, now, note))
            db.transition(run_id, "AWAITING_LABEL", "COMPLETED")
        return web.json_response({"run_id": run_id, "status": db.one("SELECT status FROM runs WHERE run_id=?", (run_id,))["status"]})

    async def events_create(request):
        body = await request.json()
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or len(events) > 100:
            raise web.HTTPBadRequest(text="events array (max 100) required")
        run_ids = {event.get("run_id") for event in events if isinstance(event, dict) and isinstance(event.get("run_id"), str)}
        valid_runs = {row["run_id"] for row in db.all(
            f"SELECT run_id FROM runs WHERE run_id IN ({','.join('?' for _ in run_ids)})", tuple(run_ids))} if run_ids else set()
        rows = []
        for event in events:
            if (not isinstance(event, dict) or not isinstance(event.get("event_type"), str) or
                    not event["event_type"] or not isinstance(event.get("run_id"), str) or
                    event["run_id"] not in valid_runs):
                raise web.HTTPBadRequest(text="valid run_id and event_type required")
            for key in ("action_id", "origin", "client_instance_id", "client_mono_ns"):
                if key in event and event[key] is not None and not isinstance(event[key], str):
                    raise web.HTTPBadRequest(text=f"invalid {key}")
            for key in ("client_seq", "client_wall_ms"):
                if key in event and event[key] is not None and type(event[key]) is not int:
                    raise web.HTTPBadRequest(text=f"invalid {key}")
            if "event_id" in event and (not isinstance(event["event_id"], str) or not NAME.fullmatch(event["event_id"])):
                raise web.HTTPBadRequest(text="invalid event_id")
            try:
                payload = json.dumps(event.get("payload", {}), separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise web.HTTPBadRequest(text="invalid payload") from exc
            wall_ns = time.time_ns()
            rows.append((event.get("event_id") or uuid.uuid4().hex, event["run_id"], event.get("action_id"),
                         event["event_type"], event.get("origin", "ui"), event.get("client_instance_id"),
                         event.get("client_seq"), event.get("client_wall_ms"), event.get("client_mono_ns"),
                         wall_ns, time.monotonic_ns(), wall_ns, payload))
        try:
            db.insert_events(rows)
        except sqlite3.IntegrityError as exc:
            raise web.HTTPConflict(text="duplicate event_id") from exc
        return web.json_response({"accepted": len(events)})

    async def runs_list(request):
        try:
            limit = int(request.query.get("limit", 50))
            offset = int(request.query.get("offset", 0))
            if not 1 <= limit <= 100 or offset < 0:
                raise ValueError("invalid pagination")
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"runs": db.all("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?", (limit, offset)),
                                  "total": db.one("SELECT count(*) AS n FROM runs")["n"]})

    async def runs_detail(request):
        detail = db.detail(request.match_info["run_id"])
        if detail is None:
            raise web.HTTPNotFound()
        return web.json_response(detail)

    async def state(_):
        return web.json_response(hub.snapshot())

    async def ports(_):
        comports = list_ports.comports()
        sorted_ports = sorted(
            comports,
            key=lambda p: (0 if "ttyACM1" in p.device else 1 if "ttyACM" in p.device else 2, p.device)
        )
        local = [
            {"device": p.device, "description": p.description, "serial_number": p.serial_number}
            for p in sorted_ports if "ttyACM" in p.device or "ttyUSB" in p.device or "by-id" in p.device
        ] or [{"device": p.device, "description": p.description, "serial_number": getattr(p, "serial_number", None)} for p in comports]

        remote = []
        def probe_remote(host: str, port: int) -> bool:
            try:
                with socket.create_connection((host, port), timeout=0.15):
                    return True
            except OSError:
                return False

        if await asyncio.to_thread(probe_remote, "192.168.1.104", 9000):
            remote.append({
                "device": "socket://192.168.1.104:9000",
                "description": "Laptop ESP32-S3 RX (192.168.1.104:9000)",
                "serial_number": "remote-192.168.1.104"
            })

        return web.json_response({
            "ports": remote + local
        })

    async def audit(_):
        return web.json_response(list(hub.audit))

    async def export(request):
        path = DATA / ("audit.jsonl" if request.path == "/api/audit/export" else "telemetry.jsonl")
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    async def mutate(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
            path = request.path
            if path == "/api/connect":
                port = body.get("port")
                src, rx = body.get("source_mac"), body.get("rx_mac")
                is_socket = isinstance(port, str) and bool(re.fullmatch(r"socket://[A-Za-z0-9_.-]+:\d+", port))
                is_dev = isinstance(port, str) and bool(re.fullmatch(r"/dev/serial/by-id/[A-Za-z0-9_.:-]+", port) or re.fullmatch(r"/dev/tty(?:ACM|USB)\d+", port))
                if (hub.port or hub.replayer or not (is_socket or is_dev)
                        or body.get("baud") != 921600 or not isinstance(src, str) or not MAC.fullmatch(src)
                        or not isinstance(rx, str) or not MAC.fullmatch(rx) or src == rx
                        or body.get("identities_verified") is not True):
                    raise ValueError("verified distinct MACs and serial device required")
                if is_socket:
                    hub.lock = None
                    hub.port = serial.serial_for_url(port, timeout=0.1)
                else:
                    serial_name = Path(port).resolve().name
                    hub.lock = open(f"/run/lock/csi-{serial_name}.lock", "a+")
                    try:
                        fcntl.flock(hub.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        hub.port = serial.Serial(port, 921600, timeout=0.1, exclusive=True, rtscts=False, dsrdtr=False)
                    except (OSError, serial.SerialException):
                        hub.lock.close()
                        hub.lock = None
                        raise
                hub.port_name, hub.source, hub.receiver, hub.verified = port, src, rx, True
                hub.mode = "LIVE"
                hub.parser = Parser()
                hub.trace.clear(); hub.arrivals.clear(); hub.events.clear()
                hub.stats = {}; hub.sequence_gaps = hub.rejected = hub.host_record_drops = 0
                hub.last_seq = hub.last_capture_us = hub.last_sample_ns = None
                hub.dsp = None
                hub.detector = FallDetector(hub.thresholds, hub.classifier)
                hub.fsm = hub.detector.fsm
                hub.detector.reset()
                hub.last_motion = None
                hub.burst_ratio = hub.motion_slope = hub.spectral_dist = None
                hub.room_presence = "UNKNOWN"
                hub.empty_capture = None
                hub.reader = asyncio.create_task(hub.read_loop())
            elif path == "/api/disconnect":
                await hub.disconnect()
            elif path == "/api/record/start":
                name, notes = body.get("name"), body.get("notes", "")
                if not hub.port or hub.recorder or not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(notes, str) or len(notes) > 2000:
                    raise ValueError("open live connection, safe unused name and bounded notes required")
                RECORDINGS.mkdir(parents=True, exist_ok=True)
                hub.recorder = (RECORDINGS / f"{name}.jsonl").open("x", encoding="utf-8")
                hub.record_name = name
                hub.record_counts = {"frames": 0, "markers": 0}
                hub.recording_status = {"state": "recording", "name": name}
                hub.write_record({"type": "meta", "version": 1, "source_mac": hub.source, "rx_mac": hub.receiver,
                                  "identities_verified": hub.verified, "notes": notes, "wall_ns": time.time_ns(), "mono_ns": time.monotonic_ns()})
                if hub.recorder is None:
                    hub.log_audit("record_start", body, "failed", hub.recording_status["error"])
                    return web.json_response({"ok": False, "error": hub.recording_status["error"], "state": hub.snapshot()}, status=500)
            elif path == "/api/record/stop":
                hub.stop_record()
                if hub.recording_status["state"] == "incomplete":
                    hub.log_audit("record_stop", body, "failed", hub.recording_status["error"])
                    return web.json_response({"ok": False, "error": hub.recording_status["error"], "state": hub.snapshot()}, status=500)
            elif path == "/api/marker":
                kind, note = body.get("kind"), body.get("note", "")
                if kind not in ("motion_start", "motion_stop", "note") or not isinstance(note, str) or len(note) > 500 or not hub.recorder:
                    raise ValueError("active recording and valid marker required")
                marker = {"type": "marker", "kind": kind, "note": note, "wall_ns": time.time_ns(),
                          "mono_ns": time.monotonic_ns(), "capture_us": hub.last_capture_us}
                hub.events.append(marker)
                hub.write_record(marker)
                if hub.recorder is None:
                    hub.log_audit("marker", body, "failed", hub.recording_status["error"])
                    return web.json_response({"ok": False, "error": hub.recording_status["error"], "state": hub.snapshot()}, status=500)
            elif path == "/api/replay/start":
                name = body.get("name")
                if hub.port or hub.replayer or not isinstance(name, str) or not NAME.fullmatch(name):
                    raise ValueError("disconnect and supply safe recording name")
                path_file = RECORDINGS / f"{name}.jsonl"
                with path_file.open(encoding="utf-8") as f:
                    meta = json.loads(f.readline())
                if meta.get("type") != "meta" or meta.get("version") != 1:
                    raise ValueError("invalid recording")
                if not isinstance(meta.get("source_mac"), str) or not MAC.fullmatch(meta["source_mac"]) or not isinstance(meta.get("rx_mac"), str) or not MAC.fullmatch(meta["rx_mac"]):
                    raise ValueError("invalid recording identities")
                hub.mode, hub.source, hub.receiver = "REPLAY", meta["source_mac"], meta["rx_mac"]
                hub.verified = False
                hub.trace.clear(); hub.events.clear(); hub.arrivals.clear()
                hub.last_seq = hub.last_capture_us = hub.last_sample_ns = None
                hub.sequence_gaps = 0
                hub.parser = Parser()
                hub.replayer = asyncio.create_task(replay(hub, path_file))
                hub.dsp = None
                hub.burst_ratio = hub.motion_slope = hub.spectral_dist = None
                hub.room_presence = "UNKNOWN"
                hub.empty_capture = None
            elif path == "/api/replay/stop":
                if hub.replayer:
                    hub.replayer.cancel()
                    try:
                        await hub.replayer
                    except asyncio.CancelledError:
                        pass
                    hub.replayer = None
                hub.mode = "LIVE"
                hub.trace.clear(); hub.arrivals.clear(); hub.stats = {}
                hub.last_seq = hub.last_capture_us = hub.last_sample_ns = None
                hub.source = hub.receiver = None
            elif path == "/api/command":
                action, ttl = body.get("action"), body.get("ttl_ms")
                if body.get("source") != "manual_test":
                    raise ValueError("manual_test source required")
                hub.send_command(action, ttl)
            elif path == "/api/alert/ack":
                hub.detector.ack_alert()
                hub.burst_ratio = None
                if hub.dsp is not None:
                    hub.dsp.burst_ratio = None
                    hub.dsp.energy.clear()
            elif path == "/api/calibrate/start":
                step = body.get("step", 1)
                if type(step) is not int or step not in (1, 2):
                    raise ValueError("calibration step must be 1 or 2")
                if step == 1:
                    hub.calib_still_samples.clear()
                    hub.calib_walk_samples.clear()
                else:
                    hub.dsp, _ = hub.replay_still()
                hub.calibrating_step = step
            elif path == "/api/empty_room/capture":
                if not ((hub.mode == "LIVE" and hub.port and hub.port.is_open) or
                        (hub.mode == "REPLAY" and hub.source and hub.receiver)):
                    raise ValueError("active CSI stream required to capture empty room")
                hub.empty_capture = []
                hub.log_audit("empty_room_capture", body, "started")
            elif path == "/api/calibrate/stop":
                custom = body.get("thresholds")
                if custom is not None:
                    allowed = {"still", "move", "spike", "candidate_spike", "burst", "empty"}
                    if not isinstance(custom, dict) or not {"still", "move", "spike"}.issubset(custom) or not set(custom).issubset(allowed):
                        raise ValueError("still, move, spike required; optional candidate_spike, burst, empty")
                    values = {k: custom[k] for k in custom if custom[k] is not None}
                    if any(type(v) not in (int, float) or (k not in ("empty",) and v > 1) for k, v in values.items()):
                        raise ValueError("thresholds must use raw motion units, not 0..100 score")
                    if "candidate_spike" not in values:
                        cand = hub.thresholds.candidate_spike if (hub.thresholds and hub.thresholds.candidate_spike is not None) else 0.160
                        if cand <= values["move"] or cand > values["spike"]:
                            cand = (values["move"] + values["spike"]) / 2
                        values["candidate_spike"] = cand
                    thresholds = Thresholds(**values)
                else:
                    if len(hub.calib_still_samples) < 5 or len(hub.calib_walk_samples) < 5:
                        raise ValueError("need at least five still and five walk motion samples")
                    dsp, still_motions = hub.replay_still()
                    thresholds = Calibrator.from_motion(still_motions, hub.calib_walk_samples, previous=hub.thresholds)
                    hub.dsp = dsp
                hub.thresholds = thresholds
                hub.detector.thresholds = thresholds
                hub.detector.fsm.thresholds = thresholds
                hub.detector.reset()
                hub.fsm = hub.detector.fsm
                if hub.dsp is not None:
                    hub.dsp.energy.clear()
                    hub.dsp.burst_ratio = None
                hub.burst_ratio = None
                hub.calibrating_step = None
                if custom is not None:
                    hub.log_audit("thresholds_update", body, "success")
            else:
                raise web.HTTPNotFound()
            audit_names = {"/api/connect": "connect", "/api/disconnect": "disconnect",
                           "/api/record/start": "record_start", "/api/record/stop": "record_stop",
                           "/api/alert/ack": "alert_ack", "/api/marker": "marker",
                           "/api/calibrate/start": "calibrate_start", "/api/calibrate/stop": "calibrate_stop"}
            if path in audit_names:
                hub.log_audit(audit_names[path], body, "success")
            return web.json_response({"ok": True, "state": hub.snapshot()})
        except (ValueError, KeyError, OSError, serial.SerialException, json.JSONDecodeError) as exc:
            name = {"/api/record/start": "record_start", "/api/record/stop": "record_stop",
                    "/api/calibrate/start": "calibrate_start", "/api/calibrate/stop": "calibrate_stop"}.get(request.path, request.path.removeprefix("/api/").replace("/", "_"))
            try:
                hub.log_audit(name, body if isinstance(body, dict) else {}, "failed", str(exc))
            except OSError:
                pass
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def ws(request):
        socket = web.WebSocketResponse(heartbeat=15)
        await socket.prepare(request)
        try:
            while not socket.closed:
                await socket.send_json({"type": "state", "state": hub.snapshot()})
                await asyncio.sleep(0.1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return socket

    async def index(_):
        return web.FileResponse(ROOT / "ui" / "index.html")

    async def cleanup(_):
        await hub.disconnect()
        if hub.replayer:
            hub.replayer.cancel()
            try:
                await hub.replayer
            except asyncio.CancelledError:
                pass

    app.on_cleanup.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/telemetry/events", events_create)
    app.router.add_post("/api/runs", runs_create)
    app.router.add_get("/api/runs", runs_list)
    app.router.add_get("/api/runs/{run_id}", runs_detail)
    app.router.add_post("/api/runs/{run_id}/{action}", runs_action)
    app.router.add_get("/api/ports", ports)
    app.router.add_get("/api/audit", audit)
    app.router.add_get("/api/audit/export", export)
    app.router.add_get("/api/telemetry/export", export)
    app.router.add_get("/ws", ws)
    for path in ("connect", "disconnect", "record/start", "record/stop", "marker", "replay/start", "replay/stop", "command", "alert/ack", "calibrate/start", "calibrate/stop", "empty_room/capture"):
        app.router.add_post("/api/"+path, mutate)
    app.router.add_static("/assets", ROOT / "ui", show_index=False)
    return app


async def replay(hub: Hub, file: Path | str) -> None:
    previous = None
    try:
        with Path(file).open(encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                if entry["type"] == "frame":
                    if previous is not None:
                        await asyncio.sleep(max(0, (entry["mono_ns"]-previous)/1e9))
                    previous = entry["mono_ns"]
                    hub.ingest(base64.b64decode(entry["raw"], validate=True), entry["wall_ns"], entry["mono_ns"], replay=True)
                elif entry["type"] == "marker":
                    hub.events.append(entry)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        hub.events.append({"type": "replay_error", "message": str(exc)})
    finally:
        hub.replayer = None
        hub.last_sample_ns = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--local-only", action="store_true", help="Restrict access to localhost only")
    parser.add_argument("--allow-remote", action="store_true", default=False, help="Allow remote connections")
    args = parser.parse_args()
    allow_remote = (not args.local_only and args.host in ("0.0.0.0", "::")) or args.allow_remote
    web.run_app(make_app(allow_remote=allow_remote), host=args.host, port=args.port)
