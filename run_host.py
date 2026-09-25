"""Runtime deterministico EAR/PERCLOS para Raspberry Pi 5 + IMX500."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from attention_fsm import AttentionZoneConfig  # noqa: E402
from camera_routing import camera_index_for_id  # noqa: E402
from danger_video_recorder import DangerVideoRecorder  # noqa: E402
from eye_calibration import load_eye_calibration  # noqa: E402
from eye_closure import EyeClosureConfig  # noqa: E402
from eye_quality_gate import EyeQualityConfig  # noqa: E402
from fatigue_fsm import FatigueFsmConfig  # noqa: E402
from fatigue_runtime import FatigueRuntime, FatigueRuntimeConfig  # noqa: E402
from feature_extractor_rt import (  # noqa: E402
    MediaPipeBackend,
    RTExtractorConfig,
    RTFrameFeatures,
    RealTimeFeatureExtractor,
)
from perclos_rt import PerclosConfig  # noqa: E402
from observation_adapter import frame_features_to_observation  # noqa: E402
from runtime_models import (  # noqa: E402
    AttentionState,
    EyeCalibration,
    FatigueState,
    RuntimeAssessment,
)
from runtime_observability import (  # noqa: E402
    assessment_log_record,
    frame_log_record,
)
from structured_logger import (  # noqa: E402
    DEFAULT_MAX_SEGMENT_AGE_S,
    DEFAULT_MAX_SEGMENT_BYTES,
    StructuredLogger,
)

logger = logging.getLogger("SALTE.host")
FATIGUE_CAMERA_ID = "70000"


class CameraBackend:
    """Unifica picamera2, webcam OpenCV e replay de arquivo."""

    def __init__(
        self,
        width: int,
        height: int,
        fps: int,
        use_picamera: bool,
        video_path: Optional[Path] = None,
        loop_video: bool = True,
        camera_num: int = 1,
        camera_id_contains: str = FATIGUE_CAMERA_ID,
    ) -> None:
        self.width, self.height, self.fps = int(width), int(height), int(fps)
        self.camera_num = int(camera_num)
        self.camera_id_contains = camera_id_contains
        self._picam: Optional[object] = None
        self._cv2_cap: Optional[cv2.VideoCapture] = None
        self._is_video_file = video_path is not None
        self._loop_video = bool(loop_video)
        self._video_exhausted = False
        self._loop_count = 0
        self._on_loop: Optional[Callable[[int], None]] = None
        self._last_timestamp_s = time.monotonic()
        self._video_duration_s = 0.0
        if video_path is not None:
            self._open_video(video_path)
        elif use_picamera:
            self._open_picamera()
        else:
            self._open_webcam()

    def _open_video(self, video_path: Path) -> None:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Falha ao abrir video: {video_path}")
        file_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._video_duration_s = frame_count / file_fps if file_fps > 0.0 else 0.0
        self._cv2_cap = cap
        logger.info("Video aberto: %s, %.2f fps, %d frames", video_path, file_fps, frame_count)

    def _open_picamera(self) -> None:
        try:
            from picamera2 import Picamera2  # type: ignore
        except ImportError as exc:
            raise RuntimeError("picamera2 indisponivel; use --no-picamera") from exc
        camera_num = self.camera_num
        if self.camera_id_contains:
            camera_info = Picamera2.global_camera_info()
            camera_num = camera_index_for_id(camera_info, self.camera_id_contains)
            logger.info("Camera fisica %s no indice %d", camera_info[camera_num]["Id"], camera_num)
        picam = Picamera2(camera_num=camera_num)
        config = picam.create_video_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"},
            controls={"FrameRate": float(self.fps)},
        )
        picam.configure(config)
        picam.start()
        time.sleep(1.0)
        self._picam = picam

    def _open_webcam(self) -> None:
        cap = cv2.VideoCapture(self.camera_num)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if not cap.isOpened():
            raise RuntimeError(f"Falha ao abrir cv2.VideoCapture({self.camera_num})")
        self._cv2_cap = cap

    def set_loop_callback(self, callback: Callable[[int], None]) -> None:
        """Registra callback executado depois de cada rewind bem-sucedido."""
        self._on_loop = callback

    def is_exhausted(self) -> bool:
        """Informa EOF terminal ou falha ao reler depois de rewind."""
        return self._video_exhausted

    def timestamp_s(self) -> float:
        """Retorna PTS continuo para video ou monotonic para camera ao vivo."""
        return self._last_timestamp_s

    def _to_target_size(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        if (height, width) == (self.height, self.width):
            return frame
        return cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)

    def _set_capture_timestamp(self) -> None:
        cap = self._cv2_cap
        if not self._is_video_file or cap is None or not hasattr(cap, "get"):
            self._last_timestamp_s = time.monotonic()
            return
        pts_s = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
        duration = getattr(self, "_video_duration_s", 0.0)
        self._last_timestamp_s = pts_s + self._loop_count * duration

    def read(self) -> Optional[np.ndarray]:
        """Retorna frame BGR no tamanho-alvo ou None em falha/EOF."""
        if self._picam is not None:
            frame = self._picam.capture_array()  # type: ignore[attr-defined]
            self._last_timestamp_s = time.monotonic()
            return None if frame is None else self._to_target_size(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        if self._cv2_cap is None:
            return None
        ok, frame = self._cv2_cap.read()
        if ok:
            self._set_capture_timestamp()
            return self._to_target_size(frame)
        return self._handle_capture_failure()

    def _handle_capture_failure(self) -> Optional[np.ndarray]:
        if not self._is_video_file or self._cv2_cap is None:
            return None
        if not self._loop_video:
            self._video_exhausted = True
            return None
        self._cv2_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = self._cv2_cap.read()
        if not ok:
            self._video_exhausted = True
            return None
        self._loop_count += 1
        self._set_capture_timestamp()
        if self._on_loop is not None:
            self._on_loop(self._loop_count)
        return self._to_target_size(frame)

    def close(self) -> None:
        """Libera a fonte de captura."""
        if self._picam is not None:
            try:
                self._picam.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                self._picam.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        if self._cv2_cap is not None:
            self._cv2_cap.release()


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-model", default=str(HERE / "face_landmarker.task"))
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--camera-num", type=int, default=1)
    parser.add_argument(
        "--camera-id-contains", default=FATIGUE_CAMERA_ID,
        help="Trecho da ID fisica do libcamera; vazio usa --camera-num (diagnostico)",
    )
    parser.add_argument("--no-picamera", action="store_true")
    parser.add_argument("--video")
    parser.add_argument("--no-loop-video", action="store_true")
    parser.add_argument("--sync-fps-from-video", action="store_true")


def _add_detector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--calibration", default=str(HERE / "config" / "eye_calibration.json"))
    parser.add_argument("--fallback-ear-threshold", type=float, default=None)
    parser.add_argument("--p80-ratio", type=float, default=0.80)
    parser.add_argument("--blink-exclusion-ms", type=float, default=400.0)
    parser.add_argument("--prolonged-closure-ms", type=float, default=1000.0)
    parser.add_argument("--perclos-window-sec", type=float, default=60.0)
    parser.add_argument("--min-perclos-coverage", type=float, default=0.80)
    parser.add_argument("--warning-perclos", type=float, default=0.15)
    parser.add_argument("--critical-perclos", type=float, default=0.30)
    parser.add_argument("--critical-recovery-ms", type=float, default=500.0)


def _add_pose_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--enable-relative-pose-gate", action="store_true")
    parser.add_argument("--pose-pitch-max-delta", type=float, default=25.0)
    parser.add_argument("--pose-yaw-max-delta", type=float, default=30.0)
    parser.add_argument("--pose-roll-max-delta", type=float, default=25.0)
    parser.add_argument("--attention-pitch-max-delta", type=float)
    parser.add_argument("--attention-yaw-max-delta", type=float)
    parser.add_argument("--attention-roll-max-delta", type=float)
    parser.add_argument("--attention-distraction-ms", type=float, default=1000.0)
    parser.add_argument("--attention-recovery-ms", type=float, default=500.0)


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--display", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--logs-dir", default=str(HERE / "logs"))
    parser.add_argument("--log-frame-every", type=int, default=1)
    parser.add_argument("--log-assessment-every", type=int, default=1)
    parser.add_argument("--max-segment-age-s", type=float, default=DEFAULT_MAX_SEGMENT_AGE_S)
    parser.add_argument("--max-segment-bytes", type=int, default=DEFAULT_MAX_SEGMENT_BYTES)
    parser.add_argument("--danger-output-dir")
    parser.add_argument("--danger-pre-roll-sec", type=float, default=3.0)
    parser.add_argument("--danger-post-roll-sec", type=float, default=6.0)
    parser.add_argument("--danger-cooldown-sec", type=float, default=30.0)
    parser.add_argument("--no-danger-record", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    """Constroi a CLI do runtime EAR/PERCLOS."""
    parser = argparse.ArgumentParser(description="SALTE EAR/PERCLOS host")
    _add_source_args(parser)
    _add_detector_args(parser)
    _add_pose_args(parser)
    _add_output_args(parser)
    return parser


def _effective_fps(args: argparse.Namespace) -> int:
    if not args.video or not args.sync_fps_from_video:
        return int(args.fps)
    cap = cv2.VideoCapture(str(args.video))
    try:
        video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    finally:
        cap.release()
    return int(round(video_fps)) if video_fps > 1.0 else int(args.fps)


def _load_calibration(path_text: str) -> Optional[EyeCalibration]:
    path = Path(path_text)
    if not path.is_file():
        logger.warning("Calibracao ocular ausente em %s; detector nao arma sem fallback", path)
        return None
    try:
        return load_eye_calibration(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.error("Calibracao ocular invalida em %s: %s", path, exc)
        return None


def _attention_config(args: argparse.Namespace) -> Optional[AttentionZoneConfig]:
    values = (
        args.attention_pitch_max_delta,
        args.attention_yaw_max_delta,
        args.attention_roll_max_delta,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("Informe os tres deltas --attention-*-max-delta ou nenhum")
    return AttentionZoneConfig(*values, args.attention_distraction_ms, args.attention_recovery_ms)


def _runtime_config(args: argparse.Namespace) -> FatigueRuntimeConfig:
    quality = EyeQualityConfig(
        require_relative_pose=args.enable_relative_pose_gate,
        pitch_max_delta=args.pose_pitch_max_delta,
        yaw_max_delta=args.pose_yaw_max_delta,
        roll_max_delta=args.pose_roll_max_delta,
    )
    closure = EyeClosureConfig(
        args.p80_ratio, args.blink_exclusion_ms,
        args.prolonged_closure_ms, args.fallback_ear_threshold,
    )
    perclos = PerclosConfig(
        args.perclos_window_sec, args.min_perclos_coverage, args.p80_ratio,
        args.blink_exclusion_ms / 1000.0,
    )
    fatigue = FatigueFsmConfig(args.warning_perclos, args.critical_perclos, args.critical_recovery_ms)
    return FatigueRuntimeConfig(quality, closure, perclos, fatigue, _attention_config(args))


class RuntimeApplication:
    """Orquestra captura e componentes puros sem logica de classificacao."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.fps = _effective_fps(args)
        logs_root = Path(args.logs_dir)
        if not logs_root.is_absolute():
            logs_root = HERE / logs_root
        self.slog = StructuredLogger(
            logs_root,
            args.log_frame_every,
            args.log_assessment_every,
            args.max_segment_age_s,
            args.max_segment_bytes,
        )
        calibration = _load_calibration(args.calibration)
        self.runtime = FatigueRuntime(calibration, _runtime_config(args))
        self.backend = MediaPipeBackend(args.task_model, fps=self.fps)
        self.extractor = RealTimeFeatureExtractor(self.backend, RTExtractorConfig(fps=self.fps))
        self.camera = CameraBackend(
            args.width, args.height, self.fps, not args.no_picamera,
            Path(args.video) if args.video else None, not args.no_loop_video,
            args.camera_num, args.camera_id_contains,
        )
        self.camera.set_loop_callback(self._on_video_loop)
        output_dir = (
            Path(args.danger_output_dir)
            if args.danger_output_dir
            else logs_root / "incidents"
        )
        if args.danger_output_dir and not output_dir.is_absolute():
            output_dir = HERE / output_dir
        self.recorder = DangerVideoRecorder(
            output_dir,
            self.fps, (args.width, args.height), args.danger_pre_roll_sec,
            args.danger_post_roll_sec, args.danger_cooldown_sec, not args.no_danger_record,
            run_id=self.slog.run_id,
        )
        self.stop = False
        self.frames_seen = 0
        self.last_health = time.monotonic()
        self.last_frame_at = time.monotonic()
        self.last_state = FatigueState.UNKNOWN
        self.active_critical_event_id: Optional[str] = None

    def _on_video_loop(self, loop_index: int) -> None:
        self.runtime.reset()
        self.slog.log_event("video_loop", {"loop_index": loop_index})

    def request_stop(self, *_: object) -> None:
        """Solicita encerramento limpo no proximo ciclo."""
        self.stop = True

    def run(self) -> int:
        """Executa o loop ate sinal, tecla q ou EOF terminal."""
        self._log_start()
        try:
            while not self.stop:
                if not self._iteration():
                    break
        finally:
            self._close()
        return 0

    def _iteration(self) -> bool:
        frame = self.camera.read()
        if frame is None:
            if self.camera.is_exhausted():
                return False
            if not self.args.video and not self.args.no_picamera and time.monotonic() - self.last_frame_at > 10.0:
                raise RuntimeError("CAM/DISP1 sem frames por mais de 10 segundos")
            time.sleep(0.01)
            return True
        self.last_frame_at = time.monotonic()
        timestamp_s = self.camera.timestamp_s()
        features = self.extractor.process_frame(frame, timestamp_s)
        assessment = self.runtime.evaluate(frame_features_to_observation(features, timestamp_s))
        self._publish(frame, features, assessment)
        return not self.stop

    def _publish(
        self, frame: np.ndarray, features: RTFrameFeatures, assessment: RuntimeAssessment
    ) -> None:
        self.frames_seen += 1
        transition = self._prepare_state_change(assessment)
        if transition is not None:
            event_path = self._persist_state_change(assessment, transition)
            if event_path is None and transition[1] == FatigueState.CRITICAL:
                self.active_critical_event_id = None
        self.recorder.on_frame(
            frame.copy(),
            assessment.observation.timestamp_s,
            assessment.fatigue.state,
            self.active_critical_event_id,
        )
        self._log_frame(assessment)
        if self.args.display:
            _draw_hud(frame, features, assessment)
            cv2.imshow("SALTE EAR/PERCLOS", frame)
            self.stop = (cv2.waitKey(1) & 0xFF) == ord("q")
        self._health_log()

    def _log_frame(self, result: RuntimeAssessment) -> None:
        self.slog.log_frame_and_assessment(
            frame_log_record(result), assessment_log_record(result)
        )

    def _prepare_state_change(
        self, result: RuntimeAssessment
    ) -> Optional[tuple[FatigueState, FatigueState, str]]:
        state = result.fatigue.state
        if state == self.last_state:
            return None
        previous = self.last_state
        event_id = self.slog.new_event_id()
        if state == FatigueState.CRITICAL:
            self.active_critical_event_id = event_id
        elif previous == FatigueState.CRITICAL and self.active_critical_event_id is not None:
            event_id, self.active_critical_event_id = self.active_critical_event_id, None
        self.last_state = state
        return previous, state, event_id

    def _persist_state_change(
        self,
        result: RuntimeAssessment,
        transition: tuple[FatigueState, FatigueState, str],
    ) -> Optional[Path]:
        previous, state, event_id = transition
        logger.info("Fadiga: %s -> %s", previous.value, state.value)
        return self.slog.log_event("fatigue_state_change", {
            "previous": previous.value, "current": state.value,
            "frame_idx": result.observation.frame_idx,
        }, event_id=event_id)

    def _health_log(self) -> None:
        now = time.monotonic()
        if now - self.last_health < 10.0:
            return
        logger.info("health: %d frames, estado=%s", self.frames_seen, self.last_state.value)
        self.last_health = now

    def _log_start(self) -> None:
        fallback = self.args.fallback_ear_threshold
        self.slog.log_run_start({
            "fps": self.fps, "camera_num": self.args.camera_num,
            "video": self.args.video, "calibration": self.args.calibration,
            "calibration_loaded": self.runtime.calibration is not None,
            "fallback_ear_threshold": fallback, "p80_ratio": self.args.p80_ratio,
            "perclos_window_sec": self.args.perclos_window_sec,
        })
        logger.info("Runtime EAR/PERCLOS iniciado; calibracao=%s fallback=%s", self.runtime.calibration is not None, fallback)

    def _close(self) -> None:
        self.recorder.close()
        self.camera.close()
        self.backend.close()
        self.slog.log_run_end({
            "frames_seen": self.frames_seen, "fatigue_state": self.last_state.value,
        })
        self.slog.close()
        if self.args.display:
            cv2.destroyAllWindows()


