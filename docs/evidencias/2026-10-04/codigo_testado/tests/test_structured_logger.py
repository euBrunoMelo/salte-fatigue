"""Deterministic tests for segmented FATIGUE persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import numpy as np

import _bootstrap  # noqa: F401

from atomic_persistence import atomic_write_json, fsync_directory, publish_directory
from feature_extractor_rt import RTFrameFeatures
from run_host import RuntimeApplication, build_parser
from runtime_models import (
    AttentionAssessment,
    AttentionState,
    EyeClosureEvent,
    EyeClosureEventType,
    EyeFrameObservation,
    EyeQuality,
    FatigueAssessment,
    FatigueState,
    ObservationState,
    PerclosMeasurement,
    PoseAngles,
    RuntimeAssessment,
)
from structured_logger import StructuredLogger


UTC_NOW = datetime(2026, 9, 11, 12, 34, 56, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeTimer:
    def __init__(self, _delay: float, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class FakeTimerFactory:
    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, delay: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(delay, callback)
        self.timers.append(timer)
        return timer


class SequentialIds:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


class CapturingLogger:
    def __init__(self) -> None:
        self.frame: dict[str, Any] = {}
        self.assessment: dict[str, Any] = {}

    def log_frame_and_assessment(
        self, frame: dict[str, Any], assessment: dict[str, Any]
    ) -> None:
        self.frame = frame
        self.assessment = assessment


class CorrelatingLogger(CapturingLogger):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str | None]] = []
        self._next_id = 1

    def new_event_id(self) -> str:
        event_id = f"logical-{self._next_id}"
        self._next_id += 1
        return event_id

    def log_event(
        self, kind: str, payload: dict[str, Any], event_id: str | None = None
    ) -> Path | None:
        self.events.append((kind, event_id))
        return Path("event.json")


class FailingCorrelatingLogger(CorrelatingLogger):
    def log_event(
        self, kind: str, payload: dict[str, Any], event_id: str | None = None
    ) -> None:
        self.events.append((kind, event_id))
        return None


class CapturingRecorder:
    def __init__(self) -> None:
        self.event_ids: list[str | None] = []

    def on_frame(self, _frame, _timestamp, _state, event_id=None) -> None:
        self.event_ids.append(event_id)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assessment(frame_idx: int = 7) -> tuple[RTFrameFeatures, RuntimeAssessment]:
    features = RTFrameFeatures(
        1000.0, frame_idx, 0.31, 0.32, True,
        pitch=1.0, yaw=2.0, roll=3.0, pose_available=True,
    )
    observation = EyeFrameObservation(
        1.0, frame_idx, True, 0.31, 0.32, PoseAngles(1.0, 2.0, 3.0)
    )
    quality = EyeQuality(True, True, True, "ok")
    closure = EyeClosureEvent(EyeClosureEventType.NONE, False, 0.0, 2)
    perclos = PerclosMeasurement(ObservationState.READY, 0.1, 1.0, 60.0, 60.0)
    fatigue = FatigueAssessment(FatigueState.SAFE, ObservationState.READY, closure, perclos)
    attention = AttentionAssessment(AttentionState.ATTENTIVE, 0.0, 1.0, 2.0, 3.0)
    return features, RuntimeAssessment(observation, quality, fatigue, attention)


class TestAtomicPersistence(unittest.TestCase):
    def test_atomic_json_fsyncs_file_links_then_fsyncs_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "event.json"
            operations: list[str] = []
            real_fsync = os.fsync
            real_link = os.link

            def observed_fsync(fd: int) -> None:
                operations.append("dir_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync")
                real_fsync(fd)

            def observed_link(source: str | Path, target: str | Path) -> None:
                self.assertEqual(Path(source).parent, Path(target).parent)
                self.assertFalse(Path(target).exists())
                json.loads(Path(source).read_text(encoding="utf-8"))
                operations.append("link")
                real_link(source, target)

            with patch("atomic_persistence.os.fsync", side_effect=observed_fsync), patch(
                "atomic_persistence.os.link", side_effect=observed_link
            ):
                atomic_write_json(destination, {"complete": True})

            self.assertEqual(operations, ["file_fsync", "link", "dir_fsync"])
            self.assertEqual(json.loads(destination.read_text()), {"complete": True})
            self.assertEqual(list(destination.parent.glob("*.partial")), [])

    def test_atomic_json_fsyncs_each_new_directory_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "one/two/event.json"
            synced: list[Path] = []

            def observed_fsync_directory(path: Path) -> None:
                synced.append(Path(path))
                fsync_directory(path)

            with patch(
                "atomic_persistence.fsync_directory",
                side_effect=observed_fsync_directory,
            ):
                atomic_write_json(destination, {"complete": True})

            self.assertEqual(synced, [root, root / "one", root / "one/two"])

    def test_publish_directory_does_not_replace_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial = root / "segment.partial"
            final = root / "segment"
            partial.mkdir()
            final.mkdir()
            (partial / "payload").write_text("new", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                publish_directory(partial, final)

            self.assertTrue(partial.is_dir())
            self.assertEqual(list(final.iterdir()), [])


class TestStructuredLogger(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _logger(self, **overrides: Any) -> StructuredLogger:
        options: dict[str, Any] = {
            "id_factory": SequentialIds("run-001", "event-001", "event-002"),
            "clock": FakeClock(),
            "utc_clock": lambda: UTC_NOW,
        }
        options.update(overrides)
        return StructuredLogger(self.root, **options)

    def test_layout_and_record_envelopes(self) -> None:
        slog = self._logger()
        self.assertEqual(slog.run_id, "run-001")
        slog.log_run_start({"source": "camera"})
        slog.log_frame_and_assessment(
            {"frame_idx": 0, "ear_left": 0.3},
            {"frame_idx": 0, "fatigue_state": "safe"},
        )
        event_path = slog.log_event("state_change", {"current": "safe"})
        slog.log_run_end({"frames_seen": 1})
        slog.close()

        run_dir = self.root / "runs/2026/09/11/run-001"
        self.assertEqual(json.loads((run_dir / "run-start.json").read_text())["data"], {"source": "camera"})
        self.assertEqual(json.loads((run_dir / "run-end.json").read_text())["data"], {"frames_seen": 1})
        segment = run_dir / "segments/000001"
        self.assertEqual(_read_jsonl(segment / "frames.jsonl")[0]["ear_left"], 0.3)
        self.assertEqual(_read_jsonl(segment / "assessments.jsonl")[0]["fatigue_state"], "safe")
        manifest = json.loads((segment / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["frame_count"], 1)
        self.assertEqual(manifest["assessment_count"], 1)
        self.assertFalse((run_dir / "segments/000001.partial").exists())
        self.assertEqual(event_path, self.root / "events/2026/09/11/event-001.json")
        event = json.loads(event_path.read_text())
        self.assertEqual(
            set(event), {"schema_version", "record_id", "event_id", "run_id", "ts_utc", "kind", "data"}
        )
        self.assertEqual(event["record_id"], event["event_id"])
        self.assertEqual(event["run_id"], "run-001")
        self.assertEqual(event["data"], {"current": "safe"})

    def test_rotation_by_age_keeps_each_pair_together(self) -> None:
        clock = FakeClock()
        slog = self._logger(clock=clock, max_segment_age_s=300.0)
        slog.log_frame_and_assessment({"frame_idx": 0}, {"frame_idx": 0})
        clock.value = 301.0
        slog.log_frame_and_assessment({"frame_idx": 1}, {"frame_idx": 1})
        slog.close()

        segments = self.root / "runs/2026/09/11/run-001/segments"
        for sequence, frame_idx in (("000001", 0), ("000002", 1)):
            self.assertEqual(_read_jsonl(segments / sequence / "frames.jsonl")[0]["frame_idx"], frame_idx)
            self.assertEqual(_read_jsonl(segments / sequence / "assessments.jsonl")[0]["frame_idx"], frame_idx)

    def test_rotation_by_size_never_splits_a_pair(self) -> None:
        slog = self._logger(max_segment_bytes=200)
        slog.log_frame_and_assessment({"frame_idx": 0}, {"frame_idx": 0})
        slog.log_frame_and_assessment({"frame_idx": 1}, {"frame_idx": 1})
        slog.close()

        segments = self.root / "runs/2026/09/11/run-001/segments"
        self.assertEqual(sorted(path.name for path in segments.iterdir()), ["000001", "000002"])
        for sequence, frame_idx in (("000001", 0), ("000002", 1)):
            self.assertEqual(_read_jsonl(segments / sequence / "frames.jsonl")[0]["frame_idx"], frame_idx)
            self.assertEqual(_read_jsonl(segments / sequence / "assessments.jsonl")[0]["frame_idx"], frame_idx)

    def test_pair_larger_than_hard_limit_is_rejected(self) -> None:
        slog = self._logger(max_segment_bytes=1)
        with self.assertLogs("SALTE.host", level="WARNING"):
            slog.log_frame_and_assessment({"frame_idx": 0}, {"frame_idx": 0})
        slog.close()

        segments = self.root / "runs/2026/09/11/run-001/segments"
        self.assertEqual(list(segments.iterdir()), [])

    def test_age_timer_publishes_without_another_frame(self) -> None:
        timers = FakeTimerFactory()
        slog = self._logger(max_segment_age_s=300.0, timer_factory=timers)
        slog.log_frame_and_assessment({"frame_idx": 0}, {"frame_idx": 0})

        self.assertTrue(timers.timers[0].started)
        timers.timers[0].fire()

        segments = self.root / "runs/2026/09/11/run-001/segments"
        self.assertTrue((segments / "000001/manifest.json").is_file())
        self.assertFalse((segments / "000001.partial").exists())

    def test_sampling_is_independent(self) -> None:
        slog = self._logger(log_frame_every=2, log_assessment_every=3)
        for frame_idx in range(7):
            slog.log_frame_and_assessment({"frame_idx": frame_idx}, {"frame_idx": frame_idx})
        slog.close()

        segment = self.root / "runs/2026/09/11/run-001/segments/000001"
        self.assertEqual([row["frame_idx"] for row in _read_jsonl(segment / "frames.jsonl")], [0, 2, 4, 6])
        self.assertEqual([row["frame_idx"] for row in _read_jsonl(segment / "assessments.jsonl")], [0, 3, 6])

    def test_final_names_are_immutable_and_empty_segment_is_not_published(self) -> None:
        slog = self._logger(id_factory=lambda: "fixed-id")
        slog.log_run_start({"first": True})
        with self.assertLogs("SALTE.host", level="WARNING"):
            slog.log_run_start({"first": False})
        first_event = slog.log_event("first")
        with self.assertLogs("SALTE.host", level="WARNING"):
            second_event = slog.log_event("second")
        slog.close()

        run_dir = self.root / "runs/2026/09/11/fixed-id"
        self.assertEqual(json.loads((run_dir / "run-start.json").read_text())["data"], {"first": True})
        self.assertEqual(json.loads(first_event.read_text())["kind"], "first")
        self.assertIsNone(second_event)
        self.assertEqual(list((run_dir / "segments").iterdir()), [])

    def test_stale_partial_is_reported_but_not_modified_or_promoted(self) -> None:
        stale = self.root / "runs/2026/09/10/old-run/segments/000007.partial"
        stale.mkdir(parents=True)
        sentinel = stale / "frames.jsonl"
        sentinel.write_text('{"frame_idx":7}\n', encoding="utf-8")
        atomic_partial = self.root / "events/2026/09/10/.event.json.abcd.partial"
        atomic_partial.parent.mkdir(parents=True)
        atomic_partial.write_text("incomplete", encoding="utf-8")

        with self.assertLogs("SALTE.host", level="WARNING"):
            slog = self._logger()
        self.assertEqual(slog.stale_partials, tuple(sorted((atomic_partial, stale))))
        slog.close()

        self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"frame_idx":7}\n')
        self.assertTrue(stale.is_dir())
        self.assertFalse(stale.with_suffix("").exists())
        self.assertEqual(atomic_partial.read_text(encoding="utf-8"), "incomplete")

    def test_event_rejects_all_reserved_data_fields(self) -> None:
        slog = self._logger()
        reserved = (
            "schema_version", "record_id", "event_id", "run_id", "ts_utc", "kind", "data"
        )
        for field in reserved:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "reserved"):
                slog.log_event("invalid", {field: "overwrite"})
        slog.close()
        self.assertEqual(list((self.root / "events").rglob("*.json")), [])

    def test_event_accepts_explicit_logical_event_id(self) -> None:
        slog = self._logger()
        event_path = slog.log_event("critical_enter", {}, event_id="critical-001")
        event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(event["event_id"], "critical-001")
        self.assertEqual(event["record_id"], "event-001")
        slog.close()

    def test_mismatched_pair_is_rejected_before_opening_segment(self) -> None:
        slog = self._logger()
        with self.assertRaisesRegex(ValueError, "different frame_idx"):
            slog.log_frame_and_assessment({"frame_idx": 1}, {"frame_idx": 2})
        slog.close()
        segments = self.root / "runs/2026/09/11/run-001/segments"
        self.assertEqual(list(segments.iterdir()), [])

    def test_disabled_logger_is_a_noop(self) -> None:
        slog = StructuredLogger(None, id_factory=lambda: "disabled-run", utc_clock=lambda: UTC_NOW)
        slog.log_run_start({})
        slog.log_frame_and_assessment({"frame_idx": 0}, {"frame_idx": 0})
        self.assertIsNone(slog.log_event("noop"))
        slog.log_run_end({})
        slog.close()
        self.assertEqual(list(self.root.iterdir()), [])


class TestRunHostLogging(unittest.TestCase):
    def test_cli_uses_log_root_and_segment_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(Path(args.logs_dir), Path(__file__).parents[1] / "logs")
        self.assertIsNone(args.danger_output_dir)
        self.assertEqual(args.log_frame_every, 1)
        self.assertEqual(args.log_assessment_every, 1)
        self.assertEqual(args.max_segment_age_s, 300.0)
        self.assertEqual(args.max_segment_bytes, 64 * 1024 * 1024)

    def test_frame_and_assessment_payloads_are_distinct(self) -> None:
        capture = CapturingLogger()
        application = RuntimeApplication.__new__(RuntimeApplication)
        application.slog = capture
        _, assessment = _assessment()

        application._log_frame(assessment)

        self.assertEqual(capture.frame["ear_left"], 0.31)
        self.assertNotIn("fatigue_state", capture.frame)
        self.assertEqual(capture.assessment["fatigue_state"], "safe")
        self.assertNotIn("ear_left", capture.assessment)
        self.assertEqual(capture.frame["frame_idx"], capture.assessment["frame_idx"])

    def test_critical_episode_correlates_state_events_and_recorder(self) -> None:
        logger = CorrelatingLogger()
        recorder = CapturingRecorder()
        application = RuntimeApplication.__new__(RuntimeApplication)
        application.slog = logger
        application.recorder = recorder
        application.args = SimpleNamespace(display=False)
        application.frames_seen = 0
        application.last_health = 10**20
        application.last_state = FatigueState.WARNING
        application.active_critical_event_id = None
        features, safe_assessment = _assessment()
        critical_fatigue = replace(safe_assessment.fatigue, state=FatigueState.CRITICAL)
        critical = replace(safe_assessment, fatigue=critical_fatigue)

        application._publish(np.zeros((48, 64, 3), dtype=np.uint8), features, critical)
        application._publish(np.zeros((48, 64, 3), dtype=np.uint8), features, safe_assessment)

        self.assertEqual(recorder.event_ids, ["logical-1", None])
        self.assertEqual(
            logger.events,
            [
                ("fatigue_state_change", "logical-1"),
                ("fatigue_state_change", "logical-1"),
            ],
        )
        self.assertIsNone(application.active_critical_event_id)

    def test_recorder_does_not_receive_unpersisted_event_id(self) -> None:
        logger = FailingCorrelatingLogger()
        recorder = CapturingRecorder()
        application = RuntimeApplication.__new__(RuntimeApplication)
        application.slog = logger
        application.recorder = recorder
        application.args = SimpleNamespace(display=False)
        application.frames_seen = 0
        application.last_health = 10**20
        application.last_state = FatigueState.WARNING
        application.active_critical_event_id = None
        features, assessment = _assessment()
        critical = replace(
            assessment,
            fatigue=replace(assessment.fatigue, state=FatigueState.CRITICAL),
        )

        application._publish(np.zeros((48, 64, 3), dtype=np.uint8), features, critical)

        self.assertEqual(recorder.event_ids, [None])
        self.assertIsNone(application.active_critical_event_id)


if __name__ == "__main__":
    unittest.main()
