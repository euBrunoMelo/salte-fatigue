"""Private incident encoder/publication worker, independent of logger IO."""
from __future__ import annotations

import hashlib
import json
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path

from atomic_persistence import ensure_durable_directory, fsync_directory, fsync_file, publish_directory
from persistence_transport import Arena, BUSY, FREE, READY
from persistence_worker import Worker, memory_sample, reconcile_json


class RecorderWorker(Worker):
    def __init__(self, config):
        super().__init__(config)
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        self.images = Arena(config["image_size"], config["image_fd"])
        self.incident = None
        self.start_index = None
        self.writer = None
        self.sidecar = None
        self.attempt = 0
        self.state["baseline_memory"] = memory_sample()
        allowance = config.get("encoder_allowance_bytes", 64 * 1024 * 1024)
        if allowance <= 0:
            raise ValueError("invalid encoder memory allowance")
        with open("/proc/self/status") as source:
            virtual_bytes = next(int(line.split()[1]) * 1024 for line in source if line.startswith("VmSize:"))
        self.state["encoder_allowance_bytes"] = allowance
        resource.setrlimit(resource.RLIMIT_AS, (virtual_bytes + allowance, virtual_bytes + allowance))

    def start(self, index):
        lane = self.lanes["controls"]
        if self.claim(lane, index) is None:
            return
        self.start_index = index
        self.incident = json.loads(lane.read(index))
        self.phase("open")
        source_date = datetime.fromisoformat(self.incident["source_trigger_utc"])
        root = Path(self.config["output_dir"]) / source_date.astimezone(timezone.utc).strftime("%Y/%m/%d")
        ensure_durable_directory(root)
        artifact_id = f"{self.incident['incident_id']}-part{self.config['generation']}-{self.attempt}"
        self.partial = root / (artifact_id + ".partial")
        self.final = root / artifact_id
        if self.config["generation"] or self.attempt:
            published = sorted(root.glob(f"{self.incident['incident_id']}-part*"))
            self.final = next((path for path in published if not path.name.endswith(".partial")), self.final)
        # A lost durable ack is reconciled without re-encoding the same artifact.
        if self.final.exists():
            self.reconcile_final()
            return
        if self.partial.exists():
            self.attempt += 1
            self.partial = root / f"{artifact_id}-retry{self.attempt}.partial"
            self.final = self.partial.with_suffix("")
        self.partial.mkdir()
        fsync_directory(root)
        self.writer = None
        for codec, suffix in (("mp4v", ".mp4"), ("XVID", ".avi")):
            self.video = self.partial / ("video" + suffix)
            candidate = self.cv2.VideoWriter(str(self.video), self.cv2.VideoWriter_fourcc(*codec),
                                             self.config["fps"], tuple(self.config["frame_size"]))
            if candidate.isOpened():
                self.writer, self.codec = candidate, codec
                break
            candidate.release()
        if self.writer is None:
            raise OSError("no video encoder opened")
        self.sidecar = (self.partial / "timestamps.jsonl").open("x", encoding="utf8")
        self.timeline = {"first": None, "last": None, "count": 0, "gap_count": 0, "max_gap_s": 0.}

    def frame(self, index):
        lane = self.lanes["video"]
        claimed = self.claim(lane, index)
        if claimed is None:
            return
        sequence, ts, token = claimed
        if not self.incident or token != self.incident["token"]:
            # A control must precede every frame; unknown association is not encoded.
            self.retry(lane, index)
            raise ValueError("video frame has no matching incident")
        image = self.np.ndarray((self.config["frame_size"][1], self.config["frame_size"][0], 3),
                                dtype=self.np.uint8, buffer=self.images.memory,
                                offset=index * self.config["frame_bytes"])
        image.flags.writeable = False
        self.phase("write")
        self.writer.write(image)
        self.sidecar.write(json.dumps({"incident_id": self.incident["incident_id"],
                                      "frame_sequence": sequence, "source_timestamp_s": ts}) + "\n")
        timeline = self.timeline
        if timeline["last"] is not None:
            gap = ts - timeline["last"]
            timeline["max_gap_s"] = max(timeline["max_gap_s"], gap)
            if gap > self.config["gap_tolerance_s"] + 1e-9:
                timeline["gap_count"] += 1
        timeline["first"] = ts if timeline["first"] is None else timeline["first"]
        timeline["last"] = ts
        timeline["count"] += 1
        self.release(lane, index)  # consumed, explicitly not durable

    def end(self, index):
        lane = self.lanes["controls"]
        if self.claim(lane, index) is None:
            return
        end = json.loads(lane.read(index))
        if not self.incident or end["token"] != self.incident["token"]:
            self.retry(lane, index)
            raise ValueError("end control has no matching incident")
        self.phase("finalize")
        self.writer.release()
        self.writer = None
        self.sidecar.flush()
        os.fsync(self.sidecar.fileno())
        self.sidecar.close()
        self.sidecar = None
        fsync_file(self.video)
        # Limit validation decoder threads, preserving mp4v/XVID formats. Without
        # this FFmpeg may reserve hundreds of MB of thread stacks on many-core PCs.
        capture = self.cv2.VideoCapture(str(self.video), self.cv2.CAP_FFMPEG,
                                      [self.cv2.CAP_PROP_N_THREADS, 1])
        try:
            verified_frames = 0
            while capture.isOpened():
                readable, decoded = capture.read()
                if not readable or decoded is None or decoded.size == 0:
                    break
                verified_frames += 1
            valid = verified_frames > 0
        finally:
            capture.release()
        if not valid:
            raise OSError("encoded video is not readable")
        timeline = self.timeline
        reasons = list(end["drops"])
        if timeline["first"] is None:
            reasons.append("no_consumed_frames")
        else:
            if timeline["first"] - end["requested_start_s"] > self.config["boundary_tolerance_s"] + 1e-9:
                reasons.append("missing_start")
            if end["requested_end_s"] - timeline["last"] > self.config["boundary_tolerance_s"] + 1e-9:
                reasons.append("missing_end")
        if timeline["gap_count"]:
            reasons.append("source_gap")
        if timeline["count"] != end["admitted_frames"]:
            reasons.append("admitted_consumed_mismatch")
        if verified_frames != timeline["count"]:
            reasons.append("encoded_frame_count_mismatch")
        if self.config["generation"] != end["writer_generation_at_admission"] or self.attempt:
            reasons.append("writer_interrupted")
        if not end["accounting_known"]:
            reasons.append("accounting_unknown")
        complete = not reasons
        def digest(path):
            result = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(65536), b""):
                    result.update(block)
            return result.hexdigest()
        metadata = {"schema_version": 2, "incident_id": end["incident_id"],
                    "artifact_id": self.final.name, "run_id": self.config["run_id"],
                    "status": end["status"], "event_ids": [], "logical_event_ids": end["event_ids"],
                    "event_reference_status": "logical_unconfirmed", "started_at": end["source_trigger_utc"], "finished_at": end["source_end_utc"],
                    "source_started_s": end["trigger_s"], "source_finished_s": end["requested_end_s"],
                    "requested_window": {"start_s": end["requested_start_s"], "trigger_s": end["trigger_s"], "end_s": end["requested_end_s"]},
                    "actual_first_source_s": timeline["first"], "actual_last_source_s": timeline["last"],
                    "admitted_frames": end["admitted_frames"], "consumed_frames": timeline["count"], "verified_encoded_frames": verified_frames,
                    "admission": "accepted", "consumption": "encoding_submitted", "file_finalized": True,
                    "durable_confirmation": "worker_ack", "coverage_complete": complete,
                    "coverage_reasons": reasons, "dropped_frames": end["drops"],
                    "gap_count": timeline["gap_count"], "max_gap_s": timeline["max_gap_s"],
                    "gap_tolerance_s": self.config["gap_tolerance_s"], "boundary_tolerance_s": self.config["boundary_tolerance_s"],
                    "codec": self.codec, "video_file": self.video.name,
                    "sha256": {self.video.name: digest(self.video), "timestamps.jsonl": digest(self.partial / "timestamps.jsonl")}}
        reconcile_json(self.partial / "metadata.json", metadata)
        publish_directory(self.partial, self.final)
        self.phase("video_ack")
        self.confirm(index, metadata)

    def confirm(self, end_index, metadata):
        lane = self.lanes["controls"]
        self.state["last_incident"] = {"incident_id": metadata["incident_id"], "artifact_id": metadata["artifact_id"],
                                        "file_finalized": True, "durably_published": True,
                                        "coverage_complete": metadata["coverage_complete"], "path": str(self.final)}
        self.release(lane, self.start_index, True, self.final)
        self.release(lane, end_index, True, self.final)
        self.incident = self.start_index = None
        self.attempt = 0

    def reconcile_final(self):
        metadata = json.loads((self.final / "metadata.json").read_text())
        if metadata["incident_id"] != self.incident["incident_id"]:
            raise ValueError("immutable incident ID mismatch")
        for name, expected in metadata["sha256"].items():
            result = hashlib.sha256()
            with (self.final / name).open("rb") as source:
                for block in iter(lambda: source.read(65536), b""):
                    result.update(block)
            if result.hexdigest() != expected:
                raise ValueError("incident checksum mismatch")
        fsync_directory(self.final.parent)
        lane = self.lanes["controls"]
        end_index = next((i for i in range(lane.count) if lane.header(i)[0] == READY
                          and lane.header(i)[4] == self.incident["token"]), None)
        if end_index is None:
            raise ValueError("published incident has no retained end control")
        self.claim(lane, end_index)
        self.confirm(end_index, metadata)

    def run(self):
        while True:
            try:
                controls, video = self.lanes["controls"], self.lanes["video"]
                ci, vi = controls.oldest(), video.oldest()
                control_seq = controls.header(ci)[2] if ci is not None else float("inf")
                video_seq = video.header(vi)[2] if vi is not None else float("inf")
                if ci is not None and control_seq < video_seq:
                    payload = json.loads(controls.read(ci))
                    self.start(ci) if payload["operation"] == "start" else self.end(ci)
                elif vi is not None:
                    self.frame(vi)
                producer = self.producer()
                if producer.get("dropped") or producer.get("unknown_outcomes"):
                    summary = {key: producer.get(key) for key in ("dropped", "first_loss_utc", "last_loss_source_s", "unknown_outcomes")}
                    digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
                    if digest != self.loss_digest:
                        root = Path(self.config["output_dir"]) / "loss-audit"
                        path = root / f"{self.config['run_id']}-{digest[:24]}.json"
                        reconcile_json(path, {"schema_version": 1, "run_id": self.config["run_id"],
                                              "kind": "video_persistence_loss_summary", "data": summary})
                        self.loss_digest = digest
                        self.state["loss_summary"] = str(path)
                if self.producer().get("closing") and self.incident is None and ci is None and vi is None:
                    break
                self.state.update(state="ready", phase="idle", error=None)
                self.report()
                self.wait()
            except Exception as exc:
                # Failed attempts remain partial. Queued images stay owned; already
                # consumed images are unavailable and coverage records this fact.
                if self.writer is not None:
                    self.writer.release()
                    self.writer = None
                if self.sidecar is not None:
                    self.sidecar.close()
                    self.sidecar = None
                for lane in self.lanes.values():
                    for i in range(lane.count):
                        if lane.header(i)[0] == BUSY:
                            self.retry(lane, i)
                self.incident = self.start_index = None
                self.attempt += 1
                self.error(exc)
        self.state.update(state="drained", phase="closed")
        self.report()
