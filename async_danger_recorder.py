"""Source-time incident FSM and admission. All image/encoder IO is in a child."""
from __future__ import annotations

import json
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from danger_video_recorder import RecorderState
from atomic_persistence import validate_persistence_id
from persistence_transport import (Arena, BUSY, FREE, HEADER_SIZE, HEALTH_SIZE, Lane,
                                   PRE, READY, RESERVED, ProcessChannel, make_lanes)
from persistence_transport import Receipt


class AsyncDangerVideoRecorder:
    def __init__(self, output_dir: Path, fps, frame_size, pre_roll_sec=3.,
                 post_roll_sec=6., cooldown_sec=30., enabled=True, *, run_id="unscoped",
                 image_budget_bytes=128 * 1024 * 1024, control_budget_bytes=512 * 1024,
                 gap_tolerance_s=.5, boundary_tolerance_s=None, fault=None, event_logger=None,
                 encoder_allowance_bytes=64 * 1024 * 1024):
        self.enabled, self.state = enabled, RecorderState.MONITORING
        self.channel = None
        self.pre_roll_sec, self.post_roll_sec, self.cooldown_sec = pre_roll_sec, post_roll_sec, cooldown_sec
        self.frame_size, self.fps = tuple(frame_size), fps
        self.event_logger = event_logger
        self.active = None
        self.last_source = None
        self.post_end = self.cooldown_end = None
        self.pre_loss_time = None
        self.pre_loss_count = 0
        self.copy_count = 0
        self.copy_seconds = 0.
        self._closed = False
        # Disabled really means no process, pool, deque, scan or frame copy.
        if not enabled:
            return
        if (fps <= 0 or min(*frame_size) <= 0
                or any(not math.isfinite(value) or value < 0 for value in (pre_roll_sec, post_roll_sec, cooldown_sec, gap_tolerance_s))):
            raise ValueError("invalid recorder configuration")
        validate_persistence_id(run_id)
        self.boundary = 1. / fps if boundary_tolerance_s is None else boundary_tolerance_s
        if not math.isfinite(self.boundary) or self.boundary < 0:
            raise ValueError("invalid boundary tolerance")
        self.frame_bytes = frame_size[0] * frame_size[1] * 3
        count = image_budget_bytes // self.frame_bytes
        descriptor_bytes = count * 128  # bounded Python prebuffer descriptors, including timestamps
        if count < 2:
            raise ValueError("image budget must hold at least two frames")
        if control_budget_bytes < count * HEADER_SIZE + descriptor_bytes + 2 * HEALTH_SIZE + 128 * 1024 + 2 * 4128:
            raise ValueError("record budget too small for frame indices and recorder controls")
        self.images = Arena(image_budget_bytes)
        self.control = Arena(control_budget_bytes)
        self.frames = Lane(self.control, 0, count, 0, "video")
        available = control_budget_bytes - count * HEADER_SIZE - descriptor_bytes - 2 * HEALTH_SIZE - 128 * 1024
        self.controls = make_lanes(self.control, [("controls", available, 256, 4096)], count * HEADER_SIZE)["controls"]
        self.prebuffer = deque()  # at most count descriptors, no Python-owned images
        self.channel = ProcessChannel("recorder", self.control, {"controls": self.controls, "video": self.frames}, {
            "output_dir": str(output_dir), "run_id": run_id, "fps": fps,
            "frame_size": frame_size, "frame_bytes": self.frame_bytes,
            "image_fd": self.images.fd, "image_size": image_budget_bytes,
            "gap_tolerance_s": gap_tolerance_s, "boundary_tolerance_s": self.boundary,
            "fault": fault,
            "encoder_allowance_bytes": encoder_allowance_bytes,
        }, extra_fds=(self.images.fd,))

    def _loss(self, reason, ts):
        receipt = self.channel.loss("video", reason, ts)
        if self.active:
            drops = self.active["drops"]
            drops[reason] = drops.get(reason, 0) + 1
        else:
            if self.pre_loss_time is None or ts - self.pre_loss_time > self.pre_roll_sec:
                self.pre_loss_count = 0
            self.pre_loss_time = ts
            self.pre_loss_count += 1
        return receipt

    def _expire(self, ts):
        while self.prebuffer and self.prebuffer[0][1] < ts - self.pre_roll_sec - 1e-9:
            index, _ = self.prebuffer.popleft()
            # Producer is sole owner of PRE slots. Expiry is not an admission loss.
            self.frames.set_header(index, FREE)

    def _copy(self, frame, ts, token=0):
        if not self.control.try_lock():
            return self._loss("contention", ts)
        try:
            index = self.frames.free_index()
            if index is None:
                return self._loss("full", ts)
            started = time.perf_counter()
            target = np.ndarray((self.frame_size[1], self.frame_size[0], 3), dtype=np.uint8,
                                buffer=self.images.memory, offset=index * self.frame_bytes)
            np.copyto(target, frame, casting="no")
            self.copy_seconds += time.perf_counter() - started
            self.copy_count += 1
            sequence = self.channel.next_sequence()
            self.frames.set_header(index, READY if token else PRE, self.frame_bytes, sequence, ts, token)
            if not token:
                self.prebuffer.append((index, ts))
        finally:
            self.control.unlock()
        self.channel.wake()
        return Receipt(True, "video", sequence, self.active["incident_id"] if token else None)

    def _start(self, ts, event_id):
        incident_id = uuid.uuid4().hex
        if not self.control.try_lock():
            self._unavailable(incident_id, ts, "contention")
            return
        try:
            first = self.controls.free_index()
            if first is None:
                self._unavailable(incident_id, ts, "full")
                return
            self.controls.set_header(first, RESERVED)
            end = self.controls.free_index()
            if end is None:
                self.controls.set_header(first, FREE)
                self._unavailable(incident_id, ts, "full")
                return
            self.controls.set_header(end, RESERVED)
            token = self.channel.next_sequence()
            self.active = {"incident_id": incident_id, "token": token, "start_slot": first,
                           "end_slot": end, "trigger_s": ts, "requested_start_s": ts - self.pre_roll_sec,
                           "source_trigger_utc": datetime.now(timezone.utc).isoformat(),
                           "writer_generation_at_admission": self.channel.generation,
                           "drops": {}, "event_ids": [event_id] if event_id else [],
                           "event_reference_status": "logical_unconfirmed", "admitted_frames": 0}
            if self.pre_loss_time is not None and self.pre_loss_time >= ts - self.pre_roll_sec:
                self.active["drops"]["prebuffer_admission"] = self.pre_loss_count
            payload = {"operation": "start", **self.active}
            self.controls.write(first, json.dumps(payload).encode(), token, ts, token)
            for index, frame_ts in self.prebuffer:
                self.frames.set_header(index, READY, self.frame_bytes, self.channel.next_sequence(), frame_ts, token)
                self.active["admitted_frames"] += 1
            self.prebuffer.clear()
        finally:
            self.control.unlock()
        self.channel.wake()

    def _unavailable(self, incident_id, ts, reason):
        self.channel.loss("controls", reason, ts)
        if self.event_logger:
            self.event_logger.log_event("incident_video_unavailable", {
                "incident_id": incident_id, "source_timestamp_s": ts, "reason": reason,
            })

    def _end(self, requested_end, status="completed"):
        if self.active:
            # END capacity was reserved at START. Accepted frames are never evicted.
            payload = {"operation": "end", **self.active, "requested_end_s": requested_end,
                       "source_end_utc": datetime.now(timezone.utc).isoformat(),
                       "status": status, "accounting_known": status == "completed"}
            self.controls.write(self.active["end_slot"], json.dumps(payload).encode(),
                                self.channel.next_sequence(), requested_end, self.active["token"])
            self.active = None
            self.channel.wake()

    def on_frame(self, frame, timestamp_s, fatigue_state, event_id=None):
        if not self.enabled or self._closed:
            return Receipt(False, "video", reason="disabled" if not self.enabled else "closed")
        if event_id is not None:
            validate_persistence_id(event_id)
        ts = float(timestamp_s)
        if not math.isfinite(ts) or (self.last_source is not None and ts <= self.last_source):
            return self._loss("timestamp_invalid", self.last_source or 0.)
        self.last_source = ts
        if frame.dtype != np.uint8 or frame.shape != (self.frame_size[1], self.frame_size[0], 3):
            return self._loss("shape", ts)
        self._expire(ts)
        critical = getattr(fatigue_state, "value", fatigue_state) == "critical"
        if self.state == RecorderState.COOLDOWN and ts >= self.cooldown_end:
            self.state = RecorderState.MONITORING
        if self.state == RecorderState.POST_ROLL and not critical and ts > self.post_end + 1e-9:
            self._end(self.post_end)
            self.state = RecorderState.COOLDOWN
            self.cooldown_end = ts + self.cooldown_sec
        if self.state == RecorderState.MONITORING and critical:
            self._start(ts, event_id)
            self.state = RecorderState.RECORDING_ACTIVE
        if self.state in (RecorderState.MONITORING, RecorderState.COOLDOWN):
            return self._copy(frame, ts)
        if self.active:
            if event_id and event_id not in self.active["event_ids"]:
                if len(self.active["event_ids"]) < 16:
                    self.active["event_ids"].append(event_id)
                else:
                    self._loss("event_reference_limit", ts)
            receipt = self._copy(frame, ts, self.active["token"])
            if receipt:
                self.active["admitted_frames"] += 1
        else:
            receipt = self._loss("incident_unavailable", ts)
        if critical:
            self.state = RecorderState.RECORDING_ACTIVE
            self.post_end = None
        elif self.state == RecorderState.RECORDING_ACTIVE:
            self.state = RecorderState.POST_ROLL
            self.post_end = ts + self.post_roll_sec
        if self.state == RecorderState.POST_ROLL and ts >= self.post_end:
            self._end(self.post_end)
            self.state = RecorderState.COOLDOWN
            self.cooldown_end = ts + self.cooldown_sec
        return receipt

    try_submit_frame = on_frame

    def snapshot(self):
        if hasattr(self, "closed_snapshot"):
            return self.closed_snapshot
        if not self.enabled:
            return {"state": "disabled", "image_capacity_bytes": 0, "copies": 0}
        result = self.channel.snapshot()
        result.update({"fsm": self.state.name, "image_capacity_bytes": self.images.size,
                       "active_incident_id": self.active["incident_id"] if self.active else None,
                       "image_reserved_bytes": self.frames.occupancy()["count"] * self.frame_bytes,
                       "copies": self.copy_count, "copy_seconds": self.copy_seconds})
        return result

    def request_close(self):
        if not self.enabled or self._closed:
            return
        self._closed = True
        self._end(self.last_source or 0., "aborted")
        for index, _ in self.prebuffer:
            self.frames.set_header(index, FREE)
        self.prebuffer.clear()
        self.channel.request_close()

    def close(self, deadline_seconds=5.):
        if hasattr(self, "closed_snapshot"):
            return self.closed_snapshot
        if not self.enabled:
            return self.snapshot()
        self.request_close()
        self.channel.close(deadline_seconds)
        self.closed_snapshot = self.snapshot()
        self.images.close()
        self.channel.dispose()
        return self.closed_snapshot
