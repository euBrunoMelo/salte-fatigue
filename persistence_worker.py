"""Private disk/encoder processes. Never import or execute inference here."""
from __future__ import annotations

import hashlib
import ctypes
import json
import os
import select
import signal
import socket
import struct
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from atomic_persistence import (atomic_write_json, ensure_durable_directory,
                                fsync_directory, fsync_file, publish_directory)
from persistence_transport import (Arena, BUSY, FREE, Lane, READY, read_health, write_health)
from structured_logger import _ActiveSegment, _encode_jsonl


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            result.update(block)
    return result.hexdigest()


def memory_sample():
    values = {}
    with open("/proc/self/smaps_rollup") as source:
        for line in source:
            key, _, value = line.partition(":")
            if key in ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem"):
                values[key + "_kib"] = int(value.split()[0])
    return values


def reconcile_json(path, record):
    """An existing immutable ID must match exactly, then re-sync before ack."""
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise FileExistsError(f"immutable ID conflict: {path}")
        fsync_file(path)
        fsync_directory(path.parent)
    else:
        atomic_write_json(path, record)


class Worker:
    def __init__(self, config):
        self.config = config
        self.arena = Arena(config["arena_size"], config["arena_fd"])
        self.lanes = {name: Lane(self.arena, *description, name) for name, description in config["lanes"].items()}
        self.notify = socket.socket(fileno=config["notify_fd"])
        self.notify.setblocking(False)
        self.state = {"state": "starting", "heartbeat": time.monotonic(), "phase": "initializing",
                      "durable": {}, "consumed": {}, "last_durable_ack": None, "error": None,
                      "generation": config["generation"], "loss_summary": None}
        self.fault_fired = False
        self.io_recovery_attempts = 0
        self.loss_digest = None
        self.report()

    def report(self):
        self.state["heartbeat"] = time.monotonic()
        # Reading procfs is child-only; consumers never need disk/procfs for status.
        if time.monotonic() - getattr(self, "last_memory_sample", 0.) > 1.:
            self.state["memory"] = memory_sample()
            self.last_memory_sample = time.monotonic()
        write_health(self.arena, self.config["health_offset"], self.state)

    def phase(self, phase):
        self.state.update(phase=phase, state="working")
        self.report()
        fault = self.config.get("fault") or {}
        if (fault.get("phase") == phase and (not self.fault_fired or fault.get("repeat"))
                and fault.get("generation", 0) in (self.config["generation"], "all")):
            self.fault_fired = True
            # Internal injection, not a CLI option; used by real-process tests.
            time.sleep(fault.get("delay_s", 0.))
            if fault.get("exit"):
                os._exit(71)
            if fault.get("raise"):
                raise OSError("injected persistence failure")

    def producer(self):
        result = read_health(self.arena, self.config["producer_offset"])
        result["closing"] = bool(struct.unpack_from("<I", self.arena.memory, self.config["producer_offset"] + 12)[0])
        return result

    def claim(self, lane, index):
        if index is None or not self.arena.try_lock():
            return None
        try:
            state, length, sequence, ts, token = lane.header(index)
            if state != READY:
                return None
            lane.set_header(index, BUSY, length, sequence, ts, token)
            return sequence, ts, token
        finally:
            self.arena.unlock()

    def release(self, lane, index, durable=False, path=None):
        sequence = lane.header(index)[2]
        self.state["consumed"][lane.name] = sequence
        if durable:
            self.state["durable"][lane.name] = sequence
            self.state["last_durable_ack"] = {"category": lane.name, "sequence": sequence,
                                               "path": str(path), "confirmed_utc": utc_now()}
        # Producer only claims FREE; single writer owns BUSY, no lock needed here.
        self.report()  # confirmation precedes reuse
        lane.set_header(index, FREE)

    def retry(self, lane, index):
        state, length, sequence, ts, token = lane.header(index)
        lane.set_header(index, READY, length, sequence, ts, token)

    def wait(self, delay=.01):
        ready, _, _ = select.select([self.notify], [], [], delay)
        if ready:
            for _ in range(64):
                try:
                    self.notify.recv(16)
                except BlockingIOError:
                    break

    def error(self, exc):
        self.state.update(state="failed", error=str(exc)[:512])
        self.report()
        if self.io_recovery_attempts < 2:
            delay = (1., 5.)[self.io_recovery_attempts]
            self.io_recovery_attempts += 1
            self.state["io_recovery_attempts"] = self.io_recovery_attempts
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not self.producer().get("closing"):
                self.wait(.05)
            return
        self.state["recovery_exhausted"] = True
        self.report()
        # Keep admitted data in RAM, no limitless file creation or replacements.
        while not self.producer().get("closing"):
            self.wait(.1)
        os._exit(72)


