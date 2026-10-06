"""Local acceptance evidence: real 40 s double stall, memory and delivery CPU.

Run with the repository venv, without camera or deployment. Artifacts are local.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import cv2
from async_danger_recorder import AsyncDangerVideoRecorder
from async_structured_logger import AsyncStructuredLogger
from fatigue_runtime import FatigueRuntime
from feature_extractor_rt import RTFrameFeatures
from persistence_transport import HealthSocket
from run_host import RuntimeApplication
from runtime_models import EyeFrameObservation, FatigueState


def percentiles(values):
    values = np.asarray(values) * 1000.
    return {"p50_ms": float(np.percentile(values, 50)), "p95_ms": float(np.percentile(values, 95)),
            "p99_ms": float(np.percentile(values, 99)), "max_ms": float(values.max())}


def memory(pid):
    result = {}
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem"):
            result[key + "_kib"] = int(value.split()[0])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"host": platform.platform(), "python": sys.version,
              "numpy": np.__version__, "opencv": cv2.__version__, "scope": "PC local; no camera/MediaPipe/Pi"}
    with tempfile.TemporaryDirectory(prefix="salte-isolation-") as tmp:
        root = Path(tmp)
        frame = np.zeros((480, 640, 3), np.uint8)
        # Default 160 MiB budget and the complete prebuffer/copy path, 1000 calls.
        logger = AsyncStructuredLogger(root / "benchmark-logs")
        recorder = AsyncDangerVideoRecorder(root / "benchmark-video", 30, (640, 480),
                                            control_budget_bytes=logger.control_budget)
        time.sleep(.3)
        durations = []
        cpu = time.process_time()
        for i in range(1000):
            started = time.perf_counter()
            recorder.on_frame(frame, 100. + i / 30., FatigueState.SAFE)
            logger.log_frame_and_assessment({"frame_idx": i, "timestamp_s": 100. + i / 30.},
                                            {"frame_idx": i, "fatigue_state": "safe"})
            durations.append(time.perf_counter() - started)
        report["default_budget_delivery"] = {**percentiles(durations), "parent_cpu_seconds": time.process_time() - cpu,
                                               "calls": 1000, "copy_seconds": recorder.copy_seconds,
                                               "copies": recorder.copy_count, "record_budget_bytes": 32 * 1024 * 1024,
                                               "image_budget_bytes": recorder.images.size,
                                               "prebuffer_descriptors": len(recorder.prebuffer)}
        report["default_budget_memory"] = {"parent": memory(os.getpid()),
                                            "logger": memory(logger.channel.process.pid),
                                            "recorder": memory(recorder.channel.process.pid)}
        recorder.close(1.)
        logger.close(1.)

        # Smaller record budget forces both channels full; image arena stays at
        # default 128 MiB so 40 s cannot be hidden by enlarging video memory.
        logger = AsyncStructuredLogger(root / "logs", budget_bytes=1024 * 1024,
                                       max_segment_age_s=.1, fault={"phase": "write", "delay_s": 40.})
        recorder = AsyncDangerVideoRecorder(root / "video", 30, (640, 480), 3., .1, 0.,
                                            control_budget_bytes=logger.control_budget,
                                            fault={"phase": "write", "delay_s": 40.}, event_logger=logger,
                                            run_id=logger.run_id)
        app = RuntimeApplication.__new__(RuntimeApplication)
        app.slog, app.recorder = logger, recorder
        app.args = SimpleNamespace(display=False)
        app.frames_seen, app.last_health = 0, 10**20
        app.last_state, app.active_critical_event_id = FatigueState.UNKNOWN, None
        app.local_status = {"frames_seen": 0, "fatigue_state": "unknown"}
        address = f"@salte-acceptance-{os.getpid()}"
        service = HealthSocket(address, app.persistence_snapshot)
        runtime = FatigueRuntime()
        samples, occupancies, snapshots = [], [], []
        def feed(index, ear):
            ts = 100. + index / 30.
            result = runtime.evaluate(EyeFrameObservation(ts, index, True, ear, ear))
            features = RTFrameFeatures(ts * 1000., index, ear, ear, True)
            started = time.perf_counter()
            app._publish(frame, features, result)
            samples.append(time.perf_counter() - started)
        for i in range(90):
            feed(i, .3)
        for i in range(25):
            logger.log_event("queued_before_loss", {"ordinal": i, "source_timestamp_s": 100.})
            logger.channel.submit("console", {"source_utc": logger.timestamp(), "message": "historical admission probe", "historical": True})
        started, cpu = time.monotonic(), time.process_time()
        index = 90
        next_tick = started
        sample_second = -1
        while time.monotonic() - started < 44.:
            elapsed = time.monotonic() - started
            feed(index, .3 if elapsed >= 42. else .1)
            index += 1
            second = int(elapsed)
            if second != sample_second:
                sample_second = second
                snapshot = app.persistence_snapshot()
                snapshots.append({"elapsed_s": elapsed, **snapshot})
                occupancies.append(snapshot["recorder"]["image_reserved_bytes"])
                if second in (10, 20, 30):
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(.5)
                        client.connect("\0" + address[1:])
                        response = json.loads(client.recv(65536))
                        assert response["current"]["fatigue_state"] == "critical"
                        assert response["current"]["frames_seen"] > 300
                if second == 30:
                    report["stalled_memory"] = {"parent": memory(os.getpid()),
                                                 "logger": memory(logger.channel.process.pid),
                                                 "recorder": memory(recorder.channel.process.pid)}
            next_tick += 1. / 30.
            time.sleep(max(0., next_tick - time.monotonic()))
        parent_cpu = time.process_time() - cpu
        service.close()
        recorder.close(2.)
        logger.log_run_end({"frames_seen": app.frames_seen})
        logger.close(2.)
        final = app.persistence_snapshot()
        assert final["logger"]["dropped"].get("telemetry:full", 0) > 0
        assert final["recorder"]["dropped"].get("video:full", 0) > 0
        assert max(occupancies) <= 128 * 1024 * 1024
        assert any(snapshot["logger"]["state"] == "stalled" and snapshot["recorder"]["state"] == "stalled" for snapshot in snapshots)
        assert final["logger"]["last_durable_ack"] is not None
        assert final["recorder"]["last_incident"]["durably_published"]
        assert not final["recorder"]["last_incident"]["coverage_complete"]
        report["double_40_second_stall"] = {**percentiles(samples), "frames_seen": app.frames_seen,
                                            "parent_cpu_seconds": parent_cpu, "wall_seconds": 44.,
                                            "peak_reserved_image_bytes": max(occupancies), "final": final}
        (args.output / "health-timeline.json").write_text(json.dumps(snapshots, indent=2) + "\n")
        metadata = [json.loads(path.read_text()) for path in (root / "video").glob("**/metadata.json")]
        manifests = [json.loads(path.read_text()) for path in (root / "logs").glob("**/manifest.json")]
        losses = [json.loads(path.read_text()) for path in root.glob("**/*.json") if "loss" in path.name]
        (args.output / "serialized-artifacts.json").write_text(json.dumps({"metadata": metadata, "manifests": manifests, "loss_summaries": losses}, indent=2) + "\n")
    (args.output / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