def _draw_hud(frame: np.ndarray, features: RTFrameFeatures, result: RuntimeAssessment) -> None:
    state = result.fatigue.state
    colors = {
        FatigueState.UNKNOWN: (100, 100, 100), FatigueState.SAFE: (0, 180, 0),
        FatigueState.WARNING: (0, 180, 255), FatigueState.CRITICAL: (0, 0, 255),
    }
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), colors[state], -1)
    perclos = result.fatigue.perclos.value
    text = f"{state.value.upper()} P80={'N/A' if perclos is None else f'{perclos:.2f}'}"
    cv2.putText(frame, text, (10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    attention = result.attention.state
    eye_text = f"L={features.ear_l:.3f} R={features.ear_r:.3f} eyes={result.quality.valid_eye_count}"
    cv2.putText(frame, eye_text, (10, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.putText(frame, f"attention={attention.value}", (10, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)


def main(argv: Optional[list[str]] = None) -> int:
    """Valida argumentos, instala sinais e inicia a aplicacao."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    try:
        application = RuntimeApplication(args)
    except ValueError as exc:
        parser.error(str(exc))
    signal.signal(signal.SIGINT, application.request_stop)
    signal.signal(signal.SIGTERM, application.request_stop)
    return application.run()


if __name__ == "__main__":
    sys.exit(main())
