"""Real-process persistence contracts, independent from camera/MediaPipe."""
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import _bootstrap  # noqa
from async_danger_recorder import AsyncDangerVideoRecorder
from async_structured_logger import AsyncStructuredLogger
from persistence_transport import BUSY, FREE, HEALTH_SIZE, HealthSocket, READY
from runtime_models import FatigueState


def until(predicate, timeout=4.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("condition timed out")


class TestAsyncLogger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.loggers = []

    def tearDown(self):
        for logger in self.loggers:
            logger.close(.5)
        self.tmp.cleanup()

    def make(self, **kwargs):
        logger = AsyncStructuredLogger(self.root, budget_bytes=1024 * 1024, max_segment_age_s=.04, **kwargs)
        self.loggers.append(logger)
        return logger

    def test_admission_is_not_a_path_or_durable_ack(self):
        logger = self.make(fault={"phase": "event_write", "delay_s": .3})
        receipt = logger.log_event("transition", {"source_timestamp_s": 100.})
        self.assertTrue(receipt.accepted)
        self.assertEqual(logger.receipt_state(receipt), "enqueued")
        until(lambda: logger.receipt_state(receipt) == "persisted")
        record = next(self.root.glob("events/**/*.json"))
        self.assertEqual(json.loads(record.read_text())["data"]["source_timestamp_s"], 100.)

    def test_full_rejects_new_and_keeps_every_accepted_id(self):
        logger = self.make(fault={"phase": "event_write", "delay_s": .5})
        lane = logger.channel.lanes["events"]
        receipts = [logger.log_event("test", {"ordinal": i}) for i in range(lane.count + 4)]
        accepted = [receipt for receipt in receipts if receipt.accepted]
        self.assertEqual(len(accepted), lane.count)
        self.assertTrue(logger.snapshot()["first_loss_utc"])
        self.assertEqual(logger.snapshot()["dropped"]["events:full"], 4)
        until(lambda: all(logger.receipt_state(receipt) == "persisted" for receipt in accepted))
        records = [json.loads(path.read_text()) for path in self.root.glob("events/**/*.json")]
        self.assertEqual({record["record_id"] for record in records if record["kind"] == "test"},
                         {receipt.record_id for receipt in accepted})
        until(lambda: any(json.loads(path.read_text())["kind"] == "persistence_loss_summary"
                          for path in self.root.glob("events/**/*.json")))

    def test_reserved_event_lane_survives_full_telemetry_and_console(self):
        logger = self.make(fault={"phase": "open", "delay_s": .5})
        for name in ("telemetry", "console"):
            lane = logger.channel.lanes[name]
            for i in range(lane.count + 3):
                if name == "telemetry":
                    logger.log_frame_and_assessment({"frame_idx": i}, {"frame_idx": i})
                else:
                    logger.channel.submit(name, {"message": "pending"})
        receipt = logger.log_event("still_admitted")
        self.assertTrue(receipt.accepted)
        self.assertIn("telemetry:full", logger.snapshot()["dropped"])
        self.assertIn("console:full", logger.snapshot()["dropped"])
        until(lambda: logger.receipt_state(receipt) == "persisted")

    def test_oversize_and_contention_are_explicit_nonblocking_rejections(self):
        logger = self.make()
        receipt = logger.log_event("large", {"message": "x" * 10000})
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason, "bytes")
        import fcntl
        other = os.open(f"/proc/self/fd/{logger.arena.fd}", os.O_RDWR)
        try:
            fcntl.flock(other, fcntl.LOCK_EX)
            started = time.perf_counter()
            receipt = logger.log_event("locked")
            self.assertLess(time.perf_counter() - started, .01)
            self.assertEqual(receipt.reason, "contention")
        finally:
            os.close(other)

    def test_worker_death_after_publication_reconciles_same_immutable_id(self):
        logger = self.make(fault={"phase": "event_ack", "exit": True})
        receipt = logger.log_event("critical", {"source_timestamp_s": 123.})
        until(lambda: logger.receipt_state(receipt) == "persisted", 6.)
        events = [json.loads(path.read_text()) for path in self.root.glob("events/**/*.json")]
        matching = [record for record in events if record["record_id"] == receipt.record_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(logger.snapshot()["recovery_attempts"], 1)

    def test_confirmed_receipts_remain_confirmed_across_worker_restart(self):
        logger = self.make()
        receipt = logger.log_event("confirmed_before_restart")
        until(lambda: logger.receipt_state(receipt) == "persisted")
        logger.channel.process.kill()
        until(lambda: logger.snapshot()["generation"] == 1 and logger.snapshot()["state"] == "ready", 6.)
        self.assertEqual(logger.receipt_state(receipt), "persisted")
        next_receipt = logger.log_event("confirmed_after_restart")
        until(lambda: logger.receipt_state(next_receipt) == "persisted")

    def test_partial_write_failure_retries_without_duplicate_telemetry(self):
        logger = self.make(fault={"phase": "write", "raise": True})
        for i in range(5):
            logger.log_frame_and_assessment({"frame_idx": i}, {"frame_idx": i})
        until(lambda: logger.snapshot().get("durable", {}).get("telemetry") == 5)
        frames = [json.loads(line) for path in self.root.glob("runs/**/frames.jsonl")
                  if not any(part.endswith(".partial") for part in path.parts)
                  for line in path.read_text().splitlines()]
        self.assertEqual([record["frame_idx"] for record in frames], list(range(5)))
        self.assertTrue(list(self.root.glob("runs/**/*.partial")))

    def test_end_barrier_waits_for_all_admitted_pairs_and_records_loss(self):
        logger = self.make(fault={"phase": "finalize", "delay_s": .2})
        for i in range(3):
            logger.log_frame_and_assessment({"frame_idx": i}, {"frame_idx": i})
        logger.log_event("too_large", {"x": "x" * 10000})
        end = logger.log_run_end({"frames_seen": 3})
        until(lambda: logger.receipt_state(end) == "persisted")
        payload = json.loads(next(self.root.glob("runs/**/run-end.json")).read_text())
        self.assertFalse(payload["data"]["history_complete"])
        frames = [line for path in self.root.glob("runs/**/frames.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual(len(frames), 3)

    def test_close_with_blocked_writer_is_bounded_and_not_durable(self):
        logger = self.make(fault={"phase": "event_write", "delay_s": 40.})
        receipt = logger.log_event("pending")
        until(lambda: logger.snapshot().get("phase") == "event_write")
        started = time.monotonic()
        snapshot = logger.close(.2)
        self.assertLess(time.monotonic() - started, .4)
        self.assertGreater(snapshot["unknown_outcomes"], 0)
        self.assertNotEqual(logger.receipt_state(receipt), "persisted")

    def test_replacement_attempts_are_bounded_to_two(self):
        logger = self.make(fault={"phase": "event_ack", "exit": True, "generation": "all"})
        receipt = logger.log_event("never_acknowledged")
        until(lambda: logger.snapshot()["recovery_attempts"] == 2 and logger.snapshot()["state"] == "dead", 9.)
        self.assertNotEqual(logger.receipt_state(receipt), "persisted")
        self.assertEqual(len([path for path in self.root.glob("events/**/*.json") if path.stem == receipt.record_id]), 1)

    def test_disk_recovery_attempts_are_bounded_without_replacing_live_worker(self):
        logger = self.make(fault={"phase": "open", "raise": True, "repeat": True})
        receipt = logger.log_event("retained")
        until(lambda: logger.snapshot().get("recovery_exhausted"), 9.)
        snapshot = logger.snapshot()
        self.assertEqual(snapshot["io_recovery_attempts"], 2)
        self.assertEqual(snapshot["recovery_attempts"], 0)
        self.assertIsNotNone(snapshot["pid"])
        self.assertEqual(logger.receipt_state(receipt), "enqueued")

    def test_sampling_size_limit_and_jsonl_manifest_fields(self):
        logger = AsyncStructuredLogger(self.root, log_frame_every=2, log_assessment_every=3,
                                       budget_bytes=1024 * 1024, max_segment_age_s=.04, max_segment_bytes=512)
        self.loggers.append(logger)
        for i in range(7):
            logger.log_frame_and_assessment({"frame_idx": i}, {"frame_idx": i})
        rejected = logger.log_frame_and_assessment({"frame_idx": 0, "x": "x" * 600}, {"frame_idx": 0})
        self.assertEqual(rejected.reason, "segment_bytes")
        logger.close(2.)
        frames = [json.loads(line)["frame_idx"] for path in sorted(self.root.glob("runs/**/frames.jsonl")) for line in path.read_text().splitlines()]
        assessments = [json.loads(line)["frame_idx"] for path in sorted(self.root.glob("runs/**/assessments.jsonl")) for line in path.read_text().splitlines()]
        self.assertEqual(frames, [0, 2, 4, 6])
        self.assertEqual(assessments, [0, 3, 6])
        for path in self.root.glob("runs/**/manifest.json"):
            record = json.loads(path.read_text())
            self.assertLessEqual(record["bytes"], 512)
            self.assertEqual(record["publication"], "finalized")
            self.assertIn("sha256", record)


class TestAsyncRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.recorders = []
        self.frame = np.zeros((24, 32, 3), np.uint8)

    def tearDown(self):
        for recorder in self.recorders:
            recorder.close(.5)
        self.tmp.cleanup()

    def make(self, **kwargs):
        options = dict(pre_roll_sec=.2, post_roll_sec=.1, cooldown_sec=0., image_budget_bytes=self.frame.nbytes * 32)
        options.update(kwargs)
        recorder = AsyncDangerVideoRecorder(self.root, 10, (32, 24), **options)
        self.recorders.append(recorder)
        return recorder

    def feed(self, recorder, timeline, critical_indices):
        for i, timestamp in enumerate(timeline):
            recorder.on_frame(self.frame, timestamp, FatigueState.CRITICAL if i in critical_indices else FatigueState.SAFE, "logical-event")
            time.sleep(.002)

    def metadata(self):
        until(lambda: list(self.root.glob("**/metadata.json")))
        return [json.loads(path.read_text()) for path in self.root.glob("**/metadata.json")]

    def test_disabled_has_no_worker_arena_prebuffer_scan_or_copy(self):
        with patch("async_danger_recorder.Arena", side_effect=AssertionError("allocation")), \
             patch.object(Path, "rglob", side_effect=AssertionError("scan")):
            recorder = self.make(enabled=False)
            recorder.on_frame(self.frame, 100., FatigueState.CRITICAL)
            self.assertIsNone(recorder.channel)
            self.assertFalse(hasattr(recorder, "prebuffer"))
            self.assertEqual(recorder.snapshot()["copies"], 0)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_private_frame_survives_source_mutation(self):
        recorder = self.make(fault={"phase": "open", "delay_s": .2})
        self.frame.fill(120)
        self.feed(recorder, (100., 100.1, 100.2, 100.3, 100.4, 100.5), {2, 3})
        self.frame.fill(0)
        self.metadata()
        import cv2
        video = next(self.root.glob("**/video.mp4"))
        capture = cv2.VideoCapture(str(video))
        try:
            ok, decoded = capture.read()
            self.assertTrue(ok)
            self.assertGreater(decoded.mean(), 100.)
        finally:
            capture.release()

    def test_finalized_durable_file_can_have_incomplete_coverage(self):
        recorder = self.make(pre_roll_sec=0., image_budget_bytes=self.frame.nbytes * 3,
                             fault={"phase": "open", "delay_s": .3})
        self.feed(recorder, [100. + i * .1 for i in range(10)], {0, 1, 2, 3, 4, 5})
        metadata = self.metadata()[0]
        self.assertTrue(metadata["file_finalized"])
        self.assertFalse(metadata["coverage_complete"])
        self.assertGreater(metadata["dropped_frames"].get("full", 0), 0)
        until(lambda: recorder.snapshot().get("last_incident", {}).get("durably_published"))
        self.assertEqual(metadata["event_ids"], [])
        self.assertEqual(metadata["logical_event_ids"], ["logical-event"])

    def test_source_gap_and_missing_boundaries_are_independent(self):
        recorder = self.make(pre_roll_sec=.4, post_roll_sec=.2)
        self.feed(recorder, (100., 100.1, 100.7, 100.8, 101.2), {0, 1, 2})
        metadata = self.metadata()[0]
        self.assertIn("source_gap", metadata["coverage_reasons"])
        self.assertIn("missing_start", metadata["coverage_reasons"])
        self.assertIn("missing_end", metadata["coverage_reasons"])
        self.assertEqual(metadata["gap_count"], 1)
        self.assertAlmostEqual(metadata["max_gap_s"], .6)

    def test_variable_fps_successive_incidents_keep_source_windows_and_ids(self):
        recorder = self.make(post_roll_sec=.2)
        self.feed(recorder, (10., 10.07, 10.15, 10.23, 10.35, 10.41, 10.6, 10.67,
                             10.8, 10.92, 11.03, 11.15, 11.25, 11.4), {3, 4, 8, 9})
        until(lambda: len(list(self.root.glob("**/metadata.json"))) == 2)
        metadata = sorted(self.metadata(), key=lambda item: item["source_started_s"])
        self.assertNotEqual(metadata[0]["incident_id"], metadata[1]["incident_id"])
        self.assertAlmostEqual(metadata[0]["requested_window"]["end_s"], 10.61)
        self.assertAlmostEqual(metadata[1]["requested_window"]["end_s"], 11.23)
        self.assertAlmostEqual(metadata[0]["actual_last_source_s"], 10.6)
        for path in self.root.glob("**/timestamps.jsonl"):
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len({row["incident_id"] for row in rows}), 1)
            self.assertTrue(all(row["source_timestamp_s"] >= 10. for row in rows))

    def test_repeated_regressive_nonfinite_timestamps_do_not_extend_windows(self):
        recorder = self.make(pre_roll_sec=0.)
        self.feed(recorder, (100., 100., 99., float("nan"), 100.1, 100.2, 100.3), {0, 1, 2, 3})
        metadata = self.metadata()[0]
        self.assertEqual(metadata["dropped_frames"]["timestamp_invalid"], 3)
        self.assertAlmostEqual(metadata["source_finished_s"], 100.2)
        self.assertFalse(metadata["coverage_complete"])

    def test_process_death_retains_queued_frames_and_marks_recovery_incomplete(self):
        recorder = self.make(pre_roll_sec=0., fault={"phase": "write", "exit": True})
        self.feed(recorder, (100., 100.1, 100.2, 100.3), {0, 1})
        metadata = self.metadata()[0]
        self.assertIn("writer_interrupted", metadata["coverage_reasons"])
        self.assertEqual(metadata["consumed_frames"], metadata["admitted_frames"])
        self.assertTrue(list(self.root.glob("**/*.partial")))
        self.assertEqual(recorder.snapshot()["recovery_attempts"], 1)

    def test_ack_lost_after_video_publication_is_reconciled_without_duplicate(self):
        recorder = self.make(pre_roll_sec=0., fault={"phase": "video_ack", "exit": True})
        self.feed(recorder, (100., 100.1, 100.2, 100.3), {0, 1})
        until(lambda: recorder.snapshot().get("last_incident", {}).get("durably_published"), 6.)
        self.assertEqual(len(self.metadata()), 1)
        self.assertTrue(self.metadata()[0]["coverage_complete"])

    def test_final_publication_exception_reconciles_without_duplicate_video(self):
        recorder = self.make(pre_roll_sec=0., fault={"phase": "video_ack", "raise": True})
        self.feed(recorder, (100., 100.1, 100.2, 100.3), {0, 1})
        until(lambda: recorder.snapshot().get("last_incident", {}).get("durably_published"), 6.)
        self.assertEqual(len(self.metadata()), 1)
        self.assertEqual(recorder.snapshot()["recovery_attempts"], 0)

    def test_full_frame_arena_rejects_before_copy(self):
        recorder = self.make(pre_roll_sec=0., image_budget_bytes=self.frame.nbytes * 2,
                             fault={"phase": "open", "delay_s": .5})
        recorder.on_frame(self.frame, 100., FatigueState.CRITICAL)
        recorder.on_frame(self.frame, 100.1, FatigueState.CRITICAL)
        with patch("async_danger_recorder.np.copyto", side_effect=AssertionError("copy after saturation")):
            receipt = recorder.try_submit_frame(self.frame, 100.2, FatigueState.CRITICAL)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason, "full")
        self.assertEqual(recorder.copy_count, 2)

    def test_blocked_recorder_does_not_hold_logger_disk_locks(self):
        recorder = self.make(pre_roll_sec=0., fault={"phase": "open", "delay_s": 40.})
        recorder.on_frame(self.frame, 100., FatigueState.CRITICAL)
        until(lambda: recorder.snapshot().get("phase") == "open")
        logger = AsyncStructuredLogger(self.root / "logs", budget_bytes=1024 * 1024)
        try:
            receipt = logger.log_event("current_alert", {"source_timestamp_s": 100.})
            until(lambda: logger.receipt_state(receipt) == "persisted", 1.)
            self.assertEqual(recorder.snapshot()["phase"], "open")
        finally:
            logger.close(.5)

    def test_close_pending_does_not_claim_finalized_or_durable(self):
        recorder = self.make(pre_roll_sec=0., fault={"phase": "finalize", "delay_s": 40.})
        self.feed(recorder, (100., 100.1, 100.2, 100.3), {0, 1})
        until(lambda: recorder.snapshot().get("phase") == "finalize")
        started = time.monotonic()
        snapshot = recorder.close(.2)
        self.assertLess(time.monotonic() - started, .4)
        self.assertGreater(snapshot["unknown_outcomes"], 0)
        self.assertFalse(snapshot.get("last_incident", {}).get("durably_published", False))
        self.assertFalse(list(self.root.glob("**/metadata.json")))

    def test_unavailable_incident_uses_independent_event_channel(self):
        logger = AsyncStructuredLogger(self.root / "logs", budget_bytes=1024 * 1024)
        try:
            recorder = self.make(pre_roll_sec=0., event_logger=logger)
            from persistence_transport import RESERVED
            for i in range(recorder.controls.count):
                recorder.controls.set_header(i, RESERVED)
            recorder.on_frame(self.frame, 100., FatigueState.CRITICAL)
            self.assertIsNone(recorder.active)
            self.assertEqual(recorder.copy_count, 0)
            until(lambda: any(json.loads(path.read_text())["kind"] == "incident_video_unavailable"
                              for path in (self.root / "logs").glob("events/**/*.json")))
            self.assertIn("controls:full", recorder.snapshot()["dropped"])
            for i in range(recorder.controls.count):
                recorder.controls.set_header(i, FREE)
        finally:
            logger.close(.5)

    def test_boundary_and_gap_tolerances_are_configured_and_inclusive(self):
        recorder = self.make(pre_roll_sec=0., post_roll_sec=0., gap_tolerance_s=.75, boundary_tolerance_s=.05)
        self.feed(recorder, (100., 100.75, 101.5), {0, 1})
        metadata = self.metadata()[0]
        self.assertEqual(metadata["gap_count"], 0)
        self.assertTrue(metadata["coverage_complete"])
        self.assertEqual(metadata["gap_tolerance_s"], .75)
        self.assertEqual(metadata["boundary_tolerance_s"], .05)


class TestHealthSocket(unittest.TestCase):
    def test_abstract_socket_snapshot_needs_no_disk_or_queue(self):
        status = {"current": {"fatigue_state": "critical"}, "dropped": {"video:full": 42}}
        address = f"@salte-test-{os.getpid()}-{time.monotonic_ns()}"
        service = HealthSocket(address, lambda: status)
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1.)
                client.connect("\0" + address[1:])
                self.assertEqual(json.loads(client.recv(65536)), status)
        finally:
            service.close()
