"""Inference-facing logger: RAM admission only; durability belongs to the child."""
from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from atomic_persistence import validate_persistence_id
from persistence_transport import Arena, HEALTH_SIZE, ProcessChannel, bounded_jsonable, make_lanes
from structured_logger import DEFAULT_MAX_SEGMENT_AGE_S, DEFAULT_MAX_SEGMENT_BYTES, _RESERVED_EVENT_FIELDS, _encode_jsonl

MIB = 1024 * 1024


class AsyncStructuredLogger:
    def __init__(self, logs_dir: Path, log_frame_every=1, log_assessment_every=1,
                 max_segment_age_s=DEFAULT_MAX_SEGMENT_AGE_S,
                 max_segment_bytes=DEFAULT_MAX_SEGMENT_BYTES, *,
                 budget_bytes=32 * MIB, run_id=None, fault=None):
        if (budget_bytes < MIB or not math.isfinite(max_segment_age_s)
                or min(log_frame_every, log_assessment_every, max_segment_age_s, max_segment_bytes) <= 0):
            raise ValueError("invalid logging budget or sampling/segment limits")
        self.run_id = validate_persistence_id(run_id or uuid.uuid4().hex)
        self.logs_dir = Path(logs_dir)
        self.log_frame_every, self.log_assessment_every = log_frame_every, log_assessment_every
        self.started_utc = self.timestamp()
        # Recorder control arena and all frame indices are paid from the record budget.
        self.control_budget = max(256 * 1024, budget_bytes // 64)
        self.arena = Arena(budget_bytes - self.control_budget)
        available = self.arena.size - 2 * HEALTH_SIZE - 256 * 1024  # all bounded Python/JSON/hash scratch
        lanes = make_lanes(self.arena, [
            ("events", available // 16, 512, 4096),
            ("console", available // 32, 512, 2048),
            ("telemetry", available * 29 // 32, 16384, 8192),
        ])
        self.channel = ProcessChannel("logger", self.arena, lanes, {
            "logs_dir": str(self.logs_dir), "run_id": self.run_id,
            "started_utc": self.started_utc, "max_segment_age_s": max_segment_age_s,
            "max_segment_bytes": max_segment_bytes, "fault": fault,
        })
        self.max_segment_bytes = max_segment_bytes

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def new_event_id(self):
        return uuid.uuid4().hex

    def _record(self, kind, data, event_id=None):
        record_id = uuid.uuid4().hex
        return {"schema_version": 1, "run_id": self.run_id, "record_id": record_id,
                "event_id": event_id or record_id, "ts_utc": self.timestamp(),
                "kind": kind, "data": data}

    def log_event(self, kind, payload=None, event_id=None):
        conflicts = _RESERVED_EVENT_FIELDS.intersection(payload or {})
        if conflicts:
            raise ValueError(f"event data contains reserved fields: {sorted(conflicts)}")
        if event_id is not None:
            validate_persistence_id(event_id)
        record = self._record(kind, payload or {}, event_id)
        return self.channel.submit("events", {"operation": "event", "record": record},
                                   record["record_id"], (payload or {}).get("source_timestamp_s"))

    def log_run_start(self, payload):
        record = self._record("run_start", payload)
        return self.channel.submit("events", {"operation": "run-start", "record": record}, record["record_id"])

    try_submit = log_event

    def log_run_end(self, payload):
        # Worker publishes end only after every previously accepted telemetry item.
        video = payload.get("recorder_snapshot_at_end_admission", {})
        video_pending = any(queue["count"] for queue in video.get("queues", {}).values())
        record = self._record("run_end", {**payload, "persistence_losses": dict(self.channel.dropped),
                                          "history_complete": not (self.channel.dropped or self.channel.unknown_outcomes
                                                                   or video.get("dropped") or video.get("unknown_outcomes") or video_pending)})
        return self.channel.submit("events", {"operation": "run-end", "record": record,
                                              "barrier": self.channel.sequence}, record["record_id"])

    def log_frame_and_assessment(self, frame, assessment):
        index = int(frame.get("frame_idx", 0))
        if index != int(assessment.get("frame_idx", 0)):
            raise ValueError("frame and assessment have different frame_idx")
        frame = {"schema_version": 1, "run_id": self.run_id, "record_type": "frame", **frame} if index % self.log_frame_every == 0 else None
        assessment = {"schema_version": 1, "run_id": self.run_id, "record_type": "assessment", **assessment} if index % self.log_assessment_every == 0 else None
        if frame is None and assessment is None:
            return None
        try:
            pair = bounded_jsonable([frame, assessment], self.channel.lanes["telemetry"].payload_size)
            frame, assessment = pair
        except ValueError:
            return self.channel.loss("telemetry", "bytes")
        if sum(len(_encode_jsonl(item).encode()) for item in (frame, assessment) if item) > self.max_segment_bytes:
            return self.channel.loss("telemetry", "segment_bytes")
        return self.channel.submit("telemetry", {"frame": frame, "assessment": assessment}, source_s=(frame or assessment).get("timestamp_s"))

    def snapshot(self):
        return self.channel.snapshot()

    def receipt_state(self, receipt):
        if not receipt.accepted:
            return "rejected"
        worker = self.snapshot()
        if worker.get("durable", {}).get(receipt.category, 0) >= receipt.sequence:
            return "persisted"
        if worker["state"] == "closed_incomplete":
            return "outcome_unknown"
        if worker.get("consumed", {}).get(receipt.category, 0) >= receipt.sequence:
            return "consumed"
        return "enqueued"

    def close(self, deadline_seconds=5.):
        result = self.channel.close(deadline_seconds)
        self.channel.dispose()
        return result


class AsyncConsoleHandler(logging.Handler):
    def __init__(self, slog):
        super().__init__()
        self.slog = slog

    def emit(self, record):
        # No synchronous fallback (including logging.Handler.handleError on stderr).
        try:
            self.slog.channel.submit("console", {
                "source_utc": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname, "name": record.name,
                "message": record.getMessage()[:1600], "historical": True,
            })
        except Exception:
            self.slog.channel.loss("console", "encoding")
