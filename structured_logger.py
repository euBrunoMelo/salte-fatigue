"""Segmented immutable persistence for FATIGUE runtime records."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, TextIO

import numpy as np

from atomic_persistence import (
    atomic_write_json,
    ensure_durable_directory,
    fsync_directory,
    publish_directory,
    validate_persistence_id,
)

logger = logging.getLogger("SALTE.host")

SCHEMA_VERSION = 1
DEFAULT_MAX_SEGMENT_AGE_S = 300.0
DEFAULT_MAX_SEGMENT_BYTES = 64 * 1024 * 1024
_RESERVED_EVENT_FIELDS = frozenset(
    {"schema_version", "record_id", "event_id", "run_id", "ts_utc", "kind", "data"}
)


class _CancelableTimer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _encode_jsonl(payload: dict[str, Any]) -> str:
    return json.dumps(_jsonable(payload), ensure_ascii=False, separators=(",", ":")) + "\n"


def _default_id() -> str:
    return uuid.uuid4().hex


class _ActiveSegment:
    def __init__(
        self, segments_dir: Path, run_id: str, sequence: int, opened_at_s: float
    ) -> None:
        self.run_id = run_id
        self.sequence = sequence
        self.opened_at_s = opened_at_s
        self.byte_count = 0
        self.frame_count = 0
        self.assessment_count = 0
        self.first_frame_idx: Optional[int] = None
        self.last_frame_idx: Optional[int] = None
        self.partial_path = segments_dir / f"{sequence:06d}.partial"
        self.final_path = segments_dir / f"{sequence:06d}"
        self.partial_path.mkdir()
        fsync_directory(segments_dir)
        self._frames = self._open_sink("frames.jsonl")
        self._assessments = self._open_sink("assessments.jsonl")

    def _open_sink(self, name: str) -> TextIO:
        return (self.partial_path / name).open("x", encoding="utf-8", buffering=1)

    def append(
        self, frame_idx: int, frame_line: Optional[str], assessment_line: Optional[str]
    ) -> None:
        if frame_line is not None:
            self._frames.write(frame_line)
            self.byte_count += len(frame_line.encode("utf-8"))
            self.frame_count += 1
        if assessment_line is not None:
            self._assessments.write(assessment_line)
            self.byte_count += len(assessment_line.encode("utf-8"))
            self.assessment_count += 1
        self.first_frame_idx = frame_idx if self.first_frame_idx is None else self.first_frame_idx
        self.last_frame_idx = frame_idx

    def _flush_and_close(self, sink: TextIO) -> None:
        try:
            sink.flush()
            os.fsync(sink.fileno())
        finally:
            sink.close()

    def _close_sinks(self) -> None:
        first_error: Optional[Exception] = None
        for sink in (self._frames, self._assessments):
            try:
                self._flush_and_close(sink)
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def publish(self, closed_at: str) -> Optional[Path]:
        self._close_sinks()
        if self.byte_count == 0:
            return None
        atomic_write_json(self.partial_path / "manifest.json", self._manifest(closed_at))
        publish_directory(self.partial_path, self.final_path)
        return self.final_path

    def _manifest(self, closed_at: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "status": "completed",
            "closed_at": closed_at,
            "bytes": self.byte_count,
            "frame_count": self.frame_count,
            "assessment_count": self.assessment_count,
            "first_frame_idx": self.first_frame_idx,
            "last_frame_idx": self.last_frame_idx,
        }


class StructuredLogger:
    """Persist one run as immutable metadata, events, and JSONL segments.

    Example: ``StructuredLogger(Path("/app/logs")).log_run_start({})``.
    """

    def __init__(
        self,
        logs_dir: Optional[Path],
        log_frame_every: int = 1,
        log_assessment_every: int = 1,
        max_segment_age_s: float = DEFAULT_MAX_SEGMENT_AGE_S,
        max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
        *,
        id_factory: Callable[[], str] = _default_id,
        clock: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timer_factory: Callable[
            [float, Callable[[], None]], _CancelableTimer
        ] = threading.Timer,
    ) -> None:
        self.log_frame_every = self._positive_int(log_frame_every, "log_frame_every")
        self.log_assessment_every = self._positive_int(log_assessment_every, "log_assessment_every")
        self.max_segment_age_s = self._positive_float(max_segment_age_s, "max_segment_age_s")
        self.max_segment_bytes = self._positive_int(max_segment_bytes, "max_segment_bytes")
        self._id_factory, self._clock, self._utc_clock = id_factory, clock, utc_clock
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._rotation_timer: Optional[_CancelableTimer] = None
        self.run_id = validate_persistence_id(self._id_factory())
        self.logs_dir = Path(logs_dir) if logs_dir is not None else None
        self.stale_partials: tuple[Path, ...] = ()
        self._active: Optional[_ActiveSegment] = None
        self._next_sequence = 1
        self._run_dir: Optional[Path] = None
        self._segments_dir: Optional[Path] = None
        if self.logs_dir is not None:
            self._initialize_layout()

    @staticmethod
    def _positive_int(value: int, name: str) -> int:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError(f"invalid {name}={value!r}; expected integer greater than zero")
        return parsed

    @staticmethod
    def _positive_float(value: float, name: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0.0:
            raise ValueError(f"invalid {name}={value!r}; expected finite number greater than zero")
        return parsed

    def _utc_now(self) -> datetime:
        now = self._utc_clock()
        if now.tzinfo is None:
            raise ValueError(f"invalid UTC clock value {now!r}; expected timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def _initialize_layout(self) -> None:
        assert self.logs_dir is not None
        ensure_durable_directory(self.logs_dir)
        self.stale_partials = tuple(sorted(
            path for path in self.logs_dir.rglob("*.partial")
        ))
        if self.stale_partials:
            logger.warning("StructuredLogger encontrou partials antigos: %s", self.stale_partials)
        run_date = self._utc_now()
        day_dir = self.logs_dir / "runs" / run_date.strftime("%Y/%m/%d")
        ensure_durable_directory(day_dir)
        self._run_dir = day_dir / self.run_id
        self._run_dir.mkdir()
        fsync_directory(day_dir)
        self._segments_dir = self._run_dir / "segments"
        self._segments_dir.mkdir()
        fsync_directory(self._run_dir)

    def _timestamp(self) -> str:
        return self._utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _run_record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "ts_utc": self._timestamp(),
            "kind": kind,
            "data": _jsonable(payload),
        }

    def _write_atomic(self, path: Path, record: dict[str, Any]) -> Optional[Path]:
        try:
            atomic_write_json(path, record)
            return path
        except Exception as exc:
            logger.warning("StructuredLogger write falhou em %s: %s", path, exc)
            return None

    def log_run_start(self, payload: dict[str, Any]) -> Optional[Path]:
        """Atomically publish immutable run-start metadata."""
        if self._run_dir is None:
            return None
        return self._write_atomic(self._run_dir / "run-start.json", self._run_record("run_start", payload))

    def _sampled_line(self, payload: dict[str, Any], every: int) -> Optional[str]:
        frame_idx = int(payload.get("frame_idx", 0))
        return _encode_jsonl(payload) if frame_idx % every == 0 else None

    def _should_rotate(self, now_s: float, incoming_bytes: int) -> bool:
        if self._active is None or self._active.byte_count == 0:
            return False
        age_expired = now_s - self._active.opened_at_s >= self.max_segment_age_s
        size_expired = self._active.byte_count + incoming_bytes > self.max_segment_bytes
        return age_expired or size_expired

    def _open_segment(self, now_s: float) -> _ActiveSegment:
        assert self._segments_dir is not None
        segment = _ActiveSegment(
            self._segments_dir, self.run_id, self._next_sequence, now_s
        )
        self._next_sequence += 1
        return segment

    def _schedule_rotation(self, segment: _ActiveSegment) -> None:
        def publish_if_current() -> None:
            with self._lock:
                if self._active is segment:
                    self._publish_active_locked()

        timer = self._timer_factory(self.max_segment_age_s, publish_if_current)
        timer.daemon = True
        self._rotation_timer = timer
        timer.start()

    def _publish_active_locked(self) -> None:
        timer, self._rotation_timer = self._rotation_timer, None
        if timer is not None:
            timer.cancel()
        segment, self._active = self._active, None
        if segment is None:
            return
        try:
            segment.publish(self._timestamp())
        except Exception as exc:
            logger.warning("StructuredLogger publish falhou em %s: %s", segment.partial_path, exc)

    def _publish_active(self) -> None:
        with self._lock:
            self._publish_active_locked()

    def log_frame_and_assessment(
        self, frame: dict[str, Any], assessment: dict[str, Any]
    ) -> None:
        """Sample and append one frame/assessment pair without rotating between them."""
        if self._segments_dir is None:
            return
        frame_idx = int(frame.get("frame_idx", 0))
        if frame_idx != int(assessment.get("frame_idx", 0)):
            raise ValueError(
                "frame and assessment have different frame_idx values: "
                f"{frame_idx!r} != {assessment.get('frame_idx')!r}"
            )
        frame_line = self._sampled_line(
            self._telemetry_record("frame", frame), self.log_frame_every
        )
        assessment_line = self._sampled_line(
            self._telemetry_record("assessment", assessment), self.log_assessment_every
        )
        if frame_line is None and assessment_line is None:
            return
        now_s = self._clock()
        incoming_bytes = len((frame_line or "").encode()) + len((assessment_line or "").encode())
        if incoming_bytes > self.max_segment_bytes:
            logger.warning(
                "Registro descartado: %d bytes excedem max_segment_bytes=%d",
                incoming_bytes,
                self.max_segment_bytes,
            )
            return
        with self._lock:
            if self._should_rotate(now_s, incoming_bytes):
                self._publish_active_locked()
            try:
                if self._active is None:
                    self._active = self._open_segment(now_s)
                    self._schedule_rotation(self._active)
                self._active.append(frame_idx, frame_line, assessment_line)
            except Exception as exc:
                logger.warning("StructuredLogger append falhou: %s", exc)

    def _telemetry_record(
        self, record_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "record_type": record_type,
            **_jsonable(payload),
        }

    def new_event_id(self) -> str:
        """Return one path-safe identifier for a logical runtime event."""
        return validate_persistence_id(self._id_factory())

    def log_event(
        self,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        event_id: Optional[str] = None,
    ) -> Optional[Path]:
        """Atomically publish one immutable event JSON document."""
        event_data = payload or {}
        conflicts = _RESERVED_EVENT_FIELDS.intersection(event_data)
        if conflicts:
            raise ValueError(f"event data contains reserved fields: {sorted(conflicts)}")
        if self.logs_dir is None:
            return None
        record_id = validate_persistence_id(self._id_factory())
        logical_event_id = validate_persistence_id(event_id) if event_id is not None else record_id
        now = self._utc_now()
        path = self.logs_dir / "events" / now.strftime("%Y/%m/%d") / f"{record_id}.json"
        record = {
            "schema_version": SCHEMA_VERSION, "record_id": record_id,
            "event_id": logical_event_id, "run_id": self.run_id,
            "ts_utc": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "kind": kind, "data": _jsonable(event_data),
        }
        return self._write_atomic(path, record)

    def log_run_end(self, payload: dict[str, Any]) -> Optional[Path]:
        """Publish pending data, then atomically publish immutable run-end metadata."""
        self._publish_active()
        if self._run_dir is None:
            return None
        return self._write_atomic(self._run_dir / "run-end.json", self._run_record("run_end", payload))

    def close(self) -> None:
        """Publish a non-empty active segment and tolerate repeated calls."""
        self._publish_active()
