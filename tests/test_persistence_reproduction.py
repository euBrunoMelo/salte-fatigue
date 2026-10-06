"""The same production-boundary reproduction runs before and after the change."""
import inspect
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import _bootstrap  # noqa
import run_host
from fatigue_runtime import FatigueRuntime
from feature_extractor_rt import RTFrameFeatures
from runtime_models import EyeFrameObservation, FatigueState


class TestPersistenceReproduction(unittest.TestCase):
    def test_publish_progresses_during_real_40_second_io_blocks(self):
        """Open/write/finalize recorder blocks, event write block, independent apps."""
        applications = []
        threads = []
        errors = []
        counts = {}
        phases = ("logger_event", "open", "write", "finalize")
        with tempfile.TemporaryDirectory() as tmp:
            for phase in phases:
                root = Path(tmp) / phase
                kwargs = {}
                asynchronous = "fault" in inspect.signature(run_host.StructuredLogger).parameters
                if asynchronous and phase == "logger_event":
                    kwargs["fault"] = {"phase": "event_write", "delay_s": 40.}
                slog = run_host.StructuredLogger(root / "logs", max_segment_age_s=.1, **kwargs)
                if not asynchronous and phase == "logger_event":
                    original = slog._write_atomic
                    def blocked(path, record, original=original):
                        time.sleep(40.)
                        return original(path, record)
                    slog._write_atomic = blocked
                kwargs = {"run_id": slog.run_id}
                async_recorder = "fault" in inspect.signature(run_host.DangerVideoRecorder).parameters
                if async_recorder and phase != "logger_event":
                    kwargs["fault"] = {"phase": phase, "delay_s": 40.}
                recorder = run_host.DangerVideoRecorder(root / "video", 30, (640, 480),
                                                        .2, .2, 0., phase != "logger_event", **kwargs)
                if not async_recorder and phase != "logger_event":
                    if phase == "open":
                        original = recorder._try_open_writer
                        def block_open(original=original):
                            time.sleep(40.)
                            return original()
                        recorder._try_open_writer = block_open
                    else:
                        original = recorder._open_codec
                        class Proxy:
                            def __init__(self, writer, phase):
                                self.writer, self.phase, self.fired = writer, phase, False
                            def write(self, frame):
                                if self.phase == "write" and not self.fired:
                                    self.fired = True
                                    time.sleep(40.)
                                return self.writer.write(frame)
                            def release(self):
                                if self.phase == "finalize" and not self.fired:
                                    self.fired = True
                                    time.sleep(40.)
                                return self.writer.release()
                        def open_proxy(filename, codec, original=original, phase=phase):
                            writer = original(filename, codec)
                            return Proxy(writer, phase) if writer is not None else None
                        recorder._open_codec = open_proxy
                app = run_host.RuntimeApplication.__new__(run_host.RuntimeApplication)
                app.slog, app.recorder = slog, recorder
                app.args = SimpleNamespace(display=False)
                app.frames_seen, app.last_health = 0, 10**20
                app.last_state, app.active_critical_event_id = FatigueState.UNKNOWN, None
                applications.append(app)
                counts[phase] = 0
                def produce(app=app, phase=phase):
                    try:
                        runtime = FatigueRuntime()
                        frame = np.zeros((480, 640, 3), np.uint8)
                        started = time.monotonic()
                        index = 0
                        while time.monotonic() - started < 43.:
                            source = 100. + index / 30.
                            ear = .3 if phase == "finalize" and index >= 65 else .1
                            assessment = runtime.evaluate(EyeFrameObservation(source, index, True, ear, ear))
                            features = RTFrameFeatures(source * 1000, index, ear, ear, True)
                            app._publish(frame, features, assessment)
                            counts[phase] += 1
                            index += 1
                            time.sleep(1. / 30.)
                    except Exception as exc:
                        errors.append(repr(exc))
                thread = threading.Thread(target=produce, daemon=True)
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join(50.)
            for app in applications:
                app.recorder.close()
                app.slog.close()
            self.assertFalse(errors, errors)
            for phase in phases:
                with self.subTest(phase=phase):
                    self.assertGreater(counts[phase], 1000, counts)


if __name__ == "__main__":
    unittest.main()