class LoggerWorker(Worker):
    def __init__(self, config):
        super().__init__(config)
        self.root = Path(config["logs_dir"])
        started = datetime.fromisoformat(config["started_utc"].replace("Z", "+00:00"))
        self.run_dir = self.root / "runs" / started.strftime("%Y/%m/%d") / config["run_id"]
        self.segments = self.run_dir / "segments"
        self.active = None
        self.held = []
        self.initialized = False

    def layout(self):
        self.phase("open")
        ensure_durable_directory(self.segments)
        self.initialized = True

    def event(self, index):
        lane = self.lanes["events"]
        payload = json.loads(lane.read(index))
        operation = payload["operation"]
        if operation == "run-end":
            telemetry = self.lanes["telemetry"]
            earlier = any(telemetry.header(i)[0] != FREE and telemetry.header(i)[2] <= payload["barrier"]
                          for i in range(telemetry.count))
            if earlier:
                if self.held:
                    self.publish_segment()
                return False
        if self.claim(lane, index) is None:
            return False
        try:
            record = payload["record"]
            if operation == "event":
                source = datetime.fromisoformat(record["ts_utc"].replace("Z", "+00:00"))
                path = self.root / "events" / source.strftime("%Y/%m/%d") / (record["record_id"] + ".json")
            else:
                path = self.run_dir / (operation + ".json")
            self.phase("event_write")
            reconcile_json(path, record)
            self.phase("event_ack")
            self.release(lane, index, True, path)
            return True
        except Exception:
            self.retry(lane, index)
            raise

    def append(self, index):
        lane = self.lanes["telemetry"]
        payload = json.loads(lane.read(index))
        frame, assessment = payload["frame"], payload["assessment"]
        frame_line, assessment_line = _encode_jsonl(frame) if frame else None, _encode_jsonl(assessment) if assessment else None
        incoming_bytes = len((frame_line or "").encode()) + len((assessment_line or "").encode())
        if self.active and self.active.byte_count + incoming_bytes > self.config["max_segment_bytes"]:
            self.publish_segment()
        claimed = self.claim(lane, index)
        if claimed is None:
            return
        sequence = claimed[0]
        self.held.append(index)
        if self.active is None:
            self.phase("segment_open")
            self.active = _ActiveSegment(self.segments, self.config["run_id"], sequence, time.monotonic())
            # Interrupted attempts never share or overwrite a partial directory.
        self.phase("write")
        self.active.append(int((frame or assessment)["frame_idx"]),
                           frame_line, assessment_line)
        self.state["consumed"]["telemetry"] = sequence
        self.report()

    def publish_segment(self):
        if not self.active:
            return
        self.phase("finalize")
        segment = self.active
        lane = self.lanes["telemetry"]
        segment._close_sinks()
        sequences = [lane.header(index)[2] for index in self.held]
        manifest = {**segment._manifest(utc_now()), "transport_sequences": sequences,
                    "admission": "accepted", "consumption": "written",
                    "publication": "finalized", "durable_confirmation": "worker_ack"}
        hashes = {name: file_digest(segment.partial_path / name)
                  for name in ("frames.jsonl", "assessments.jsonl")}
        manifest["sha256"] = hashes
        reconcile_json(segment.partial_path / "manifest.json", manifest)
        publish_directory(segment.partial_path, segment.final_path)
        self.phase("segment_ack")
        for index in self.held:
            self.release(lane, index, True, segment.final_path)
        self.active, self.held = None, []

    def recover_segments(self):
        # Reconcile an ack lost after rename/fsync; never append replay to a final.
        lane = self.lanes["telemetry"]
        for path in self.segments.iterdir():
            if not path.is_dir() or path.name.endswith(".partial"):
                continue
            manifest = json.loads((path / "manifest.json").read_text())
            sequences = set(manifest.get("transport_sequences", ()))
            if not sequences:
                continue
            for name, expected in manifest.get("sha256", {}).items():
                if file_digest(path / name) != expected:
                    raise ValueError("segment checksum mismatch")
            fsync_directory(path.parent)
            for i in range(lane.count):
                if lane.header(i)[0] == READY and lane.header(i)[2] in sequences:
                    if self.claim(lane, i):
                        self.release(lane, i, True, path)
        # Keep interrupted partials intact; next attempt gets a fresh path.
        for path in self.segments.glob("*.partial"):
            # Numeric sequence directory is reserved, rename within partial namespace.
            if path.name[:-8].isdigit():
                path.rename(path.with_name(path.stem + "-interrupted-" + uuid.uuid4().hex + ".partial"))
                fsync_directory(self.segments)

    def loss_summary(self):
        producer = self.producer()
        if not producer.get("dropped") and not producer.get("unknown_outcomes"):
            return
        summary = {key: producer.get(key) for key in ("dropped", "first_loss_utc", "last_loss_source_s", "unknown_outcomes")}
        digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
        if digest == self.loss_digest:
            return
        record_id = f"{self.config['run_id']}-loss-{digest[:24]}"
        source = datetime.fromisoformat(self.config["started_utc"].replace("Z", "+00:00"))
        path = self.root / "events" / source.strftime("%Y/%m/%d") / (record_id + ".json")
        record = {"schema_version": 1, "record_id": record_id, "event_id": record_id,
                  "run_id": self.config["run_id"], "ts_utc": summary["first_loss_utc"] or self.config["started_utc"],
                  "kind": "persistence_loss_summary", "data": {**summary, "historical": True}}
        reconcile_json(path, record)
        self.loss_digest = digest
        self.state["loss_summary"] = str(path)

    def run(self):
        while True:
            try:
                if not self.initialized:
                    self.layout()
                    self.recover_segments()
                events, telemetry, console = (self.lanes[key] for key in ("events", "telemetry", "console"))
                index = events.oldest()
                if index is not None:
                    self.event(index)
                index = console.oldest()
                if index is not None and self.claim(console, index):
                    try:
                        payload = json.loads(console.read(index))
                        self.phase("console_write")
                        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
                        self.release(console, index)
                    except Exception:
                        self.retry(console, index)
                        raise
                index = telemetry.oldest()
                if index is not None:
                    self.append(index)
                if self.active and (len(self.held) >= min(256, max(1, telemetry.count // 2)) or self.active.byte_count >= self.config["max_segment_bytes"]
                                    or time.monotonic() - self.active.opened_at_s >= self.config["max_segment_age_s"]
                                    or self.producer().get("closing")):
                    self.publish_segment()
                self.loss_summary()
                if self.producer().get("closing") and all(lane.occupancy()["count"] == 0 for lane in self.lanes.values()):
                    break
                self.state.update(state="ready", phase="idle", error=None)
                self.report()
                self.wait()
            except Exception as exc:
                if self.active:
                    # Never duplicate a partially written line on retry.
                    try:
                        for sink in (self.active._frames, self.active._assessments):
                            sink.close()
                    except Exception:
                        pass
                    self.active = None
                for i in self.held:
                    self.retry(self.lanes["telemetry"], i)
                self.held = []
                self.initialized = False
                self.error(exc)
        self.state.update(state="drained", phase="closed")
        self.report()



def main():
    config = json.loads(os.pread(int(sys.argv[1]), 65536, 0))
    # Set in the child, never preexec_fn in the threaded camera process.
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG")
    if os.getppid() != config["parent_pid"]:
        return
    if config["kind"] == "logger":
        worker = LoggerWorker(config)
    else:
        from recorder_persistence_worker import RecorderWorker
        worker = RecorderWorker(config)
    worker.run()


if __name__ == "__main__":
    main()
